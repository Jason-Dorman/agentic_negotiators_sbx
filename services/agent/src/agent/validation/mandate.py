"""`MandateValidator`: the complete trade, checked before anything is signed.

docs/protocol.md section 11 splits validation in two, and this is the second half. A decision that
parsed is checked against the protocol's turn and offer rules, the active offer, the agent's own
mandate and its own holdings. An offer is validated as fully as an acceptance, because an active
offer is authority for the counterparty to execute exactly that trade (ADR-004).

Everything here reads the observation, which the turn executor has already matched against the
session this instance approved. Nothing here reads a clock: chain time comes from the observation
(docs/protocol.md section 6).

The validator never alters a proposal. An amount one minor unit over the reservation is refused,
with feedback, and the policy decides again; it is not clamped (CLAUDE.md, ADR-006). Each refusal's
feedback names this agent's own reservation or holdings, which is safe precisely because it goes
back only to the agent that owns them.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent.observation import ActiveOffer, Observation
from agent.validation.codes import Code
from agent.validation.decisions import (
    ProposedAccept,
    ProposedOffer,
    ValidAccept,
    ValidDecision,
    ValidOffer,
    ValidWalkAway,
)
from agent.validation.structure import Refusal, parse_decision
from negotiation_protocol import MinorAmount


@dataclass(frozen=True, slots=True)
class Validation:
    """The outcome of validating one response: a decision to sign, or a refusal to report."""

    decision: ValidDecision | None
    code: Code | None
    feedback: str | None

    @property
    def ok(self) -> bool:
        return self.decision is not None

    @classmethod
    def accepted(cls, decision: ValidDecision) -> Validation:
        return cls(decision, None, None)

    @classmethod
    def refused(cls, refusal: Refusal) -> Validation:
        return cls(None, refusal.code, refusal.feedback)


class MandateValidator:
    """Structural then economic validation, producing private feedback (architecture 3.3)."""

    def validate(self, response: object, observation: Observation) -> Validation:
        outcome = parse_decision(response).outcome
        if isinstance(outcome, Refusal):
            return Validation.refused(outcome)
        if isinstance(outcome, ProposedOffer):
            return _validate_offer(outcome, observation)
        if isinstance(outcome, ProposedAccept):
            return _validate_accept(outcome, observation)
        # A walk-away cannot breach a mandate, and Close is legal whoever's turn it is
        # (docs/protocol.md section 5, rule 4). Its reason is the agent's statement, not a fact
        # this validator checks (section 10).
        return Validation.accepted(ValidWalkAway(outcome.reason, outcome.reason_code))


def _validate_offer(proposal: ProposedOffer, observation: Observation) -> Validation:
    amount = proposal.quote_amount
    refusal = (
        _refuse_out_of_turn(observation)
        or _refuse_past_offer_limit(observation)
        or _refuse_outside_reservation(observation, amount, "Your offer")
        or _refuse_unaffordable(observation, amount)
        or _refuse_below_inventory_floor(observation, amount)
    )
    if refusal is not None:
        return Validation.refused(refusal)
    return Validation.accepted(ValidOffer(amount))


def _validate_accept(proposal: ProposedAccept, observation: Observation) -> Validation:
    active = observation.active_offer
    if active is None:
        return Validation.refused(
            Refusal(Code.NO_ACTIVE_OFFER, "There is no active offer to accept.")
        )
    refusal = (
        _refuse_own_offer(observation, active)
        or _refuse_stale_hash(proposal, active)
        or _refuse_expired(observation, active)
        or _refuse_outside_reservation(observation, active.quote_amount, "The active offer")
        or _refuse_unaffordable(observation, active.quote_amount)
        or _refuse_below_inventory_floor(observation, active.quote_amount)
    )
    if refusal is not None:
        return Validation.refused(refusal)
    return Validation.accepted(ValidAccept(active.offer_hash))


# --------------------------------------------------------------------------------------
# Protocol legality (docs/protocol.md section 5)
# --------------------------------------------------------------------------------------


def _refuse_out_of_turn(observation: Observation) -> Refusal | None:
    next_proposer = observation.next_proposer()
    if next_proposer == observation.role:
        return None
    return Refusal(
        Code.NOT_YOUR_TURN,
        f"It is not your turn to offer: the next offer belongs to the {next_proposer}.",
    )


def _refuse_past_offer_limit(observation: Observation) -> Refusal | None:
    """Rule 7: the session's own limit, as well as this agent's share of it."""
    session_open = observation.offers_recorded() < observation.session.max_offers
    if observation.offers_remaining_for_me > 0 and session_open:
        return None
    return Refusal(
        Code.OFFER_LIMIT_REACHED,
        "You have no offers left in this session. You may accept the active offer or walk away.",
    )


def _refuse_own_offer(observation: Observation, active: ActiveOffer) -> Refusal | None:
    if active.proposer != observation.role:
        return None
    return Refusal(Code.SELF_ACCEPTANCE, "The active offer is your own; you cannot accept it.")


def _refuse_stale_hash(proposal: ProposedAccept, active: ActiveOffer) -> Refusal | None:
    if proposal.offer_hash == active.offer_hash:
        return None
    return Refusal(
        Code.STALE_OFFER_HASH,
        f'"offer_hash" is not the active offer. The active offer is {active.offer_hash}.',
    )


def _refuse_expired(observation: Observation, active: ActiveOffer) -> Refusal | None:
    """`acceptAndSettle` requires `block.timestamp < activeValidUntil` (section 6)."""
    if observation.chain_time < active.valid_until:
        return None
    return Refusal(
        Code.OFFER_EXPIRED,
        f"The active offer expired at chain time {active.valid_until} and the chain time is now "
        f"{observation.chain_time}; it can no longer be accepted.",
    )


# --------------------------------------------------------------------------------------
# The mandate and the holdings: the complete trade
# --------------------------------------------------------------------------------------


def _refuse_outside_reservation(
    observation: Observation, amount: MinorAmount, subject: str
) -> Refusal | None:
    bound = observation.mandate.reservation_price
    if observation.role == "buyer":
        if amount <= bound:
            return None
        return Refusal(
            Code.ABOVE_RESERVATION,
            f"{subject} of {amount} is above your reservation price of {bound}. "
            "As the buyer you may not pay more than that.",
        )
    if amount >= bound:
        return None
    return Refusal(
        Code.BELOW_RESERVATION,
        f"{subject} of {amount} is below your reservation price of {bound}. "
        "As the seller you may not accept less than that.",
    )


def _refuse_unaffordable(observation: Observation, amount: MinorAmount) -> Refusal | None:
    """This party's own leg of the settlement, from its current balance."""
    balances = observation.my_balances
    if observation.role == "buyer":
        if balances.quote >= amount:
            return None
        return Refusal(
            Code.INSUFFICIENT_BALANCE,
            f"This trade would pay {amount} of the quote token, but your quote balance is only "
            f"{balances.quote}.",
        )
    base_amount = observation.session.base_amount
    if balances.base >= base_amount:
        return None
    return Refusal(
        Code.INSUFFICIENT_BALANCE,
        f"This trade would deliver {base_amount} of the base token, but your base balance is only "
        f"{balances.base}.",
    )


def _refuse_below_inventory_floor(observation: Observation, amount: MinorAmount) -> Refusal | None:
    """The holding this party gives up, after settlement, against its own floor (ADR-045).

    "Inventory" is whatever the party trades away: the asset for the seller, which delivers the
    base amount, and capital for the buyer, which pays the quote amount. A buyer with 250 mUSD and a
    floor of 100 mUSD may pay at most 150, whatever its reservation price.
    """
    if observation.role == "buyer":
        token, after = "quote", observation.my_balances.quote - amount
    else:
        token, after = "base", observation.my_balances.base - observation.session.base_amount
    floor = observation.mandate.min_remaining_inventory
    if after >= floor:
        return None
    return Refusal(
        Code.INVENTORY_FLOOR,
        f"This trade would leave you {after} of the {token} token, below your minimum remaining "
        f"inventory of {floor}.",
    )
