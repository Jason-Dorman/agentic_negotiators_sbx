"""ADR-063: an RPC outage that outlasts the limit is `RECOVERY_REQUIRED`, never a result.

The adapter here is the real one with a switch: while it is down every call raises
`RpcUnavailableError`, as an unanswered hosted RPC does. One failed poll is waited out; an outage
that lasts the whole limit moves the run to `RECOVERY_REQUIRED` with cause `rpc_timeout`, in
negotiation and — through the transition ADR-063 adds — in setup. The operator's resume, once the
RPC answers, reconciles and carries on: a negotiation to `paused`, a setup to `preparing`, from
where it stopped, without minting or funding twice.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from api_chain import AnvilChain
from api_controller import ControllerHarness

from api.chain import BlockRef, FeeQuote, RpcUnavailableError, Web3ChainAdapter
from api.controller import Progress
from api.db import Database, OutcomeKind, Party, RunRecord, RunState, SnapshotStage, TxKind


class Switchable(Web3ChainAdapter):
    def __init__(self, rpc_url: str) -> None:
        super().__init__(rpc_url)
        self.down = False

    async def head(self) -> BlockRef:
        if self.down:
            raise RpcUnavailableError("ConnectionError: the RPC did not answer")
        return await super().head()


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


async def finished(run: RunRecord) -> bool:
    return run.state in (RunState.TERMINAL, RunState.FAILED_SETUP, RunState.RECOVERY_REQUIRED)


def harness_for(
    database: Database, chain: AnvilChain, **kwargs: Any
) -> tuple[ControllerHarness, Switchable, Clock]:
    adapter, clock = Switchable(chain.rpc_url), Clock()
    harness = ControllerHarness(
        database, chain, background=False, adapter=adapter, clock=clock, outage_limit_s=60, **kwargs
    )
    return harness, adapter, clock


async def test_an_outage_within_the_limit_is_waited_out(
    database: Database, chain: AnvilChain
) -> None:
    harness, adapter, clock = harness_for(database, chain)
    try:
        run = await harness.validated()
        await harness.controller.start(run.id)
        adapter.down = True
        assert await harness.controller.driver.tick(run.id) == Progress.WAITING
        clock.now += timedelta(seconds=59)
        assert await harness.controller.driver.tick(run.id) == Progress.WAITING
        assert (await harness.run(run.id)).state == RunState.PREPARING
        adapter.down = False
        run = await harness.tick_until(run.id, finished)
        assert run.outcome_kind == OutcomeKind.SETTLED
    finally:
        await harness.aclose()


async def test_an_outage_in_negotiation_is_recovery_required(
    database: Database, chain: AnvilChain
) -> None:
    harness, adapter, clock = harness_for(database, chain)
    try:
        run = await harness.validated()
        await harness.controller.start(run.id)
        run = await harness.tick_until(run.id, _running)
        adapter.down = True
        await harness.controller.driver.tick(run.id)
        clock.now += timedelta(seconds=60)
        assert await harness.controller.driver.tick(run.id) == Progress.IDLE
        run = await harness.run(run.id)
        assert (run.state, run.state_cause) == (RunState.RECOVERY_REQUIRED, "rpc_timeout")
        assert run.outcome_kind == OutcomeKind.PENDING, "an outage is never a no-deal result"

        adapter.down = False
        recovered = await harness.controller.resume(run.id)
        assert (recovered.state, recovered.state_cause) == (RunState.PAUSED, "recovered")
        await harness.controller.resume(run.id)
        run = await harness.tick_until(run.id, finished)
        assert (run.state, run.outcome_kind) == (RunState.TERMINAL, OutcomeKind.SETTLED)
    finally:
        await harness.aclose()


async def test_an_outage_in_setup_is_recovered_and_setup_carries_on(
    database: Database, chain: AnvilChain
) -> None:
    harness, adapter, clock = harness_for(database, chain)
    try:
        run = await harness.validated()
        await harness.controller.start(run.id)

        async def funded(run: RunRecord) -> bool:
            async with database.unit_of_work() as uow:
                rows = await uow.outbox.list_for_run(run.id)
            return any(row.kind == TxKind.FUND_ETH for row in rows)

        run = await harness.tick_until(run.id, funded)
        assert run.state == RunState.PREPARING
        adapter.down = True
        await harness.controller.driver.tick(run.id)
        clock.now += timedelta(seconds=61)
        await harness.controller.driver.tick(run.id)
        run = await harness.run(run.id)
        assert (run.state, run.state_cause) == (RunState.RECOVERY_REQUIRED, "rpc_timeout")

        adapter.down = False
        resumed = await harness.controller.resume(run.id)
        assert (resumed.state, resumed.state_cause) == (RunState.PREPARING, "recovered")
        run = await harness.tick_until(run.id, _paused_or_done)
        assert (run.state, run.state_cause) == (RunState.PAUSED, "session_open")

        async with database.unit_of_work() as uow:
            rows = await uow.outbox.list_for_run(run.id)
            snapshots = await uow.balances.canonical_for_run(run.id)
        kinds = [row.kind for row in rows]
        assert kinds.count(TxKind.MINT) == 2 and kinds.count(TxKind.FUND_ETH) == 2
        assert kinds.count(TxKind.APPROVE) == 2 and kinds.count(TxKind.CREATE_SESSION) == 1
        assert any(s.stage == SnapshotStage.POST_SETUP for s in snapshots)

        await harness.controller.resume(run.id)
        run = await harness.tick_until(run.id, finished)
        assert run.outcome_kind == OutcomeKind.SETTLED
    finally:
        await harness.aclose()


async def _running(run: RunRecord) -> bool:
    return run.state == RunState.RUNNING


async def _paused_or_done(run: RunRecord) -> bool:
    return run.state in (RunState.PAUSED, RunState.TERMINAL, RunState.RECOVERY_REQUIRED)


class FeesUnanswered(Web3ChainAdapter):
    """Polls answer; the fee quote every send needs does not — an outage only the act phase meets,
    as an operator out of funds makes `estimate_gas` look to a hosted RPC."""

    def __init__(self, rpc_url: str) -> None:
        super().__init__(rpc_url)
        self.down = False

    async def fee_quote(self) -> FeeQuote:
        if self.down:
            raise RpcUnavailableError("ReadTimeout: the RPC did not answer")
        return await super().fee_quote()


async def test_failures_in_the_act_phase_count_toward_the_outage_limit(
    database: Database, chain: AnvilChain
) -> None:
    """The window is cleared only by a whole tick that succeeds, so a successful poll in front of
    a failing send does not reset it (stage 2.4 review)."""
    adapter, clock = FeesUnanswered(chain.rpc_url), Clock()
    harness = ControllerHarness(
        database, chain, background=False, adapter=adapter, clock=clock, outage_limit_s=60
    )
    try:
        run = await harness.validated()
        await harness.controller.start(run.id)
        assert await harness.controller.driver.tick(run.id) == Progress.ADVANCED  # pre_setup
        adapter.down = True  # minting is next, and it needs a fee quote
        assert await harness.controller.driver.tick(run.id) == Progress.WAITING
        clock.now += timedelta(seconds=30)
        assert await harness.controller.driver.tick(run.id) == Progress.WAITING
        clock.now += timedelta(seconds=31)
        assert await harness.controller.driver.tick(run.id) == Progress.IDLE
        run = await harness.run(run.id)
        assert (run.state, run.state_cause) == (RunState.RECOVERY_REQUIRED, "rpc_timeout")
    finally:
        await harness.aclose()


async def test_resume_begins_a_fresh_outage_window(database: Database, chain: AnvilChain) -> None:
    harness, adapter, clock = harness_for(database, chain)
    try:
        run = await harness.validated()
        await harness.controller.start(run.id)
        run = await harness.tick_until(run.id, _running)
        adapter.down = True
        await harness.controller.driver.tick(run.id)
        clock.now += timedelta(seconds=60)
        await harness.controller.driver.tick(run.id)
        adapter.down = False
        await harness.controller.resume(run.id)
        await harness.controller.resume(run.id)
        adapter.down = True
        assert await harness.controller.driver.tick(run.id) == Progress.WAITING
        assert (await harness.run(run.id)).state == RunState.RUNNING
    finally:
        await harness.aclose()


async def test_an_unanswering_agent_is_waited_for_within_the_limit_and_no_longer(
    database: Database, chain: AnvilChain
) -> None:
    """ADR-064: the same turn is asked again until the window has lasted the limit, and an answer
    in between starts the window afresh."""
    harness, _, clock = harness_for(database, chain)
    try:
        run = await harness.validated()
        await harness.controller.step(run.id)

        async def stepped(run: RunRecord) -> bool:
            return run.state_cause == "step_complete"

        run = await harness.tick_until(run.id, stepped)

        def down(request: httpx.Request) -> httpx.Response | None:
            raise httpx.ConnectError("refused", request=request)

        harness.agents.intercept[Party.SELLER] = down
        await harness.controller.step(run.id)
        assert await harness.controller.driver.tick(run.id) == Progress.WAITING
        clock.now += timedelta(seconds=59)
        assert await harness.controller.driver.tick(run.id) == Progress.WAITING
        asked = len(harness.agents.requests[Party.SELLER])
        assert asked >= 2, "the same turn was asked again"
        clock.now += timedelta(seconds=1)
        assert await harness.controller.driver.tick(run.id) == Progress.IDLE
        run = await harness.run(run.id)
        assert (run.state, run.state_cause) == (RunState.RECOVERY_REQUIRED, "agent_unavailable")
        async with database.unit_of_work() as uow:
            turns = await uow.turns.list_for_run(run.id)
        assert len(turns) == 2, "one turn, asked again, never a new one"
    finally:
        await harness.aclose()
