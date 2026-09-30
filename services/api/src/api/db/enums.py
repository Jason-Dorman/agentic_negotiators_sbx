"""The enumerations of docs/data_model.md section 4, as Python `StrEnum`s.

Each is also a PostgreSQL enum type with exactly these values, in this order, and the values are the
API strings verbatim (data model principle 6), so a value crosses the database, the API and the
export without a mapping table. Enum values are only ever added, with `ALTER TYPE ... ADD VALUE`,
never removed (data model section 8); `test_schema_matches_data_model.py` compares both the order
and the membership of every type against the document.

These live in `api.db`, the lowest backend layer but one, because every layer above needs them and
`.importlinter` lets any module import `api.db` but not the reverse.
"""

from __future__ import annotations

from enum import StrEnum


class Party(StrEnum):
    BUYER = "buyer"
    SELLER = "seller"


class PartyOrOperator(StrEnum):
    """Who a terminal event is attributed to. `anyone` is the public caller of `expireSession`."""

    BUYER = "buyer"
    SELLER = "seller"
    OPERATOR = "operator"
    ANYONE = "anyone"


class PolicyKind(StrEnum):
    DETERMINISTIC = "deterministic"
    MODEL = "model"


class RunMode(StrEnum):
    """`fixture` marks a run whose decisions did not come from a live model (test_strategy 1.4)."""

    LIVE = "live"
    FIXTURE = "fixture"


class RunState(StrEnum):
    """The run's *operational* state (architecture section 6.1). Never an economic outcome."""

    DRAFT = "draft"
    VALIDATED = "validated"
    PREPARING = "preparing"
    RUNNING = "running"
    PAUSED = "paused"
    RECOVERY_REQUIRED = "recovery_required"
    TERMINAL = "terminal"
    FAILED_SETUP = "failed_setup"


class OutcomeKind(StrEnum):
    """The run's *economic* outcome, set only from a canonical terminal event (data model 5)."""

    PENDING = "pending"
    SETTLED = "settled"
    CLOSED = "closed"
    EXPIRED = "expired"
    ABORTED = "aborted"


class TurnState(StrEnum):
    """Architecture section 6.2."""

    OBSERVING = "observing"
    DECIDING = "deciding"
    REPAIRING = "repairing"
    SIGNING = "signing"
    BROADCASTING = "broadcasting"
    CONFIRMING = "confirming"
    CONFIRMED = "confirmed"
    MODEL_FAILED = "model_failed"
    EXECUTION_FAILED = "execution_failed"


class ActionKind(StrEnum):
    """The three participant-signed typed messages (docs/protocol.md section 4)."""

    OFFER = "offer"
    ACCEPT = "accept"
    CLOSE = "close"


class ActionStatus(StrEnum):
    SIGNED = "signed"
    SUBMITTED = "submitted"
    INCLUDED = "included"
    CONFIRMED = "confirmed"
    FINALIZED = "finalized"
    REVERTED = "reverted"
    SUPERSEDED = "superseded"


class TxKind(StrEnum):
    RECORD_OFFER = "record_offer"
    ACCEPT_AND_SETTLE = "accept_and_settle"
    CLOSE_SESSION = "close_session"
    EXPIRE_SESSION = "expire_session"
    ABORT_SESSION = "abort_session"
    CREATE_SESSION = "create_session"
    MINT = "mint"
    APPROVE = "approve"
    FUND_ETH = "fund_eth"


class TxStatus(StrEnum):
    """Spec section 9.3. `confirmed` is threshold depth; it is never labelled finality."""

    PENDING = "pending"
    SUBMITTED = "submitted"
    INCLUDED = "included"
    CONFIRMED = "confirmed"
    FINALIZED = "finalized"
    REVERTED = "reverted"
    REPLACED = "replaced"
    DROPPED = "dropped"


class SnapshotStage(StrEnum):
    PRE_SETUP = "pre_setup"
    POST_SETUP = "post_setup"
    PRE_SETTLEMENT = "pre_settlement"
    POST_SETTLEMENT = "post_settlement"
    TERMINAL = "terminal"


class TokenRole(StrEnum):
    BASE = "base"
    QUOTE = "quote"
    ETH = "eth"


class OperationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


#: A transaction in one of these states can no longer be the live broadcast of its signed action.
#: The partial unique index on `tx_outbox (signed_action_id)` excludes exactly these, so at most one
#: transaction per signed action is in any other state (data model section 3.10).
TX_STATUSES_NOT_LIVE = (TxStatus.REPLACED, TxStatus.DROPPED, TxStatus.REVERTED)
