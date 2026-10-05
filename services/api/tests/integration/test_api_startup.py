"""The backend process as `python -m api` runs it: the real lifespan, under a real uvicorn.

`ApiHarness` serves the app with its lifespan off, so the stage 2.5 review could delete the whole
start-up path — migrations, the manifest and its chain check, the scenarios, `resume_all`, recovery
— with the suite green. Here `create_app_from_environment` reads a real environment and is served by
`api.main.make_server`, against the session's Anvil and database and two real agent applications
served by uvicorn on their own ports, as Compose would wire them.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import signal
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn
from anvil_chain import ANVIL_KEYS
from api_chain import AnvilChain, deployment_from_manifest
from api_controller import SCENARIOS, Agents
from api_http import free_port, run_body
from api_seed import scenario_record

from api.db import Database, NewOperation, OperationStatus
from api.main import StartupError, create_app_from_environment, make_server
from api.routes import Shutdown


@pytest.fixture
async def agents() -> AsyncIterator[tuple[Agents, dict[str, int]]]:
    """The two agent applications on real ports, as Compose's agent-a and agent-b."""
    pool = Agents()
    ports: dict[str, int] = {}
    servers = []
    for party, app in pool.apps.items():
        port = free_port()
        server = uvicorn.Server(uvicorn.Config(app, port=port, log_config=None, lifespan="off"))
        servers.append((server, asyncio.create_task(server.serve())))
        ports[party.value] = port
    for server, _ in servers:
        while not server.started:  # noqa: ASYNC110  reason: uvicorn offers a flag, no event
            await asyncio.sleep(0.01)
    yield pool, ports
    for server, task in servers:
        server.should_exit = True
        await task


@pytest.fixture
def environment(
    monkeypatch: pytest.MonkeyPatch,
    migrated_url: str,
    chain: AnvilChain,
    agents: tuple[Agents, dict[str, int]],
    tmp_path: Path,
) -> Iterator[Path]:
    """Every variable `python -m api` reads, as infra/.env.example names them. Yields the manifest
    file, which a test may rewrite before the app starts."""
    pool, ports = agents
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps({k: v for k, v in chain.manifest.items() if k != "_path"}), encoding="utf-8"
    )
    from api.db import Party

    values = {
        "DATABASE_URL": migrated_url,
        "AUTO_MIGRATE": "true",
        "DEPLOYMENT_MANIFEST": str(manifest),
        "SCENARIOS_DIR": str(SCENARIOS),
        "CHAIN_RPC_URL": chain.rpc_url,
        "RELAY_KEY_REF": "env:RELAY_KEY",
        "RELAY_KEY": ANVIL_KEYS[1],
        "OPERATOR_KEY_REF": "env:OPERATOR_KEY",
        "OPERATOR_KEY": ANVIL_KEYS[0],
        "AGENT_A_URL": f"http://127.0.0.1:{ports['buyer']}",
        "AGENT_B_URL": f"http://127.0.0.1:{ports['seller']}",
        "AGENT_A_SHARED_SECRET": pool.secrets[Party.BUYER],
        "AGENT_B_SHARED_SECRET": pool.secrets[Party.SELLER],
        "BUYER_ROOT_KEY_REF": "env:BUYER_ROOT_KEY",
        "SELLER_ROOT_KEY_REF": "env:SELLER_ROOT_KEY",
        "INDEXER_POLL_INTERVAL_S": "0.05",
        "EVENT_POLL_INTERVAL_S": "0.02",
        "SOFTWARE_VERSION": "0.1.0+startup-test",
        "OPERATOR_TOKEN": "",
        "RPC_PRICE_TABLE": "",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    yield manifest


async def _serve(shutdown: Shutdown) -> tuple[uvicorn.Server, asyncio.Task[None], str]:
    port = free_port()
    server = make_server(
        create_app_from_environment(shutdown), shutdown, host="127.0.0.1", port=port
    )
    task = asyncio.create_task(server.serve())
    async with asyncio.timeout(30):
        while not server.started and not task.done():  # noqa: ASYNC110  reason: as above
            await asyncio.sleep(0.02)
    return server, task, f"http://127.0.0.1:{port}"


async def test_the_process_starts_serves_and_tracks_what_it_left(
    database: Database, chain: AnvilChain, environment: Path
) -> None:
    # What a process that died left behind: a deployment row and scenarios are re-upserted, one
    # request answered at once still claimed, and a long-running one never accepted.
    async with database.unit_of_work() as uow:
        await uow.deployments.upsert(deployment_from_manifest(chain.manifest, chain.genesis_hash))
        await uow.scenarios.upsert(scenario_record("default-overlap"))
        left = [
            await uow.operations.create(
                NewOperation(
                    kind=kind,
                    route=f"POST /v1/{kind}",
                    request_hash="0x00",
                    idempotency_key=str(uuid.uuid4()),
                )
            )
            for kind in ("create_run", "start_run")
        ]

    shutdown = Shutdown()
    server, task, url = await _serve(shutdown)
    assert server.started, "the lifespan refused to start"
    try:
        async with httpx.AsyncClient(base_url=url, timeout=30) as client:
            health = (await client.get("/v1/health")).json()
            assert (health["status"], health["version"]) == ("ok", "0.1.0+startup-test")
            deployments = (await client.get("/v1/deployments")).json()["deployments"]
            assert [d["deployment_id"] for d in deployments] == [chain.manifest["deployment_id"]]
            scenarios = (await client.get("/v1/scenarios")).json()["scenarios"]
            assert {s["scenario_id"] for s in scenarios} == {"default-overlap", "infeasible-clone"}

            # Both orphans were recorded interrupted, their keys free.
            for operation in left:
                record = (await client.get(f"/v1/operations/{operation.id}")).json()
                assert (record["status"], record["error"]["code"]) == ("failed", "interrupted")

            # A stream open at shutdown ends at once, not holding the server's graceful phase.
            body = run_body("default-overlap", chain.manifest["deployment_id"])
            run = (await client.post("/v1/runs", json=body)).json()
            async with client.stream("GET", f"/v1/runs/{run['run_id']}/events") as stream:
                lines = stream.aiter_lines()
                assert (await anext(lines)).startswith("id: ")
                server.handle_exit(signal.SIGTERM, None)
                async with asyncio.timeout(3):
                    async for _ in lines:
                        pass
        async with asyncio.timeout(8):
            await task
    finally:
        server.should_exit = True
        if not task.done():
            await task
    async with database.unit_of_work() as uow:
        for operation in left:
            stored = await uow.operations.get(operation.id)
            assert stored is not None and stored.idempotency_key is None
            assert stored.status == OperationStatus.FAILED


def _break(problem: str, manifest_file: Path, tmp_path: Path, mp: pytest.MonkeyPatch) -> None:
    if problem == "other_chain":
        manifest: dict[str, Any] = json.loads(manifest_file.read_text())
        manifest["exchange_address"] = "0x" + "12" * 20
        manifest_file.write_text(json.dumps(manifest))
    elif problem == "missing_scenarios":
        mp.setenv("SCENARIOS_DIR", str(tmp_path / "nowhere"))
    else:
        broken = tmp_path / "scenarios"
        broken.mkdir()
        document = json.loads((SCENARIOS / "default-overlap.json").read_text())
        document["buyer"]["mandate"]["secret_note"] = "PRIVATE-" + secrets.token_hex(4)
        (broken / "default-overlap.json").write_text(json.dumps(document))
        mp.setenv("SCENARIOS_DIR", str(broken))


@pytest.mark.parametrize("problem", ["other_chain", "missing_scenarios", "bad_scenario"])
async def test_the_process_refuses_to_start_on_what_it_cannot_serve(
    environment: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, problem: str
) -> None:
    _break(problem, environment, tmp_path, monkeypatch)
    # The app's own lifespan, entered directly: uvicorn turns its refusal into exit status 3 with
    # `sys.exit`, which inside a task stops the event loop itself and cannot be awaited.
    app = create_app_from_environment(Shutdown())
    with pytest.raises(StartupError) as refused:
        async with app.router.lifespan_context(app):
            pass
    assert "PRIVATE-" not in str(refused.value)
