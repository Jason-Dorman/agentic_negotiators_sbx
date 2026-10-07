"""The fixture model client (Q73, ADR-088): canned answers through the run's real budget guard."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from agent_observations import alternating_offers, observation

from agent.budget import BudgetGuard, BudgetLimits, ModelPriceTable, RefusalReason, TokenUsage
from agent.model import (
    DecisionEnvelope,
    FixtureError,
    FixtureModelClient,
    FixtureScript,
    ModelCallConfig,
    ModelOutcome,
    load_fixture_scripts,
)

REPOSITORY = Path(__file__).resolve().parents[4]
FIXTURES = REPOSITORY / "tests" / "fixtures" / "model_responses"
SONNET = ModelPriceTable.load().price("claude-sonnet-5-5")
CONFIG = ModelCallConfig("claude-sonnet-5-5", "high", 16_000, 45.0)
USAGE = {"input_tokens": 600, "output_tokens": 200, "cache_creation_input_tokens": 2000}
#: The seller facing the buyer's opening offer, so `active_offer` is set.
SELLER_TURN = json.dumps(observation("seller", history=alternating_offers(80_000_000)))


def script(*responses: dict[str, Any]) -> FixtureScript:
    return FixtureScript.model_validate(
        {"fixture_version": "1", "description": "test", "responses": list(responses)}
    )


def guard(spend: str = "2.00", calls: int = 20) -> BudgetGuard:
    return BudgetGuard(BudgetLimits(calls, Decimal(spend)), SONNET, allow_unknown_price=False)


async def play(client: FixtureModelClient, **kwargs: Any) -> Any:
    return await client.decide("system", SELLER_TURN, DecisionEnvelope, **kwargs)


@pytest.mark.parametrize(
    "directory", sorted(path.name for path in FIXTURES.iterdir() if path.is_dir())
)
def test_every_committed_fixture_set_loads(directory: str) -> None:
    scripts = load_fixture_scripts(FIXTURES / directory)
    assert set(scripts) == {"buyer", "seller"}
    assert all(script.responses for script in scripts.values())


async def test_an_answer_is_played_sorted_and_priced_as_a_real_one() -> None:
    budget = guard()
    envelope = {"decision": {"action": "offer", "quote_amount_minor": "95000000"}}
    client = FixtureModelClient(script({"answer": envelope, "usage": USAGE}), CONFIG, budget)

    result = await play(client)

    assert result.outcome is ModelOutcome.DECIDED
    assert result.decision == envelope
    assert result.sent
    assert result.stop_reason == "end_turn"
    assert result.served_model == "claude-sonnet-5-5"
    usage = TokenUsage(600, 200, 0, 2000)
    assert result.usage == usage
    assert SONNET is not None
    # The guard priced the prompt the usage describes, all 2,600 tokens, before the call.
    assert result.cost_estimated_usd == SONNET.bound_usd(2_600, 16_000)
    assert result.cost_reported_usd == SONNET.cost_usd(usage)
    assert budget.calls == 1
    assert budget.spent_usd == SONNET.cost_usd(usage)


async def test_an_accept_names_the_active_offer_filled_from_the_observation() -> None:
    envelope = {"decision": {"action": "accept", "offer_hash": "{{active_offer.offer_hash}}"}}
    client = FixtureModelClient(script({"answer": envelope, "usage": USAGE}), CONFIG, guard())
    result = await play(client)
    active = json.loads(SELLER_TURN)["active_offer"]["offer_hash"]
    assert result.decision == {"decision": {"action": "accept", "offer_hash": active}}


async def test_a_path_the_observation_lacks_is_a_fixture_error_not_a_move() -> None:
    envelope = {"decision": {"action": "accept", "offer_hash": "{{active_offer.no_such}}"}}
    client = FixtureModelClient(script({"answer": envelope, "usage": USAGE}), CONFIG, guard())
    result = await play(client)
    assert result.outcome is ModelOutcome.PROVIDER_FAILURE
    assert result.provider_error is not None
    assert result.provider_error.error_type == "fixture_error"


async def test_entries_are_played_in_order_one_per_call() -> None:
    first = {"decision": {"action": "offer", "quote_amount_minor": "85000000"}}
    second = {"decision": {"action": "offer", "quote_amount_minor": "95000000"}}
    client = FixtureModelClient(
        script({"answer": first, "usage": USAGE}, {"answer": second, "usage": USAGE}),
        CONFIG,
        guard(),
    )
    assert (await play(client)).decision == first
    assert (await play(client, repair="refused")).decision == second
    assert client.calls_played == 2


async def test_text_and_stop_reasons_are_judged_as_a_real_answer_is() -> None:
    client = FixtureModelClient(
        script(
            {"text": "I would rather not.", "usage": USAGE},
            {"text": "", "stop_reason": "refusal", "usage": USAGE},
            {"text": '{"decision": {"act', "stop_reason": "max_tokens", "usage": USAGE},
        ),
        CONFIG,
        guard(),
    )
    unparseable, refusal, truncated = [await play(client) for _ in range(3)]
    assert unparseable.outcome is ModelOutcome.UNPARSEABLE
    assert unparseable.text == "I would rather not."
    assert refusal.outcome is ModelOutcome.REFUSAL
    assert refusal.text is None
    assert truncated.outcome is ModelOutcome.MAX_TOKENS


@pytest.mark.parametrize(
    ("failure", "outcome", "status"),
    [
        ("timeout", ModelOutcome.TIMEOUT, None),
        ("rate_limited", ModelOutcome.RATE_LIMITED, 429),
        ("rejected", ModelOutcome.REJECTED, 400),
        ("provider_failure", ModelOutcome.PROVIDER_FAILURE, 529),
        ("connection_failed", ModelOutcome.CONNECTION_FAILED, None),
    ],
)
async def test_a_failure_is_sent_and_charged_its_estimate(
    failure: str, outcome: ModelOutcome, status: int | None
) -> None:
    budget = guard()
    client = FixtureModelClient(script({"failure": failure, "input_tokens": 2_600}), CONFIG, budget)
    result = await play(client)
    assert result.outcome is outcome
    assert result.sent
    assert result.provider_error is not None
    assert result.provider_error.status == status
    assert SONNET is not None
    assert result.cost_estimated_usd == SONNET.bound_usd(2_600, 16_000)
    assert result.cost_reported_usd is None
    assert budget.spent_usd == result.cost_estimated_usd


async def test_the_guard_refuses_what_it_would_refuse_live() -> None:
    envelope = {"decision": {"action": "offer", "quote_amount_minor": "95000000"}}
    budget = guard(spend="0.10")
    client = FixtureModelClient(script({"answer": envelope, "usage": USAGE}), CONFIG, budget)
    result = await play(client)
    assert result.outcome is ModelOutcome.BUDGET_REFUSED
    assert result.budget_refusal is not None
    assert result.budget_refusal.reason is RefusalReason.SPEND_CEILING
    assert not result.sent
    assert budget.calls == 0


async def test_a_held_back_answer_runs_into_the_deadline() -> None:
    envelope = {"decision": {"action": "offer", "quote_amount_minor": "95000000"}}
    budget = guard()
    client = FixtureModelClient(
        script({"answer": envelope, "usage": USAGE, "delay_s": 5}), CONFIG, budget
    )
    result = await play(client, within_s=0.05)
    assert result.outcome is ModelOutcome.TIMEOUT
    assert result.sent
    assert budget.spent_usd == result.cost_estimated_usd


async def test_a_script_that_runs_out_fails_and_sends_nothing() -> None:
    budget = guard()
    client = FixtureModelClient(script(), CONFIG, budget)
    result = await play(client)
    assert result.outcome is ModelOutcome.PROVIDER_FAILURE
    assert not result.sent
    assert result.provider_error is not None
    assert result.provider_error.error_type == "fixture_exhausted"
    assert budget.calls == 0


@pytest.mark.parametrize(
    ("entry", "location"),
    [
        ({"answer": {}, "text": "x", "usage": USAGE}, "responses.0"),
        ({"answer": {}}, "responses.0"),
        ({}, "responses.0"),
        ({"failure": "exploded"}, "responses.0.failure"),
        ({"text": "x", "usage": {"input_tokens": -1, "output_tokens": 0}}, "responses.0.usage"),
        ({"text": "x", "usage": USAGE, "mood": "sunny"}, "responses.0.mood"),
    ],
)
def test_a_malformed_script_is_refused_by_location(
    tmp_path: Path, entry: dict[str, Any], location: str
) -> None:
    document = {"fixture_version": "1", "description": "bad", "responses": [entry]}
    (tmp_path / "buyer.json").write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(FixtureError, match=location.replace(".", r"\.")):
        FixtureScript.load(tmp_path / "buyer.json")


def test_a_directory_missing_a_role_is_refused(tmp_path: Path) -> None:
    good = {"fixture_version": "1", "description": "ok", "responses": []}
    (tmp_path / "buyer.json").write_text(json.dumps(good), encoding="utf-8")
    with pytest.raises(FixtureError, match=r"seller\.json"):
        load_fixture_scripts(tmp_path)


def test_a_missing_directory_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FixtureError, match="does not exist"):
        load_fixture_scripts(tmp_path / "nowhere")


# --------------------------------------------------------------------------------------
# What the adversarial review of stage 3.2 found
# --------------------------------------------------------------------------------------

ENVELOPE = {"decision": {"action": "offer", "quote_amount_minor": "95000000"}}


@pytest.mark.parametrize(
    ("timeout_s", "within_s", "message"),
    [
        (0.05, None, "no answer within the timeout"),
        (0.05, 30.0, "no answer within the timeout"),
        (45.0, 0.05, "no answer before the turn's deadline"),
    ],
    ids=["timeout_alone", "timeout_first", "deadline_first"],
)
async def test_a_held_back_answer_is_bounded_like_a_live_call(
    timeout_s: float, within_s: float | None, message: str
) -> None:
    """The run's `model_timeout_s` bounds a canned answer as it bounds a live one."""
    budget = guard()
    config = ModelCallConfig("claude-sonnet-5-5", "high", 16_000, timeout_s)
    client = FixtureModelClient(
        script({"answer": ENVELOPE, "usage": USAGE, "delay_s": 5}), config, budget
    )
    result = await play(client, within_s=within_s)
    assert result.outcome is ModelOutcome.TIMEOUT
    assert result.provider_error is not None
    assert result.provider_error.message == message
    assert result.sent
    assert budget.spent_usd == result.cost_estimated_usd


async def test_a_cancelled_canned_call_is_charged_its_estimate() -> None:
    import asyncio

    budget = guard()
    client = FixtureModelClient(
        script({"answer": ENVELOPE, "usage": USAGE, "delay_s": 5}), CONFIG, budget
    )
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(play(client), 0.05)
    assert SONNET is not None
    assert budget.calls == 1
    assert budget.spent_usd == SONNET.bound_usd(2_600, 16_000)


async def test_a_fill_error_is_charged_its_estimate() -> None:
    budget = guard()
    envelope = {"decision": {"action": "accept", "offer_hash": "{{no.such.path}}"}}
    client = FixtureModelClient(script({"answer": envelope, "usage": USAGE}), CONFIG, budget)
    result = await play(client)
    assert result.outcome is ModelOutcome.PROVIDER_FAILURE
    assert budget.spent_usd == result.cost_estimated_usd is not None


@pytest.mark.parametrize(
    ("failure", "error_type"),
    [
        ("rate_limited", "rate_limit_error"),
        ("rejected", "invalid_request_error"),
        ("provider_failure", "overloaded_error"),
        ("timeout", None),
        ("connection_failed", None),
    ],
)
async def test_a_canned_failure_carries_the_providers_error_type(
    failure: str, error_type: str | None
) -> None:
    client = FixtureModelClient(script({"failure": failure}), CONFIG, guard())
    result = await play(client)
    assert result.provider_error is not None
    assert result.provider_error.error_type == error_type


async def test_a_path_is_filled_inside_a_list_too() -> None:
    envelope = {**ENVELOPE, "explanation": ["{{active_offer.offer_hash}}", "kept"]}
    client = FixtureModelClient(script({"answer": envelope, "usage": USAGE}), CONFIG, guard())
    result = await play(client)
    active = json.loads(SELLER_TURN)["active_offer"]["offer_hash"]
    assert result.text is not None
    assert json.loads(result.text)["explanation"] == [active, "kept"]
