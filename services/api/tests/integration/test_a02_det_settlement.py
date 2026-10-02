"""A02: the deterministic policies settle a known feasible scenario end to end (stage 2.4).

Driven in-process through the run controller — create, validate, start — against Anvil, PostgreSQL
and two real agent applications. Nothing in the run is a stand-in: setup funds, mints and relays
the agents' own approvals, the agents decide and sign, the relay persists and broadcasts, the
indexer confirms, and the controller records the outcome from the canonical terminal event.

The deterministic pair on `default-overlap` goes 80, 108, 86.666666, 102, 93.333333, and the seller
accepts — the sequence the stage 2.2 exit test established with a stand-in for the backend.
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
    Party,
    PartyOrOperator,
    RunState,
    SnapshotStage,
    TokenRole,
    TurnState,
    TxKind,
    TxStatus,
)
from negotiation_protocol import Address


@pytest.fixture
async def harness(database: Database, chain: AnvilChain) -> AsyncIterator[ControllerHarness]:
    harness = ControllerHarness(database, chain)
    yield harness
    await harness.aclose()


async def test_the_deterministic_pair_settles_at_93_333333(harness: ControllerHarness) -> None:
    run = await harness.validated()
    await harness.controller.start(run.id)
    await harness.settle()

    run = await harness.run(run.id)
    assert run.state == RunState.TERMINAL
    assert run.outcome_kind == OutcomeKind.SETTLED
    assert run.outcome_actor == PartyOrOperator.SELLER
    assert run.outcome_reason_code is None

    async with harness.database.unit_of_work() as uow:
        actions = sorted(await uow.signed_actions.list_for_run(run.id), key=lambda a: a.sequence)
        turns = sorted(await uow.turns.list_for_run(run.id), key=lambda t: t.turn)
        wallets = {w.party: w for w in await uow.wallets.list_for_run(run.id)}
        snapshots = await uow.balances.canonical_for_run(run.id)
        events = await uow.run_events.after(run.id, 0, limit=10_000)
        active = await uow.leases.active_run_id()

    offers = [int(a.typed_message["quoteAmount"]) for a in actions if a.kind == ActionKind.OFFER]
    assert offers == [80_000_000, 108_000_000, 86_666_666, 102_000_000, 93_333_333]
    assert [a.kind for a in actions][-1] == ActionKind.ACCEPT
    assert [a.sequence for a in actions] == [1, 2, 3, 4, 5, 6]
    assert all(turn.state == TurnState.CONFIRMED for turn in turns)
    assert [turn.party for turn in turns] == [Party.BUYER, Party.SELLER] * 3
    for action, turn in zip(actions, turns, strict=True):
        assert action.signer == wallets[turn.party].address

    def holding(stage: SnapshotStage, party: Party, token: TokenRole) -> int:
        (snapshot,) = [
            s for s in snapshots if s.stage == stage and s.party == party and s.token == token
        ]
        return int(snapshot.amount_minor)

    pre, post = SnapshotStage.PRE_SETTLEMENT, SnapshotStage.POST_SETTLEMENT
    assert (
        holding(post, Party.BUYER, TokenRole.QUOTE) - holding(pre, Party.BUYER, TokenRole.QUOTE)
        == -93_333_333
    )
    assert (
        holding(post, Party.SELLER, TokenRole.QUOTE) - holding(pre, Party.SELLER, TokenRole.QUOTE)
        == 93_333_333
    )
    assert (
        holding(post, Party.BUYER, TokenRole.BASE) - holding(pre, Party.BUYER, TokenRole.BASE)
        == 10_000_000
    )
    assert holding(SnapshotStage.POST_SETUP, Party.SELLER, TokenRole.BASE) == 25_000_000

    counts = await table_counts(harness.database, run.id)
    assert all(count > 0 for count in counts.values()), counts
    assert active is None, "the active run is released once the run is terminal"

    types = [event.event_type for event in events]
    for expected in (
        "run.state",
        "turn.started",
        "turn.decision",
        "turn.signed",
        "tx.status",
        "chain.event",
        "balances",
    ):
        assert expected in types, expected
    # The events say what happened, once each: a timeline entry per canonical event, the turns in
    # order with their public actions, and the balances at the stages that were taken.
    entries = [e.data for e in events if e.event_type == "chain.event"]
    assert [(entry["kind"], entry["sequence"]) for entry in entries] == [
        ("offer", 1),
        ("offer", 2),
        ("offer", 3),
        ("offer", 4),
        ("offer", 5),
        ("accept", 6),
        ("settle", 6),
    ]
    started = [e.data for e in events if e.event_type == "turn.started"]
    assert started == [
        {"turn": n, "party": party.value, "expected_sequence": n}
        for n, party in enumerate([Party.BUYER, Party.SELLER] * 3, start=1)
    ]
    decided = [e.data["action"] for e in events if e.event_type == "turn.decision"]
    assert decided[0] == {"action": "offer", "quote_amount_minor": "80000000"}
    assert decided[-1]["action"] == "accept"
    stages = [e.data["stage"] for e in events if e.event_type == "balances"]
    assert stages == ["pre_setup", "post_setup", "pre_settlement", "post_settlement", "terminal"]
    final = [e for e in events if e.event_type == "run.state"][-1]
    assert final.data["state"] == "terminal"
    assert final.data["outcome"]["kind"] == "settled"


async def test_setup_funds_each_wallet_with_its_approvals_worst_case(
    harness: ControllerHarness,
) -> None:
    """ADR-065: the wallet's ETH is exactly the approval's gas limit times its fee cap."""
    run = await harness.validated()
    await harness.controller.step(run.id)
    await harness.settle()

    from api.relay import decode_raw_transaction

    async with harness.database.unit_of_work() as uow:
        rows = await uow.outbox.list_for_run(run.id)
        wallets = {w.party: w for w in await uow.wallets.list_for_run(run.id)}
    for party, wallet in wallets.items():
        (funding,) = [
            r
            for r in rows
            if r.kind == TxKind.FUND_ETH
            and decode_raw_transaction(r.raw_tx)["to"] == wallet.address
        ]
        (approval,) = [r for r in rows if r.kind == TxKind.APPROVE and r.sender == wallet.address]
        approve = decode_raw_transaction(approval.raw_tx)
        assert (
            decode_raw_transaction(funding.raw_tx)["value"]
            == approve["gas"] * approve["maxFeePerGas"]
        )
        assert 21_000 < approve["gas"] < 100_000, "the estimate plus a quarter, below the bound"
        assert approval.status in (TxStatus.CONFIRMED, TxStatus.FINALIZED), party
        assert Address(str(approve["to"])) == (
            harness.deployment.quote_token_address
            if party == Party.BUYER
            else harness.deployment.base_token_address
        )
