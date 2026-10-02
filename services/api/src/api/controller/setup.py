"""Setup: from a validated run to a session both agents approved (docs/architecture.md 5.1).

`SessionSetup.advance` does what it can and says why it stopped; the controller polls the indexer
between calls. Each step looks up what was already done **in the outbox** — the record of what was
sent, persisted before it was sent — rather than in a label written afterwards, so a process that
dies between sending and labelling resumes without minting or funding twice (ADR-063: resume carries
setup on from where it stopped). In order:

1. `pre_setup` balances, at the head.
2. Mint each party's initial balances, from the operator.
3. Each party's setup approval (ADR-040), with its test ETH first (ADR-065): the approval's gas
   limit and fee caps are fixed, the wallet is funded with exactly `gas_limit * max_fee_per_gas`,
   and once that funding is confirmed the agent is asked to sign an approval on those same terms,
   which is relayed by its own bytes.
4. Every setup transaction confirmed at the run's threshold.
5. `createSession`, with `runs.session_id` written **before** it is broadcast, because the indexer
   attributes `SessionOpened` to its run by session id as it records it (build_plan stage 2.4).
6. `SessionOpened` confirmed; both agents approve the session from the canonical event and its
   block's time (ADR-044). An agent that refuses it ends the run in `failed_setup` through an abort
   with `execution_failure` (ADR-066).
7. `post_setup` balances, at the head. Their presence is what says setup is done.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final

import structlog
from eth_account import Account

from api.agent_client import (
    AgentClient,
    AgentRefusedError,
    AgentUnavailableError,
    SetupApproval,
    setup_approval_body,
)
from api.chain import CallRequest, ChainAdapter, ExchangeCodec, ExecutionRevertedError
from api.config import RelayPolicy, confirmation_threshold
from api.controller.events import balances
from api.db.enums import Party, SnapshotStage, TxKind, TxStatus
from api.db.protocols import Transactions
from api.db.records import (
    BalanceSnapshotRecord,
    ChainEventRecord,
    DeploymentRecord,
    OutboxRecord,
    RunRecord,
    WalletRecord,
)
from api.indexer import Indexer
from api.relay import (
    Fees,
    Relay,
    RelayError,
    bumped_fees,
    decode_raw_transaction,
    gas_limit,
    initial_fees,
)
from api.turns import AgentSessions, tx_status_event
from negotiation_protocol import Address, Digest, SessionConfig, SessionId

_log = structlog.get_logger(component="controller.setup")

SETUP_KINDS: Final = frozenset({TxKind.FUND_ETH, TxKind.MINT, TxKind.APPROVE})
_GONE: Final = frozenset({TxStatus.DROPPED, TxStatus.REVERTED})
_SETTLED: Final = frozenset({TxStatus.CONFIRMED, TxStatus.FINALIZED})


class SetupStatus(StrEnum):
    #: Something was sent or recorded; the next step can follow at once.
    PROGRESSED = "progressed"
    #: Waiting on the chain; poll and come back.
    WAITING = "waiting"
    #: Both agents approved the session and `post_setup` is recorded.
    DONE = "done"
    #: A setup transaction reverted, before any session could be relied on.
    FAILED = "failed"
    #: An agent refused the opened session (ADR-066).
    SESSION_REFUSED = "session_refused"
    #: An agent did not answer (ADR-064).
    AGENT_UNAVAILABLE = "agent_unavailable"
    #: A defect: an agent refused something it should not have.
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True, slots=True)
class SetupStep:
    status: SetupStatus
    cause: str | None = None


@dataclass(frozen=True, slots=True)
class ApprovalTerms:
    """What the backend asks an agent to sign its approval with (ADR-065)."""

    gas_limit: int
    max_fee_per_gas: int
    max_priority_fee_per_gas: int

    @property
    def worst_case_wei(self) -> int:
        return self.gas_limit * self.max_fee_per_gas


@dataclass(frozen=True, slots=True)
class _State:
    run: RunRecord
    wallets: dict[Party, WalletRecord]
    rows: list[OutboxRecord]
    snapshots: list[BalanceSnapshotRecord]
    events: list[ChainEventRecord]


class SessionSetup:
    def __init__(
        self,
        transactions: Transactions,
        chain: ChainAdapter,
        relay: Relay,
        indexer: Indexer,
        codec: ExchangeCodec,
        deployment: DeploymentRecord,
        agents: Mapping[Party, AgentClient],
        sessions: AgentSessions,
        *,
        relay_policy: RelayPolicy,
        setup_gas_limit_max: int,
        default_threshold: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._transactions = transactions
        self._chain = chain
        self._relay = relay
        self._indexer = indexer
        self._codec = codec
        self._deployment = deployment
        self._agents = agents
        self._sessions = sessions
        self._relay_policy = relay_policy
        self._gas_max = setup_gas_limit_max
        self._default_threshold = default_threshold
        self._clock = clock
        #: Terms decided for an approval whose funding is in flight. In memory: after a restart they
        #: are decided again, and a wallet funded for older terms is topped up to the new ones.
        self._terms: dict[tuple[uuid.UUID, Party], ApprovalTerms] = {}

    async def advance(self, run: RunRecord) -> SetupStep:
        state = await self._load(run)
        failed = _reverted(state.rows)
        if failed is not None:
            return SetupStep(SetupStatus.FAILED, f"{failed.value}_reverted")
        steps = (
            self._pre_setup,
            self._mint,
            self._approvals,
            self._setup_confirmed,
            self._open_session,
            self._session_confirmed,
            self._approve_session,
            self._post_setup,
        )
        for step in steps:
            result = await step(state)
            if result is not None:
                return result
        return SetupStep(SetupStatus.DONE)

    def complete(self, snapshots: Sequence[BalanceSnapshotRecord]) -> bool:
        return any(snapshot.stage == SnapshotStage.POST_SETUP for snapshot in snapshots)

    async def _load(self, run: RunRecord) -> _State:
        async with self._transactions.unit_of_work() as uow:
            current = await uow.runs.get(run.id) or run
            wallets = {wallet.party: wallet for wallet in await uow.wallets.list_for_run(run.id)}
            rows = await uow.outbox.list_for_run(run.id)
            snapshots = await uow.balances.canonical_for_run(run.id)
            events = await uow.chain_events.canonical_for_run(run.id)
        return _State(current, wallets, rows, snapshots, events)

    # -----------------------------------------------------------------------------------------
    # 1 and 7. Balances
    # -----------------------------------------------------------------------------------------

    async def _pre_setup(self, state: _State) -> SetupStep | None:
        if any(snapshot.stage == SnapshotStage.PRE_SETUP for snapshot in state.snapshots):
            return None
        await self._snapshot(state, SnapshotStage.PRE_SETUP)
        return SetupStep(SetupStatus.PROGRESSED)

    async def _post_setup(self, state: _State) -> SetupStep | None:
        """At the `SessionOpened` block, which is confirmed by now (ADR-069): every setup
        transaction was confirmed before `createSession` was sent, so its balances include them
        all, and a reorg within the threshold cannot take the snapshot away."""
        opened = next(e for e in state.events if e.event_name == "SessionOpened")
        await self._snapshot(state, SnapshotStage.POST_SETUP, opened.block_number)
        return None

    async def _snapshot(
        self, state: _State, stage: SnapshotStage, number: int | None = None
    ) -> None:
        if number is None:
            number = (await self._chain.head()).number
        parties = {party: wallet.address for party, wallet in state.wallets.items()}
        recorded = await self._indexer.snapshot(state.run.id, stage, number, parties)
        async with self._transactions.unit_of_work() as uow:
            for event in balances(recorded):
                await uow.run_events.append(state.run.id, "balances", event)

    # -----------------------------------------------------------------------------------------
    # 2. Minting
    # -----------------------------------------------------------------------------------------

    async def _mint(self, state: _State) -> SetupStep | None:
        sent = False
        for party, wallet in state.wallets.items():
            for label, token, amount in (
                ("mint_base", self._deployment.base_token_address, wallet.initial_base_minor),
                ("mint_quote", self._deployment.quote_token_address, wallet.initial_quote_minor),
            ):
                data = self._codec.encode_mint(wallet.address, int(amount))
                if int(amount) == 0 or _sent(state.rows, TxKind.MINT, token, data):
                    continue
                row = await self._relay.submit_call(
                    state.run.id, TxKind.MINT, token, data, as_operator=True
                )
                await self._recorded(state.run.id, party, label, row)
                sent = True
        return SetupStep(SetupStatus.PROGRESSED) if sent else None

    # -----------------------------------------------------------------------------------------
    # 3. Funding and setup approvals
    # -----------------------------------------------------------------------------------------

    async def _approvals(self, state: _State) -> SetupStep | None:
        results = [
            await self._approval(state, party, wallet) for party, wallet in state.wallets.items()
        ]
        for status in (SetupStatus.RECOVERY_REQUIRED, SetupStatus.AGENT_UNAVAILABLE):
            found = next((step for step in results if step and step.status == status), None)
            if found is not None:
                return found
        if any(step is not None and step.status == SetupStatus.PROGRESSED for step in results):
            return SetupStep(SetupStatus.PROGRESSED)
        if any(step is not None for step in results):
            return SetupStep(SetupStatus.WAITING)
        return None

    async def _approval(
        self, state: _State, party: Party, wallet: WalletRecord
    ) -> SetupStep | None:
        sent = [
            row
            for row in state.rows
            if row.kind == TxKind.APPROVE
            and row.sender == wallet.address
            and row.status not in _GONE
        ]
        if sent:
            replacement = await self._replacement(sent[-1])
            if replacement is None:
                return None
            terms, nonce = replacement, sent[-1].nonce
        else:
            terms = await self._terms_for(state.run.id, party, wallet)
            nonce = wallet.setup_nonce_next
        funding = [
            row
            for row in state.rows
            if row.kind == TxKind.FUND_ETH
            and row.status not in _GONE
            and decode_raw_transaction(row.raw_tx)["to"] == wallet.address
        ]
        if any(row.status not in _SETTLED and row.status != TxStatus.REPLACED for row in funding):
            return SetupStep(SetupStatus.WAITING)
        head = await self._chain.head()
        balance = await self._chain.eth_balance(wallet.address, head.number)
        if balance < terms.worst_case_wei:
            row = await self._relay.submit_call(
                state.run.id,
                TxKind.FUND_ETH,
                wallet.address,
                b"",
                as_operator=True,
                value=terms.worst_case_wei - balance,
            )
            label = "fund_eth" if not funding else f"fund_eth_{len(funding) + 1}"
            await self._recorded(state.run.id, party, label, row)
            return SetupStep(SetupStatus.PROGRESSED)
        return await self._request_approval(state.run.id, party, wallet, terms, nonce)

    async def _replacement(self, newest: OutboxRecord) -> ApprovalTerms | None:
        """ADR-072: an approval still not included `replace_after_blocks` after its broadcast is
        asked of its agent again, at the same nonce, with both fee caps raised as ADR-050 raises
        them — the relay holds no key to re-sign it. None while it is not stuck, or at the
        ceiling."""
        if (
            newest.status != TxStatus.SUBMITTED
            or newest.block_number is not None
            or newest.submitted_block is None
        ):
            return None
        head = await self._chain.head()
        if head.number - newest.submitted_block < self._relay_policy.replace_after_blocks:
            return None
        fields = decode_raw_transaction(newest.raw_tx)
        fees = bumped_fees(
            Fees(fields["maxFeePerGas"], fields["maxPriorityFeePerGas"]), self._relay_policy
        )
        if fees is None:
            return None
        return ApprovalTerms(fields["gas"], fees.max_fee_per_gas, fees.max_priority_fee_per_gas)

    def forget(self, run_id: uuid.UUID) -> None:
        """Drop the run's cached approval terms, so they are quoted afresh (ADR-072)."""
        for key in [key for key in self._terms if key[0] == run_id]:
            del self._terms[key]

    async def _terms_for(
        self, run_id: uuid.UUID, party: Party, wallet: WalletRecord
    ) -> ApprovalTerms:
        key = (run_id, party)
        if key not in self._terms:
            fees = initial_fees(await self._chain.fee_quote(), self._relay_policy)
            data = self._codec.encode_approve(
                self._deployment.exchange_address, int(wallet.allowance_minor)
            )
            try:
                estimate: int | None = await self._chain.estimate_gas(
                    CallRequest(
                        sender=wallet.address, to=_token(self._deployment, party), data=data
                    )
                )
            except ExecutionRevertedError:
                estimate = None
            self._terms[key] = ApprovalTerms(
                gas_limit=min(gas_limit(estimate, self._relay_policy), self._gas_max),
                max_fee_per_gas=fees.max_fee_per_gas,
                max_priority_fee_per_gas=fees.max_priority_fee_per_gas,
            )
        return self._terms[key]

    async def _request_approval(
        self,
        run_id: uuid.UUID,
        party: Party,
        wallet: WalletRecord,
        terms: ApprovalTerms,
        nonce: int,
    ) -> SetupStep:
        body = setup_approval_body(
            nonce, terms.gas_limit, terms.max_fee_per_gas, terms.max_priority_fee_per_gas
        )
        try:
            approval = await self._ask_for_approval(run_id, party, body)
        except AgentUnavailableError:
            return SetupStep(SetupStatus.AGENT_UNAVAILABLE, "agent_unavailable")
        except AgentRefusedError as refusal:
            return SetupStep(SetupStatus.RECOVERY_REQUIRED, f"agent_{refusal.code}")
        raw = bytes.fromhex(approval.raw_tx.removeprefix("0x"))
        if not self._matches(approval, raw, party, wallet, terms, nonce):
            return SetupStep(SetupStatus.RECOVERY_REQUIRED, "agent_setup_approval_mismatch")
        row = await self._relay.submit_presigned(run_id, TxKind.APPROVE, raw)
        async with self._transactions.unit_of_work() as uow:
            await uow.wallets.set_setup_nonce(
                run_id, party, max(wallet.setup_nonce_next, nonce + 1)
            )
        await self._recorded(run_id, party, "approve", row)
        self._terms.pop((run_id, party), None)
        return SetupStep(SetupStatus.PROGRESSED)

    async def _ask_for_approval(
        self, run_id: uuid.UUID, party: Party, body: dict[str, object]
    ) -> SetupApproval:
        """An agent restarted during setup has forgotten the run: it is provisioned again with the
        stored address — there is no session yet to approve — and asked once more (ADR-048)."""
        try:
            return await self._agents[party].setup_approval(run_id, body)
        except AgentRefusedError as refusal:
            if not refusal.unprovisioned:
                raise
        await self._sessions.reprovision(run_id, party)
        return await self._agents[party].setup_approval(run_id, body)

    def _matches(
        self,
        approval: SetupApproval,
        raw: bytes,
        party: Party,
        wallet: WalletRecord,
        terms: ApprovalTerms,
        nonce: int,
    ) -> bool:
        """The agent built the approval from its own provisioned state (ADR-040). This checks the
        bytes that will be relayed, not only the agent's description of them: sent from the
        party's wallet, an `approve` of the exchange for the allowance on the party's token, at
        the nonce and on the terms asked for, on this chain, carrying no value."""
        token = _token(self._deployment, party)
        try:
            fields = decode_raw_transaction(raw)
            sender = Address(Account.recover_transaction(raw))
        except (RelayError, ValueError, TypeError):
            return False
        allowance = int(wallet.allowance_minor)
        return (
            Address(approval.from_) == wallet.address == sender
            and Address(approval.token) == token == fields["to"]
            and Address(approval.spender) == self._deployment.exchange_address
            and int(approval.amount_minor) == allowance
            and approval.nonce == nonce == fields["nonce"]
            and fields["data"]
            == self._codec.encode_approve(self._deployment.exchange_address, allowance)
            and fields["value"] == 0
            and fields["chainId"] == self._deployment.chain_id
            and fields["gas"] == terms.gas_limit
            and fields["maxFeePerGas"] == terms.max_fee_per_gas
            and fields["maxPriorityFeePerGas"] == terms.max_priority_fee_per_gas
        )

    async def _recorded(
        self, run_id: uuid.UUID, party: Party, label: str, row: OutboxRecord
    ) -> None:
        async with self._transactions.unit_of_work() as uow:
            await uow.wallets.record_funding_tx(run_id, party, label, row.tx_hash)
            await uow.run_events.append(
                run_id,
                "tx.status",
                tx_status_event(row, None, explorer_base_url=self._deployment.explorer_base_url),
            )

    # -----------------------------------------------------------------------------------------
    # 4. Setup confirmed
    # -----------------------------------------------------------------------------------------

    async def _setup_confirmed(self, state: _State) -> SetupStep | None:
        live = [row for row in state.rows if row.kind in SETUP_KINDS and row.status not in _GONE]
        mined = [row for row in live if row.status != TxStatus.REPLACED]
        if mined and all(row.status in _SETTLED for row in mined):
            return None
        return SetupStep(SetupStatus.WAITING)

    # -----------------------------------------------------------------------------------------
    # 5 and 6. The session
    # -----------------------------------------------------------------------------------------

    async def _open_session(self, state: _State) -> SetupStep | None:
        if any(row.kind == TxKind.CREATE_SESSION and row.status not in _GONE for row in state.rows):
            return None
        run = state.run
        config = await self._config(state)
        if run.session_id is None:
            async with self._transactions.unit_of_work() as uow:
                # Before the broadcast: the indexer attributes SessionOpened by session id.
                await uow.runs.set_session(
                    run.id,
                    SessionId(config.session_id),
                    Digest(
                        config.config_hash(
                            self._deployment.base_token_address,
                            self._deployment.quote_token_address,
                        )
                    ),
                    config.expires_at,
                    self._clock(),
                )
        row = await self._relay.submit_call(
            run.id,
            TxKind.CREATE_SESSION,
            self._deployment.exchange_address,
            self._codec.encode_create_session(config),
            as_operator=True,
        )
        async with self._transactions.unit_of_work() as uow:
            await uow.run_events.append(
                run.id,
                "tx.status",
                tx_status_event(row, None, explorer_base_url=self._deployment.explorer_base_url),
            )
        return SetupStep(SetupStatus.PROGRESSED)

    async def _config(self, state: _State) -> SessionConfig:
        """A fresh session, or — after a crash between recording it and sending it — the one
        already recorded, so the session id the indexer looks for is the one sent."""
        run = state.run
        public = run.public_config
        if run.session_id is not None and run.session_expires_at_ts is not None:
            session_id = bytes.fromhex(str(run.session_id)[2:])
            expires_at = run.session_expires_at_ts
        else:
            session_id = secrets.token_bytes(32)
            expires_at = (await self._chain.head()).timestamp + int(public["session_duration_s"])
        return SessionConfig(
            session_id,
            str(state.wallets[Party.BUYER].address),
            str(state.wallets[Party.SELLER].address),
            int(public["base_amount_minor"]),
            expires_at,
            int(public["max_offers"]),
        )

    async def _session_confirmed(self, state: _State) -> SetupStep | None:
        threshold = confirmation_threshold(state.run.public_config, self._default_threshold)
        opened = next((e for e in state.events if e.event_name == "SessionOpened"), None)
        if opened is not None and (opened.confirmations_at_index or 0) >= threshold:
            return None
        return SetupStep(SetupStatus.WAITING)

    async def _approve_session(self, state: _State) -> SetupStep | None:
        for party in (Party.BUYER, Party.SELLER):
            try:
                await self._sessions.approve(state.run.id, party)
            except AgentUnavailableError:
                return SetupStep(SetupStatus.AGENT_UNAVAILABLE, "agent_unavailable")
            except AgentRefusedError as refusal:
                if refusal.unprovisioned:
                    try:
                        await self._sessions.restore(state.run.id, party)
                        continue
                    except AgentUnavailableError:
                        return SetupStep(SetupStatus.AGENT_UNAVAILABLE, "agent_unavailable")
                    except AgentRefusedError as again:
                        refusal = again
                if refusal.code == "session_mismatch":
                    _log.warning(
                        "setup.session_refused", run_id=str(state.run.id), party=party.value
                    )
                    return SetupStep(SetupStatus.SESSION_REFUSED, "session_refused")
                return SetupStep(SetupStatus.RECOVERY_REQUIRED, f"agent_{refusal.code}")
        return None


def _token(deployment: DeploymentRecord, party: Party) -> Address:
    """The token a party approves: what it gives up — quote for the buyer, base for the seller."""
    return deployment.quote_token_address if party == Party.BUYER else deployment.base_token_address


def _sent(rows: Sequence[OutboxRecord], kind: TxKind, to: Address, data: bytes) -> bool:
    for row in rows:
        if row.kind != kind or row.status in _GONE:
            continue
        fields = decode_raw_transaction(row.raw_tx)
        if fields["to"] == to and fields["data"] == data:
            return True
    return False


def _reverted(rows: Sequence[OutboxRecord]) -> TxKind | None:
    for row in rows:
        if row.status == TxStatus.REVERTED and row.kind in SETUP_KINDS | {TxKind.CREATE_SESSION}:
            return row.kind
    return None
