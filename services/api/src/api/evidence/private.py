"""**Private** views: both mandates, every decision record, the observations each agent was sent.

Privacy-sensitive (docs/contributing.md section 1.2). These leave the server only through the
routes that require `X-Observer-Reveal: true`, which log the access, and through a private export.
Nothing here is ever appended to `run_events`, streamed, or sent to an agent.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any, Final

from api.db.enums import Party, TurnState
from api.db.errors import NotFoundError
from api.db.protocols import Transactions
from api.db.records import DecisionRecord, MandateVersionRecord, TurnRecord
from api.evidence.ports import RunMetrics
from api.evidence.resource import iso

#: FR-U8. A decision record is an operational artefact, never an offer.
DECISION_LABEL: Final = "private_operational_record_not_an_authorized_offer"
_USAGE_KEYS: Final = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)
_ACTIONS: Final = frozenset({"offer", "accept", "walk_away"})
_USD: Final = Decimal("0.000001")


def usd(value: Decimal | None) -> str | None:
    return None if value is None else f"{value.quantize(_USD):f}"


def mandate_view(mandate: MandateVersionRecord) -> dict[str, Any]:
    return {
        "mandate_version_id": str(mandate.id),
        "version": mandate.version,
        **mandate.as_document(),
    }


def mandates_view(
    run_id: uuid.UUID,
    mandates: Mapping[Party, MandateVersionRecord],
    evaluator: dict[str, Any],
) -> dict[str, Any]:
    """`GET /v1/runs/{run_id}/mandates` (api_contract section 2.2)."""
    return {
        "run_id": str(run_id),
        **{party.value: mandate_view(mandates[party]) for party in Party if party in mandates},
        "evaluator": evaluator,
    }


def decision_status(decision: DecisionRecord, turn: TurnRecord | None, last: bool) -> str:
    """As the `turn.decision` event says it: the last attempt of a turn whose model failed is
    `model_failed`; otherwise `valid` or `invalid` by its validation."""
    if last and turn is not None and turn.state == TurnState.MODEL_FAILED:
        return "model_failed"
    return "valid" if decision.validation_ok else "invalid"


def _last_attempts(decisions: Iterable[DecisionRecord]) -> dict[uuid.UUID, int]:
    last: dict[uuid.UUID, int] = {}
    for decision in decisions:
        last[decision.turn_id] = max(last.get(decision.turn_id, 0), decision.attempt)
    return last


def _ordered(
    decisions: Iterable[DecisionRecord], turns: Mapping[uuid.UUID, TurnRecord]
) -> list[DecisionRecord]:
    def key(decision: DecisionRecord) -> tuple[int, int]:
        turn = turns.get(decision.turn_id)
        return (0 if turn is None else turn.turn, decision.attempt)

    return sorted(decisions, key=key)


def decision_records(
    decisions: Iterable[DecisionRecord], turns: Iterable[TurnRecord]
) -> list[dict[str, Any]]:
    """**Private.** Every attempt in full, raw response and validation feedback included
    (`GET /v1/runs/{run_id}/decisions`, and a private export's `decisions`)."""
    by_id = {turn.id: turn for turn in turns}
    decisions = list(decisions)
    last = _last_attempts(decisions)
    return [
        {
            "decision_id": str(decision.id),
            "turn": by_id[decision.turn_id].turn if decision.turn_id in by_id else None,
            "party": decision.party.value,
            "attempt": decision.attempt,
            "status": decision_status(
                decision, by_id.get(decision.turn_id), decision.attempt == last[decision.turn_id]
            ),
            "policy": decision.policy.value,
            "model_id": decision.model_id,
            "prompt_template_version": decision.prompt_template_version,
            "effort": decision.effort,
            "request_hash": decision.request_hash,
            "raw_response": decision.raw_response,
            "validation": {
                "ok": decision.validation_ok,
                "code": decision.validation_code,
                "feedback": decision.validation_feedback,
            },
            "stop_reason": decision.stop_reason,
            "usage": decision.usage,
            "cost_estimated_usd": usd(decision.cost_estimated_usd),
            "cost_reported_usd": usd(decision.cost_reported_usd),
            "latency_ms": decision.latency_ms,
            "requested_at": iso(decision.requested_at),
            "observation_hash": (
                by_id[decision.turn_id].observation_hash if decision.turn_id in by_id else None
            ),
            "authorized": decision.authorized,
            "label": DECISION_LABEL,
        }
        for decision in _ordered(decisions, by_id)
    ]


def _attempted_action(raw_response: Any) -> str | None:
    """The kind of move an attempt made, without its amount or reason: `offer`, `accept`,
    `walk_away`, or null for a response that was not a decision."""
    if not isinstance(raw_response, dict):
        return None
    decision = raw_response.get("decision")
    action = decision.get("action") if isinstance(decision, dict) else None
    return action if action in _ACTIONS else None


def _usage(usage: Mapping[str, Any] | None) -> dict[str, int] | None:
    if usage is None:
        return None
    return {key: int(usage[key]) for key in _USAGE_KEYS if isinstance(usage.get(key), int)}


def public_decisions(
    decisions: Iterable[DecisionRecord], turns: Iterable[TurnRecord]
) -> list[dict[str, Any]]:
    """The export's `decisions_public`: deliberately narrow — no response text, no amount, no
    feedback, which would quote the mandate it refused against (export.v1.json)."""
    by_id = {turn.id: turn for turn in turns}
    decisions = list(decisions)
    last = _last_attempts(decisions)
    return [
        {
            "turn": by_id[decision.turn_id].turn,
            "party": decision.party.value,
            "attempt": decision.attempt,
            "status": decision_status(
                decision, by_id.get(decision.turn_id), decision.attempt == last[decision.turn_id]
            ),
            "action": _attempted_action(decision.raw_response),
            "usage": _usage(decision.usage),
            "latency_ms": decision.latency_ms,
            "cost_estimated_usd": usd(decision.cost_estimated_usd),
            "cost_reported_usd": usd(decision.cost_reported_usd),
            "label": DECISION_LABEL,
        }
        for decision in _ordered(decisions, by_id)
        if decision.turn_id in by_id
    ]


def observations(turns: Iterable[TurnRecord]) -> list[dict[str, Any]]:
    """**Private.** Each observation as its own agent was sent it, its own mandate included."""
    return [
        {
            "turn": turn.turn,
            "party": turn.party.value,
            "expected_sequence": turn.expected_sequence,
            "observation_hash": turn.observation_hash,
            "observation": turn.observation,
        }
        for turn in sorted(turns, key=lambda item: item.turn)
    ]


class ObserverViews:
    """The two observer-only routes' documents. The route requires the reveal header and logs."""

    def __init__(self, transactions: Transactions, metrics: RunMetrics) -> None:
        self._transactions = transactions
        self._metrics = metrics

    async def mandates(self, run_id: uuid.UUID) -> dict[str, Any]:
        async with self._transactions.unit_of_work() as uow:
            if await uow.runs.get(run_id) is None:
                raise NotFoundError(f"no run {run_id}")
            mandates = await uow.mandates.get_both(run_id)
        return mandates_view(run_id, mandates, await self._metrics.evaluator(run_id))

    async def decisions(self, run_id: uuid.UUID) -> dict[str, Any]:
        async with self._transactions.unit_of_work() as uow:
            if await uow.runs.get(run_id) is None:
                raise NotFoundError(f"no run {run_id}")
            decisions = await uow.decisions.list_for_run(run_id)
            turns = await uow.turns.list_for_run(run_id)
        return {"decisions": decision_records(decisions, turns)}
