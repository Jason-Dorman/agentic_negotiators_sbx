"""A14 at the chain layer (docs/build_plan.md stage 2.3), against Anvil and PostgreSQL.

`evm_snapshot` before an action, the action indexed, `evm_revert`, and different blocks mined at the
same heights: the stored block hashes no longer match, so the indexer marks the removed events
non-canonical, clears the inclusions back to `submitted`, invalidates balance snapshots, and the
projection rolls back. The relay then reconciles the cleared transactions — reverted away, so
neither mined nor pooled, with their nonces free — by sending the stored bytes again, and the same
signed action lands in a new block as a new canonical row.

Anvil drops a reverted-away transaction from its pool entirely, which is what a node does with a
transaction from an orphaned block it never re-imports; the recovery path is the same.
"""

from __future__ import annotations

from api_chain import AnvilChain, Backend

from api.db import ActionStatus, SnapshotStage, TxStatus
from api.relay import Reconciliation


async def test_a14_a_reorg_before_the_threshold_rolls_the_projection_back(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await backend.open_session(threshold=2)
    await backend.poll()
    chain.mine()
    await backend.poll()  # SessionOpened at depth 2: confirmed

    snapshot = chain.snapshot()
    offer = session.offer(session.buyer, 1, 80_000_000, chain.chain_time() + 600)
    action = await backend.act(session, offer)
    report = await backend.poll()
    (indexed,) = [event for event in report.indexed if event.event_name == "OfferRecorded"]
    assert indexed.confirmations_at_index == 1  # below the threshold of 2
    projection = await backend.project(session)
    assert projection.session is not None
    assert projection.session["offer_count"] == 1
    assert projection.timeline[0]["tx"]["status"] == "included"

    chain.revert(snapshot)
    chain.mine(2)  # the heights are refilled by different, empty blocks
    report = await backend.poll()

    assert report.reorg is not None
    assert report.reorg.fork_block == indexed.block_number
    assert offer.digest in report.reorg.invalidated_digests
    assert session.run_id in report.reorg.run_ids
    async with backend.database.unit_of_work() as uow:
        history = await uow.chain_events.history_for_export(session.run_id)
        row = await uow.outbox.live_for_signed_action(action.id)
        recorded = await uow.signed_actions.get(action.id)
    (removed,) = [event for event in history if event.event_name == "OfferRecorded"]
    assert removed.canonical is False
    assert removed.invalidated_at is not None
    assert row is not None
    assert (row.status, row.block_number, row.block_hash) == (TxStatus.SUBMITTED, None, None)
    assert recorded is not None
    assert recorded.status == ActionStatus.SUBMITTED

    projection = await backend.project(session)
    assert projection.session is not None
    assert (projection.session["offer_count"], projection.session["active_offer"]) == (0, None)
    assert projection.timeline == []

    # Recovery: the stored transaction is sent again and lands in a new block.
    results = await backend.relay.reconcile(session.run_id)
    assert [result.outcome for result in results] == [Reconciliation.REBROADCAST]
    await backend.poll()
    chain.mine()
    await backend.poll()
    async with backend.database.unit_of_work() as uow:
        history = await uow.chain_events.history_for_export(session.run_id)
    offers = [event for event in history if event.event_name == "OfferRecorded"]
    assert [event.canonical for event in offers] == [False, True]
    assert offers[0].block_hash != offers[1].block_hash
    assert offers[1].confirmations_at_index == 2  # depth tracked up to the threshold
    projection = await backend.project(session)
    assert [entry["sentence"] for entry in projection.timeline] == [
        "Buyer offers 80 mUSD for 10 mASSET."
    ]
    assert projection.timeline[0]["tx"]["status"] == "confirmed"


async def test_a_reorg_invalidates_the_balance_snapshots_of_a_settlement(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await backend.open_session()
    await backend.poll()
    valid_until = chain.chain_time() + 600
    await backend.act(session, session.offer(session.buyer, 1, 80_000_000, valid_until))
    counter = session.offer(session.seller, 2, 96_000_000, valid_until)
    await backend.act(session, counter)
    await backend.poll()

    snapshot = chain.snapshot()
    await backend.act(session, session.accept(session.buyer, 3, counter.digest))
    await backend.poll()
    projection = await backend.project(session)
    assert projection.outcome is not None
    assert projection.balances["buyer"]["base_minor"] == "10000000"

    chain.revert(snapshot)
    chain.mine(2)
    report = await backend.poll()
    assert report.reorg is not None
    projection = await backend.project(session)
    assert projection.outcome is None
    assert projection.session is not None
    assert projection.session["status"] == "open"
    # The snapshots at and after the settlement block are gone; the one before it, at a block the
    # reorg did not touch, still stands, so the balances shown are the pre-settlement ones.
    async with backend.database.unit_of_work() as uow:
        snapshots = await uow.balances.history_for_export(session.run_id)
    assert {(item.stage, item.canonical) for item in snapshots} == {
        (SnapshotStage.PRE_SETTLEMENT, True),
        (SnapshotStage.POST_SETTLEMENT, False),
        (SnapshotStage.TERMINAL, False),
    }
    assert projection.balances == {
        "buyer": {"base_minor": "0", "quote_minor": "250000000"},
        "seller": {"base_minor": "25000000", "quote_minor": "0"},
    }

    # The same signed acceptance is recovered on the new fork, and settles there once.
    await backend.relay.reconcile(session.run_id)
    await backend.poll()
    projection = await backend.project(session)
    assert projection.session is not None
    assert projection.session["status"] == "settled"
    async with backend.database.unit_of_work() as uow:
        canonical = await uow.balances.canonical_for_run(session.run_id)
    assert {item.stage for item in canonical} == {
        SnapshotStage.PRE_SETTLEMENT,
        SnapshotStage.POST_SETTLEMENT,
        SnapshotStage.TERMINAL,
    }


async def test_a_reorg_that_removed_none_of_our_rows_is_still_rescanned(
    backend: Backend, chain: AnvilChain
) -> None:
    """The reorg check sees only what was stored; the scan must not rely on it to rewind."""
    session = await backend.open_session(duration=120)
    await backend.poll()
    snapshot = chain.snapshot()
    chain.mine(3)
    await backend.poll()  # the scan has now read past these heights, which hold nothing of ours

    chain.revert(snapshot)
    chain.advance_time(121)
    chain.send(
        chain.outsider,
        chain.exchange.functions.expireSession(bytes.fromhex(session.session_id[2:])),
    )
    report = await backend.poll()
    assert report.reorg is None  # nothing stored was removed
    assert [event.event_name for event in report.indexed if event.run_id == session.run_id] == [
        "SessionExpired"
    ]
