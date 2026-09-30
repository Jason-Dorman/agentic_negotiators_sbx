"""The repository interfaces the rest of the backend depends on (docs/contributing.md section 2.1).

Upper layers type against these `Protocol`s, never against the SQL implementations, so unit tests
can hand them in-memory fakes and the composition root (`main.py`, a test fixture) decides which is
real. Each SQL repository subclasses its protocol explicitly, which makes mypy check that the two
have not drifted.

Three of these interfaces carry a rule, not only a shape:

- `MandateRepository` offers the acting party's row and, separately, both rows. The observation
  builder may use only the first; a test in stage 2.4 checks that it never names the second
  (docs/data_model.md section 3.4).
- `ChainEventRepository` and `BalanceSnapshotRepository` return canonical rows from every method a
  projection can use (data model invariant 6). The single method that also returns invalidated
  rows is named for the export, which must show a reorg that happened rather than hide it.
- `LeaseRepository` owns the single active run and the relay nonce, so the two can only be taken
  together (ADR-019).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from typing import Any, Protocol

from api.db.enums import ActionStatus, OperationStatus, Party, RunState, TurnState, TxStatus
from api.db.records import (
    BalanceSnapshotRecord,
    ChainEventRecord,
    DecisionRecord,
    DeploymentRecord,
    Inclusion,
    LeaseRecord,
    MandateVersionRecord,
    NewBalanceSnapshot,
    NewChainEvent,
    NewDecision,
    NewMandate,
    NewOperation,
    NewOutboxTx,
    NewRun,
    NewSignedAction,
    NewTurn,
    NewWallet,
    OperationRecord,
    OutboxRecord,
    Outcome,
    RunEventRecord,
    RunMetricsRecord,
    RunRecord,
    ScenarioRecord,
    SignedActionRecord,
    TurnRecord,
    WalletRecord,
)
from negotiation_protocol import Address, Digest, SessionId


class ScenarioRepository(Protocol):
    async def upsert(self, scenario: ScenarioRecord) -> None: ...
    async def get(self, scenario_id: str) -> ScenarioRecord | None: ...
    async def list_all(self) -> list[ScenarioRecord]: ...


class DeploymentRepository(Protocol):
    async def upsert(self, deployment: DeploymentRecord) -> None: ...
    async def get(self, deployment_id: str) -> DeploymentRecord | None: ...
    async def list_all(self) -> list[DeploymentRecord]: ...


class RunRepository(Protocol):
    async def add(self, new: NewRun) -> RunRecord: ...
    async def get(self, run_id: uuid.UUID) -> RunRecord | None: ...

    async def get_for_update(self, run_id: uuid.UUID) -> RunRecord | None:
        """The row, locked until the unit of work ends. Serialises state transitions."""
        ...

    async def update_state(
        self, run_id: uuid.UUID, state: RunState, cause: str | None = None
    ) -> RunRecord: ...

    async def set_session(
        self,
        run_id: uuid.UUID,
        session_id: SessionId,
        config_hash: Digest,
        expires_at_ts: int,
        started_at: datetime,
    ) -> RunRecord: ...

    async def record_outcome(
        self,
        run_id: uuid.UUID,
        outcome: Outcome,
        terminal_at: datetime,
        state: RunState = RunState.TERMINAL,
        cause: str | None = None,
    ) -> RunRecord:
        """Set the economic outcome, and the state with it: a check constraint couples the two.

        Only ever from a canonical terminal event (data model section 5).
        """
        ...

    async def set_versions(
        self,
        run_id: uuid.UUID,
        policy_versions: dict[str, Any],
        prompt_template_versions: dict[str, Any],
    ) -> RunRecord: ...


class MandateRepository(Protocol):
    """**Private** rows. See the module docstring for who may call which method."""

    async def add(self, new: NewMandate) -> MandateVersionRecord: ...

    async def get_for_party(self, run_id: uuid.UUID, party: Party) -> MandateVersionRecord | None:
        """The one row an observation builder may read: the acting party's own."""
        ...

    async def get_both(self, run_id: uuid.UUID) -> dict[Party, MandateVersionRecord]:
        """Both rows. Provisioning, the observer route, the private export and metrics only."""
        ...


class WalletRepository(Protocol):
    async def add(self, new: NewWallet) -> WalletRecord: ...
    async def get(self, run_id: uuid.UUID, party: Party) -> WalletRecord | None: ...
    async def list_for_run(self, run_id: uuid.UUID) -> list[WalletRecord]: ...
    async def set_setup_nonce(self, run_id: uuid.UUID, party: Party, next_nonce: int) -> None: ...

    async def record_funding_tx(
        self, run_id: uuid.UUID, party: Party, label: str, tx_hash: Digest
    ) -> WalletRecord: ...


class LeaseRepository(Protocol):
    async def claim_active_run(self, run_id: uuid.UUID) -> bool:
        """Take the single active-run slot. False if a different run holds it (ADR-019)."""
        ...

    async def active_run_id(self) -> uuid.UUID | None: ...
    async def release_active_run(self, run_id: uuid.UUID) -> None: ...

    async def acquire(
        self,
        run_id: uuid.UUID,
        holder: str,
        ttl: timedelta,
        now: datetime,
        relay_nonce_floor: int,
    ) -> LeaseRecord | None:
        """Take or renew the lease unless another holder's lease is still live."""
        ...

    async def release(self, run_id: uuid.UUID, holder: str) -> None: ...
    async def expired(self, now: datetime) -> list[LeaseRecord]: ...

    async def take_relay_nonce(self, run_id: uuid.UUID, holder: str, chain_nonce: int) -> int:
        """The next relay nonce: the larger of the stored one and the chain's, then advanced."""
        ...


class TurnRepository(Protocol):
    async def add(self, new: NewTurn) -> TurnRecord: ...
    async def get(self, turn_id: uuid.UUID) -> TurnRecord | None: ...
    async def get_by_number(self, run_id: uuid.UUID, turn: int) -> TurnRecord | None: ...
    async def list_for_run(self, run_id: uuid.UUID) -> list[TurnRecord]: ...

    async def update_state(
        self,
        turn_id: uuid.UUID,
        state: TurnState,
        *,
        finished_at: datetime | None = None,
        failure_code: str | None = None,
        failure_detail: str | None = None,
    ) -> TurnRecord: ...


class DecisionRepository(Protocol):
    """**Private** rows."""

    async def add(self, new: NewDecision) -> DecisionRecord: ...
    async def list_for_run(self, run_id: uuid.UUID) -> list[DecisionRecord]: ...
    async def list_for_party(self, run_id: uuid.UUID, party: Party) -> list[DecisionRecord]: ...
    async def mark_authorized(self, decision_id: uuid.UUID) -> DecisionRecord: ...


class SignedActionRepository(Protocol):
    async def add(self, new: NewSignedAction) -> SignedActionRecord: ...
    async def get(self, action_id: uuid.UUID) -> SignedActionRecord | None: ...
    async def get_by_digest(self, digest: Digest) -> SignedActionRecord | None: ...
    async def list_for_run(self, run_id: uuid.UUID) -> list[SignedActionRecord]: ...

    async def update_status(
        self, action_id: uuid.UUID, status: ActionStatus, revert_error: str | None = None
    ) -> SignedActionRecord: ...


class OutboxRepository(Protocol):
    async def add(self, new: NewOutboxTx) -> OutboxRecord: ...
    async def get(self, outbox_id: uuid.UUID) -> OutboxRecord | None: ...
    async def get_by_hash(self, tx_hash: Digest) -> OutboxRecord | None: ...
    async def list_for_run(self, run_id: uuid.UUID) -> list[OutboxRecord]: ...

    async def unfinished(self, run_id: uuid.UUID) -> list[OutboxRecord]:
        """Rows still `pending`, `submitted` or `included`: what recovery has to reconcile."""
        ...

    async def live_for_signed_action(self, signed_action_id: uuid.UUID) -> OutboxRecord | None: ...
    async def max_nonce(self, sender: Address) -> int | None: ...
    async def mark_submitted(self, outbox_id: uuid.UUID, at: datetime) -> OutboxRecord: ...

    async def mark_included(self, outbox_id: uuid.UUID, inclusion: Inclusion) -> OutboxRecord:
        """A receipt was seen. Status `included`; `confirmed` is the indexer's call."""
        ...

    async def update_status(
        self, outbox_id: uuid.UUID, status: TxStatus, last_error: str | None = None
    ) -> OutboxRecord: ...

    async def clear_inclusion(self, outbox_id: uuid.UUID) -> OutboxRecord:
        """A reorg removed the block: back to `submitted`, with no block recorded."""
        ...

    async def record_attempt(
        self, outbox_id: uuid.UUID, error: str | None = None
    ) -> OutboxRecord: ...


class ChainEventRepository(Protocol):
    """Canonical rows only, except `history_for_export`."""

    async def add(self, new: NewChainEvent) -> ChainEventRecord | None:
        """Insert unless this exact log in this exact block is already recorded. None if it was."""
        ...

    async def canonical_for_run(self, run_id: uuid.UUID) -> list[ChainEventRecord]: ...
    async def canonical_for_session(self, session_id: SessionId) -> list[ChainEventRecord]: ...

    async def canonical_from_block(
        self, chain_id: int, contract_address: Address, from_block: int
    ) -> list[ChainEventRecord]:
        """What a reorg check must re-verify: every canonical event at or above a height."""
        ...

    async def set_confirmations(self, event_id: uuid.UUID, confirmations: int) -> None: ...

    async def invalidate_from_block(
        self, chain_id: int, contract_address: Address, from_block: int, at: datetime
    ) -> list[ChainEventRecord]:
        """Mark every canonical event at or above `from_block` non-canonical. Returns them."""
        ...

    async def history_for_export(self, run_id: uuid.UUID) -> list[ChainEventRecord]:
        """Every row for the run, invalidated ones included. The export only."""
        ...


class BalanceSnapshotRepository(Protocol):
    async def add(self, new: NewBalanceSnapshot) -> BalanceSnapshotRecord | None: ...
    async def canonical_for_run(self, run_id: uuid.UUID) -> list[BalanceSnapshotRecord]: ...

    async def invalidate_from_block(
        self, run_id: uuid.UUID, from_block: int
    ) -> list[BalanceSnapshotRecord]: ...

    async def history_for_export(self, run_id: uuid.UUID) -> list[BalanceSnapshotRecord]: ...


class RunMetricsRepository(Protocol):
    async def upsert(self, metrics: RunMetricsRecord) -> None: ...
    async def get(self, run_id: uuid.UUID) -> RunMetricsRecord | None: ...


class RunEventRepository(Protocol):
    async def append(
        self, run_id: uuid.UUID, event_type: str, data: dict[str, Any]
    ) -> RunEventRecord:
        """Append with the next per-run cursor. `data` must already be public content."""
        ...

    async def after(
        self, run_id: uuid.UUID, cursor: int, limit: int = 500
    ) -> list[RunEventRecord]: ...


class OperationRepository(Protocol):
    async def create(self, new: NewOperation) -> OperationRecord:
        """Raises `DuplicateError` when `(route, idempotency_key)` is already taken."""
        ...

    async def get(self, operation_id: uuid.UUID) -> OperationRecord | None: ...
    async def get_by_key(self, route: str, idempotency_key: str) -> OperationRecord | None: ...

    async def update(
        self,
        operation_id: uuid.UUID,
        status: OperationStatus,
        *,
        result: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> OperationRecord: ...


class UnitOfWork(Protocol):
    """One database transaction, and every repository bound to it.

    Obtained from `Database.unit_of_work()`: it commits when the block exits normally and rolls back
    when it raises. Nothing the repositories return outlives it except records, which are values.
    """

    scenarios: ScenarioRepository
    deployments: DeploymentRepository
    runs: RunRepository
    mandates: MandateRepository
    wallets: WalletRepository
    leases: LeaseRepository
    turns: TurnRepository
    decisions: DecisionRepository
    signed_actions: SignedActionRepository
    outbox: OutboxRepository
    chain_events: ChainEventRepository
    balances: BalanceSnapshotRepository
    metrics: RunMetricsRepository
    run_events: RunEventRepository
    operations: OperationRepository


__all__ = [
    "BalanceSnapshotRepository",
    "ChainEventRepository",
    "DecisionRepository",
    "DeploymentRepository",
    "LeaseRepository",
    "MandateRepository",
    "OperationRepository",
    "OutboxRepository",
    "RunEventRepository",
    "RunMetricsRepository",
    "RunRepository",
    "ScenarioRepository",
    "SignedActionRepository",
    "TurnRepository",
    "UnitOfWork",
    "WalletRepository",
]
