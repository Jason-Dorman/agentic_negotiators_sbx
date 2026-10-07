"""`BudgetGuard` and the model price table (docs/test_strategy.md section 6, FR-A9, ADR-085).

The guard's four required cases — the call ceiling, the spend ceiling with a price table, an
unknown price refused unless the operator allows it, and the estimate kept apart from the reported
cost — each against figures worked by hand from the packaged table's rates, never recomputed with
the code under test.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from agent.budget import (
    DEFAULT_MODEL_PRICE_TABLE,
    Admission,
    BudgetGuard,
    BudgetLimits,
    BudgetRefusal,
    ModelPrice,
    ModelPriceTable,
    PriceTableError,
    RefusalReason,
    TokenUsage,
)

TABLE = ModelPriceTable.load()
SONNET = TABLE.price("claude-sonnet-5-5")
#: 1,000 input tokens at $4.00 a million (the dearest input rate, a 1-hour cache write) and 16,000
#: output tokens at $10.00: $0.004 + $0.16.
BOUND_1000 = Decimal("0.164000")


def sonnet() -> ModelPrice:
    assert SONNET is not None
    return SONNET


def guard(calls: int = 20, spend: str = "2.00", *, allow_unknown: bool = False) -> BudgetGuard:
    return BudgetGuard(
        BudgetLimits(calls, Decimal(spend)), sonnet(), allow_unknown_price=allow_unknown
    )


def admitted(outcome: Admission | BudgetRefusal) -> Admission:
    assert isinstance(outcome, Admission), outcome
    return outcome


def refused(outcome: Admission | BudgetRefusal) -> BudgetRefusal:
    assert isinstance(outcome, BudgetRefusal), outcome
    return outcome


# ---------------------------------------------------------------------------------------------
# The price table
# ---------------------------------------------------------------------------------------------


def test_the_packaged_table_prices_the_default_model_at_its_published_rates() -> None:
    price = sonnet()
    assert (
        price.input,
        price.output,
        price.cache_write_5m,
        price.cache_write_1h,
        price.cache_read,
    ) == (Decimal("2"), Decimal("10"), Decimal("2.50"), Decimal("4"), Decimal("0.20"))
    assert price.last_verified == date(2026, 10, 4)


def test_every_packaged_entry_says_where_its_price_came_from_and_when() -> None:
    document = json.loads(DEFAULT_MODEL_PRICE_TABLE.read_text(encoding="utf-8"))
    assert set(document["models"]) == {
        "claude-sonnet-5-5",
        "claude-sonnet-5",
        "claude-opus-5-5",
        "claude-opus-5",
    }
    for entry in document["models"].values():
        assert entry["source"].startswith(
            "https://platform.claude.com/docs/en/about-claude/pricing"
        )
        date.fromisoformat(entry["last_verified"])


def test_a_model_the_table_does_not_list_has_no_price_not_a_zero_one() -> None:
    assert TABLE.price("claude-unlisted") is None


def test_reported_cost_prices_each_kind_of_token_at_its_own_rate() -> None:
    usage = TokenUsage(
        input_tokens=500,
        output_tokens=2_000,
        cache_read_input_tokens=3_000,
        cache_creation_input_tokens=1_500,
        cache_creation_1h_input_tokens=500,
    )
    # 500 x 2 + 2,000 x 10 + 3,000 x 0.20 + 1,000 x 2.50 + 500 x 4 = 1,000 + 20,000 + 600 + 2,500
    # + 2,000 = 26,100 micro-dollars.
    assert sonnet().cost_usd(usage) == Decimal("0.026100")


def test_costs_are_rounded_up_to_the_micro_dollar() -> None:
    # One cache read is $0.0000002, rounded up; one output token is exactly $0.00001.
    assert sonnet().cost_usd(TokenUsage(0, 0, cache_read_input_tokens=1)) == Decimal("0.000001")
    assert sonnet().bound_usd(0, 1) == Decimal("0.000010")


def test_the_bound_prices_every_input_token_at_the_dearest_input_rate() -> None:
    assert sonnet().bound_usd(1_000, 16_000) == BOUND_1000
    # The dearest of five input rates, whichever it is.
    odd = ModelPrice(
        "odd", date(2026, 10, 4), Decimal(1), Decimal(1), Decimal(1), Decimal(1), Decimal(9)
    )
    assert odd.bound_usd(1_000_000, 1) == Decimal("9.000001")


def test_the_usage_record_has_the_four_counts_the_export_schema_admits() -> None:
    usage = TokenUsage(1, 2, 3, 4, 1)
    assert usage.as_record() == {
        "input_tokens": 1,
        "output_tokens": 2,
        "cache_read_input_tokens": 3,
        "cache_creation_input_tokens": 4,
    }


def _table(**model_overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "description": "test",
        "source": "test",
        "last_verified": "2026-10-04",
        "usd_per_million_tokens": {
            "input": "2",
            "output": "10",
            "cache_write_5m": "2.50",
            "cache_write_1h": "4",
            "cache_read": "0.20",
        },
    }
    entry.update(model_overrides)
    return {"table_version": "1", "models": {"m": entry}}


@pytest.mark.parametrize(
    ("document", "location"),
    [
        ({**_table(), "table_version": "2"}, "table_version"),
        (_table(extra="x"), "models.m.extra"),
        (_table(last_verified="yesterday"), "models.m.last_verified"),
        (
            _table(
                usd_per_million_tokens={
                    "input": "-2",
                    "output": "10",
                    "cache_write_5m": "2.50",
                    "cache_write_1h": "4",
                    "cache_read": "0.20",
                }
            ),
            "models.m.usd_per_million_tokens.input",
        ),
        (
            _table(usd_per_million_tokens={"input": "2", "output": "10"}),
            "models.m.usd_per_million_tokens.cache_read",
        ),
    ],
    ids=["version", "unknown field", "date", "negative rate", "missing rate"],
)
def test_a_malformed_table_is_refused_by_location(document: Any, location: str) -> None:
    with pytest.raises(PriceTableError, match=location):
        ModelPriceTable.from_document(document)


def test_an_unreadable_table_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(PriceTableError, match="cannot read"):
        ModelPriceTable.load(tmp_path / "missing.json")
    broken = tmp_path / "broken.json"
    broken.write_text("{", encoding="utf-8")
    with pytest.raises(PriceTableError, match="cannot read"):
        ModelPriceTable.load(broken)


def test_a_model_priced_twice_in_one_file_is_refused_not_resolved_silently(tmp_path: Path) -> None:
    entry = json.dumps(_table()["models"]["m"])
    twice = tmp_path / "twice.json"
    twice.write_text(
        '{"table_version": "1", "models": {"m": ' + entry + ', "m": ' + entry + "}}",
        encoding="utf-8",
    )
    with pytest.raises(PriceTableError, match="appears twice"):
        ModelPriceTable.load(twice)


def test_an_operators_own_table_is_read_from_its_path(tmp_path: Path) -> None:
    own = tmp_path / "prices.json"
    own.write_text(json.dumps(_table()), encoding="utf-8")
    assert ModelPriceTable.load(own).price("m") is not None


# ---------------------------------------------------------------------------------------------
# The call ceiling
# ---------------------------------------------------------------------------------------------


def test_the_call_ceiling_admits_exactly_that_many_calls() -> None:
    budget = guard(calls=2)
    admitted(budget.admit(1_000, 16_000))
    admitted(budget.admit(1_000, 16_000))
    refusal = refused(budget.admit(1_000, 16_000))
    assert refusal.reason is RefusalReason.CALL_CEILING
    assert refusal.detail == "2 of 2 model calls made"
    assert budget.calls == 2


def test_a_ceiling_of_zero_calls_refuses_the_first() -> None:
    assert refused(guard(calls=0).admit(1, 1)).reason is RefusalReason.CALL_CEILING


def test_the_call_ceiling_is_checked_before_the_price() -> None:
    budget = BudgetGuard(BudgetLimits(0, Decimal(2)), None, allow_unknown_price=False)
    assert refused(budget.admit(1, 1)).reason is RefusalReason.CALL_CEILING


# ---------------------------------------------------------------------------------------------
# The spend ceiling
# ---------------------------------------------------------------------------------------------


def test_a_call_whose_bound_reaches_the_ceiling_exactly_is_admitted() -> None:
    assert admitted(guard(spend="0.164").admit(1_000, 16_000)).estimated_usd == BOUND_1000


def test_a_call_whose_bound_is_a_micro_dollar_over_the_ceiling_is_refused_and_not_counted() -> None:
    budget = guard(spend="0.163999")
    refusal = refused(budget.admit(1_000, 16_000))
    assert refusal.reason is RefusalReason.SPEND_CEILING
    assert refusal.estimated_usd == BOUND_1000
    assert refusal.detail == (
        "USD 0 spent and USD 0.164000 at most for this call would cross the USD 0.163999 ceiling"
    )
    assert budget.calls == 0


def test_the_spend_so_far_is_what_was_reported_not_what_was_estimated() -> None:
    budget = guard(spend="0.20")
    admission = admitted(budget.admit(1_000, 16_000))
    # 1,000 input at $2.00 and 2,000 output at $10.00: $0.022.
    reported = budget.settle(admission, TokenUsage(1_000, 2_000))
    assert reported == Decimal("0.022000")
    assert budget.spent_usd == Decimal("0.022000")
    # $0.022 + $0.164 = $0.186, under $0.20; a second worst case would not be.
    admitted(budget.admit(1_000, 16_000))
    budget.settle(admission, TokenUsage(1_000, 2_000))
    assert refused(budget.admit(1_000, 16_000)).reason is RefusalReason.SPEND_CEILING


def test_a_call_that_reported_no_usage_is_charged_its_estimate() -> None:
    budget = guard()
    admission = admitted(budget.admit(1_000, 16_000))
    assert budget.settle(admission, None) is None
    assert budget.spent_usd == BOUND_1000


def test_the_estimate_and_the_reported_cost_are_kept_apart() -> None:
    budget = guard()
    admission = admitted(budget.admit(1_000, 16_000))
    reported = budget.settle(admission, TokenUsage(1_000, 300))
    assert admission.estimated_usd == BOUND_1000
    assert reported == Decimal("0.005000")
    assert admission.estimated_usd != reported


# ---------------------------------------------------------------------------------------------
# An unknown price
# ---------------------------------------------------------------------------------------------


def test_an_unknown_price_is_refused_by_default() -> None:
    budget = BudgetGuard(BudgetLimits(20, Decimal(2)), None, allow_unknown_price=False)
    refusal = refused(budget.admit(1_000, 16_000))
    assert refusal.reason is RefusalReason.UNKNOWN_PRICE
    assert refusal.estimated_usd is None
    assert budget.calls == 0


def test_an_unknown_price_the_operator_allows_is_called_with_only_the_call_ceiling_to_stop_it() -> (
    None
):
    budget = BudgetGuard(BudgetLimits(2, Decimal(0)), None, allow_unknown_price=True)
    admission = admitted(budget.admit(1_000_000, 128_000))
    assert admission.estimated_usd is None
    assert budget.settle(admission, TokenUsage(1_000_000, 128_000)) is None
    assert budget.spent_usd is None
    admitted(budget.admit(1, 1))
    assert refused(budget.admit(1, 1)).reason is RefusalReason.CALL_CEILING


# ---------------------------------------------------------------------------------------------
# What the guard refuses to be asked
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("input_tokens", "max_tokens"), [(-1, 1), (0, 0)])
def test_a_negative_count_or_a_zero_max_tokens_is_a_programming_error(
    input_tokens: int, max_tokens: int
) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        guard().admit(input_tokens, max_tokens)


def test_a_negative_ceiling_is_refused() -> None:
    with pytest.raises(ValueError, match="negative"):
        BudgetLimits(-1, Decimal(2))
    with pytest.raises(ValueError, match="negative"):
        BudgetLimits(1, Decimal("-0.01"))
