"""The run routes of api_contract section 2.2, except the event stream (`sse.py`).

Each route is thin: it checks what the request is allowed to ask, claims its idempotency key, calls
one controller operation or one view, and records the answer. Long-running routes — start, step,
resume, abort — answer `202` with an operation record once the controller has made the run's
transition, and the operation tracker records how it ended (Q54). Replay is stage 4's (Q55).
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from fastapi.responses import JSONResponse

from api.controller import RunRequest
from api.db.enums import OperationStatus, OutcomeKind, RunState
from api.db.records import OperationRecord, RunRecord
from api.evidence import outcome_view, run_summary
from api.routes import models
from api.routes.errors import BadRequestError, RouteNotFoundError
from api.routes.idempotency import Claim
from api.routes.operations import ABORT, RESUME, START, STEP, operation_view
from api.routes.services import Services, require_reveal, revealed, services

router = APIRouter(prefix="/v1", tags=["runs"])

_ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": models.ErrorEnvelope} for status in (400, 401, 403, 404, 409, 422, 503)
}
_Svc = Annotated[Services, Depends(services)]
_RunId = Annotated[uuid.UUID, "the run's id"]
_SYNC_STATUS = {"create_run": 201, "clone_run": 201, "validate_run": 200, "pause_run": 200}
REPLAYED = "Idempotent-Replayed"


def _error_document(error: BaseException) -> dict[str, Any]:
    code = getattr(error, "code", "internal_error")
    return {"code": code, "message": getattr(error, "message", "refused")}


async def _sync(
    request: Request,
    svc: Services,
    kind: str,
    run_id: uuid.UUID | None,
    work: Callable[[], Awaitable[dict[str, Any]]],
) -> Any:
    """A route answered at once: replay a stored answer, or do the work and store it."""
    claim = await svc.idempotency.claim(request, kind, run_id, long_running=False)
    if claim.replay is not None:
        return JSONResponse(
            claim.replay.result, status_code=_SYNC_STATUS[kind], headers={REPLAYED: "true"}
        )
    try:
        body = await work()
    except BaseException as error:
        # Re-raised: the response is the error's. Only the key is released, so a retry does the
        # work again rather than replaying a refusal — a cancelled request's too, its client gone.
        await asyncio.shield(svc.idempotency.refused(claim, _error_document(error)))
        raise
    await svc.idempotency.succeeded(claim, body)
    return body


async def _long_running(
    request: Request,
    response: Response,
    svc: Services,
    run_id: uuid.UUID,
    kind: str,
    act: Callable[[], Awaitable[RunRecord]],
) -> dict[str, Any]:
    await svc.resources.run(run_id)  # 404 before an operation is recorded against it
    claim = await svc.idempotency.claim(request, kind, run_id, long_running=True)
    operation = await _operation(svc, claim, run_id, act)
    response.status_code = 202
    response.headers["Location"] = f"/v1/operations/{operation.id}"
    if claim.replay is not None:
        response.headers[REPLAYED] = "true"
    return operation_view(operation)


async def _operation(
    svc: Services, claim: Claim, run_id: uuid.UUID, act: Callable[[], Awaitable[RunRecord]]
) -> OperationRecord:
    if claim.replay is not None:
        async with svc.transactions.unit_of_work() as uow:
            current = await uow.operations.get(claim.replay.id)
        return current or claim.replay
    assert claim.operation is not None
    cursor = await svc.operations.cursor(run_id)
    try:
        run = await act()
    except BaseException as error:
        # Re-raised as the response; the operation is recorded as refused and its key freed.
        await asyncio.shield(_refuse_operation(svc, claim, _error_document(error)))
        raise
    return await svc.operations.accepted(claim.operation, run, outcome_view(run), cursor)


async def _refuse_operation(svc: Services, claim: Claim, error: dict[str, Any]) -> None:
    assert claim.operation is not None
    await svc.operations.transition(claim.operation, OperationStatus.FAILED, error=error)
    await svc.idempotency.refused(claim, error)


# ---------------------------------------------------------------------------------------------
# Creating, listing, reading
# ---------------------------------------------------------------------------------------------


@router.post("/runs", status_code=201, response_model=models.Run, responses=_ERRORS)
async def create_run(request: Request, body: RunRequest, svc: _Svc) -> Any:
    """Validate and save a run, with its separately classified mandates. Touches no chain; both
    agents provision it before the response (ADR-039)."""

    async def work() -> dict[str, Any]:
        run = await svc.controller.create_run(body)
        return await svc.resources.resource(run.id)

    return await _sync(request, svc, "create_run", None, work)


def _encode_cursor(run: RunRecord) -> str:
    raw = json.dumps([run.created_at.isoformat(), str(run.id)]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        created_at, run_id = json.loads(base64.urlsafe_b64decode(padded))
        return datetime.fromisoformat(created_at), uuid.UUID(run_id)
    except (binascii.Error, ValueError, TypeError):
        raise BadRequestError("cursor is not one this API issued", {"query": "cursor"}) from None


@router.get("/runs", response_model=models.RunList, responses=_ERRORS)
async def list_runs(
    svc: _Svc,
    state: RunState | None = None,
    outcome: OutcomeKind | None = None,
    batch_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    cursor: str | None = None,
) -> Any:
    """Newest first. `next_cursor` is null on the last page."""
    before = None if cursor is None else _decode_cursor(cursor)
    async with svc.transactions.unit_of_work() as uow:
        page = await uow.runs.list_page(
            limit=limit + 1, state=state, outcome=outcome, batch_id=batch_id, before=before
        )
    runs = page[:limit]
    next_cursor = _encode_cursor(runs[-1]) if len(page) > limit else None
    return {"runs": [run_summary(run) for run in runs], "next_cursor": next_cursor}


@router.get("/runs/{run_id}", response_model=models.Run, responses=_ERRORS)
async def get_run(run_id: _RunId, svc: _Svc) -> Any:
    """Public configuration, timeline and status. Never a mandate, a private feedback, a prompt
    or a credential."""
    return await svc.resources.resource(run_id)


# ---------------------------------------------------------------------------------------------
# Operations
# ---------------------------------------------------------------------------------------------


@router.post("/runs/{run_id}/validate", response_model=models.ValidationReport, responses=_ERRORS)
async def validate_run(run_id: _RunId, request: Request, svc: _Svc) -> Any:
    """Spec 3.1's setup validation. Every check is reported, passing or not."""
    await svc.resources.run(run_id)

    async def work() -> dict[str, Any]:
        return (await svc.controller.validate(run_id)).to_json()

    return await _sync(request, svc, "validate_run", run_id, work)


@router.post(
    "/runs/{run_id}/start", status_code=202, response_model=models.Operation, responses=_ERRORS
)
async def start_run(run_id: _RunId, request: Request, response: Response, svc: _Svc) -> Any:
    """Prepare the chain session if needed and run automatically. From `validated` or
    `paused`."""
    return await _long_running(
        request, response, svc, run_id, START, lambda: svc.controller.start(run_id)
    )


@router.post(
    "/runs/{run_id}/step", status_code=202, response_model=models.Operation, responses=_ERRORS
)
async def step_run(run_id: _RunId, request: Request, response: Response, svc: _Svc) -> Any:
    """Prepare the session if needed, then exactly one turn, and stay `paused`."""
    return await _long_running(
        request, response, svc, run_id, STEP, lambda: svc.controller.step(run_id)
    )


@router.post("/runs/{run_id}/pause", response_model=models.Run, responses=_ERRORS)
async def pause_run(run_id: _RunId, request: Request, svc: _Svc) -> Any:
    """Stop requesting decisions; a broadcast or confirmation in flight completes, and offer and
    session expiry continue."""
    await svc.resources.run(run_id)

    async def work() -> dict[str, Any]:
        await svc.controller.pause(run_id)
        return await svc.resources.resource(run_id)

    return await _sync(request, svc, "pause_run", run_id, work)


@router.post(
    "/runs/{run_id}/resume", status_code=202, response_model=models.Operation, responses=_ERRORS
)
async def resume_run(run_id: _RunId, request: Request, response: Response, svc: _Svc) -> Any:
    """Reconcile, then run on; from `recovery_required`, the recovery procedure."""
    return await _long_running(
        request, response, svc, run_id, RESUME, lambda: svc.controller.resume(run_id)
    )


@router.post(
    "/runs/{run_id}/abort", status_code=202, response_model=models.Operation, responses=_ERRORS
)
async def abort_run(
    run_id: _RunId,
    request: Request,
    response: Response,
    svc: _Svc,
    body: Annotated[models.AbortRequest | None, Body()] = None,
) -> Any:
    """End the session on chain: `abortSession`, or `expireSession` past the deadline. The
    operation's result says whether the abort or a settlement won."""
    reason = (body or models.AbortRequest()).reason
    return await _long_running(
        request, response, svc, run_id, ABORT, lambda: svc.controller.abort(run_id, reason)
    )


@router.post("/runs/{run_id}/clone", status_code=201, response_model=models.Run, responses=_ERRORS)
async def clone_run(
    run_id: _RunId,
    request: Request,
    svc: _Svc,
    patch: Annotated[
        dict[str, Any], Body(description="A JSON merge patch over the parent's request")
    ],
) -> Any:
    """A fresh `draft` run from the parent's request with the patch applied; its mandates are
    re-versioned. The parent is untouched."""
    await svc.resources.run(run_id)

    async def work() -> dict[str, Any]:
        run = await svc.controller.clone(run_id, patch)
        return await svc.resources.resource(run.id)

    return await _sync(request, svc, "clone_run", run_id, work)


# ---------------------------------------------------------------------------------------------
# Private views, the export and the metrics
# ---------------------------------------------------------------------------------------------


@router.get("/runs/{run_id}/mandates", response_model=models.Mandates, responses=_ERRORS)
async def get_mandates(run_id: _RunId, request: Request, svc: _Svc) -> Any:
    """Observer only, behind `X-Observer-Reveal: true`; never available to an agent."""
    require_reveal(request, "a run's mandates")
    return await svc.observer.mandates(run_id)


@router.get("/runs/{run_id}/decisions", response_model=models.Decisions, responses=_ERRORS)
async def get_decisions(run_id: _RunId, request: Request, svc: _Svc) -> Any:
    """Private operational records, raw responses and validation feedback included. Not one of
    them is an authorised offer (FR-U8)."""
    require_reveal(request, "a run's decision records")
    return await svc.observer.decisions(run_id)


@router.get(
    "/runs/{run_id}/export",
    responses={
        **_ERRORS,
        200: {
            "description": "The evidence export document; it validates against "
            "packages/protocol/schemas/export.v1.json.",
            "content": {"application/json": {"schema": {"type": "object"}}},
        },
    },
)
async def export_run(
    run_id: _RunId, request: Request, svc: _Svc, include_private: bool = False
) -> JSONResponse:
    """The evidence export (api_contract section 5). Private inputs only with
    `include_private=true` and the reveal header."""
    if include_private:
        require_reveal(request, "a private export")
    document = await svc.exporter.export(run_id, include_private=include_private)
    return JSONResponse(
        document,
        headers={"Content-Disposition": f'attachment; filename="run-{run_id}.export.json"'},
    )


@router.get("/runs/{run_id}/metrics", response_model=models.Metrics, responses=_ERRORS)
async def get_metrics(run_id: _RunId, request: Request, svc: _Svc) -> Any:
    """Spec 11.2, recomputed now, with the RPC request counts and estimated cost of ADR-061.
    The utilities need both mandates, and are null without the reveal header."""
    record = await svc.metrics.refresh(run_id)
    private = {key: None for key in svc.metrics.private(None)}
    if revealed(request):
        require_reveal(request, "a run's reservation utilities")
        private = svc.metrics.private(record)
    return {
        "run_id": str(run_id),
        **svc.metrics.public(record),
        "rpc_price_last_verified": svc.metrics.rpc_price_verified,
        **private,
    }


@router.get("/operations/{operation_id}", response_model=models.Operation, responses=_ERRORS)
async def get_operation(operation_id: uuid.UUID, svc: _Svc) -> Any:
    async with svc.transactions.unit_of_work() as uow:
        operation = await uow.operations.get(operation_id)
    if operation is None:
        raise RouteNotFoundError(f"no operation {operation_id}")
    return operation_view(operation)
