"""`ModelResult`: how one model call ended, in one of a closed set of outcomes.

Every way a call can end is its own outcome, so that a refusal, a truncation, a timeout and a
provider fault never look alike in a decision record or a failure (ADR-007, docs/architecture.md
goal 4). Nothing here decides what an outcome means for the turn: `ModelPolicy` (stage 3.2) maps
them to refused attempts and to the turn response's `failure.code`.

| Outcome | The call |
|---|---|
| `decided` | answered with text that is a decision envelope |
| `unparseable` | answered, but its text is missing or not a decision envelope |
| `refusal` | was declined by the provider's safeguards, `stop_reason: "refusal"` |
| `max_tokens` | was cut off at `max_tokens`, its text most likely incomplete |
| `timeout` | was not answered within the run's `model_timeout_s` |
| `rate_limited` | was refused with HTTP 429 |
| `rejected` | was refused with another 4xx, or redirected (never followed): misconfigured |
| `provider_failure` | failed at the provider (5xx, 529), or answered with a body that is not one |
| `connection_failed` | lost its connection, before the provider or before its answer was read |
| `budget_refused` | was not sent: `BudgetGuard` refused it |

The model's text and the provider's error message are private evidence for the decision record.
They are never logged, and `ModelResult` has no `repr` that shows them.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Any

from agent.budget import BudgetRefusal, TokenUsage


class ModelOutcome(StrEnum):
    DECIDED = "decided"
    UNPARSEABLE = "unparseable"
    REFUSAL = "refusal"
    MAX_TOKENS = "max_tokens"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    REJECTED = "rejected"
    PROVIDER_FAILURE = "provider_failure"
    CONNECTION_FAILED = "connection_failed"
    BUDGET_REFUSED = "budget_refused"


@dataclass(frozen=True, slots=True)
class ProviderError:
    """An HTTP failure: its status (None when there was no response), the provider's error type,
    and its message — private, like the model's own text."""

    status: int | None
    error_type: str | None
    message: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class ModelResult:
    outcome: ModelOutcome
    #: The model the request named. Fallbacks are off, so it is the model that decided (ADR-015).
    model_id: str
    #: The model the provider says answered; differs from `model_id` only if something substituted.
    served_model: str | None = None
    #: The decision envelope as the model wrote it, `decided` only.
    decision: Mapping[str, Any] | None = field(default=None, repr=False)
    #: Every text block the model wrote, joined; None when it wrote none or there was no answer.
    text: str | None = field(default=None, repr=False)
    stop_reason: str | None = None
    usage: TokenUsage | None = None
    cost_estimated_usd: Decimal | None = None
    cost_reported_usd: Decimal | None = None
    request_id: str | None = None
    latency_ms: int | None = None
    provider_error: ProviderError | None = None
    budget_refusal: BudgetRefusal | None = None
