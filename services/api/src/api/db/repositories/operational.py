"""Run metrics, the SSE event log, and operation records."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from typing import Any

from sqlalchemy import func, literal, select, update
from sqlalchemy.dialects.postgresql import JSONB, insert

from api.db.enums import OperationStatus
from api.db.models import Operation, Run, RunEvent, RunMetrics
from api.db.protocols import OperationRepository, RunEventRepository, RunMetricsRepository
from api.db.records import NewOperation, OperationRecord, RunEventRecord, RunMetricsRecord
from api.db.repositories._base import SqlRepository, required


class SqlRunMetricsRepository(SqlRepository, RunMetricsRepository):
    async def upsert(self, metrics: RunMetricsRecord) -> None:
        values = asdict(metrics)
        statement = insert(RunMetrics).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[RunMetrics.run_id],
            set_={
                **{name: statement.excluded[name] for name in values if name != "run_id"},
                "updated_at": func.now(),
            },
        )
        await self._write(statement)

    async def get(self, run_id: uuid.UUID) -> RunMetricsRecord | None:
        row = await self._get(RunMetrics, run_id)
        return None if row is None else RunMetricsRecord.from_row(row)


class SqlRunEventRepository(SqlRepository, RunEventRepository):
    """Append-only: a trigger refuses UPDATE and DELETE. The cursor is per run and gapless."""

    async def append(
        self, run_id: uuid.UUID, event_type: str, data: dict[str, Any]
    ) -> RunEventRecord:
        # Serialise appends for one run on its row. FOR NO KEY UPDATE rather than FOR UPDATE, so the
        # lock does not block the foreign-key checks of inserts into tables that refer to the run.
        locked = await self._one_or_none(
            select(Run).where(Run.id == run_id).with_for_update(key_share=True)
        )
        required(locked, f"run {run_id}")
        next_cursor = (
            select(func.coalesce(func.max(RunEvent.cursor), 0) + 1)
            .where(RunEvent.run_id == run_id)
            .scalar_subquery()
        )
        statement = (
            insert(RunEvent)
            .values(
                run_id=run_id,
                cursor=next_cursor,
                event_type=event_type,
                data=literal(dict(data), JSONB),
            )
            .returning(RunEvent)
        )
        return RunEventRecord.from_row(await self._write_scalar(statement))

    async def after(self, run_id: uuid.UUID, cursor: int, limit: int = 500) -> list[RunEventRecord]:
        rows = await self._all(
            select(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.cursor > cursor)
            .order_by(RunEvent.cursor)
            .limit(limit)
        )
        return [RunEventRecord.from_row(row) for row in rows]

    async def latest(self, run_id: uuid.UUID, event_type: str) -> RunEventRecord | None:
        row = await self._one_or_none(
            select(RunEvent)
            .where(RunEvent.run_id == run_id, RunEvent.event_type == event_type)
            .order_by(RunEvent.cursor.desc())
            .limit(1)
        )
        return None if row is None else RunEventRecord.from_row(row)


class SqlOperationRepository(SqlRepository, OperationRepository):
    async def create(self, new: NewOperation) -> OperationRecord:
        row = await self._write_scalar(insert(Operation).values(**asdict(new)).returning(Operation))
        return OperationRecord.from_row(row)

    async def get(self, operation_id: uuid.UUID) -> OperationRecord | None:
        row = await self._get(Operation, operation_id)
        return None if row is None else OperationRecord.from_row(row)

    async def get_by_key(self, route: str, idempotency_key: str) -> OperationRecord | None:
        row = await self._one_or_none(
            select(Operation).where(
                Operation.route == route, Operation.idempotency_key == idempotency_key
            )
        )
        return None if row is None else OperationRecord.from_row(row)

    async def update(
        self,
        operation_id: uuid.UUID,
        status: OperationStatus,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> OperationRecord:
        statement = (
            update(Operation)
            .where(Operation.id == operation_id)
            .values(status=status, result=result, error=error)
            .returning(Operation)
        )
        row = required(await self._write_scalar(statement), f"operation {operation_id}")
        return OperationRecord.from_row(row)
