"""`Projector`: one run's public picture, computed from canonical rows each time it is asked for.

Nothing is cached between calls, so after a reorg the next projection simply no longer contains
what the reorg removed (A14). Every chain read goes through the repositories' canonical methods;
`history_for_export`, the one method that also returns invalidated rows, is never called here, and
a test reads this package's source to keep it that way (docs/test_strategy.md section 7).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from api.config import confirmation_threshold
from api.db.errors import NotFoundError
from api.db.protocols import Transactions
from api.db.records import Outcome
from api.projection.views import (
    TimelineContext,
    build_timeline,
    derive_outcome,
    latest_balances,
    session_view,
)


@dataclass(frozen=True, slots=True)
class RunProjection:
    """The public, chain-derived parts of the run resource (docs/api_contract.md section 2.2)."""

    run_id: uuid.UUID
    confirmation_threshold: int
    session: dict[str, Any] | None
    timeline: list[dict[str, Any]]
    balances: dict[str, dict[str, str]]
    #: The outcome the canonical terminal event supports at the threshold, if one does yet. The
    #: controller records it (ADR-052); this is the derivation, not the record.
    outcome: Outcome | None


class Projector:
    def __init__(self, transactions: Transactions, *, default_threshold: int = 1) -> None:
        self._transactions = transactions
        self._default_threshold = default_threshold

    async def project(self, run_id: uuid.UUID) -> RunProjection:
        async with self._transactions.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
            if run is None:
                raise NotFoundError(f"no run {run_id}")
            deployment = await uow.deployments.get(run.deployment_id)
            events = await uow.chain_events.canonical_for_run(run_id)
            outbox = await uow.outbox.list_for_run(run_id)
            actions = await uow.signed_actions.list_for_run(run_id)
            snapshots = await uow.balances.canonical_for_run(run_id)

        threshold = confirmation_threshold(run.public_config, self._default_threshold)
        context = TimelineContext(
            threshold=threshold,
            explorer_base_url=None if deployment is None else deployment.explorer_base_url,
        )
        return RunProjection(
            run_id=run_id,
            confirmation_threshold=threshold,
            session=session_view(events),
            timeline=build_timeline(events, outbox, actions, context),
            balances=latest_balances(snapshots),
            outcome=derive_outcome(events, threshold),
        )
