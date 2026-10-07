"""The run controller of stage 2.4, wired the way production wires it, against a real chain.

`ControllerHarness` builds the controller through the composition root, `api.composition`, with the
real relay, indexer and projection, the throwaway Anvil and the real deploy script of `anvil_chain`,
the real PostgreSQL of the integration conftest — and two real agent applications, built by the
agent service's own `create_app` and served in-process through `httpx.ASGITransport`. Every request
between them is signed and verified with the real HMAC (ADR-041); nothing is mocked. The agents'
roots are fresh per harness and reach only their own app.

Shared helpers live here, in a named module on the `pythonpath`, never in a conftest
(docs/contributing.md section 3).
"""

from __future__ import annotations

import asyncio
import json
import secrets
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import httpx
from anvil_chain import ANVIL_KEYS
from api_chain import AnvilChain, deployment_from_manifest
from eth_account import Account
from eth_account.signers.local import LocalAccount
from fastapi import FastAPI
from pydantic import SecretStr

from agent.keys.derivation import KeyDerivation, derive_run_key
from agent.main import create_app
from agent.settings import AgentSettings
from api.chain import ChainAdapter
from api.composition import Backend, build_controller
from api.config import ChainSettings, ControllerSettings
from api.controller import Progress, RunController, RunRequest
from api.db import Database, Party, RunRecord, ScenarioRecord

SCENARIOS: Final = Path(__file__).resolve().parents[4] / "scenarios"
#: Canned model responses (docs/test_strategy.md section 11, ADR-088).
MODEL_FIXTURES: Final = (
    Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "model_responses"
)
ENVIRON: Final = {"RELAY_KEY": ANVIL_KEYS[1], "OPERATOR_KEY": ANVIL_KEYS[0]}


def scenario(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads((SCENARIOS / f"{name}.json").read_text())
    return document


def run_request(
    name: str,
    deployment_id: str,
    *,
    threshold: int | None = None,
    session_duration_s: int | None = None,
    scenario_id: str | None = None,
) -> RunRequest:
    """`POST /v1/runs` for a scenario file, both parties on the deterministic policy."""
    document = scenario(name)
    public = dict(document["public_config"])
    if threshold is not None:
        public["confirmation_threshold"] = threshold
    if session_duration_s is not None:
        public["session_duration_s"] = session_duration_s

    def party(role: str) -> dict[str, Any]:
        template = document[role]
        return {
            "policy": "deterministic",
            "model_id": None,
            "effort": None,
            "initial_balances": template["initial_balances"],
            "allowance_minor": template["allowance_minor"],
            "mandate": template["mandate"],
        }

    return RunRequest.parse(
        {
            "name": document["name"],
            "scenario_id": scenario_id,
            "deployment_id": deployment_id,
            "public_config": public,
            "buyer": party("buyer"),
            "seller": party("seller"),
            "limits": {
                "model_call_ceiling": 20,
                "model_spend_ceiling_usd": "2.00",
                "model_timeout_s": 45,
                "repair_attempts": 1,
            },
        }
    )


def scenario_record(name: str) -> ScenarioRecord:
    document = scenario(name)
    return ScenarioRecord(
        scenario_id=document["scenario_id"],
        name=document["name"],
        description=document["description"],
        public_config=document["public_config"],
        buyer_template=document["buyer"],
        seller_template=document["seller"],
        source_hash="0x" + "00" * 32,
    )


class Agents:
    """Two agent applications, each with its own root and shared secret.

    `model_fixtures` starts both in fixture mode (ADR-088): each answers its model runs from its
    role's canned script in that directory, through the real policy, validator and signer.
    """

    def __init__(self, model_fixtures: Path | None = None) -> None:
        self.model_fixtures = model_fixtures
        self.roots = {
            Party.BUYER: "0x" + secrets.token_hex(32),
            Party.SELLER: "0x" + secrets.token_hex(32),
        }
        self.secrets = {Party.BUYER: secrets.token_hex(32), Party.SELLER: secrets.token_hex(32)}
        self.apps = {party: self.app(party) for party in self.roots}
        #: Per party, a function that may answer a request in the agent's place: a refusal, a
        #: failure, an outage. None lets the request through to the real app.
        self.intercept: dict[Party, Callable[[httpx.Request], httpx.Response | None]] = {}
        self.requests: dict[Party, list[str]] = {Party.BUYER: [], Party.SELLER: []}
        #: Per party, a function that rewrites the real app's successful JSON answer to a route:
        #: an agent that answers, but with something the backend must not trust.
        self.rewrite: dict[Party, Callable[[str, dict[str, Any]], dict[str, Any]]] = {}

    def app(self, party: Party) -> FastAPI:
        """A fresh instance of the party's agent: what a restart of its process gives."""
        variable = f"{party.value.upper()}_ROOT_KEY"
        settings = AgentSettings(
            role=party.value,
            instance="agent-a" if party == Party.BUYER else "agent-b",
            root_key_ref=f"env:{variable}",
            shared_secret=SecretStr(self.secrets[party]),
            port=8101 if party == Party.BUYER else 8102,
            model_fixtures=self.model_fixtures,
        )
        return create_app(settings, environ={variable: self.roots[party]})

    def run_account(self, party: Party, run_id: uuid.UUID, chain_id: int) -> LocalAccount:
        """The run's participant key, derived from this harness's root as the agent derives it
        (ADR-039): for a test that must sign something the agent would not."""
        spec = KeyDerivation(chain_id=chain_id, role=party.value, run_id=run_id)
        root = bytes.fromhex(self.roots[party].removeprefix("0x"))
        account: LocalAccount = Account.from_key(derive_run_key(root, spec))
        return account

    def restart(self, party: Party) -> None:
        self.apps[party] = self.app(party)

    def transports(self) -> dict[Party, httpx.AsyncBaseTransport]:
        return {party: _Routed(self, party) for party in self.apps}


class _Routed(httpx.AsyncBaseTransport):
    """Sends to whichever app currently serves the party, so a test can restart one."""

    def __init__(self, agents: Agents, party: Party) -> None:
        self._agents = agents
        self._party = party

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._agents.requests[self._party].append(request.url.path)
        intercept = self._agents.intercept.get(self._party)
        if intercept is not None:
            answer = intercept(request)
            if answer is not None:
                return answer
        transport = httpx.ASGITransport(app=self._agents.apps[self._party])
        response = await transport.handle_async_request(request)
        rewrite = self._agents.rewrite.get(self._party)
        if rewrite is None or response.status_code != 200:
            return response
        body = json.loads(await response.aread())
        return httpx.Response(200, content=json.dumps(rewrite(request.url.path, body)).encode())


class ControllerHarness:
    def __init__(
        self,
        database: Database,
        chain: AnvilChain,
        *,
        agents: Agents | None = None,
        threshold: int = 1,
        poll_interval_s: float = 0.02,
        lease_ttl_s: float = 30.0,
        outage_limit_s: float = 60.0,
        holder: str | None = None,
        background: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] | None = None,
        adapter: ChainAdapter | None = None,
    ) -> None:
        self.database = database
        self.chain = chain
        self.agents = agents or Agents()
        self.deployment = deployment_from_manifest(chain.manifest, chain.genesis_hash)
        self.threshold = threshold
        self.holder = holder or f"test-{uuid.uuid4().hex[:8]}"
        self.sleep = sleep or self._sleep
        chain_settings = ChainSettings(
            chain_rpc_url=chain.rpc_url,
            relay_key_ref="env:RELAY_KEY",
            operator_key_ref="env:OPERATOR_KEY",
            confirmation_threshold=1,
            indexer_poll_interval_s=poll_interval_s,
        )
        controller_settings = ControllerSettings(
            agent_a_url="http://agent-a",
            agent_b_url="http://agent-b",
            agent_a_shared_secret=SecretStr(self.agents.secrets[Party.BUYER]),
            agent_b_shared_secret=SecretStr(self.agents.secrets[Party.SELLER]),
            buyer_root_key_ref="env:BUYER_ROOT_KEY",
            seller_root_key_ref="env:SELLER_ROOT_KEY",
            rpc_outage_limit_s=outage_limit_s,
            run_lease_ttl_s=lease_ttl_s,
            software_version="0.1.0+test",
        )
        self.backend: Backend = build_controller(
            database,
            self.deployment,
            chain_settings,
            controller_settings,
            environ=ENVIRON,
            transports=self.agents.transports(),
            holder=self.holder,
            adapter=adapter,
            background=background,
            clock=clock,
            sleep=self.sleep,
        )
        self.controller: RunController = self.backend.controller

    async def _sleep(self, seconds: float) -> None:
        """Above threshold 1 a confirmation needs a later block, and Anvil mines only on demand."""
        if self.threshold > 1:
            await asyncio.to_thread(self.chain.mine)
        await asyncio.sleep(seconds)

    async def prepare(self) -> None:
        async with self.database.unit_of_work() as uow:
            await uow.deployments.upsert(self.deployment)
            for name in ("default-overlap", "infeasible-clone"):
                await uow.scenarios.upsert(scenario_record(name))

    async def create(self, name: str = "default-overlap", **kwargs: Any) -> RunRecord:
        await self.prepare()
        request = run_request(
            name,
            self.deployment.deployment_id,
            threshold=kwargs.pop("threshold", self.threshold),
            scenario_id=name,
            **kwargs,
        )
        return await self.controller.create_run(request)

    async def validated(self, name: str = "default-overlap", **kwargs: Any) -> RunRecord:
        run = await self.create(name, **kwargs)
        report = await self.controller.validate(run.id)
        assert report.ok, report.to_json()
        return await self.run(run.id)

    async def run(self, run_id: uuid.UUID) -> RunRecord:
        async with self.database.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
        assert run is not None
        return run

    async def tick_until(
        self,
        run_id: uuid.UUID,
        done: Callable[[RunRecord], Awaitable[bool]],
        *,
        limit: int = 600,
    ) -> RunRecord:
        """Drive the run tick by tick, in this task, until `done` holds."""
        for _ in range(limit):
            run = await self.run(run_id)
            if await done(run):
                return run
            if await self.controller.driver.tick(run_id) == Progress.WAITING:
                await self.sleep(0.01)
        raise AssertionError(f"the run did not get there in {limit} ticks")

    async def settle(self, limit_s: float = 60.0) -> None:
        """Wait for the background driver to run out of things to drive."""
        async with asyncio.timeout(limit_s):
            await self.controller.wait()

    async def aclose(self) -> None:
        await self.backend.aclose()


async def table_counts(database: Database, run_id: uuid.UUID) -> Mapping[str, int]:
    """Rows per table for one run, through the repositories."""
    async with database.unit_of_work() as uow:
        return {
            "runs": int(await uow.runs.get(run_id) is not None),
            "mandate_versions": len(await uow.mandates.get_both(run_id)),
            "wallets": len(await uow.wallets.list_for_run(run_id)),
            "turns": len(await uow.turns.list_for_run(run_id)),
            "decisions": len(await uow.decisions.list_for_run(run_id)),
            "signed_actions": len(await uow.signed_actions.list_for_run(run_id)),
            "tx_outbox": len(await uow.outbox.list_for_run(run_id)),
            "chain_events": len(await uow.chain_events.canonical_for_run(run_id)),
            "balance_snapshots": len(await uow.balances.canonical_for_run(run_id)),
            "run_events": len(await uow.run_events.after(run_id, 0, limit=10_000)),
        }
