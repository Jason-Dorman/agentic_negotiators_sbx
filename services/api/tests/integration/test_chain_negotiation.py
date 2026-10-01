"""Stage 2.3's exit condition, first half: signed actions submitted through the relay are indexed
as canonical events and projected into the session view and the timeline (docs/build_plan.md).

Against Anvil and PostgreSQL. Each test negotiates a real session through the real contract: the
participants' messages are signed with their own keys (`api_chain`), and everything from the
signed action onwards — outbox row, broadcast, receipt, event, sentence, view — is the code under
test. The session view is compared with the contract's own `getSession`, and a settled session with
the A15 reconstruction tool, so neither the projection nor its test is the only witness.
"""

from __future__ import annotations

from typing import Any

from api_chain import (
    BASE_AMOUNT,
    BUYER_QUOTE,
    SELLER_BASE,
    AnvilChain,
    Backend,
    Session,
    export_errors,
)
from reconstruct import Reconstructor

from api.db import (
    ActionStatus,
    OutcomeKind,
    Party,
    PartyOrOperator,
    SnapshotStage,
    TokenRole,
    TxKind,
    TxStatus,
)


async def _negotiate_to_settlement(backend: Backend, session: Session) -> None:
    valid_until = backend.chain.chain_time() + 600
    first = session.offer(session.buyer, 1, 80_000_000, valid_until)
    await backend.act(session, first)
    await backend.poll()
    counter = session.offer(session.seller, 2, 96_000_000, valid_until)
    await backend.act(session, counter)
    await backend.poll()
    await backend.act(session, session.accept(session.buyer, 3, counter.digest))
    await backend.poll()


def _view_matches_contract(view: dict[str, Any], state: dict[str, Any]) -> None:
    for name in (
        "session_id",
        "config_hash",
        "buyer_address",
        "seller_address",
        "base_amount_minor",
        "expires_at_ts",
        "max_offers",
        "status",
        "sequence",
        "offer_count",
    ):
        assert view[name] == state[name], name
    active = view["active_offer"]
    expected_hash = (
        None if state["active_offer_hash"] == "0x" + "00" * 32 else state["active_offer_hash"]
    )
    assert (None if active is None else active["offer_hash"]) == expected_hash


async def test_a_settlement_is_relayed_indexed_and_projected(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await backend.open_session()
    report = await backend.poll()
    assert [event.event_name for event in report.indexed if event.run_id == session.run_id] == [
        "SessionOpened"
    ]

    await _negotiate_to_settlement(backend, session)
    projection = await backend.project(session)

    # The session view is the contract's own state, field by field.
    assert projection.session is not None
    _view_matches_contract(projection.session, chain.session_state(session.session_id))
    assert projection.session["status"] == "settled"
    assert projection.session["active_offer"] is None

    # The timeline: every event but the opening, in order, with the sentences of api_contract 4.
    assert [entry["sentence"] for entry in projection.timeline] == [
        "Buyer offers 80 mUSD for 10 mASSET.",
        "Seller counters at 96 mUSD for 10 mASSET.",
        "Buyer accepts 96 mUSD for 10 mASSET.",
        "Settled: 10 mASSET to buyer, 96 mUSD to seller.",
    ]
    assert [
        (entry["kind"], entry["actor"], entry["sequence"]) for entry in projection.timeline
    ] == [
        ("offer", "buyer", 1),
        ("offer", "seller", 2),
        ("accept", "buyer", 3),
        ("settle", "buyer", 3),
    ]
    assert all(entry["tx"]["status"] == "confirmed" for entry in projection.timeline)
    # Exactly the shapes the export will carry (api_contract section 5).
    assert export_errors("session", projection.session) == []
    for entry in projection.timeline:
        assert export_errors("timelineEntry", entry) == []

    # The outcome the canonical terminal event supports; the controller records it (ADR-052).
    assert projection.outcome is not None
    assert projection.outcome.kind == OutcomeKind.SETTLED
    assert projection.outcome.actor == PartyOrOperator.BUYER

    # Balances at the settlement block, and the deltas exactly the two signed legs (invariant 1).
    snapshots = {
        (snapshot.stage, snapshot.party, snapshot.token): int(snapshot.amount_minor)
        for snapshot in await _snapshots(backend, session)
    }
    for party, token, delta in (
        (Party.BUYER, TokenRole.BASE, BASE_AMOUNT),
        (Party.BUYER, TokenRole.QUOTE, -96_000_000),
        (Party.SELLER, TokenRole.BASE, -BASE_AMOUNT),
        (Party.SELLER, TokenRole.QUOTE, 96_000_000),
    ):
        before = snapshots[(SnapshotStage.PRE_SETTLEMENT, party, token)]
        after = snapshots[(SnapshotStage.POST_SETTLEMENT, party, token)]
        assert after - before == delta, (party, token)
    assert snapshots[(SnapshotStage.PRE_SETTLEMENT, Party.BUYER, TokenRole.QUOTE)] == BUYER_QUOTE
    assert snapshots[(SnapshotStage.PRE_SETTLEMENT, Party.SELLER, TokenRole.BASE)] == SELLER_BASE
    assert projection.balances == {
        "buyer": {"base_minor": str(BASE_AMOUNT), "quote_minor": str(BUYER_QUOTE - 96_000_000)},
        "seller": {"base_minor": str(SELLER_BASE - BASE_AMOUNT), "quote_minor": "96000000"},
    }

    # Every signed action confirmed through its own transaction, signed by the relay only.
    async with backend.database.unit_of_work() as uow:
        actions = await uow.signed_actions.list_for_run(session.run_id)
        outbox = await uow.outbox.list_for_run(session.run_id)
    assert [action.status for action in actions] == [ActionStatus.CONFIRMED] * 3
    assert [row.kind for row in outbox] == [
        TxKind.CREATE_SESSION,
        TxKind.RECORD_OFFER,
        TxKind.RECORD_OFFER,
        TxKind.ACCEPT_AND_SETTLE,
    ]
    assert all(row.status == TxStatus.CONFIRMED for row in outbox)
    assert [row.sender for row in outbox[1:]] == [backend.relay.relay_address] * 3


async def test_a_settlement_verifies_and_reconstructs(backend: Backend, chain: AnvilChain) -> None:
    session = await backend.open_session()
    await backend.poll()
    valid_until = chain.chain_time() + 600
    first = session.offer(session.buyer, 1, 80_000_000, valid_until)
    await backend.act(session, first)
    await backend.poll()
    counter = session.offer(session.seller, 2, 96_000_000, valid_until)
    await backend.act(session, counter)
    await backend.poll()
    await backend.act(session, session.accept(session.buyer, 3, counter.digest))
    report = await backend.poll()

    # Section 5.3: acceptance, settlement and exactly the two signed legs, in one receipt.
    (terminal,) = [item for item in report.terminal if item.run_id == session.run_id]
    assert terminal.event.event_name == "SettlementCompleted"
    assert terminal.settlement is not None
    assert terminal.settlement.problems == ()

    # Calldata is stored once per transaction, with the signature an agent made (A15).
    async with backend.database.unit_of_work() as uow:
        events = await uow.chain_events.canonical_for_run(session.run_id)
    settle_tx = [event for event in events if event.tx_hash == terminal.event.tx_hash]
    assert [event.calldata is not None for event in settle_tx] == [True, False]
    calldata = settle_tx[0].calldata
    assert calldata is not None
    assert calldata["decoded_function"] == "acceptAndSettle"
    assert calldata["decoded_args"]["acceptance"]["offerHash"] == str(counter.digest)

    # And the reconstruction tool, reading only the chain, agrees with what was indexed.
    result = Reconstructor(chain.w3, chain.manifest).run(str(session.session_id))
    assert result.ok, result.as_dict()


async def test_a_close_and_an_abort_are_projected(backend: Backend, chain: AnvilChain) -> None:
    closed = await backend.open_session()
    await backend.poll()
    valid_until = chain.chain_time() + 600
    await backend.act(closed, closed.offer(closed.buyer, 1, 80_000_000, valid_until))
    await backend.act(closed, closed.close(closed.seller, 2, 3))
    await backend.poll()
    projection = await backend.project(closed)
    assert projection.timeline[-1]["sentence"] == "Seller walks away (no further concession)."
    assert (projection.timeline[-1]["reason_code"], projection.timeline[-1]["reason"]) == (
        3,
        "no_further_concession",
    )
    assert projection.outcome is not None
    assert (projection.outcome.kind, projection.outcome.actor, projection.outcome.reason_code) == (
        OutcomeKind.CLOSED,
        PartyOrOperator.SELLER,
        3,
    )
    # Not settled, so the contract kept the active offer; the view mirrors it.
    assert projection.session is not None
    _view_matches_contract(projection.session, chain.session_state(closed.session_id))

    aborted = await backend.open_session()
    await backend.relay.submit_call(
        aborted.run_id,
        TxKind.ABORT_SESSION,
        backend.deployment.exchange_address,
        backend.codec.encode_abort_session(aborted.session_id, 1),
        as_operator=True,
    )
    await backend.poll()
    projection = await backend.project(aborted)
    assert [entry["sentence"] for entry in projection.timeline] == [
        "Operator aborted the session (operator request)."
    ]
    assert projection.timeline[0]["actor"] == "operator"
    assert projection.outcome is not None
    assert projection.outcome.kind == OutcomeKind.ABORTED


async def _snapshots(backend: Backend, session: Session) -> list[Any]:
    async with backend.database.unit_of_work() as uow:
        return list(await uow.balances.canonical_for_run(session.run_id))
