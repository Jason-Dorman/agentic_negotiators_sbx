"""Runs, their private mandates, their wallets, and the single active run with its lease."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, literal, select, update
from sqlalchemy.dialects.postgresql import insert

from api.db.enums import Party, RunState
from api.db.errors import LeaseLostError
from api.db.models import ActiveRun, MandateVersion, Run, RunLease, Wallet
from api.db.protocols import LeaseRepository, MandateRepository, RunRepository, WalletRepository
from api.db.records import (
    LeaseRecord,
    MandateVersionRecord,
    NewMandate,
    NewRun,
    NewWallet,
    Outcome,
    RunRecord,
    WalletRecord,
)
from api.db.repositories._base import SqlRepository, required
from negotiation_protocol import Digest, SessionId, json_sha256


class SqlRunRepository(SqlRepository, RunRepository):
    async def add(self, new: NewRun) -> RunRecord:
        row = await self._write_scalar(insert(Run).values(**asdict(new)).returning(Run))
        return RunRecord.from_row(row)

    async def get(self, run_id: uuid.UUID) -> RunRecord | None:
        row = await self._get(Run, run_id)
        return None if row is None else RunRecord.from_row(row)

    async def get_for_update(self, run_id: uuid.UUID) -> RunRecord | None:
        row = await self._one_or_none(select(Run).where(Run.id == run_id).with_for_update())
        return None if row is None else RunRecord.from_row(row)

    async def with_open_sessions(self) -> list[RunRecord]:
        """Runs the indexer watches: a session id, and not finished. A `failed_setup` run is
        finished too — its session never opened, or was aborted and its outcome recorded (ADR-066)
        — and watching it would repeat its terminal event on every poll for good."""
        rows = await self._all(
            select(Run)
            .where(
                Run.session_id.is_not(None),
                Run.state.not_in((RunState.TERMINAL, RunState.FAILED_SETUP)),
            )
            .order_by(Run.created_at)
        )
        return [RunRecord.from_row(row) for row in rows]

    async def get_by_session(self, session_id: SessionId) -> RunRecord | None:
        row = await self._one_or_none(select(Run).where(Run.session_id == SessionId(session_id)))
        return None if row is None else RunRecord.from_row(row)

    async def _update(self, run_id: uuid.UUID, **values: Any) -> RunRecord:
        statement = update(Run).where(Run.id == run_id).values(**values).returning(Run)
        return RunRecord.from_row(required(await self._write_scalar(statement), f"run {run_id}"))

    async def update_state(
        self, run_id: uuid.UUID, state: RunState, cause: str | None = None
    ) -> RunRecord:
        return await self._update(run_id, state=state, state_cause=cause)

    async def set_session(
        self,
        run_id: uuid.UUID,
        session_id: SessionId,
        config_hash: Digest,
        expires_at_ts: int,
        started_at: datetime,
    ) -> RunRecord:
        return await self._update(
            run_id,
            session_id=session_id,
            config_hash=config_hash,
            session_expires_at_ts=expires_at_ts,
            started_at=started_at,
        )

    async def record_outcome(
        self,
        run_id: uuid.UUID,
        outcome: Outcome,
        terminal_at: datetime,
        state: RunState = RunState.TERMINAL,
        cause: str | None = None,
    ) -> RunRecord:
        """One statement for outcome and state, because the check constraint couples them."""
        return await self._update(
            run_id,
            outcome_kind=outcome.kind,
            outcome_reason_code=outcome.reason_code,
            outcome_actor=outcome.actor,
            outcome_tx_hash=outcome.tx_hash,
            terminal_at=terminal_at,
            state=state,
            state_cause=cause,
        )

    async def request_termination(
        self, run_id: uuid.UUID, cause: str, code: int | None
    ) -> RunRecord:
        statement = (
            update(Run)
            .where(Run.id == run_id, Run.termination_cause.is_(None))
            .values(termination_cause=cause, termination_code=code)
            .returning(Run)
        )
        row = await self._write_scalar(statement)
        if row is not None:
            return RunRecord.from_row(row)
        return required(await self.get(run_id), f"run {run_id}")

    async def set_versions(
        self,
        run_id: uuid.UUID,
        policy_versions: dict[str, Any],
        prompt_template_versions: dict[str, Any],
    ) -> RunRecord:
        return await self._update(
            run_id,
            policy_versions=dict(policy_versions),
            prompt_template_versions=dict(prompt_template_versions),
        )


class SqlMandateRepository(SqlRepository, MandateRepository):
    """**Private** rows. Immutable once written: a trigger refuses UPDATE and DELETE."""

    async def add(self, new: NewMandate) -> MandateVersionRecord:
        statement = (
            insert(MandateVersion)
            .values(
                run_id=new.run_id,
                party=new.party,
                version=new.version,
                reservation_price_minor=new.reservation_price_minor,
                min_remaining_inventory_minor=new.min_remaining_inventory_minor,
                instructions=new.instructions,
                mandate_hash=json_sha256(new.as_document()),
            )
            .returning(MandateVersion)
        )
        return MandateVersionRecord.from_row(await self._write_scalar(statement))

    async def get_for_party(self, run_id: uuid.UUID, party: Party) -> MandateVersionRecord | None:
        row = await self._one_or_none(
            select(MandateVersion).where(
                MandateVersion.run_id == run_id, MandateVersion.party == party
            )
        )
        return None if row is None else MandateVersionRecord.from_row(row)

    async def get_both(self, run_id: uuid.UUID) -> dict[Party, MandateVersionRecord]:
        rows = await self._all(select(MandateVersion).where(MandateVersion.run_id == run_id))
        return {row.party: MandateVersionRecord.from_row(row) for row in rows}


class SqlWalletRepository(SqlRepository, WalletRepository):
    async def add(self, new: NewWallet) -> WalletRecord:
        values = asdict(new)
        values["key_derivation"] = dict(new.key_derivation)
        row = await self._write_scalar(insert(Wallet).values(**values).returning(Wallet))
        return WalletRecord.from_row(row)

    async def get(self, run_id: uuid.UUID, party: Party) -> WalletRecord | None:
        row = await self._one_or_none(
            select(Wallet).where(Wallet.run_id == run_id, Wallet.party == party)
        )
        return None if row is None else WalletRecord.from_row(row)

    async def list_for_run(self, run_id: uuid.UUID) -> list[WalletRecord]:
        rows = await self._all(select(Wallet).where(Wallet.run_id == run_id).order_by(Wallet.party))
        return [WalletRecord.from_row(row) for row in rows]

    async def set_setup_nonce(self, run_id: uuid.UUID, party: Party, next_nonce: int) -> None:
        statement = (
            update(Wallet)
            .where(Wallet.run_id == run_id, Wallet.party == party)
            .values(setup_nonce_next=next_nonce)
            .returning(Wallet)
        )
        required(await self._write_scalar(statement), f"{party} wallet of run {run_id}")

    async def record_funding_tx(
        self, run_id: uuid.UUID, party: Party, label: str, tx_hash: Digest
    ) -> WalletRecord:
        merged = Wallet.funded_tx_hashes.op("||")(
            func.jsonb_build_object(literal(label), literal(str(tx_hash)))
        )
        statement = (
            update(Wallet)
            .where(Wallet.run_id == run_id, Wallet.party == party)
            .values(funded_tx_hashes=merged)
            .returning(Wallet)
        )
        row = required(await self._write_scalar(statement), f"{party} wallet of run {run_id}")
        return WalletRecord.from_row(row)


class SqlLeaseRepository(SqlRepository, LeaseRepository):
    """ADR-019: one active run, enforced by a single-row table, plus a lease with an expiry.

    The active-run row says *which* run may touch the chain. The lease says *which process* is
    driving it, and expires, so a crashed process's run can be reclaimed on restart (A13). The relay
    nonce lives on the lease, so only the holder can take one.
    """

    async def claim_active_run(self, run_id: uuid.UUID) -> bool:
        statement = (
            insert(ActiveRun)
            .values(id=1, run_id=run_id)
            .on_conflict_do_nothing(index_elements=[ActiveRun.id])
            .returning(ActiveRun)
        )
        if await self._write_scalar(statement) is not None:
            return True
        return await self.active_run_id() == run_id

    async def active_run_id(self) -> uuid.UUID | None:
        row = await self._get(ActiveRun, 1)
        return None if row is None else row.run_id

    async def release_active_run(self, run_id: uuid.UUID) -> None:
        await self._write(delete(ActiveRun).where(ActiveRun.id == 1, ActiveRun.run_id == run_id))

    async def acquire(
        self,
        run_id: uuid.UUID,
        holder: str,
        ttl: timedelta,
        now: datetime,
        relay_nonce_floor: int,
    ) -> LeaseRecord | None:
        """Insert, renew our own, or take over an expired one. None if another holder's is live.

        An existing lease keeps its `relay_nonce_next`: the counter belongs to the relay key, not to
        whoever happens to hold the lease, and resetting it on takeover would hand out a nonce the
        previous holder already used.
        """
        expires_at = now + ttl
        fresh = insert(RunLease).values(
            run_id=run_id, holder=holder, expires_at=expires_at, relay_nonce_next=relay_nonce_floor
        )
        upsert = fresh.on_conflict_do_update(
            index_elements=[RunLease.run_id],
            set_={"holder": holder, "expires_at": expires_at, "updated_at": func.now()},
            where=(RunLease.holder == holder) | (RunLease.expires_at <= now),
        ).returning(RunLease)
        row = await self._write_scalar(upsert)
        return None if row is None else LeaseRecord.from_row(row)

    async def release(self, run_id: uuid.UUID, holder: str) -> None:
        await self._write(
            delete(RunLease).where(RunLease.run_id == run_id, RunLease.holder == holder)
        )

    async def expired(self, now: datetime) -> list[LeaseRecord]:
        rows = await self._all(
            select(RunLease).where(RunLease.expires_at <= now).order_by(RunLease.expires_at)
        )
        return [LeaseRecord.from_row(row) for row in rows]

    async def take_relay_nonce(self, run_id: uuid.UUID, holder: str, chain_nonce: int) -> int:
        """Atomically: the larger of the stored nonce and the chain's, and store it plus one.

        The chain's pending nonce is the floor because something outside this process may have used
        the relay key; the stored one is the floor because a transaction this process signed may not
        be visible to the node yet. Taking the larger, in one statement, is what serialises the
        relay's nonces under the lease (docs/architecture.md section 8).
        """
        taken = func.greatest(RunLease.relay_nonce_next, chain_nonce)
        statement = (
            update(RunLease)
            .where(RunLease.run_id == run_id, RunLease.holder == holder)
            .values(relay_nonce_next=taken + 1)
            .returning(RunLease)
        )
        row = await self._write_scalar(statement)
        if row is None:
            raise LeaseLostError(f"{holder} does not hold the lease on run {run_id}")
        return int(row.relay_nonce_next) - 1
