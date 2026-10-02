"""A13 at the run level: a controller dies after broadcast and before receipt persistence (2.4).

The stage 2.3 test stopped a relay; this one stops the whole controller at the worst moment of a
run — the node has just accepted the seller's `acceptAndSettle`, and nothing has recorded that —
and starts a new one, a new lease holder in a new "process", with only the database and the chain.
`recover` takes the lease once the old one has expired, reconciles the outbox, and drives the run:
one transaction, one settlement, nothing sent twice (architecture 5.4, FR-E5).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from api_chain import AnvilChain
from api_controller import ControllerHarness
from api_faults import CountingAdapter, ProcessKilledError
from eth_typing import HexStr
from web3 import Web3

from api.chain import TransactionRejectedError
from api.controller import Progress
from api.db import Database, OutcomeKind, RunRecord, RunState, TxKind, TxStatus
from api.relay import decode_raw_transaction
from negotiation_protocol import Digest

ACCEPT_SELECTOR = bytes(
    Web3.keccak(text="acceptAndSettle((bytes32,bytes32,uint64,address,bytes32),bytes)")[:4]
)


class DiesAfterSendingTheSettlement(CountingAdapter):
    async def send_raw(self, raw_tx: bytes) -> Digest:
        digest = await super().send_raw(raw_tx)
        if decode_raw_transaction(raw_tx)["data"][:4] == ACCEPT_SELECTOR:
            raise ProcessKilledError("killed after the node accepted acceptAndSettle")
        return digest


class CountsSettlements(CountingAdapter):
    def __init__(self, rpc_url: str) -> None:
        super().__init__(rpc_url)
        self.settlements_sent = 0

    async def send_raw(self, raw_tx: bytes) -> Digest:
        if decode_raw_transaction(raw_tx)["data"][:4] == ACCEPT_SELECTOR:
            self.settlements_sent += 1
        return await super().send_raw(raw_tx)


async def finished(run: RunRecord) -> bool:
    return run.state in (RunState.TERMINAL, RunState.FAILED_SETUP, RunState.RECOVERY_REQUIRED)


async def test_a_restarted_controller_recovers_one_settlement(
    database: Database, chain: AnvilChain
) -> None:
    dying = ControllerHarness(
        database,
        chain,
        background=False,
        holder="process-1",
        adapter=DiesAfterSendingTheSettlement(chain.rpc_url),
    )
    run = await dying.validated()
    await dying.controller.start(run.id)
    with pytest.raises(ProcessKilledError):
        await dying.tick_until(run.id, finished)

    async with database.unit_of_work() as uow:
        rows = [
            r for r in await uow.outbox.list_for_run(run.id) if r.kind == TxKind.ACCEPT_AND_SETTLE
        ]
        lease = (await uow.leases.expired(datetime.now(UTC) + timedelta(hours=1)))[0]
    (accept,) = rows
    assert accept.status == TxStatus.PENDING, "the broadcast happened and was never recorded"
    assert lease.holder == "process-1"
    assert (await dying.run(run.id)).state == RunState.RUNNING

    later = datetime.now(UTC) + timedelta(minutes=5)
    adapter = CountsSettlements(chain.rpc_url)
    restarted = ControllerHarness(
        database,
        chain,
        agents=dying.agents,
        background=False,
        holder="process-2",
        clock=lambda: later,
        adapter=adapter,
    )
    try:
        assert await restarted.controller.recover() == run.id
        run = await restarted.tick_until(run.id, finished)
    finally:
        await restarted.aclose()
        await dying.aclose()

    assert (run.state, run.outcome_kind) == (RunState.TERMINAL, OutcomeKind.SETTLED)
    assert adapter.settlements_sent == 0, "recovery found the transaction mined and sent nothing"
    async with database.unit_of_work() as uow:
        rows = [
            r for r in await uow.outbox.list_for_run(run.id) if r.kind == TxKind.ACCEPT_AND_SETTLE
        ]
        settlements = [
            e
            for e in await uow.chain_events.canonical_for_run(run.id)
            if e.event_name == "SettlementCompleted"
        ]
    assert [r.tx_hash for r in rows] == [accept.tx_hash]
    assert [e.tx_hash for e in settlements] == [accept.tx_hash]
    assert rows[0].status in (TxStatus.CONFIRMED, TxStatus.FINALIZED)


async def test_a_live_lease_is_waited_for_and_never_shared(
    database: Database, chain: AnvilChain
) -> None:
    """Recovery takes the run over only once the old holder's lease has expired, waiting for it
    rather than giving up (architecture 5.4); until then two processes never drive one run."""
    first = ControllerHarness(database, chain, background=False, holder="process-1")
    run = await first.validated()
    await first.controller.step(run.id)

    now = [datetime.now(UTC)]
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        now[0] += timedelta(seconds=seconds)

    second = ControllerHarness(
        database,
        chain,
        agents=first.agents,
        background=False,
        holder="process-2",
        clock=lambda: now[0],
        sleep=sleep,
    )
    try:
        assert await second.controller.recover(wait=False) is None
        assert not await second.controller.driver.hold(run.id)
        assert await second.controller.driver.tick(run.id) == Progress.IDLE, "no lease, no tick"
        assert await first.controller.driver.hold(run.id)

        assert await second.controller.recover() == run.id
        assert slept and sum(slept) >= 30, "it waited out the old lease"
        assert not await first.controller.driver.hold(run.id), "the run has one holder"
    finally:
        await second.aclose()
        await first.aclose()


async def test_a_graceful_shutdown_hands_the_run_over_at_once(
    database: Database, chain: AnvilChain
) -> None:
    first = ControllerHarness(database, chain, background=False, holder="process-1")
    run = await first.validated()
    await first.controller.step(run.id)
    await first.aclose()
    second = ControllerHarness(
        database, chain, agents=first.agents, background=False, holder="process-2"
    )
    try:
        assert await second.controller.recover(wait=False) == run.id
    finally:
        await second.aclose()


RECORD_OFFER = bytes(
    Web3.keccak(text="recordOffer((bytes32,bytes32,uint64,address,uint256,uint64),bytes)")[:4]
)


class DiesBeforeSendingTheOffer(CountingAdapter):
    """The offer's transaction is persisted, and the process dies before the node has it."""

    async def send_raw(self, raw_tx: bytes) -> Digest:
        if decode_raw_transaction(raw_tx)["data"][:4] == RECORD_OFFER:
            raise ProcessKilledError("killed after persisting and before broadcasting")
        return await super().send_raw(raw_tx)


async def _crashed_before_the_offer_was_sent(
    database: Database, chain: AnvilChain
) -> tuple[ControllerHarness, RunRecord]:
    dying = ControllerHarness(
        database,
        chain,
        background=False,
        holder="process-1",
        adapter=DiesBeforeSendingTheOffer(chain.rpc_url),
    )
    run = await dying.validated()
    await dying.controller.step(run.id)
    with pytest.raises(ProcessKilledError):
        await dying.tick_until(run.id, finished)
    return dying, run


async def test_recovery_sends_what_was_persisted_before_anything_is_driven(
    database: Database, chain: AnvilChain
) -> None:
    """Start-up reconcile (architecture 5.4), on its own: the persisted offer is on chain as soon
    as `recover` returns, before any tick."""
    dying, run = await _crashed_before_the_offer_was_sent(database, chain)
    later = datetime.now(UTC) + timedelta(minutes=5)
    restarted = ControllerHarness(
        database, chain, agents=dying.agents, background=False, clock=lambda: later
    )
    try:
        assert await restarted.controller.recover() == run.id
        async with database.unit_of_work() as uow:
            (offer,) = [
                r for r in await uow.outbox.list_for_run(run.id) if r.kind == TxKind.RECORD_OFFER
            ]
        receipt = chain.w3.eth.get_transaction_receipt(HexStr(str(offer.tx_hash)))
        assert receipt["status"] == 1
    finally:
        await restarted.aclose()
        await dying.aclose()


class RefusesEverything(CountingAdapter):
    async def send_raw(self, raw_tx: bytes) -> Digest:
        raise TransactionRejectedError("insufficient funds for gas * price + value")


async def test_a_refused_resend_at_recovery_is_recovery_required(
    database: Database, chain: AnvilChain
) -> None:
    """ADR-057 at the run level: the node refuses the persisted bytes, so the run is a person's."""
    dying, run = await _crashed_before_the_offer_was_sent(database, chain)
    later = datetime.now(UTC) + timedelta(minutes=5)
    restarted = ControllerHarness(
        database,
        chain,
        agents=dying.agents,
        background=False,
        clock=lambda: later,
        adapter=RefusesEverything(chain.rpc_url),
    )
    try:
        await restarted.controller.recover()
        run = await restarted.run(run.id)
        assert (run.state, run.state_cause) == (RunState.RECOVERY_REQUIRED, "refused")
    finally:
        await restarted.aclose()
        await dying.aclose()
