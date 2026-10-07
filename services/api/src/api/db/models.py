"""SQLAlchemy models for every table in docs/data_model.md section 3.

The migration in `migrations/versions/` is the authority on the schema (data model header); these
models are the application's view of it, and `test_migrations.py` runs Alembic's autogenerate
comparison between the two and fails on any difference. `test_schema_matches_data_model.py` checks
the migrated database against a transcription of the document, so the three cannot drift apart
silently in any direction.

Nothing outside `api.db` imports this module (`.importlinter`, contract `sessions-stay-in-db`).
Repositories hand the rest of the backend frozen records from `records.py`, never these objects.

Conventions, each stated in the data model:

- Every table has `created_at` and `updated_at`, except `run_events`, which is append-only and has
  `created_at` alone. A table whose primary key the document names has no `id`; every other table
  has `id UUID DEFAULT gen_random_uuid()`.
- Amounts are `NUMERIC(78,0)` through `MinorAmountType`; addresses and digests go through the
  value-object column types.
- Constraints are named by the convention below so the migration, the models and the tests all
  refer to one name.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    MetaData,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

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
from api.db.types import (
    USD_NUMERIC,
    AddressType,
    DigestType,
    MinorAmountType,
    SessionIdType,
    SignedAmountType,
)
from negotiation_protocol import Address, Digest, MinorAmount, SessionId

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def pg_enum(enum_class: type[Any], name: str) -> Enum:
    """A native PostgreSQL enum whose values are the `StrEnum` *values*, in declaration order."""
    return Enum(
        enum_class,
        name=name,
        native_enum=True,
        create_constraint=False,
        values_callable=lambda members: [member.value for member in members],
        validate_strings=True,
    )


PARTY = pg_enum(Party, "party")
PARTY_OR_OPERATOR = pg_enum(PartyOrOperator, "party_or_operator")
POLICY_KIND = pg_enum(PolicyKind, "policy_kind")
RUN_MODE = pg_enum(RunMode, "run_mode")
RUN_STATE = pg_enum(RunState, "run_state")
OUTCOME_KIND = pg_enum(OutcomeKind, "outcome_kind")
TURN_STATE = pg_enum(TurnState, "turn_state")
ACTION_KIND = pg_enum(ActionKind, "action_kind")
ACTION_STATUS = pg_enum(ActionStatus, "action_status")
TX_KIND = pg_enum(TxKind, "tx_kind")
TX_STATUS = pg_enum(TxStatus, "tx_status")
SNAPSHOT_STAGE = pg_enum(SnapshotStage, "snapshot_stage")
TOKEN_ROLE = pg_enum(TokenRole, "token_role")
OPERATION_STATUS = pg_enum(OperationStatus, "operation_status")

#: Every enum type, in the order the migration creates them.
ALL_ENUMS = (
    PARTY,
    PARTY_OR_OPERATOR,
    POLICY_KIND,
    RUN_MODE,
    RUN_STATE,
    OUTCOME_KIND,
    TURN_STATE,
    ACTION_KIND,
    ACTION_STATUS,
    TX_KIND,
    TX_STATUS,
    SNAPSHOT_STAGE,
    TOKEN_ROLE,
    OPERATION_STATUS,
)

TIMESTAMP = DateTime(timezone=True)


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), primary_key=True, server_default=func.gen_random_uuid()
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(TIMESTAMP, nullable=False, server_default=func.now())


def _updated_at() -> Mapped[datetime]:
    return mapped_column(TIMESTAMP, nullable=False, server_default=func.now(), onupdate=func.now())


def _non_negative(*columns: str) -> list[CheckConstraint]:
    return [CheckConstraint(f"{column} >= 0", name=f"{column}_non_negative") for column in columns]


def _jsonb_default(literal: str) -> Any:
    return text(f"'{literal}'::jsonb")


# ---------------------------------------------------------------------------------------------
# Reference data: scenarios and deployments
# ---------------------------------------------------------------------------------------------


class Scenario(Base):
    """Section 3.1. `buyer_template` and `seller_template` hold mandates and are private."""

    __tablename__ = "scenarios"

    scenario_id: Mapped[str] = mapped_column(Text, primary_key=True)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    public_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    buyer_template: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    seller_template: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Deployment(Base):
    """Section 3.2. One row per deployment manifest."""

    __tablename__ = "deployments"
    __table_args__ = (
        # Migration 0004 (ADR-081): a chain is identified by its genesis block as well as its ID,
        # so a restarted Anvil, with the same addresses, is a deployment of its own.
        UniqueConstraint("chain_id", "genesis_hash", "exchange_address"),
        *_non_negative("start_block"),
    )

    deployment_id: Mapped[str] = mapped_column(Text, primary_key=True)
    chain_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    protocol_version: Mapped[str] = mapped_column(Text, nullable=False)
    exchange_address: Mapped[Address] = mapped_column(AddressType, nullable=False)
    base_token_address: Mapped[Address] = mapped_column(AddressType, nullable=False)
    quote_token_address: Mapped[Address] = mapped_column(AddressType, nullable=False)
    operator_address: Mapped[Address] = mapped_column(AddressType, nullable=False)
    relay_address: Mapped[Address] = mapped_column(AddressType, nullable=False)
    code_hashes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    compiler: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    explorer_base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    ens: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    manifest: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    start_block: Mapped[int] = mapped_column(BigInteger, nullable=False)
    deployed_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    genesis_hash: Mapped[Digest | None] = mapped_column(DigestType, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


# ---------------------------------------------------------------------------------------------
# Batches (stage 6 fills them; the tables exist because runs.batch_id refers to one)
# ---------------------------------------------------------------------------------------------


class Batch(Base):
    """Section 3.16. `report` is private: it is computed from mandates."""

    __tablename__ = "batches"
    __table_args__ = (*_non_negative("repetitions"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    population: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    pairings: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    repetitions: Mapped[int] = mapped_column(Integer, nullable=False)
    template_run: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    report: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class BatchScenario(Base):
    """Section 3.16. `feasible` and both mandates are private."""

    __tablename__ = "batch_scenarios"
    __table_args__ = (
        UniqueConstraint("batch_id", "index"),
        *_non_negative("index"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("batches.id"), nullable=False)
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    feasible: Mapped[bool] = mapped_column(Boolean, nullable=False)
    buyer_mandate: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    seller_mandate: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    public_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


# ---------------------------------------------------------------------------------------------
# Runs and their private inputs
# ---------------------------------------------------------------------------------------------

#: Data model section 3.3: an economic outcome exists only once a terminal event does. The state
#: may be `failed_setup` as well as `terminal`: a session opened during setup and then refused by
#: an agent is aborted, and that run's setup failed.
RUN_OUTCOME_REQUIRES_TERMINAL_EVENT = (
    "outcome_kind = 'pending' OR "
    "(state IN ('terminal', 'failed_setup') AND outcome_tx_hash IS NOT NULL)"
)

#: Data model section 5, as a constraint: which reason codes and actors each outcome admits.
#:
#: Every clause tests `IS NOT NULL` before comparing, and that is not redundant. A CHECK passes when
#: its expression is NULL, and `'closed' AND NULL BETWEEN 1 AND 3` is NULL, so without the guard a
#: closed outcome with no reason code — or a settlement with no actor — was accepted. The constraint
#: tests caught it; SQL's three-valued logic will not.
RUN_OUTCOME_REASON_MATCHES_KIND = (
    "(outcome_kind IN ('pending', 'settled', 'expired') AND outcome_reason_code IS NULL) OR "
    "(outcome_kind = 'closed' AND outcome_reason_code IS NOT NULL "
    "AND outcome_reason_code BETWEEN 1 AND 3) OR "
    "(outcome_kind = 'aborted' AND outcome_reason_code IS NOT NULL "
    "AND outcome_reason_code BETWEEN 1 AND 4)"
)
RUN_OUTCOME_ACTOR_MATCHES_KIND = (
    "(outcome_kind = 'pending' AND outcome_actor IS NULL) OR "
    "(outcome_kind IN ('settled', 'closed') AND outcome_actor IS NOT NULL "
    "AND outcome_actor IN ('buyer', 'seller')) OR "
    "(outcome_kind = 'expired' AND outcome_actor IS NOT NULL AND outcome_actor = 'anyone') OR "
    "(outcome_kind = 'aborted' AND outcome_actor IS NOT NULL AND outcome_actor = 'operator')"
)


class Run(Base):
    """Section 3.3. Every column is public; mandates live in `mandate_versions`."""

    __tablename__ = "runs"
    __table_args__ = (
        CheckConstraint(
            RUN_OUTCOME_REQUIRES_TERMINAL_EVENT, name="outcome_requires_terminal_event"
        ),
        CheckConstraint(RUN_OUTCOME_REASON_MATCHES_KIND, name="outcome_reason_matches_kind"),
        CheckConstraint(RUN_OUTCOME_ACTOR_MATCHES_KIND, name="outcome_actor_matches_kind"),
        CheckConstraint(
            "termination_code IS NULL "
            "OR (termination_cause IS NOT NULL AND termination_code BETWEEN 1 AND 4)",
            name="termination_code_valid",
        ),
        *_non_negative("session_expires_at_ts"),
        Index(None, "state"),
        Index(None, "batch_id"),
        Index("ix_runs_created_at_desc", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("batches.id"), nullable=True)
    scenario_id: Mapped[str | None] = mapped_column(
        ForeignKey("scenarios.scenario_id"), nullable=True
    )
    deployment_id: Mapped[str] = mapped_column(
        ForeignKey("deployments.deployment_id"), nullable=False
    )
    public_config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    limits: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    buyer_policy: Mapped[PolicyKind] = mapped_column(POLICY_KIND, nullable=False)
    seller_policy: Mapped[PolicyKind] = mapped_column(POLICY_KIND, nullable=False)
    buyer_model_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    seller_model_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    buyer_effort: Mapped[str | None] = mapped_column(Text, nullable=True)
    seller_effort: Mapped[str | None] = mapped_column(Text, nullable=True)
    policy_versions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_jsonb_default("{}")
    )
    prompt_template_versions: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_jsonb_default("{}")
    )
    software_version: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[RunState] = mapped_column(RUN_STATE, nullable=False)
    state_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    mode: Mapped[RunMode] = mapped_column(RUN_MODE, nullable=False, server_default="live")
    outcome_kind: Mapped[OutcomeKind] = mapped_column(
        OUTCOME_KIND, nullable=False, server_default="pending"
    )
    outcome_reason_code: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    outcome_actor: Mapped[PartyOrOperator | None] = mapped_column(PARTY_OR_OPERATOR, nullable=True)
    outcome_tx_hash: Mapped[Digest | None] = mapped_column(DigestType, nullable=True)
    session_id: Mapped[SessionId | None] = mapped_column(SessionIdType, nullable=True, unique=True)
    config_hash: Mapped[Digest | None] = mapped_column(DigestType, nullable=True)
    session_expires_at_ts: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    terminal_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    termination_cause: Mapped[str | None] = mapped_column(Text, nullable=True)
    termination_code: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class MandateVersion(Base):
    """Section 3.4. **Private.** Immutable after insert: a trigger refuses UPDATE and DELETE."""

    __tablename__ = "mandate_versions"
    __table_args__ = (
        UniqueConstraint("run_id", "party"),
        CheckConstraint("version >= 1", name="version_positive"),
        *_non_negative("reservation_price_minor", "min_remaining_inventory_minor"),
        # mandate.v1.json closes `extra` with additionalProperties: false, so it is empty by
        # definition until a field is added to the schema; the column says the same thing.
        CheckConstraint("extra = '{}'::jsonb", name="extra_is_empty"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), nullable=False)
    party: Mapped[Party] = mapped_column(PARTY, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    reservation_price_minor: Mapped[MinorAmount] = mapped_column(MinorAmountType, nullable=False)
    min_remaining_inventory_minor: Mapped[MinorAmount] = mapped_column(
        MinorAmountType, nullable=False
    )
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    extra: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_jsonb_default("{}")
    )
    mandate_hash: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Wallet(Base):
    """Section 3.5. The address is derived per run inside the agent service (ADR-039).

    `key_ref` names the agent instance's *root*, never a key, and the check below refuses anything
    that is not an `env:` or `keystore:` reference — a hex private key pasted here by mistake is a
    constraint violation rather than a stored secret. `address` is unique across the table: an
    address is never reused across runs, and this is what makes that a database error.
    """

    __tablename__ = "wallets"
    __table_args__ = (
        UniqueConstraint("run_id", "party"),
        CheckConstraint("key_ref ~ '^(env|keystore):.+'", name="key_ref_is_a_reference"),
        *_non_negative(
            "initial_base_minor", "initial_quote_minor", "allowance_minor", "setup_nonce_next"
        ),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), nullable=False)
    party: Mapped[Party] = mapped_column(PARTY, nullable=False)
    address: Mapped[Address] = mapped_column(AddressType, nullable=False, unique=True)
    key_ref: Mapped[str] = mapped_column(Text, nullable=False)
    key_derivation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    initial_base_minor: Mapped[MinorAmount] = mapped_column(MinorAmountType, nullable=False)
    initial_quote_minor: Mapped[MinorAmount] = mapped_column(MinorAmountType, nullable=False)
    allowance_minor: Mapped[MinorAmount] = mapped_column(MinorAmountType, nullable=False)
    setup_nonce_next: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    funded_tx_hashes: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_jsonb_default("{}")
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


# ---------------------------------------------------------------------------------------------
# One active run: the single-row table and the lease (section 3.6, ADR-019)
# ---------------------------------------------------------------------------------------------


class ActiveRun(Base):
    """At most one row, and `CHECK (id = 1)` is what makes that so."""

    __tablename__ = "active_run"
    __table_args__ = (CheckConstraint("id = 1", name="single_row"),)

    id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), nullable=False, unique=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class RunLease(Base):
    __tablename__ = "run_leases"
    __table_args__ = (*_non_negative("relay_nonce_next"),)

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    holder: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    relay_nonce_next: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


# ---------------------------------------------------------------------------------------------
# Turns, decisions and signed actions (sections 3.7 to 3.9)
# ---------------------------------------------------------------------------------------------


class Turn(Base):
    """Section 3.7. `observation` is **private**: it contains the acting party's own mandate."""

    __tablename__ = "turns"
    __table_args__ = (
        UniqueConstraint("run_id", "turn"),
        CheckConstraint("turn >= 1", name="turn_positive"),
        CheckConstraint("expected_sequence >= 1", name="expected_sequence_positive"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), nullable=False)
    turn: Mapped[int] = mapped_column(Integer, nullable=False)
    party: Mapped[Party] = mapped_column(PARTY, nullable=False)
    expected_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    state: Mapped[TurnState] = mapped_column(TURN_STATE, nullable=False)
    observation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    observation_hash: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        TIMESTAMP, nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Decision(Base):
    """Section 3.8. **Private.** `authorized` implies `validation_ok` (data model invariant 4)."""

    __tablename__ = "decisions"
    __table_args__ = (
        UniqueConstraint("turn_id", "attempt"),
        CheckConstraint("attempt >= 1", name="attempt_positive"),
        CheckConstraint("NOT authorized OR validation_ok", name="authorized_implies_valid"),
        *_non_negative("latency_ms"),
        Index(None, "run_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    turn_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("turns.id"), nullable=False)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), nullable=False)
    party: Mapped[Party] = mapped_column(PARTY, nullable=False)
    attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    policy: Mapped[PolicyKind] = mapped_column(POLICY_KIND, nullable=False)
    model_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    effort: Mapped[str | None] = mapped_column(Text, nullable=True)
    prompt_template_version: Mapped[str | None] = mapped_column(Text, nullable=True)
    request_hash: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_response: Mapped[Any] = mapped_column(JSONB, nullable=False)
    stop_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    validation_code: Mapped[str | None] = mapped_column(Text, nullable=True)
    validation_feedback: Mapped[str | None] = mapped_column(Text, nullable=True)
    usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    cost_estimated_usd: Mapped[Decimal | None] = mapped_column(USD_NUMERIC, nullable=True)
    cost_reported_usd: Mapped[Decimal | None] = mapped_column(USD_NUMERIC, nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    authorized: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Migration 0005 (ADR-090, Q71): a NUL in the response or its feedback was stored as `\u0000`.
    raw_response_escaped: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class SignedAction(Base):
    """Section 3.9. Logical action identity is the digest, not the transaction hash.

    `(run_id, sequence)` unique is what makes a duplicate action impossible to insert; `digest`
    unique makes the same signed message impossible to record twice under any run; `decision_id`
    and `turn_id` unique say a decision authorises at most one action and a turn produces at most
    one (data model invariant 4). A trigger refuses any change to the columns that identify the
    action — only `status` and `revert_error` move.
    """

    __tablename__ = "signed_actions"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence"),
        UniqueConstraint("digest"),
        UniqueConstraint("decision_id"),
        UniqueConstraint("turn_id"),
        CheckConstraint("sequence >= 1", name="sequence_positive"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), nullable=False)
    turn_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("turns.id"), nullable=False)
    decision_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("decisions.id"), nullable=False)
    sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    kind: Mapped[ActionKind] = mapped_column(ACTION_KIND, nullable=False)
    typed_message: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    digest: Mapped[Digest] = mapped_column(DigestType, nullable=False)
    signer: Mapped[Address] = mapped_column(AddressType, nullable=False)
    signature: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[ActionStatus] = mapped_column(ACTION_STATUS, nullable=False)
    revert_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


# ---------------------------------------------------------------------------------------------
# The outbox and what the chain recorded (sections 3.10 to 3.12)
# ---------------------------------------------------------------------------------------------

#: At most one live transaction per signed action (section 3.10). A gas replacement marks the
#: original `replaced` before its successor is inserted, so the two are never live together.
OUTBOX_LIVE_PER_SIGNED_ACTION = "status NOT IN ('replaced', 'dropped', 'reverted')"


class OutboxTx(Base):
    """Section 3.10. A transaction is persisted here, signed, before it is broadcast."""

    __tablename__ = "tx_outbox"
    __table_args__ = (
        UniqueConstraint("sender", "nonce", "tx_hash"),
        UniqueConstraint("tx_hash"),
        Index(
            "uq_tx_outbox_signed_action_id_live",
            "signed_action_id",
            unique=True,
            postgresql_where=text(OUTBOX_LIVE_PER_SIGNED_ACTION),
        ),
        *_non_negative("nonce", "attempts", "block_number", "submitted_block"),
        Index(None, "run_id"),
        Index(None, "status"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), nullable=False)
    signed_action_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("signed_actions.id"), nullable=True
    )
    kind: Mapped[TxKind] = mapped_column(TX_KIND, nullable=False)
    sender: Mapped[Address] = mapped_column(AddressType, nullable=False)
    nonce: Mapped[int] = mapped_column(BigInteger, nullable=False)
    raw_tx: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    tx_hash: Mapped[Digest] = mapped_column(DigestType, nullable=False)
    replaces_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tx_outbox.id"), nullable=True)
    status: Mapped[TxStatus] = mapped_column(TX_STATUS, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    block_number: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    block_hash: Mapped[Digest | None] = mapped_column(DigestType, nullable=True)
    gas_used: Mapped[MinorAmount | None] = mapped_column(MinorAmountType, nullable=True)
    effective_gas_price_wei: Mapped[MinorAmount | None] = mapped_column(
        MinorAmountType, nullable=True
    )
    submitted_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    included_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    # Migration 0002 (stage 2.3): the chain head at first broadcast, which the relay's replacement
    # trigger counts from (ADR-050), and an execution failure's timeline sentence (ADR-051).
    submitted_block: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    sentence: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class ChainEvent(Base):
    """Section 3.11. A decoded log, with the block it was seen in.

    The unique key includes `block_hash`, so the same log re-indexed after a reorg in a different
    block is a new row and the old one is marked non-canonical. `canonical` and `invalidated_at`
    move together — the check says a row is canonical exactly when it has not been invalidated. A
    trigger refuses any change to the columns that are the evidence: where and what the log was.
    """

    __tablename__ = "chain_events"
    __table_args__ = (
        UniqueConstraint("block_hash", "tx_hash", "log_index"),
        CheckConstraint(
            "canonical = (invalidated_at IS NULL)", name="canonical_iff_not_invalidated"
        ),
        *_non_negative("block_number", "log_index", "confirmations_at_index"),
        Index(None, "run_id"),
        Index(None, "session_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    chain_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    contract_address: Mapped[Address] = mapped_column(AddressType, nullable=False)
    session_id: Mapped[SessionId | None] = mapped_column(SessionIdType, nullable=True)
    block_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    block_hash: Mapped[Digest] = mapped_column(DigestType, nullable=False)
    tx_hash: Mapped[Digest] = mapped_column(DigestType, nullable=False)
    log_index: Mapped[int] = mapped_column(Integer, nullable=False)
    event_name: Mapped[str] = mapped_column(Text, nullable=False)
    decoded: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    calldata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    canonical: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    invalidated_at: Mapped[datetime | None] = mapped_column(TIMESTAMP, nullable=True)
    confirmations_at_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sentence: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class BalanceSnapshot(Base):
    """Section 3.12."""

    __tablename__ = "balance_snapshots"
    __table_args__ = (
        UniqueConstraint("run_id", "stage", "party", "token", "block_hash"),
        *_non_negative("amount_minor", "block_number"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), nullable=False)
    stage: Mapped[SnapshotStage] = mapped_column(SNAPSHOT_STAGE, nullable=False)
    party: Mapped[Party] = mapped_column(PARTY, nullable=False)
    token: Mapped[TokenRole] = mapped_column(TOKEN_ROLE, nullable=False)
    amount_minor: Mapped[MinorAmount] = mapped_column(MinorAmountType, nullable=False)
    block_number: Mapped[int] = mapped_column(BigInteger, nullable=False)
    block_hash: Mapped[Digest] = mapped_column(DigestType, nullable=False)
    canonical: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


# ---------------------------------------------------------------------------------------------
# Derived and operational records (sections 3.13 to 3.15)
# ---------------------------------------------------------------------------------------------

FAILURE_CLASSES = ("model", "signing", "rpc", "execution", "none")


class RunMetrics(Base):
    """Section 3.13. The utility and feasibility columns are **private**: they need mandates."""

    __tablename__ = "run_metrics"
    __table_args__ = (
        CheckConstraint(
            "failure_class IS NULL OR failure_class IN "
            + "("
            + ", ".join(f"'{value}'" for value in FAILURE_CLASSES)
            + ")",
            name="failure_class_known",
        ),
        *_non_negative(
            "recorded_offers",
            "decision_time_ms",
            "chain_wait_ms",
            "setup_chain_wait_ms",
            "model_calls",
            "input_tokens",
            "output_tokens",
            "mandate_violations",
            "rpc_requests",
        ),
    )

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    recorded_offers: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    decision_time_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    chain_wait_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    setup_chain_wait_ms: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    model_calls: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    model_cost_estimated_usd: Mapped[Decimal | None] = mapped_column(USD_NUMERIC, nullable=True)
    model_cost_reported_usd: Mapped[Decimal | None] = mapped_column(USD_NUMERIC, nullable=True)
    gas_used_negotiation: Mapped[MinorAmount] = mapped_column(
        MinorAmountType, nullable=False, server_default="0"
    )
    gas_used_setup: Mapped[MinorAmount] = mapped_column(
        MinorAmountType, nullable=False, server_default="0"
    )
    fee_wei_negotiation: Mapped[MinorAmount] = mapped_column(
        MinorAmountType, nullable=False, server_default="0"
    )
    fee_wei_setup: Mapped[MinorAmount] = mapped_column(
        MinorAmountType, nullable=False, server_default="0"
    )
    settled_quote_minor: Mapped[MinorAmount | None] = mapped_column(MinorAmountType, nullable=True)
    buyer_utility_minor: Mapped[int | None] = mapped_column(SignedAmountType, nullable=True)
    seller_utility_minor: Mapped[int | None] = mapped_column(SignedAmountType, nullable=True)
    captured_surplus_minor: Mapped[int | None] = mapped_column(SignedAmountType, nullable=True)
    feasible: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    feasible_surplus_minor: Mapped[int | None] = mapped_column(SignedAmountType, nullable=True)
    mandate_violations: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failure_class: Mapped[str | None] = mapped_column(Text, nullable=True)
    audit_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    computed_at: Mapped[datetime] = mapped_column(TIMESTAMP, nullable=False)
    # Migration 0004 (ADR-061): the chain adapter's request counts for the run, and their price.
    rpc_requests: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    rpc_requests_by_method: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=_jsonb_default("{}")
    )
    rpc_cost_estimated_usd: Mapped[Decimal | None] = mapped_column(USD_NUMERIC, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class RunEvent(Base):
    """Section 3.14. The SSE log: append-only, so it has `created_at` and no `updated_at`."""

    __tablename__ = "run_events"
    __table_args__ = (CheckConstraint("cursor >= 1", name="cursor_positive"),)

    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    cursor: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    event_type: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class Operation(Base):
    """Section 3.15. The idempotency record of every mutation route and every long operation."""

    __tablename__ = "operations"
    __table_args__ = (
        UniqueConstraint("route", "idempotency_key"),
        Index(None, "run_id"),
    )

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id"), nullable=True)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("batches.id"), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    route: Mapped[str] = mapped_column(Text, nullable=False)
    request_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[OperationStatus] = mapped_column(OPERATION_STATUS, nullable=False)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()
