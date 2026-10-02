"""A03: buyer 100, seller 105 — no settlement, and the terminal reason recorded (stage 2.4).

The deterministic pair on `infeasible-clone` uses all eight offers — 80, 126, 86.666666, 119,
93.333333, 112, 100, 105 — and the buyer, with no opportunity left and the seller's 105 above its
bound, walks away with `terms_unacceptable` (protocol 13). The outcome is `closed`, never
`settled`, and no token moves.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from api_chain import AnvilChain
from api_controller import ControllerHarness, table_counts

from api.db import (
    ActionKind,
    Database,
    OutcomeKind,
    PartyOrOperator,
    RunState,
    SnapshotStage,
    TokenRole,
)


@pytest.fixture
async def harness(database: Database, chain: AnvilChain) -> AsyncIterator[ControllerHarness]:
    harness = ControllerHarness(database, chain)
    yield harness
    await harness.aclose()


async def test_the_infeasible_pair_closes_with_terms_unacceptable(
    harness: ControllerHarness,
) -> None:
    run = await harness.validated("infeasible-clone")
    await harness.controller.start(run.id)
    await harness.settle()

    run = await harness.run(run.id)
    assert run.state == RunState.TERMINAL
    assert run.outcome_kind != OutcomeKind.SETTLED
    assert run.outcome_kind == OutcomeKind.CLOSED
    assert run.outcome_reason_code == 1
    assert run.outcome_actor == PartyOrOperator.BUYER

    async with harness.database.unit_of_work() as uow:
        actions = sorted(await uow.signed_actions.list_for_run(run.id), key=lambda a: a.sequence)
        snapshots = await uow.balances.canonical_for_run(run.id)
    offers = [int(a.typed_message["quoteAmount"]) for a in actions if a.kind == ActionKind.OFFER]
    assert offers == [
        80_000_000,
        126_000_000,
        86_666_666,
        119_000_000,
        93_333_333,
        112_000_000,
        100_000_000,
        105_000_000,
    ]
    assert actions[-1].kind == ActionKind.CLOSE
    assert actions[-1].typed_message["reason"] == 1

    def tokens(stage: SnapshotStage) -> dict[tuple[str, str], int]:
        return {
            (s.party.value, s.token.value): int(s.amount_minor)
            for s in snapshots
            if s.stage == stage and s.token != TokenRole.ETH
        }

    assert tokens(SnapshotStage.TERMINAL) == tokens(SnapshotStage.POST_SETUP), "no token moved"
    counts = await table_counts(harness.database, run.id)
    assert all(count > 0 for count in counts.values()), counts
