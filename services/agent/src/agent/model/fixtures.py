"""The fixture model client: canned answers in place of the provider (Q73, ADR-088).

An instance runs in fixture mode when its own configuration says so — `AGENT_MODEL_FIXTURES` names
a directory — and never because a run request asked: no operator request can turn a live run into
a canned one. The instance reports `model_mode: "fixture"` in its health, and the backend records
the run as `fixture` from that report at validation, so every surface labels it.

The directory holds one script per role, `buyer.json` and `seller.json`. Each run on the instance
plays its role's script from the start, one entry per model call, in order:

- `answer`: a decision envelope, as an object. A string value that is exactly `{{path}}` is
  replaced by the value at that dotted path in the observation — `{{active_offer.offer_hash}}` —
  because a digest differs from run to run and a canned accept must still name the active offer.
- `text`: the model's text, verbatim, for an answer that is not an envelope.
- `failure`: a call that ends without an answer — `timeout`, `rate_limited`, `rejected`,
  `provider_failure` or `connection_failed`.

An answer carries `usage`, as the provider would report it, and a `stop_reason`, `end_turn` unless
given. Every call goes through the run's real `BudgetGuard`: the prompt's size in the usage is the
token count the guard prices, so ceilings, estimates and reported costs behave as they do live, and
a fixture can drive a run into its spend ceiling. `delay_s` holds an answer back, and is bounded as
a live call is: by the run's `model_timeout_s` and by the turn's deadline, whichever comes first,
and an answer later than either is a `timeout`, charged its estimate.

The answers are judged exactly as a real model's are: `sort_answer` sorts them, the validator
checks them, the signer signs only what passed. A script that runs out is a `provider_failure` that
was never sent, which fails the turn rather than inventing a move.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from agent.budget import Admission, BudgetGuard, BudgetRefusal, TokenUsage
from agent.model.client import ModelCallConfig, sort_answer
from agent.model.result import ModelOutcome, ModelResult, ProviderError
from agent.observation import Role

FailureKind = Literal[
    "timeout", "rate_limited", "rejected", "provider_failure", "connection_failed"
]

_FAILURES: Final[Mapping[str, tuple[ModelOutcome, int | None, str]]] = {
    "timeout": (ModelOutcome.TIMEOUT, None, "no answer within the timeout"),
    "rate_limited": (ModelOutcome.RATE_LIMITED, 429, "rate limited"),
    "rejected": (ModelOutcome.REJECTED, 400, "invalid request"),
    "provider_failure": (ModelOutcome.PROVIDER_FAILURE, 529, "overloaded"),
    "connection_failed": (ModelOutcome.CONNECTION_FAILED, None, "connection error"),
}
_ERROR_TYPES: Final = {
    "rate_limited": "rate_limit_error",
    "rejected": "invalid_request_error",
    "provider_failure": "overloaded_error",
}


class FixtureError(Exception):
    """A fixture directory or script that cannot be used. The message names the file."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class FixtureUsage(_Strict):
    input_tokens: Annotated[int, Field(ge=0)]
    output_tokens: Annotated[int, Field(ge=0)]
    cache_read_input_tokens: Annotated[int, Field(ge=0)] = 0
    cache_creation_input_tokens: Annotated[int, Field(ge=0)] = 0

    def as_usage(self) -> TokenUsage:
        return TokenUsage(
            input_tokens=self.input_tokens,
            output_tokens=self.output_tokens,
            cache_read_input_tokens=self.cache_read_input_tokens,
            cache_creation_input_tokens=self.cache_creation_input_tokens,
        )

    @property
    def prompt_tokens(self) -> int:
        """What `count_tokens` would have said: every input token, cached or not (ADR-079)."""
        return self.input_tokens + self.cache_read_input_tokens + self.cache_creation_input_tokens


class FixtureEntry(_Strict):
    answer: dict[str, Any] | None = None
    text: str | None = None
    failure: FailureKind | None = None
    usage: FixtureUsage | None = None
    stop_reason: str = "end_turn"
    #: The token count of a call that fails: what the guard prices before it is sent.
    input_tokens: Annotated[int, Field(ge=0)] = 0
    delay_s: Annotated[float, Field(ge=0, le=600)] = Field(default=0.0, strict=False)

    @model_validator(mode="after")
    def _one_kind(self) -> FixtureEntry:
        kinds = [name for name in ("answer", "text", "failure") if getattr(self, name) is not None]
        if len(kinds) != 1:
            raise ValueError("an entry has exactly one of answer, text and failure")
        if self.failure is None and self.usage is None:
            raise ValueError("an answer or a text needs the usage the provider would report")
        return self


class FixtureScript(_Strict):
    fixture_version: Literal["1"]
    description: str
    responses: list[FixtureEntry]

    @classmethod
    def load(cls, path: Path) -> FixtureScript:
        try:
            return cls.model_validate_json(path.read_bytes())
        except OSError as error:
            raise FixtureError(f"cannot read the fixture script {path}: {error}") from None
        except ValidationError as error:
            locations = sorted(
                ".".join(str(part) for part in item["loc"]) or "<root>"
                for item in error.errors(include_input=False, include_url=False)
            )
            raise FixtureError(f"invalid fixture script {path} at {', '.join(locations)}") from None


def load_fixture_scripts(directory: Path) -> dict[Role, FixtureScript]:
    """Both roles' scripts. An instance plays only its own role's, but a directory missing either
    is refused, so a fixture set is always a whole negotiation."""
    if not directory.is_dir():
        raise FixtureError(f"the fixture directory {directory} does not exist")
    return {role: FixtureScript.load(directory / f"{role}.json") for role in ("buyer", "seller")}


class FixtureModelClient:
    """One run's client: its script's next entry per call, through the run's guard."""

    def __init__(
        self,
        script: FixtureScript,
        config: ModelCallConfig,
        guard: BudgetGuard,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._entries = script.responses
        self._config = config
        self._guard = guard
        self._monotonic = monotonic
        self._next = 0

    @property
    def calls_played(self) -> int:
        return self._next

    async def decide(
        self,
        system_prompt: str,
        observation: str,
        schema: type[BaseModel],
        *,
        repair: str | None = None,
        within_s: float | None = None,
    ) -> ModelResult:
        if self._next >= len(self._entries):
            return self._unsent("fixture_exhausted", "the fixture script has no answer left")
        entry = self._entries[self._next]
        self._next += 1
        count = entry.usage.prompt_tokens if entry.usage is not None else entry.input_tokens
        admission = self._guard.admit(count, self._config.max_tokens)
        if isinstance(admission, BudgetRefusal):
            return ModelResult(
                ModelOutcome.BUDGET_REFUSED, self._config.model_id, budget_refusal=admission
            )
        started = self._monotonic()
        deadline_binds = within_s is not None and within_s < self._config.timeout_s
        bound = (
            max(0.0, within_s)
            if within_s is not None and deadline_binds
            else self._config.timeout_s
        )
        limit = asyncio.timeout(bound)
        try:
            async with limit:
                await asyncio.sleep(entry.delay_s)
        except TimeoutError:
            if not limit.expired():
                raise
            self._guard.settle(admission, None)
            message = (
                "no answer before the turn's deadline"
                if deadline_binds
                else "no answer within the timeout"
            )
            return ModelResult(
                ModelOutcome.TIMEOUT,
                self._config.model_id,
                cost_estimated_usd=admission.estimated_usd,
                latency_ms=self._elapsed_ms(started),
                provider_error=ProviderError(None, None, message),
                sent=True,
            )
        except BaseException:
            self._guard.settle(admission, None)
            raise
        return self._play(entry, observation, schema, admission, started)

    def _play(
        self,
        entry: FixtureEntry,
        observation: str,
        schema: type[BaseModel],
        admission: Admission,
        started: float,
    ) -> ModelResult:
        request_id = f"fixture-{self._next}"
        if entry.failure is not None:
            self._guard.settle(admission, None)
            outcome, status, message = _FAILURES[entry.failure]
            return ModelResult(
                outcome,
                self._config.model_id,
                cost_estimated_usd=admission.estimated_usd,
                request_id=request_id,
                latency_ms=self._elapsed_ms(started),
                provider_error=ProviderError(status, _ERROR_TYPES.get(entry.failure), message),
                sent=True,
            )
        assert entry.usage is not None  # _one_kind
        if entry.answer is not None:
            try:
                text = json.dumps(_fill(entry.answer, json.loads(observation)))
            except (KeyError, TypeError, ValueError) as error:
                self._guard.settle(admission, None)
                return ModelResult(
                    ModelOutcome.PROVIDER_FAILURE,
                    self._config.model_id,
                    cost_estimated_usd=admission.estimated_usd,
                    request_id=request_id,
                    provider_error=ProviderError(None, "fixture_error", f"cannot fill: {error}"),
                    sent=True,
                )
        else:
            text = entry.text or ""
        usage = entry.usage.as_usage()
        reported = self._guard.settle(admission, usage)
        outcome, decision = sort_answer(entry.stop_reason, text or None, schema)
        return ModelResult(
            outcome,
            self._config.model_id,
            served_model=self._config.model_id,
            decision=decision,
            text=text or None,
            stop_reason=entry.stop_reason,
            usage=usage,
            cost_estimated_usd=admission.estimated_usd,
            cost_reported_usd=reported,
            request_id=request_id,
            latency_ms=self._elapsed_ms(started),
            sent=True,
        )

    def _unsent(self, error_type: str, message: str) -> ModelResult:
        return ModelResult(
            ModelOutcome.PROVIDER_FAILURE,
            self._config.model_id,
            provider_error=ProviderError(None, error_type, message),
        )

    def _elapsed_ms(self, started: float) -> int:
        return max(0, round((self._monotonic() - started) * 1000))


def _fill(value: Any, observation: Mapping[str, Any]) -> Any:
    """`{{path}}` strings replaced by the observation's value at that dotted path."""
    if isinstance(value, dict):
        return {key: _fill(item, observation) for key, item in value.items()}
    if isinstance(value, list):
        return [_fill(item, observation) for item in value]
    if isinstance(value, str) and value.startswith("{{") and value.endswith("}}"):
        found: Any = observation
        for part in value[2:-2].strip().split("."):
            if not isinstance(found, Mapping):
                raise KeyError(value)
            found = found[part]
        return found
    return value


__all__ = [
    "FixtureEntry",
    "FixtureError",
    "FixtureModelClient",
    "FixtureScript",
    "FixtureUsage",
    "load_fixture_scripts",
]
