"""Setup under faults: an agent restarted, a crash at the session's creation, a broadcast refused,
an approval that is not what was asked for, a setup transaction that reverts, an approval stuck
in the pool — and the post-setup balances taken where they cannot be reorged away (ADR-069).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from api_chain import AnvilChain
from api_controller import ControllerHarness
from api_faults import CountingAdapter, ProcessKilledError
from eth_account import Account
from web3 import Web3

from api.chain import TransactionRejectedError
from api.db import (
    Database,
    OutcomeKind,
    Party,
    RunRecord,
    RunState,
    SnapshotStage,
    TxKind,
)
from api.relay import decode_raw_transaction
from negotiation_protocol import Digest

CREATE_SESSION = bytes(
    Web3.keccak(text="createSession((bytes32,address,address,uint256,uint64,uint16))")[:4]
)
MINT = bytes(Web3.keccak(text="mint(address,uint256)")[:4])


async def finished(run: RunRecord) -> bool:
    return run.state in (RunState.TERMINAL, RunState.FAILED_SETUP, RunState.RECOVERY_REQUIRED)


async def set_up(run: RunRecord) -> bool:
    return run.state in (RunState.RUNNING, RunState.PAUSED) or await finished(run)


async def _rows(harness: ControllerHarness, run: RunRecord) -> list[Any]:
    async with harness.database.unit_of_work() as uow:
        return list(await uow.outbox.list_for_run(run.id))


@pytest.fixture
async def harness(database: Database, chain: AnvilChain) -> AsyncIterator[ControllerHarness]:
    harness = ControllerHarness(database, chain, background=False)
    yield harness
    await harness.aclose()
    chain.automine(True)


async def test_an_agent_restarted_during_setup_is_provisioned_again(
    harness: ControllerHarness,
) -> None:
    """ADR-048 during setup: no session exists to approve, so the agent is provisioned again with
    its stored address and asked for its approval once more."""
    run = await harness.validated()
    await harness.controller.start(run.id)

    async def funding(run: RunRecord) -> bool:
        return any(row.kind == TxKind.FUND_ETH for row in await _rows(harness, run))

    run = await harness.tick_until(run.id, funding)
    assert not any(row.kind == TxKind.APPROVE for row in await _rows(harness, run))
    for party in (Party.BUYER, Party.SELLER):
        harness.agents.restart(party)
        harness.agents.requests[party].clear()
    run = await harness.tick_until(run.id, finished)
    assert (run.state, run.outcome_kind) == (RunState.TERMINAL, OutcomeKind.SETTLED)
    routes = [path.rsplit("/", 1)[-1] for path in harness.agents.requests[Party.BUYER]]
    assert routes[:3] == ["setup-approval", "provision", "setup-approval"]


class DiesAfterSendingCreateSession(CountingAdapter):
    async def send_raw(self, raw_tx: bytes) -> Digest:
        digest = await super().send_raw(raw_tx)
        if decode_raw_transaction(raw_tx)["data"][:4] == CREATE_SESSION:
            raise ProcessKilledError("killed after the node accepted createSession")
        return digest


async def test_a_crash_after_create_session_is_sent_finds_its_session(
    database: Database, chain: AnvilChain
) -> None:
    """The session id is written before `createSession` is broadcast, so the indexer can attribute
    `SessionOpened` to the run even when the process dies the moment the node has it."""
    dying = ControllerHarness(
        database,
        chain,
        background=False,
        holder="process-1",
        adapter=DiesAfterSendingCreateSession(chain.rpc_url),
    )
    run = await dying.validated()
    await dying.controller.start(run.id)
    with pytest.raises(ProcessKilledError):
        await dying.tick_until(run.id, finished)
    later = datetime.now(UTC) + timedelta(minutes=5)
    restarted = ControllerHarness(
        database, chain, agents=dying.agents, background=False, clock=lambda: later
    )
    try:
        await restarted.controller.recover()
        run = await restarted.tick_until(run.id, finished)
    finally:
        await restarted.aclose()
        await dying.aclose()
    assert (run.state, run.outcome_kind) == (RunState.TERMINAL, OutcomeKind.SETTLED)
    creates = [row for row in await _rows(restarted, run) if row.kind == TxKind.CREATE_SESSION]
    assert len(creates) == 1


class RefusesMints(CountingAdapter):
    """The node refuses the operator's mints, as it does an operator out of test ETH."""

    def __init__(self, rpc_url: str) -> None:
        super().__init__(rpc_url)
        self.refusing = True

    async def send_raw(self, raw_tx: bytes) -> Digest:
        if self.refusing and decode_raw_transaction(raw_tx)["data"][:4] == MINT:
            raise TransactionRejectedError("insufficient funds for gas * price + value")
        return await super().send_raw(raw_tx)


async def test_a_refused_setup_broadcast_goes_to_a_person_and_resume_carries_on(
    database: Database, chain: AnvilChain
) -> None:
    adapter = RefusesMints(chain.rpc_url)
    harness = ControllerHarness(database, chain, background=False, adapter=adapter)
    try:
        run = await harness.validated()
        await harness.controller.start(run.id)
        run = await harness.tick_until(run.id, finished)
        assert (run.state, run.state_cause) == (RunState.RECOVERY_REQUIRED, "refused")

        adapter.refusing = False  # the operator funded its key
        resumed = await harness.controller.resume(run.id)
        assert (resumed.state, resumed.state_cause) == (RunState.PREPARING, "recovered")
        run = await harness.tick_until(run.id, set_up)
        assert run.state == RunState.PAUSED
        mints = [row for row in await _rows(harness, run) if row.kind == TxKind.MINT]
        assert len(mints) == 2, "the refused bytes were sent again, not signed again"
    finally:
        await harness.aclose()


async def test_an_approval_that_is_not_what_was_asked_for_is_never_relayed(
    harness: ControllerHarness,
) -> None:
    """The bytes are checked, not only the agent's description of them: an approval signed by the
    party's own key, described correctly, but for a larger gas limit than asked."""
    run = await harness.validated()
    chain_id = harness.deployment.chain_id

    def other_bytes(path: str, body: dict[str, Any]) -> dict[str, Any]:
        if path.endswith("/setup-approval"):
            fields = decode_raw_transaction(bytes.fromhex(body["raw_tx"][2:]))
            account = harness.agents.run_account(Party.BUYER, run.id, chain_id)
            signed = Account.sign_transaction({**fields, "gas": fields["gas"] + 1}, account.key)
            body["raw_tx"] = "0x" + bytes(signed.raw_transaction).hex()
            body["tx_hash"] = "0x" + bytes(signed.hash).hex()
        return body

    harness.agents.rewrite[Party.BUYER] = other_bytes
    await harness.controller.start(run.id)
    run = await harness.tick_until(run.id, finished)
    assert (run.state, run.state_cause) == (
        RunState.RECOVERY_REQUIRED,
        "agent_setup_approval_mismatch",
    )
    buyer = harness.agents.run_account(Party.BUYER, run.id, chain_id).address
    assert not [
        r for r in await _rows(harness, run) if r.kind == TxKind.APPROVE and r.sender == buyer
    ]


@pytest.mark.parametrize("field", ["spender", "amount_minor", "nonce", "token"])
async def test_an_approval_described_wrongly_is_never_relayed(
    harness: ControllerHarness, field: str
) -> None:
    def describe(path: str, body: dict[str, Any]) -> dict[str, Any]:
        if path.endswith("/setup-approval"):
            body[field] = {
                "spender": "0x000000000000000000000000000000000000dEaD",
                "amount_minor": "1",
                "nonce": 7,
                "token": str(harness.deployment.base_token_address),
            }[field]
        return body

    harness.agents.rewrite[Party.BUYER] = describe
    run = await harness.validated()
    await harness.controller.start(run.id)
    run = await harness.tick_until(run.id, finished)
    assert run.state_cause == "agent_setup_approval_mismatch"


async def test_a_setup_transaction_that_reverts_fails_the_setup(
    harness: ControllerHarness,
) -> None:
    run = await harness.validated()
    await harness.controller.start(run.id)

    async def minted(run: RunRecord) -> bool:
        return any(row.kind == TxKind.MINT for row in await _rows(harness, run))

    run = await harness.tick_until(run.id, minted)
    mint = next(row for row in await _rows(harness, run) if row.kind == TxKind.MINT)
    async with harness.database.unit_of_work() as uow:
        await uow.outbox.mark_reverted(mint.id, "ERC20InvalidReceiver", "Transaction reverted.")
    run = await harness.tick_until(run.id, finished)
    assert (run.state, run.state_cause) == (RunState.FAILED_SETUP, "mint_reverted")
    async with harness.database.unit_of_work() as uow:
        assert await uow.leases.active_run_id() is None


async def test_a_stuck_approval_is_re_signed_by_its_agent_at_higher_fees(
    harness: ControllerHarness, chain: AnvilChain
) -> None:
    """ADR-072: the relay holds no key for it, so the agent signs its replacement, at the same
    nonce, both fee caps up an eighth, and the wallet is topped up to the new worst case first."""

    def hold_back(path: str, body: dict[str, Any]) -> dict[str, Any]:
        if path.endswith("/setup-approval"):
            chain.automine(False)  # before the backend broadcasts what the agent signed
        return body

    for party in (Party.BUYER, Party.SELLER):
        harness.agents.rewrite[party] = hold_back
    run = await harness.validated()
    await harness.controller.start(run.id)

    async def approvals_sent(run: RunRecord) -> bool:
        rows = await _rows(harness, run)
        return len([row for row in rows if row.kind == TxKind.APPROVE]) == 2

    run = await harness.tick_until(run.id, approvals_sent)
    harness.agents.rewrite.clear()
    originals = [row for row in await _rows(harness, run) if row.kind == TxKind.APPROVE]
    assert chain.pooled() == 2
    for row in originals:
        chain.drop(str(row.tx_hash))
    chain.mine(3)
    chain.automine(True)
    run = await harness.tick_until(run.id, set_up)
    assert run.state == RunState.RUNNING

    rows = await _rows(harness, run)
    for original in originals:
        group = [r for r in rows if r.kind == TxKind.APPROVE and r.sender == original.sender]
        assert len(group) == 2, "one replacement, signed by the agent"
        first, second = (decode_raw_transaction(r.raw_tx) for r in (original, group[-1]))
        assert second["nonce"] == first["nonce"]
        assert second["maxFeePerGas"] == -(-first["maxFeePerGas"] * 9 // 8)
        fundings = [
            decode_raw_transaction(r.raw_tx)["value"]
            for r in rows
            if r.kind == TxKind.FUND_ETH
            and decode_raw_transaction(r.raw_tx)["to"] == original.sender
        ]
        assert sum(fundings) == second["gas"] * second["maxFeePerGas"], "topped up, exactly"
    async with harness.database.unit_of_work() as uow:
        statuses = [
            e.data["status"]
            for e in await uow.run_events.after(run.id, 0, 10_000)
            if e.event_type == "tx.status"
            and e.data["tx_hash"] in {str(o.tx_hash) for o in originals}
        ]
    assert "dropped" in statuses, "a rival's fate is a status transition (api_contract 3)"
    run = await harness.tick_until(run.id, finished)
    assert run.outcome_kind == OutcomeKind.SETTLED


async def test_resume_quotes_the_setup_approval_afresh(harness: ControllerHarness) -> None:
    """ADR-072: terms the agent refused — here a gas limit above its bound — are not asked for
    again after the operator's resume; they are quoted again."""
    from api.controller import ApprovalTerms

    run = await harness.validated()
    await harness.controller.start(run.id)
    setup = harness.controller.driver.setup
    for party in (Party.BUYER, Party.SELLER):
        setup._terms[(run.id, party)] = ApprovalTerms(10**6, 1, 1)
    run = await harness.tick_until(run.id, finished)
    assert (run.state, run.state_cause) == (RunState.RECOVERY_REQUIRED, "agent_validation_error")

    resumed = await harness.controller.resume(run.id)
    assert resumed.state == RunState.PREPARING
    run = await harness.tick_until(run.id, set_up)
    assert run.state == RunState.PAUSED


async def test_post_setup_balances_are_taken_at_the_session_opened_block(
    database: Database, chain: AnvilChain
) -> None:
    """ADR-069. At threshold 2 the session is confirmed only once a later block exists, so the
    head when setup finishes is past the `SessionOpened` block — the case where the two differ."""
    harness = ControllerHarness(database, chain, background=False, threshold=2)
    try:
        run = await harness.validated()
        await harness.controller.step(run.id)
        run = await harness.tick_until(run.id, set_up)
        async with harness.database.unit_of_work() as uow:
            opened = next(
                e
                for e in await uow.chain_events.canonical_for_run(run.id)
                if e.event_name == "SessionOpened"
            )
            post = {
                s.block_number
                for s in await uow.balances.canonical_for_run(run.id)
                if s.stage == SnapshotStage.POST_SETUP
            }
        assert chain.head() > opened.block_number, "control: the head has moved past it"
        assert post == {opened.block_number}
    finally:
        await harness.aclose()
