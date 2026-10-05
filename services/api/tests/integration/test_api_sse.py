"""`GET /v1/runs/{run_id}/events` (api_contract section 3), read from a real server.

`ASGITransport` collects a whole response before returning it, so these tests serve the app under
uvicorn on a free port and read the stream as a browser would: every event once, in cursor order,
`Last-Event-ID` replaying exactly what came after it, a keepalive in a silence, and nothing private
in any frame.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from api_chain import AnvilChain
from api_controller import ControllerHarness
from api_http import ApiHarness, serve

from api.db import Database
from api.routes.sse import STREAMED


@pytest.fixture
async def api(database: Database, chain: AnvilChain) -> AsyncIterator[ApiHarness]:
    harness = ApiHarness(ControllerHarness(database, chain), sse_keepalive_s=0.2)
    yield harness
    await harness.aclose()


async def _read(
    url: str, run_id: str, *, until: int, headers: dict[str, str] | None = None
) -> tuple[list[dict[str, Any]], list[str]]:
    """Frames until one with cursor `until` arrives, and every comment line seen meanwhile."""
    frames: list[dict[str, Any]] = []
    comments: list[str] = []
    current: dict[str, Any] = {}
    async with (
        httpx.AsyncClient(base_url=url, timeout=30) as client,
        client.stream("GET", f"/v1/runs/{run_id}/events", headers=headers) as response,
    ):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        async for line in response.aiter_lines():
            if line.startswith(":"):
                comments.append(line)
            elif line.startswith("id: "):
                current = {"id": int(line[4:])}
            elif line.startswith("event: "):
                current["event"] = line[7:]
            elif line.startswith("data: "):
                current["data"] = json.loads(line[6:])
            elif line == "" and current:
                frames.append(current)
                if current["id"] >= until:
                    break
                current = {}
    return frames, comments


async def _cursors(api: ApiHarness, run_id: str) -> list[tuple[int, str]]:
    async with api.database.unit_of_work() as uow:
        events = await uow.run_events.after(uuid.UUID(run_id), 0, limit=10_000)
    return [(event.cursor, event.event_type) for event in events]


async def test_the_stream_sends_every_event_once_and_replays_from_a_cursor(api: ApiHarness) -> None:
    run_id = await api.validated()
    await api.client.post(f"/v1/runs/{run_id}/start")
    await api.settle()
    stored = await _cursors(api, run_id)
    last = stored[-1][0]

    async with serve(api) as url:
        frames, _ = await _read(url, run_id, until=last)
        assert [(f["id"], f["event"]) for f in frames] == stored
        assert {f["event"] for f in frames} <= STREAMED
        for expected in (
            "run.state",
            "turn.decision",
            "chain.event",
            "balances",
            "metrics",
            "operation",
            "tx.status",
        ):
            assert expected in {f["event"] for f in frames}, expected

        middle = stored[len(stored) // 2][0]
        replayed, _ = await _read(url, run_id, until=last, headers={"Last-Event-ID": str(middle)})
        assert [f["id"] for f in replayed] == [c for c, _ in stored if c > middle]

    # Nothing private in any frame: no mandate number — whether a value, a bare number or inside a
    # sentence — no instruction, no feedback.
    text = json.dumps(frames)
    for secret in ("Pay as little", "Obtain as much", "feedback", "raw_response"):
        assert secret not in text
    numbers = _numbers([frame["data"] for frame in frames])
    assert numbers, "the scan read something"
    assert "100000000" not in numbers and "90000000" not in numbers


def _numbers(value: object) -> set[str]:
    """Every whole number in the value: each number, and each run of digits in each string. Whole
    runs, so a balance that contains 100000000 as a substring is not mistaken for it."""
    if isinstance(value, dict):
        return set().union(*(_numbers(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_numbers(item) for item in value))
    if isinstance(value, bool) or value is None:
        return set()
    if isinstance(value, int):
        return {str(value)}
    return set(re.findall(r"\d+", str(value)))


async def test_a_live_stream_carries_events_as_they_happen_and_keeps_alive(
    api: ApiHarness,
) -> None:
    run_id = await api.validated()
    before = (await _cursors(api, run_id))[-1][0]
    async with serve(api) as url:
        reader = asyncio.create_task(
            _read(url, run_id, until=before + 1, headers={"Last-Event-ID": str(before)})
        )
        await asyncio.sleep(0.5)  # silence: at least one keepalive at 0.2 s
        await api.client.post(f"/v1/runs/{run_id}/step")
        frames, comments = await asyncio.wait_for(reader, 30)
    assert frames[0]["id"] == before + 1
    assert ": keepalive" in comments
    await api.settle()


async def test_only_the_contracts_event_types_are_streamed(api: ApiHarness) -> None:
    """The second guard: a row of a type api_contract section 3 does not list never leaves, even
    if something appended one."""
    run = await api.create()
    run_id = uuid.UUID(run["run_id"])
    async with api.database.unit_of_work() as uow:
        await uow.run_events.append(run_id, "debug.private", {"mandate": "100000000"})
        last = await uow.run_events.append(run_id, "notice", {"level": "info", "message": "x"})
    async with serve(api) as url:
        frames, _ = await _read(url, str(run_id), until=last.cursor)
    assert "debug.private" not in {frame["event"] for frame in frames}
    assert frames[-1]["event"] == "notice"


async def test_a_stream_for_no_run_or_with_a_bad_cursor_is_refused(api: ApiHarness) -> None:
    missing = await api.client.get(f"/v1/runs/{uuid.uuid4()}/events")
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "not_found")
    run = await api.create()
    for cursor in ("seven", "-1", str(2**63)):
        bad = await api.client.get(
            f"/v1/runs/{run['run_id']}/events", headers={"Last-Event-ID": cursor}
        )
        assert (bad.status_code, bad.json()["error"]["code"]) == (400, "bad_request"), cursor
