"""Ending a session early, made durable (ADR-067, ADR-068, ADR-070).

The stage 2.4 review found that the decision to end a session lived only in memory between closing
a failed turn and persisting its abort: one RPC error there, or a crash, and the next tick asked the
agent for a new decision and the run could settle. A termination is now recorded on the run in the
same unit of work as whatever decided it, and only the driver sends it. Each test here puts a fault
exactly where the old design lost the termination.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from api_chain import AnvilChain
from api_controller import ControllerHarness
from api_faults import ProcessKilledError
from web3 import Web3

from api.chain import BlockRef, FeeQuote, RpcUnavailableError, Web3ChainAdapter
from api.controller import InvalidStateError
from api.db import (
    Database,
    OutcomeKind,
    Party,
    RunRecord,
    RunState,
    TurnState,
    TxKind,
    TxStatus,
)
from api.relay import decode_raw_transaction

ABORT = bytes(Web3.keccak(text="abortSession(bytes32,uint8)")[:4])


class Faults(Web3ChainAdapter):
    """The real adapter with faults to arm: `head` unanswered N times, or the process dying at the
    abort's first RPC call."""

    def __init__(self, rpc_url: str) -> None:
        super().__init__(rpc_url)
        self.heads_to_fail = 0
        self.down = False
        self.die_before_abort = False

    async def head(self) -> BlockRef:
        if self.down or self.heads_to_fail > 0:
            self.heads_to_fail = max(self.heads_to_fail - 1, 0)
            raise RpcUnavailableError("ConnectionError: the RPC did not answer")
        return await super().head()

    async def fee_quote(self) -> FeeQuote:
        if self.die_before_abort:
            self.die_before_abort = False
            raise ProcessKilledError("killed before the abort was persisted")
        return await super().fee_quote()


class Clock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


async def finished(run: RunRecord) -> bool:
    return run.state in (RunState.TERMINAL, RunState.FAILED_SETUP, RunState.RECOVERY_REQUIRED)


async def stepped(run: RunRecord) -> bool:
    return run.state == RunState.PAUSED and run.state_cause == "step_complete"


def as_model_failure(then: Callable[[], None] = lambda: None, status: str = "model_failed") -> Any:
    """Rewrites the real agent's answer to a turn into a model failure: the same decision records
    over the same observation hash, every one refused."""

    def rewrite(path: str, body: dict[str, Any]) -> dict[str, Any]:
        if not path.endswith("/turn"):
            return body
        for decision in body["decisions"]:
            decision["validation"] = {"ok": False, "code": "below_reservation", "feedback": "f"}
        body.update(
            status=status,
            signed_action=None,
            failure={"code": "repair_exhausted", "detail": "none of 1 attempt(s) passed"},
        )
        then()
        return body

    return rewrite


@pytest.fixture
def faults(chain: AnvilChain) -> Faults:
    return Faults(chain.rpc_url)


@pytest.fixture
async def harness(
    database: Database, chain: AnvilChain, faults: Faults
) -> AsyncIterator[ControllerHarness]:
    harness = ControllerHarness(database, chain, background=False, adapter=faults)
    yield harness
    await harness.aclose()
    chain.automine(True)


async def test_an_rpc_blip_after_a_model_failure_does_not_lose_the_abort(
    harness: ControllerHarness, faults: Faults
) -> None:
    def arm() -> None:
        faults.heads_to_fail = 1

    harness.agents.rewrite[Party.BUYER] = as_model_failure(arm)
    run = await harness.validated()
    await harness.controller.start(run.id)
    run = await harness.tick_until(run.id, finished)

    assert (run.state, run.state_cause) == (RunState.TERMINAL, "model_failure")
    assert (run.outcome_kind, run.outcome_reason_code) == (OutcomeKind.ABORTED, 2)
    turns = [t for t in await _turns(harness, run)]
    assert [(t.party, t.state) for t in turns] == [(Party.BUYER, TurnState.MODEL_FAILED)]
    paths = harness.agents.requests[Party.BUYER]
    assert len([p for p in paths if p.endswith("/turn")]) == 1, "no second decision (FR-E2)"


async def test_a_crash_before_the_abort_is_sent_does_not_lose_it(
    database: Database, chain: AnvilChain, harness: ControllerHarness, faults: Faults
) -> None:
    def arm() -> None:
        faults.die_before_abort = True

    harness.agents.rewrite[Party.BUYER] = as_model_failure(arm)
    run = await harness.validated()
    await harness.controller.start(run.id)
    with pytest.raises(ProcessKilledError):
        await harness.tick_until(run.id, finished)
    assert (await harness.run(run.id)).termination_cause == "model_failure"

    later = datetime.now(UTC) + timedelta(minutes=5)
    restarted = ControllerHarness(
        database, chain, agents=harness.agents, background=False, clock=lambda: later
    )
    try:
        await restarted.controller.recover()
        run = await restarted.tick_until(run.id, finished)
    finally:
        await restarted.aclose()
    assert (run.outcome_kind, run.outcome_reason_code) == (OutcomeKind.ABORTED, 2)
    assert len(await _turns(harness, run)) == 1


async def test_a_budget_ceiling_aborts_with_reason_3(harness: ControllerHarness) -> None:
    harness.agents.rewrite[Party.BUYER] = as_model_failure(status="budget_exhausted")
    run = await harness.validated()
    await harness.controller.start(run.id)
    run = await harness.tick_until(run.id, finished)
    assert (run.outcome_kind, run.outcome_reason_code, run.state_cause) == (
        OutcomeKind.ABORTED,
        3,
        "budget_exhausted",
    )


# ---------------------------------------------------------------------------------------------
# A run with no session (ADR-067)
# ---------------------------------------------------------------------------------------------


async def test_abort_before_any_session_ends_the_run_in_failed_setup(
    harness: ControllerHarness,
) -> None:
    run = await harness.validated()
    await harness.controller.start(run.id)

    async def minting(run: RunRecord) -> bool:
        return any(row.kind == TxKind.MINT for row in await _rows(harness, run))

    run = await harness.tick_until(run.id, minting)
    assert run.state == RunState.PREPARING
    await harness.controller.abort(run.id)
    await harness.controller.driver.tick(run.id)
    run = await harness.run(run.id)
    assert (run.state, run.state_cause) == (RunState.FAILED_SETUP, "abort_requested")
    assert run.outcome_kind == OutcomeKind.PENDING, "no session, so no on-chain outcome"
    assert TxKind.CREATE_SESSION not in [row.kind for row in await _rows(harness, run)]
    async with harness.database.unit_of_work() as uow:
        assert await uow.leases.active_run_id() is None
    assert harness.agents.requests[Party.BUYER][-1].endswith("/release")


async def test_abort_from_a_setup_time_recovery_needs_no_rpc(
    harness: ControllerHarness, faults: Faults
) -> None:
    clock = Clock()
    harness.controller.driver._clock = clock  # the outage window's clock
    harness.controller.driver._rpc._clock = clock
    run = await harness.validated()
    await harness.controller.start(run.id)
    await harness.controller.driver.tick(run.id)
    faults.down = True
    await harness.controller.driver.tick(run.id)
    clock.now += timedelta(seconds=61)
    await harness.controller.driver.tick(run.id)
    run = await harness.run(run.id)
    assert (run.state, run.state_cause) == (RunState.RECOVERY_REQUIRED, "rpc_timeout")

    await harness.controller.abort(run.id)
    await harness.controller.driver.tick(run.id)
    run = await harness.run(run.id)
    assert (run.state, run.state_cause) == (RunState.FAILED_SETUP, "abort_requested")
    async with harness.database.unit_of_work() as uow:
        assert await uow.leases.active_run_id() is None


async def test_abort_while_the_session_is_being_created_aborts_it_once_it_opens(
    harness: ControllerHarness,
) -> None:
    run = await harness.validated()
    await harness.controller.start(run.id)

    async def creating(run: RunRecord) -> bool:
        return any(row.kind == TxKind.CREATE_SESSION for row in await _rows(harness, run))

    run = await harness.tick_until(run.id, creating)
    await harness.controller.abort(run.id)
    run = await harness.tick_until(run.id, finished)
    assert (run.state, run.state_cause) == (RunState.FAILED_SETUP, "abort_requested")
    assert (run.outcome_kind, run.outcome_reason_code) == (OutcomeKind.ABORTED, 1)


# ---------------------------------------------------------------------------------------------
# A termination that does not land (blocker 3 of the review)
# ---------------------------------------------------------------------------------------------


async def test_a_replaced_abort_whose_successor_reverts_falls_back_to_expiry(
    harness: ControllerHarness, chain: AnvilChain
) -> None:
    run = await harness.validated(session_duration_s=300)
    await harness.controller.step(run.id)
    run = await harness.tick_until(run.id, stepped)

    chain.automine(False)
    await harness.controller.abort(run.id)
    await harness.controller.driver.tick(run.id)
    (stuck,) = [row for row in await _rows(harness, run) if row.kind == TxKind.ABORT_SESSION]
    assert stuck.status == TxStatus.SUBMITTED
    chain.drop(str(stuck.tx_hash))  # accepted, then lost by the node: it will not be mined
    chain.mine(3)
    chain.rpc("evm_increaseTime", hex(400))  # past the deadline, at the next block
    chain.automine(True)
    await harness.controller.driver.tick(run.id)  # the relay replaces the stuck abort

    async def replaced(run: RunRecord) -> bool:
        statuses = {
            row.status for row in await _rows(harness, run) if row.kind == TxKind.ABORT_SESSION
        }
        return TxStatus.REPLACED in statuses and TxStatus.REVERTED in statuses

    await harness.tick_until(run.id, replaced, limit=20)
    run = await harness.tick_until(run.id, finished)
    assert (run.state, run.outcome_kind) == (RunState.TERMINAL, OutcomeKind.EXPIRED)
    assert run.state_cause == "abort_requested", "the termination's cause is kept (ADR-068)"
    async with harness.database.unit_of_work() as uow:
        statuses = [
            event.data["status"]
            for event in await uow.run_events.after(run.id, 0, 10_000)
            if event.event_type == "tx.status"
        ]
    assert "replaced" in statuses, "a replacement is a status transition (api_contract 3)"


# ---------------------------------------------------------------------------------------------
# Abort and decisions in progress (ADR-070)
# ---------------------------------------------------------------------------------------------


async def test_a_decision_that_arrives_after_an_abort_is_never_signed_into_the_run(
    harness: ControllerHarness,
) -> None:
    run = await harness.validated()
    agent = harness.backend.agents[Party.BUYER]
    original = agent.turn

    async def abort_while_deciding(run_id: Any, body: Any, *, timeout_s: float) -> Any:
        answer = await original(run_id, body, timeout_s=timeout_s)
        await harness.controller.abort(run_id)
        return answer

    agent.turn = abort_while_deciding  # type: ignore[method-assign]
    await harness.controller.start(run.id)
    run = await harness.tick_until(run.id, finished)
    assert (run.outcome_kind, run.outcome_reason_code) == (OutcomeKind.ABORTED, 1)
    async with harness.database.unit_of_work() as uow:
        assert await uow.signed_actions.list_for_run(run.id) == []
        decisions = await uow.decisions.list_for_run(run.id)
    (turn,) = await _turns(harness, run)
    assert turn.failure_code == "termination_requested"
    assert [d.authorized for d in decisions] == [False], "kept as a private record only"
    assert TxKind.RECORD_OFFER not in [row.kind for row in await _rows(harness, run)]


async def test_two_aborts_send_one_termination_and_nothing_resumes_meanwhile(
    harness: ControllerHarness,
) -> None:
    run = await harness.validated()
    await harness.controller.step(run.id)
    run = await harness.tick_until(run.id, stepped)

    await asyncio.gather(harness.controller.abort(run.id), harness.controller.abort(run.id))
    for operation in (harness.controller.resume, harness.controller.step, harness.controller.start):
        with pytest.raises(InvalidStateError) as refused:
            await operation(run.id)
        assert refused.value.details["termination"] == "abort_requested"
    run = await harness.tick_until(run.id, finished)
    aborts = [row for row in await _rows(harness, run) if row.kind == TxKind.ABORT_SESSION]
    assert len(aborts) == 1
    assert run.outcome_kind == OutcomeKind.ABORTED


async def test_a_fault_crossing_a_termination_does_not_overwrite_its_cause(
    harness: ControllerHarness, faults: Faults
) -> None:
    """ADR-066 and ADR-068: a refused session crossed by an RPC outage still ends `failed_setup`
    with `session_refused`."""
    clock = Clock()
    harness.controller.driver._rpc._clock = clock
    body = json.dumps(
        {"error": {"code": "session_mismatch", "message": "m", "details": {}, "request_id": "x"}}
    ).encode()

    def refuse(request: Any) -> Any:
        import httpx

        if request.url.path.endswith("/approve-session"):
            return httpx.Response(409, content=body)
        return None

    harness.agents.intercept[Party.SELLER] = refuse
    run = await harness.validated()
    await harness.controller.start(run.id)

    async def recorded(run: RunRecord) -> bool:
        return run.termination_cause is not None

    run = await harness.tick_until(run.id, recorded)
    faults.down = True
    await harness.controller.driver.tick(run.id)
    clock.now += timedelta(seconds=61)
    await harness.controller.driver.tick(run.id)
    assert (await harness.run(run.id)).state == RunState.RECOVERY_REQUIRED

    faults.down = False
    await harness.controller.abort(run.id)

    async def ended(run: RunRecord) -> bool:
        return run.state in (RunState.TERMINAL, RunState.FAILED_SETUP)

    run = await harness.tick_until(run.id, ended)
    assert (run.state, run.state_cause) == (RunState.FAILED_SETUP, "session_refused")
    assert (run.outcome_kind, run.outcome_reason_code) == (OutcomeKind.ABORTED, 4)


async def _rows(harness: ControllerHarness, run: RunRecord) -> list[Any]:
    async with harness.database.unit_of_work() as uow:
        return list(await uow.outbox.list_for_run(run.id))


async def _turns(harness: ControllerHarness, run: RunRecord) -> list[Any]:
    async with harness.database.unit_of_work() as uow:
        return sorted(await uow.turns.list_for_run(run.id), key=lambda t: t.turn)


__all__ = ["decode_raw_transaction"]
