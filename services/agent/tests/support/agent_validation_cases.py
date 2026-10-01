"""The table of refused decisions: one row per way a policy's response can be refused.

Two suites read it. `test_validator.py` asserts each row's code and its private feedback text, word
for word, because that text is what a model reads on repair. `test_turns.py` runs each row through
the turn executor and asserts that nothing was signed (docs/test_strategy.md section 6).

`schema_agrees` is False on the rows where the validator is deliberately stricter than
`agent_decision.v1.json` as Python's `jsonschema` evaluates it: a trailing newline, which
`re.search` lets `$` accept, and a 78-digit amount above `uint256`, which the pattern's length
bound admits.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from agent_observations import (
    active_from,
    alternating_offers,
    digest_for,
    observation,
    offer_entry,
)

UINT256_MAX = 2**256 - 1

#: A buyer that keeps at least 200 mUSD of its 250: it can pay at most 50 (ADR-045).
BUYER_FLOOR_200 = {
    "reservation_price_minor": "100000000",
    "min_remaining_inventory_minor": "200000000",
    "instructions": "",
}


def offer(amount: Any) -> dict[str, Any]:
    return {"decision": {"action": "offer", "quote_amount_minor": amount}}


def accept(offer_hash: Any) -> dict[str, Any]:
    return {"decision": {"action": "accept", "offer_hash": offer_hash}}


def walk_away(reason: Any) -> dict[str, Any]:
    return {"decision": {"action": "walk_away", "reason": reason}}


def first_turn() -> dict[str, Any]:
    return observation("buyer")


def seller_facing(quote: int, **kwargs: Any) -> dict[str, Any]:
    """The seller's turn after the buyer's opening offer of `quote`."""
    return observation("seller", history=alternating_offers(quote), **kwargs)


def buyer_facing(buyer_quote: int, seller_quote: int, **kwargs: Any) -> dict[str, Any]:
    """The buyer's turn after its own offer and the seller's counter of `seller_quote`."""
    return observation("buyer", history=alternating_offers(buyer_quote, seller_quote), **kwargs)


@dataclass(frozen=True)
class Refusal:
    name: str
    response: Any
    code: str
    feedback: str
    build: Callable[[], dict[str, Any]] = first_turn
    schema_agrees: bool = True


AMOUNT_FEEDBACK = (
    '"quote_amount_minor" must be a whole number of minor units written as a base-10 string, '
    'with no sign, decimal point, exponent or leading zeros, for example "94000000".'
)
REASON_FEEDBACK = (
    '"reason" must be "terms_unacceptable", "inventory_constraint" or "no_further_concession".'
)
ENVELOPE_FEEDBACK = (
    'Your response must be a JSON object: {"decision": {...}} with an optional "explanation".'
)

STRUCTURAL: tuple[Refusal, ...] = (
    Refusal("not an object: a string", "offer", "schema_error", ENVELOPE_FEEDBACK),
    Refusal("not an object: a list", [offer("1")], "schema_error", ENVELOPE_FEEDBACK),
    Refusal("not an object: null", None, "schema_error", ENVELOPE_FEEDBACK),
    Refusal(
        "extra envelope field",
        {**offer("80000000"), "reasoning": "anchor low"},
        "extra_fields",
        'Your response may contain only "decision" and an optional "explanation"; '
        'remove "reasoning".',
    ),
    Refusal(
        "several extra envelope fields, named in order",
        {**offer("80000000"), "b": 1, "a": 2},
        "extra_fields",
        'Your response may contain only "decision" and an optional "explanation"; remove "a", "b".',
    ),
    Refusal(
        "no decision",
        {"explanation": "thinking"},
        "schema_error",
        'Your response has no "decision". It must contain exactly one decision.',
    ),
    Refusal(
        "explanation over 280 characters",
        {**offer("80000000"), "explanation": "x" * 281},
        "schema_error",
        '"explanation" must be a string of at most 280 characters.',
    ),
    Refusal(
        "explanation not a string",
        {**offer("80000000"), "explanation": 7},
        "schema_error",
        '"explanation" must be a string of at most 280 characters.',
    ),
    Refusal(
        "decision not an object",
        {"decision": "offer"},
        "schema_error",
        '"decision" must be a JSON object.',
    ),
    Refusal(
        "unknown action",
        {"decision": {"action": "counter", "quote_amount_minor": "1"}},
        "schema_error",
        '"decision.action" must be "offer", "accept" or "walk_away".',
    ),
    Refusal(
        "no action",
        {"decision": {"quote_amount_minor": "80000000"}},
        "schema_error",
        '"decision.action" must be "offer", "accept" or "walk_away".',
    ),
    Refusal(
        "action not a string",
        {"decision": {"action": ["offer"], "quote_amount_minor": "80000000"}},
        "schema_error",
        '"decision.action" must be "offer", "accept" or "walk_away".',
    ),
    Refusal(
        "extra field in an offer: the model tries to set validUntil",
        {"decision": {"action": "offer", "quote_amount_minor": "80000000", "valid_until": 1}},
        "extra_fields",
        'An offer decision may contain only "action" and "quote_amount_minor"; '
        'remove "valid_until".',
    ),
    Refusal(
        "extra field in an accept: the model tries to name a counterparty",
        {"decision": {"action": "accept", "offer_hash": digest_for(1), "actor": "0x0"}},
        "extra_fields",
        'An accept decision may contain only "action" and "offer_hash"; remove "actor".',
    ),
    Refusal(
        "extra field in a walk-away",
        {"decision": {"action": "walk_away", "reason": "terms_unacceptable", "code": 1}},
        "extra_fields",
        'A walk_away decision may contain only "action" and "reason"; remove "code".',
    ),
    Refusal(
        "offer with no amount",
        {"decision": {"action": "offer"}},
        "schema_error",
        'An offer decision must include "quote_amount_minor".',
    ),
    Refusal(
        "accept with no hash",
        {"decision": {"action": "accept"}},
        "schema_error",
        'An accept decision must include "offer_hash".',
    ),
    Refusal(
        "walk-away with no reason",
        {"decision": {"action": "walk_away"}},
        "schema_error",
        'A walk_away decision must include "reason".',
    ),
    Refusal("non-integer amount", offer("94.5"), "invalid_amount", AMOUNT_FEEDBACK),
    Refusal("amount as a JSON number", offer(94000000), "invalid_amount", AMOUNT_FEEDBACK),
    Refusal("amount as a boolean", offer(True), "invalid_amount", AMOUNT_FEEDBACK),
    Refusal("negative amount", offer("-5"), "invalid_amount", AMOUNT_FEEDBACK),
    Refusal("leading zeros", offer("007"), "invalid_amount", AMOUNT_FEEDBACK),
    Refusal("exponent", offer("1e6"), "invalid_amount", AMOUNT_FEEDBACK),
    Refusal("leading space", offer(" 94000000"), "invalid_amount", AMOUNT_FEEDBACK),
    Refusal("79 digits", offer("1" + "0" * 78), "invalid_amount", AMOUNT_FEEDBACK),
    Refusal(
        "trailing newline",
        offer("94000000\n"),
        "invalid_amount",
        AMOUNT_FEEDBACK,
        schema_agrees=False,
    ),
    Refusal(
        "78 digits above uint256",
        offer(str(UINT256_MAX + 1)),
        "invalid_amount",
        AMOUNT_FEEDBACK,
        schema_agrees=False,
    ),
    Refusal(
        "zero amount",
        offer("0"),
        "zero_amount",
        '"quote_amount_minor" must be greater than zero.',
    ),
    Refusal(
        "offer hash too short",
        accept("0x1234"),
        "invalid_offer_hash",
        '"offer_hash" must be 0x followed by 64 hexadecimal characters.',
        build=lambda: seller_facing(95_000_000),
    ),
    Refusal(
        "offer hash with no 0x",
        accept(digest_for(1)[2:]),
        "invalid_offer_hash",
        '"offer_hash" must be 0x followed by 64 hexadecimal characters.',
        build=lambda: seller_facing(95_000_000),
    ),
    Refusal(
        "offer hash as a number",
        accept(1),
        "invalid_offer_hash",
        '"offer_hash" must be 0x followed by 64 hexadecimal characters.',
        build=lambda: seller_facing(95_000_000),
    ),
    Refusal("reason not in the table", walk_away("bored"), "invalid_reason", REASON_FEEDBACK),
    Refusal("reason as its code", walk_away(1), "invalid_reason", REASON_FEEDBACK),
    Refusal("reason null", walk_away(None), "invalid_reason", REASON_FEEDBACK),
    Refusal(
        "reason as a list: unhashable, so a lookup would raise",
        walk_away(["terms_unacceptable"]),
        "invalid_reason",
        REASON_FEEDBACK,
    ),
)


ECONOMIC: tuple[Refusal, ...] = (
    Refusal(
        "the seller offers first",
        offer("108000000"),
        "not_your_turn",
        "It is not your turn to offer: the next offer belongs to the buyer.",
        build=lambda: observation("seller"),
    ),
    Refusal(
        "the buyer offers twice in a row",
        offer("85000000"),
        "not_your_turn",
        "It is not your turn to offer: the next offer belongs to the seller.",
        build=lambda: observation("buyer", history=alternating_offers(80_000_000)),
    ),
    Refusal(
        "the turn after an expired offer still belongs to its counterparty (ADR-016)",
        offer("85000000"),
        "not_your_turn",
        "It is not your turn to offer: the next offer belongs to the seller.",
        build=lambda: observation(
            "buyer",
            history=[offer_entry(1, "buyer", 80_000_000, status="expired")],
            active=None,
        ),
    ),
    Refusal(
        "no offers left for me",
        offer("86666666"),
        "offer_limit_reached",
        "You have no offers left in this session. You may accept the active offer or walk away.",
        build=lambda: buyer_facing(80_000_000, 108_000_000, offers_remaining=0),
    ),
    Refusal(
        "the session's offer limit is reached even if my own count says otherwise",
        offer("100000000"),
        "offer_limit_reached",
        "You have no offers left in this session. You may accept the active offer or walk away.",
        build=lambda: observation(
            "buyer",
            history=alternating_offers(1, 2, 3, 4, 5, 6, 7, 8),
            offers_remaining=1,
        ),
    ),
    Refusal(
        "the buyer offers one minor unit above its reservation",
        offer("100000001"),
        "above_reservation",
        "Your offer of 100000001 is above your reservation price of 100000000. "
        "As the buyer you may not pay more than that.",
    ),
    Refusal(
        "the seller offers one minor unit below its reservation",
        offer("89999999"),
        "below_reservation",
        "Your offer of 89999999 is below your reservation price of 90000000. "
        "As the seller you may not accept less than that.",
        build=lambda: seller_facing(80_000_000),
    ),
    Refusal(
        "the buyer offers more than its quote balance",
        offer("95000000"),
        "insufficient_balance",
        "This trade would pay 95000000 of the quote token, but your quote balance is only "
        "90000000.",
        build=lambda: observation("buyer", balances={"base_minor": "0", "quote_minor": "90000000"}),
    ),
    Refusal(
        "the seller holds less than the base amount",
        offer("100000000"),
        "insufficient_balance",
        "This trade would deliver 10000000 of the base token, but your base balance is only "
        "5000000.",
        build=lambda: seller_facing(
            80_000_000,
            balances={"base_minor": "5000000", "quote_minor": "0"},
            mandate={
                "reservation_price_minor": "90000000",
                "min_remaining_inventory_minor": "0",
                "instructions": "",
            },
        ),
    ),
    Refusal(
        "the seller's trade would breach its inventory floor",
        offer("100000000"),
        "inventory_floor",
        "This trade would leave you 5000000 of the base token, below your minimum remaining "
        "inventory of 10000000.",
        build=lambda: seller_facing(
            80_000_000, balances={"base_minor": "15000000", "quote_minor": "0"}
        ),
    ),
    Refusal(
        "a buyer's capital floor: paying would leave it below the mUSD it keeps (ADR-045)",
        offer("80000000"),
        "inventory_floor",
        "This trade would leave you 170000000 of the quote token, below your minimum remaining "
        "inventory of 200000000.",
        build=lambda: observation("buyer", mandate=BUYER_FLOOR_200),
    ),
    Refusal(
        "a buyer's capital floor refuses an acceptance too",
        accept(digest_for(2)),
        "inventory_floor",
        "This trade would leave you 155000000 of the quote token, below your minimum remaining "
        "inventory of 200000000.",
        build=lambda: buyer_facing(80_000_000, 95_000_000, mandate=BUYER_FLOOR_200),
    ),
    Refusal(
        "accept with no active offer",
        accept(digest_for(1)),
        "no_active_offer",
        "There is no active offer to accept.",
        build=lambda: seller_facing(95_000_000, active=None),
    ),
    Refusal(
        "the buyer accepts its own offer",
        accept(digest_for(1)),
        "self_acceptance",
        "The active offer is your own; you cannot accept it.",
        build=lambda: observation("buyer", history=alternating_offers(80_000_000)),
    ),
    Refusal(
        "accept of a replaced offer",
        accept(digest_for(1)),
        "stale_offer_hash",
        f'"offer_hash" is not the active offer. The active offer is {digest_for(3)}.',
        build=lambda: observation(
            "seller", history=alternating_offers(80_000_000, 108_000_000, 86_000_000)
        ),
    ),
    Refusal(
        "accept at the offer's validUntil",
        accept(digest_for(1)),
        "offer_expired",
        "The active offer expired at chain time 1760000600 and the chain time is now 1760000600; "
        "it can no longer be accepted.",
        build=lambda: seller_facing(95_000_000, chain_time=1_760_000_600),
    ),
    Refusal(
        "the seller accepts below its reservation",
        accept(digest_for(1)),
        "below_reservation",
        "The active offer of 85000000 is below your reservation price of 90000000. "
        "As the seller you may not accept less than that.",
        build=lambda: seller_facing(85_000_000),
    ),
    Refusal(
        "the buyer accepts above its reservation",
        accept(digest_for(2)),
        "above_reservation",
        "The active offer of 101000000 is above your reservation price of 100000000. "
        "As the buyer you may not pay more than that.",
        build=lambda: buyer_facing(80_000_000, 101_000_000),
    ),
    Refusal(
        "the buyer accepts more than its quote balance",
        accept(digest_for(2)),
        "insufficient_balance",
        "This trade would pay 95000000 of the quote token, but your quote balance is only "
        "90000000.",
        build=lambda: buyer_facing(
            80_000_000, 95_000_000, balances={"base_minor": "0", "quote_minor": "90000000"}
        ),
    ),
    Refusal(
        "the seller's acceptance would breach its inventory floor",
        accept(digest_for(1)),
        "inventory_floor",
        "This trade would leave you 5000000 of the base token, below your minimum remaining "
        "inventory of 10000000.",
        build=lambda: seller_facing(
            95_000_000, balances={"base_minor": "15000000", "quote_minor": "0"}
        ),
    ),
)

ALL: tuple[Refusal, ...] = STRUCTURAL + ECONOMIC


def active_offer_of(document: dict[str, Any]) -> dict[str, Any]:
    """The active offer a builder produced, for tests that need its hash."""
    active = document["active_offer"]
    assert active is not None
    return dict(active)


__all__ = [
    "ALL",
    "ECONOMIC",
    "STRUCTURAL",
    "Refusal",
    "accept",
    "active_from",
    "active_offer_of",
    "buyer_facing",
    "offer",
    "seller_facing",
    "walk_away",
]
