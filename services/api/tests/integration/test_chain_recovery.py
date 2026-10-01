"""A13 and A06 at the chain layer (docs/build_plan.md stage 2.3), against Anvil and PostgreSQL.

A13: a relay stopped between `send_raw_transaction` and receipt persistence recovers the
transaction it sent and sends no second one. The stop is a `BaseException` raised from inside the
real adapter right after the node accepted the bytes (`api_faults`), and recovery is a *new* relay
— a restarted process — with only the database to go on.

A06: a duplicate submission of a confirmed action reconciles as already complete, with no extra
transfer: a second submission resumes the first transaction, a second signed-action row for the
same digest is refused by the database, two submitters racing produce one transaction, and the same
signed acceptance sent again by someone else is refused by the contract.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any, cast

import pytest
from api_chain import AnvilChain, Backend, Session, SignedMessage
from api_faults import (
    CountingAdapter,
    DiesAfterSend,
    DiesBeforeSend,
    MeetsAtEstimate,
    ProcessKilledError,
    Unreachable,
    UnreachableOnSend,
)

from api.db import ActionStatus, DuplicateError, OutboxRecord, TxKind, TxStatus
from api.relay import Reconciliation
from negotiation_protocol import Address


async def _opened(backend: Backend) -> Session:
    session = await backend.open_session()
    await backend.poll()
    return session


async def _offers(backend: Backend, session: Session) -> SignedMessage:
    """Buyer opens, seller counters. Returns the counter, which the buyer may accept."""
    valid_until = backend.chain.chain_time() + 600
    await backend.act(session, session.offer(session.buyer, 1, 80_000_000, valid_until))
    counter = session.offer(session.seller, 2, 96_000_000, valid_until)
    await backend.act(session, counter)
    await backend.poll()
    return counter


async def _rows(backend: Backend, run_id: uuid.UUID) -> list[OutboxRecord]:
    async with backend.database.unit_of_work() as uow:
        return await uow.outbox.list_for_run(run_id)


def _quote_balances(chain: AnvilChain, session: Session) -> tuple[int, int]:
    return chain.token_balances(session.buyer.address)[1], chain.token_balances(
        session.seller.address
    )[1]


# ---------------------------------------------------------------------------------------------
# A13
# ---------------------------------------------------------------------------------------------


async def test_a13_a_relay_killed_after_broadcast_recovers_without_a_second_transaction(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await _opened(backend)
    counter = await _offers(backend, session)
    start = chain.head() + 1
    action = await backend.record(session, session.accept(session.buyer, 3, counter.digest))

    dying = backend.new_relay(DiesAfterSend(chain.rpc_url))
    with pytest.raises(ProcessKilledError):
        await dying.submit_action(session.run_id, action.id)
    (row,) = [
        row for row in await _rows(backend, session.run_id) if row.signed_action_id == action.id
    ]
    assert row.status == TxStatus.PENDING  # the broadcast was never recorded

    restarted = CountingAdapter(chain.rpc_url)
    recovered = backend.new_relay(restarted)
    results = await recovered.reconcile(session.run_id)
    assert [(result.tx_hash, result.outcome) for result in results] == [
        (row.tx_hash, Reconciliation.MINED)
    ]
    assert restarted.sends == 0  # looked up, not sent again

    await backend.poll()
    projection = await backend.project(session)
    assert projection.session is not None
    assert projection.session["status"] == "settled"
    # One transaction from the relay, and one settlement: the buyer paid once.
    assert chain.transactions_from(backend.relay.relay_address, start) == [row.tx_hash]
    assert _quote_balances(chain, session) == (250_000_000 - 96_000_000, 96_000_000)


async def test_a13_a_transaction_still_in_the_pool_is_left_there(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await _opened(backend)
    message = session.offer(session.buyer, 1, 80_000_000, chain.chain_time() + 600)
    action = await backend.record(session, message)
    chain.automine(False)

    with pytest.raises(ProcessKilledError):
        await backend.new_relay(DiesAfterSend(chain.rpc_url)).submit_action(
            session.run_id, action.id
        )

    restarted = CountingAdapter(chain.rpc_url)
    results = await backend.new_relay(restarted).reconcile(session.run_id)
    assert [result.outcome for result in results] == [Reconciliation.IN_POOL]
    assert restarted.sends == 0
    (row,) = [row for row in await _rows(backend, session.run_id) if row.signed_action_id]
    assert row.status == TxStatus.SUBMITTED  # recovery recorded the broadcast it found
    async with backend.database.unit_of_work() as uow:
        recorded = await uow.signed_actions.get(action.id)
    assert recorded is not None
    assert recorded.status == ActionStatus.SUBMITTED  # and the action it carries

    chain.mine()
    await backend.poll()
    (row,) = [row for row in await _rows(backend, session.run_id) if row.signed_action_id]
    assert row.status == TxStatus.CONFIRMED


async def test_a13_a_transaction_persisted_but_never_sent_is_rebroadcast_once(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await _opened(backend)
    message = session.offer(session.buyer, 1, 80_000_000, chain.chain_time() + 600)
    action = await backend.record(session, message)
    start = chain.head() + 1

    with pytest.raises(ProcessKilledError):
        await backend.new_relay(DiesBeforeSend(chain.rpc_url)).submit_action(
            session.run_id, action.id
        )
    assert chain.transactions_from(backend.relay.relay_address, start) == []

    restarted = CountingAdapter(chain.rpc_url)
    results = await backend.new_relay(restarted).reconcile(session.run_id)
    assert [result.outcome for result in results] == [Reconciliation.REBROADCAST]
    assert restarted.sends == 1
    (row,) = [row for row in await _rows(backend, session.run_id) if row.signed_action_id]
    assert chain.transactions_from(backend.relay.relay_address, start) == [row.tx_hash]

    # A second recovery pass finds it mined and sends nothing.
    again = CountingAdapter(chain.rpc_url)
    results = await backend.new_relay(again).reconcile(session.run_id)
    assert [result.outcome for result in results] == [Reconciliation.MINED]
    assert again.sends == 0


async def test_a_nonce_taken_by_an_unknown_transaction_is_a_conflict_not_a_rebroadcast(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await _opened(backend)
    message = session.offer(session.buyer, 1, 80_000_000, chain.chain_time() + 600)
    action = await backend.record(session, message)
    with pytest.raises(ProcessKilledError):
        await backend.new_relay(DiesBeforeSend(chain.rpc_url)).submit_action(
            session.run_id, action.id
        )
    # Something outside this outbox uses the relay key's nonce first.
    chain.send(chain.relay, chain.base_token.functions.approve(chain.relay.address, 1))

    restarted = CountingAdapter(chain.rpc_url)
    results = await backend.new_relay(restarted).reconcile(session.run_id)
    assert [result.outcome for result in results] == [Reconciliation.NONCE_CONFLICT]
    assert restarted.sends == 0


# ---------------------------------------------------------------------------------------------
# A06
# ---------------------------------------------------------------------------------------------


async def test_a06_a_second_submission_of_a_confirmed_action_is_already_complete(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await _opened(backend)
    counter = await _offers(backend, session)
    accept = session.accept(session.buyer, 3, counter.digest)
    action = await backend.act(session, accept)
    await backend.poll()
    rows_before = await _rows(backend, session.run_id)
    balances = _quote_balances(chain, session)

    counting = CountingAdapter(chain.rpc_url)
    again = await backend.new_relay(counting).submit_action(session.run_id, action.id)
    assert again.signed_action_id == action.id
    assert again.status == TxStatus.CONFIRMED
    assert counting.sends == 0
    assert await _rows(backend, session.run_id) == rows_before

    # The same signed acceptance cannot be recorded twice: the database refuses it.
    with pytest.raises(DuplicateError) as refused:
        await backend.record(session, accept)
    assert refused.value.constraint in {
        "uq_signed_actions_digest",
        "uq_signed_actions_run_id_sequence",
    }

    # And the same signature, relayed again by anyone, is refused by the contract.
    data = backend.codec.encode_signed_action("accept", accept.typed_message, accept.signature)
    tx = {
        "type": 2,
        "chainId": 31337,
        "nonce": chain.w3.eth.get_transaction_count(chain.outsider.address, "pending"),
        "to": backend.deployment.exchange_address,
        "data": data,
        "value": 0,
        "gas": 500_000,
        "maxFeePerGas": 10 * 10**9,
        "maxPriorityFeePerGas": 10**9,
    }
    tx_hash = chain.w3.eth.send_raw_transaction(
        chain.outsider.sign_transaction(cast("Any", tx)).raw_transaction
    )
    assert chain.w3.eth.wait_for_transaction_receipt(tx_hash)["status"] == 0
    assert _quote_balances(chain, session) == balances


async def test_a06_two_submitters_racing_produce_one_transaction(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await _opened(backend)
    message = session.offer(session.buyer, 1, 80_000_000, chain.chain_time() + 600)
    action = await backend.record(session, message)
    start = chain.head() + 1

    meeting = MeetsAtEstimate(chain.rpc_url, parties=2)
    first, second = await asyncio.gather(
        backend.new_relay(meeting).submit_action(session.run_id, action.id),
        backend.new_relay(meeting).submit_action(session.run_id, action.id),
    )
    # Both reached the estimate — the race happened — and signed a transaction; the partial
    # unique index let one be persisted, and the other submitter resumed it.
    assert meeting.arrivals == 2
    assert first.id == second.id
    rows = [row for row in await _rows(backend, session.run_id) if row.signed_action_id]
    assert [row.id for row in rows] == [first.id]
    assert chain.transactions_from(backend.relay.relay_address, start) == [first.tx_hash]
    await backend.poll()
    async with backend.database.unit_of_work() as uow:
        recorded = await uow.signed_actions.get(action.id)
    assert recorded is not None
    assert recorded.status == ActionStatus.CONFIRMED


# ---------------------------------------------------------------------------------------------
# The broadcast's other failure modes
# ---------------------------------------------------------------------------------------------


async def test_a_broadcast_lost_to_an_rpc_timeout_is_recorded_and_sent_again_by_recovery(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await _opened(backend)
    message = session.offer(session.buyer, 1, 80_000_000, chain.chain_time() + 600)
    action = await backend.record(session, message)
    start = chain.head() + 1

    timing_out = UnreachableOnSend(chain.rpc_url)
    row = await backend.new_relay(timing_out).submit_action(session.run_id, action.id)
    assert (row.status, row.attempts) == (TxStatus.PENDING, 1)
    assert row.last_error is not None
    assert "did not answer" in row.last_error

    results = await backend.relay.reconcile(session.run_id)
    assert [result.outcome for result in results] == [Reconciliation.REBROADCAST]
    assert chain.transactions_from(backend.relay.relay_address, start) == [row.tx_hash]


async def test_recovery_that_cannot_reach_the_rpc_learns_nothing_and_sends_nothing(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await _opened(backend)
    message = session.offer(session.buyer, 1, 80_000_000, chain.chain_time() + 600)
    action = await backend.record(session, message)
    with pytest.raises(ProcessKilledError):
        await backend.new_relay(DiesBeforeSend(chain.rpc_url)).submit_action(
            session.run_id, action.id
        )
    results = await backend.new_relay(Unreachable(chain.rpc_url)).reconcile(session.run_id)
    # An unresolved outage is RECOVERY_REQUIRED for the controller, never a guess (FR-E6).
    assert [result.outcome for result in results] == [Reconciliation.UNREACHABLE]


async def test_resubmitting_a_pending_transaction_the_pool_already_holds_records_it(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await _opened(backend)
    message = session.offer(session.buyer, 1, 80_000_000, chain.chain_time() + 600)
    action = await backend.record(session, message)
    chain.automine(False)
    with pytest.raises(ProcessKilledError):
        await backend.new_relay(DiesAfterSend(chain.rpc_url)).submit_action(
            session.run_id, action.id
        )
    # The turn is retried: the same bytes go to the node, which already holds them.
    row = await backend.relay.submit_action(session.run_id, action.id)
    assert row.status == TxStatus.SUBMITTED
    chain.mine()
    await backend.poll()
    assert [r.status for r in await _rows(backend, session.run_id) if r.signed_action_id] == [
        TxStatus.CONFIRMED
    ]


async def test_a_setup_approval_an_agent_signed_is_persisted_broadcast_and_confirmed(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await _opened(backend)
    participant = session.buyer
    data = backend.codec.encode_approve(backend.deployment.exchange_address, 7)
    raw = bytes(
        participant.sign_transaction(
            cast(
                "Any",
                {
                    "type": 2,
                    "chainId": 31337,
                    "nonce": chain.w3.eth.get_transaction_count(participant.address),
                    "to": str(backend.deployment.quote_token_address),
                    "value": 0,
                    "data": data,
                    "gas": 70_000,
                    "maxFeePerGas": 10 * 10**9,
                    "maxPriorityFeePerGas": 10**9,
                },
            )
        ).raw_transaction
    )
    row = await backend.relay.submit_presigned(session.run_id, TxKind.APPROVE, raw)
    # Sender and nonce are read from the bytes, never taken from the caller.
    assert (row.kind, row.sender) == (TxKind.APPROVE, Address(participant.address))
    assert row.status == TxStatus.SUBMITTED
    again = await backend.relay.submit_presigned(session.run_id, TxKind.APPROVE, raw)
    assert again.id == row.id  # idempotent by hash

    await backend.poll()
    (confirmed,) = [r for r in await _rows(backend, session.run_id) if r.kind == TxKind.APPROVE]
    assert confirmed.status == TxStatus.CONFIRMED
    allowance = chain.quote_token.functions.allowance(
        participant.address, backend.deployment.exchange_address
    ).call()
    assert allowance == 7
