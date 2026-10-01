"""Fee and gas arithmetic for the relay, in integers, with the ceilings of ADR-050 and ADR-054.

Pure functions, so every boundary is a unit test: the first fee caps, the 12.5 percent bump, the
point at which the ceiling stops replacement, and the gas margin.
"""

from __future__ import annotations

from dataclasses import dataclass

from api.chain import FeeQuote
from api.config import FEE_BUMP_DENOMINATOR, FEE_BUMP_NUMERATOR, RelayPolicy

#: A node replaces a pooled transaction only for a fee at least this much higher: 10 percent.
NODE_MINIMUM_BUMP_NUMERATOR = 11
NODE_MINIMUM_BUMP_DENOMINATOR = 10


@dataclass(frozen=True, slots=True)
class Fees:
    max_fee_per_gas: int
    max_priority_fee_per_gas: int


def _ceil_ratio(value: int, numerator: int, denominator: int) -> int:
    return -(-value * numerator // denominator)


def initial_fees(quote: FeeQuote, policy: RelayPolicy) -> Fees:
    """Twice the base fee plus the suggested tip, the usual EIP-1559 headroom, within the ceiling.

    Twice the base fee absorbs six full blocks of 12.5 percent base-fee growth before the
    transaction stops being includable, which is what makes replacement the exception.
    """
    ceiling = policy.max_fee_per_gas_wei
    priority = min(quote.max_priority_fee_per_gas, ceiling)
    max_fee = min(2 * quote.base_fee_per_gas + priority, ceiling)
    return Fees(max_fee_per_gas=max_fee, max_priority_fee_per_gas=min(priority, max_fee))


def bumped_fees(current: Fees, policy: RelayPolicy) -> Fees | None:
    """Both caps raised by an eighth (ADR-050), or None once the ceiling forbids a valid bump.

    A bump clipped to the ceiling is used only while it still clears the node's 10 percent
    minimum; below that the node would refuse it, so the relay stops replacing and waits.
    """
    ceiling = policy.max_fee_per_gas_wei
    max_fee = min(
        _ceil_ratio(current.max_fee_per_gas, FEE_BUMP_NUMERATOR, FEE_BUMP_DENOMINATOR), ceiling
    )
    minimum = _ceil_ratio(
        current.max_fee_per_gas, NODE_MINIMUM_BUMP_NUMERATOR, NODE_MINIMUM_BUMP_DENOMINATOR
    )
    if max_fee < minimum:
        return None
    priority = _ceil_ratio(
        current.max_priority_fee_per_gas, FEE_BUMP_NUMERATOR, FEE_BUMP_DENOMINATOR
    )
    return Fees(max_fee_per_gas=max_fee, max_priority_fee_per_gas=min(priority, max_fee))


def gas_limit(estimate: int | None, policy: RelayPolicy) -> int:
    """The estimate plus a quarter, or the fallback when the node predicted a revert (ADR-054)."""
    if estimate is None:
        return policy.fallback_gas_limit
    return _ceil_ratio(estimate, policy.gas_margin_quarters, 4)
