"""`agent.consistency`: an observation that contradicts itself is refused, not signed on (ADR-046).

Every observation here starts consistent — real EIP-712 digests for the fixture session, statuses as
protocol section 12 implies — and each test breaks exactly one thing and asserts the location that
is named. The first tests show the consistent ones pass, so a check that refused everything could
not pass this file.
"""

from __future__ import annotations

from typing import Any

import pytest
from agent_observations import (
    CHAIN_TIME,
    active_from,
    consistent_observation,
    fixture_approval,
    typed,
)

from agent.consistency import contradictions
from negotiation_protocol import UINT64_MAX


def problems(document: dict[str, Any]) -> dict[str, str]:
    return contradictions(typed(document), fixture_approval())


@pytest.mark.parametrize(
    ("role", "quotes", "kwargs"),
    [
        ("buyer", (), {}),
        ("seller", (80_000_000,), {}),
        ("buyer", (80_000_000, 108_000_000), {}),
        ("seller", (80_000_000, 108_000_000, 86_666_666), {}),
        ("seller", (80_000_000,), {"chain_time": CHAIN_TIME + 700}),  # the offer has expired
    ],
)
def test_a_consistent_observation_has_no_contradictions(
    role: str, quotes: tuple[int, ...], kwargs: dict[str, Any]
) -> None:
    assert problems(consistent_observation(role, *quotes, **kwargs)) == {}  # type: ignore[arg-type]  # reason: parametrised Literal


def test_history_out_of_ascending_order_is_named() -> None:
    document = consistent_observation("buyer", 80_000_000, 108_000_000)
    document["history"].reverse()
    assert "history/0/sequence" in problems(document)


def test_a_terminal_kind_in_history_is_named() -> None:
    document = consistent_observation("seller", 80_000_000)
    document["history"][0]["kind"] = "close"
    assert set(problems(document)) >= {"history/0/kind"}


def test_offers_that_do_not_alternate_from_the_buyer_are_named() -> None:
    document = consistent_observation("seller", 80_000_000)
    document["history"][0]["actor"] = "seller"
    assert "history/0/actor" in problems(document)


@pytest.mark.parametrize("field", ["quote_amount_minor", "valid_until", "offer_hash", "status"])
def test_an_offer_missing_any_field_is_named(field: str) -> None:
    """The schema makes all four optional on a history entry; an offer needs every one."""
    document = consistent_observation("seller", 80_000_000)
    del document["history"][0][field]
    assert "history/0" in problems(document)


def test_an_offer_hash_that_is_not_its_fields_digest_is_named() -> None:
    """The amount the mandate is checked against is bound to the hash a policy may accept."""
    document = consistent_observation("seller", 95_000_000)
    for place in (document["history"][0], document["active_offer"]):
        place["quote_amount_minor"] = "96000000"
    assert set(problems(document)) == {"history/0/offer_hash"}


def test_a_valid_until_beyond_uint64_is_named_not_raised() -> None:
    document = consistent_observation("seller", 95_000_000)
    for place in (document["history"][0], document["active_offer"]):
        place["valid_until"] = UINT64_MAX + 1
    assert "history/0/offer_hash" in problems(document)


@pytest.mark.parametrize(("index", "status"), [(0, "active"), (1, "replaced"), (1, "expired")])
def test_a_wrong_status_is_named(index: int, status: str) -> None:
    document = consistent_observation("buyer", 80_000_000, 108_000_000)
    document["history"][index]["status"] = status
    if index == 1 and status != "active":
        document["active_offer"] = active_from(document["history"][1])
    assert f"history/{index}/status" in problems(document)


def test_more_offers_than_the_session_allows_are_named() -> None:
    document = consistent_observation("buyer", 1, 2, max_offers=1, offers_remaining=0)
    assert "history" in problems(document)


def test_an_expected_sequence_that_is_not_the_next_is_named() -> None:
    document = consistent_observation("seller", 80_000_000)
    document["expected_sequence"] = 3
    assert set(problems(document)) == {"expected_sequence"}


def test_offers_remaining_that_is_not_the_share_less_recorded_offers_is_named() -> None:
    document = consistent_observation("seller", 80_000_000, offers_remaining=3)
    assert set(problems(document)) == {"offers_remaining_for_me"}


def test_a_standing_offer_left_out_of_active_offer_is_named() -> None:
    document = consistent_observation("seller", 80_000_000)
    document["active_offer"] = None
    assert set(problems(document)) == {"active_offer"}


def test_an_expired_offer_sent_as_active_is_named() -> None:
    """Protocol 12: an expired offer does not stand, so active_offer is null."""
    document = consistent_observation("seller", 80_000_000, chain_time=CHAIN_TIME + 700)
    document["active_offer"] = active_from(document["history"][0])
    assert set(problems(document)) == {"active_offer"}


def test_an_active_offer_that_is_not_the_last_recorded_one_is_named() -> None:
    document = consistent_observation("seller", 80_000_000, 108_000_000, 86_666_666)
    document["active_offer"] = active_from(document["history"][1])
    assert set(problems(document)) == {"active_offer"}


def test_an_active_offer_with_no_history_is_named() -> None:
    document = consistent_observation("buyer")
    standing = consistent_observation("seller", 80_000_000)["active_offer"]
    document["active_offer"] = standing
    assert set(problems(document)) == {"active_offer"}
