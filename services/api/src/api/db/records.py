"""What the repositories hand to the rest of the backend: frozen records, never ORM objects.

An ORM object is a live handle on a session. Passed upward it would let a route lazy-load a
relationship, or mutate a row that a later flush writes back, outside the unit of work that owns it
— which is exactly the coupling `.importlinter`'s `sessions-stay-in-db` contract exists to prevent.
A record is a value: it can be passed anywhere, compared, and logged by field, and it cannot write.

Field names are the column names, so a record is built from a row mechanically (`from_row`). The
`New*` classes are the inputs to an insert: everything the caller decides, and nothing the database
assigns (ids, timestamps, defaults).

JSON columns are typed `dict[str, Any]`. A frozen dataclass does not freeze a dict inside it; the
convention is that nobody mutates one, and the repositories copy on the way in.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, fields
from datetime import datetime
from decimal import Decimal
from typing import Any, Self

from api.db.enums import (
    ActionKind,
    ActionStatus,
    OperationStatus,
    OutcomeKind,
    Party,
    PartyOrOperator,
    PolicyKind,
    RunMode,
    RunState,
    SnapshotStage,
    TokenRole,
    TurnState,
    TxKind,
    TxStatus,
)
from negotiation_protocol import Address, Digest, MinorAmount, SessionId


class _FromRow:
    """Build a record from any object that has an attribute per field — an ORM row, in practice."""

    __slots__ = ()

    @classmethod
    def from_row(cls, row: object) -> Self:
        names = [item.name for item in fields(cls)]  # type: ignore[arg-type]  # reason: every subclass is a dataclass
        return cls(**{name: getattr(row, name) for name in names})


# ---------------------------------------------------------------------------------------------
# Reference data
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ScenarioRecord(_FromRow):
    """Section 3.1. The two templates hold mandates and are private."""

    scenario_id: str
    name: str
    description: str
    public_config: dict[str, Any]
    buyer_template: dict[str, Any]
    seller_template: dict[str, Any]
    source_hash: str


@dataclass(frozen=True, slots=True)
class DeploymentRecord(_FromRow):
    deployment_id: str
    chain_id: int
    protocol_version: str
    exchange_address: Address
    base_token_address: Address
    quote_token_address: Address
    operator_address: Address
    relay_address: Address
    code_hashes: dict[str, Any]
    compiler: dict[str, Any]
    explorer_base_url: str | None
    ens: dict[str, Any] | None
    manifest: dict[str, Any]
    start_block: int
    deployed_at: datetime
    #: ADR-081: block 0 of the chain the deployment was loaded against; None when unknown.
    genesis_hash: Digest | None = None


# ---------------------------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NewRun:
    name: str
    deployment_id: str
    public_config: dict[str, Any]
    limits: dict[str, Any]
    buyer_policy: PolicyKind
    seller_policy: PolicyKind
    software_version: str
    mode: RunMode
    scenario_id: str | None = None
    parent_run_id: uuid.UUID | None = None
    batch_id: uuid.UUID | None = None
    buyer_model_id: str | None = None
    seller_model_id: str | None = None
    buyer_effort: str | None = None
    seller_effort: str | None = None
    state: RunState = RunState.DRAFT


@dataclass(frozen=True, slots=True)
class RunRecord(_FromRow):
    id: uuid.UUID
    name: str
    parent_run_id: uuid.UUID | None
    batch_id: uuid.UUID | None
    scenario_id: str | None
    deployment_id: str
    public_config: dict[str, Any]
    limits: dict[str, Any]
    buyer_policy: PolicyKind
    seller_policy: PolicyKind
    buyer_model_id: str | None
    seller_model_id: str | None
    buyer_effort: str | None
    seller_effort: str | None
    policy_versions: dict[str, Any]
    prompt_template_versions: dict[str, Any]
    software_version: str
    state: RunState
    state_cause: str | None
    mode: RunMode
    outcome_kind: OutcomeKind
    outcome_reason_code: int | None
    outcome_actor: PartyOrOperator | None
    outcome_tx_hash: Digest | None
    session_id: SessionId | None
    config_hash: Digest | None
    session_expires_at_ts: int | None
    started_at: datetime | None
    terminal_at: datetime | None
    termination_cause: str | None
    termination_code: int | None
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class Outcome:
    """An economic outcome, as derived from a canonical terminal event (data model section 5)."""

    kind: OutcomeKind
    actor: PartyOrOperator
    tx_hash: Digest
    reason_code: int | None = None


def mandate_document(
    reservation_price_minor: MinorAmount,
    min_remaining_inventory_minor: MinorAmount,
    instructions: str,
) -> dict[str, Any]:
    """A mandate shaped as `mandate.v1.json`: what an agent receives and `mandate_hash` covers.

    `extra` is omitted rather than written as `{}`: the schema makes it optional, the column
    constrains it to be empty, and a hash that changed with an empty object's presence would be a
    hash of formatting rather than of the mandate.
    """
    return {
        "reservation_price_minor": reservation_price_minor.to_json(),
        "min_remaining_inventory_minor": min_remaining_inventory_minor.to_json(),
        "instructions": instructions,
    }


@dataclass(frozen=True, slots=True)
class NewMandate:
    """One party's private limits. `extra` is empty by schema and by constraint."""

    run_id: uuid.UUID
    party: Party
    version: int
    reservation_price_minor: MinorAmount
    min_remaining_inventory_minor: MinorAmount
    instructions: str

    def as_document(self) -> dict[str, Any]:
        """The mandate as `mandate.v1.json` shapes it, which is what `mandate_hash` covers."""
        return mandate_document(
            self.reservation_price_minor, self.min_remaining_inventory_minor, self.instructions
        )


@dataclass(frozen=True, slots=True)
class MandateVersionRecord(_FromRow):
    """Section 3.4. **Private.**"""

    id: uuid.UUID
    run_id: uuid.UUID
    party: Party
    version: int
    reservation_price_minor: MinorAmount
    min_remaining_inventory_minor: MinorAmount
    instructions: str
    extra: dict[str, Any]
    mandate_hash: str

    def as_document(self) -> dict[str, Any]:
        return mandate_document(
            self.reservation_price_minor, self.min_remaining_inventory_minor, self.instructions
        )


@dataclass(frozen=True, slots=True)
class NewWallet:
    """A run's participant wallet. The address was derived by the agent service (ADR-039)."""

    run_id: uuid.UUID
    party: Party
    address: Address
    key_ref: str
    key_derivation: dict[str, Any]
    initial_base_minor: MinorAmount
    initial_quote_minor: MinorAmount
    allowance_minor: MinorAmount


@dataclass(frozen=True, slots=True)
class WalletRecord(_FromRow):
    id: uuid.UUID
    run_id: uuid.UUID
    party: Party
    address: Address
    key_ref: str
    key_derivation: dict[str, Any]
    initial_base_minor: MinorAmount
    initial_quote_minor: MinorAmount
    allowance_minor: MinorAmount
    setup_nonce_next: int
    funded_tx_hashes: dict[str, Any]


@dataclass(frozen=True, slots=True)
class LeaseRecord(_FromRow):
    run_id: uuid.UUID
    holder: str
    expires_at: datetime
    relay_nonce_next: int


# ---------------------------------------------------------------------------------------------
# Turns, decisions, signed actions
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NewTurn:
    run_id: uuid.UUID
    turn: int
    party: Party
    expected_sequence: int
    state: TurnState
    observation: dict[str, Any]
    observation_hash: str


@dataclass(frozen=True, slots=True)
class TurnRecord(_FromRow):
    id: uuid.UUID
    run_id: uuid.UUID
    turn: int
    party: Party
    expected_sequence: int
    state: TurnState
    observation: dict[str, Any]
    observation_hash: str
    started_at: datetime
    finished_at: datetime | None
    failure_code: str | None
    failure_detail: str | None


@dataclass(frozen=True, slots=True)
class NewDecision:
    """One decision attempt as the agent service reported it (docs/api_contract.md section 6)."""

    turn_id: uuid.UUID
    run_id: uuid.UUID
    party: Party
    attempt: int
    policy: PolicyKind
    raw_response: Any
    validation_ok: bool
    requested_at: datetime
    model_id: str | None = None
    effort: str | None = None
    prompt_template_version: str | None = None
    request_hash: str | None = None
    stop_reason: str | None = None
    validation_code: str | None = None
    validation_feedback: str | None = None
    usage: dict[str, Any] | None = None
    cost_estimated_usd: Decimal | None = None
    cost_reported_usd: Decimal | None = None
    latency_ms: int | None = None
    authorized: bool = False


@dataclass(frozen=True, slots=True)
class DecisionRecord(_FromRow):
    """Section 3.8. **Private.**"""

    id: uuid.UUID
    turn_id: uuid.UUID
    run_id: uuid.UUID
    party: Party
    attempt: int
    policy: PolicyKind
    model_id: str | None
    effort: str | None
    prompt_template_version: str | None
    request_hash: str | None
    raw_response: Any
    stop_reason: str | None
    validation_ok: bool
    validation_code: str | None
    validation_feedback: str | None
    usage: dict[str, Any] | None
    cost_estimated_usd: Decimal | None
    cost_reported_usd: Decimal | None
    latency_ms: int | None
    requested_at: datetime
    authorized: bool


@dataclass(frozen=True, slots=True)
class NewSignedAction:
    run_id: uuid.UUID
    turn_id: uuid.UUID
    decision_id: uuid.UUID
    sequence: int
    kind: ActionKind
    typed_message: dict[str, Any]
    digest: Digest
    signer: Address
    signature: str
    status: ActionStatus = ActionStatus.SIGNED


@dataclass(frozen=True, slots=True)
class SignedActionRecord(_FromRow):
    id: uuid.UUID
    run_id: uuid.UUID
    turn_id: uuid.UUID
    decision_id: uuid.UUID
    sequence: int
    kind: ActionKind
    typed_message: dict[str, Any]
    digest: Digest
    signer: Address
    signature: str
    status: ActionStatus
    revert_error: str | None


# ---------------------------------------------------------------------------------------------
# The outbox and the chain
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class NewOutboxTx:
    """A signed raw transaction, persisted before it is broadcast (spec section 9.2 step 5)."""

    run_id: uuid.UUID
    kind: TxKind
    sender: Address
    nonce: int
    raw_tx: bytes
    tx_hash: Digest
    signed_action_id: uuid.UUID | None = None
    replaces_id: uuid.UUID | None = None
    status: TxStatus = TxStatus.PENDING


@dataclass(frozen=True, slots=True)
class OutboxRecord(_FromRow):
    id: uuid.UUID
    run_id: uuid.UUID
    signed_action_id: uuid.UUID | None
    kind: TxKind
    sender: Address
    nonce: int
    raw_tx: bytes
    tx_hash: Digest
    replaces_id: uuid.UUID | None
    status: TxStatus
    attempts: int
    last_error: str | None
    block_number: int | None
    block_hash: Digest | None
    gas_used: MinorAmount | None
    effective_gas_price_wei: MinorAmount | None
    submitted_at: datetime | None
    included_at: datetime | None
    submitted_block: int | None
    sentence: str | None


@dataclass(frozen=True, slots=True)
class Inclusion:
    """Where a receipt put a transaction."""

    block_number: int
    block_hash: Digest
    gas_used: MinorAmount
    effective_gas_price_wei: MinorAmount
    included_at: datetime


@dataclass(frozen=True, slots=True)
class NewChainEvent:
    chain_id: int
    contract_address: Address
    block_number: int
    block_hash: Digest
    tx_hash: Digest
    log_index: int
    event_name: str
    decoded: dict[str, Any]
    run_id: uuid.UUID | None = None
    session_id: SessionId | None = None
    calldata: dict[str, Any] | None = None
    confirmations_at_index: int | None = None
    sentence: str | None = None


@dataclass(frozen=True, slots=True)
class ChainEventRecord(_FromRow):
    id: uuid.UUID
    run_id: uuid.UUID | None
    chain_id: int
    contract_address: Address
    session_id: SessionId | None
    block_number: int
    block_hash: Digest
    tx_hash: Digest
    log_index: int
    event_name: str
    decoded: dict[str, Any]
    calldata: dict[str, Any] | None
    canonical: bool
    invalidated_at: datetime | None
    confirmations_at_index: int | None
    sentence: str | None
    created_at: datetime


@dataclass(frozen=True, slots=True)
class NewBalanceSnapshot:
    run_id: uuid.UUID
    stage: SnapshotStage
    party: Party
    token: TokenRole
    amount_minor: MinorAmount
    block_number: int
    block_hash: Digest


@dataclass(frozen=True, slots=True)
class BalanceSnapshotRecord(_FromRow):
    id: uuid.UUID
    run_id: uuid.UUID
    stage: SnapshotStage
    party: Party
    token: TokenRole
    amount_minor: MinorAmount
    block_number: int
    block_hash: Digest
    canonical: bool


# ---------------------------------------------------------------------------------------------
# Derived and operational records
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RunMetricsRecord(_FromRow):
    """Section 3.13. The utility and feasibility fields are **private**."""

    run_id: uuid.UUID
    computed_at: datetime
    recorded_offers: int = 0
    decision_time_ms: int = 0
    chain_wait_ms: int = 0
    setup_chain_wait_ms: int = 0
    model_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    model_cost_estimated_usd: Decimal | None = None
    model_cost_reported_usd: Decimal | None = None
    gas_used_negotiation: MinorAmount = field(default_factory=lambda: MinorAmount(0))
    gas_used_setup: MinorAmount = field(default_factory=lambda: MinorAmount(0))
    fee_wei_negotiation: MinorAmount = field(default_factory=lambda: MinorAmount(0))
    fee_wei_setup: MinorAmount = field(default_factory=lambda: MinorAmount(0))
    settled_quote_minor: MinorAmount | None = None
    buyer_utility_minor: int | None = None
    seller_utility_minor: int | None = None
    captured_surplus_minor: int | None = None
    feasible: bool | None = None
    feasible_surplus_minor: int | None = None
    mandate_violations: int = 0
    failure_class: str | None = None
    audit_complete: bool = False
    #: ADR-061. The counts are accumulated by the controller as the run is driven; the metrics
    #: calculator carries them through a recomputation and prices them.
    rpc_requests: int = 0
    rpc_requests_by_method: dict[str, Any] = field(default_factory=dict)
    rpc_cost_estimated_usd: Decimal | None = None


@dataclass(frozen=True, slots=True)
class RunEventRecord(_FromRow):
    """Section 3.14. `data` is already filtered to public content when it is appended."""

    run_id: uuid.UUID
    cursor: int
    event_type: str
    data: dict[str, Any]
    created_at: datetime


@dataclass(frozen=True, slots=True)
class NewOperation:
    kind: str
    route: str
    request_hash: str
    run_id: uuid.UUID | None = None
    batch_id: uuid.UUID | None = None
    idempotency_key: str | None = None
    status: OperationStatus = OperationStatus.PENDING


@dataclass(frozen=True, slots=True)
class OperationRecord(_FromRow):
    id: uuid.UUID
    kind: str
    run_id: uuid.UUID | None
    batch_id: uuid.UUID | None
    idempotency_key: str | None
    route: str
    request_hash: str
    status: OperationStatus
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime
