"""The outbound-context assertion in the send path: every model client sits behind it (ADR-092).

`ModelRuntime` wraps each run's client, live or fixture, in a `CheckedModelClient`. Before the call
is handed on, the request is built exactly as the live client sends it — `request_body`, plus
`max_tokens` — and every string in it is checked against the run's `OutboundCheck`. A hit raises
`OutboundContextRefusedError` before anything is counted, priced or sent; its log line names the
kind of hit, never the text. A fixture run sends nothing, but it is checked all the same, so the
isolation suite's runs exercise the assertion a live run depends on.

`observer`, when the composition root is given one, sees each request that passed the check: what
would leave the process. Only the isolation suite gives one (A12); in service it is None.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

import structlog
from pydantic import BaseModel

from agent.errors import OutboundContextRefusedError
from agent.model.client import ModelCallConfig, ModelClient, request_body
from agent.model.result import ModelResult
from agent.outbound import OutboundCheck

RequestObserver = Callable[[Mapping[str, Any]], None]

_log = structlog.get_logger(component="agent.model")


class CheckedModelClient:
    def __init__(
        self,
        inner: ModelClient,
        config: ModelCallConfig,
        check: OutboundCheck,
        observer: RequestObserver | None = None,
    ) -> None:
        self._inner = inner
        self._config = config
        self._check = check
        self._observer = observer

    async def decide(
        self,
        system_prompt: str,
        observation: str,
        schema: type[BaseModel],
        *,
        repair: str | None = None,
        within_s: float | None = None,
    ) -> ModelResult:
        body = {
            **request_body(self._config, system_prompt, observation, schema, repair),
            "max_tokens": self._config.max_tokens,
        }
        kind = self._check.violation(body)
        if kind is not None:
            _log.error("outbound_context_refused", kind=kind)
            raise OutboundContextRefusedError(kind)
        if self._observer is not None:
            self._observer(body)
        return await self._inner.decide(
            system_prompt, observation, schema, repair=repair, within_s=within_s
        )


__all__ = ["CheckedModelClient", "RequestObserver"]
