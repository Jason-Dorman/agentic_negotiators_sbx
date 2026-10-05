"""`GET /v1/runs/{run_id}/events`: the run's event log as server-sent events (api_contract 3).

Privacy-sensitive (docs/contributing.md section 1.2). Every event is a `run_events` row, which holds
public content only when it is appended (data model 3.14); this stream adds a second guard and
sends only the event types the contract lists, so a row of any other type never leaves.

Each event is `id: <cursor>`, `event: <type>`, `data: <json>`. A reconnect sends `Last-Event-ID`
and gets every event after that cursor, from the database, so a client that dropped misses nothing
(architecture section 11). A `: keepalive` comment follows every 15 s without an event.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Annotated, Final

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse

from api.db.records import RunEventRecord
from api.routes import models
from api.routes.errors import BadRequestError
from api.routes.services import Services, services

router = APIRouter(prefix="/v1", tags=["runs"])

#: api_contract section 3, exactly.
STREAMED: Final = frozenset(
    {
        "run.state",
        "turn.started",
        "turn.decision",
        "turn.signed",
        "tx.status",
        "chain.event",
        "chain.reorg",
        "balances",
        "metrics",
        "operation",
        "notice",
    }
)
KEEPALIVE: Final = ": keepalive\n\n"
#: `run_events.cursor` is BIGINT.
_MAX_CURSOR: Final = 2**63 - 1


def frame(event: RunEventRecord) -> str:
    data = json.dumps(event.data, separators=(",", ":"), sort_keys=True)
    return f"id: {event.cursor}\nevent: {event.event_type}\ndata: {data}\n\n"


def _cursor(last_event_id: str | None) -> int:
    if last_event_id is None or last_event_id == "":
        return 0
    try:
        cursor = int(last_event_id)
    except ValueError:
        raise BadRequestError("Last-Event-ID is a cursor this stream sent") from None
    if not 0 <= cursor <= _MAX_CURSOR:
        raise BadRequestError("Last-Event-ID is a cursor this stream sent")
    return cursor


async def _after(svc: Services, run_id: uuid.UUID, cursor: int) -> list[RunEventRecord]:
    async with svc.transactions.unit_of_work() as uow:
        return await uow.run_events.after(run_id, cursor)


async def stream(
    svc: Services,
    run_id: uuid.UUID,
    cursor: int,
    disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[str]:
    poll = svc.settings.event_poll_interval_s
    keepalive = svc.settings.sse_keepalive_s
    last_sent = time.monotonic()
    # ADR-082: a shutdown ends every stream at its next look; the client reconnects with
    # Last-Event-ID to whichever process serves next.
    while not svc.shutdown.requested and not await disconnected():
        # Shielded: a client that leaves cancels the stream, and a read cancelled half-way would
        # leave its database connection stranded rather than returned to the pool.
        events = await asyncio.shield(_after(svc, run_id, cursor))
        for event in events:
            cursor = event.cursor
            if event.event_type in STREAMED:
                last_sent = time.monotonic()
                yield frame(event)
        if events:
            continue
        if time.monotonic() - last_sent >= keepalive:
            last_sent = time.monotonic()
            yield KEEPALIVE
        await asyncio.sleep(poll)


@router.get(
    "/runs/{run_id}/events",
    responses={
        200: {
            "description": "Server-sent events (api_contract section 3).",
            "content": {"text/event-stream": {"schema": {"type": "string"}}},
        },
        **{status: {"model": models.ErrorEnvelope} for status in (400, 401, 404, 422)},
    },
    response_class=StreamingResponse,
)
async def run_events(
    run_id: uuid.UUID,
    request: Request,
    svc: Annotated[Services, Depends(services)],
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    cursor = _cursor(last_event_id)
    await svc.resources.run(run_id)  # 404 for an unknown run, before the stream opens
    return StreamingResponse(
        stream(svc, run_id, cursor, request.is_disconnected),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
