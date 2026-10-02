"""The run events the controller appends (api_contract section 3), built in one place.

`run_events.data` is public and streamed as it is (data model section 7), so every builder here
takes public records only — the run, the outcome, the timeline the projection builds from canonical
rows, balance snapshots — and nothing from `mandate_versions`, `decisions` or `turns.observation`.
The turn's own events (`turn.started`, `turn.decision`, `turn.signed`) are the turn executor's, and
`chain.reorg` the indexer's (ADR-058).
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Final

from api.db.enums import OutcomeKind, Party, SnapshotStage, TokenRole
from api.db.records import BalanceSnapshotRecord, RunRecord
from negotiation_protocol import abort_reason_name, close_reason_name

PAUSE_NOTICE: Final = "Paused; offer and session expiry continue."


def outcome(run: RunRecord) -> dict[str, Any]:
    """The run resource's `outcome` (api_contract section 2.2)."""
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


def run_state(run: RunRecord) -> dict[str, Any]:
    return {"state": run.state.value, "state_cause": run.state_cause, "outcome": outcome(run)}


def notice(level: str, message: str) -> dict[str, Any]:
    return {"level": level, "message": message}


def balances(snapshots: Iterable[BalanceSnapshotRecord]) -> list[dict[str, Any]]:
    """One `balances` event per stage and block, each party's base, quote and ETH."""
    grouped: dict[tuple[SnapshotStage, int], dict[str, dict[str, str]]] = {}
    for snapshot in snapshots:
        stage = grouped.setdefault((snapshot.stage, snapshot.block_number), {})
        party = stage.setdefault(snapshot.party.value, {})
        party[_KEY[snapshot.token]] = str(int(snapshot.amount_minor))
    order = list(SnapshotStage)
    return [
        {
            "stage": stage.value,
            "buyer": parties.get(Party.BUYER.value, {}),
            "seller": parties.get(Party.SELLER.value, {}),
            "block_number": block,
        }
        for (stage, block), parties in sorted(
            grouped.items(), key=lambda item: (order.index(item[0][0]), item[0][1])
        )
    ]


_KEY: Final = {
    TokenRole.BASE: "base_minor",
    TokenRole.QUOTE: "quote_minor",
    TokenRole.ETH: "eth_wei",
}
