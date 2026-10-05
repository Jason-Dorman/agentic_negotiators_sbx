"""Operation records for the long-running routes: `start_run`, `step_run`, `resume_run`,
`abort_run` (api_contract section 1.2).

The route makes the run's transition and returns `202` with the operation `running`; this module
then watches the run's `run.state` events for the outcome the operation was for, and records it
(Q54):

- `start_run` succeeds when the run is `running`, or `terminal` if it ended first.
- `step_run` succeeds when the run is `paused` with cause `step_complete`, or `terminal`.
- `resume_run` succeeds when the run is `running`, `paused` or `preparing` with cause `recovered`,
  or `terminal`.
- Each of those fails when the run lands in `recovery_required` or `failed_setup`; a start or a
  step also fails when a reorg or a recovery leaves the run `paused` with cause `reorg` or
  `session_open` instead — the request did not complete, and resuming is a new operation (Q64).
- `abort_run` succeeds when the run is `terminal` or `failed_setup` — the result's outcome says
  whether the abort or a settlement won — and fails when it lands in `recovery_required` again.

The result is the deciding event's `{state, state_cause, outcome}`; a failure's error names the
state and its cause. Every status change is appended as an `operation` run event, so a client
watching the run's SSE stream sees it without polling. A restarted process tracks every accepted
operation again from the events written since it was created, and records every other unfinished
one `failed`, code `interrupted`, freeing its key: its request died with the process (Q63). A
watcher that meets a database error waits and tries again from where it was; it never gives up.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable, Mapping
from typing import Any, Final

import structlog

from api.db.enums import OperationStatus
from api.db.protocols import Transactions
from api.db.records import OperationRecord, RunRecord

_log = structlog.get_logger(component="routes")

START, STEP, RESUME, ABORT = "start_run", "step_run", "resume_run", "abort_run"
LONG_RUNNING: Final = frozenset({START, STEP, RESUME, ABORT})
#: Q64: a start or a step the chain interrupted ends paused with one of these, not as it asked.
_INTERRUPTED_PAUSES: Final = frozenset({"reorg", "session_open"})
#: A watcher's wait after a database error, doubling to the cap.
_RETRY_S: Final = 0.5
_RETRY_CAP_S: Final = 10.0


def interrupted() -> dict[str, Any]:
    """The error of an operation whose request never answered (Q63)."""
    return {
        "code": "interrupted",
        "message": "the request was interrupted before it answered; the run is as its state says",
        "details": {},
    }


def iso(moment: Any) -> str:
    return str(moment.isoformat()).replace("+00:00", "Z")


def operation_view(operation: OperationRecord) -> dict[str, Any]:
    return {
        "operation_id": str(operation.id),
        "kind": operation.kind,
        "run_id": None if operation.run_id is None else str(operation.run_id),
        "status": operation.status.value,
        "created_at": iso(operation.created_at),
        "updated_at": iso(operation.updated_at),
        "result": operation.result,
        "error": operation.error,
    }


def verdict(kind: str, data: Mapping[str, Any], *, initial: bool = False) -> OperationStatus | None:
    """What one `run.state` event means for an operation of this kind, or None if nothing yet.

    `initial` is the run as the route left it: an abort may be sent from `recovery_required`, so
    for an abort that state means nothing until a later event moves the run there again."""
    state, cause = data.get("state"), data.get("state_cause")
    if state == "recovery_required":
        return None if initial and kind == ABORT else OperationStatus.FAILED
    if state == "terminal":
        return OperationStatus.SUCCEEDED
    if state == "failed_setup":
        return OperationStatus.SUCCEEDED if kind == ABORT else OperationStatus.FAILED
    if kind in (START, STEP) and state == "paused" and cause in _INTERRUPTED_PAUSES:
        return OperationStatus.FAILED
    succeeded = (
        (kind == START and state == "running")
        or (kind == STEP and state == "paused" and cause == "step_complete")
        or (
            kind == RESUME
            and (state == "running" or (state in ("paused", "preparing") and cause == "recovered"))
        )
    )
    return OperationStatus.SUCCEEDED if succeeded else None


def state_data(run: RunRecord, outcome: Mapping[str, Any]) -> dict[str, Any]:
    return {"state": run.state.value, "state_cause": run.state_cause, "outcome": dict(outcome)}


class OperationTracker:
    def __init__(
        self,
        transactions: Transactions,
        *,
        poll_interval_s: float,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._transactions = transactions
        self._poll_interval_s = poll_interval_s
        self._sleep = sleep
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}

    async def cursor(self, run_id: uuid.UUID) -> int:
        """The run's latest event cursor: an operation watches what is written after it."""
        async with self._transactions.unit_of_work() as uow:
            return await uow.run_events.last_cursor(run_id)

    async def transition(
        self,
        operation: OperationRecord,
        status: OperationStatus,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> OperationRecord:
        """Record a status and append the `operation` run event in one unit of work."""
        async with self._transactions.unit_of_work() as uow:
            updated = await uow.operations.update(operation.id, status, result=result, error=error)
            if updated.run_id is not None:
                await uow.run_events.append(updated.run_id, "operation", operation_view(updated))
        return updated

    async def accepted(
        self, operation: OperationRecord, run: RunRecord, outcome: Mapping[str, Any], cursor: int
    ) -> OperationRecord:
        """The route made its transition: `running`, or already decided by the run as the route
        left it — a resume from recovery has done its work by the time it returns."""
        data = state_data(run, outcome)
        decided = verdict(operation.kind, data, initial=True)
        if decided is not None:
            return await self._finish(operation, decided, data)
        running = await self.transition(operation, OperationStatus.RUNNING)
        self._watch(running, cursor)
        return running

    def _watch(self, operation: OperationRecord, cursor: int) -> None:
        assert operation.run_id is not None
        task = asyncio.create_task(
            self._follow(operation, operation.run_id, cursor), name=f"operation-{operation.id}"
        )
        self._tasks[operation.id] = task

    async def _follow(self, operation: OperationRecord, run_id: uuid.UUID, cursor: int) -> None:
        """The watcher task's top level (docs/contributing.md section 2.1): a failure is logged,
        and the watcher waits and carries on from the last event it read — an operation is never
        left `running` because one read failed."""
        delay = _RETRY_S
        while True:
            try:
                decided, cursor = await self._read(operation, run_id, cursor)
            except Exception:
                _log.exception("operation.watch_failed", operation_id=str(operation.id))
                await self._sleep(delay)
                delay = min(delay * 2, _RETRY_CAP_S)
                continue
            delay = _RETRY_S
            if decided:
                return

    async def _read(
        self, operation: OperationRecord, run_id: uuid.UUID, cursor: int
    ) -> tuple[bool, int]:
        """One look at the events after `cursor`: whether the operation is now decided, and the
        cursor to look from next. Only a recorded verdict moves the cursor past its event."""
        async with self._transactions.unit_of_work() as uow:
            events = await uow.run_events.after(run_id, cursor)
        for event in events:
            if event.event_type == "run.state":
                decided = verdict(operation.kind, event.data)
                if decided is not None:
                    await self._finish(operation, decided, dict(event.data))
                    return True, event.cursor
            cursor = event.cursor
        if not events:
            await self._sleep(self._poll_interval_s)
        return False, cursor

    async def _finish(
        self, operation: OperationRecord, status: OperationStatus, data: dict[str, Any]
    ) -> OperationRecord:
        if status == OperationStatus.SUCCEEDED:
            return await self.transition(operation, status, result=data)
        error = {
            "code": str(data.get("state")),
            "message": f"the run is {data.get('state')}, cause {data.get('state_cause')}",
            "details": data,
        }
        return await self.transition(operation, status, error=error)

    async def resume_all(self) -> None:
        """At start-up: watch every accepted operation again, from the first event written after
        it was created; every other unfinished one — a request answered at once, or a long-running
        one never accepted — died with the process, and is `failed` with its key freed (Q63)."""
        async with self._transactions.unit_of_work() as uow:
            unfinished = await uow.operations.unfinished()
        for operation in unfinished:
            accepted = (
                operation.kind in LONG_RUNNING and operation.status == OperationStatus.RUNNING
            )
            if operation.run_id is None or not accepted:
                async with self._transactions.unit_of_work() as uow:
                    await uow.operations.update(
                        operation.id, OperationStatus.FAILED, error=interrupted()
                    )
                    await uow.operations.release_key(operation.id)
                _log.info("operation.interrupted", operation_id=str(operation.id))
                continue
            async with self._transactions.unit_of_work() as uow:
                cursor = await uow.run_events.last_cursor(operation.run_id, operation.created_at)
            self._watch(operation, cursor)
            _log.info("operation.resumed", operation_id=str(operation.id))

    async def wait(self) -> None:
        """Until every watched operation has finished (tests)."""
        for task in list(self._tasks.values()):
            await task

    async def aclose(self) -> None:
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
