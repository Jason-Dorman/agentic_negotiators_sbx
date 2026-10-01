"""The stage 2.3 adversarial review's confirmed defects, each pinned by a test that failed before
its fix, against Anvil and PostgreSQL (docs/build_plan.md stage 2.3, ADR-055 to ADR-060).

- A block the RPC fails to return is not a reorg, and a row a rewind invalidated comes back when
  its unchanged block is read again (ADR-055).
- One signed action is signed into one transaction: after recovery drops a successor as superseded,
  a retried submission sends nothing.
- A run whose records the indexer cannot complete is reported, and the poll goes on for the others.
- Recovery keeps an unanswered resend and a refused one apart from a resend that happened (ADR-057).
- Depth keeps growing for a watched run's events after they pass the finalized head.
- A reorg is written as a `chain.reorg` run event, and a terminal event is reported on every poll
  (ADR-058).
- An invalid threshold is refused, not defaulted (ADR-059).
- A deadline revert is named by its protocol error (ADR-056).
- A reorg that removes a reverted transaction removes its failure from the timeline.
- "Nonce too low" is believed only with a receipt; an operator's nonce counts its own outbox.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest
from api_chain import AnvilChain, Backend, Session
from api_faults import (
    CountingAdapter,
    DiesBeforeSend,
    FailsBlockOnce,
    HeadOneBehind,
    ProcessKilledError,
    RefusesSend,
    UnreachableOnSend,
)

from api.chain import RpcUnavailableError
from api.config import InvalidRunConfigError
from api.db import ActionStatus, TxKind, TxStatus
from api.relay import Reconciliation, RelayError


async def _offer(backend: Backend, session: Session, chain: AnvilChain, lifetime: int = 600) -> Any:
    return await backend.act(
        session, session.offer(session.buyer, 1, 80_000_000, chain.chain_time() + lifetime)
    )


async def _history(backend: Backend, session: Session) -> list[tuple[str, bool]]:
    async with backend.database.unit_of_work() as uow:
        rows = await uow.chain_events.history_for_export(session.run_id)
    return [(row.event_name, row.canonical) for row in rows]


# ---------------------------------------------------------------------------------------------
# B1: a transient RPC fault is not a reorg; ADR-055: an invalidated row comes back
# ---------------------------------------------------------------------------------------------


async def test_a_block_the_rpc_fails_to_return_is_an_outage_not_a_reorg(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await backend.open_session()
    await backend.poll()
    await _offer(backend, session, chain)
    await backend.poll()
    async with backend.database.unit_of_work() as uow:
        opened = (await uow.chain_events.canonical_for_run(session.run_id))[0]

    flaky = Backend(
        backend.database, chain, adapter=FailsBlockOnce(chain.rpc_url, opened.block_number)
    )
    with pytest.raises(RpcUnavailableError):
        await flaky.indexer.poll()
    report = await flaky.indexer.poll()
    assert report.reorg is None
    assert await _history(backend, session) == [("SessionOpened", True), ("OfferRecorded", True)]
    projection = await backend.project(session)
    assert projection.session is not None
    assert projection.session["offer_count"] == 1


async def test_a_row_invalidated_wrongly_is_canonical_again_when_its_block_is_read(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await backend.open_session()
    await backend.poll()
    await _offer(backend, session, chain)
    await backend.poll()
    async with backend.database.unit_of_work() as uow:
        rows = await uow.chain_events.canonical_for_run(session.run_id)
        await uow.chain_events.invalidate_blocks(
            31337,
            backend.deployment.exchange_address,
            [row.block_hash for row in rows],
            datetime.now(UTC),
        )
    assert await _history(backend, session) == [("SessionOpened", False), ("OfferRecorded", False)]

    report = await backend.poll()  # the rescan reads the same logs in the same, unchanged blocks
    restored = [event for event in report.indexed if event.run_id == session.run_id]
    assert [event.event_name for event in restored] == ["SessionOpened", "OfferRecorded"]
    assert await _history(backend, session) == [("SessionOpened", True), ("OfferRecorded", True)]
    projection = await backend.project(session)
    assert projection.session is not None
    assert projection.session["offer_count"] == 1
    assert projection.timeline[0]["sentence"] == "Buyer offers 80 mUSD for 10 mASSET."


# ---------------------------------------------------------------------------------------------
# B2: one signed action, one transaction
# ---------------------------------------------------------------------------------------------


async def test_a_retried_submission_after_a_superseded_successor_sends_nothing(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await backend.open_session()
    await backend.poll()
    chain.automine(False)
    action = await _offer(backend, session, chain)
    chain.block_gas_limit(50_000)
    chain.mine(3)
    chain.block_gas_limit(30_000_000)
    (successor,) = await backend.relay.replace_stuck(session.run_id)
    async with backend.database.unit_of_work() as uow:
        original = await uow.outbox.get(successor.replaces_id)  # type: ignore[arg-type]
    assert original is not None
    chain.drop(successor.tx_hash)
    chain.w3.eth.send_raw_transaction(original.raw_tx)
    chain.mine()
    results = await backend.relay.reconcile(session.run_id)
    assert [result.outcome for result in results] == [Reconciliation.SUPERSEDED]

    counting = CountingAdapter(chain.rpc_url)
    await backend.new_relay(counting).submit_action(session.run_id, action.id)  # a retried turn
    assert counting.sends == 0

    report = await backend.poll()  # must not fail on a second live transaction
    assert [row.id for row in report.included] == [original.id]
    async with backend.database.unit_of_work() as uow:
        rows = await uow.outbox.for_signed_action(action.id)
        recorded = await uow.signed_actions.get(action.id)
    assert [row.status for row in rows] == [TxStatus.CONFIRMED, TxStatus.DROPPED]
    assert recorded is not None
    assert recorded.status == ActionStatus.CONFIRMED


# ---------------------------------------------------------------------------------------------
# B3 and ADR-059: one run's fault is reported and does not stop the poll
# ---------------------------------------------------------------------------------------------


async def test_a_terminal_event_without_its_opening_is_a_reported_problem(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await backend.open_session()
    await backend.poll()
    chain.mine(70)  # the opening's block is now below the finalized head and is not re-read
    await backend.poll()
    await _offer(backend, session, chain)
    await backend.act(session, session.close(session.seller, 2, 3))
    async with backend.database.unit_of_work() as uow:
        opened = (await uow.chain_events.canonical_for_run(session.run_id))[0]
        await uow.chain_events.invalidate_blocks(
            31337, backend.deployment.exchange_address, [opened.block_hash], datetime.now(UTC)
        )
    report = await backend.poll()
    assert [(problem.run_id, problem.code) for problem in report.problems] == [
        (session.run_id, "session_opening_missing")
    ]
    assert [item for item in report.terminal if item.run_id == session.run_id] == []


async def test_an_invalid_threshold_is_refused_not_defaulted(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await backend.open_session()
    async with backend.database.unit_of_work() as uow:
        run = await uow.runs.get(session.run_id)
    assert run is not None
    # A threshold written as text: nothing is confirmed under a value nobody chose (ADR-059).
    await _set_public_config(backend, session, {**run.public_config, "confirmation_threshold": "2"})
    report = await backend.poll()
    assert [(problem.run_id, problem.code) for problem in report.problems] == [
        (session.run_id, "invalid_confirmation_threshold")
    ]
    assert [row for row in report.confirmed if row.run_id == session.run_id] == []
    with pytest.raises(InvalidRunConfigError):
        await backend.project(session)


async def _set_public_config(backend: Backend, session: Session, config: dict[str, Any]) -> None:
    from sqlalchemy import text  # the repositories have no update of a run's configuration
    from sqlalchemy.ext.asyncio import create_async_engine

    url = str(backend.database._engine.url.render_as_string(hide_password=False))
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text("UPDATE runs SET public_config = CAST(:config AS jsonb) WHERE id = :id"),
                {"config": __import__("json").dumps(config), "id": session.run_id},
            )
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------------------------
# B4 and ADR-057: recovery says what happened to a resend
# ---------------------------------------------------------------------------------------------


async def _persisted_unsent(backend: Backend, chain: AnvilChain) -> tuple[Session, Any]:
    session = await backend.open_session()
    await backend.poll()
    message = session.offer(session.buyer, 1, 80_000_000, chain.chain_time() + 600)
    action = await backend.record(session, message)
    with pytest.raises(ProcessKilledError):
        await backend.new_relay(DiesBeforeSend(chain.rpc_url)).submit_action(
            session.run_id, action.id
        )
    return session, action


async def test_a_resend_the_node_refuses_is_refused_with_its_reason(
    backend: Backend, chain: AnvilChain
) -> None:
    session, _ = await _persisted_unsent(backend, chain)
    (result,) = await backend.new_relay(RefusesSend(chain.rpc_url)).reconcile(session.run_id)
    assert result.outcome == Reconciliation.REFUSED
    assert result.detail is not None
    assert "insufficient funds" in result.detail
    async with backend.database.unit_of_work() as uow:
        (row,) = [r for r in await uow.outbox.list_for_run(session.run_id) if r.signed_action_id]
    assert row.status == TxStatus.PENDING
    assert row.last_error is not None
    assert "insufficient funds" in row.last_error


async def test_a_resend_that_goes_unanswered_is_unreachable_not_rebroadcast(
    backend: Backend, chain: AnvilChain
) -> None:
    session, _ = await _persisted_unsent(backend, chain)
    (result,) = await backend.new_relay(UnreachableOnSend(chain.rpc_url)).reconcile(session.run_id)
    assert result.outcome == Reconciliation.UNREACHABLE


async def test_nonce_too_low_without_a_receipt_is_not_taken_for_sent(
    backend: Backend, chain: AnvilChain
) -> None:
    session, action = await _persisted_unsent(backend, chain)
    # Something outside this outbox uses the relay key's nonce first.
    chain.send(chain.relay, chain.base_token.functions.approve(chain.relay.address, 1))
    row = await backend.relay.submit_action(session.run_id, action.id)  # resumes; the node refuses
    assert row.status == TxStatus.PENDING
    assert row.last_error is not None
    assert "nonce too low" in row.last_error.lower()


async def test_an_operator_nonce_counts_what_its_outbox_has_recorded(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await backend.open_session()
    data = backend.codec.encode_mint(backend.deployment.exchange_address, 1)
    token = backend.deployment.base_token_address
    with pytest.raises(ProcessKilledError):
        await backend.new_relay(DiesBeforeSend(chain.rpc_url)).submit_call(
            session.run_id, TxKind.MINT, token, data, as_operator=True
        )
    second = await backend.relay.submit_call(
        session.run_id, TxKind.MINT, token, data, as_operator=True
    )
    async with backend.database.unit_of_work() as uow:
        mints = [r for r in await uow.outbox.list_for_run(session.run_id) if r.kind == TxKind.MINT]
    # The chain's pending count does not know the unsent first one; the outbox does.
    assert [row.nonce for row in mints] == [mints[0].nonce, mints[0].nonce + 1]
    assert second.nonce == mints[0].nonce + 1


async def test_presigned_bytes_for_another_chain_or_unreadable_are_refused(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await backend.open_session()
    other_chain = bytes(
        session.buyer.sign_transaction(
            cast(
                "Any",
                {
                    "type": 2,
                    "chainId": 11155111,
                    "nonce": 0,
                    "to": str(backend.deployment.quote_token_address),
                    "value": 0,
                    "data": b"",
                    "gas": 70_000,
                    "maxFeePerGas": 10**10,
                    "maxPriorityFeePerGas": 10**9,
                },
            )
        ).raw_transaction
    )
    with pytest.raises(RelayError, match="chain 11155111"):
        await backend.relay.submit_presigned(session.run_id, TxKind.APPROVE, other_chain)
    with pytest.raises(RelayError, match="not a readable transaction"):
        await backend.relay.submit_presigned(session.run_id, TxKind.APPROVE, b"\x02\xff\x00")


# ---------------------------------------------------------------------------------------------
# B6, B7 and ADR-058: depth after finality; durable reorg events; level-triggered terminal
# ---------------------------------------------------------------------------------------------


async def test_depth_reaches_the_threshold_after_an_event_passes_the_finalized_head(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await backend.open_session(threshold=2)
    await backend.poll()
    await _offer(backend, session, chain)
    await backend.act(session, session.close(session.seller, 2, 3))
    await backend.poll()  # the close at depth 1: below the threshold
    assert (await backend.project(session)).outcome is None
    chain.mine(70)  # no poll while the close passes the finalized head
    await backend.poll()
    async with backend.database.unit_of_work() as uow:
        events = await uow.chain_events.canonical_for_run(session.run_id)
    assert all((event.confirmations_at_index or 0) >= 2 for event in events)
    projection = await backend.project(session)
    assert projection.outcome is not None


async def test_a_reorg_is_written_as_a_run_event_and_a_terminal_event_is_reported_every_poll(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await backend.open_session()
    await backend.poll()
    snapshot = chain.snapshot()
    offer = await _offer(backend, session, chain)
    await backend.poll()
    chain.revert(snapshot)
    chain.mine(2)
    report = await backend.poll()
    assert report.reorg is not None
    async with backend.database.unit_of_work() as uow:
        events = await uow.run_events.after(session.run_id, 0)
    (reorg,) = [event for event in events if event.event_type == "chain.reorg"]
    assert reorg.data["from_block"] == report.reorg.fork_block
    assert str(offer.digest) in reorg.data["invalidated_digests"]

    await backend.relay.reconcile(session.run_id)
    await backend.act(session, session.close(session.seller, 2, 3))
    first, second = await backend.poll(), await backend.poll()
    for poll in (first, second):  # a lost report loses nothing: the next poll says it again
        assert [
            item.event.event_name for item in poll.terminal if item.run_id == session.run_id
        ] == ["SessionClosed"]


# ---------------------------------------------------------------------------------------------
# ADR-056, and a reorg of a reverted transaction
# ---------------------------------------------------------------------------------------------


async def test_a_deadline_revert_is_named_by_its_protocol_error(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await backend.open_session()
    await backend.poll()
    valid_until = chain.chain_time() + 30
    offer = session.offer(session.buyer, 1, 80_000_000, valid_until)
    await backend.act(session, offer)
    # The acceptance is mined in the first block at or after `validUntil`: the offer was still
    # live in the parent's state, so only a replay at the inclusion block's time sees the expiry.
    chain.rpc("evm_setNextBlockTimestamp", hex(valid_until))
    await backend.act(session, session.accept(session.seller, 2, offer.digest))
    report = await backend.poll()
    (reverted,) = report.reverted
    assert (
        reverted.last_error == "OfferExpired"
    )  # replayed at its own block's time, not the parent's


async def test_a_reorg_that_removes_a_reverted_transaction_removes_its_failure(
    backend: Backend, chain: AnvilChain
) -> None:
    session = await backend.open_session()
    await backend.poll()
    offer = session.offer(session.buyer, 1, 80_000_000, chain.chain_time() + 600)
    await backend.act(session, offer)
    await backend.poll()
    snapshot = chain.snapshot()
    action = await backend.act(session, session.accept(session.buyer, 2, offer.digest))
    report = await backend.poll()
    assert [row.last_error for row in report.reverted] == ["SelfAcceptance"]

    chain.revert(snapshot)
    chain.mine(2)
    report = await backend.poll()  # only the outbox row records that block: its leg finds it
    assert report.reorg is not None
    async with backend.database.unit_of_work() as uow:
        (row,) = await uow.outbox.for_signed_action(action.id)
        recorded = await uow.signed_actions.get(action.id)
    assert (row.status, row.last_error, row.sentence) == (TxStatus.SUBMITTED, None, None)
    assert recorded is not None
    assert (recorded.status, recorded.revert_error) == (ActionStatus.SUBMITTED, None)
    projection = await backend.project(session)
    assert [entry["kind"] for entry in projection.timeline] == ["offer"]


async def test_a_second_broadcast_record_keeps_the_first_block_the_replacement_counts_from(
    backend: Backend,
) -> None:
    session = await backend.open_session()
    async with backend.database.unit_of_work() as uow:
        (row,) = await uow.outbox.list_for_run(session.run_id)
        again = await uow.outbox.mark_submitted(row.id, datetime.now(UTC), 999_999)
    assert row.submitted_block is not None
    assert (again.submitted_block, again.submitted_at) == (row.submitted_block, row.submitted_at)


async def test_a_receipt_mined_after_the_poll_read_its_head_waits_for_the_next_poll(
    backend: Backend, chain: AnvilChain
) -> None:
    """A poll describes the chain at the head it read: nothing above it is recorded at depth 0."""
    session = await backend.open_session()
    lagging = Backend(backend.database, chain, adapter=HeadOneBehind(chain.rpc_url))
    report = await lagging.indexer.poll()  # the opening's block is one above the head it read
    assert [row for row in report.included if row.run_id == session.run_id] == []
    assert [event for event in report.indexed if event.run_id == session.run_id] == []

    report = await lagging.indexer.poll()
    (opened,) = [event for event in report.indexed if event.run_id == session.run_id]
    assert opened.event_name == "SessionOpened"
    assert opened.confirmations_at_index == 1
