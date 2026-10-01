"""`MandateValidator`: every refusal, its code and its feedback text (test_strategy section 6).

The feedback is asserted word for word because it is the one thing the validator says to a model:
on a repair it is the model's only account of what it did wrong, and it is private to that agent.
The accepted boundaries are asserted beside the refusals, so a validator that refused everything
could not pass this file.
"""

from __future__ import annotations

from typing import Any

import pytest
from agent_observations import (
    alternating_offers,
    digest_for,
    observation,
    typed,
)
from agent_validation_cases import (
    ALL,
    STRUCTURAL,
    Refusal,
    accept,
    buyer_facing,
    offer,
    seller_facing,
    walk_away,
)
from jsonschema import Draft202012Validator

from agent.validation import (
    MandateValidator,
    ValidAccept,
    ValidOffer,
    ValidWalkAway,
    parse_decision,
)
from negotiation_protocol import Digest, MinorAmount, load_schema

VALIDATOR = MandateValidator()


@pytest.mark.parametrize("case", ALL, ids=lambda case: case.name)
def test_each_refusal_has_its_code_and_its_feedback(case: Refusal) -> None:
    result = VALIDATOR.validate(case.response, typed(case.build()))
    assert not result.ok
    assert result.decision is None
    assert result.code == case.code
    assert result.feedback == case.feedback


@pytest.mark.parametrize("case", ALL, ids=lambda case: case.name)
def test_feedback_fits_the_observation_schema_limit(case: Refusal) -> None:
    """`previousDecision.feedback` is capped at 500 characters in observation.v1.json."""
    assert len(case.feedback) <= 500


# --------------------------------------------------------------------------------------
# What is accepted, at the boundaries the refusals sit next to
# --------------------------------------------------------------------------------------


def test_the_buyers_opening_offer_is_accepted_as_a_validated_amount() -> None:
    result = VALIDATOR.validate(offer("80000000"), typed(observation("buyer")))
    assert result.ok
    assert result.code is None and result.feedback is None
    assert result.decision == ValidOffer(MinorAmount(80_000_000))
    assert type(result.decision.quote_amount) is MinorAmount


def test_the_buyer_may_offer_exactly_its_reservation() -> None:
    assert VALIDATOR.validate(offer("100000000"), typed(observation("buyer"))).ok


def test_the_seller_may_offer_exactly_its_reservation() -> None:
    assert VALIDATOR.validate(offer("90000000"), typed(seller_facing(80_000_000))).ok


def test_the_buyer_may_offer_its_whole_quote_balance() -> None:
    document = observation("buyer", balances={"base_minor": "0", "quote_minor": "95000000"})
    assert VALIDATOR.validate(offer("95000000"), typed(document)).ok


def test_the_seller_may_trade_down_to_exactly_its_inventory_floor() -> None:
    document = seller_facing(80_000_000, balances={"base_minor": "20000000", "quote_minor": "0"})
    assert VALIDATOR.validate(offer("100000000"), typed(document)).ok


def test_the_seller_may_offer_its_whole_base_balance_with_no_floor() -> None:
    document = seller_facing(
        80_000_000,
        balances={"base_minor": "10000000", "quote_minor": "0"},
        mandate={
            "reservation_price_minor": "90000000",
            "min_remaining_inventory_minor": "0",
            "instructions": "",
        },
    )
    assert VALIDATOR.validate(offer("100000000"), typed(document)).ok


def test_a_counteroffer_is_accepted_while_an_offer_is_active() -> None:
    assert VALIDATOR.validate(offer("102000000"), typed(seller_facing(80_000_000))).ok


def test_an_offer_on_the_last_opportunity_is_accepted() -> None:
    document = buyer_facing(80_000_000, 108_000_000, offers_remaining=1)
    assert VALIDATOR.validate(offer("90000000"), typed(document)).ok


def test_the_seller_accepts_an_offer_within_its_bound() -> None:
    result = VALIDATOR.validate(accept(digest_for(1)), typed(seller_facing(90_000_000)))
    assert result.ok
    assert result.decision == ValidAccept(Digest(digest_for(1)))


def test_the_buyer_accepts_an_offer_at_exactly_its_reservation() -> None:
    document = buyer_facing(80_000_000, 100_000_000)
    assert VALIDATOR.validate(accept(digest_for(2)), typed(document)).ok


def test_accept_one_second_before_validuntil_is_accepted() -> None:
    document = seller_facing(95_000_000, chain_time=1_760_000_599)
    assert VALIDATOR.validate(accept(digest_for(1)), typed(document)).ok


def test_an_upper_case_offer_hash_names_the_same_digest() -> None:
    upper = "0x" + digest_for(1)[2:].upper()
    result = VALIDATOR.validate(accept(upper), typed(seller_facing(95_000_000)))
    assert result.decision == ValidAccept(Digest(digest_for(1)))


def test_acceptance_needs_no_offer_opportunity() -> None:
    """docs/protocol.md section 13: acceptance does not consume one."""
    document = buyer_facing(80_000_000, 95_000_000, offers_remaining=0)
    assert VALIDATOR.validate(accept(digest_for(2)), typed(document)).ok


@pytest.mark.parametrize(
    ("reason", "code"),
    [("terms_unacceptable", 1), ("inventory_constraint", 2), ("no_further_concession", 3)],
)
def test_each_walk_away_reason_maps_to_its_on_chain_code(reason: str, code: int) -> None:
    result = VALIDATOR.validate(walk_away(reason), typed(observation("buyer")))
    assert result.decision == ValidWalkAway(reason, code)  # type: ignore[arg-type]  # reason: the parametrised str is one of the three Literal values


def test_either_party_may_walk_away_out_of_turn() -> None:
    """docs/protocol.md section 5, rule 4: Close is allowed regardless of turn."""
    assert VALIDATOR.validate(walk_away("terms_unacceptable"), typed(observation("seller"))).ok


def test_a_walk_away_is_accepted_whatever_the_holdings() -> None:
    """Leaving can breach no mandate, so a party below its floor may still leave."""
    document = seller_facing(90_000_000, balances={"base_minor": "0", "quote_minor": "0"})
    assert VALIDATOR.validate(walk_away("inventory_constraint"), typed(document)).ok


def test_an_explanation_of_exactly_280_characters_is_accepted() -> None:
    response = {**offer("80000000"), "explanation": "x" * 280}
    assert VALIDATOR.validate(response, typed(observation("buyer"))).ok


def test_the_explanation_does_not_reach_the_validated_decision() -> None:
    """It is operator-facing only (ADR-013); nothing downstream of validation may carry it."""
    response = {**offer("80000000"), "explanation": "anchoring low"}
    result = VALIDATOR.validate(response, typed(observation("buyer")))
    assert result.decision == ValidOffer(MinorAmount(80_000_000))


# --------------------------------------------------------------------------------------
# The order of checks, where more than one would refuse
# --------------------------------------------------------------------------------------


def test_the_turn_is_checked_before_the_price() -> None:
    result = VALIDATOR.validate(offer("200000000"), typed(observation("seller")))
    assert result.code == "not_your_turn"


def test_extra_fields_are_reported_before_a_missing_one() -> None:
    result = VALIDATOR.validate(
        {"decision": {"action": "offer", "price": "1"}}, typed(observation("buyer"))
    )
    assert result.code == "extra_fields"


def test_a_stale_hash_is_reported_before_the_price_it_would_have_paid() -> None:
    document = observation("seller", history=alternating_offers(80_000_000, 108_000_000, 1))
    assert VALIDATOR.validate(accept(digest_for(1)), typed(document)).code == "stale_offer_hash"


# --------------------------------------------------------------------------------------
# Structural validation agrees with agent_decision.v1.json
# --------------------------------------------------------------------------------------

DECISION_SCHEMA = Draft202012Validator(load_schema("agent_decision.v1.json"))

VALID_RESPONSES: tuple[Any, ...] = (
    offer("1"),
    offer("80000000"),
    offer("9" * 77),
    accept(digest_for(1)),
    accept("0x" + "AB" * 32),
    walk_away("terms_unacceptable"),
    walk_away("inventory_constraint"),
    walk_away("no_further_concession"),
    {**offer("80000000"), "explanation": ""},
    {**offer("80000000"), "explanation": "x" * 280},
)


@pytest.mark.parametrize("response", VALID_RESPONSES)
def test_every_schema_valid_response_parses(response: Any) -> None:
    assert DECISION_SCHEMA.is_valid(response)
    assert parse_decision(response).ok


@pytest.mark.parametrize("case", STRUCTURAL, ids=lambda case: case.name)
def test_every_structural_refusal_is_one_the_schema_makes_too(case: Refusal) -> None:
    """Except the two rows where the validator is knowingly stricter; see agent_validation_cases."""
    assert not parse_decision(case.response).ok
    assert DECISION_SCHEMA.is_valid(case.response) is not case.schema_agrees


# --------------------------------------------------------------------------------------
# Every adjacent pair in protocol 11.1's order: a decision that breaks both rules is refused for the
# earlier one. The adversarial review found seven of eight adjacent swaps went unnoticed, and one of
# them changes the deterministic policy's signed close reason (ADR-043).
# --------------------------------------------------------------------------------------

POOR_BUYER = {"base_minor": "0", "quote_minor": "90000000"}
FLOORED_BUYER = {
    "reservation_price_minor": "100000000",
    "min_remaining_inventory_minor": "200000000",
    "instructions": "",
}

ORDER_PAIRS: list[tuple[str, Any, Any, str]] = [
    (
        "turn before offer limit",
        offer("108000000"),
        lambda: observation("seller", offers_remaining=0),
        "not_your_turn",
    ),
    (
        "offer limit before reservation",
        offer("100000001"),
        lambda: buyer_facing(80_000_000, 108_000_000, offers_remaining=0),
        "offer_limit_reached",
    ),
    (
        "reservation before balance (offer)",
        offer("100000001"),
        lambda: observation("buyer", balances=POOR_BUYER),
        "above_reservation",
    ),
    (
        "balance before floor (offer)",
        offer("95000000"),
        lambda: observation("buyer", balances=POOR_BUYER, mandate=FLOORED_BUYER),
        "insufficient_balance",
    ),
    (
        "self-acceptance before stale digest",
        accept(digest_for(9)),
        lambda: observation("buyer", history=alternating_offers(80_000_000)),
        "self_acceptance",
    ),
    (
        "stale digest before expiry",
        accept(digest_for(9)),
        lambda: seller_facing(95_000_000, chain_time=1_760_000_600),
        "stale_offer_hash",
    ),
    (
        "expiry before reservation",
        accept(digest_for(1)),
        lambda: seller_facing(85_000_000, chain_time=1_760_000_600),
        "offer_expired",
    ),
    (
        "reservation before balance (accept)",
        accept(digest_for(2)),
        lambda: buyer_facing(80_000_000, 101_000_000, balances=POOR_BUYER),
        "above_reservation",
    ),
    (
        "balance before floor (accept)",
        accept(digest_for(2)),
        lambda: buyer_facing(80_000_000, 95_000_000, balances=POOR_BUYER, mandate=FLOORED_BUYER),
        "insufficient_balance",
    ),
]


@pytest.mark.parametrize(
    ("response", "build", "code"),
    [(response, build, code) for _, response, build, code in ORDER_PAIRS],
    ids=[name for name, *_ in ORDER_PAIRS],
)
def test_the_earlier_of_two_broken_rules_is_the_one_reported(
    response: Any, build: Any, code: str
) -> None:
    assert VALIDATOR.validate(response, typed(build())).code == code
