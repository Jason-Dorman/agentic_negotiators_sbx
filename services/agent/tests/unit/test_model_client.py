"""`AnthropicModelClient` against a local server that answers as the provider does (stage 3.1).

Every way a call can end is driven through a real HTTP exchange and must come out as exactly one
`ModelOutcome`, with the guard told what it used: a decision, a refusal, a truncation, an answer
that is not an envelope, a timeout the server causes by not answering, a 429, other 4xx and 5xx
statuses, a body that is not a message, a refused connection, a failed token count, and a call the
budget refuses before anything is sent. Nothing is retried, and nothing reaches the network.

The request itself is asserted field by field — the shape ADR-014 and ADR-015 fix — and so is what
the client does *not* send: no tools, no prefill, no fallbacks, no credential but the one its key
reference names.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import Iterator
from decimal import Decimal
from typing import Any

import anthropic
import pytest
from agent_validation_cases import STRUCTURAL, VALID_RESPONSES, Refusal
from fake_anthropic import (
    COUNT_TOKENS,
    MESSAGES,
    Answer,
    FakeAnthropic,
    json_answer,
    message,
    provider_error,
    serve,
    token_count,
    unused_base_url,
)
from pydantic import SecretStr, ValidationError

from agent.budget import BudgetGuard, BudgetLimits, ModelPriceTable, RefusalReason
from agent.logs import QUIET_LOGGERS, configure_logging
from agent.model import (
    AnthropicModelClient,
    DecisionEnvelope,
    ModelCallConfig,
    ModelOutcome,
    ModelResult,
    anthropic_sdk,
)

KEY = "sk-ant-api03-" + "k" * 40
SYSTEM_PROMPT = "SYSTEM-PROMPT-MARKER: you negotiate for the seller."
OBSERVATION = '{"OBSERVATION-MARKER": true}'
WALK_AWAY = '{"decision": {"action": "walk_away", "reason": "terms_unacceptable"}}'
SONNET = ModelPriceTable.load().price("claude-sonnet-5-5")
#: 1,200 input tokens at the dearest input rate, $4.00 a million, and 16,000 output at $10.00.
BOUND = Decimal("0.164800")


@pytest.fixture
def fake() -> FakeAnthropic:
    return FakeAnthropic()


@pytest.fixture
def base_url(fake: FakeAnthropic) -> Iterator[str]:
    with serve(fake) as url:
        yield url


def config(**overrides: Any) -> ModelCallConfig:
    values: dict[str, Any] = {
        "model_id": "claude-sonnet-5-5",
        "effort": "high",
        "max_tokens": 16_000,
        "timeout_s": 5.0,
    }
    return ModelCallConfig(**{**values, **overrides})


def guard(
    *, calls: int = 20, spend: str = "2.00", priced: bool = True, allow_unknown: bool = False
) -> BudgetGuard:
    return BudgetGuard(
        BudgetLimits(calls, Decimal(spend)),
        SONNET if priced else None,
        allow_unknown_price=allow_unknown,
    )


async def decide(
    url: str, budget: BudgetGuard | None = None, **overrides: Any
) -> tuple[ModelResult, BudgetGuard]:
    budget = budget or guard()
    client = AnthropicModelClient(
        anthropic_sdk(SecretStr(KEY), base_url=url), config(**overrides), budget
    )
    return await client.decide(SYSTEM_PROMPT, OBSERVATION, DecisionEnvelope), budget


# ---------------------------------------------------------------------------------------------
# A decision, and the request that asked for it
# ---------------------------------------------------------------------------------------------


async def test_a_decision_comes_back_as_written_with_its_usage_and_both_costs(
    fake: FakeAnthropic, base_url: str
) -> None:
    written = '{"decision":{"action":"offer","quote_amount_minor":"94000000"},"explanation":"x"}'
    fake.script(MESSAGES, message(written, input_tokens=200, cache_read=1_000, output_tokens=300))

    result, budget = await decide(base_url)

    assert result.outcome is ModelOutcome.DECIDED
    assert result.decision == json.loads(written)
    assert result.text == written
    assert result.stop_reason == "end_turn"
    assert result.model_id == result.served_model == "claude-sonnet-5-5"
    assert result.usage is not None
    assert result.usage.as_record() == {
        "input_tokens": 200,
        "output_tokens": 300,
        "cache_read_input_tokens": 1_000,
        "cache_creation_input_tokens": 0,
    }
    assert result.request_id == "req_fake_01"
    assert result.latency_ms is not None and result.latency_ms >= 0
    assert result.cost_estimated_usd == BOUND
    # 200 x $2.00 + 1,000 x $0.20 + 300 x $10.00, a million each: $0.0036.
    assert result.cost_reported_usd == Decimal("0.003600")
    assert result.provider_error is None and result.budget_refusal is None
    assert budget.calls == 1
    assert budget.spent_usd == Decimal("0.003600")


async def test_the_decision_keeps_the_models_own_key_order(
    fake: FakeAnthropic, base_url: str
) -> None:
    written = (
        '{"explanation": "x", "decision": {"quote_amount_minor": "94000000", "action": "offer"}}'
    )
    fake.script(MESSAGES, message(written))
    result, _ = await decide(base_url)
    assert result.decision is not None
    assert list(result.decision) == ["explanation", "decision"]
    assert list(result.decision["decision"]) == ["quote_amount_minor", "action"]


async def test_cache_writes_are_read_and_priced_at_their_own_durations(
    fake: FakeAnthropic, base_url: str
) -> None:
    fake.script(
        MESSAGES,
        message(
            WALK_AWAY, input_tokens=100, cache_write_5m=1_000, cache_write_1h=500, output_tokens=200
        ),
    )

    result, budget = await decide(base_url)

    assert result.usage is not None
    assert result.usage.cache_creation_input_tokens == 1_500
    assert result.usage.cache_creation_1h_input_tokens == 500
    # 100 x $2.00 + 1,000 x $2.50 + 500 x $4.00 + 200 x $10.00, a million each: $0.0067.
    assert result.cost_reported_usd == Decimal("0.006700")
    assert budget.spent_usd == Decimal("0.006700")


async def test_the_request_is_the_shape_adr_014_fixes_and_nothing_else(
    fake: FakeAnthropic, base_url: str
) -> None:
    await decide(base_url, effort="medium")

    [counted] = fake.requests_to(COUNT_TOKENS)
    [sent] = fake.requests_to(MESSAGES)
    assert [request.path for request in fake.received] == [COUNT_TOKENS, MESSAGES]
    assert sent.body == {
        "model": "claude-sonnet-5-5",
        "max_tokens": 16_000,
        "system": [{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": OBSERVATION}],
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": "medium",
            "format": {
                "type": "json_schema",
                "schema": anthropic.transform_schema(DecisionEnvelope),
            },
        },
    }
    # count_tokens is asked about exactly the request that is then sent.
    assert counted.body == {key: value for key, value in sent.body.items() if key != "max_tokens"}
    for request in (counted, sent):
        assert request.headers["x-api-key"] == KEY
        assert "authorization" not in request.headers
        # No beta header: server-side fallbacks are off (ADR-015), and nothing else needs one.
        assert "anthropic-beta" not in request.headers


def _walk(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


async def test_the_schema_sent_lets_the_provider_enforce_each_action_and_a_non_null_explanation(
    fake: FakeAnthropic, base_url: str
) -> None:
    """Written out literally, not compared with `transform_schema` of the same model: the
    enforcement ADR-014's as-built note describes must not be undone without a test failing."""
    await decide(base_url)

    [sent] = fake.requests_to(MESSAGES)
    schema = sent.body["output_config"]["format"]["schema"]
    shapes = schema["$defs"]
    assert schema["required"] == ["decision"]
    assert schema["properties"]["decision"] == {
        "anyOf": [
            {"$ref": "#/$defs/OfferDecision"},
            {"$ref": "#/$defs/AcceptDecision"},
            {"$ref": "#/$defs/WalkAwayDecision"},
        ],
        "title": "Decision",
    }
    for shape, action, field in (
        ("OfferDecision", "offer", "quote_amount_minor"),
        ("AcceptDecision", "accept", "offer_hash"),
        ("WalkAwayDecision", "walk_away", "reason"),
    ):
        assert shapes[shape]["properties"]["action"]["enum"] == [action]
        assert shapes[shape]["properties"]["action"]["type"] == "string"
        assert shapes[shape]["required"] == ["action", field]
    assert shapes["WalkAwayDecision"]["properties"]["reason"]["enum"] == [
        "terms_unacceptable",
        "inventory_constraint",
        "no_further_concession",
    ]
    explanation = schema["properties"]["explanation"]
    assert explanation["type"] == "string"
    assert "anyOf" not in explanation
    # The SDK demotes what the provider cannot enforce into the description as "{keyword: …}".
    assert "{default" not in json.dumps(schema)
    assert "{const" not in json.dumps(schema)
    for node in _walk(schema):
        assert "default" not in node
        assert "const" not in node
        assert node.get("type") != "null"
        if node.get("type") == "object":
            assert node["additionalProperties"] is False


async def test_the_key_and_the_url_come_from_the_reference_never_the_sdk_environment(
    fake: FakeAnthropic, base_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    other_key = "sk-ant-api03-" + "w" * 40
    monkeypatch.setenv("ANTHROPIC_API_KEY", other_key)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "the-wrong-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", unused_base_url())
    # The SDK reads this one whenever a client is built or copied, and sends it after the key.
    monkeypatch.setenv(
        "ANTHROPIC_CUSTOM_HEADERS",
        f"X-Api-Key: {other_key}\nAuthorization: Bearer the-wrong-token\n"
        "anthropic-beta: server-side-fallback-2026-07-01\nX-Injected: 1",
    )

    result, _ = await decide(base_url)

    assert result.outcome is ModelOutcome.DECIDED
    for sent in fake.received:
        assert sent.headers["x-api-key"] == KEY
        assert "authorization" not in sent.headers
        assert "anthropic-beta" not in sent.headers
        assert "x-injected" not in sent.headers
    assert anthropic_sdk(SecretStr(KEY)).base_url.host == "api.anthropic.com"


async def test_the_model_the_provider_says_answered_is_recorded_beside_the_one_asked_for(
    fake: FakeAnthropic, base_url: str
) -> None:
    fake.script(MESSAGES, message(WALK_AWAY, model="claude-opus-5"))
    result, _ = await decide(base_url)
    assert (result.model_id, result.served_model) == ("claude-sonnet-5-5", "claude-opus-5")


# ---------------------------------------------------------------------------------------------
# Answers that are not decisions
# ---------------------------------------------------------------------------------------------


async def test_a_refusal_is_its_own_outcome_and_still_costs_what_it_used(
    fake: FakeAnthropic, base_url: str
) -> None:
    refusal_details = {"stop_details": {"type": "refusal", "category": None, "explanation": None}}
    fake.script(
        MESSAGES, message(None, stop_reason="refusal", output_tokens=0, extra=refusal_details)
    )

    result, budget = await decide(base_url)

    assert result.outcome is ModelOutcome.REFUSAL
    assert result.decision is None and result.text is None
    assert result.stop_reason == "refusal"
    assert result.cost_reported_usd == Decimal("0.002400")
    assert budget.spent_usd == Decimal("0.002400")


async def test_a_refusal_after_partial_text_is_a_refusal_not_an_unparseable_answer(
    fake: FakeAnthropic, base_url: str
) -> None:
    fake.script(MESSAGES, message('{"decision": {"act', stop_reason="refusal"))
    result, _ = await decide(base_url)
    assert result.outcome is ModelOutcome.REFUSAL
    assert result.text == '{"decision": {"act'


async def test_an_answer_cut_off_at_max_tokens_is_its_own_outcome(
    fake: FakeAnthropic, base_url: str
) -> None:
    fake.script(MESSAGES, message('{"decision": {"action": "of', stop_reason="max_tokens"))

    result, _ = await decide(base_url)

    assert result.outcome is ModelOutcome.MAX_TOKENS
    assert result.stop_reason == "max_tokens"
    assert result.text == '{"decision": {"action": "of'
    assert result.decision is None
    assert result.cost_reported_usd is not None


@pytest.mark.parametrize(
    "text",
    [
        "I would offer 94 mUSD.",
        '{"decision": {"action": "offer", "quote_amount_minor": "94000000", "extra": 1}}',
        '{"decision": {"action": "offer", "quote_amount_minor": 94000000}}',
        '{"decision": {"action": "walk_away", "reason": "terms_unacceptable"}, '
        '"explanation": null}',
        '{"decision": {"action": "offer", "quote_amount_minor": "094"}}',
        '{"decision": {"action": "walk_away", "reason": "terms_unacceptable"}, "explanation": "'
        + "x" * 281
        + '"}',
        "",
        None,
    ],
    ids=[
        "prose",
        "extra field",
        "a number where a string belongs",
        "null explanation",
        "leading zero",
        "explanation too long",
        "empty text",
        "no text block",
    ],
)
async def test_an_answer_that_is_not_an_envelope_is_unparseable_and_kept_as_written(
    fake: FakeAnthropic, base_url: str, text: str | None
) -> None:
    fake.script(MESSAGES, message(text))

    result, budget = await decide(base_url)

    assert result.outcome is ModelOutcome.UNPARSEABLE
    assert result.decision is None
    assert result.text == (text or None)
    assert result.stop_reason == "end_turn"
    assert result.usage is not None and result.cost_reported_usd is not None
    assert budget.spent_usd == result.cost_reported_usd


# ---------------------------------------------------------------------------------------------
# Calls that fail
# ---------------------------------------------------------------------------------------------


async def test_a_call_the_server_does_not_answer_times_out_once_and_is_charged_its_estimate(
    fake: FakeAnthropic, base_url: str
) -> None:
    fake.script(MESSAGES, Answer(delay_s=None))

    result, budget = await decide(base_url, timeout_s=0.3)

    assert result.outcome is ModelOutcome.TIMEOUT
    assert len(fake.requests_to(MESSAGES)) == 1
    # The run's timeout, not the SDK's ten-minute default.
    assert result.latency_ms is not None and 300 <= result.latency_ms < 5_000
    assert result.usage is None and result.cost_reported_usd is None
    assert result.cost_estimated_usd == BOUND
    assert budget.calls == 1
    # No usage came back, and the provider may still bill the call: the ceiling counts its bound.
    assert budget.spent_usd == BOUND


@pytest.mark.parametrize(
    ("status", "error_type", "outcome"),
    [
        (429, "rate_limit_error", ModelOutcome.RATE_LIMITED),
        (400, "invalid_request_error", ModelOutcome.REJECTED),
        (401, "authentication_error", ModelOutcome.REJECTED),
        (403, "permission_error", ModelOutcome.REJECTED),
        (404, "not_found_error", ModelOutcome.REJECTED),
        (413, "request_too_large", ModelOutcome.REJECTED),
        (500, "api_error", ModelOutcome.PROVIDER_FAILURE),
        (503, "api_error", ModelOutcome.PROVIDER_FAILURE),
        (529, "overloaded_error", ModelOutcome.PROVIDER_FAILURE),
    ],
)
async def test_an_error_status_is_sorted_by_status_and_never_retried(
    fake: FakeAnthropic, base_url: str, status: int, error_type: str, outcome: ModelOutcome
) -> None:
    # The SDK would retry 429, 5xx and 529 by default; `x-should-retry` would force it to.
    answer = provider_error(status, error_type)
    fake.script(
        MESSAGES,
        Answer(
            answer.status,
            answer.body,
            headers={**answer.headers, "x-should-retry": "true", "retry-after": "0"},
        ),
    )

    result, budget = await decide(base_url)

    assert result.outcome is outcome
    assert len(fake.requests_to(MESSAGES)) == 1
    assert result.provider_error is not None
    assert (result.provider_error.status, result.provider_error.error_type) == (status, error_type)
    assert result.request_id == "req_fake_error"
    assert result.cost_reported_usd is None
    assert budget.spent_usd == BOUND


_BAD_ID = {"request-id": "req_fake_bad"}
_USAGE = {"input_tokens": 1_200, "output_tokens": 300}


@pytest.mark.parametrize(
    "answer",
    [
        Answer(200, b"<html>gateway</html>", content_type="text/html", headers=_BAD_ID),
        json_answer(200, {"type": "message", "content": "not a list"}, headers=_BAD_ID),
        json_answer(200, ["not", "a", "message"], headers=_BAD_ID),
        Answer(200, b'{"id": "msg_', headers=_BAD_ID),
        Answer(200, b"", headers=_BAD_ID),
        Answer(200, b'{"id": "\xff\xfe"}', headers=_BAD_ID),
        message(WALK_AWAY, request_id="req_fake_bad", extra={"content": "not a list"}),
        message(WALK_AWAY, request_id="req_fake_bad", extra={"content": None}),
        message(WALK_AWAY, request_id="req_fake_bad", extra={"content": ["text"]}),
        message(WALK_AWAY, request_id="req_fake_bad", extra={"content": [{"type": "text"}]}),
        message(
            WALK_AWAY, request_id="req_fake_bad", extra={"content": [{"type": "text", "text": 5}]}
        ),
        message(WALK_AWAY, request_id="req_fake_bad", extra={"content": [{"text": WALK_AWAY}]}),
        message(WALK_AWAY, request_id="req_fake_bad", extra={"stop_reason": 7}),
        message(WALK_AWAY, request_id="req_fake_bad", extra={"usage": {"output_tokens": 300}}),
        message(
            WALK_AWAY, request_id="req_fake_bad", extra={"usage": {**_USAGE, "output_tokens": -1}}
        ),
        message(
            WALK_AWAY, request_id="req_fake_bad", extra={"usage": {**_USAGE, "input_tokens": True}}
        ),
        message(
            WALK_AWAY,
            request_id="req_fake_bad",
            extra={"usage": {**_USAGE, "input_tokens": 10**30}},
        ),
        message(
            WALK_AWAY,
            request_id="req_fake_bad",
            extra={
                "usage": {
                    **_USAGE,
                    "cache_creation_input_tokens": 10,
                    "cache_creation": {"ephemeral_1h_input_tokens": 11},
                }
            },
        ),
    ],
    ids=[
        "an html page",
        "a message missing its fields",
        "a json list",
        "json cut off",
        "an empty body",
        "not utf-8",
        "content a string",
        "content null",
        "content a list of strings",
        "a text block with no text",
        "a text block whose text is a number",
        "a block with no type",
        "a stop reason that is a number",
        "usage with no input count",
        "a negative count",
        "a boolean count",
        "a count no model could produce",
        "more 1-hour cache writes than cache writes",
    ],
)
async def test_a_success_status_whose_body_is_not_a_message_is_a_provider_failure_and_charged(
    fake: FakeAnthropic, base_url: str, answer: Answer
) -> None:
    fake.script(MESSAGES, answer)

    result, budget = await decide(base_url)

    assert result.outcome is ModelOutcome.PROVIDER_FAILURE
    assert result.provider_error is not None and result.provider_error.status == 200
    assert result.usage is None and result.cost_reported_usd is None
    assert result.request_id == "req_fake_bad"
    assert result.cost_estimated_usd == BOUND
    assert result.latency_ms is not None and result.latency_ms >= 0
    assert budget.calls == 1
    assert budget.spent_usd == BOUND


async def test_a_redirect_is_not_followed_and_the_request_goes_nowhere_else(
    fake: FakeAnthropic, base_url: str
) -> None:
    fake.script(MESSAGES, Answer(307, b"", headers={"location": base_url + "/v1/elsewhere"}))

    result, budget = await decide(base_url)

    assert result.outcome is ModelOutcome.REJECTED
    assert result.provider_error is not None and result.provider_error.status == 307
    assert [request.path for request in fake.received] == [COUNT_TOKENS, MESSAGES]
    assert budget.spent_usd == BOUND


async def test_a_cancelled_call_is_charged_its_estimate_and_the_cancellation_propagates(
    fake: FakeAnthropic, base_url: str
) -> None:
    """How a deadline around `decide` will end a call: the call was sent and may be billed."""
    fake.script(MESSAGES, Answer(delay_s=None))
    budget = guard()
    client = AnthropicModelClient(
        anthropic_sdk(SecretStr(KEY), base_url=base_url), config(), budget
    )

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(client.decide(SYSTEM_PROMPT, OBSERVATION, DecisionEnvelope), 0.5)

    assert len(fake.requests_to(MESSAGES)) == 1
    assert budget.calls == 1
    assert budget.spent_usd == BOUND


async def test_the_client_turns_retries_off_whatever_sdk_client_it_is_given(
    fake: FakeAnthropic, base_url: str
) -> None:
    sdk = anthropic.AsyncAnthropic(api_key=KEY, base_url=base_url, max_retries=3)
    answer = provider_error(529, "overloaded_error")
    fake.script(
        MESSAGES,
        Answer(answer.status, answer.body, headers={"x-should-retry": "true", "retry-after": "0"}),
    )
    client = AnthropicModelClient(sdk, config(), guard())

    result = await client.decide(SYSTEM_PROMPT, OBSERVATION, DecisionEnvelope)

    assert result.outcome is ModelOutcome.PROVIDER_FAILURE
    assert len(fake.requests_to(MESSAGES)) == 1


async def test_a_provider_nothing_listens_for_is_a_connection_failure_and_admits_nothing() -> None:
    result, budget = await decide(unused_base_url())
    assert result.outcome is ModelOutcome.CONNECTION_FAILED
    assert result.cost_estimated_usd is None
    assert budget.calls == 0
    assert budget.spent_usd == 0


@pytest.mark.parametrize(
    ("answer", "outcome"),
    [
        (provider_error(500, "api_error"), ModelOutcome.PROVIDER_FAILURE),
        (provider_error(429, "rate_limit_error"), ModelOutcome.RATE_LIMITED),
        (Answer(delay_s=None), ModelOutcome.TIMEOUT),
        (json_answer(200, {"input_tokens": "1200"}), ModelOutcome.PROVIDER_FAILURE),
        (json_answer(200, {"input_tokens": -1}), ModelOutcome.PROVIDER_FAILURE),
        (json_answer(200, {"input_tokens": True}), ModelOutcome.PROVIDER_FAILURE),
        (json_answer(200, {"input_tokens": 10**30}), ModelOutcome.PROVIDER_FAILURE),
        (Answer(200, b'{"input_tokens": '), ModelOutcome.PROVIDER_FAILURE),
    ],
    ids=[
        "500",
        "429",
        "no answer",
        "a count that is not a count",
        "a negative count",
        "a boolean count",
        "a count no model could produce",
        "json cut off",
    ],
)
async def test_a_failed_token_count_sends_no_call_and_admits_nothing(
    fake: FakeAnthropic, base_url: str, answer: Answer, outcome: ModelOutcome
) -> None:
    fake.script(COUNT_TOKENS, answer)

    result, budget = await decide(base_url, timeout_s=0.3)

    assert result.outcome is outcome
    assert fake.requests_to(MESSAGES) == []
    assert budget.calls == 0


# ---------------------------------------------------------------------------------------------
# The budget, before anything is sent
# ---------------------------------------------------------------------------------------------


async def test_a_call_whose_bound_would_cross_the_spend_ceiling_is_never_sent(
    fake: FakeAnthropic, base_url: str
) -> None:
    result, budget = await decide(base_url, guard(spend="0.164799"))

    assert result.outcome is ModelOutcome.BUDGET_REFUSED
    assert result.budget_refusal is not None
    assert result.budget_refusal.reason is RefusalReason.SPEND_CEILING
    assert result.budget_refusal.estimated_usd == BOUND
    assert result.cost_estimated_usd is None
    assert len(fake.requests_to(COUNT_TOKENS)) == 1
    assert fake.requests_to(MESSAGES) == []
    assert budget.calls == 0


async def test_the_call_ceiling_refuses_the_call_after_the_last_one_allowed(
    fake: FakeAnthropic, base_url: str
) -> None:
    budget = guard(calls=1)
    first, _ = await decide(base_url, budget)
    second, _ = await decide(base_url, budget)

    assert first.outcome is ModelOutcome.DECIDED
    assert second.outcome is ModelOutcome.BUDGET_REFUSED
    assert second.budget_refusal is not None
    assert second.budget_refusal.reason is RefusalReason.CALL_CEILING
    assert len(fake.requests_to(MESSAGES)) == 1


async def test_a_model_with_no_price_is_refused_unless_unknown_prices_are_allowed(
    fake: FakeAnthropic, base_url: str
) -> None:
    refused, _ = await decide(base_url, guard(priced=False))
    assert refused.outcome is ModelOutcome.BUDGET_REFUSED
    assert refused.budget_refusal is not None
    assert refused.budget_refusal.reason is RefusalReason.UNKNOWN_PRICE
    assert fake.requests_to(MESSAGES) == []

    allowed, budget = await decide(base_url, guard(priced=False, allow_unknown=True))
    assert allowed.outcome is ModelOutcome.DECIDED
    assert allowed.usage is not None
    # Unknown is never zero: neither cost is a number, and the spend is not known either.
    assert allowed.cost_estimated_usd is None and allowed.cost_reported_usd is None
    assert budget.spent_usd is None
    assert budget.calls == 1


# ---------------------------------------------------------------------------------------------
# Nothing private reaches the logs
# ---------------------------------------------------------------------------------------------


async def test_at_debug_level_no_log_line_carries_the_key_the_prompt_or_the_answer(
    fake: FakeAnthropic, base_url: str
) -> None:
    stream = io.StringIO()
    # ANTHROPIC_LOG=debug would have set these at import; configure_logging must override it.
    for name in ("anthropic", "httpx2"):
        logging.getLogger(name).setLevel(logging.DEBUG)
    configure_logging(level="DEBUG", instance="agent-b", role="seller", stream=stream)
    fake.script(MESSAGES, message(WALK_AWAY), provider_error(400, "invalid_request_error"))

    await decide(base_url)
    await decide(base_url)
    logging.getLogger("agent.test").debug("a line, to show the handler is live")

    output = stream.getvalue()
    assert "a line, to show the handler is live" in output
    for private in (KEY, "SYSTEM-PROMPT-MARKER", "OBSERVATION-MARKER", "terms_unacceptable"):
        assert private not in output


def test_every_logger_under_the_sdk_is_held_at_warning_at_debug_level() -> None:
    configure_logging(level="DEBUG", instance="agent-b", role="seller", stream=io.StringIO())
    assert set(QUIET_LOGGERS) >= {"anthropic", "httpx2", "httpcore2"}
    for name in QUIET_LOGGERS:
        assert logging.getLogger(name).getEffectiveLevel() == logging.WARNING


def test_a_result_repr_shows_neither_the_answer_nor_the_provider_message() -> None:
    from agent.model import ProviderError

    result = ModelResult(
        ModelOutcome.PROVIDER_FAILURE,
        "claude-sonnet-5-5",
        decision={"decision": {"action": "walk_away"}},
        text="ANSWER-MARKER",
        provider_error=ProviderError(400, "invalid_request_error", "MESSAGE-MARKER"),
    )
    assert "ANSWER-MARKER" not in repr(result)
    assert "MESSAGE-MARKER" not in repr(result)
    assert "walk_away" not in repr(result)


# ---------------------------------------------------------------------------------------------
# The envelope requested agrees with the decision schema
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("response", VALID_RESPONSES)
def test_every_response_the_schema_admits_the_envelope_admits(response: Any) -> None:
    DecisionEnvelope.model_validate_json(json.dumps(response))


@pytest.mark.parametrize("case", STRUCTURAL, ids=lambda case: case.name)
def test_every_structural_refusal_the_schema_makes_the_envelope_makes(case: Refusal) -> None:
    """The envelope is the requested shape, so it may admit what the validator refuses — the
    validator is the authority — but it must not admit what the decision schema refuses."""
    if not case.schema_agrees:
        pytest.skip("the validator is knowingly stricter than the schema here")
    with pytest.raises(ValidationError):
        DecisionEnvelope.model_validate_json(json.dumps(case.response))


def test_token_count_answers_are_what_the_fake_sends_by_default() -> None:
    """A guard on the fixture itself: the bound every test above relies on."""
    assert json.loads(token_count(1_200).body) == {"input_tokens": 1_200}
    assert SONNET is not None
    assert SONNET.bound_usd(1_200, 16_000) == BOUND


# ---------------------------------------------------------------------------------------------
# Stage 3.2: a repair's feedback block, and the turn's deadline over the whole call (ADR-089)
# ---------------------------------------------------------------------------------------------

REPAIR = "REPAIR-MARKER: your previous answer was refused."


async def test_a_repair_is_a_second_text_block_after_the_observation_in_both_requests(
    fake: FakeAnthropic, base_url: str
) -> None:
    client = AnthropicModelClient(
        anthropic_sdk(SecretStr(KEY), base_url=base_url), config(), guard()
    )
    await client.decide(SYSTEM_PROMPT, OBSERVATION, DecisionEnvelope, repair=REPAIR)

    for request in fake.received:
        assert request.body["messages"] == [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": OBSERVATION},
                    {"type": "text", "text": REPAIR},
                ],
            }
        ]
        # The system prompt and its cache breakpoint are unchanged by a repair.
        assert request.body["system"] == [
            {"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}
        ]


async def test_a_first_attempt_sends_the_observation_alone(
    fake: FakeAnthropic, base_url: str
) -> None:
    await decide(base_url)
    for request in fake.received:
        assert request.body["messages"] == [{"role": "user", "content": OBSERVATION}]


async def test_a_call_unanswered_by_the_deadline_is_a_timeout_charged_its_estimate(
    fake: FakeAnthropic, base_url: str
) -> None:
    fake.script(MESSAGES, Answer(delay_s=None))
    budget = guard()
    client = AnthropicModelClient(
        anthropic_sdk(SecretStr(KEY), base_url=base_url), config(timeout_s=30.0), budget
    )

    result = await client.decide(SYSTEM_PROMPT, OBSERVATION, DecisionEnvelope, within_s=0.5)

    assert result.outcome is ModelOutcome.TIMEOUT
    assert result.sent
    assert result.cost_estimated_usd == BOUND
    assert result.cost_reported_usd is None
    assert result.usage is None
    assert result.latency_ms is not None and 400 <= result.latency_ms < 5_000
    assert len(fake.requests_to(MESSAGES)) == 1
    assert budget.calls == 1
    assert budget.spent_usd == BOUND


async def test_a_deadline_reached_while_counting_tokens_sends_and_charges_nothing(
    fake: FakeAnthropic, base_url: str
) -> None:
    fake.script(COUNT_TOKENS, Answer(delay_s=None))
    budget = guard()
    client = AnthropicModelClient(
        anthropic_sdk(SecretStr(KEY), base_url=base_url), config(timeout_s=30.0), budget
    )

    result = await client.decide(SYSTEM_PROMPT, OBSERVATION, DecisionEnvelope, within_s=0.5)

    assert result.outcome is ModelOutcome.TIMEOUT
    assert not result.sent
    assert result.cost_estimated_usd is None
    assert fake.requests_to(MESSAGES) == []
    assert budget.calls == 0
    assert budget.spent_usd == 0


async def test_no_time_left_sends_nothing(fake: FakeAnthropic, base_url: str) -> None:
    budget = guard()
    client = AnthropicModelClient(
        anthropic_sdk(SecretStr(KEY), base_url=base_url), config(), budget
    )
    result = await client.decide(SYSTEM_PROMPT, OBSERVATION, DecisionEnvelope, within_s=-1.0)
    assert result.outcome is ModelOutcome.TIMEOUT
    assert not result.sent
    assert fake.requests_to(MESSAGES) == []
    assert budget.calls == 0


async def test_a_deadline_with_time_to_spare_changes_nothing(
    fake: FakeAnthropic, base_url: str
) -> None:
    client = AnthropicModelClient(
        anthropic_sdk(SecretStr(KEY), base_url=base_url), config(), guard()
    )
    result = await client.decide(SYSTEM_PROMPT, OBSERVATION, DecisionEnvelope, within_s=30.0)
    assert result.outcome is ModelOutcome.DECIDED
    assert result.sent


async def test_a_cancellation_from_outside_still_propagates_under_a_deadline(
    fake: FakeAnthropic, base_url: str
) -> None:
    """Only the deadline's own expiry becomes a timeout; any other cancellation is not swallowed."""
    fake.script(MESSAGES, Answer(delay_s=None))
    budget = guard()
    client = AnthropicModelClient(
        anthropic_sdk(SecretStr(KEY), base_url=base_url), config(timeout_s=30.0), budget
    )
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            client.decide(SYSTEM_PROMPT, OBSERVATION, DecisionEnvelope, within_s=30.0), 0.5
        )
    assert budget.spent_usd == BOUND


@pytest.mark.parametrize(
    ("answer", "sent"),
    [
        (provider_error(529, "overloaded_error"), True),
        (Answer(200, b"not json"), True),
    ],
    ids=["provider_failure", "malformed"],
)
async def test_a_failed_admitted_call_says_it_was_sent(
    fake: FakeAnthropic, base_url: str, answer: Answer, sent: bool
) -> None:
    fake.script(MESSAGES, answer)
    result, _ = await decide(base_url)
    assert result.sent is sent


async def test_a_failed_token_count_says_nothing_was_sent(
    fake: FakeAnthropic, base_url: str
) -> None:
    fake.script(COUNT_TOKENS, provider_error(500, "api_error"))
    result, _ = await decide(base_url)
    assert result.outcome is ModelOutcome.PROVIDER_FAILURE
    assert not result.sent


@pytest.mark.parametrize(
    "answer",
    [Answer(200, b"not json"), json_answer(200, {"input_tokens": -1})],
    ids=["not_json", "impossible_count"],
)
async def test_a_malformed_token_count_sends_and_charges_nothing(
    fake: FakeAnthropic, base_url: str, answer: Answer
) -> None:
    fake.script(COUNT_TOKENS, answer)
    result, budget = await decide(base_url)
    assert result.outcome is ModelOutcome.PROVIDER_FAILURE
    assert not result.sent
    assert fake.requests_to(MESSAGES) == []
    assert budget.calls == 0


class _RaisingSdk:
    """An SDK whose token count raises a builtin TimeoutError that is not the deadline's."""

    def with_options(self, **kwargs: Any) -> _RaisingSdk:
        return self

    @property
    def messages(self) -> _RaisingSdk:
        return self

    async def count_tokens(self, **kwargs: Any) -> Any:
        raise TimeoutError("not the deadline")


async def test_a_timeout_error_that_is_not_the_deadlines_propagates() -> None:
    """Only the deadline's own expiry becomes a `timeout` outcome (ADR-089)."""
    client = AnthropicModelClient(_RaisingSdk(), config(), guard())  # type: ignore[arg-type]  # reason: a stand-in for the SDK
    with pytest.raises(TimeoutError, match="not the deadline"):
        await client.decide(SYSTEM_PROMPT, OBSERVATION, DecisionEnvelope, within_s=30.0)
