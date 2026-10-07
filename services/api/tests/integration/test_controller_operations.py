"""The run controller's operations and failure paths, against Anvil, PostgreSQL and real agents.

Each test drives the run tick by tick in its own task (`background=False`), so a pause, a restart
or a fault lands at an exact point rather than wherever a background task happens to be. Failure
kinds stay distinguishable (architecture goal 4): every path here ends in its own state, cause and
outcome, and none of them looks like an economic result it is not.
"""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from api_chain import AnvilChain
from api_controller import ControllerHarness, scenario
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from api.controller import (
    AgentProvisioningError,
    AnotherRunActiveError,
    InvalidStateError,
    RunRequestError,
    TurnInProgressError,
)
from api.db import (
    ActionKind,
    Database,
    OutcomeKind,
    Party,
    PartyOrOperator,
    RunRecord,
    RunState,
    TurnState,
    TxKind,
)
from negotiation_protocol import Address, json_sha256


@pytest.fixture
async def harness(database: Database, chain: AnvilChain) -> AsyncIterator[ControllerHarness]:
    harness = ControllerHarness(database, chain, background=False, outage_limit_s=0.001)
    yield harness
    await harness.aclose()
    chain.automine(True)


def state(*states: RunState, cause: str | None = None) -> Callable[[RunRecord], Any]:
    async def done(run: RunRecord) -> bool:
        return run.state in states and (cause is None or run.state_cause == cause)

    return done


async def finished(run: RunRecord) -> bool:
    return run.state in (RunState.TERMINAL, RunState.FAILED_SETUP, RunState.RECOVERY_REQUIRED)


async def turns_of(harness: ControllerHarness, run: RunRecord) -> list[Any]:
    async with harness.database.unit_of_work() as uow:
        return sorted(await uow.turns.list_for_run(run.id), key=lambda turn: turn.turn)


def refuse(
    code: str, status: int, route: str, details: dict[str, Any] | None = None
) -> Callable[[httpx.Request], httpx.Response | None]:
    """An agent answering `route` with an error envelope instead of the real app."""

    def answer(request: httpx.Request) -> httpx.Response | None:
        if not request.url.path.endswith(f"/{route}"):
            return None
        body = {
            "error": {"code": code, "message": code, "details": details or {}, "request_id": "x"}
        }
        return httpx.Response(status, content=json.dumps(body).encode())

    return answer


# ---------------------------------------------------------------------------------------------
# Step, pause, resume
# ---------------------------------------------------------------------------------------------


async def test_step_prepares_takes_one_turn_and_pauses(harness: ControllerHarness) -> None:
    run = await harness.validated()
    await harness.controller.step(run.id)
    run = await harness.tick_until(run.id, state(RunState.PAUSED, cause="step_complete"))

    turns = await turns_of(harness, run)
    assert [(t.turn, t.party, t.state) for t in turns] == [(1, Party.BUYER, TurnState.CONFIRMED)]

    await harness.controller.step(run.id)
    with pytest.raises(TurnInProgressError):
        await harness.controller.step(run.id)
    run = await harness.tick_until(run.id, state(RunState.PAUSED, cause="step_complete"))
    assert [t.party for t in await turns_of(harness, run)] == [Party.BUYER, Party.SELLER]
    # Paused with nothing in flight: nothing is driven, so nothing is polled.
    assert not await harness.controller.driver.driven(run)


async def test_pause_holds_turns_and_resume_runs_to_settlement(harness: ControllerHarness) -> None:
    run = await harness.validated()
    await harness.controller.start(run.id)
    await harness.tick_until(run.id, state(RunState.RUNNING))

    async def two_turns(run: RunRecord) -> bool:
        return len(await turns_of(harness, run)) >= 2

    await harness.tick_until(run.id, two_turns)
    paused = await harness.controller.pause(run.id)
    assert (paused.state, paused.state_cause) == (RunState.PAUSED, "operator_pause")
    run = await harness.tick_until(run.id, lambda r: _idle(harness, r))
    count = len(await turns_of(harness, run))
    for _ in range(5):
        await harness.controller.driver.tick(run.id)
    assert len(await turns_of(harness, run)) == count, "no turn begins while paused"

    async with harness.database.unit_of_work() as uow:
        notices = [
            e for e in await uow.run_events.after(run.id, 0, 10_000) if e.event_type == "notice"
        ]
    assert notices[-1].data == {
        "level": "info",
        "message": "Paused; offer and session expiry continue.",
    }

    await harness.controller.resume(run.id)
    run = await harness.tick_until(run.id, finished)
    assert (run.state, run.outcome_kind) == (RunState.TERMINAL, OutcomeKind.SETTLED)


async def _idle(harness: ControllerHarness, run: RunRecord) -> bool:
    return not await harness.controller.driver.driven(run)


# ---------------------------------------------------------------------------------------------
# Abort and expiry
# ---------------------------------------------------------------------------------------------


async def test_abort_ends_the_session_as_an_operator_request(harness: ControllerHarness) -> None:
    run = await harness.validated()
    await harness.controller.step(run.id)
    run = await harness.tick_until(run.id, state(RunState.PAUSED, cause="step_complete"))

    await harness.controller.abort(run.id)
    run = await harness.tick_until(run.id, finished)
    assert run.state == RunState.TERMINAL
    assert (run.outcome_kind, run.outcome_reason_code) == (OutcomeKind.ABORTED, 1)
    assert run.outcome_actor == PartyOrOperator.OPERATOR
    assert run.state_cause == "abort_requested"
    assert (await turns_of(harness, run))[-1].state == TurnState.CONFIRMED


async def test_abort_after_the_deadline_records_expiry(
    harness: ControllerHarness, chain: AnvilChain
) -> None:
    run = await harness.validated(session_duration_s=300)
    await harness.controller.step(run.id)
    run = await harness.tick_until(run.id, state(RunState.PAUSED, cause="step_complete"))

    chain.advance_time(400)
    await harness.controller.abort(run.id)
    run = await harness.tick_until(run.id, finished)
    assert (run.state, run.outcome_kind) == (RunState.TERMINAL, OutcomeKind.EXPIRED)
    async with harness.database.unit_of_work() as uow:
        kinds = [row.kind for row in await uow.outbox.list_for_run(run.id)]
    assert TxKind.EXPIRE_SESSION in kinds and TxKind.ABORT_SESSION not in kinds


async def test_the_deadline_reached_while_running_is_expiry(
    harness: ControllerHarness, chain: AnvilChain
) -> None:
    run = await harness.validated(session_duration_s=300)
    await harness.controller.step(run.id)
    run = await harness.tick_until(run.id, state(RunState.PAUSED, cause="step_complete"))
    chain.advance_time(400)

    await harness.controller.resume(run.id)
    run = await harness.tick_until(run.id, finished)
    assert (run.outcome_kind, run.outcome_actor) == (OutcomeKind.EXPIRED, PartyOrOperator.ANYONE)
    assert run.state_cause == "session_deadline"
    assert len(await turns_of(harness, run)) == 1, "no decision is asked for past the deadline"


# ---------------------------------------------------------------------------------------------
# Agents: restart, outage, refusals
# ---------------------------------------------------------------------------------------------


async def test_a_restarted_agent_is_reprovisioned_and_reapproved(
    harness: ControllerHarness,
) -> None:
    """ADR-048: the seller's agent forgets the run; the controller restores it from what was
    stored — the same derived address — and the turn goes ahead."""
    run = await harness.validated()
    await harness.controller.step(run.id)
    run = await harness.tick_until(run.id, state(RunState.PAUSED, cause="step_complete"))
    async with harness.database.unit_of_work() as uow:
        before = await uow.wallets.get(run.id, Party.SELLER)

    harness.agents.restart(Party.SELLER)
    harness.agents.requests[Party.SELLER].clear()
    await harness.controller.step(run.id)
    run = await harness.tick_until(run.id, state(RunState.PAUSED, cause="step_complete"))

    paths = [path.rsplit("/", 1)[-1] for path in harness.agents.requests[Party.SELLER]]
    assert paths == ["turn", "provision", "approve-session", "turn"]
    turns = await turns_of(harness, run)
    assert [(t.party, t.state) for t in turns][-1] == (Party.SELLER, TurnState.CONFIRMED)
    async with harness.database.unit_of_work() as uow:
        actions = await uow.signed_actions.list_for_run(run.id)
    assert before is not None
    assert actions[-1].signer == before.address


async def test_an_unreachable_agent_past_the_limit_is_recovery_required(
    harness: ControllerHarness,
) -> None:
    """ADR-064: the same turn is asked again within the outage limit — here a millisecond — and
    then the run is a person's to look at, never an abort. Resume, once the agent answers, carries
    on."""
    run = await harness.validated()
    await harness.controller.step(run.id)
    run = await harness.tick_until(run.id, state(RunState.PAUSED, cause="step_complete"))

    def down(request: httpx.Request) -> httpx.Response | None:
        raise httpx.ConnectError("refused", request=request)

    harness.agents.intercept[Party.SELLER] = down
    await harness.controller.step(run.id)
    run = await harness.tick_until(run.id, finished)
    assert (run.state, run.state_cause) == (RunState.RECOVERY_REQUIRED, "agent_unavailable")
    assert run.outcome_kind == OutcomeKind.PENDING
    open_turn = (await turns_of(harness, run))[-1]
    assert open_turn.finished_at is None, "the turn stays open to be asked again"

    del harness.agents.intercept[Party.SELLER]
    recovered = await harness.controller.resume(run.id)
    assert (recovered.state, recovered.state_cause) == (RunState.PAUSED, "recovered")
    await harness.controller.resume(run.id)
    run = await harness.tick_until(run.id, finished)
    assert (run.state, run.outcome_kind) == (RunState.TERMINAL, OutcomeKind.SETTLED)
    assert open_turn.turn in [
        t.turn for t in await turns_of(harness, run) if t.state == TurnState.CONFIRMED
    ]


async def test_an_inconsistent_observation_five_times_is_recovery_required(
    harness: ControllerHarness,
) -> None:
    """ADR-046: rebuilt and retried; five refusals in a row are a defect, not an abort."""
    run = await harness.validated()
    harness.agents.intercept[Party.BUYER] = refuse(
        "observation_inconsistent", 422, "turn", {"fields": {"expected_sequence": "x"}}
    )
    await harness.controller.step(run.id)
    run = await harness.tick_until(run.id, finished)
    assert (run.state, run.state_cause) == (RunState.RECOVERY_REQUIRED, "observation_inconsistent")
    turns = await turns_of(harness, run)
    assert [t.failure_code for t in turns] == ["observation_inconsistent"] * 5
    assert len({t.turn for t in turns}) == 5, "each retry is a fresh observation"
    async with harness.database.unit_of_work() as uow:
        assert await uow.signed_actions.list_for_run(run.id) == []

    # Abort is the way out of a run that cannot go on: RECOVERY_REQUIRED -> TERMINAL.
    await harness.controller.abort(run.id)

    async def ended(run: RunRecord) -> bool:
        return run.state == RunState.TERMINAL

    run = await harness.tick_until(run.id, ended)
    assert (run.outcome_kind, run.outcome_reason_code) == (OutcomeKind.ABORTED, 1)


async def test_a_model_failure_aborts_with_reason_2(harness: ControllerHarness) -> None:
    run = await harness.validated()

    def failed(request: httpx.Request) -> httpx.Response | None:
        if not request.url.path.endswith("/turn"):
            return None
        sent = json.loads(request.content)
        document = {k: v for k, v in sent.items() if k not in ("turn", "deadline_at")}
        document["mandate"] = scenario("default-overlap")["buyer"]["mandate"]
        decision = {
            "attempt": 1,
            "raw_response": {"decision": {"action": "offer", "quote_amount_minor": "1"}},
            "validation": {"ok": False, "code": "below_reservation", "feedback": "private"},
            "stop_reason": None,
            "usage": None,
            "latency_ms": 0,
            "cost_estimated_usd": None,
            "cost_reported_usd": None,
            "prompt_template_version": None,
            "observation_hash": json_sha256(document),
            "requested_at": "2026-10-01T00:00:00.000Z",
        }
        body = {
            "turn": sent["turn"],
            "status": "model_failed",
            "signed_action": None,
            "decisions": [decision, {**decision, "attempt": 2}],
            "failure": {"code": "repair_exhausted", "detail": "none of 2 attempt(s) passed"},
        }
        return httpx.Response(200, content=json.dumps(body).encode())

    harness.agents.intercept[Party.BUYER] = failed
    await harness.controller.start(run.id)
    run = await harness.tick_until(run.id, finished)
    assert (run.state, run.state_cause) == (RunState.TERMINAL, "model_failure")
    assert (run.outcome_kind, run.outcome_reason_code) == (OutcomeKind.ABORTED, 2)
    (turn,) = await turns_of(harness, run)
    assert (turn.state, turn.failure_code) == (TurnState.MODEL_FAILED, "repair_exhausted")
    async with harness.database.unit_of_work() as uow:
        decisions = await uow.decisions.list_for_run(run.id)
        events = await uow.run_events.after(run.id, 0, 10_000)
    assert [(d.attempt, d.validation_ok, d.authorized) for d in decisions] == [
        (1, False, False),
        (2, False, False),
    ]
    published = json.dumps([e.data for e in events])
    assert "private" not in published and "below_reservation" not in published
    decided = [e.data for e in events if e.event_type == "turn.decision"]
    assert [(d["attempt"], d["status"]) for d in decided] == [(1, "invalid"), (2, "model_failed")]
    assert all(d["action"] is None and d["sentence"] is None for d in decided), (
        "a refused attempt was never an offer (FR-U8)"
    )


async def test_an_execution_failure_aborts_with_reason_4(
    harness: ControllerHarness, chain: AnvilChain
) -> None:
    """The seller's accept is mined after the offer it accepts has expired: `OfferExpired`, an
    execution failure, and the session aborted (spec 9.4)."""
    run = await harness.validated()
    await harness.controller.start(run.id)

    async def five_confirmed(run: RunRecord) -> bool:
        turns = await turns_of(harness, run)
        return len(turns) == 5 and turns[-1].state == TurnState.CONFIRMED

    await harness.tick_until(run.id, five_confirmed)
    chain.automine(False)

    async def accept_sent(run: RunRecord) -> bool:
        async with harness.database.unit_of_work() as uow:
            actions = await uow.signed_actions.list_for_run(run.id)
        return any(action.kind == ActionKind.ACCEPT for action in actions) and chain.pooled() > 0

    await harness.tick_until(run.id, accept_sent)
    chain.advance_time(700)
    chain.automine(True)
    run = await harness.tick_until(run.id, finished)
    assert (run.state, run.state_cause) == (RunState.TERMINAL, "execution_failure")
    assert (run.outcome_kind, run.outcome_reason_code) == (OutcomeKind.ABORTED, 4)
    turn = (await turns_of(harness, run))[-1]
    assert (turn.state, turn.failure_detail) == (TurnState.EXECUTION_FAILED, "OfferExpired")


async def test_a_session_an_agent_refuses_ends_in_failed_setup(
    harness: ControllerHarness,
) -> None:
    """ADR-066: aborted with `execution_failure`; the run `failed_setup`, `session_refused`."""
    run = await harness.validated()
    harness.agents.intercept[Party.SELLER] = refuse(
        "session_mismatch", 409, "approve-session", {"fields": {"expires_at_ts": {}}}
    )
    await harness.controller.start(run.id)
    run = await harness.tick_until(run.id, finished)
    assert (run.state, run.state_cause) == (RunState.FAILED_SETUP, "session_refused")
    assert (run.outcome_kind, run.outcome_reason_code) == (OutcomeKind.ABORTED, 4)
    async with harness.database.unit_of_work() as uow:
        assert await uow.leases.active_run_id() is None
        watched = await uow.runs.with_open_sessions(harness.deployment.deployment_id)
    assert run.id not in [r.id for r in watched], "a failed setup is not watched for good"


# ---------------------------------------------------------------------------------------------
# What is refused
# ---------------------------------------------------------------------------------------------


async def test_operations_refused_in_the_wrong_state(harness: ControllerHarness) -> None:
    draft = await harness.create()
    for operation in (harness.controller.start, harness.controller.step):
        with pytest.raises(InvalidStateError) as refused:
            await operation(draft.id)
        assert refused.value.details == {"state": "draft", "allowed_from": ["validated", "paused"]}
    for operation in (
        harness.controller.pause,
        harness.controller.resume,
        harness.controller.abort,
    ):
        with pytest.raises(InvalidStateError):
            await operation(draft.id)
    with pytest.raises(RunRequestError):
        await harness.controller.abort(draft.id, "because")

    report = await harness.controller.validate(draft.id)
    assert report.ok
    run = await harness.validated()
    await harness.controller.start(run.id)
    with pytest.raises(InvalidStateError):
        await harness.controller.validate(run.id)
    with pytest.raises(InvalidStateError):
        await harness.controller.pause(run.id)  # preparing
    with pytest.raises(AnotherRunActiveError):
        await harness.controller.start(draft.id)
    run = await harness.tick_until(run.id, finished)
    with pytest.raises(InvalidStateError):
        await harness.controller.resume(run.id)
    with pytest.raises(InvalidStateError):
        await harness.controller.abort(run.id)


async def test_a_validation_that_no_longer_passes_returns_the_run_to_draft(
    harness: ControllerHarness,
) -> None:
    run = await harness.validated()
    harness.agents.intercept[Party.BUYER] = lambda request: httpx.Response(
        503,
        content=b'{"error":{"code":"dependency_unavailable","message":"x","details":{},"request_id":null}}',
    )
    report = await harness.controller.validate(run.id)
    assert not report.ok
    failing = [check.check for check in report.checks if not check.ok]
    assert failing == ["buyer_agent"]
    run = await harness.run(run.id)
    assert (run.state, run.state_cause) == (RunState.DRAFT, "validation_failed")


async def test_the_validator_refuses_a_bad_threshold_and_foreign_bytecode(
    harness: ControllerHarness, chain: AnvilChain
) -> None:
    """ADR-059: a threshold that is not an integer of at least 1 is refused before the run starts;
    spec 8: bytecode that does not hash to the manifest's is refused, as is another chain."""
    from dataclasses import replace

    from api.chain import Web3ChainAdapter
    from api.validation import SetupValidator

    run = await harness.create()
    agents = harness.backend.agents
    validator = SetupValidator(
        harness.database,
        Web3ChainAdapter(chain.rpc_url),
        agents,
        harness.deployment,
        default_threshold=1,
    )
    for threshold in (0, "2", True, 1.5):
        bad = replace(run, public_config={**run.public_config, "confirmation_threshold": threshold})
        report = await validator.validate(bad)
        assert [c.check for c in report.checks if not c.ok] == ["confirmation_threshold"]

    swapped = {
        **harness.deployment.code_hashes,
        "exchange": harness.deployment.code_hashes["base_token"],
    }
    foreign = SetupValidator(
        harness.database,
        Web3ChainAdapter(chain.rpc_url),
        agents,
        replace(harness.deployment, code_hashes=swapped, chain_id=11155111),
        default_threshold=1,
    )
    report = await foreign.validate(run)
    assert sorted(c.check for c in report.checks if not c.ok) == ["chain_id", "exchange_code_hash"]
    published = repr(report.to_json())
    async with harness.database.unit_of_work() as uow:
        for mandate in (await uow.mandates.get_both(run.id)).values():
            assert mandate.instructions not in published
            amount = str(int(mandate.reservation_price_minor))
            assert re.search(rf"(?<![0-9]){amount}(?![0-9])", published) is None


async def test_a_run_an_agent_will_not_provision_is_not_kept(
    harness: ControllerHarness, raw_sql: AsyncConnection
) -> None:
    """Provisioning is inside the run's unit of work: a refusal leaves no run, no mandate and no
    wallet, and the agent that had provisioned it is told to release it."""
    harness.agents.intercept[Party.SELLER] = refuse(
        "validation_error", 422, "provision", {"fields": {"policy": "not offered"}}
    )
    with pytest.raises(RunRequestError) as refused:
        await harness.create()
    assert refused.value.details["agent_code"] == "validation_error"
    paths = [path.rsplit("/", 1)[-1] for path in harness.agents.requests[Party.BUYER]]
    assert paths == ["provision", "release"]
    counts = [
        (await raw_sql.execute(text(f"SELECT count(*) FROM {table}"))).scalar()
        for table in ("runs", "mandate_versions", "wallets")
    ]
    assert counts == [0, 0, 0]


# ---------------------------------------------------------------------------------------------
# From the stage 2.4 review
# ---------------------------------------------------------------------------------------------


async def test_an_unexpected_exception_is_recovery_required_not_an_undriven_run(
    harness: ControllerHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """docs/contributing.md 2.1: the top-level runner logs and marks the run recovery_required."""
    run = await harness.validated()
    await harness.controller.start(run.id)
    await harness.tick_until(run.id, state(RunState.RUNNING))

    async def broken(run: RunRecord) -> Any:
        raise RuntimeError("an unexpected fault")

    monkeypatch.setattr(harness.controller.driver.turns, "advance", broken)
    await harness.controller.drive(run.id)
    run = await harness.run(run.id)
    assert (run.state, run.state_cause) == (RunState.RECOVERY_REQUIRED, "internal_error")


async def test_a_stale_stored_observation_is_not_asked_again(
    harness: ControllerHarness, chain: AnvilChain
) -> None:
    """ADR-071: the seller's agent is down until the buyer's offer has expired; the stored
    observation still shows it active, so its turn closes and a fresh one is built."""
    run = await harness.validated()
    await harness.controller.step(run.id)
    run = await harness.tick_until(run.id, state(RunState.PAUSED, cause="step_complete"))

    def down(request: httpx.Request) -> httpx.Response | None:
        raise httpx.ConnectError("refused", request=request)

    harness.agents.intercept[Party.SELLER] = down
    harness.controller.driver._agent._limit = timedelta(hours=1)
    await harness.controller.step(run.id)
    await harness.controller.driver.tick(run.id)
    stored = (await turns_of(harness, run))[-1]
    assert stored.finished_at is None and stored.observation["active_offer"] is not None

    chain.advance_time(700)  # past the buyer's offer lifetime of 600 s
    del harness.agents.intercept[Party.SELLER]
    run = await harness.tick_until(run.id, state(RunState.PAUSED, cause="step_complete"))
    turns = await turns_of(harness, run)
    assert turns[1].failure_code == "observation_stale"
    assert turns[2].observation["active_offer"] is None
    assert turns[2].state == TurnState.CONFIRMED


async def test_a_restore_that_stopped_half_way_is_completed(harness: ControllerHarness) -> None:
    """An agent provisioned again but not approved — the approval did not get through — answers
    `provisioned`; it is restored again rather than sent to recovery."""
    run = await harness.validated()
    await harness.controller.step(run.id)
    run = await harness.tick_until(run.id, state(RunState.PAUSED, cause="step_complete"))
    harness.agents.restart(Party.SELLER)
    failures = {"left": 1}

    def approval_lost(request: httpx.Request) -> httpx.Response | None:
        if request.url.path.endswith("/approve-session") and failures["left"]:
            failures["left"] -= 1
            raise httpx.ReadTimeout("lost", request=request)
        return None

    harness.agents.intercept[Party.SELLER] = approval_lost
    await harness.controller.step(run.id)
    run = await harness.tick_until(run.id, state(RunState.PAUSED, cause="step_complete"))
    assert [t.party for t in await turns_of(harness, run)] == [Party.BUYER, Party.SELLER]


async def test_a_refused_attempt_before_the_signed_one_publishes_no_action(
    harness: ControllerHarness,
) -> None:
    """FR-U8 for a turn that ends signed: the refused first attempt's action stays private."""

    def refused_first(path: str, body: dict[str, Any]) -> dict[str, Any]:
        if path.endswith("/turn") and body["signed_action"] is not None:
            (signed,) = body["decisions"]
            refused = {
                **signed,
                "attempt": 1,
                "raw_response": {"decision": {"action": "offer", "quote_amount_minor": "1"}},
                "validation": {"ok": False, "code": "below_reservation", "feedback": "f"},
            }
            body["decisions"] = [refused, {**signed, "attempt": 2}]
        return body

    harness.agents.rewrite[Party.BUYER] = refused_first
    run = await harness.validated()
    await harness.controller.step(run.id)
    run = await harness.tick_until(run.id, state(RunState.PAUSED, cause="step_complete"))
    async with harness.database.unit_of_work() as uow:
        events = await uow.run_events.after(run.id, 0, 10_000)
        decisions = await uow.decisions.list_for_run(run.id)
    decided = [e.data for e in events if e.event_type == "turn.decision"]
    assert [(d["attempt"], d["status"]) for d in decided] == [(1, "invalid"), (2, "valid")]
    assert decided[0]["action"] is None and decided[0]["sentence"] is None
    assert decided[1]["action"] == {"action": "offer", "quote_amount_minor": "80000000"}
    assert sorted((d.attempt, d.authorized) for d in decisions) == [(1, False), (2, True)]


async def test_an_answer_for_another_turn_is_refused(harness: ControllerHarness) -> None:
    def other_turn(path: str, body: dict[str, Any]) -> dict[str, Any]:
        if path.endswith("/turn"):
            body["turn"] += 1
        return body

    harness.agents.rewrite[Party.BUYER] = other_turn
    run = await harness.validated()
    await harness.controller.step(run.id)
    run = await harness.tick_until(run.id, finished)
    assert (run.state, run.state_cause) == (RunState.RECOVERY_REQUIRED, "agent_wrong_turn")
    async with harness.database.unit_of_work() as uow:
        assert await uow.signed_actions.list_for_run(run.id) == []


async def test_an_operation_arriving_as_the_driver_exits_is_not_lost(
    database: Database, chain: AnvilChain, monkeypatch: pytest.MonkeyPatch
) -> None:
    harness = ControllerHarness(database, chain, background=True)
    try:
        run = await harness.validated()
        calls: list[int] = []

        async def drive(run_id: Any) -> None:
            calls.append(1)
            if len(calls) == 1:
                harness.controller._schedule(run_id)  # a step, arriving as this drive returns

        monkeypatch.setattr(harness.controller.driver, "drive", drive)
        harness.controller._schedule(run.id)
        await harness.settle()
        assert len(calls) == 2
    finally:
        await harness.aclose()


async def test_recover_releases_a_finished_runs_hold_on_the_active_run(
    harness: ControllerHarness,
) -> None:
    run = await harness.validated()
    await harness.controller.start(run.id)
    run = await harness.tick_until(run.id, finished)
    async with harness.database.unit_of_work() as uow:
        assert await uow.leases.active_run_id() is None
        assert not [lease for lease in await uow.leases.expired(datetime.max.replace(tzinfo=UTC))]
        assert await uow.leases.claim_active_run(run.id)  # left behind, as by a crash
    assert await harness.controller.recover(wait=False) == run.id
    async with harness.database.unit_of_work() as uow:
        assert await uow.leases.active_run_id() is None


async def test_unknown_scenarios_and_deployments_are_refused(harness: ControllerHarness) -> None:
    from api_controller import run_request

    await harness.prepare()
    with pytest.raises(RunRequestError) as refused:
        await harness.controller.create_run(
            run_request("default-overlap", harness.deployment.deployment_id, scenario_id="nope")
        )
    assert refused.value.details == {"fields": {"scenario_id": "unknown"}}
    with pytest.raises(RunRequestError) as refused:
        await harness.controller.create_run(run_request("default-overlap", "elsewhere"))
    assert refused.value.details == {"fields": {"deployment_id": "unknown"}}


async def test_the_validator_checks_each_agent_and_each_key_holder(
    harness: ControllerHarness, chain: AnvilChain
) -> None:
    from dataclasses import replace

    from api.chain import Web3ChainAdapter
    from api.db import PolicyKind
    from api.validation import SetupValidator

    run = await harness.create()

    def as_the_seller(request: httpx.Request) -> httpx.Response | None:
        if request.url.path == "/internal/health":
            health = {
                "status": "ok",
                "role": "seller",
                "instance": "agent-a",
                "policy_kinds": ["deterministic"],
                "model_ok": False,
                "model_mode": None,
                "signer_ok": False,
            }
            return httpx.Response(200, content=json.dumps(health).encode())
        return None

    harness.agents.intercept[Party.BUYER] = as_the_seller
    empty = Address("0x" + "11" * 20)
    validator = SetupValidator(
        harness.database,
        Web3ChainAdapter(chain.rpc_url),
        harness.backend.agents,
        replace(harness.deployment, relay_address=empty),
        default_threshold=1,
    )
    report = await validator.validate(replace(run, buyer_policy=PolicyKind.MODEL))
    failing = {check.check: check.detail for check in report.checks if not check.ok}
    assert failing == {
        "relay_eth_balance": "0 wei",
        "buyer_agent": "serves the seller; its signer did not load; cannot run a model policy",
        "buyer_agent_model_available": None,
    }


async def test_an_agent_whose_provisioning_answer_was_lost_is_released(
    harness: ControllerHarness,
) -> None:
    def lost(path: str, body: dict[str, Any]) -> dict[str, Any]:
        if path.endswith("/provision"):
            raise httpx.ReadTimeout("the answer was lost")
        return body

    harness.agents.rewrite[Party.SELLER] = lost
    with pytest.raises(AgentProvisioningError):
        await harness.create()
    seller = [path.rsplit("/", 1)[-1] for path in harness.agents.requests[Party.SELLER]]
    assert seller == ["provision", "release"]


async def test_a_state_change_decided_on_a_stale_read_is_refused(
    harness: ControllerHarness,
) -> None:
    """`RunStates.move` decides again under the row lock: a pause decided while the run was
    running does not pull it out of `recovery_required` (stage 2.4 review)."""
    run = await harness.validated()
    await harness.controller.step(run.id)
    run = await harness.tick_until(run.id, state(RunState.PAUSED, cause="step_complete"))
    await harness.controller.driver.states.recovery(run.id, "rpc_timeout")
    with pytest.raises(InvalidStateError) as refused:
        await harness.controller.driver.states.move(
            run.id, RunState.PAUSED, "operator_pause", expect=(RunState.RUNNING,), operation="pause"
        )
    assert refused.value.details["state"] == "recovery_required"
    assert (await harness.run(run.id)).state == RunState.RECOVERY_REQUIRED
