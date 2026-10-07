"""The A12 scanner's own forms (stage 3.3 review): what a needle finds, in the shapes surfaces use.

The A12 runs exercise the scanner on the values the suite happens to choose; these pin each form it
claims, so a needle that lost one would fail here rather than leave a surface silently unscanned.
"""

from __future__ import annotations

import json

import pytest
from leakage import Needle, Scanner

FEEDBACK = 'Your response may contain only "decision"; remove "x" — über \\ wrong.'


@pytest.mark.parametrize(
    "rendered",
    [
        FEEDBACK,
        json.dumps({"f": FEEDBACK}),
        json.dumps({"f": FEEDBACK}, ensure_ascii=False),
        json.dumps(json.dumps({"f": FEEDBACK})),
        json.dumps(json.dumps({"f": FEEDBACK}, ensure_ascii=False), ensure_ascii=False),
    ],
    ids=["raw", "escaped_ascii", "escaped", "twice_ascii", "twice"],
)
def test_a_text_needle_is_found_raw_and_json_escaped(rendered: str) -> None:
    assert Scanner([Needle.text("f", FEEDBACK)]).found(rendered) == ["f"]


@pytest.mark.parametrize(
    ("minor", "rendered"),
    [
        (88_642_317, "88642317"),
        (88_642_317, "floor 88.642317 mUSD"),
        (88_642_310, "88.642310"),
        (88_642_310, "Seller counters at 88.64231 mUSD."),  # format_minor trims the zero
    ],
)
def test_an_amount_needle_is_found_in_each_rendering(minor: int, rendered: str) -> None:
    assert Scanner([Needle.amount("a", minor)]).found(rendered) == ["a"]


@pytest.mark.parametrize("rendered", ["188642317", "88642317 1", "x 886423170"])
def test_an_amount_needle_is_a_whole_number_not_a_substring(rendered: str) -> None:
    found = Scanner([Needle.amount("a", 88_642_317)]).found(rendered)
    assert found == (["a"] if rendered == "88642317 1" else [])


def test_a_whole_token_amount_is_refused_as_a_needle() -> None:
    """`99000000` renders as `99`, which any text may hold: the suite must choose another."""
    with pytest.raises(AssertionError, match="whole number of tokens"):
        Needle.amount("a", 99_000_000)


def test_a_hex_key_is_found_in_any_case_with_or_without_0x() -> None:
    key = "ab" * 32
    scanner = Scanner([Needle.hex_key("k", "0x" + key)])
    for rendered in (key, key.upper(), "0x" + key, f'{{"k": "0X{key.upper()}"}}'):
        assert scanner.found(rendered) == ["k"]
    assert scanner.found(key[:-1]) == []
