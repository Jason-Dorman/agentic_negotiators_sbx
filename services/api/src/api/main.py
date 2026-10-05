"""The backend process: settings from the environment, the graph built once, the app served.

`create_app_from_environment` is what `python -m api` serves. Its lifespan, in order:

1. applies the migrations, when `AUTO_MIGRATE` is set — the local profile does; a Sepolia host runs
   them by hand (data model section 8);
2. loads the deployment manifest named by `DEPLOYMENT_MANIFEST`, validated against
   `deployment_manifest.v1.json`, and checks it against the chain `CHAIN_RPC_URL` reaches: the same
   chain ID, the exchange's code where the manifest says, and — for a deployment loaded before —
   the same genesis block, so a restarted Anvil is never taken for the chain it replaced (ADR-081).
   The deployment is stored with its chain's genesis hash, and this backend serves it;
3. loads every scenario in `SCENARIOS_DIR`, each validated against `scenario.v1.json` with its
   `scenario_id` matching its file name, and upserts them by id (data model 3.1). A missing or empty
   directory stops start-up (Q69);
4. builds the controller and the HTTP services (`api.composition`), tracks every unfinished
   operation again, and starts `RunController.recover` in the background: it waits for a lease a
   crashed process still holds, which must not keep the API from answering meanwhile
   (architecture 5.4).

A refusal at start-up names the file and the rule it broke, never a value: a scenario holds both
mandates, and a schema error's own text quotes the instance it refused.

`make_server` is how the process is served: a shutdown first tells every event stream to end, so the
graceful phase is short and the lifespan's teardown — which gives the lease up, so a process started
next takes the run over at once — runs before the container's stop grace runs out (ADR-082).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType
from typing import Any, Final, Literal

import structlog
import uvicorn
from eth_utils.crypto import keccak
from fastapi import FastAPI
from jsonschema import ValidationError

from api.chain import ChainAdapter, Web3ChainAdapter
from api.composition import build_controller, build_services
from api.config import (
    DEFAULT_RPC_PRICE_TABLE,
    ApiSettings,
    RpcPriceTable,
    load_api_settings,
    load_chain_settings,
    load_controller_settings,
)
from api.controller import RunController
from api.db import Database, DeploymentRecord, ScenarioRecord
from api.db.migrate import upgrade
from api.routes import Shutdown, create_app
from negotiation_protocol import Address, Digest, validate

_log = structlog.get_logger(component="main")

#: ADR-082: how long open connections may take to close once shutdown begins. Streams end at once;
#: this bounds the rest, inside Docker's default ten-second stop grace.
GRACEFUL_SHUTDOWN_S: Final = 5


class StartupError(Exception):
    """The configuration names a file, or a chain, this process cannot serve."""


def _schema_refusal(name: str, schema: str, error: ValidationError) -> StartupError:
    """Where the document broke the schema and which rule, never the instance it quoted."""
    location = "/".join(str(part) for part in error.absolute_path) or "<root>"
    return StartupError(f"{name} does not match {schema} at {location} ({error.validator})")


def load_deployment(path: Path) -> DeploymentRecord:
    try:
        document: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise StartupError(f"cannot read the deployment manifest {path}: {error}") from None
    try:
        validate(document, "deployment_manifest.v1.json")
    except ValidationError as error:
        raise _schema_refusal(path.name, "deployment_manifest.v1.json", error) from None
    return DeploymentRecord(
        deployment_id=document["deployment_id"],
        chain_id=int(document["chain_id"]),
        protocol_version=document["protocol_version"],
        exchange_address=Address(document["exchange_address"]),
        base_token_address=Address(document["base_token_address"]),
        quote_token_address=Address(document["quote_token_address"]),
        operator_address=Address(document["operator_address"]),
        relay_address=Address(document["relay_address"]),
        code_hashes=document["code_hashes"],
        compiler=document["compiler"],
        explorer_base_url=document["explorer_base_url"],
        ens=document["ens"],
        manifest=document,
        start_block=int(document["start_block"]),
        deployed_at=datetime.fromtimestamp(int(document["deployed_at_ts"]), tz=UTC),
    )


def _scenario(path: Path) -> ScenarioRecord:
    raw = path.read_bytes()
    try:
        document = json.loads(raw)
        validate(document, "scenario.v1.json")
    except json.JSONDecodeError as error:
        raise StartupError(f"{path.name} is not JSON: line {error.lineno}") from None
    except ValidationError as error:
        raise _schema_refusal(path.name, "scenario.v1.json", error) from None
    if document["scenario_id"] != path.stem:
        raise StartupError(f"{path.name}: scenario_id must match the file name")
    return ScenarioRecord(
        scenario_id=document["scenario_id"],
        name=document["name"],
        description=document["description"],
        public_config=document["public_config"],
        buyer_template=document["buyer"],
        seller_template=document["seller"],
        source_hash="0x" + hashlib.sha256(raw).hexdigest(),
    )


def load_scenarios(directory: Path) -> list[ScenarioRecord]:
    """Every `*.json` in the directory, each a valid scenario named for its own id. A directory
    that is missing or holds none stops start-up (Q69): a misspelt path is no empty catalogue."""
    if not directory.is_dir():
        raise StartupError(f"SCENARIOS_DIR {directory} is not a directory")
    scenarios = [_scenario(path) for path in sorted(directory.glob("*.json"))]
    if not scenarios:
        raise StartupError(f"SCENARIOS_DIR {directory} holds no scenario")
    return scenarios


async def check_chain(
    chain: ChainAdapter, deployment: DeploymentRecord, stored: DeploymentRecord | None
) -> DeploymentRecord:
    """The deployment, with its chain's genesis hash, once the chain is shown to be its own
    (ADR-081). A manifest for another chain, or a deployment id reused on a restarted chain, stops
    start-up rather than letting the indexer read one chain's history against another."""
    chain_id = await chain.chain_id()
    if chain_id != deployment.chain_id:
        raise StartupError(
            f"CHAIN_RPC_URL serves chain {chain_id}; deployment {deployment.deployment_id} is on "
            f"chain {deployment.chain_id}"
        )
    code_hash = Digest(keccak(await chain.code(deployment.exchange_address)))
    if str(code_hash) != str(deployment.code_hashes.get("exchange")):
        raise StartupError(
            f"the exchange of deployment {deployment.deployment_id} is not on this chain at "
            f"{deployment.exchange_address}: is CHAIN_RPC_URL the chain it was deployed to?"
        )
    genesis = await chain.block(0)
    if genesis is None:
        raise StartupError("the RPC returned no genesis block")
    if stored is not None and stored.genesis_hash not in (None, genesis.hash):
        raise StartupError(
            f"deployment {deployment.deployment_id} was loaded against another chain, whose "
            f"genesis was {stored.genesis_hash}. A chain that was reset needs a new deployment id; "
            "the local Compose deploy gives it one"
        )
    return replace(deployment, genesis_hash=genesis.hash)


async def _seed(database: Database, settings: ApiSettings, chain: ChainAdapter) -> DeploymentRecord:
    loaded = load_deployment(settings.deployment_manifest)
    scenarios = load_scenarios(settings.scenarios_dir)
    async with database.unit_of_work() as uow:
        stored = await uow.deployments.get(loaded.deployment_id)
    deployment = await check_chain(chain, loaded, stored)
    async with database.unit_of_work() as uow:
        await uow.deployments.upsert(deployment)
        for scenario in scenarios:
            await uow.scenarios.upsert(scenario)
    _log.info(
        "startup.loaded",
        deployment_id=deployment.deployment_id,
        chain_id=deployment.chain_id,
        genesis_hash=str(deployment.genesis_hash),
        scenarios=len(scenarios),
    )
    return deployment


async def _recover(controller: RunController) -> None:
    """Start-up recovery's top level (docs/contributing.md section 2.1): a failure is logged, and
    the API goes on answering; the run stays as the database has it, for an operator to resume."""
    try:
        recovered = await controller.recover()
    except Exception:
        _log.exception("startup.recover_failed")
        return
    _log.info("startup.recovered", run_id=None if recovered is None else str(recovered))


def create_app_from_environment(shutdown: Shutdown | None = None) -> FastAPI:
    settings = load_api_settings()
    chain_settings = load_chain_settings()
    controller_settings = load_controller_settings()
    shutdown = shutdown or Shutdown()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if settings.auto_migrate:
            await asyncio.to_thread(upgrade, settings.database_url)
        database = Database(settings.database_url)
        probe = Web3ChainAdapter(
            chain_settings.chain_rpc_url, timeout_s=chain_settings.rpc_timeout_s
        )
        try:
            deployment = await _seed(database, settings, probe)
        except BaseException:
            await database.dispose()
            raise
        backend = build_controller(
            database,
            deployment,
            chain_settings,
            controller_settings,
            environ=os.environ,
            prices=RpcPriceTable.load(settings.rpc_price_table or DEFAULT_RPC_PRICE_TABLE),
            rpc_provider=settings.rpc_provider,
        )
        services = build_services(
            backend,
            database,
            settings,
            software_version=controller_settings.software_version,
            shutdown=shutdown,
        )
        app.state.services = services
        await services.operations.resume_all()
        recovery = asyncio.create_task(_recover(backend.controller), name="recover")
        try:
            yield
        finally:
            shutdown.request()
            recovery.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await recovery
            await services.operations.aclose()
            await backend.aclose()
            await database.dispose()

    return create_app(settings, lifespan=lifespan)


class _Server(uvicorn.Server):
    """Uvicorn, telling the event streams to end as soon as a shutdown begins (ADR-082)."""

    def __init__(self, config: uvicorn.Config, shutdown: Shutdown) -> None:
        super().__init__(config)
        self._shutdown = shutdown

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        self._shutdown.request()
        super().handle_exit(sig, frame)


def make_server(
    app: FastAPI,
    shutdown: Shutdown,
    *,
    host: str,
    port: int,
    lifespan: Literal["auto", "on", "off"] = "on",
) -> uvicorn.Server:
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        access_log=False,
        log_config=None,
        lifespan=lifespan,
        timeout_graceful_shutdown=GRACEFUL_SHUTDOWN_S,
    )
    return _Server(config, shutdown)
