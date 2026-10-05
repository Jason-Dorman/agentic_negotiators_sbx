"""The evidence export of api_contract section 5: one run as a self-contained JSON document.

Privacy-sensitive (docs/contributing.md section 1.2). The default export is public: it must validate
against `export.v1.json` with `private: null`, and that schema closes every object, so a mandate, a
raw model response or a validation feedback string anywhere in it is a schema failure. Private
inputs are added under `private` only when the route was asked for them with the observer reveal
header; credentials and keys are never stored, so no option can export them.

The chain sections include rows a reorg invalidated, marked `canonical: false`: the export is the
archive of record and must show a reorg that happened rather than hide it (data model 3.11). The
document makes zero model calls and sends zero transactions.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Any, Final

from api.db.enums import Party, TxStatus
from api.db.errors import NotFoundError
from api.db.protocols import Transactions
from api.db.records import (
    BalanceSnapshotRecord,
    ChainEventRecord,
    DecisionRecord,
    OutboxRecord,
    RunRecord,
    SignedActionRecord,
    TurnRecord,
)
from api.evidence import private
from api.evidence.ports import RunMetrics
from api.evidence.resource import RunResources, iso

EXPORT_VERSION: Final = "1"
DISCLAIMER: Final = (
    "Test assets and simulated economics. Public testnets may be reset; this export is the "
    "archive of record."
)
_MINED: Final = frozenset(
    {TxStatus.INCLUDED, TxStatus.CONFIRMED, TxStatus.FINALIZED, TxStatus.REVERTED}
)
_LIVE: Final = frozenset({TxStatus.PENDING, TxStatus.SUBMITTED})


def _per_party(buyer: str | None, seller: str | None) -> dict[str, str | None]:
    return {Party.BUYER.value: buyer, Party.SELLER.value: seller}


def reproducibility(run: RunRecord, protocol_version: str) -> dict[str, Any]:
    """Spec 11.3: what a reader needs to know to judge whether a rerun is comparable."""
    return {
        "scenario_id": run.scenario_id,
        "policy_versions": {party.value: run.policy_versions.get(party.value) for party in Party},
        "prompt_template_versions": {
            party.value: run.prompt_template_versions.get(party.value) for party in Party
        },
        "model_ids": _per_party(run.buyer_model_id, run.seller_model_id),
        "effort": _per_party(run.buyer_effort, run.seller_effort),
        # No provider seed is used in v0.1, and a seed would not guarantee identical output.
        "seed": None,
        "seed_supported": False,
        "protocol_version": protocol_version,
        "software_version": run.software_version,
    }


def _action_tx(rows: Iterable[OutboxRecord]) -> str | None:
    """The transaction that carried an action: the one mined, else the one still live."""
    rows = list(rows)
    for wanted in (_MINED, _LIVE):
        chosen = [row for row in rows if row.status in wanted]
        if chosen:
            return str(chosen[-1].tx_hash)
    return None


def signed_action(action: SignedActionRecord, rows: Iterable[OutboxRecord]) -> dict[str, Any]:
    """The authority chain, one entry per action: enough to recompute the digest and recover the
    signer without trusting either."""
    return {
        "sequence": action.sequence,
        "kind": action.kind.value,
        "typed_message": dict(action.typed_message),
        "digest": str(action.digest),
        "signer": str(action.signer),
        "signature": action.signature,
        "tx_hash": _action_tx(rows),
    }


def chain_event(event: ChainEventRecord) -> dict[str, Any]:
    return {
        "event": event.event_name,
        "block_number": event.block_number,
        "block_hash": str(event.block_hash),
        "tx_hash": str(event.tx_hash),
        "log_index": event.log_index,
        "canonical": event.canonical,
        "decoded": dict(event.decoded),
    }


def calldata(events: Iterable[ChainEventRecord]) -> list[dict[str, Any]]:
    """Each transaction's decoded calldata once, as the indexer stored it with its first event."""
    seen: set[str] = set()
    documents: list[dict[str, Any]] = []
    for event in events:
        if event.calldata is None or str(event.tx_hash) in seen:
            continue
        seen.add(str(event.tx_hash))
        stored = event.calldata
        documents.append(
            {
                "tx_hash": str(event.tx_hash),
                "to": stored["to"],
                "input": stored["input"],
                "decoded_function": stored["decoded_function"],
                "decoded_args": dict(stored["decoded_args"]),
            }
        )
    return documents


def balance_snapshot(snapshot: BalanceSnapshotRecord) -> dict[str, Any]:
    return {
        "stage": snapshot.stage.value,
        "party": snapshot.party.value,
        "token": snapshot.token.value,
        "amount_minor": str(int(snapshot.amount_minor)),
        "block_number": snapshot.block_number,
        "block_hash": str(snapshot.block_hash),
        "canonical": snapshot.canonical,
    }


class Exporter:
    def __init__(
        self,
        transactions: Transactions,
        resources: RunResources,
        metrics: RunMetrics,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._transactions = transactions
        self._resources = resources
        self._metrics = metrics
        self._clock = clock

    async def export(self, run_id: uuid.UUID, *, include_private: bool = False) -> dict[str, Any]:
        """The export document. `include_private` only behind the observer reveal header."""
        record = await self._metrics.refresh(run_id)
        resource = await self._resources.resource(run_id)
        async with self._transactions.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
            if run is None:
                raise NotFoundError(f"no run {run_id}")
            deployment = await uow.deployments.get(run.deployment_id)
            actions = await uow.signed_actions.list_for_run(run_id)
            outbox = await uow.outbox.list_for_run(run_id)
            events = await uow.chain_events.history_for_export(run_id)
            snapshots = await uow.balances.history_for_export(run_id)
            decisions = await uow.decisions.list_for_run(run_id)
            turns = await uow.turns.list_for_run(run_id)
        if deployment is None:
            raise NotFoundError(f"no deployment {run.deployment_id}")
        by_action: dict[uuid.UUID, list[OutboxRecord]] = {}
        for row in outbox:
            if row.signed_action_id is not None:
                by_action.setdefault(row.signed_action_id, []).append(row)
        document: dict[str, Any] = {
            "export_version": EXPORT_VERSION,
            "exported_at": iso(self._clock()),
            "includes_private": include_private,
            "disclaimer": DISCLAIMER,
            "run": resource,
            "deployment_manifest": dict(deployment.manifest),
            "reproducibility": reproducibility(run, deployment.protocol_version),
            "signed_actions": [
                signed_action(action, by_action.get(action.id, ()))
                for action in sorted(actions, key=lambda item: item.sequence)
            ],
            "chain_events": [chain_event(event) for event in events],
            "calldata": calldata(events),
            "balance_snapshots": [balance_snapshot(snapshot) for snapshot in snapshots],
            "decisions_public": private.public_decisions(decisions, turns),
            "metrics": self._metrics.public(record),
            "private": None,
        }
        if include_private:
            document["private"] = await self._private(run_id, decisions, turns)
        return document

    async def _private(
        self, run_id: uuid.UUID, decisions: list[DecisionRecord], turns: list[TurnRecord]
    ) -> dict[str, Any]:
        """**Private.** Both mandates, every decision in full, every observation, the evaluator."""
        async with self._transactions.unit_of_work() as uow:
            mandates = await uow.mandates.get_both(run_id)
        return {
            "mandates": {party.value: mandates[party].as_document() for party in Party},
            "decisions": private.decision_records(decisions, turns),
            "observations": private.observations(turns),
            "evaluator": await self._metrics.evaluator(run_id),
        }
