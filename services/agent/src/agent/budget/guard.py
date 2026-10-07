"""`BudgetGuard`: one run's model-call ceiling and spend ceiling, checked before every call (FR-A9).

The guard is asked before a call is sent and told afterwards what it used. Before: the call's input
token count — `count_tokens` on the exact request — and its `max_tokens` are priced from the table
into the call's *estimate*, a bound it cannot exceed. The call is refused when it would be one call
past the ceiling, when its model has no price and the operator has not allowed unknown prices, or
when the spend so far plus the estimate would cross the spend ceiling. After: the provider's usage
priced from the same table is the call's *reported* cost (ADR-085).

The spend so far is the reported cost of every call that reported usage, and the *estimate* of
every admitted call that did not — a timeout, say, which the provider may still have billed. That
keeps the ceiling conservative without charging every call its worst case: with `max_tokens` at
16,000, estimates alone would exhaust a $2.00 ceiling in a handful of calls that really cost cents.

With unknown prices allowed, there is no estimate and no spend to check: only the call ceiling
holds, and both costs are recorded as unknown, never zero.

The guard holds no lock. A run has one turn in flight at a time, and a turn's attempts are
sequential (ADR-012), so its calls never race one another.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from agent.budget.prices import ModelPrice, TokenUsage


class RefusalReason(StrEnum):
    CALL_CEILING = "call_ceiling"
    UNKNOWN_PRICE = "unknown_price"
    SPEND_CEILING = "spend_ceiling"


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """The run's `limits.model_call_ceiling` and `limits.model_spend_ceiling_usd`."""

    call_ceiling: int
    spend_ceiling_usd: Decimal

    def __post_init__(self) -> None:
        if self.call_ceiling < 0 or self.spend_ceiling_usd < 0:
            raise ValueError("a budget ceiling cannot be negative")


@dataclass(frozen=True, slots=True)
class Admission:
    """A call the guard let through, and the bound it was admitted at; None when unpriced."""

    estimated_usd: Decimal | None


@dataclass(frozen=True, slots=True)
class BudgetRefusal:
    """A call the guard refused. `detail` holds counts and amounts of the budget, nothing else.

    `estimated_usd` is the bound the call would have had, for the operator; no call was made, so
    it is not a cost.
    """

    reason: RefusalReason
    detail: str
    estimated_usd: Decimal | None = None


class BudgetGuard:
    def __init__(
        self, limits: BudgetLimits, price: ModelPrice | None, *, allow_unknown_price: bool
    ) -> None:
        self._limits = limits
        self._price = price
        self._allow_unknown_price = allow_unknown_price
        self._calls = 0
        self._spent = Decimal(0)

    @property
    def calls(self) -> int:
        """Calls admitted so far, whatever became of them."""
        return self._calls

    @property
    def spent_usd(self) -> Decimal | None:
        """Reported cost where usage came back, estimates where it did not; None when unpriced."""
        return None if self._price is None else self._spent

    def admit(self, input_tokens: int, max_tokens: int) -> Admission | BudgetRefusal:
        """Admit one call, counting it, or refuse it and count nothing."""
        if input_tokens < 0 or max_tokens < 1:
            raise ValueError("a call needs a non-negative input count and a positive max_tokens")
        ceiling = self._limits.call_ceiling
        if self._calls + 1 > ceiling:
            return BudgetRefusal(
                RefusalReason.CALL_CEILING, f"{self._calls} of {ceiling} model calls made"
            )
        if self._price is None:
            if not self._allow_unknown_price:
                return BudgetRefusal(
                    RefusalReason.UNKNOWN_PRICE,
                    "the model price table does not price this model, and unknown prices are "
                    "not allowed",
                )
            self._calls += 1
            return Admission(None)
        estimate = self._price.bound_usd(input_tokens, max_tokens)
        if self._spent + estimate > self._limits.spend_ceiling_usd:
            return BudgetRefusal(
                RefusalReason.SPEND_CEILING,
                f"USD {self._spent} spent and USD {estimate} at most for this call would cross "
                f"the USD {self._limits.spend_ceiling_usd} ceiling",
                estimate,
            )
        self._calls += 1
        return Admission(estimate)

    def settle(self, admission: Admission, usage: TokenUsage | None) -> Decimal | None:
        """Record what an admitted call used and return its reported cost.

        None — unknown — when the provider reported no usage or the model has no price. A call
        with no usage is charged its estimate against the ceiling: it may still have been billed.
        """
        if self._price is None:
            return None
        if usage is None:
            self._spent += admission.estimated_usd or Decimal(0)
            return None
        reported = self._price.cost_usd(usage)
        self._spent += reported
        return reported
