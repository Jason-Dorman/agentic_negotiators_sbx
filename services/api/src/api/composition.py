"""The backend's composition root: settings in, a run controller out.

Concrete classes are chosen here and nowhere else (docs/contributing.md section 2.1). Stage 2.5's
application factory calls `build_controller` once at start-up and then `RunController.recover`;
the integration suite builds the same graph through the same function, with the agents served
in-process through `transports`.

Keys arrive as references and are resolved by the relay's signer, never held here (ADR-049).
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import httpx

from api.agent_client import HttpAgentClient
from api.chain import ChainAdapter, ExchangeCodec, Web3ChainAdapter
from api.config import ChainSettings, ControllerSettings
from api.controller import RunController
from api.db import Database, DeploymentRecord, Party
from api.indexer import Indexer
from api.projection import TimelineSentences
from api.relay import LocalTransactionSigner, Relay


@dataclass(frozen=True, slots=True)
class Backend:
    controller: RunController
    agents: Mapping[Party, HttpAgentClient]

    async def aclose(self) -> None:
        await self.controller.aclose()
        for agent in self.agents.values():
            await agent.aclose()


def holder_id() -> str:
    """This process, as a lease holder: host, process and a nonce, so a restart is a new holder."""
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def build_controller(
    database: Database,
    deployment: DeploymentRecord,
    chain_settings: ChainSettings,
    controller_settings: ControllerSettings,
    *,
    environ: Mapping[str, str],
    transports: Mapping[Party, httpx.AsyncBaseTransport] | None = None,
    holder: str | None = None,
    adapter: ChainAdapter | None = None,
    background: bool = True,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> Backend:
    adapter = adapter or Web3ChainAdapter(
        chain_settings.chain_rpc_url, timeout_s=chain_settings.rpc_timeout_s
    )
    codec = ExchangeCodec(
        deployment.exchange_address, deployment.base_token_address, deployment.quote_token_address
    )
    owner = holder or holder_id()
    relay_policy = chain_settings.relay_policy()
    relay = Relay(
        database,
        adapter,
        codec,
        chain_id=deployment.chain_id,
        relay_signer=LocalTransactionSigner.from_reference(
            chain_settings.relay_key_ref, environ, "relay"
        ),
        operator_signer=LocalTransactionSigner.from_reference(
            chain_settings.operator_key_ref, environ, "operator"
        ),
        holder=owner,
        policy=relay_policy,
        clock=clock,
    )
    indexer = Indexer(
        database,
        adapter,
        codec,
        deployment,
        TimelineSentences(),
        policy=chain_settings.indexer_policy(),
        clock=clock,
    )
    endpoints = controller_settings.endpoints()
    policy = controller_settings.policy(chain_settings)
    agents = {
        party: HttpAgentClient(
            endpoints[party.value],
            timeout_s=policy.agent_timeout_s,
            transport=None if transports is None else transports.get(party),
        )
        for party in (Party.BUYER, Party.SELLER)
    }
    controller = RunController(
        database,
        adapter,
        codec,
        deployment,
        relay,
        indexer,
        agents,
        {party: endpoints[party.value].root_key_ref for party in agents},
        policy=policy,
        relay_policy=relay_policy,
        default_threshold=chain_settings.confirmation_threshold,
        holder=owner,
        software_version=controller_settings.software_version,
        background=background,
        clock=clock,
        sleep=sleep,
    )
    return Backend(controller, agents)
