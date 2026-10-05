"""The public run resource of api_contract section 2.2, and the run summary the list route shows.

Built from public records only — the run, its deployment, its wallets' addresses, its turns' states,
the projection of its canonical events and its stored metrics — and never from `mandate_versions`,
`decisions` or `turns.observation` (data model section 7). The same resource is the `run` object of
the export, which `export.v1.json` closes with `additionalProperties: false` at every level, so a
private field added here fails the export's schema test rather than relying on a reviewer.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Final

from api.db.enums import OutcomeKind, Party, RunMode, TurnState
from api.db.errors import NotFoundError
from api.db.protocols import Transactions
from api.db.records import DeploymentRecord, RunRecord, TurnRecord, WalletRecord
from api.evidence.ports import ProjectionView, RunMetrics, RunProjections
from negotiation_protocol import abort_reason_name, close_reason_name

#: A party's public action, from the state of its open turn (architecture 6.2).
_CURRENT_ACTION: Final = {
    TurnState.OBSERVING: "deciding",
    TurnState.DECIDING: "deciding",
    TurnState.REPAIRING: "deciding",
    TurnState.SIGNING: "signing",
    TurnState.BROADCASTING: "awaiting_confirmation",
    TurnState.CONFIRMING: "awaiting_confirmation",
}

#: Shown on every screen and in every export (FR-U7).
_LABELS: Final = {"test_assets": True, "simulated_economics": True, "operator_abort_power": True}


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def outcome_view(run: RunRecord) -> dict[str, Any]:
    """The resource's `outcome`: the recorded economic outcome, with its reason by name."""
    reason: str | None = None
    if run.outcome_reason_code is not None:
        if run.outcome_kind == OutcomeKind.CLOSED:
            reason = close_reason_name(run.outcome_reason_code)
        elif run.outcome_kind == OutcomeKind.ABORTED:
            reason = abort_reason_name(run.outcome_reason_code)
    return {
        "kind": run.outcome_kind.value,
        "reason_code": run.outcome_reason_code,
        "reason": reason,
        "actor": None if run.outcome_actor is None else run.outcome_actor.value,
    }


def run_summary(run: RunRecord) -> dict[str, Any]:
    """One row of `GET /v1/runs`: identity, state and outcome, nothing derived from the chain."""
    return {
        "run_id": str(run.id),
        "name": run.name,
        "parent_run_id": None if run.parent_run_id is None else str(run.parent_run_id),
        "batch_id": None if run.batch_id is None else str(run.batch_id),
        "scenario_id": run.scenario_id,
        "deployment_id": run.deployment_id,
        "created_at": iso(run.created_at),
        "state": run.state.value,
        "state_cause": run.state_cause,
        "mode": run.mode.value,
        "outcome": outcome_view(run),
    }


def _party_view(
    run: RunRecord,
    party: Party,
    wallet: WalletRecord,
    turns: list[TurnRecord],
    projection: ProjectionView,
) -> dict[str, Any]:
    own = [turn for turn in turns if turn.party == party]
    open_turn = next((turn for turn in reversed(own) if turn.finished_at is None), None)
    balances = projection.balances.get(party.value, {})
    is_buyer = party == Party.BUYER
    return {
        "address": str(wallet.address),
        "policy": (run.buyer_policy if is_buyer else run.seller_policy).value,
        "model_id": run.buyer_model_id if is_buyer else run.seller_model_id,
        "effort": run.buyer_effort if is_buyer else run.seller_effort,
        # Before setup's snapshot the fresh wallet holds nothing (ADR-039).
        "balances": {
            "base_minor": balances.get("base_minor", "0"),
            "quote_minor": balances.get("quote_minor", "0"),
        },
        "current_action": "idle" if open_turn is None else _CURRENT_ACTION.get(open_turn.state),
        "decision_status": own[-1].state.value if own else None,
    }


class RunResources:
    def __init__(
        self, transactions: Transactions, projections: RunProjections, metrics: RunMetrics
    ) -> None:
        self._transactions = transactions
        self._projections = projections
        self._metrics = metrics

    async def run(self, run_id: uuid.UUID) -> RunRecord:
        async with self._transactions.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
        if run is None:
            raise NotFoundError(f"no run {run_id}")
        return run

    async def resource(self, run_id: uuid.UUID) -> dict[str, Any]:
        """`GET /v1/runs/{run_id}`: never a mandate, a private feedback, a prompt or a key."""
        async with self._transactions.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
            if run is None:
                raise NotFoundError(f"no run {run_id}")
            deployment = await uow.deployments.get(run.deployment_id)
            wallets = {wallet.party: wallet for wallet in await uow.wallets.list_for_run(run_id)}
            turns = await uow.turns.list_for_run(run_id)
            metrics = await uow.metrics.get(run_id)
        if metrics is None:
            # A run not yet measured — a draft, a run in setup — shows what is known of it: no
            # model called and no request made is zero, not unknown.
            metrics = await self._metrics.refresh(run_id)
        if deployment is None:
            raise NotFoundError(f"no deployment {run.deployment_id}")
        projection = await self._projections.project(run_id)
        return {
            "run_id": str(run.id),
            "name": run.name,
            "parent_run_id": None if run.parent_run_id is None else str(run.parent_run_id),
            "created_at": iso(run.created_at),
            "state": run.state.value,
            "state_cause": run.state_cause,
            "mode": run.mode.value,
            "outcome": outcome_view(run),
            "deployment": _deployment_view(deployment),
            "session": projection.session,
            "parties": {
                party.value: _party_view(run, party, wallets[party], turns, projection)
                for party in (Party.BUYER, Party.SELLER)
            },
            "timeline": projection.timeline,
            "metrics": self._metrics.summary(metrics),
            "labels": {**_LABELS, "fixture": run.mode == RunMode.FIXTURE},
        }


def _deployment_view(deployment: DeploymentRecord) -> dict[str, Any]:
    return {
        "deployment_id": deployment.deployment_id,
        "chain_id": deployment.chain_id,
        "exchange_address": str(deployment.exchange_address),
        "explorer_base_url": deployment.explorer_base_url,
    }
