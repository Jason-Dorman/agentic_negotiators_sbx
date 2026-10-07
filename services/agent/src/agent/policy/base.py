"""The `Policy` protocol: `decide(observation) -> PolicyResponse` (docs/architecture.md 3.3).

A policy chooses among moves; it has no authority. Whatever it returns is validated by the
`MandateValidator` and signed, if at all, by the signer from validated state (ADR-006). So the
response is deliberately untyped where it matters — `raw_response` is whatever the policy produced,
exactly as produced — because the decision record has to show what was proposed, not what the
validator made of it.

A model call can also end without an answer to validate — a timeout, a provider fault, a budget
that refuses it. That is a `PolicyFailure`, which ends the turn at once with no repair (ADR-089):
asking again would be another billed call into the same fault, and the spec stops new decisions on
a timeout or a spent budget. It carries the attempt's record when the call was sent, and none when
it never left: a call that was not made is not a decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from agent.observation import Observation

PolicyKind = Literal["deterministic", "model"]
#: The turn response's `failure.code` for an attempt that ended without an answer (ADR-089).
FailureCode = Literal["timeout", "provider_error", "budget_exhausted"]


@dataclass(frozen=True, slots=True)
class Repair:
    """The previous attempt's refusal: this agent's own private feedback, and nothing else."""

    code: str
    feedback: str


@dataclass(frozen=True, slots=True)
class PolicyResponse:
    """One attempt's output and its accounting, for the decision record (data_model 3.8)."""

    raw_response: Any
    stop_reason: str | None
    usage: Mapping[str, int] | None
    cost_estimated_usd: str | None
    cost_reported_usd: str | None

    @classmethod
    def computed(cls, raw_response: Any) -> PolicyResponse:
        """A response no model produced: no stop reason, no tokens, no cost."""
        return cls(raw_response, None, None, None, None)


@dataclass(frozen=True, slots=True)
class PolicyFailure:
    """An attempt that ended without an answer, and so the turn. `detail` is public: it names the
    kind of fault and never anything the model or the provider said."""

    code: FailureCode
    detail: str
    #: The attempt's record when the call was sent; None when it never left the agent.
    response: PolicyResponse | None = None


class Policy(Protocol):
    @property
    def kind(self) -> PolicyKind: ...

    @property
    def version(self) -> str: ...

    @property
    def prompt_template_version(self) -> str | None: ...

    async def decide(
        self, observation: Observation, repair: Repair | None, *, time_left_s: float | None
    ) -> PolicyResponse | PolicyFailure:
        """Choose one move. `repair` is set on a repair attempt and names what was refused.
        `time_left_s` is how long the turn's deadline still allows, None when it sets none."""
        ...
