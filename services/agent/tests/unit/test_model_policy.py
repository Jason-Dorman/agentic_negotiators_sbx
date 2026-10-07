"""`ModelPolicy` through the turn executor, with a fake `ModelClient` (test_strategy section 6).

The cases the exit condition of build_plan stage 3.2 names: a valid first attempt; an invalid one
then a valid repair; invalid twice; a timeout; a `refusal` stop reason; a `max_tokens` stop reason;
a provider error — and the repair message holding only this agent's own feedback. Then what ADR-089
adds: no repair after a timeout or a provider fault, no record of a call that was never sent, the
budget ending the turn `budget_exhausted`, and the turn's deadline bounding every call.

Each turn runs through the real `TurnExecutor`, `MandateValidator` and signer, so "refused" and
"signed" mean what they mean in production.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from agent_fakes import FIXTURE_KEYS, FakeModelClient, KeyedKeyHolder, KeyedRunSigner
from agent_observations import MANDATES, alternating_offers, fixture_approval, observation, typed

from agent.budget import BudgetRefusal, RefusalReason, TokenUsage
from agent.keys import KeyDerivation
from agent.model import DecisionEnvelope, ModelOutcome, ModelResult, ProviderError, sort_answer
from agent.observation import Role
from agent.policy import MODEL_VERSION, ModelPolicy, observation_message
from agent.policy.model import MAX_JSON_DEPTH
from agent.prompting import PromptTemplate
from agent.turns import RESPONSE_MARGIN_S, TurnExecutor, TurnOutcome
from agent.validation import MandateValidator

TEMPLATE = PromptTemplate.load(DecisionEnvelope)
MODEL = "claude-sonnet-5-5"
USAGE = TokenUsage(
    input_tokens=40, output_tokens=120, cache_read_input_tokens=1500, cache_creation_input_tokens=0
)
ESTIMATE = Decimal("0.163080")
REPORTED = Decimal("0.001580")
START = datetime(2026, 9, 30, 12, 0, 0, tzinfo=UTC)
RUN = UUID("6f1c2a4e-9d7b-4c3a-8e2f-1a2b3c4d5e6f")

#: The seller's turn after the buyer opened at 80: the A04 position, the seller's floor 90.
SELLER_TURN = observation("seller", history=alternating_offers(80_000_000))


class Clock:
    """Wall time that moves only when a test moves it; monotonic time 40 ms per read."""

    def __init__(self, now: datetime = START) -> None:
        self.moment = now
        self._mono = 0.0

    def now(self) -> datetime:
        return self.moment

    def monotonic(self) -> float:
        self._mono += 0.040
        return self._mono


def answer(
    content: dict[str, Any] | str | None,
    *,
    stop_reason: str = "end_turn",
    usage: TokenUsage | None = USAGE,
) -> ModelResult:
    """A call that came back with text, sorted exactly as the real client sorts it."""
    text = content if isinstance(content, str) or content is None else json.dumps(content)
    outcome, decision = sort_answer(stop_reason, text, DecisionEnvelope)
    return ModelResult(
        outcome,
        MODEL,
        served_model=MODEL,
        decision=decision,
        text=text,
        stop_reason=stop_reason,
        usage=usage,
        cost_estimated_usd=ESTIMATE,
        cost_reported_usd=REPORTED,
        request_id="req_answer",
        latency_ms=900,
        sent=True,
    )


def failed(
    outcome: ModelOutcome,
    *,
    sent: bool = True,
    status: int | None = None,
    error_type: str | None = None,
    message: str = "the provider's own words, private",
) -> ModelResult:
    return ModelResult(
        outcome,
        MODEL,
        cost_estimated_usd=ESTIMATE if sent else None,
        request_id="req_failed" if sent else None,
        latency_ms=45_000 if sent else None,
        provider_error=ProviderError(status, error_type, message),
        sent=sent,
    )


def budget_refused() -> ModelResult:
    refusal = BudgetRefusal(
        RefusalReason.SPEND_CEILING,
        "USD 1.900000 spent and USD 0.163080 at most for this call would cross the USD 2.00 "
        "ceiling",
        ESTIMATE,
    )
    return ModelResult(ModelOutcome.BUDGET_REFUSED, MODEL, budget_refusal=refusal)


def offer(amount: str, explanation: str | None = None) -> dict[str, Any]:
    envelope: dict[str, Any] = {"decision": {"action": "offer", "quote_amount_minor": amount}}
    if explanation is not None:
        envelope["explanation"] = explanation
    return envelope


def policy_for(client: FakeModelClient, role: Role = "seller") -> ModelPolicy:
    return ModelPolicy(client, TEMPLATE, role=role, instructions=MANDATES[role]["instructions"])


async def turn(
    client: FakeModelClient,
    document: dict[str, Any] = SELLER_TURN,
    *,
    attempts: int = 2,
    deadline: datetime | None = START + timedelta(seconds=105),
    clock: Clock | None = None,
) -> tuple[TurnOutcome, KeyedRunSigner]:
    role = document["role"]
    key = KeyedRunSigner(FIXTURE_KEYS[role], KeyDerivation(chain_id=31337, role=role, run_id=RUN))
    outcome = await TurnExecutor(MandateValidator(), clock or Clock()).run(
        turn=2,
        observation=typed(document),
        observation_hash="0x" + "cd" * 32,
        policy=policy_for(client, role),
        attempts=attempts,
        approval=fixture_approval(),
        signer=key,
        still_open=lambda: True,
        deadline=deadline,
    )
    return outcome, key


# --------------------------------------------------------------------------------------
# The exit condition's cases
# --------------------------------------------------------------------------------------


async def test_a_valid_first_answer_is_signed_and_recorded_with_its_accounting() -> None:
    client = FakeModelClient(answer(offer("96000000", "Opening above my floor.")))
    outcome, key = await turn(client)

    assert outcome.status == "signed"
    assert outcome.failure is None
    assert outcome.signed_action is not None
    assert outcome.signed_action.typed_message["quoteAmount"] == "96000000"
    assert key.signatures_made == 1
    assert len(client.calls) == 1
    [record] = outcome.decisions
    payload = record.to_json()
    assert payload["raw_response"] == offer("96000000", "Opening above my floor.")
    assert payload["validation"] == {"ok": True, "code": None, "feedback": None}
    assert payload["stop_reason"] == "end_turn"
    assert payload["usage"] == {
        "input_tokens": 40,
        "output_tokens": 120,
        "cache_read_input_tokens": 1500,
        "cache_creation_input_tokens": 0,
    }
    assert payload["cost_estimated_usd"] == "0.163080"
    assert payload["cost_reported_usd"] == "0.001580"
    assert payload["prompt_template_version"] == TEMPLATE.version


async def test_an_invalid_answer_is_repaired_once_with_only_its_own_feedback() -> None:
    client = FakeModelClient(answer(offer("85000000")), answer(offer("95000000")))
    outcome, _ = await turn(client)

    assert outcome.status == "signed"
    # The repaired price is signed exactly as the model proposed it, never clamped.
    assert outcome.signed_action is not None
    assert outcome.signed_action.typed_message["quoteAmount"] == "95000000"
    first, second = outcome.decisions
    assert first.validation.code == "below_reservation"
    assert second.validation.ok

    one, two = client.calls
    assert one.repair is None
    feedback = first.validation.feedback
    assert feedback is not None
    # The repair block is the template's text around this agent's own code and feedback, and
    # nothing else: the system prompt and the observation are sent unchanged.
    assert two.repair == TEMPLATE.repair_message("below_reservation", feedback)
    assert two.system_prompt == one.system_prompt
    assert two.observation == one.observation


async def test_two_invalid_answers_fail_the_turn_and_sign_nothing() -> None:
    """A04 at the agent: a seller's model proposing 85 against a floor of 90, twice."""
    client = FakeModelClient(answer(offer("85000000")), answer(offer("85000000")))
    outcome, key = await turn(client)

    assert outcome.status == "model_failed"
    assert outcome.signed_action is None
    assert key.signatures_made == 0
    assert outcome.failure == {
        "code": "repair_exhausted",
        "detail": "none of 2 attempt(s) passed validation",
    }
    assert [record.validation.code for record in outcome.decisions] == [
        "below_reservation",
        "below_reservation",
    ]
    assert len(client.calls) == 2


async def test_a_timeout_fails_the_turn_at_once_without_a_repair() -> None:
    client = FakeModelClient(failed(ModelOutcome.TIMEOUT))
    outcome, key = await turn(client)

    assert outcome.status == "model_failed"
    assert outcome.failure == {
        "code": "timeout",
        "detail": "the model call was not answered in time",
    }
    assert len(client.calls) == 1
    assert key.signatures_made == 0
    [record] = outcome.decisions
    payload = record.to_json()
    # Sent, so recorded: charged its estimate, with nothing reported.
    assert payload["validation"] == {"ok": False, "code": None, "feedback": None}
    assert payload["cost_estimated_usd"] == "0.163080"
    assert payload["cost_reported_usd"] is None
    assert payload["usage"] is None
    assert payload["raw_response"] == {
        "error": {
            "outcome": "timeout",
            "status": None,
            "type": None,
            "message": "the provider's own words, private",
            "request_id": "req_failed",
        }
    }


async def test_a_refusal_is_a_refused_attempt_and_names_the_failure_when_last() -> None:
    client = FakeModelClient(
        answer(None, stop_reason="refusal"), answer("I can't help.", stop_reason="refusal")
    )
    outcome, _ = await turn(client)

    assert outcome.status == "model_failed"
    assert outcome.failure == {
        "code": "refusal",
        "detail": "the model declined to answer on the last of 2 attempt(s)",
    }
    assert [record.response.stop_reason for record in outcome.decisions] == ["refusal", "refusal"]
    assert [record.validation.code for record in outcome.decisions] == [
        "schema_error",
        "schema_error",
    ]
    assert [record.response.raw_response for record in outcome.decisions] == [None, "I can't help."]
    assert client.calls[1].repair is not None


async def test_a_refusal_then_a_valid_answer_is_signed() -> None:
    client = FakeModelClient(answer(None, stop_reason="refusal"), answer(offer("95000000")))
    outcome, _ = await turn(client)
    assert outcome.status == "signed"


async def test_an_answer_cut_off_at_max_tokens_is_refused_and_repaired() -> None:
    truncated = '{"decision": {"action": "offer", "quote_amount_minor": "9500'
    client = FakeModelClient(answer(truncated, stop_reason="max_tokens"), answer(offer("95000000")))
    outcome, _ = await turn(client)

    assert outcome.status == "signed"
    first = outcome.decisions[0]
    assert first.response.stop_reason == "max_tokens"
    assert first.response.raw_response == truncated
    assert first.validation.code == "schema_error"


async def test_a_max_tokens_cut_off_twice_is_repair_exhausted() -> None:
    client = FakeModelClient(
        answer("{", stop_reason="max_tokens"), answer("{", stop_reason="max_tokens")
    )
    outcome, _ = await turn(client)
    assert outcome.failure is not None
    assert outcome.failure["code"] == "repair_exhausted"


@pytest.mark.parametrize(
    ("result", "detail"),
    [
        (
            failed(ModelOutcome.RATE_LIMITED, status=429, error_type="rate_limit_error"),
            "the model call failed: rate_limited (HTTP 429 rate_limit_error)",
        ),
        (
            failed(ModelOutcome.REJECTED, status=400, error_type="invalid_request_error"),
            "the model call failed: rejected (HTTP 400 invalid_request_error)",
        ),
        (
            failed(ModelOutcome.PROVIDER_FAILURE, status=529, error_type="overloaded_error"),
            "the model call failed: provider_failure (HTTP 529 overloaded_error)",
        ),
        (
            failed(ModelOutcome.CONNECTION_FAILED),
            "the model call failed: connection_failed",
        ),
    ],
    ids=["rate_limited", "rejected", "provider_failure", "connection_failed"],
)
async def test_a_provider_error_fails_the_turn_at_once(result: ModelResult, detail: str) -> None:
    client = FakeModelClient(result)
    outcome, key = await turn(client)

    assert outcome.status == "model_failed"
    assert outcome.failure == {"code": "provider_error", "detail": detail}
    assert len(client.calls) == 1
    assert key.signatures_made == 0
    # The provider's message is private evidence: in the record, never in the public detail.
    assert "private" not in detail
    [record] = outcome.decisions
    assert record.response.raw_response["error"]["message"] == "the provider's own words, private"


# --------------------------------------------------------------------------------------
# Calls that were never sent, and the budget
# --------------------------------------------------------------------------------------


async def test_a_call_that_never_left_is_not_recorded() -> None:
    """The token count failed, so nothing was admitted or billed: no decision exists (ADR-089)."""
    client = FakeModelClient(failed(ModelOutcome.CONNECTION_FAILED, sent=False))
    outcome, _ = await turn(client)
    assert outcome.status == "model_failed"
    assert outcome.failure is not None
    assert outcome.failure["code"] == "provider_error"
    assert outcome.decisions == ()


async def test_a_refused_budget_ends_the_turn_budget_exhausted_with_no_record() -> None:
    client = FakeModelClient(budget_refused())
    outcome, key = await turn(client)

    assert outcome.status == "budget_exhausted"
    assert outcome.decisions == ()
    assert key.signatures_made == 0
    assert outcome.failure == {
        "code": "budget_exhausted",
        "detail": "the model budget refused the call: spend_ceiling, USD 1.900000 spent and USD "
        "0.163080 at most for this call would cross the USD 2.00 ceiling",
    }


async def test_a_budget_refused_repair_keeps_the_first_attempts_record() -> None:
    client = FakeModelClient(answer(offer("85000000")), budget_refused())
    outcome, _ = await turn(client)
    assert outcome.status == "budget_exhausted"
    assert [record.attempt for record in outcome.decisions] == [1]
    assert outcome.decisions[0].validation.code == "below_reservation"


# --------------------------------------------------------------------------------------
# What the validator is given, and what the model is sent
# --------------------------------------------------------------------------------------


async def test_json_that_is_not_an_envelope_is_refused_by_the_first_rule_it_breaks() -> None:
    """The parsed JSON, not the text, goes to the validator, so the repair can be specific."""
    extra = {"decision": {"action": "offer", "quote_amount_minor": "95000000", "valid_until": 1}}
    client = FakeModelClient(answer(extra), answer(offer("95000000")))
    outcome, _ = await turn(client)
    first = outcome.decisions[0]
    assert first.response.raw_response == extra
    assert first.validation.code == "extra_fields"


@pytest.mark.parametrize("text", ['{"decision": NaN}', '{"x": 1e400}', "not json", "[" * 5000])
async def test_text_that_cannot_travel_as_json_is_kept_as_text(text: str) -> None:
    client = FakeModelClient(answer(text), answer(offer("95000000")))
    outcome, _ = await turn(client)
    first = outcome.decisions[0]
    assert first.response.raw_response == text
    assert first.validation.code == "schema_error"
    json.dumps(outcome.to_json(), allow_nan=False)


def test_the_observation_is_sent_as_validated_less_the_instructions() -> None:
    message = observation_message(typed(SELLER_TURN))
    expected = json.loads(json.dumps(SELLER_TURN))
    del expected["mandate"]["instructions"]
    assert json.loads(message) == expected
    assert MANDATES["seller"]["instructions"] not in message


async def test_the_system_prompt_is_the_runs_own_and_static() -> None:
    client = FakeModelClient(answer(offer("85000000")), answer(offer("95000000")))
    await turn(client)
    prompt = TEMPLATE.system_prompt("seller", MANDATES["seller"]["instructions"])
    assert [call.system_prompt for call in client.calls] == [prompt, prompt]
    assert all(call.schema is DecisionEnvelope for call in client.calls)


def test_the_policy_reports_its_kind_and_versions() -> None:
    policy = policy_for(FakeModelClient())
    assert policy.kind == "model"
    assert policy.version == MODEL_VERSION == "model-1.0.0"
    assert policy.prompt_template_version == TEMPLATE.version


# --------------------------------------------------------------------------------------
# The turn's deadline (ADR-089)
# --------------------------------------------------------------------------------------


async def test_each_call_is_given_the_time_left_before_the_deadline_less_the_margin() -> None:
    clock = Clock()
    client = FakeModelClient(answer(offer("85000000")), answer(offer("95000000")))
    await turn(client, deadline=START + timedelta(seconds=105), clock=clock)
    assert [call.within_s for call in client.calls] == [
        105 - RESPONSE_MARGIN_S,
        105 - RESPONSE_MARGIN_S,
    ]


async def test_with_no_time_left_no_call_is_made() -> None:
    client = FakeModelClient()
    outcome, _ = await turn(client, deadline=START + timedelta(seconds=RESPONSE_MARGIN_S))
    assert client.calls == []
    assert outcome.status == "model_failed"
    assert outcome.decisions == ()
    assert outcome.failure == {
        "code": "timeout",
        "detail": "the turn's deadline left no time for attempt 1",
    }


class _AdvancingClient(FakeModelClient):
    """Each call takes the wall clock forward, as a slow model would."""

    def __init__(self, clock: Clock, seconds: float, *results: ModelResult) -> None:
        super().__init__(*results)
        self._clock = clock
        self._seconds = seconds

    async def decide(self, *args: Any, **kwargs: Any) -> ModelResult:
        self._clock.moment += timedelta(seconds=self._seconds)
        return await super().decide(*args, **kwargs)


async def test_a_repair_with_no_time_left_is_not_attempted() -> None:
    clock = Clock()
    client = _AdvancingClient(clock, 60, answer(offer("85000000")), answer(offer("95000000")))
    outcome, _ = await turn(client, deadline=START + timedelta(seconds=60), clock=clock)
    assert len(client.calls) == 1
    assert outcome.failure == {
        "code": "timeout",
        "detail": "the turn's deadline left no time for attempt 2",
    }
    assert [record.validation.code for record in outcome.decisions] == ["below_reservation"]


# --------------------------------------------------------------------------------------
# What the adversarial review of stage 3.2 found
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "explanation",
    ["\\ud800", "half an emoji \\ud83d", "\\udc00 low"],
    ids=["high", "high_after_text", "low"],
)
async def test_a_lone_surrogate_escape_is_kept_as_text_and_refused(explanation: str) -> None:
    """`json.loads` accepts `\\ud800`; UTF-8 cannot encode what it returns. Signed and cached, the
    turn would answer 500 on every ask and strand the run, so the answer stays text."""
    text = '{"decision":{"action":"offer","quote_amount_minor":"95000000"},"explanation":"%s"}'
    client = FakeModelClient(answer(text % explanation), answer(offer("95000000")))
    outcome, key = await turn(client)

    first = outcome.decisions[0]
    assert first.response.raw_response == text % explanation
    assert first.validation.code == "schema_error"
    assert key.signatures_made == 1  # the repair's, not the surrogate's
    json.dumps(outcome.to_json(), ensure_ascii=False).encode("utf-8")


async def test_a_lone_surrogate_in_text_or_stop_reason_is_written_as_its_escape() -> None:
    """What the provider's JSON already decoded to a lone surrogate cannot be kept as is."""
    result = answer("not json \ud800")
    result = ModelResult(
        result.outcome, MODEL, text=result.text, stop_reason="odd\udfff", usage=USAGE, sent=True
    )
    client = FakeModelClient(result, answer(offer("95000000")))
    outcome, _ = await turn(client)
    first = outcome.decisions[0]
    assert first.response.raw_response == "not json \\ud800"
    assert first.response.stop_reason == "odd\\udfff"
    json.dumps(outcome.to_json(), ensure_ascii=False).encode("utf-8")


async def test_a_provider_error_that_cannot_be_encoded_is_written_as_its_escape() -> None:
    client = FakeModelClient(
        failed(ModelOutcome.REJECTED, status=400, error_type="bad\ud800", message="m\udc00")
    )
    outcome, _ = await turn(client)
    assert outcome.failure == {
        "code": "provider_error",
        "detail": "the model call failed: rejected (HTTP 400 bad\\ud800)",
    }
    error = outcome.decisions[0].response.raw_response["error"]
    assert (error["type"], error["message"]) == ("bad\\ud800", "m\\udc00")
    json.dumps(outcome.to_json(), ensure_ascii=False).encode("utf-8")


async def test_json_nested_past_the_limit_is_kept_as_text_without_parsing() -> None:
    deep = "[" * (MAX_JSON_DEPTH + 1) + "]" * (MAX_JSON_DEPTH + 1)
    at_limit = "[" * MAX_JSON_DEPTH + "]" * MAX_JSON_DEPTH
    huge = "[" * 200_000 + "]" * 200_000  # would exhaust the parser's stack if parsed
    strings = '{"x": "' + "[" * 100 + '"}'  # brackets inside a string are not nesting
    client = FakeModelClient(*(answer(text) for text in (deep, at_limit, huge, strings)))
    raws = []
    for _ in range(4):
        outcome, _ = await turn(client, attempts=1)
        raws.append(outcome.decisions[0].response.raw_response)
    assert raws[0] == deep
    assert isinstance(raws[1], list)
    assert raws[2] == huge
    assert raws[3] == {"x": "[" * 100}


def test_the_json_depth_limit_is_far_above_a_decision() -> None:
    assert MAX_JSON_DEPTH == 32


async def test_a_refusal_whose_text_is_a_valid_decision_is_signed() -> None:
    """ADR-089: the validator judges what was written, whatever the stop reason."""
    for stop_reason in ("refusal", "max_tokens"):
        client = FakeModelClient(answer(offer("95000000"), stop_reason=stop_reason))
        outcome, _ = await turn(client)
        assert outcome.status == "signed"
        assert outcome.decisions[0].response.stop_reason == stop_reason


async def test_the_margin_kept_back_from_the_deadline_is_one_second() -> None:
    client = FakeModelClient(answer(offer("95000000")))
    await turn(client, deadline=START + timedelta(seconds=105))
    assert client.calls[0].within_s == 104.0

    on_the_margin = FakeModelClient()
    outcome, _ = await turn(on_the_margin, deadline=START + timedelta(seconds=1))
    assert on_the_margin.calls == []
    assert outcome.failure is not None and outcome.failure["code"] == "timeout"

    just_inside = FakeModelClient(answer(offer("95000000")))
    outcome, _ = await turn(just_inside, deadline=START + timedelta(seconds=1, milliseconds=1))
    assert len(just_inside.calls) == 1


@pytest.mark.parametrize(
    ("first", "second", "code"),
    [
        ("refusal", "end_turn", "repair_exhausted"),
        ("end_turn", "refusal", "refusal"),
    ],
    ids=["refusal_then_invalid", "invalid_then_refusal"],
)
async def test_refusal_names_the_failure_only_when_the_last_attempt_was_one(
    first: str, second: str, code: str
) -> None:
    client = FakeModelClient(
        answer(offer("85000000"), stop_reason=first)
        if first == "end_turn"
        else answer(None, stop_reason=first),
        answer(offer("85000000"), stop_reason=second)
        if second == "end_turn"
        else answer(None, stop_reason=second),
    )
    outcome, _ = await turn(client)
    assert outcome.failure is not None
    assert outcome.failure["code"] == code


async def test_each_repair_carries_the_feedback_of_the_attempt_just_before() -> None:
    """Asserted against the validator's own words, never the template that renders them."""
    extra = {"decision": {"action": "offer", "quote_amount_minor": "95000000", "actor": "seller"}}
    client = FakeModelClient(answer(offer("85000000")), answer(extra), answer(offer("95000000")))
    outcome, _ = await turn(client, attempts=3)
    assert outcome.status == "signed"
    first, second, _third = outcome.decisions
    assert (first.validation.code, second.validation.code) == ("below_reservation", "extra_fields")
    assert first.validation.feedback and second.validation.feedback
    one, two, three = (call.repair for call in client.calls)
    assert one is None
    assert two is not None and "below_reservation" in two and first.validation.feedback in two
    assert three is not None and "extra_fields" in three and second.validation.feedback in three
    assert first.validation.feedback not in three
    assert "90000000" in first.validation.feedback  # the seller's own floor, in its own repair


@pytest.mark.parametrize(
    ("status", "error_type", "detail"),
    [
        (
            529,
            "overloaded_error",
            "the model call failed: provider_failure (HTTP 529 overloaded_error)",
        ),
        (502, None, "the model call failed: provider_failure (HTTP 502)"),
        (None, "fixture_error", "the model call failed: provider_failure (fixture_error)"),
        (None, None, "the model call failed: provider_failure"),
    ],
    ids=["status_and_type", "status_only", "type_only", "neither"],
)
async def test_a_failed_calls_record_and_public_detail(
    status: int | None, error_type: str | None, detail: str
) -> None:
    client = FakeModelClient(
        failed(ModelOutcome.PROVIDER_FAILURE, status=status, error_type=error_type)
    )
    outcome, _ = await turn(client)
    assert outcome.failure == {"code": "provider_error", "detail": detail}
    assert outcome.decisions[0].response.raw_response == {
        "error": {
            "outcome": "provider_failure",
            "status": status,
            "type": error_type,
            "message": "the provider's own words, private",
            "request_id": "req_failed",
        }
    }


async def test_a_provisioned_run_prompts_its_own_role_and_instructions_with_its_own_settings() -> (
    None
):
    """The run's role, instructions, model, effort and timeout reach the client it is given."""
    from agent.budget import ModelPriceTable
    from agent.model import ModelCallConfig, ModelRuntime
    from agent.service import model_policy_factory
    from agent.state import Provisioning

    made: list[tuple[ModelCallConfig, Any]] = []
    fake = FakeModelClient(answer(offer("95000000")))

    def make(config: ModelCallConfig, guard: Any) -> FakeModelClient:
        made.append((config, guard))
        return fake

    runtime = ModelRuntime("fixture", ModelPriceTable.load(), False, 16_000, make)
    instructions = "SELLER-INSTRUCTIONS-MARKER: hold firm above 95."
    request = Provisioning(
        role="seller",
        policy="model",
        model_id="claude-opus-5-5",
        effort="medium",
        repair_attempts=1,
        model_call_ceiling=7,
        model_spend_ceiling_usd=Decimal("0.50"),
        model_timeout_s=30,
        expected=None,  # type: ignore[arg-type]  # reason: the factory never reads the session
        key_ref="env:SELLER_ROOT_KEY",
        mandate_version_id=RUN,
        mandate_document={**MANDATES["seller"], "instructions": instructions},
        initial_base=None,  # type: ignore[arg-type]  # reason: as above
        initial_quote=None,  # type: ignore[arg-type]  # reason: as above
        allowance=None,  # type: ignore[arg-type]  # reason: as above
        fingerprint="0x",
    )
    signer = KeyedKeyHolder().signer_for(KeyDerivation(31337, "seller", RUN))
    policy = model_policy_factory(runtime, None, TEMPLATE)(request, signer)
    await policy.decide(typed(SELLER_TURN), None, time_left_s=None)

    [(config, guard)] = made
    assert (config.model_id, config.effort, config.max_tokens, config.timeout_s) == (
        "claude-opus-5-5",
        "medium",
        16_000,
        30.0,
    )
    assert guard.calls == 0 and guard.spent_usd == 0
    system_prompt = fake.calls[0].system_prompt
    assert "You are the SELLER" in system_prompt
    assert "You are the BUYER" not in system_prompt
    assert instructions in system_prompt
    assert instructions not in fake.calls[0].observation
