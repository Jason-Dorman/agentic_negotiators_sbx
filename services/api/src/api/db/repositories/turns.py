"""Turns, the decision attempts inside them, and the signed actions they authorise."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Any, Final

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert

from api.db.enums import ActionStatus, Party, TurnState
from api.db.models import Decision, SignedAction, Turn
from api.db.protocols import DecisionRepository, SignedActionRepository, TurnRepository
from api.db.records import (
    DecisionRecord,
    NewDecision,
    NewSignedAction,
    NewTurn,
    SignedActionRecord,
    TurnRecord,
)
from api.db.repositories._base import SqlRepository, required
from negotiation_protocol import Digest


class SqlTurnRepository(SqlRepository, TurnRepository):
    """`observation` is **private**: it carries the acting party's own mandate."""

    async def add(self, new: NewTurn) -> TurnRecord:
        values = asdict(new)
        values["observation"] = dict(new.observation)
        row = await self._write_scalar(insert(Turn).values(**values).returning(Turn))
        return TurnRecord.from_row(row)

    async def get(self, turn_id: uuid.UUID) -> TurnRecord | None:
        row = await self._get(Turn, turn_id)
        return None if row is None else TurnRecord.from_row(row)

    async def get_by_number(self, run_id: uuid.UUID, turn: int) -> TurnRecord | None:
        row = await self._one_or_none(select(Turn).where(Turn.run_id == run_id, Turn.turn == turn))
        return None if row is None else TurnRecord.from_row(row)

    async def list_for_run(self, run_id: uuid.UUID) -> list[TurnRecord]:
        rows = await self._all(select(Turn).where(Turn.run_id == run_id).order_by(Turn.turn))
        return [TurnRecord.from_row(row) for row in rows]

    async def update_state(
        self,
        turn_id: uuid.UUID,
        state: TurnState,
        *,
        finished_at: datetime | None = None,
        failure_code: str | None = None,
        failure_detail: str | None = None,
    ) -> TurnRecord:
        values: dict[str, Any] = {"state": state}
        if finished_at is not None:
            values["finished_at"] = finished_at
        if failure_code is not None:
            # An agent's failure can quote a provider's error type, which can hold a NUL (ADR-090).
            values["failure_code"] = (
                escape_nul(failure_code) if has_nul(failure_code) else failure_code
            )
            values["failure_detail"] = (
                escape_nul(failure_detail) if has_nul(failure_detail) else failure_detail
            )
        statement = update(Turn).where(Turn.id == turn_id).values(**values).returning(Turn)
        return TurnRecord.from_row(required(await self._write_scalar(statement), f"turn {turn_id}"))


class SqlDecisionRepository(SqlRepository, DecisionRepository):
    """**Private** rows: raw model output and the validation feedback it drew."""

    async def add(self, new: NewDecision) -> DecisionRecord:
        """Stored as the agent reported it, except when the response, its stop reason or its
        feedback holds a NUL, which JSONB and TEXT refuse: then every string of those three is
        escaped — each backslash doubled, each NUL written as the six characters `\\u0000` — and
        the record says so (ADR-090, Q71). Doubling the backslashes keeps the escape reversible, so
        a NUL and the literal text `\\u0000` never become the same key or value."""
        values = asdict(new)
        escaped = any(
            has_nul(new_value)
            for new_value in (new.raw_response, new.stop_reason, new.validation_feedback)
        )
        if escaped:
            for name in ("raw_response", "stop_reason", "validation_feedback"):
                values[name] = escape_nul(values[name])
        values["raw_response_escaped"] = escaped
        row = await self._write_scalar(insert(Decision).values(**values).returning(Decision))
        return DecisionRecord.from_row(row)

    async def list_for_run(self, run_id: uuid.UUID) -> list[DecisionRecord]:
        rows = await self._all(
            select(Decision)
            .join(Turn, Turn.id == Decision.turn_id)
            .where(Decision.run_id == run_id)
            .order_by(Turn.turn, Decision.attempt)
        )
        return [DecisionRecord.from_row(row) for row in rows]

    async def list_for_party(self, run_id: uuid.UUID, party: Party) -> list[DecisionRecord]:
        rows = await self._all(
            select(Decision)
            .join(Turn, Turn.id == Decision.turn_id)
            .where(Decision.run_id == run_id, Decision.party == party)
            .order_by(Turn.turn, Decision.attempt)
        )
        return [DecisionRecord.from_row(row) for row in rows]

    async def mark_authorized(self, decision_id: uuid.UUID) -> DecisionRecord:
        """The check constraint refuses this for a decision that did not validate (invariant 4)."""
        statement = (
            update(Decision)
            .where(Decision.id == decision_id)
            .values(authorized=True)
            .returning(Decision)
        )
        row = required(await self._write_scalar(statement), f"decision {decision_id}")
        return DecisionRecord.from_row(row)


class SqlSignedActionRepository(SqlRepository, SignedActionRepository):
    """Identity is the digest. `(run_id, sequence)` unique makes a duplicate action impossible."""

    async def add(self, new: NewSignedAction) -> SignedActionRecord:
        values = asdict(new)
        values["typed_message"] = dict(new.typed_message)
        row = await self._write_scalar(
            insert(SignedAction).values(**values).returning(SignedAction)
        )
        return SignedActionRecord.from_row(row)

    async def get(self, action_id: uuid.UUID) -> SignedActionRecord | None:
        row = await self._get(SignedAction, action_id)
        return None if row is None else SignedActionRecord.from_row(row)

    async def get_by_digest(self, digest: Digest) -> SignedActionRecord | None:
        row = await self._one_or_none(
            select(SignedAction).where(SignedAction.digest == Digest(digest))
        )
        return None if row is None else SignedActionRecord.from_row(row)

    async def list_for_run(self, run_id: uuid.UUID) -> list[SignedActionRecord]:
        rows = await self._all(
            select(SignedAction)
            .where(SignedAction.run_id == run_id)
            .order_by(SignedAction.sequence)
        )
        return [SignedActionRecord.from_row(row) for row in rows]

    async def reset_to_submitted(self, action_id: uuid.UUID) -> SignedActionRecord:
        """A reorg removed the block that included it: back to `submitted`, revert error cleared."""
        statement = (
            update(SignedAction)
            .where(SignedAction.id == action_id)
            .values(status=ActionStatus.SUBMITTED, revert_error=None)
            .returning(SignedAction)
        )
        row = required(await self._write_scalar(statement), f"signed action {action_id}")
        return SignedActionRecord.from_row(row)

    async def update_status(
        self, action_id: uuid.UUID, status: ActionStatus, revert_error: str | None = None
    ) -> SignedActionRecord:
        values: dict[str, Any] = {"status": status}
        if revert_error is not None:
            values["revert_error"] = revert_error
        statement = (
            update(SignedAction)
            .where(SignedAction.id == action_id)
            .values(**values)
            .returning(SignedAction)
        )
        row = required(await self._write_scalar(statement), f"signed action {action_id}")
        return SignedActionRecord.from_row(row)


NUL: Final = "\x00"


def has_nul(value: Any) -> bool:
    """Whether any string in `value`, keys included, at any depth, holds a NUL."""
    if isinstance(value, str):
        return NUL in value
    if isinstance(value, dict):
        return any(has_nul(key) or has_nul(item) for key, item in value.items())
    if isinstance(value, list):
        return any(has_nul(item) for item in value)
    return False


def escape_nul(value: Any) -> Any:
    """Every string in `value`, keys included, with each backslash doubled and each NUL written as
    `\\u0000`: reversible, and storable as JSONB and TEXT. JSON's structure is otherwise
    untouched."""
    if isinstance(value, str):
        return value.replace("\\", "\\\\").replace(NUL, "\\u0000")
    if isinstance(value, dict):
        return {escape_nul(key): escape_nul(item) for key, item in value.items()}
    if isinstance(value, list):
        return [escape_nul(item) for item in value]
    return value
