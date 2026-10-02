"""A14 at the run level: a reorganisation pauses the run with cause `reorg` (stage 2.4).

At confirmation threshold 2 on Anvil, the seller's offer is mined at depth 1 and then reverted
away with `evm_snapshot` / `evm_revert`, and different blocks are mined in its place. The indexer
writes the `chain.reorg` run event with its rewind (ADR-058); the controller, on its next poll,
pauses the run with cause `reorg` and has the relay reconcile, which sends the stored bytes again
(architecture 5.5). The offer lands in a new block and its turn completes while the run stays
paused; resume carries the negotiation on to its settlement.

The control: the same run before the revert is not paused, so the pause is the reorg's doing.
"""

from __future__ import annotations

import asyncio

import pytest
from api_chain import AnvilChain
from api_controller import ControllerHarness
from eth_typing import HexStr

from api.db import (
    ActionStatus,
    Database,
    OutcomeKind,
    Party,
    RunEventRecord,
    RunRecord,
    RunState,
    TurnState,
    TxKind,
    TxStatus,
)
from api.observation import ObservationBuilder, ObservationError


async def test_a_reorg_pauses_the_run_and_reconcile_resends(
    database: Database, chain: AnvilChain
) -> None:
    mining = {"on": True}

    async def sleep(seconds: float) -> None:
        if mining["on"]:
            await asyncio.to_thread(chain.mine)
        await asyncio.sleep(seconds)

    harness = ControllerHarness(database, chain, background=False, threshold=2, sleep=sleep)
    try:
        run = await harness.validated()
        await harness.controller.step(run.id)

        async def stepped(run: RunRecord) -> bool:
            return run.state == RunState.PAUSED and run.state_cause == "step_complete"

        run = await harness.tick_until(run.id, stepped)
        snapshot = chain.snapshot()

        mining["on"] = False

        async def seller_included(run: RunRecord) -> bool:
            async with database.unit_of_work() as uow:
                actions = await uow.signed_actions.list_for_run(run.id)
            return any(a.sequence == 2 and a.status == ActionStatus.INCLUDED for a in actions)

        await harness.controller.step(run.id)
        run = await harness.tick_until(run.id, seller_included)
        assert (run.state, run.state_cause) == (RunState.PAUSED, "step"), "control: no reorg yet"
        with pytest.raises(ObservationError) as unsettled:
            await ObservationBuilder(database, default_threshold=2).build(
                run.id, chain.chain_time()
            )
        assert unsettled.value.code == "not_confirmed", "no observation below the threshold"

        async with database.unit_of_work() as uow:
            (removed,) = [
                r
                for r in await uow.outbox.list_for_run(run.id)
                if r.kind == TxKind.RECORD_OFFER and r.status == TxStatus.INCLUDED
            ]
        chain.revert(snapshot)
        chain.mine(2)
        await harness.controller.driver.tick(run.id)
        # Reconcile sent the stored bytes again at once: the same transaction is mined on the new
        # fork, before any replacement could have been signed (ADR-050 waits three blocks).
        receipt = chain.w3.eth.get_transaction_receipt(HexStr(str(removed.tx_hash)))
        assert receipt["status"] == 1
        run = await harness.run(run.id)
        assert (run.state, run.state_cause) == (RunState.PAUSED, "reorg")

        async with database.unit_of_work() as uow:
            events = await uow.run_events.after(run.id, 0, 10_000)
            offer_rows = [
                r for r in await uow.outbox.list_for_run(run.id) if r.kind == TxKind.RECORD_OFFER
            ]
        reorg = [e for e in events if e.event_type == "chain.reorg"]
        assert reorg, "the indexer wrote the reorg with its rewind"
        acted = [
            e
            for e in events
            if e.event_type == "run.state"
            and e.data["state_cause"] == "reorg"
            and e.cursor > reorg[-1].cursor
        ]
        assert len(acted) == 1
        assert len({r.tx_hash for r in offer_rows}) == 2, "one row per offer: resent, not re-signed"

        await harness.controller.driver.tick(run.id)
        assert (
            len(
                [
                    e
                    for e in await _events(database, run)
                    if e.event_type == "run.state" and e.data["state_cause"] == "reorg"
                ]
            )
            == 1
        ), "a reorg is acted on once"

        # A second reorg while the run is paused for the first is acted on too — once.
        async with database.unit_of_work() as uow:
            await uow.run_events.append(
                run.id, "chain.reorg", {"from_block": 1, "to_block": 2, "invalidated_digests": []}
            )
        for _ in range(2):
            await harness.controller.driver.tick(run.id)
        acted_on = [
            e
            for e in await _events(database, run)
            if e.event_type == "run.state" and e.data["state_cause"] == "reorg"
        ]
        assert len(acted_on) == 2

        mining["on"] = True

        async def seller_confirmed(run: RunRecord) -> bool:
            async with database.unit_of_work() as uow:
                turns = await uow.turns.list_for_run(run.id)
            return any(t.party == Party.SELLER and t.state == TurnState.CONFIRMED for t in turns)

        run = await harness.tick_until(run.id, seller_confirmed)
        assert run.state == RunState.PAUSED, "the turn in flight completes; the run stays paused"

        await harness.controller.resume(run.id)

        async def finished(run: RunRecord) -> bool:
            return run.state in (RunState.TERMINAL, RunState.RECOVERY_REQUIRED)

        run = await harness.tick_until(run.id, finished)
        assert (run.state, run.outcome_kind) == (RunState.TERMINAL, OutcomeKind.SETTLED)
    finally:
        await harness.aclose()


async def _events(database: Database, run: RunRecord) -> list[RunEventRecord]:
    async with database.unit_of_work() as uow:
        return await uow.run_events.after(run.id, 0, 10_000)


async def test_a_reorg_of_a_confirmed_action_never_asks_for_a_second_decision(
    database: Database, chain: AnvilChain
) -> None:
    """The buyer's confirmed offer is reverted away and sent again; until it is confirmed again,
    no observation is built and the seller is not asked, and resume carries the run on with one
    action per sequence (stage 2.4 review)."""
    harness = ControllerHarness(database, chain, background=False)
    try:
        run = await harness.validated()
        await harness.controller.step(run.id)

        async def session_open(run: RunRecord) -> bool:
            return run.state == RunState.PAUSED

        await harness.tick_until(run.id, session_open)
        snapshot = chain.snapshot()

        async def stepped(run: RunRecord) -> bool:
            return run.state_cause == "step_complete"

        run = await harness.tick_until(run.id, stepped)
        chain.automine(False)
        chain.revert(snapshot)
        chain.mine(2)
        # A paused run is not polled: the operator's resume is what finds the reorg.
        await harness.controller.resume(run.id)
        await harness.controller.driver.tick(run.id)
        run = await harness.run(run.id)
        assert (run.state, run.state_cause) == (RunState.PAUSED, "reorg")
        assert chain.pooled() == 1, "the offer was sent again, and waits in the pool"

        await harness.controller.resume(run.id)
        asked = len(harness.agents.requests[Party.SELLER])
        for _ in range(4):
            await harness.controller.driver.tick(run.id)
        assert len(harness.agents.requests[Party.SELLER]) == asked, (
            "no decision on an unsettled chain"
        )

        chain.automine(True)
        chain.mine()

        async def finished(run: RunRecord) -> bool:
            return run.state in (RunState.TERMINAL, RunState.RECOVERY_REQUIRED)

        run = await harness.tick_until(run.id, finished)
        assert (run.state, run.outcome_kind) == (RunState.TERMINAL, OutcomeKind.SETTLED)
        async with database.unit_of_work() as uow:
            sequences = [a.sequence for a in await uow.signed_actions.list_for_run(run.id)]
        assert sorted(sequences) == list(range(1, len(sequences) + 1))
    finally:
        await harness.aclose()
        chain.automine(True)
