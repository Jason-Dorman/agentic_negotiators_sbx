"""An in-memory stand-in for the database an `OperationTracker` reads, whose event reads fail a set
number of times first: what a database restart under a running operation looks like to it.

Only the two repositories the tracker touches are faked, `operations.update` and `run_events`; a
call to anything else is an AttributeError, so the fake cannot quietly stand in for more than it
says.
"""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

from api.db import OperationRecord, OperationStatus, RunEventRecord, RunState

NOW = datetime(2026, 10, 3, tzinfo=UTC)


class FlakyEvents:
    def __init__(self, *, failures: int) -> None:
        self.failures = failures
        self.failures_seen = 0
        self.recorded: list[OperationStatus] = []
        run_id = uuid.uuid4()
        self.operation = OperationRecord(
            id=uuid.uuid4(),
            kind="start_run",
            run_id=run_id,
            batch_id=None,
            idempotency_key=None,
            route="POST /v1/runs/x/start",
            request_hash="0x00",
            status=OperationStatus.PENDING,
            result=None,
            error=None,
            created_at=NOW,
            updated_at=NOW,
        )
        #: The run as the route left it: preparing, which decides nothing for a start.
        self.run: Any = SimpleNamespace(id=run_id, state=RunState.PREPARING, state_cause="start")

    @contextlib.asynccontextmanager
    async def unit_of_work(self) -> AsyncIterator[Any]:
        yield SimpleNamespace(operations=_Operations(self), run_events=_RunEvents(self))

    async def sleep(self, seconds: float) -> None:
        return None


class _Operations:
    def __init__(self, owner: FlakyEvents) -> None:
        self._owner = owner

    async def update(
        self,
        operation_id: uuid.UUID,
        status: OperationStatus,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> OperationRecord:
        self._owner.recorded.append(status)
        return replace(self._owner.operation, status=status, result=result, error=error)


class _RunEvents:
    def __init__(self, owner: FlakyEvents) -> None:
        self._owner = owner

    async def append(self, run_id: uuid.UUID, event_type: str, data: dict[str, Any]) -> None:
        return None

    async def after(self, run_id: uuid.UUID, cursor: int, limit: int = 500) -> list[RunEventRecord]:
        if self._owner.failures:
            self._owner.failures -= 1
            self._owner.failures_seen += 1
            raise OSError("the database restarted")
        if cursor >= 1:
            return []
        data = {"state": "running", "state_cause": None, "outcome": {"kind": "pending"}}
        return [RunEventRecord(run_id, 1, "run.state", data, NOW)]
