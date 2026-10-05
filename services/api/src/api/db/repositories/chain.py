"""The transaction outbox, the chain events the indexer records, and balance snapshots.

Canonicality is enforced here, where there is exactly one place to get it wrong: every method a
projection can call filters on `canonical = true` (data model invariant 6). The export is the one
reader that must see invalidated rows too — a reorg that removed an event is part of the record —
and it gets them from a method whose name says so, `history_for_export`, which the stage 2.3
projection test forbids a projection from calling.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime
from typing import Any

from sqlalchemy import exists, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import aliased

from api.db.enums import TX_STATUSES_NOT_LIVE, RunState, TxStatus
from api.db.models import BalanceSnapshot, ChainEvent, OutboxTx, Run
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

#: A transaction in one of these, with no block, may still be mined: a `replaced` original can win
#: the race against its own replacement, and a row recovery `dropped` as superseded can turn out,
#: after a reorg, to be the one that lands.
AWAITING_RECEIPT_STATUSES = (
    TxStatus.PENDING,
    TxStatus.SUBMITTED,
    TxStatus.REPLACED,
    TxStatus.DROPPED,
)


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

    async def for_signed_action(self, signed_action_id: uuid.UUID) -> list[OutboxRecord]:
        rows = await self._all(
            select(OutboxTx)
            .where(OutboxTx.signed_action_id == signed_action_id)
            .order_by(OutboxTx.created_at)
        )
        return [OutboxRecord.from_row(row) for row in rows]

    async def nonce_group(
        self, sender: Address, nonce: int, deployment_id: str
    ) -> list[OutboxRecord]:
        rows = await self._all(
            select(OutboxTx)
            .join(Run, Run.id == OutboxTx.run_id)
            .where(
                OutboxTx.sender == Address(sender),
                OutboxTx.nonce == nonce,
                Run.deployment_id == deployment_id,
            )
            .order_by(OutboxTx.created_at)
        )
        return [OutboxRecord.from_row(row) for row in rows]

    async def awaiting_receipt(self, deployment_id: str) -> list[OutboxRecord]:
        sibling = aliased(OutboxTx)
        # ADR-081: a nonce is a nonce on one chain; another deployment's row at the same sender
        # and nonce says nothing about this one.
        on_this_chain = select(Run.id).where(Run.deployment_id == deployment_id)
        group_included = exists().where(
            sibling.sender == OutboxTx.sender,
            sibling.nonce == OutboxTx.nonce,
            sibling.block_number.is_not(None),
            sibling.run_id.in_(on_this_chain),
        )
        rows = await self._all(
            select(OutboxTx)
            .join(Run, Run.id == OutboxTx.run_id)
            .where(
                OutboxTx.status.in_(AWAITING_RECEIPT_STATUSES),
                OutboxTx.block_number.is_(None),
                Run.state != RunState.TERMINAL,
                Run.deployment_id == deployment_id,
                ~group_included,
            )
            .order_by(OutboxTx.sender, OutboxTx.nonce, OutboxTx.created_at)
        )
        return [OutboxRecord.from_row(row) for row in rows]

    async def with_block_from(self, from_block: int, deployment_id: str) -> list[OutboxRecord]:
        rows = await self._all(
            select(OutboxTx)
            .join(Run, Run.id == OutboxTx.run_id)
            .where(OutboxTx.block_number >= from_block, Run.deployment_id == deployment_id)
            .order_by(OutboxTx.block_number, OutboxTx.nonce)
        )
        return [OutboxRecord.from_row(row) for row in rows]

    async def in_status(self, *statuses: TxStatus, deployment_id: str) -> list[OutboxRecord]:
        rows = await self._all(
            select(OutboxTx)
            .join(Run, Run.id == OutboxTx.run_id)
            .where(OutboxTx.status.in_(statuses), Run.deployment_id == deployment_id)
            .order_by(OutboxTx.sender, OutboxTx.nonce, OutboxTx.created_at)
        )
        return [OutboxRecord.from_row(row) for row in rows]

    async def max_nonce(self, sender: Address, deployment_id: str) -> int | None:
        result = await self._session.execute(
            select(func.max(OutboxTx.nonce))
            .join(Run, Run.id == OutboxTx.run_id)
            .where(OutboxTx.sender == Address(sender), Run.deployment_id == deployment_id)
        )
        value = result.scalar_one_or_none()
        return None if value is None else int(value)

    async def _update(self, outbox_id: uuid.UUID, **values: Any) -> OutboxRecord:
        statement = (
            update(OutboxTx).where(OutboxTx.id == outbox_id).values(**values).returning(OutboxTx)
        )
        row = required(await self._write_scalar(statement), f"outbox row {outbox_id}")
        return OutboxRecord.from_row(row)

    async def mark_submitted(
        self, outbox_id: uuid.UUID, at: datetime, submitted_block: int | None = None
    ) -> OutboxRecord:
        return await self._update(
            outbox_id,
            status=TxStatus.SUBMITTED,
            submitted_at=func.coalesce(OutboxTx.submitted_at, at),
            submitted_block=func.coalesce(OutboxTx.submitted_block, submitted_block),
            attempts=OutboxTx.attempts + 1,
            last_error=None,
        )

    async def mark_reverted(self, outbox_id: uuid.UUID, error: str, sentence: str) -> OutboxRecord:
        return await self._update(
            outbox_id, status=TxStatus.REVERTED, last_error=error, sentence=sentence
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
        # A revert recorded from the removed block was a fact about that block only: its error and
        # sentence go with it, and are written again if the transaction reverts on the new fork.
        return await self._update(
            outbox_id,
            status=TxStatus.SUBMITTED,
            block_number=None,
            block_hash=None,
            gas_used=None,
            effective_gas_price_wei=None,
            included_at=None,
            last_error=None,
            sentence=None,
        )

    async def record_attempt(self, outbox_id: uuid.UUID, error: str | None = None) -> OutboxRecord:
        return await self._update(outbox_id, attempts=OutboxTx.attempts + 1, last_error=error)


class SqlChainEventRepository(SqlRepository, ChainEventRepository):
    """Canonical rows only, except `history_for_export`."""

    async def add(self, new: NewChainEvent) -> ChainEventRecord | None:
        """Insert, or make an invalidated row canonical again (ADR-055); None if it already was.

        The key includes the block hash, so a row that comes back under the same key is the same
        log in the same block: the block is canonical again — a reorg that flipped back, or a
        rewind that should not have happened — and the evidence row is restored rather than lost.
        """
        values = asdict(new)
        values["decoded"] = dict(new.decoded)
        inserted = insert(ChainEvent).values(**values)
        statement = inserted.on_conflict_do_update(
            index_elements=[ChainEvent.block_hash, ChainEvent.tx_hash, ChainEvent.log_index],
            set_={
                "canonical": True,
                "invalidated_at": None,
                "confirmations_at_index": inserted.excluded.confirmations_at_index,
                "updated_at": func.now(),
            },
            where=ChainEvent.canonical.is_(False),
        ).returning(ChainEvent)
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

    async def invalidate_blocks(
        self, chain_id: int, contract_address: Address, block_hashes: Sequence[Digest], at: datetime
    ) -> list[ChainEventRecord]:
        if not block_hashes:
            return []
        statement = (
            update(ChainEvent)
            .where(
                ChainEvent.canonical.is_(True),
                ChainEvent.chain_id == chain_id,
                ChainEvent.contract_address == Address(contract_address),
                ChainEvent.block_hash.in_([Digest(value) for value in block_hashes]),
            )
            .values(canonical=False, invalidated_at=at)
            .returning(ChainEvent)
        )
        records = [ChainEventRecord.from_row(row) for row in await self._write_all(statement)]
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
        """Insert, or make an invalidated snapshot of the same block canonical again (ADR-055)."""
        statement = (
            insert(BalanceSnapshot)
            .values(**asdict(new))
            .on_conflict_do_update(
                index_elements=[
                    BalanceSnapshot.run_id,
                    BalanceSnapshot.stage,
                    BalanceSnapshot.party,
                    BalanceSnapshot.token,
                    BalanceSnapshot.block_hash,
                ],
                set_={"canonical": True, "updated_at": func.now()},
                where=BalanceSnapshot.canonical.is_(False),
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

    async def invalidate_blocks(
        self, run_id: uuid.UUID, block_hashes: Sequence[Digest]
    ) -> list[BalanceSnapshotRecord]:
        if not block_hashes:
            return []
        statement = (
            update(BalanceSnapshot)
            .where(
                BalanceSnapshot.run_id == run_id,
                BalanceSnapshot.canonical.is_(True),
                BalanceSnapshot.block_hash.in_([Digest(value) for value in block_hashes]),
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
