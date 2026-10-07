"""The operator API of stage 2.5, served in-process over the stage 2.4 controller harness.

`ApiHarness` builds the same graph `api.main` builds — `build_controller`, then `build_services`,
then `create_app` — over the real PostgreSQL, the throwaway Anvil and the two real agent
applications of `ControllerHarness`, and talks to it with `httpx` through `ASGITransport`. Nothing
between the client and the chain is a stand-in.

`ASGITransport` collects a whole response before returning it, so it cannot read an endless stream;
`serve` runs the same app under a real uvicorn server on a free port for the SSE tests.

Shared helpers live here, in a named module on the `pythonpath`, never in a conftest
(docs/contributing.md section 3).
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import uuid
from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from api_controller import ControllerHarness, scenario

from api.composition import build_services
from api.config import ApiSettings
from api.db import Database
from api.routes import Services, create_app

LIMITS = {
    "model_call_ceiling": 20,
    "model_spend_ceiling_usd": "2.00",
    "model_timeout_s": 45,
    "repair_attempts": 1,
}


def api_settings(**overrides: Any) -> ApiSettings:
    """Settings for an app whose services are built by the test, not by the lifespan."""
    values: dict[str, Any] = {
        "database_url": "postgresql+asyncpg://unused",
        "deployment_manifest": Path("unused.json"),
        "event_poll_interval_s": 0.02,
        **overrides,
    }
    return ApiSettings(**values)


def run_body(
    name: str,
    deployment_id: str,
    *,
    threshold: int | None = 1,
    scenario_id: str | None = None,
    model_id: str | None = None,
    limits: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """`POST /v1/runs` for a scenario file, as JSON: both parties deterministic, or both on the
    model policy at high effort when `model_id` is given."""
    document = scenario(name)
    public = dict(document["public_config"])
    if threshold is not None:
        public["confirmation_threshold"] = threshold

    def party(role: str) -> dict[str, Any]:
        template = document[role]
        return {
            "policy": "deterministic" if model_id is None else "model",
            "model_id": model_id,
            "effort": None if model_id is None else "high",
            "initial_balances": template["initial_balances"],
            "allowance_minor": template["allowance_minor"],
            "mandate": template["mandate"],
        }

    return {
        "name": document["name"],
        "scenario_id": scenario_id,
        "deployment_id": deployment_id,
        "public_config": public,
        "buyer": party("buyer"),
        "seller": party("seller"),
        "limits": {**LIMITS, **(limits or {})},
        "parent_run_id": None,
    }


class ApiHarness:
    def __init__(self, controller: ControllerHarness, **settings: Any) -> None:
        self.controller = controller
        self.database: Database = controller.database
        self.settings = api_settings(**settings)
        self.services: Services = build_services(
            controller.backend,
            controller.database,
            self.settings,
            software_version="0.1.0+test",
        )
        self.app = create_app(self.settings, self.services)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://api"
        )

    @property
    def deployment_id(self) -> str:
        return self.controller.deployment.deployment_id

    async def prepare(self) -> None:
        await self.controller.prepare()

    async def create(
        self, name: str = "default-overlap", headers: Mapping[str, str] | None = None, **kw: Any
    ) -> dict[str, Any]:
        await self.prepare()
        body = run_body(name, self.deployment_id, scenario_id=name, **kw)
        response = await self.client.post("/v1/runs", json=body, headers=headers)
        assert response.status_code == 201, response.text
        created: dict[str, Any] = response.json()
        return created

    async def validated(self, name: str = "default-overlap", **kw: Any) -> str:
        run = await self.create(name, **kw)
        response = await self.client.post(f"/v1/runs/{run['run_id']}/validate")
        assert response.status_code == 200, response.text
        assert response.json()["ok"], response.json()
        return str(run["run_id"])

    async def settle(self, limit_s: float = 90.0) -> None:
        """Until the controller has nothing to drive and every operation has its verdict."""
        await self.controller.settle(limit_s)
        async with asyncio.timeout(limit_s):
            await self.services.operations.wait()

    async def run(self, run_id: str | uuid.UUID) -> dict[str, Any]:
        response = await self.client.get(f"/v1/runs/{run_id}")
        assert response.status_code == 200, response.text
        resource: dict[str, Any] = response.json()
        return resource

    async def operation(self, operation_id: str) -> dict[str, Any]:
        response = await self.client.get(f"/v1/operations/{operation_id}")
        assert response.status_code == 200, response.text
        record: dict[str, Any] = response.json()
        return record

    async def aclose(self) -> None:
        await self.client.aclose()
        await self.services.operations.aclose()
        await self.controller.aclose()


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


@contextlib.asynccontextmanager
async def serve(harness: ApiHarness) -> AsyncIterator[str]:
    """The harness's app under a real uvicorn server, for a client that must read a stream."""
    port = free_port()
    config = uvicorn.Config(
        harness.app, host="127.0.0.1", port=port, log_config=None, lifespan="off"
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    async with asyncio.timeout(10):
        while not server.started:  # noqa: ASYNC110  reason: uvicorn offers a flag, no event
            await asyncio.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
