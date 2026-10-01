"""`DeterministicPolicy`: the linear-concession baseline (docs/protocol.md section 13, spec 11.1).

It sees what a model policy sees — the observation and its own mandate — and nothing else. It
accepts an incoming offer the validator would let it accept; otherwise it makes the next offer on
its concession schedule; otherwise it walks away. "Would let it" is asked of the validator itself,
so the baseline and the gate that signs agree by construction on what is legal, and the baseline
never needs a repair.

This is the only place a price is clamped, and it clamps its own output to its own bound, as
section 13 requires. A model's price is never clamped anywhere (CLAUDE.md).

The walk-away reason follows section 13, amended by ADR-043: when the move the policy would
otherwise make — accepting the active offer, or making its scheduled offer — is refused for its
balance or its inventory floor, it leaves with `inventory_constraint`, so a mandate that cannot
trade is distinguishable from a price impasse in the evaluation.
"""

from __future__ import annotations

from typing import Final

from agent.observation import Observation, Role
from agent.policy.base import PolicyKind, PolicyResponse, Repair
from agent.validation import HOLDINGS_CODES, MandateValidator
from negotiation_protocol import UINT256_MAX, CloseReason, MinorAmount

VERSION: Final = "det-1.0.0"


def opportunities(role: Role, max_offers: int) -> int:
    """Own offer opportunities: buyer `ceil(maxOffers / 2)`, seller `floor(maxOffers / 2)`."""
    return (max_offers + 1) // 2 if role == "buyer" else max_offers // 2


def concession_price(role: Role, bound: MinorAmount, k: int, n: int) -> int:
    """The price of own offer `k` (0-based) of `n`, in whole minor units.

    `anchor + (bound - anchor) * k / (n - 1)` with the buyer's anchor at 0.8 R and the seller's at
    1.2 R, computed exactly in integers: the buyer's is `R (4(n-1) + k) / 5(n-1)` rounded down and
    the seller's `R (6(n-1) - k) / 5(n-1)` rounded up. With `n == 1`, the bound itself. The result
    is clamped to the policy's own bound, kept at least 1, and kept within `uint256`.
    """
    if n < 1:
        raise ValueError(f"no price exists with {n} opportunities")
    r = int(bound)
    span = 5 * (n - 1)
    if n == 1:
        price = r
    elif role == "buyer":
        price = min(r * (4 * (n - 1) + k) // span, r)
    else:
        price = max(-(-r * (6 * (n - 1) - k) // span), r)
    return min(max(price, 1), UINT256_MAX)


class DeterministicPolicy:
    """The baseline. Stateless: the same observation always gives the same decision."""

    def __init__(self, validator: MandateValidator) -> None:
        self._validator = validator

    @property
    def kind(self) -> PolicyKind:
        return "deterministic"

    @property
    def version(self) -> str:
        return VERSION

    @property
    def prompt_template_version(self) -> str | None:
        return None

    async def decide(self, observation: Observation, repair: Repair | None) -> PolicyResponse:
        # `repair` is ignored: a deterministic policy asked again would decide the same thing, and
        # it consults the validator before deciding, so it is never refused in the first place.
        return PolicyResponse.computed({"decision": self._choose(observation)})

    def _choose(self, observation: Observation) -> dict[str, str]:
        held_back = False
        active = observation.active_offer
        if active is not None and active.proposer != observation.role:
            acceptance = {"action": "accept", "offer_hash": str(active.offer_hash)}
            verdict = self._validator.validate({"decision": acceptance}, observation)
            if verdict.ok:
                return acceptance
            held_back = verdict.code in HOLDINGS_CODES
        if observation.offers_remaining_for_me > 0:
            offer = {"action": "offer", "quote_amount_minor": str(self._next_price(observation))}
            verdict = self._validator.validate({"decision": offer}, observation)
            if verdict.ok:
                return offer
            held_back = held_back or verdict.code in HOLDINGS_CODES
        return {"action": "walk_away", "reason": self._reason(observation, held_back)}

    def _next_price(self, observation: Observation) -> int:
        role = observation.role
        n = opportunities(role, observation.session.max_offers)
        k = observation.own_offers_recorded()
        return concession_price(role, observation.mandate.reservation_price, k, n)

    def _reason(self, observation: Observation, held_back: bool) -> CloseReason:
        if held_back:
            return "inventory_constraint"
        if observation.offers_remaining_for_me == 0 and _incoming_outside_bound(observation):
            return "terms_unacceptable"
        return "no_further_concession"


def _incoming_outside_bound(observation: Observation) -> bool:
    """An offer from the counterparty stands, at a price this party's mandate refuses."""
    active = observation.active_offer
    if active is None or active.proposer == observation.role:
        return False
    bound = observation.mandate.reservation_price
    if observation.role == "buyer":
        return active.quote_amount > bound
    return active.quote_amount < bound
