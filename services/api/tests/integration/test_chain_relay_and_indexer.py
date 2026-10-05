"""The relay's gas replacement (ADR-050) and the indexer's other duties, against Anvil.

- **Replacement.** A transaction is held out of blocks by a block gas limit below its own gas, so
  it stays pooled while blocks are mined: stuck, exactly as a transaction priced under the market
  is. After `replace_after_blocks` the relay re-signs it at the same nonce with both fee caps raised
  by an eighth; the original is `replaced`, the successor is linked to it, and only one of the two
  can ever be mined. The race the other way — the replaced original mined after all — is staged by
  dropping the successor from the pool and resending the original.
- **Execution failure** (ADR-051, ADR-054). An action the node predicts will revert is still
  broadcast, at the fallback gas limit; its receipt has status 0, the revert is decoded to its
  protocol error, and the timeline carries the failure as its own entry.
- **The log scan** finds what this backend did not send: an expiry called by someone else.
- **Restart idempotency** and **finality**: a new indexer rescans from `start_block` and records
  nothing twice; a transaction is `finalized` only once the RPC's finalized head covers it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from typing import Any, cast

from api_chain import AnvilChain, Backend, Session, export_errors

from api.config import IndexerPolicy, RelayPolicy
from api.db import (
    ActionStatus,
    OutboxRecord,
    OutcomeKind,
    PartyOrOperator,
    RunState,
    TxKind,
    TxStatus,
)
from api.indexer import Indexer
from api.projection import TimelineSentences
from api.relay import Reconciliation, decode_raw_transaction

LOW_BLOCK_GAS_LIMIT = 50_000


async def _stuck_offer(backend: Backend, chain: AnvilChain, blocks: int) -> Session:
    """An offer broadcast and then held out of `blocks` mined blocks."""
    session = await backend.open_session()
    await backend.poll()
    chain.automine(False)
    await backend.act(
        session, session.offer(session.buyer, 1, 80_000_000, chain.chain_time() + 600)
    )
    chain.block_gas_limit(LOW_BLOCK_GAS_LIMIT)
    chain.mine(blocks)
    chain.block_gas_limit(30_000_000)  # a replacement must fit a block to be accepted
    return session


async def _stuck_row(backend: Backend, session: Session) -> OutboxRecord:
    async with backend.database.unit_of_work() as uow:
        (row,) = [
            row
            for row in await uow.outbox.list_for_run(session.run_id)
            if row.signed_action_id is not None
        ]
    return row


async def test_a_stuck_transaction_is_replaced_at_the_same_nonce_with_higher_fees(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await _stuck_offer(backend, chain, blocks=2)
    assert await backend.relay.replace_stuck(session.run_id) == []  # two blocks: not yet

    chain.block_gas_limit(LOW_BLOCK_GAS_LIMIT)
    chain.mine()  # the third, still without it
    chain.block_gas_limit(30_000_000)
    original = await _stuck_row(backend, session)
    (successor,) = await backend.relay.replace_stuck(session.run_id)
    async with backend.database.unit_of_work() as uow:
        replaced = await uow.outbox.get(original.id)
    assert replaced is not None
    assert replaced.status == TxStatus.REPLACED
    assert successor.replaces_id == original.id
    assert (successor.sender, successor.nonce) == (original.sender, original.nonce)
    assert successor.signed_action_id == original.signed_action_id
    assert successor.status == TxStatus.SUBMITTED
    old, new = decode_raw_transaction(original.raw_tx), decode_raw_transaction(successor.raw_tx)
    assert new["maxFeePerGas"] == -(-old["maxFeePerGas"] * 9 // 8)
    assert new["maxPriorityFeePerGas"] == -(-old["maxPriorityFeePerGas"] * 9 // 8)
    assert (new["to"], new["data"], new["gas"]) == (old["to"], old["data"], old["gas"])

    chain.mine()
    await backend.poll()
    async with backend.database.unit_of_work() as uow:
        rows = {row.id: row for row in await uow.outbox.list_for_run(session.run_id)}
        action = await uow.signed_actions.get(original.signed_action_id)  # type: ignore[arg-type]
        events = await uow.chain_events.canonical_for_run(session.run_id)
    assert rows[successor.id].status == TxStatus.CONFIRMED
    assert rows[original.id].status == TxStatus.REPLACED
    assert action is not None
    assert action.status == ActionStatus.CONFIRMED
    assert [event.event_name for event in events].count("OfferRecorded") == 1


async def test_the_replaced_original_can_still_be_the_one_mined(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await _stuck_offer(backend, chain, blocks=3)
    run_id = session.run_id
    original = await _stuck_row(backend, session)
    (successor,) = await backend.relay.replace_stuck(run_id)
    chain.drop(successor.tx_hash)
    chain.w3.eth.send_raw_transaction(original.raw_tx)
    chain.mine()

    await backend.poll()
    async with backend.database.unit_of_work() as uow:
        rows = {row.id: row for row in await uow.outbox.list_for_run(run_id)}
    assert rows[original.id].status == TxStatus.CONFIRMED
    assert rows[successor.id].status == TxStatus.DROPPED
    async with backend.database.unit_of_work() as uow:
        action = await uow.signed_actions.get(original.signed_action_id)  # type: ignore[arg-type]
    assert action is not None
    assert action.status == ActionStatus.CONFIRMED


async def test_replacement_stops_at_the_fee_ceiling(backend: Backend, chain: AnvilChain) -> None:
    session = await _stuck_offer(backend, chain, blocks=3)
    row = await _stuck_row(backend, session)
    ceiling = decode_raw_transaction(row.raw_tx)["maxFeePerGas"]
    capped = Backend(backend.database, chain, relay_policy=RelayPolicy(max_fee_per_gas_wei=ceiling))
    assert await capped.relay.replace_stuck(session.run_id) == []


async def test_an_action_the_node_predicts_will_revert_is_broadcast_and_recorded_as_a_failure(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await backend.open_session()
    await backend.poll()
    offer = session.offer(session.buyer, 1, 80_000_000, chain.chain_time() + 600)
    await backend.act(session, offer)
    balances = chain.token_balances(session.buyer.address)
    # The buyer accepting its own offer: the contract refuses it with SelfAcceptance.
    action = await backend.act(session, session.accept(session.buyer, 2, offer.digest))
    report = await backend.poll()

    (reverted,) = report.reverted
    assert reverted.signed_action_id == action.id
    assert (reverted.status, reverted.last_error) == (TxStatus.REVERTED, "SelfAcceptance")
    assert reverted.sentence == "Transaction reverted: SelfAcceptance. No trade occurred."
    assert decode_raw_transaction(reverted.raw_tx)["gas"] == RelayPolicy().fallback_gas_limit
    async with backend.database.unit_of_work() as uow:
        recorded = await uow.signed_actions.get(action.id)
    assert recorded is not None
    assert (recorded.status, recorded.revert_error) == (ActionStatus.REVERTED, "SelfAcceptance")
    assert chain.token_balances(session.buyer.address) == balances

    projection = await backend.project(session)
    failure = projection.timeline[-1]
    assert (failure["kind"], failure["actor"], failure["sequence"]) == (
        "execution_failure",
        "buyer",
        2,
    )
    assert failure["sentence"] == "Transaction reverted: SelfAcceptance. No trade occurred."
    assert failure["tx"]["status"] == "reverted"
    assert export_errors("timelineEntry", failure) == []  # the kind ADR-051 added

    # Spec 9.4: an execution failure is followed by an operator abort, reason 4.
    await backend.relay.submit_call(
        session.run_id,
        TxKind.ABORT_SESSION,
        backend.deployment.exchange_address,
        backend.codec.encode_abort_session(session.session_id, 4),
        as_operator=True,
    )
    await backend.poll()
    projection = await backend.project(session)
    assert (
        projection.timeline[-1]["sentence"] == "Operator aborted the session (execution failure)."
    )
    assert projection.outcome is not None
    assert (projection.outcome.kind, projection.outcome.reason_code) == (OutcomeKind.ABORTED, 4)


async def test_an_expiry_someone_else_sent_is_found_by_the_log_scan(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await backend.open_session(duration=120)
    await backend.poll()
    chain.advance_time(121)
    chain.send(
        chain.outsider,
        chain.exchange.functions.expireSession(bytes.fromhex(session.session_id[2:])),
    )

    report = await backend.poll()
    (expired,) = [event for event in report.indexed if event.event_name == "SessionExpired"]
    assert expired.run_id == session.run_id
    assert expired.calldata is not None
    assert expired.calldata["decoded_function"] == "expireSession"
    projection = await backend.project(session)
    (entry,) = projection.timeline
    assert entry["kind"] == "expire"
    assert entry["actor"] == "anyone"
    assert entry["sentence"].startswith("Session expired at ")
    assert entry["sentence"].endswith(" UTC.")
    assert entry["tx"]["status"] == "confirmed"  # not this backend's transaction: from its depth
    assert projection.outcome is not None
    assert (projection.outcome.kind, projection.outcome.actor) == (
        OutcomeKind.EXPIRED,
        PartyOrOperator.ANYONE,
    )


async def test_polling_again_or_restarting_records_nothing_twice(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await backend.open_session()
    await backend.poll()
    await backend.act(
        session, session.offer(session.buyer, 1, 80_000_000, chain.chain_time() + 600)
    )
    await backend.poll()
    async with backend.database.unit_of_work() as uow:
        before = await uow.chain_events.history_for_export(session.run_id)

    again = await backend.poll()
    restarted = await backend.new_indexer().poll()  # a new process rescans from start_block
    assert [event for event in again.indexed if event.run_id == session.run_id] == []
    assert [event for event in restarted.indexed if event.run_id == session.run_id] == []
    async with backend.database.unit_of_work() as uow:
        after = await uow.chain_events.history_for_export(session.run_id)
    assert [event.id for event in after] == [event.id for event in before]


async def test_confirmed_is_not_final_until_the_finalized_head_covers_it(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await backend.open_session(threshold=2)
    await backend.poll()
    async with backend.database.unit_of_work() as uow:
        (opening,) = await uow.outbox.list_for_run(session.run_id)
    chain.mine()
    report = await backend.poll()
    assert [row.status for row in report.confirmed if row.run_id == session.run_id] == [
        TxStatus.CONFIRMED
    ]

    # Anvil's finalized head trails its head by 64 blocks. One block short of covering the
    # transaction it is still only confirmed; two confirmations are never called final.
    assert opening.block_number is not None
    chain.mine(opening.block_number + 63 - chain.head())
    report = await backend.poll()
    assert report.finalized_block == opening.block_number - 1
    assert [row for row in report.finalized if row.run_id == session.run_id] == []

    chain.mine()
    report = await backend.poll()
    assert report.finalized_block == opening.block_number
    assert [row.status for row in report.finalized if row.run_id == session.run_id] == [
        TxStatus.FINALIZED
    ]


async def test_a_successor_whose_original_was_mined_is_superseded_on_recovery(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await _stuck_offer(backend, chain, blocks=3)
    original = await _stuck_row(backend, session)
    (successor,) = await backend.relay.replace_stuck(session.run_id)
    chain.drop(successor.tx_hash)
    chain.w3.eth.send_raw_transaction(original.raw_tx)
    chain.mine()

    # Recovery runs before the indexer has seen the receipt: the successor's nonce is consumed by a
    # transaction this outbox recorded, so it is dropped rather than called a conflict.
    results = await backend.relay.reconcile(session.run_id)
    assert {(result.outbox_id, result.outcome) for result in results} == {
        (successor.id, Reconciliation.SUPERSEDED)
    }


async def test_a_stuck_transaction_the_relay_did_not_sign_is_not_replaced(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await backend.open_session()
    await backend.poll()
    chain.automine(False)
    participant = session.seller
    raw = bytes(
        participant.sign_transaction(
            cast(
                "Any",
                {
                    "type": 2,
                    "chainId": 31337,
                    "nonce": chain.w3.eth.get_transaction_count(participant.address),
                    "to": str(backend.deployment.base_token_address),
                    "value": 0,
                    "data": backend.codec.encode_approve(backend.deployment.exchange_address, 1),
                    "gas": 70_000,
                    "maxFeePerGas": 10 * 10**9,
                    "maxPriorityFeePerGas": 10**9,
                },
            )
        ).raw_transaction
    )
    await backend.relay.submit_presigned(session.run_id, TxKind.APPROVE, raw)
    chain.block_gas_limit(LOW_BLOCK_GAS_LIMIT)
    chain.mine(3)
    chain.block_gas_limit(30_000_000)
    # The agent's key signed it (ADR-040); the relay holds no key that could re-sign it.
    assert await backend.relay.replace_stuck(session.run_id) == []


async def test_a_terminal_run_is_no_longer_watched_for_reorgs(
    backend: Backend, chain: AnvilChain
) -> None:
    """Q20's interim answer, deferred to stage 5: TERMINAL stays final."""
    snapshot = chain.snapshot()
    session = await backend.open_session()
    await backend.poll()
    async with backend.database.unit_of_work() as uow:
        await uow.runs.update_state(session.run_id, RunState.TERMINAL)
    chain.revert(snapshot)  # the session's opening is no longer on the chain
    chain.mine(2)
    assert (await backend.poll()).reorg is None

    # The control: the same missing block, for a run that is not terminal, is a reorg.
    async with backend.database.unit_of_work() as uow:
        await uow.runs.update_state(session.run_id, RunState.RUNNING)
    report = await backend.poll()
    assert report.reorg is not None
    assert report.reorg.run_ids == (session.run_id,)


async def test_a_session_whose_opening_was_never_seen_is_recorded_without_sentences(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await backend.open_session()
    await backend.poll()
    late = Indexer(
        backend.database,
        backend.adapter,
        backend.codec,
        replace(backend.deployment, start_block=chain.head() + 1),
        TimelineSentences(),
        policy=IndexerPolicy(),
    )
    async with backend.database.unit_of_work() as uow:
        await uow.chain_events.invalidate_from_block(
            31337, backend.deployment.exchange_address, 0, datetime.now(UTC)
        )
    await backend.act(
        session, session.offer(session.buyer, 1, 80_000_000, chain.chain_time() + 600)
    )
    await backend.mined()
    report = await late.poll()
    (offer,) = [event for event in report.indexed if event.run_id == session.run_id]
    assert offer.event_name == "OfferRecorded"
    assert offer.sentence is None  # the evidence is recorded; only the description is missing
