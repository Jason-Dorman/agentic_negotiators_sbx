"""`DeterministicPolicy`: the linear-concession baseline of docs/protocol.md section 13.

The expected quotes are written out, not recomputed through the formula under test. Each is
`anchor + (R - anchor) * k / (n - 1)` worked by hand with the buyer's anchor at 0.8 R rounded down
and the seller's at 1.2 R rounded up; the comments show the arithmetic where it is not obvious.
"""

from __future__ import annotations

from typing import Any

import pytest
from agent_observations import (
    alternating_offers,
    digest_for,
    observation,
    offer_entry,
    typed,
)

from agent.observation import Role
from agent.policy import DeterministicPolicy, concession_price
from agent.validation import MandateValidator
from negotiation_protocol import MinorAmount

POLICY = DeterministicPolicy(MandateValidator())

BUYER_R = 100_000_000
SELLER_R = 90_000_000
# Quotes the other side offers that this side will never accept, so the policy offers instead.
UNACCEPTABLE_TO: dict[Role, int] = {"buyer": 200_000_000, "seller": 1}


def facing_own_offer_index(role: Role, k: int, max_offers: int, **kwargs: Any) -> dict[str, Any]:
    """`role`'s turn, having made `k` offers, facing an unacceptable counteroffer (if any)."""
    other = UNACCEPTABLE_TO[role]
    own = 55_000_000
    count = 2 * k if role == "buyer" else 2 * k + 1
    quotes = [own if (index % 2 == 0) == (role == "buyer") else other for index in range(count)]
    return observation(role, history=alternating_offers(*quotes), max_offers=max_offers, **kwargs)


async def decided(document: dict[str, Any]) -> dict[str, Any]:
    response = await POLICY.decide(typed(document), None, time_left_s=None)
    raw = response.raw_response
    assert isinstance(raw, dict)
    assert set(raw) == {"decision"}, "the baseline states no explanation"
    decision: dict[str, Any] = raw["decision"]
    return decision


def offer(amount: int) -> dict[str, Any]:
    return {"action": "offer", "quote_amount_minor": str(amount)}


# --------------------------------------------------------------------------------------
# The concession schedule
# --------------------------------------------------------------------------------------

SCHEDULES: list[tuple[Role, int, list[int]]] = [
    # maxOffers 1: the buyer has one opportunity and offers R. The seller has none, which
    # test_the_seller_with_no_opportunities_refuses_an_offer_outside_bound covers.
    ("buyer", 1, [100_000_000]),
    # maxOffers 2: one each, so each offers its bound.
    ("buyer", 2, [100_000_000]),
    ("seller", 2, [90_000_000]),
    # maxOffers 3: the buyer gets the odd one. n = 2: 80 then 100.
    ("buyer", 3, [80_000_000, 100_000_000]),
    ("seller", 3, [90_000_000]),
    # maxOffers 8, the default: n = 4 each.
    # Buyer 80 + 20 k/3: 80, 86.666666.. (down), 93.333333.. (down), 100.
    ("buyer", 8, [80_000_000, 86_666_666, 93_333_333, 100_000_000]),
    # Seller 108 - 18 k/3: 108, 102, 96, 90.
    ("seller", 8, [108_000_000, 102_000_000, 96_000_000, 90_000_000]),
]


@pytest.mark.parametrize(("role", "max_offers", "quotes"), SCHEDULES)
async def test_the_schedule_for_small_offer_limits(
    role: Role, max_offers: int, quotes: list[int]
) -> None:
    for k, quote in enumerate(quotes):
        document = facing_own_offer_index(role, k, max_offers)
        assert await decided(document) == offer(quote), f"own offer index {k}"


@pytest.mark.parametrize(
    ("role", "k", "quote"),
    [
        # n = 16, so the step is (R - anchor) / 15.
        ("buyer", 0, 80_000_000),
        ("buyer", 1, 81_333_333),  # 80 + 20/15 = 81.333333.. rounded down
        ("buyer", 7, 89_333_333),  # 80 + 140/15 = 89.333333.. rounded down
        ("buyer", 15, 100_000_000),
        ("seller", 0, 108_000_000),
        ("seller", 1, 106_800_000),  # 108 - 18/15 = 106.8 exactly
        ("seller", 7, 99_600_000),  # 108 - 126/15 = 99.6 exactly
        ("seller", 15, 90_000_000),
    ],
)
async def test_the_schedule_at_thirty_two_offers(role: Role, k: int, quote: int) -> None:
    assert await decided(facing_own_offer_index(role, k, 32)) == offer(quote)


async def test_the_buyer_rounds_down_and_the_seller_rounds_up() -> None:
    """R = 90000001: the buyer's anchor 72000000.8 and the seller's 108000001.2."""
    odd_r = {"reservation_price_minor": "90000001", "min_remaining_inventory_minor": "0"}
    buyer = facing_own_offer_index("buyer", 0, 8, mandate={**odd_r, "instructions": ""})
    seller = facing_own_offer_index(
        "seller",
        0,
        8,
        mandate={
            "reservation_price_minor": "90000001",
            "min_remaining_inventory_minor": "10000000",
            "instructions": "",
        },
    )
    assert await decided(buyer) == offer(72_000_000)
    assert await decided(seller) == offer(108_000_002)


async def test_a_price_never_falls_below_one_minor_unit() -> None:
    """R = 1: the buyer's anchor rounds down to 0, and the policy offers 1 instead."""
    mandate = {
        "reservation_price_minor": "1",
        "min_remaining_inventory_minor": "0",
        "instructions": "",
    }
    assert await decided(observation("buyer", mandate=mandate)) == offer(1)


@pytest.mark.parametrize(
    ("role", "bound", "k", "n", "expected"),
    [
        ("buyer", 100, 3, 4, 100),
        ("buyer", 100, 7, 4, 100),  # k past the last index still stops at R
        ("seller", 90, 7, 4, 90),
        ("buyer", 100, 0, 1, 100),  # one opportunity: offer R
        ("seller", 90, 0, 1, 90),
        ("buyer", 0, 0, 4, 1),  # a zero bound still gives a positive price; validation refuses it
    ],
)
def test_the_price_is_clamped_to_the_policys_own_bound_and_kept_positive(
    role: Role, bound: int, k: int, n: int, expected: int
) -> None:
    assert concession_price(role, MinorAmount(bound), k, n) == expected


def test_a_seller_price_stops_at_the_largest_amount_a_uint256_can_hold() -> None:
    """1.2 R overflows uint256 for R near its top; the policy keeps itself representable."""
    top = MinorAmount(2**256 - 1)
    assert concession_price("seller", top, 0, 4) == 2**256 - 1


@pytest.mark.parametrize("n", [0, -1])
def test_no_price_exists_without_an_opportunity(n: int) -> None:
    with pytest.raises(ValueError, match="opportunit"):
        concession_price("buyer", MinorAmount(100), 0, n)


# --------------------------------------------------------------------------------------
# Acceptance
# --------------------------------------------------------------------------------------


async def test_the_seller_accepts_an_offer_within_its_bound() -> None:
    document = observation("seller", history=alternating_offers(95_000_000))
    assert await decided(document) == {"action": "accept", "offer_hash": digest_for(1)}


async def test_the_seller_accepts_an_offer_exactly_at_its_bound() -> None:
    document = observation("seller", history=alternating_offers(90_000_000))
    assert await decided(document) == {"action": "accept", "offer_hash": digest_for(1)}


async def test_the_buyer_accepts_an_offer_exactly_at_its_bound() -> None:
    document = observation("buyer", history=alternating_offers(80_000_000, 100_000_000))
    assert await decided(document) == {"action": "accept", "offer_hash": digest_for(2)}


async def test_the_seller_counters_an_offer_one_unit_below_its_bound() -> None:
    document = observation("seller", history=alternating_offers(89_999_999))
    assert await decided(document) == offer(108_000_000)


async def test_acceptance_needs_no_remaining_opportunity() -> None:
    document = observation(
        "buyer", history=alternating_offers(80_000_000, 95_000_000), offers_remaining=0
    )
    assert await decided(document) == {"action": "accept", "offer_hash": digest_for(2)}


async def test_an_expired_offer_within_bound_is_not_accepted_and_the_policy_offers() -> None:
    history = [
        offer_entry(1, "buyer", 80_000_000),
        offer_entry(2, "seller", 95_000_000, valid_until=1_759_999_000, status="expired"),
    ]
    document = observation("buyer", history=history, active=None)
    assert await decided(document) == offer(86_666_666)


# --------------------------------------------------------------------------------------
# Walking away (docs/protocol.md section 13, ADR-043)
# --------------------------------------------------------------------------------------


def walk(reason: str) -> dict[str, Any]:
    return {"action": "walk_away", "reason": reason}


async def test_out_of_opportunities_facing_an_offer_outside_bound_is_terms_unacceptable() -> None:
    """The infeasible clone's last turn: the seller's final 105 against the buyer's 100."""
    quotes = [80, 126, 86, 119, 93, 112, 100, 105]
    document = observation("buyer", history=alternating_offers(*(q * 10**6 for q in quotes)))
    assert document["offers_remaining_for_me"] == 0
    assert await decided(document) == walk("terms_unacceptable")


async def test_the_seller_with_no_opportunities_refuses_an_offer_outside_bound() -> None:
    document = observation("seller", history=alternating_offers(80_000_000), max_offers=1)
    assert await decided(document) == walk("terms_unacceptable")


async def test_out_of_opportunities_with_no_active_offer_is_no_further_concession() -> None:
    history = [
        offer_entry(1, "buyer", 80_000_000),
        offer_entry(2, "seller", 150_000_000, valid_until=1_759_999_000, status="expired"),
    ]
    document = observation("buyer", history=history, active=None, offers_remaining=0)
    assert await decided(document) == walk("no_further_concession")


async def test_out_of_opportunities_facing_an_expired_offer_within_bound() -> None:
    """Within bound but expired: not an offer outside bound, so no_further_concession."""
    history = [
        offer_entry(1, "buyer", 80_000_000),
        offer_entry(2, "seller", 95_000_000, valid_until=1_760_000_000, status="active"),
    ]
    document = observation("buyer", history=history, offers_remaining=0)
    assert await decided(document) == walk("no_further_concession")


async def test_a_zero_bound_makes_every_offer_illegal_so_the_buyer_leaves() -> None:
    mandate = {
        "reservation_price_minor": "0",
        "min_remaining_inventory_minor": "0",
        "instructions": "",
    }
    assert await decided(observation("buyer", mandate=mandate)) == walk("no_further_concession")


async def test_a_seller_below_its_inventory_floor_leaves_with_inventory_constraint() -> None:
    """Within bound, but accepting and offering both breach the floor (ADR-043)."""
    document = observation(
        "seller",
        history=alternating_offers(95_000_000),
        balances={"base_minor": "15000000", "quote_minor": "0"},
    )
    assert await decided(document) == walk("inventory_constraint")


async def test_the_floor_that_blocks_its_counteroffer_is_the_reason_it_gives() -> None:
    """Facing an offer outside bound it would counter; the floor refuses the counter (ADR-043)."""
    document = observation(
        "seller",
        history=alternating_offers(1),
        balances={"base_minor": "15000000", "quote_minor": "0"},
    )
    assert await decided(document) == walk("inventory_constraint")


async def test_with_no_counter_left_to_make_the_price_is_the_reason() -> None:
    """No opportunity remains, so no offer was blocked by the floor: the price was refused."""
    document = observation(
        "seller",
        history=alternating_offers(1),
        balances={"base_minor": "15000000", "quote_minor": "0"},
        offers_remaining=0,
    )
    assert await decided(document) == walk("terms_unacceptable")


async def test_an_expired_offer_outside_bound_with_no_opportunity_is_terms_unacceptable() -> None:
    """Section 13 names the incoming offer's price, whichever check the validator reports first."""
    history = [
        offer_entry(1, "buyer", 80_000_000),
        offer_entry(2, "seller", 150_000_000, valid_until=1_760_000_000, status="active"),
    ]
    document = observation("buyer", history=history, offers_remaining=0)
    assert await decided(document) == walk("terms_unacceptable")


async def test_a_buyer_that_cannot_afford_the_offer_it_would_accept_counters_instead() -> None:
    document = observation(
        "buyer",
        history=alternating_offers(80_000_000, 95_000_000),
        balances={"base_minor": "0", "quote_minor": "90000000"},
    )
    assert await decided(document) == offer(86_666_666)


async def test_a_buyer_that_can_afford_neither_leaves_with_inventory_constraint() -> None:
    document = observation(
        "buyer",
        history=alternating_offers(80_000_000, 95_000_000),
        balances={"base_minor": "0", "quote_minor": "85000000"},
    )
    assert await decided(document) == walk("inventory_constraint")


async def test_a_buyer_out_of_opportunities_that_cannot_afford_the_offer() -> None:
    document = observation(
        "buyer",
        history=alternating_offers(80_000_000, 95_000_000),
        balances={"base_minor": "0", "quote_minor": "90000000"},
        offers_remaining=0,
    )
    assert await decided(document) == walk("inventory_constraint")


# --------------------------------------------------------------------------------------
# Identity and determinism
# --------------------------------------------------------------------------------------


async def test_every_decision_it_makes_passes_the_validator() -> None:
    """Across the whole default schedule, both sides: the baseline never needs a repair."""
    validator = MandateValidator()
    roles: tuple[Role, ...] = ("buyer", "seller")
    for role in roles:
        for k in range(4):
            document = facing_own_offer_index(role, k, 8)
            response = await POLICY.decide(typed(document), None, time_left_s=None)
            assert validator.validate(response.raw_response, typed(document)).ok


async def test_the_same_observation_gives_the_same_decision() -> None:
    document = facing_own_offer_index("buyer", 2, 8)
    first = await POLICY.decide(typed(document), None, time_left_s=None)
    second = await POLICY.decide(typed(document), None, time_left_s=None)
    assert first == second


def test_it_reports_its_kind_and_version_and_no_prompt() -> None:
    assert POLICY.kind == "deterministic"
    assert POLICY.version == "det-1.0.0"
    assert POLICY.prompt_template_version is None


async def test_a_deterministic_response_carries_no_model_accounting() -> None:
    response = await POLICY.decide(typed(observation("buyer")), None, time_left_s=None)
    assert response.stop_reason is None
    assert response.usage is None
    assert response.cost_estimated_usd is None
    assert response.cost_reported_usd is None


async def test_an_offer_outside_bound_is_refused_on_price_before_holdings() -> None:
    """Above its bound AND unaffordable, with no counter left: the price is the reason.

    This is where protocol 11.1's order reaches the chain. Were the balance checked before the
    reservation, the refusal would be a holdings one and the signed Close would say
    `inventory_constraint` (ADR-043) for what is a price impasse.
    """
    document = observation(
        "buyer",
        history=alternating_offers(80_000_000, 101_000_000),
        balances={"base_minor": "0", "quote_minor": "90000000"},
        offers_remaining=0,
    )
    assert await decided(document) == walk("terms_unacceptable")


async def test_a_buyers_capital_floor_stops_its_concessions_at_what_it_can_spare() -> None:
    """250 mUSD with a 160 floor leaves 90 to spend: 80 and 86.666666 fit, 93.333333 does not."""
    mandate = {
        "reservation_price_minor": "100000000",
        "min_remaining_inventory_minor": "160000000",
        "instructions": "",
    }
    assert await decided(facing_own_offer_index("buyer", 1, 8, mandate=mandate)) == offer(
        86_666_666
    )
    third = facing_own_offer_index("buyer", 2, 8, mandate=mandate)
    assert await decided(third) == walk("inventory_constraint")
