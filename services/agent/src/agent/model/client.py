"""`ModelClient` and its Anthropic implementation (docs/architecture.md section 7, ADR-014).

The policy sees one method, `decide(system_prompt, observation, schema) -> ModelResult`, so it can
be tested with a fake and another provider is an adapter (ADR-027). The Anthropic client is built
per run, with the run's model, effort, `max_tokens` and timeout and the run's `BudgetGuard`. Each
call is:

1. the request, built once: the system prompt as one text block behind a cache breakpoint, the
   observation as the only user message, adaptive thinking, the run's effort, and the envelope's
   JSON schema as the output format. No tools, no prefill, no sampling settings, no `fallbacks`
   (ADR-015);
2. `count_tokens` on exactly that request, so the guard can price the call before it is sent;
3. the guard's admission, or a `budget_refused` result with nothing sent;
4. the call, with the SDK's retries at 0 and redirects not followed, so that nothing is sent twice
   behind the guard's back and nothing is sent anywhere but the provider (ADR-086);
5. one `ModelResult`, whichever way the call ended, with the guard told what it used — exactly once,
   and also when the call is cancelled, which then propagates (ADR-085).

The caller may bound the whole of it, the token count included, with `within_s`: the turn's
deadline less a margin for the answer to travel (ADR-089). A call cut short there is `timeout`,
charged its estimate if it was admitted, rather than a cancellation.

Only an exception that is not about the provider or the network escapes `decide`: cancellation, and
a programming error. A body the endpoint would never send — not JSON, not a message, content of the
wrong shape, a count no model could produce — is `provider_failure`, not an exception.

The key and the base URL are given to the SDK explicitly, and the SDK client is a subclass that
drops the headers `ANTHROPIC_CUSTOM_HEADERS` would add, because the SDK reads that variable every
time a client is built or copied and sends its headers after the key: one variable could otherwise
replace the referenced key or add a beta header (ADR-086, ADR-015).

The output format is the one `messages.parse` would send — the SDK's own `transform_schema` of the
envelope model — but the request goes through `messages.create` and the text is validated here.
`parse` raises when the text does not validate, and the exception carries neither the usage nor
the request id, which a refused, truncated or malformed answer must still record (ADR-014 as
built).

Nothing here logs. The request, the response and the provider's error messages are private: they
reach the decision record through `ModelResult` and nowhere else. The SDK's own loggers are held at
`WARNING` by `agent.logs`, because at `DEBUG` they print request bodies.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final, Protocol

import anthropic
from anthropic.types import Message
from pydantic import BaseModel, SecretStr, ValidationError

from agent.budget import Admission, BudgetGuard, BudgetRefusal, TokenUsage
from agent.model.result import ModelOutcome, ModelResult, ProviderError

DEFAULT_BASE_URL: Final = "https://api.anthropic.com"
#: A token count above this is not one a model could report: the largest context window is 1M.
_MAX_TOKEN_COUNT: Final = 10_000_000


class ModelClient(Protocol):
    async def decide(
        self,
        system_prompt: str,
        observation: str,
        schema: type[BaseModel],
        *,
        repair: str | None = None,
        within_s: float | None = None,
    ) -> ModelResult:
        """One model call. Never raises for anything the provider or the network does.

        `repair` is a repair attempt's own feedback, sent as a second text block after the
        observation. `within_s` bounds the whole call, the token count included: a call still
        unanswered then is a `timeout`, charged its estimate if it was admitted (ADR-089).
        """
        ...


@dataclass(frozen=True, slots=True)
class ModelCallConfig:
    """A run's model settings: `model_id`, `effort`, the agent's `max_tokens`, and
    `limits.model_timeout_s`."""

    model_id: str
    effort: str
    max_tokens: int
    timeout_s: float


class _ReferencedKeyAnthropic(anthropic.AsyncAnthropic):
    """The SDK client with no headers from the environment.

    The SDK merges `ANTHROPIC_CUSTOM_HEADERS` into every client it builds, `with_options` copies
    included, and sends them after its own: `X-Api-Key`, `Authorization` and `anthropic-beta` can
    all arrive that way. Clearing them on every construction leaves the referenced key as the only
    credential. `test_model_client.py` sets the variable, so an SDK that moves the merge fails it.
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._custom_headers = {}


def anthropic_sdk(
    api_key: SecretStr, *, base_url: str = DEFAULT_BASE_URL
) -> anthropic.AsyncAnthropic:
    """The SDK client every run's calls share. The key and the base URL are passed explicitly, so
    the SDK reads neither `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, a login profile nor
    `ANTHROPIC_BASE_URL` from the environment, and no header from `ANTHROPIC_CUSTOM_HEADERS`; it
    never retries, and never follows a redirect, which would send the key and the prompt again to
    wherever `Location` points."""
    return _ReferencedKeyAnthropic(
        api_key=api_key.get_secret_value(),
        base_url=base_url,
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(follow_redirects=False),
    )


class _Charge:
    """One admitted call's settlement with the guard, made exactly once."""

    def __init__(self, guard: BudgetGuard, admission: Admission) -> None:
        self._guard = guard
        self.admission = admission
        self._settled = False
        self.reported_usd: Decimal | None = None

    def settle(self, usage: TokenUsage | None) -> Decimal | None:
        if not self._settled:
            self._settled = True
            self.reported_usd = self._guard.settle(self.admission, usage)
        return self.reported_usd


class _InFlight:
    """What `decide` needs to know about a call the deadline cut short."""

    def __init__(self) -> None:
        self.charge: _Charge | None = None
        self.started: float | None = None


class AnthropicModelClient:
    def __init__(
        self,
        sdk: anthropic.AsyncAnthropic,
        config: ModelCallConfig,
        guard: BudgetGuard,
        *,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sdk = sdk.with_options(timeout=config.timeout_s, max_retries=0)
        self._config = config
        self._guard = guard
        self._monotonic = monotonic

    async def decide(
        self,
        system_prompt: str,
        observation: str,
        schema: type[BaseModel],
        *,
        repair: str | None = None,
        within_s: float | None = None,
    ) -> ModelResult:
        request = request_body(self._config, system_prompt, observation, schema, repair)
        call = _InFlight()
        limit = asyncio.timeout(None if within_s is None else max(0.0, within_s))
        try:
            async with limit:
                return await self._decide(request, schema, call)
        except TimeoutError:
            if not limit.expired():
                raise
            # The turn's deadline, not the provider: `_decide` has already charged an admitted
            # call its estimate on the way out, as it does for any cancellation.
            return self._past_deadline(call)

    async def _decide(
        self, request: Mapping[str, Any], schema: type[BaseModel], call: _InFlight
    ) -> ModelResult:
        counted = await self._count_tokens(request)
        if isinstance(counted, ModelResult):
            return counted
        admission = self._guard.admit(counted, self._config.max_tokens)
        if isinstance(admission, BudgetRefusal):
            return ModelResult(
                ModelOutcome.BUDGET_REFUSED, self._config.model_id, budget_refusal=admission
            )
        call.charge = _Charge(self._guard, admission)
        call.started = self._monotonic()
        try:
            return await self._call(request, call.charge, schema)
        except BaseException:
            # Cancelled — by a deadline, say — or a programming error: the call may still have
            # been sent and billed, so it is charged its estimate before the exception propagates.
            call.charge.settle(None)
            raise

    def _past_deadline(self, call: _InFlight) -> ModelResult:
        charge = call.charge
        return ModelResult(
            ModelOutcome.TIMEOUT,
            self._config.model_id,
            cost_estimated_usd=None if charge is None else charge.admission.estimated_usd,
            latency_ms=None if call.started is None else self._elapsed_ms(call.started),
            provider_error=ProviderError(None, None, "no answer before the turn's deadline"),
            sent=charge is not None,
        )

    async def _call(
        self, request: Mapping[str, Any], charge: _Charge, schema: type[BaseModel]
    ) -> ModelResult:
        started = self._monotonic()
        request_id: str | None = None
        try:
            raw = await self._sdk.messages.with_raw_response.create(
                max_tokens=self._config.max_tokens, **request
            )
            request_id = raw.request_id
            message: object = await raw.parse()
        except anthropic.APIError as error:
            return self._failed(error, charge, self._elapsed_ms(started))
        except ValueError:
            # A success status whose body does not decode: not JSON, or not UTF-8.
            return self._malformed(charge, request_id, self._elapsed_ms(started))
        return self._answered(message, request_id, charge, self._elapsed_ms(started), schema)

    async def _count_tokens(self, request: Mapping[str, Any]) -> int | ModelResult:
        try:
            counted: object = await self._sdk.messages.count_tokens(**request)
        except anthropic.APIError as error:
            return self._failed(error, None, None)
        except ValueError:
            return self._malformed(None, None, None)
        input_tokens = getattr(counted, "input_tokens", None)
        if not _is_count(input_tokens):
            return self._malformed(None, None, None)
        assert isinstance(input_tokens, int)  # narrowed by _is_count
        return input_tokens

    def _answered(
        self,
        message: object,
        request_id: str | None,
        charge: _Charge,
        latency_ms: int,
        schema: type[BaseModel],
    ) -> ModelResult:
        # Everything that can find the body malformed runs before the guard is told the usage.
        usage = _usage(message)
        texts = _texts(getattr(message, "content", None))
        stop_reason = getattr(message, "stop_reason", None)
        model = getattr(message, "model", None)
        if (
            not isinstance(message, Message)
            or usage is None
            or texts is None
            or not isinstance(stop_reason, str | None)
            or not isinstance(model, str | None)
        ):
            return self._malformed(charge, request_id, latency_ms)
        reported = charge.settle(usage)
        text = "".join(texts) or None
        outcome, decision = sort_answer(stop_reason, text, schema)
        return ModelResult(
            outcome,
            self._config.model_id,
            served_model=model,
            decision=decision,
            text=text,
            stop_reason=stop_reason,
            usage=usage,
            cost_estimated_usd=charge.admission.estimated_usd,
            cost_reported_usd=reported,
            request_id=request_id,
            latency_ms=latency_ms,
            sent=True,
        )

    def _failed(
        self, error: anthropic.APIError, charge: _Charge | None, latency_ms: int | None
    ) -> ModelResult:
        """An error the SDK raised. A call that was admitted is charged its estimate: it may have
        been billed although no usage came back."""
        if charge is not None:
            charge.settle(None)
        outcome, provider_error = _sort_error(error)
        return ModelResult(
            outcome,
            self._config.model_id,
            cost_estimated_usd=None if charge is None else charge.admission.estimated_usd,
            request_id=getattr(error, "request_id", None),
            latency_ms=latency_ms,
            provider_error=provider_error,
            sent=charge is not None,
        )

    def _malformed(
        self, charge: _Charge | None, request_id: str | None, latency_ms: int | None
    ) -> ModelResult:
        """A success status whose body is not what the endpoint returns."""
        if charge is not None:
            charge.settle(None)
        return ModelResult(
            ModelOutcome.PROVIDER_FAILURE,
            self._config.model_id,
            cost_estimated_usd=None if charge is None else charge.admission.estimated_usd,
            request_id=request_id,
            latency_ms=latency_ms,
            provider_error=ProviderError(
                200, None, "the response body is not what the endpoint returns"
            ),
            sent=charge is not None,
        )

    def _elapsed_ms(self, started: float) -> int:
        return max(0, round((self._monotonic() - started) * 1000))


def request_body(
    config: ModelCallConfig,
    system_prompt: str,
    observation: str,
    schema: type[BaseModel],
    repair: str | None,
) -> dict[str, Any]:
    """The request both endpoints are sent, `max_tokens` aside, which `count_tokens` has no use
    for. A repair is a second text block in the same user message: nothing but this agent's own
    feedback is added, and the cached system prompt is unchanged. The outbound-context assertion
    checks exactly this, for a fixture run too, which sends nothing (ADR-092)."""
    content: str | list[dict[str, str]] = (
        observation
        if repair is None
        else [{"type": "text", "text": observation}, {"type": "text", "text": repair}]
    )
    return {
        "model": config.model_id,
        "system": [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        "messages": [{"role": "user", "content": content}],
        "thinking": {"type": "adaptive"},
        "output_config": {
            "effort": config.effort,
            "format": {"type": "json_schema", "schema": anthropic.transform_schema(schema)},
        },
    }


def sort_answer(
    stop_reason: str | None, text: str | None, schema: type[BaseModel]
) -> tuple[ModelOutcome, Mapping[str, Any] | None]:
    """An answered call's outcome, and the decision as the model wrote it when it is one. The
    fixture client sorts its canned answers with this too, so they are judged as a real one is."""
    if stop_reason == "refusal":
        return ModelOutcome.REFUSAL, None
    if stop_reason == "max_tokens":
        return ModelOutcome.MAX_TOKENS, None
    if text is None:
        return ModelOutcome.UNPARSEABLE, None
    try:
        schema.model_validate_json(text)
    except ValidationError:
        return ModelOutcome.UNPARSEABLE, None
    # The decision as the model wrote it, not as the envelope model would re-serialise it.
    decision = json.loads(text)
    return ModelOutcome.DECIDED, decision


def _sort_error(error: anthropic.APIError) -> tuple[ModelOutcome, ProviderError]:
    """Most specific first: a timeout is a connection error, and a 429 a status error. A redirect,
    never followed, is `rejected` with its 3xx status."""
    if isinstance(error, anthropic.APITimeoutError):
        return ModelOutcome.TIMEOUT, ProviderError(None, None, "no answer within the timeout")
    if isinstance(error, anthropic.APIConnectionError):
        return ModelOutcome.CONNECTION_FAILED, ProviderError(None, None, error.message)
    if isinstance(error, anthropic.APIStatusError):
        status = error.status_code
        provider_error = ProviderError(status, _error_type(error.body), error.message)
        if status == 429:
            return ModelOutcome.RATE_LIMITED, provider_error
        if status >= 500:
            return ModelOutcome.PROVIDER_FAILURE, provider_error
        return ModelOutcome.REJECTED, provider_error
    return ModelOutcome.PROVIDER_FAILURE, ProviderError(None, None, error.message)


def _error_type(body: object) -> str | None:
    """`error.type` from the provider's error envelope, when there is one."""
    if isinstance(body, Mapping):
        inner = body.get("error")
        if isinstance(inner, Mapping) and isinstance(inner.get("type"), str):
            return str(inner["type"])
    return None


def _usage(message: object) -> TokenUsage | None:
    """The usage a message reports, or None when it reports none that can be read."""
    usage = getattr(message, "usage", None)
    counts = {
        name: getattr(usage, name, None)
        for name in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
        )
    }
    one_hour = getattr(getattr(usage, "cache_creation", None), "ephemeral_1h_input_tokens", None)
    values = [*counts.values(), one_hour]
    if not all(value is None or _is_count(value) for value in values):
        return None
    if counts["input_tokens"] is None or counts["output_tokens"] is None:
        return None
    # Part of the cache writes, so never more than all of them: a 5-minute share below zero would
    # price the call below what was used.
    if (one_hour or 0) > (counts["cache_creation_input_tokens"] or 0):
        return None
    return TokenUsage(
        input_tokens=counts["input_tokens"],
        output_tokens=counts["output_tokens"],
        cache_read_input_tokens=counts["cache_read_input_tokens"] or 0,
        cache_creation_input_tokens=counts["cache_creation_input_tokens"] or 0,
        cache_creation_1h_input_tokens=one_hour or 0,
    )


def _texts(content: object) -> list[str] | None:
    """The text of every text block, or None when the content is not a list of blocks or a text
    block holds no string."""
    if not isinstance(content, Sequence) or isinstance(content, str | bytes):
        return None
    texts: list[str] = []
    for block in content:
        kind = getattr(block, "type", None)
        if not isinstance(kind, str):
            return None
        if kind == "text":
            text = getattr(block, "text", None)
            if not isinstance(text, str):
                return None
            texts.append(text)
    return texts


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= _MAX_TOKEN_COUNT
