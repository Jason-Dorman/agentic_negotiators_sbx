"""What the controller refuses to believe: an agent's answer it can check and finds wrong, and a
chain report that does not support the outcome it would record.

A negotiation that goes well exercises none of these, which is why each has its own test: removing
any one of the checks leaves every settlement test green (docs/contributing.md section 3). Each leg
of the signed-action check is tested alone, the others holding — the tampered message is signed
again with the party's own derived key — because a tamper that also breaks the signature shows only
that some leg fails.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import replace
from typing import Any

import pytest
from api_chain import AnvilChain
from api_controller import ControllerHarness
from eth_typing import Hash32

from api.db import Database, OutcomeKind, Party, RunRecord, RunState, TurnState
from api.indexer import PollReport, RunProblem, SettlementCheck
from negotiation_protocol import Domain, Offer


@pytest.fixture
async def harness(database: Database, chain: AnvilChain) -> AsyncIterator[ControllerHarness]:
    harness = ControllerHarness(database, chain, background=False)
    yield harness
    await harness.aclose()


async def finished(run: RunRecord) -> bool:
    return run.state in (RunState.TERMINAL, RunState.FAILED_SETUP, RunState.RECOVERY_REQUIRED)


async def nothing_signed(harness: ControllerHarness, run: RunRecord) -> None:
    async with harness.database.unit_of_work() as uow:
        assert await uow.signed_actions.list_for_run(run.id) == []
        assert await uow.decisions.list_for_run(run.id) == []
        (turn,) = await uow.turns.list_for_run(run.id)
    assert turn.finished_at is not None and turn.state != TurnState.CONFIRMED


SECP256K1_N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


def _signed(
    harness: ControllerHarness, run_id: uuid.UUID, message: dict[str, Any]
) -> tuple[str, str]:
    """The digest and signature of an offer as the party's own key signs it: a tampered field
    that still carries a valid signature, so exactly one leg of the check can fail."""
    deployment = harness.deployment
    domain = Domain(deployment.chain_id, deployment.exchange_address)
    digest = Offer(
        bytes.fromhex(str(message["sessionId"])[2:]),
        bytes.fromhex(str(message["configHash"])[2:]),
        int(message["sequence"]),
        str(message["proposer"]),
        int(message["quoteAmount"]),
        int(message["validUntil"]),
    ).digest(domain)
    account = harness.agents.run_account(Party.BUYER, run_id, deployment.chain_id)
    signature = bytes(account.unsafe_sign_hash(Hash32(digest)).signature)
    return "0x" + digest.hex(), "0x" + signature.hex()


def _high_s(signature: str) -> str:
    """The same signature's malleable twin: it recovers to the same signer, and OpenZeppelin's
    ECDSA refuses it (protocol 8.1)."""
    raw = bytes.fromhex(signature[2:])
    r, s, v = raw[:32], int.from_bytes(raw[32:64], "big"), raw[64]
    return "0x" + (r + (SECP256K1_N - s).to_bytes(32, "big") + bytes([55 - v])).hex()


OTHER = "0x000000000000000000000000000000000000dEaD"

#: Each leg's tamper: a change to the typed message, re-signed with the party's own key...
RESIGNED: dict[str, Any] = {
    "sequence": lambda message: message.update(sequence=2),
    "session": lambda message: message.update(sessionId="0x" + "ab" * 32),
    "terms": lambda message: message.update(validUntil=int(message["validUntil"]) + 10**6),
    "message signer": lambda message: message.update(proposer=OTHER),
}
#: ...or a change to the signed action around it, which leaves the signature valid.
AROUND: dict[str, Any] = {
    "fields": lambda action: action["typed_message"].update(note="the codec would ignore this"),
    "signer": lambda action: action.update(signer=OTHER),
    "digest": lambda action: action.update(digest="0x" + "cd" * 32),
    "signature": lambda action: action.update(signature=_high_s(action["signature"])),
}


def _tamper(harness: ControllerHarness, leg: str) -> Any:
    def rewrite(path: str, body: dict[str, Any]) -> dict[str, Any]:
        action = body.get("signed_action")
        if not path.endswith("/turn") or action is None:
            return body
        if leg in RESIGNED:
            RESIGNED[leg](action["typed_message"])
            run_id = uuid.UUID(path.split("/")[3])
            action["digest"], action["signature"] = _signed(
                harness, run_id, action["typed_message"]
            )
        else:
            AROUND[leg](action)
        return body

    return rewrite


@pytest.mark.parametrize(
    "leg",
    ["sequence", "session", "terms", "message signer", "fields", "signer", "digest", "signature"],
)
async def test_a_signed_action_that_is_not_the_turns_is_refused(
    harness: ControllerHarness, leg: str
) -> None:
    """Each leg of the check alone: every other leg holds, the signature included."""
    harness.agents.rewrite[Party.BUYER] = _tamper(harness, leg)
    run = await harness.validated()
    await harness.controller.step(run.id)
    run = await harness.tick_until(run.id, finished)
    assert (run.state, run.state_cause) == (
        RunState.RECOVERY_REQUIRED,
        "agent_signed_action_mismatch",
    )
    await nothing_signed(harness, run)


async def test_an_untampered_action_passes_every_leg(harness: ControllerHarness) -> None:
    """The control: the same rewrite hook, changing nothing, lets the turn through."""
    harness.agents.rewrite[Party.BUYER] = lambda path, body: body
    run = await harness.validated()
    await harness.controller.step(run.id)

    async def stepped(run: RunRecord) -> bool:
        return run.state_cause == "step_complete" or await finished(run)

    run = await harness.tick_until(run.id, stepped)
    assert run.state_cause == "step_complete"


async def test_a_decision_over_another_observation_is_refused(harness: ControllerHarness) -> None:
    def other(path: str, body: dict[str, Any]) -> dict[str, Any]:
        if path.endswith("/turn"):
            for decision in body["decisions"]:
                decision["observation_hash"] = "0x" + "cd" * 32
        return body

    harness.agents.rewrite[Party.BUYER] = other
    run = await harness.validated()
    await harness.controller.step(run.id)
    run = await harness.tick_until(run.id, finished)
    assert (run.state, run.state_cause) == (
        RunState.RECOVERY_REQUIRED,
        "agent_observation_hash_mismatch",
    )
    await nothing_signed(harness, run)


class Reports:
    """The real indexer, with each poll's report passed through `change` before the controller
    reads it — the controller's half of ADR-052 and ADR-059, without forging the chain."""

    def __init__(self, indexer: Any, change: Any) -> None:
        self._indexer = indexer
        self._change = change

    async def poll(self) -> PollReport:
        report: PollReport = self._change(await self._indexer.poll())
        return report

    def __getattr__(self, name: str) -> Any:
        return getattr(self._indexer, name)


async def test_a_settlement_whose_receipt_check_fails_is_not_recorded(
    harness: ControllerHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(report: PollReport) -> PollReport:
        terminal = tuple(
            replace(t, settlement=SettlementCheck(t.event.tx_hash, ("a third transfer",)))
            if t.settlement is not None
            else t
            for t in report.terminal
        )
        return replace(report, terminal=terminal)

    driver = harness.controller.driver
    monkeypatch.setattr(driver, "_indexer", Reports(driver._indexer, failing))
    run = await harness.validated()
    await harness.controller.start(run.id)
    run = await harness.tick_until(run.id, finished)
    assert (run.state, run.state_cause) == (RunState.RECOVERY_REQUIRED, "settlement_check_failed")
    assert run.outcome_kind == OutcomeKind.PENDING, "no settlement without a passing check"


async def test_a_run_problem_is_recovery_required(
    harness: ControllerHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = await harness.validated()

    def problem(report: PollReport) -> PollReport:
        found = RunProblem(run.id, "session_opening_missing", "reported for this test")
        return replace(report, problems=(found,))

    await harness.controller.step(run.id)

    async def stepped(run: RunRecord) -> bool:
        return run.state_cause == "step_complete"

    run = await harness.tick_until(run.id, stepped)
    driver = harness.controller.driver
    monkeypatch.setattr(driver, "_indexer", Reports(driver._indexer, problem))
    await harness.controller.resume(run.id)
    run = await harness.tick_until(run.id, finished)
    assert (run.state, run.state_cause) == (RunState.RECOVERY_REQUIRED, "session_opening_missing")
