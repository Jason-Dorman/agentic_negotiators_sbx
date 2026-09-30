"""The transaction outbox, the chain events the indexer records, and balance snapshots.

Canonicality is enforced here, where there is exactly one place to get it wrong: every method a
projection can call filters on `canonical = true` (data model invariant 6). The export is the one
reader that must see invalidated rows too — a reorg that removed an event is part of the record —
and it gets them from a method whose name says so, `history_for_export`, which the stage 2.3
projection test forbids a projection from calling.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert

from api.db.enums import TX_STATUSES_NOT_LIVE, TxStatus
from api.db.models import BalanceSnapshot, ChainEvent, OutboxTx
from api.db.protocols import BalanceSnapshotRepository, ChainEventRepository, OutboxRepository
from api.db.records import (
    BalanceSnapshotRecord,
    ChainEventRecord,
    Inclusion,
    NewBalanceSnapshot,
    NewChainEvent,
    NewOutboxTx,
    OutboxRecord,
)
from api.db.repositories._base import SqlRepository, required
from negotiation_protocol import Address, Digest, SessionId

#: Statuses recovery still has to reconcile (docs/architecture.md section 5.4).
UNFINISHED_TX_STATUSES = (TxStatus.PENDING, TxStatus.SUBMITTED, TxStatus.INCLUDED)


class SqlOutboxRepository(SqlRepository, OutboxRepository):
    async def add(self, new: NewOutboxTx) -> OutboxRecord:
        row = await self._write_scalar(insert(OutboxTx).values(**asdict(new)).returning(OutboxTx))
        return OutboxRecord.from_row(row)

    async def get(self, outbox_id: uuid.UUID) -> OutboxRecord | None:
        row = await self._get(OutboxTx, outbox_id)
        return None if row is None else OutboxRecord.from_row(row)

    async def get_by_hash(self, tx_hash: Digest) -> OutboxRecord | None:
        row = await self._one_or_none(select(OutboxTx).where(OutboxTx.tx_hash == Digest(tx_hash)))
        return None if row is None else OutboxRecord.from_row(row)

    async def list_for_run(self, run_id: uuid.UUID) -> list[OutboxRecord]:
        rows = await self._all(
            select(OutboxTx)
            .where(OutboxTx.run_id == run_id)
            .order_by(OutboxTx.created_at, OutboxTx.nonce)
        )
        return [OutboxRecord.from_row(row) for row in rows]

    async def unfinished(self, run_id: uuid.UUID) -> list[OutboxRecord]:
        rows = await self._all(
            select(OutboxTx)
            .where(OutboxTx.run_id == run_id, OutboxTx.status.in_(UNFINISHED_TX_STATUSES))
            .order_by(OutboxTx.sender, OutboxTx.nonce)
        )
        return [OutboxRecord.from_row(row) for row in rows]

    async def live_for_signed_action(self, signed_action_id: uuid.UUID) -> OutboxRecord | None:
        row = await self._one_or_none(
            select(OutboxTx).where(
                OutboxTx.signed_action_id == signed_action_id,
                OutboxTx.status.not_in(TX_STATUSES_NOT_LIVE),
            )
        )
        return None if row is None else OutboxRecord.from_row(row)

    async def max_nonce(self, sender: Address) -> int | None:
        result = await self._session.execute(
            select(func.max(OutboxTx.nonce)).where(OutboxTx.sender == Address(sender))
        )
        value = result.scalar_one_or_none()
        return None if value is None else int(value)

    async def _update(self, outbox_id: uuid.UUID, **values: Any) -> OutboxRecord:
        statement = (
            update(OutboxTx).where(OutboxTx.id == outbox_id).values(**values).returning(OutboxTx)
        )
        row = required(await self._write_scalar(statement), f"outbox row {outbox_id}")
        return OutboxRecord.from_row(row)

    async def mark_submitted(self, outbox_id: uuid.UUID, at: datetime) -> OutboxRecord:
        return await self._update(
            outbox_id,
            status=TxStatus.SUBMITTED,
            submitted_at=func.coalesce(OutboxTx.submitted_at, at),
            attempts=OutboxTx.attempts + 1,
            last_error=None,
        )

    async def mark_included(self, outbox_id: uuid.UUID, inclusion: Inclusion) -> OutboxRecord:
        return await self._update(
            outbox_id,
            status=TxStatus.INCLUDED,
            block_number=inclusion.block_number,
            block_hash=inclusion.block_hash,
            gas_used=inclusion.gas_used,
            effective_gas_price_wei=inclusion.effective_gas_price_wei,
            included_at=inclusion.included_at,
        )

    async def update_status(
        self, outbox_id: uuid.UUID, status: TxStatus, last_error: str | None = None
    ) -> OutboxRecord:
        values: dict[str, Any] = {"status": status}
        if last_error is not None:
            values["last_error"] = last_error
        return await self._update(outbox_id, **values)

    async def clear_inclusion(self, outbox_id: uuid.UUID) -> OutboxRecord:
        return await self._update(
            outbox_id,
            status=TxStatus.SUBMITTED,
            block_number=None,
            block_hash=None,
            gas_used=None,
            effective_gas_price_wei=None,
            included_at=None,
        )

    async def record_attempt(self, outbox_id: uuid.UUID, error: str | None = None) -> OutboxRecord:
        return await self._update(outbox_id, attempts=OutboxTx.attempts + 1, last_error=error)


class SqlChainEventRepository(SqlRepository, ChainEventRepository):
    """Canonical rows only, except `history_for_export`."""

    async def add(self, new: NewChainEvent) -> ChainEventRecord | None:
        values = asdict(new)
        values["decoded"] = dict(new.decoded)
        statement = (
            insert(ChainEvent)
            .values(**values)
            .on_conflict_do_nothing(
                index_elements=[ChainEvent.block_hash, ChainEvent.tx_hash, ChainEvent.log_index]
            )
            .returning(ChainEvent)
        )
        row = await self._write_scalar(statement)
        return None if row is None else ChainEventRecord.from_row(row)

    def _canonical(self) -> Any:
        return select(ChainEvent).where(ChainEvent.canonical.is_(True))

    @staticmethod
    def _chain_order() -> tuple[Any, ...]:
        # Block then log index: the order the chain put them in, which is a fact about the chain
        # rather than about when this process happened to see them.
        return (ChainEvent.block_number, ChainEvent.log_index)

    async def canonical_for_run(self, run_id: uuid.UUID) -> list[ChainEventRecord]:
        rows = await self._all(
            self._canonical().where(ChainEvent.run_id == run_id).order_by(*self._chain_order())
        )
        return [ChainEventRecord.from_row(row) for row in rows]

    async def canonical_for_session(self, session_id: SessionId) -> list[ChainEventRecord]:
        rows = await self._all(
            self._canonical()
            .where(ChainEvent.session_id == SessionId(session_id))
            .order_by(*self._chain_order())
        )
        return [ChainEventRecord.from_row(row) for row in rows]

    async def canonical_from_block(
        self, chain_id: int, contract_address: Address, from_block: int
    ) -> list[ChainEventRecord]:
        rows = await self._all(
            self._canonical()
            .where(
                ChainEvent.chain_id == chain_id,
                ChainEvent.contract_address == Address(contract_address),
                ChainEvent.block_number >= from_block,
            )
            .order_by(*self._chain_order())
        )
        return [ChainEventRecord.from_row(row) for row in rows]

    async def set_confirmations(self, event_id: uuid.UUID, confirmations: int) -> None:
        statement = (
            update(ChainEvent)
            .where(ChainEvent.id == event_id)
            .values(confirmations_at_index=confirmations)
            .returning(ChainEvent)
        )
        required(await self._write_scalar(statement), f"chain event {event_id}")

    async def invalidate_from_block(
        self, chain_id: int, contract_address: Address, from_block: int, at: datetime
    ) -> list[ChainEventRecord]:
        statement = (
            update(ChainEvent)
            .where(
                ChainEvent.canonical.is_(True),
                ChainEvent.chain_id == chain_id,
                ChainEvent.contract_address == Address(contract_address),
                ChainEvent.block_number >= from_block,
            )
            .values(canonical=False, invalidated_at=at)
            .returning(ChainEvent)
        )
        rows = await self._write_all(statement)
        records = [ChainEventRecord.from_row(row) for row in rows]
        return sorted(records, key=lambda record: (record.block_number, record.log_index))

    async def history_for_export(self, run_id: uuid.UUID) -> list[ChainEventRecord]:
        rows = await self._all(
            select(ChainEvent)
            .where(ChainEvent.run_id == run_id)
            .order_by(*self._chain_order(), ChainEvent.created_at)
        )
        return [ChainEventRecord.from_row(row) for row in rows]


class SqlBalanceSnapshotRepository(SqlRepository, BalanceSnapshotRepository):
    """Canonical rows only, except `history_for_export`."""

    async def add(self, new: NewBalanceSnapshot) -> BalanceSnapshotRecord | None:
        statement = (
            insert(BalanceSnapshot)
            .values(**asdict(new))
            .on_conflict_do_nothing(
                index_elements=[
                    BalanceSnapshot.run_id,
                    BalanceSnapshot.stage,
                    BalanceSnapshot.party,
                    BalanceSnapshot.token,
                    BalanceSnapshot.block_hash,
                ]
            )
            .returning(BalanceSnapshot)
        )
        row = await self._write_scalar(statement)
        return None if row is None else BalanceSnapshotRecord.from_row(row)

    async def canonical_for_run(self, run_id: uuid.UUID) -> list[BalanceSnapshotRecord]:
        rows = await self._all(
            select(BalanceSnapshot)
            .where(BalanceSnapshot.run_id == run_id, BalanceSnapshot.canonical.is_(True))
            .order_by(BalanceSnapshot.block_number, BalanceSnapshot.stage, BalanceSnapshot.party)
        )
        return [BalanceSnapshotRecord.from_row(row) for row in rows]

    async def invalidate_from_block(
        self, run_id: uuid.UUID, from_block: int
    ) -> list[BalanceSnapshotRecord]:
        statement = (
            update(BalanceSnapshot)
            .where(
                BalanceSnapshot.run_id == run_id,
                BalanceSnapshot.canonical.is_(True),
                BalanceSnapshot.block_number >= from_block,
            )
            .values(canonical=False)
            .returning(BalanceSnapshot)
        )
        return [BalanceSnapshotRecord.from_row(row) for row in await self._write_all(statement)]

    async def history_for_export(self, run_id: uuid.UUID) -> list[BalanceSnapshotRecord]:
        rows = await self._all(
            select(BalanceSnapshot)
            .where(BalanceSnapshot.run_id == run_id)
            .order_by(BalanceSnapshot.block_number, BalanceSnapshot.stage, BalanceSnapshot.party)
        )
        return [BalanceSnapshotRecord.from_row(row) for row in rows]
