"""A06 at the run level: a duplicate action, and a second settlement attempt (stage 2.4).

The chain layer's A06 (stage 2.3) showed the relay resuming rather than re-signing, the database
refusing a second row for one digest, and two racing submitters producing one transaction. Here the
same guarantees hold for a run the controller drives: a turn advanced again while its action is in
flight — as a restarted driver would — neither asks for a new decision nor sends a second
transaction, and a settled run driven again, or its settlement submitted again, sends nothing.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from api_chain import AnvilChain
from api_controller import ControllerHarness

from api.controller import Progress
from api.db import ActionKind, Database, OutcomeKind, Party, RunRecord, RunState, TxKind
from api.turns import TurnStatus


@pytest.fixture
async def harness(database: Database, chain: AnvilChain) -> AsyncIterator[ControllerHarness]:
    harness = ControllerHarness(database, chain, background=False)
    yield harness
    await harness.aclose()
    chain.automine(True)


async def finished(run: RunRecord) -> bool:
    return run.state in (RunState.TERMINAL, RunState.RECOVERY_REQUIRED)


async def test_a_turn_advanced_again_in_flight_sends_nothing_more(
    harness: ControllerHarness, chain: AnvilChain
) -> None:
    run = await harness.validated()
    await harness.controller.step(run.id)

    async def stepped(run: RunRecord) -> bool:
        return run.state_cause == "step_complete"

    await harness.tick_until(run.id, stepped)
    chain.automine(False)
    await harness.controller.step(run.id)

    async def signed(run: RunRecord) -> bool:
        async with harness.database.unit_of_work() as uow:
            return len(await uow.signed_actions.list_for_run(run.id)) == 2

    run = await harness.tick_until(run.id, signed)
    asked = len(harness.agents.requests[Party.SELLER])
    for _ in range(3):
        step = await harness.controller.driver.turns.advance(run)
        assert step.status == TurnStatus.IN_FLIGHT
    assert len(harness.agents.requests[Party.SELLER]) == asked, "no new decision was asked for"
    async with harness.database.unit_of_work() as uow:
        offers = [r for r in await uow.outbox.list_for_run(run.id) if r.kind == TxKind.RECORD_OFFER]
    assert len(offers) == 2
    assert chain.pooled() == 1


async def test_a_settled_run_is_never_settled_twice(harness: ControllerHarness) -> None:
    run = await harness.validated()
    await harness.controller.start(run.id)
    run = await harness.tick_until(run.id, finished)
    assert run.outcome_kind == OutcomeKind.SETTLED

    async with harness.database.unit_of_work() as uow:
        before = await uow.outbox.list_for_run(run.id)
        actions = await uow.signed_actions.list_for_run(run.id)
        turns = await uow.turns.list_for_run(run.id)
    (accept,) = [a for a in actions if a.kind == ActionKind.ACCEPT]

    assert await harness.controller.driver.tick(run.id) == Progress.IDLE
    row = await harness.controller.driver.relay.submit_action(run.id, accept.id)
    assert row.tx_hash == next(r.tx_hash for r in before if r.signed_action_id == accept.id)
    async with harness.database.unit_of_work() as uow:
        assert len(await uow.outbox.list_for_run(run.id)) == len(before)
        assert len(await uow.turns.list_for_run(run.id)) == len(turns)
