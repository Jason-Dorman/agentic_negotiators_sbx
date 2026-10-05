"""The backend's composition root: settings in, a run controller and the services around it out.

Concrete classes are chosen here and nowhere else (docs/contributing.md section 2.1). The process
entry point, `api.main`, calls `build_controller` once at start-up, then `build_services` for the
HTTP layer, then `RunController.recover`; the integration suite builds the same graph through the
same functions, with the agents served in-process through `transports`.

The evidence module is handed the projector and the metrics calculator here, because both are its
siblings under `.importlinter`; the driver is handed the calculator as its `RunMetricsSink` and the
chain adapter's `RpcCounter` (ADR-061).

Keys arrive as references and are resolved by the relay's signer, never held here (ADR-049).
"""

from __future__ import annotations

import asyncio
import os
import socket
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import httpx

from api.agent_client import HttpAgentClient
from api.chain import ChainAdapter, ExchangeCodec, RpcCounter, Web3ChainAdapter
from api.config import ApiSettings, ChainSettings, ControllerSettings, RpcPriceTable
from api.controller import RunController
from api.db import Database, DeploymentRecord, Party
from api.evidence import Exporter, ObserverViews, RunResources
from api.indexer import Indexer
from api.metrics import MetricsCalculator
from api.projection import Projector, TimelineSentences
from api.relay import LocalTransactionSigner, Relay
from api.routes import Idempotency, OperationTracker, Services, Shutdown


@dataclass(frozen=True, slots=True)
class Backend:
    controller: RunController
    agents: Mapping[Party, HttpAgentClient]
    metrics: MetricsCalculator
    resources: RunResources
    exporter: Exporter
    observer: ObserverViews
    counter: RpcCounter

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
    prices: RpcPriceTable | None = None,
    rpc_provider: str = "anvil",
    counter: RpcCounter | None = None,
) -> Backend:
    counter = counter or RpcCounter()
    adapter = adapter or Web3ChainAdapter(
        chain_settings.chain_rpc_url, timeout_s=chain_settings.rpc_timeout_s, counter=counter
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
        deployment_id=deployment.deployment_id,
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
    threshold = chain_settings.confirmation_threshold
    metrics = MetricsCalculator(
        database,
        prices or RpcPriceTable.load(),
        rpc_provider=rpc_provider,
        default_threshold=threshold,
        clock=clock,
    )
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
        counter=counter,
        metrics=metrics,
        db_ping=database.ping,
    )
    resources = RunResources(database, Projector(database, default_threshold=threshold), metrics)
    return Backend(
        controller=controller,
        agents=agents,
        metrics=metrics,
        resources=resources,
        exporter=Exporter(database, resources, metrics, clock=clock),
        observer=ObserverViews(database, metrics),
        counter=counter,
    )


def build_services(
    backend: Backend,
    database: Database,
    settings: ApiSettings,
    *,
    software_version: str,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    shutdown: Shutdown | None = None,
) -> Services:
    """What the HTTP layer is handed (`api.routes.Services`)."""
    return Services(
        transactions=database,
        controller=backend.controller,
        resources=backend.resources,
        exporter=backend.exporter,
        observer=backend.observer,
        metrics=backend.metrics,
        operations=OperationTracker(database, poll_interval_s=settings.event_poll_interval_s),
        idempotency=Idempotency(
            database,
            retention=timedelta(hours=settings.idempotency_retention_h),
            claim_timeout=timedelta(seconds=settings.idempotency_claim_timeout_s),
            clock=clock,
        ),
        settings=settings,
        software_version=software_version,
        clock=clock,
        shutdown=shutdown or Shutdown(),
    )
