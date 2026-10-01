"""One turn inside the agent: decide, validate, repair at most as provisioned, sign (ADR-012).

The loop the whole design turns on. A policy decides; the validator checks the complete trade; a
refusal goes back to the *same* policy as private feedback for the provisioned number of repairs; a
decision that passes is signed from validated state. Every attempt, accepted or refused, becomes a
decision record the backend stores verbatim as private evidence (data_model section 3.8).

Nothing here alters a proposal. If every attempt is refused, the turn ends `model_failed` with
`repair_exhausted`, and the controller aborts the session with `model_failure` — a recorded failure,
never an economic walk-away (spec section 5.3, ADR-007). The failure's `detail` names the count of
refused attempts and no validation code or feedback: those are private and live in the decision
records, and the failure is not.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from agent.errors import InvalidStateError
from agent.keys import RunSigner
from agent.observation import Observation
from agent.policy import Policy, PolicyResponse, Repair
from agent.signing import ApprovedSession, SignedAction, sign_decision
from agent.validation import MandateValidator, Validation

TurnStatus = Literal["signed", "model_failed", "budget_exhausted"]


class Clock(Protocol):
    def now(self) -> datetime: ...

    def monotonic(self) -> float: ...


class SystemClock:
    """Wall-clock time, used for `requested_at` and latency only — never for protocol time."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    def monotonic(self) -> float:
        return time.monotonic()


def iso_utc(moment: datetime) -> str:
    """ISO 8601 in UTC with a `Z`, millisecond precision (docs/api_contract.md section 1)."""
    return moment.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    attempt: int
    response: PolicyResponse
    validation: Validation
    latency_ms: int
    prompt_template_version: str | None
    observation_hash: str
    requested_at: datetime

    def to_json(self) -> dict[str, Any]:
        usage: Mapping[str, int] | None = self.response.usage
        return {
            "attempt": self.attempt,
            "raw_response": self.response.raw_response,
            "validation": {
                "ok": self.validation.ok,
                "code": None if self.validation.code is None else str(self.validation.code),
                "feedback": self.validation.feedback,
            },
            "stop_reason": self.response.stop_reason,
            "usage": None if usage is None else dict(usage),
            "latency_ms": self.latency_ms,
            "cost_estimated_usd": self.response.cost_estimated_usd,
            "cost_reported_usd": self.response.cost_reported_usd,
            "prompt_template_version": self.prompt_template_version,
            "observation_hash": self.observation_hash,
            "requested_at": iso_utc(self.requested_at),
        }


@dataclass(frozen=True, slots=True)
class TurnOutcome:
    turn: int
    status: TurnStatus
    signed_action: SignedAction | None
    decisions: tuple[DecisionRecord, ...]
    failure: Mapping[str, str] | None

    def to_json(self) -> dict[str, Any]:
        return {
            "turn": self.turn,
            "status": self.status,
            "signed_action": None if self.signed_action is None else self.signed_action.to_json(),
            "decisions": [record.to_json() for record in self.decisions],
            "failure": None if self.failure is None else dict(self.failure),
        }


class TurnExecutor:
    def __init__(self, validator: MandateValidator, clock: Clock) -> None:
        self._validator = validator
        self._clock = clock

    async def run(
        self,
        *,
        turn: int,
        observation: Observation,
        observation_hash: str,
        policy: Policy,
        attempts: int,
        approval: ApprovedSession,
        signer: RunSigner,
        still_open: Callable[[], bool],
    ) -> TurnOutcome:
        """`still_open` is asked once more after the decision and before the signature: a run
        released while its policy was deciding signs nothing (ADR-048)."""
        if attempts < 1:
            raise ValueError("a turn needs at least one attempt")
        records: list[DecisionRecord] = []
        repair: Repair | None = None
        for attempt in range(1, attempts + 1):
            record = await self._attempt(attempt, observation, observation_hash, policy, repair)
            records.append(record)
            decision = record.validation.decision
            if decision is not None:
                if not still_open():
                    raise InvalidStateError(
                        "the run was released while this turn was deciding; nothing was signed",
                        state="released",
                        allowed_from=["approved"],
                    )
                action = sign_decision(
                    decision,
                    approval=approval,
                    sequence=observation.expected_sequence,
                    chain_time=observation.chain_time,
                    signer=signer,
                )
                return TurnOutcome(turn, "signed", action, tuple(records), None)
            repair = Repair(str(record.validation.code), record.validation.feedback or "")
        failure = {
            "code": "repair_exhausted",
            "detail": f"none of {attempts} attempt(s) passed validation",
        }
        return TurnOutcome(turn, "model_failed", None, tuple(records), failure)

    async def _attempt(
        self,
        attempt: int,
        observation: Observation,
        observation_hash: str,
        policy: Policy,
        repair: Repair | None,
    ) -> DecisionRecord:
        requested_at = self._clock.now()
        started = self._clock.monotonic()
        response = await policy.decide(observation, repair)
        latency_ms = max(0, round((self._clock.monotonic() - started) * 1000))
        return DecisionRecord(
            attempt=attempt,
            response=response,
            validation=self._validator.validate(response.raw_response, observation),
            latency_ms=latency_ms,
            prompt_template_version=policy.prompt_template_version,
            observation_hash=observation_hash,
            requested_at=requested_at,
        )


__all__ = [
    "Clock",
    "DecisionRecord",
    "SystemClock",
    "TurnExecutor",
    "TurnOutcome",
    "TurnStatus",
    "iso_utc",
]
