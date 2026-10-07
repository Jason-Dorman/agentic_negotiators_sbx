"""`ModelPolicy`: a model chooses the move (docs/architecture.md sections 3.3 and 7, ADR-089).

The policy turns the observation into a request, asks its run's `ModelClient`, and hands back what
the model wrote — exactly as written — for the `MandateValidator` to judge. It has no authority and
alters nothing: a price the model proposes is never chosen, clamped or fixed here or anywhere
(CLAUDE.md), and the signer builds every signed message from validated state, never from these
bytes.

**The request.** The run's system prompt, rendered once from the versioned template (ADR-029), and
the observation as compact JSON. The mandate's `instructions` are already in the system prompt's
delimited section, so the observation's copy of them is left out of the user message rather than
sent twice; nothing else is removed and nothing is added. A repair attempt sends, as a second text
block, this agent's own validation code and feedback for the attempt before, and nothing else.

**What each outcome means for the turn (ADR-089).**

- `decided`: the decision as written, for the validator; signed, or refused and repaired.
- `unparseable`, `max_tokens`: the text, for the validator to refuse and a repair to follow; the
  turn's failure is `repair_exhausted` if it is the last attempt.
- `refusal`: the same, and the failure is `refusal` if it is the last attempt.
- `timeout`: a `PolicyFailure` with no repair; the turn fails `timeout`.
- `rate_limited`, `rejected`, `provider_failure`, `connection_failed`: a `PolicyFailure` with no
  repair; the turn fails `provider_error`.
- `budget_refused`: a `PolicyFailure` with no record, since nothing was sent; the turn ends
  `budget_exhausted`.

Every answer is judged by the validator, whatever its stop reason: a `refusal` or `max_tokens`
answer whose text happens to be a valid, in-mandate decision is signed like any other (ADR-089).
An answer that is not a decision is given to the validator as the JSON it parses to, or as the raw
text when it is not JSON, so the refusal names the first rule it breaks and the repair has
something specific to act on. A failure's record — only when the call was sent — holds the outcome
and the provider's error, which are private like the model's text; its public `detail` names only
the kind of fault.

**Everything returned must travel.** The turn response is JSON encoded as UTF-8, and the backend
stores it, so a value that cannot be encoded would fail the response after the turn was signed and
cached, and strand the run. JSON text is parsed only when it nests no deeper than
`MAX_JSON_DEPTH` — a decision is two levels deep, and a pathologically deep answer would exhaust the
parser's stack — and only when what it parses to holds no `NaN`, no `Infinity`, no number too large
for a double and no lone UTF-16 surrogate; otherwise it is kept as text, which the validator
refuses. Text, a stop reason and a provider's error that hold a lone surrogate are kept with it
written as its escape, `\\ud800`, the one change made to what the provider returned.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Final

from agent.model import DecisionEnvelope, ModelClient, ModelOutcome, ModelResult
from agent.observation import Observation, Role
from agent.policy.base import PolicyFailure, PolicyKind, PolicyResponse, Repair
from agent.prompting import PromptTemplate

VERSION: Final = "model-1.0.0"
#: Deeper JSON is kept as text, unparsed. Far above any decision's two levels.
MAX_JSON_DEPTH: Final = 32

#: Outcomes that are answers, however wrong: the validator judges them and a repair may follow.
_ANSWERED: Final = frozenset(
    {
        ModelOutcome.DECIDED,
        ModelOutcome.UNPARSEABLE,
        ModelOutcome.REFUSAL,
        ModelOutcome.MAX_TOKENS,
    }
)


class ModelPolicy:
    """One run's model policy: its client, its rendered system prompt, its template version."""

    def __init__(
        self, client: ModelClient, template: PromptTemplate, *, role: Role, instructions: str
    ) -> None:
        self._client = client
        self._template = template
        self._system_prompt = template.system_prompt(role, instructions)

    @property
    def kind(self) -> PolicyKind:
        return "model"

    @property
    def version(self) -> str:
        return VERSION

    @property
    def prompt_template_version(self) -> str | None:
        return self._template.version

    async def decide(
        self, observation: Observation, repair: Repair | None, *, time_left_s: float | None
    ) -> PolicyResponse | PolicyFailure:
        result = await self._client.decide(
            self._system_prompt,
            observation_message(observation),
            DecisionEnvelope,
            repair=None
            if repair is None
            else self._template.repair_message(repair.code, repair.feedback),
            within_s=time_left_s,
        )
        return policy_result(result)


def observation_message(observation: Observation) -> str:
    """The user message: the observation as validated, less the instructions the system prompt
    already carries."""
    document = dict(observation.document)
    mandate = document.get("mandate")
    if isinstance(mandate, dict):
        document["mandate"] = {
            key: value for key, value in mandate.items() if key != "instructions"
        }
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"))


def policy_result(result: ModelResult) -> PolicyResponse | PolicyFailure:
    usage = None if result.usage is None else result.usage.as_record()
    estimated = _usd(result.cost_estimated_usd)
    reported = _usd(result.cost_reported_usd)
    stop_reason = _travelling(result.stop_reason)
    if result.outcome in _ANSWERED:
        return PolicyResponse(_answer(result), stop_reason, usage, estimated, reported)
    if result.outcome is ModelOutcome.BUDGET_REFUSED:
        refusal = result.budget_refusal
        detail = "the model budget refused the call" + (
            "" if refusal is None else f": {refusal.reason}, {refusal.detail}"
        )
        return PolicyFailure("budget_exhausted", detail)
    record = (
        PolicyResponse(_error(result), stop_reason, usage, estimated, reported)
        if result.sent
        else None
    )
    if result.outcome is ModelOutcome.TIMEOUT:
        return PolicyFailure("timeout", "the model call was not answered in time", record)
    return PolicyFailure("provider_error", _provider_detail(result), record)


def _answer(result: ModelResult) -> Any:
    """What the model wrote: the decision, the JSON its text parses to, or the text itself."""
    if result.decision is not None:
        return dict(result.decision)
    text = result.text
    if text is None:
        return None
    if _nesting(text) > MAX_JSON_DEPTH:
        return _travelling(text)
    try:
        parsed = json.loads(text, parse_constant=_not_json, parse_float=_finite)
        json.dumps(parsed, ensure_ascii=False).encode("utf-8")
    except (ValueError, UnicodeEncodeError):
        return _travelling(text)
    return parsed


def _nesting(text: str) -> int:
    """The deepest nesting of `[` and `{` outside strings, without parsing: one linear pass."""
    depth = deepest = 0
    in_string = escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
        elif char == '"':
            in_string = True
        elif char in "[{":
            depth += 1
            deepest = max(deepest, depth)
        elif char in "]}":
            depth -= 1
    return deepest


def _travelling(text: str | None) -> str | None:
    """`text`, with any lone surrogate — which UTF-8 cannot encode — written as its escape."""
    if text is None:
        return None
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-8", "backslashreplace").decode("utf-8")
    return text


def _not_json(name: str) -> Any:
    """`NaN` and `Infinity` are not JSON, and a response that held one could not be sent on."""
    raise ValueError(f"{name} is not JSON")


def _finite(literal: str) -> float:
    value = float(literal)
    if value in (float("inf"), float("-inf")):
        raise ValueError("a number too large for a float")
    return value


def _error(result: ModelResult) -> dict[str, Any]:
    """A sent call's private record when it ended without an answer."""
    error = result.provider_error
    return {
        "error": {
            "outcome": str(result.outcome),
            "status": None if error is None else error.status,
            "type": None if error is None else _travelling(error.error_type),
            "message": None if error is None else _travelling(error.message),
            "request_id": result.request_id,
        }
    }


def _provider_detail(result: ModelResult) -> str:
    error = result.provider_error
    error_type = None if error is None else _travelling(error.error_type)
    detail = f"the model call failed: {result.outcome}"
    if error is not None and error.status is not None:
        detail += f" (HTTP {error.status}" + (f" {error_type})" if error_type else ")")
    elif error_type is not None:
        detail += f" ({error_type})"
    return detail


def _usd(value: Decimal | None) -> str | None:
    return None if value is None else f"{value:f}"


__all__ = ["MAX_JSON_DEPTH", "VERSION", "ModelPolicy", "observation_message", "policy_result"]
