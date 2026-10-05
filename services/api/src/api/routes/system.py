"""The system routes of api_contract section 2.1: health, deployments and scenarios."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from api.db.records import DeploymentRecord
from api.routes import models
from api.routes.errors import RouteNotFoundError
from api.routes.services import Services, require_reveal, services

router = APIRouter(prefix="/v1", tags=["system"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": models.ErrorEnvelope} for status in (401, 403, 404, 422)
}
_Svc = Annotated[Services, Depends(services)]


@router.get(
    "/health",
    response_model=models.Health,
    responses={503: {"model": models.Health, "description": "A dependency is down"}},
)
async def health(svc: _Svc) -> Any:
    """Never behind the operator token. `503`, same shape, when any dependency is down."""
    checks = await svc.controller.health()
    healthy = all(checks[name] for name in ("rpc_ok", "db_ok", "agent_a_ok", "agent_b_ok"))
    body = {"status": "ok" if healthy else "degraded", "version": svc.software_version, **checks}
    if healthy:
        return body
    return JSONResponse(models.Health.model_validate(body).model_dump(), status_code=503)


def _deployment(deployment: DeploymentRecord) -> dict[str, Any]:
    """ENS names are display metadata served verbatim from the manifest, never resolved here and
    never an identity (ADR-030)."""
    return {
        "deployment_id": deployment.deployment_id,
        "chain_id": deployment.chain_id,
        "protocol_version": deployment.protocol_version,
        "exchange_address": str(deployment.exchange_address),
        "base_token_address": str(deployment.base_token_address),
        "quote_token_address": str(deployment.quote_token_address),
        "operator_address": str(deployment.operator_address),
        "relay_address": str(deployment.relay_address),
        "code_hashes": dict(deployment.code_hashes),
        "compiler": dict(deployment.compiler),
        "explorer_base_url": deployment.explorer_base_url,
        "ens": deployment.ens,
        "deployed_at": deployment.deployed_at.isoformat().replace("+00:00", "Z"),
    }


@router.get("/deployments", response_model=models.DeploymentList, responses=_ERRORS)
async def list_deployments(svc: _Svc) -> Any:
    async with svc.transactions.unit_of_work() as uow:
        deployments = await uow.deployments.list_all()
    return {"deployments": [_deployment(deployment) for deployment in deployments]}


@router.get("/scenarios", response_model=models.ScenarioList, responses=_ERRORS)
async def list_scenarios(svc: _Svc) -> Any:
    """The public fields only. `feasibility_hint` is always null: feasibility is computed by the
    evaluator alone, never on a setup route."""
    async with svc.transactions.unit_of_work() as uow:
        scenarios = await uow.scenarios.list_all()
    return {
        "scenarios": [
            {
                "scenario_id": scenario.scenario_id,
                "name": scenario.name,
                "description": scenario.description,
                "feasibility_hint": None,
            }
            for scenario in scenarios
        ]
    }


@router.get("/scenarios/{scenario_id}", response_model=models.Scenario, responses=_ERRORS)
async def get_scenario(scenario_id: str, request: Request, svc: _Svc) -> Any:
    """The whole template, both mandates included, so behind the observer reveal header."""
    require_reveal(request, "a scenario's mandates")
    async with svc.transactions.unit_of_work() as uow:
        scenario = await uow.scenarios.get(scenario_id)
    if scenario is None:
        raise RouteNotFoundError(f"no scenario {scenario_id}")
    return {
        "scenario_id": scenario.scenario_id,
        "name": scenario.name,
        "description": scenario.description,
        "public_config": scenario.public_config,
        "buyer": scenario.buyer_template,
        "seller": scenario.seller_template,
    }
