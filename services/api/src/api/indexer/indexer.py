"""The indexer: what the chain says happened, recorded, and re-checked until it is final.

One `poll()` does five things, in an order that matters:

1. **Reorg check first** (docs/architecture.md section 5.5). Every canonical event, every recorded
   inclusion and every balance snapshot above the RPC's finalized head, for runs not yet terminal
   (ADR-053, Q20), is compared with the chain's block hash at its height. Each row whose block the
   chain no longer has is marked non-canonical, its inclusion cleared back to `submitted` so the
   relay reconciles it, and each affected run gets a `chain.reorg` run event in the same transaction
   (ADR-058) — before anything new is read on top of a fork that is gone. A block the RPC fails to
   return is an unanswered question, never a mismatch: the adapter raises, and the poll fails as an
   outage rather than inventing a reorg (stage 2.3 review).
2. **Receipts.** Every transaction still awaiting one — a `replaced` original or a `dropped`
   sibling included — is looked up by hash. A poll describes the chain at the head it read, so a
   receipt in a newer block waits for the next poll. A receipt records the inclusion; the first
   included transaction of a nonce group, or of a signed action, drops the others. A receipt with
   status 0 is an execution failure: the revert is replayed against the inclusion block and then
   its parent, decoded to its protocol error (ADR-017), or recorded as `Undetermined` when neither
   replay reproduces it (ADR-056); its timeline sentence is rendered and stored (ADR-051).
3. **The log scan**, from the manifest's `start_block` on the first poll and after that from the
   lower of where the last poll stopped and the first unfinalized block, in chunks the RPC will
   serve. It finds events from transactions this backend did not send — an expiry anyone may call,
   say — as well as its own, including ones a reorg put at heights an earlier poll had read.
4. **Recording events.** Each exchange event is attributed to its run through its `sessionId`,
   stored with its block hash and the calldata of its transaction (A15), and given its timeline
   sentence, rendered once by the renderer the composition root supplies (ADR-024). The same log
   seen again in the same block is the same row, made canonical again if a rewind had invalidated
   it (ADR-055).
5. **Depth.** Confirmations are updated for every event above the finalized head and every event of
   a watched run, at any height; a transaction at the run's threshold is `confirmed`, and at or
   below the finalized head `finalized` — two confirmations are never called final (spec 9.3). A
   watched run's terminal event at the threshold has its balances snapshotted and, for a
   settlement, its receipt verified (section 5.3), and is reported on **every** poll until the run
   is terminal: the report is a statement of the chain's state, not a one-shot message (ADR-058).

The indexer never writes an economic outcome and never changes a run's operational state: it
reports, and the controller decides (ADR-052). A fault in one run's records — a threshold the run's
configuration states invalidly (ADR-059), a terminal event without its session's opening — is
reported as a `RunProblem` for that run and never stops the poll for the others.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final, Protocol

from api.chain import (
    CallRequest,
    ChainAdapter,
    DecodedEvent,
    ExchangeCodec,
    ExecutionRevertedError,
    RawLog,
    Receipt,
)
from api.config import IndexerPolicy, InvalidRunConfigError, confirmation_threshold
from api.db.enums import (
    ActionStatus,
    Party,
    RunState,
    SnapshotStage,
    TokenRole,
    TxStatus,
)
from api.db.protocols import Transactions, UnitOfWork
from api.db.records import (
    BalanceSnapshotRecord,
    ChainEventRecord,
    DeploymentRecord,
    Inclusion,
    NewBalanceSnapshot,
    NewChainEvent,
    OutboxRecord,
    RunRecord,
)
from api.indexer.reports import (
    PollReport,
    Reorg,
    RunProblem,
    SettlementCheck,
    TerminalConfirmed,
)
from api.indexer.settlement import check_settlement
from negotiation_protocol import Address, Digest, MinorAmount, SessionId

TERMINAL_EVENT_NAMES: Final = frozenset(
    {"SettlementCompleted", "SessionClosed", "SessionExpired", "SessionAborted"}
)

#: A status-0 receipt whose revert neither replay reproduces (ADR-056). Not a protocol error name,
#: and not `NoRevertData`, which is a replay that reverted with nothing to decode.
UNDETERMINED_REVERT: Final = "Undetermined"

#: The signed action's status that mirrors its live transaction's.
_ACTION_STATUS: Final = {
    TxStatus.INCLUDED: ActionStatus.INCLUDED,
    TxStatus.CONFIRMED: ActionStatus.CONFIRMED,
    TxStatus.FINALIZED: ActionStatus.FINALIZED,
    TxStatus.REVERTED: ActionStatus.REVERTED,
}


class SentenceRenderer(Protocol):
    """What the indexer needs to store a timeline sentence (ADR-024). The projection provides it."""

    def event_sentence(
        self, name: str, args: Mapping[str, Any], earlier: Sequence[tuple[str, Mapping[str, Any]]]
    ) -> str | None: ...

    def execution_failure_sentence(self, error_name: str) -> str: ...


def _utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass
class _Poll:
    """Everything one poll learns, collected as it goes and reported at the end."""

    head: int
    finalized: int
    watched: dict[uuid.UUID, RunRecord] = field(default_factory=dict)
    runs: dict[uuid.UUID, RunRecord | None] = field(default_factory=dict)
    logs: dict[tuple[Digest, Digest, int], RawLog] = field(default_factory=dict)
    included: list[OutboxRecord] = field(default_factory=list)
    reverted: list[OutboxRecord] = field(default_factory=list)
    dropped: list[OutboxRecord] = field(default_factory=list)
    problems: dict[tuple[uuid.UUID, str], RunProblem] = field(default_factory=dict)

    def problem(self, run_id: uuid.UUID, code: str, detail: str) -> None:
        self.problems.setdefault((run_id, code), RunProblem(run_id, code, detail))


class Indexer:
    def __init__(
        self,
        transactions: Transactions,
        chain: ChainAdapter,
        codec: ExchangeCodec,
        deployment: DeploymentRecord,
        renderer: SentenceRenderer,
        *,
        policy: IndexerPolicy,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._transactions = transactions
        self._chain = chain
        self._codec = codec
        self._chain_id = deployment.chain_id
        #: ADR-081: only this deployment's runs are watched, polled and re-verified. A run on
        #: another chain — a restarted Anvil's predecessor — is never compared with this one.
        self._deployment_id = deployment.deployment_id
        self._renderer = renderer
        self._policy = policy
        self._clock = clock
        #: The next block the log scan reads. In memory: a restart rescans from `start_block`,
        #: which is idempotent, because a log already recorded is not recorded again.
        self._start_block = deployment.start_block
        self._next_block = deployment.start_block

    async def poll(self) -> PollReport:
        head = await self._chain.head()
        finalized = min(await self._chain.finalized_number(), head.number)
        poll = _Poll(head=head.number, finalized=finalized)
        async with self._transactions.unit_of_work() as uow:
            poll.watched = {
                run.id: run for run in await uow.runs.with_open_sessions(self._deployment_id)
            }
        reorg = await self._check_reorg(poll)
        await self._poll_receipts(poll)
        await self._scan_logs(poll)
        indexed = await self._record_events(poll)
        confirmed, final = await self._update_depth(poll)
        terminal = await self._confirm_terminal(poll)
        return PollReport(
            head_block=poll.head,
            finalized_block=poll.finalized,
            reorg=reorg,
            indexed=tuple(indexed),
            included=tuple(poll.included),
            reverted=tuple(poll.reverted),
            confirmed=tuple(confirmed),
            finalized=tuple(final),
            terminal=tuple(terminal),
            problems=tuple(poll.problems.values()),
            dropped=tuple(poll.dropped),
        )

    # -----------------------------------------------------------------------------------------
    # Runs and thresholds
    # -----------------------------------------------------------------------------------------

    async def _run(self, uow: UnitOfWork, poll: _Poll, run_id: uuid.UUID) -> RunRecord | None:
        if run_id in poll.watched:
            return poll.watched[run_id]
        if run_id not in poll.runs:
            poll.runs[run_id] = await uow.runs.get(run_id)
        return poll.runs[run_id]

    def _threshold(self, poll: _Poll, run: RunRecord | None) -> int | None:
        """The run's threshold, or None — reported — when its configuration states one invalidly."""
        default = self._policy.default_confirmation_threshold
        if run is None:
            return default
        try:
            return confirmation_threshold(run.public_config, default)
        except InvalidRunConfigError as error:
            poll.problem(run.id, "invalid_confirmation_threshold", str(error))
            return None

    def _watched(self, run: RunRecord | None) -> bool:
        # Q20, interim answer: a terminal run is no longer watched for reorgs; nor is a run on
        # another chain (ADR-081).
        if run is None:
            return True
        return run.state != RunState.TERMINAL and run.deployment_id == self._deployment_id

    # -----------------------------------------------------------------------------------------
    # 1. Reorg check
    # -----------------------------------------------------------------------------------------

    async def _check_reorg(self, poll: _Poll) -> Reorg | None:
        stored: dict[int, set[Digest]] = {}

        def watch(number: int | None, block_hash: Digest | None) -> None:
            if number is not None and block_hash is not None and number > poll.finalized:
                stored.setdefault(number, set()).add(block_hash)

        async with self._transactions.unit_of_work() as uow:
            for event in await uow.chain_events.canonical_from_block(
                self._chain_id, self._codec.exchange, poll.finalized + 1
            ):
                if event.run_id is None or self._watched(await self._run(uow, poll, event.run_id)):
                    watch(event.block_number, event.block_hash)
            for row in await uow.outbox.with_block_from(poll.finalized + 1, self._deployment_id):
                if self._watched(await self._run(uow, poll, row.run_id)):
                    watch(row.block_number, row.block_hash)
            for run_id in poll.watched:
                for snapshot in await uow.balances.canonical_for_run(run_id):
                    watch(snapshot.block_number, snapshot.block_hash)

        stale, fork = await self._stale(stored)
        return None if fork is None else await self._rewind(poll, stale, fork)

    async def _stale(self, stored: Mapping[int, set[Digest]]) -> tuple[set[Digest], int | None]:
        """Every stored block hash the chain no longer has at its height, and the lowest height.

        A height above the head has lost every block stored at it. `ChainAdapter.block` raises,
        rather than answering, when it cannot tell — so an RPC fault fails the poll instead of
        being taken for a reorg.
        """
        stale: set[Digest] = set()
        fork: int | None = None
        for number in sorted(stored):
            block = await self._chain.block(number)
            gone = stored[number] if block is None else stored[number] - {block.hash}
            if gone:
                stale |= gone
                fork = number if fork is None else fork
        return stale, fork

    async def _rewind(self, poll: _Poll, stale: set[Digest], fork: int) -> Reorg:
        now = self._clock()
        hashes = sorted(stale)
        digests: dict[uuid.UUID, list[Digest]] = {}
        async with self._transactions.unit_of_work() as uow:
            events = await uow.chain_events.invalidate_blocks(
                self._chain_id, self._codec.exchange, hashes, now
            )
            for event in events:
                if event.run_id is not None:
                    found = digests.setdefault(event.run_id, [])
                    if event.event_name == "OfferRecorded":
                        found.append(Digest(str(event.decoded["offerHash"])))
            reset = await self._reset_inclusions(uow, stale, fork, digests)
            for run_id in poll.watched:
                if await uow.balances.invalidate_blocks(run_id, hashes):
                    digests.setdefault(run_id, [])
            for run_id, found in digests.items():
                await uow.run_events.append(
                    run_id,
                    "chain.reorg",
                    {
                        "from_block": fork,
                        "to_block": poll.head,
                        "invalidated_digests": [str(digest) for digest in dict.fromkeys(found)],
                    },
                )
        self._next_block = min(self._next_block, fork)
        everything = [digest for found in digests.values() for digest in found]
        return Reorg(
            fork_block=fork,
            head_block=poll.head,
            invalidated_events=tuple(events),
            reset_transactions=tuple(reset),
            invalidated_digests=tuple(dict.fromkeys(everything)),
            run_ids=tuple(sorted(digests, key=str)),
        )

    async def _reset_inclusions(
        self,
        uow: UnitOfWork,
        stale: set[Digest],
        fork: int,
        digests: dict[uuid.UUID, list[Digest]],
    ) -> list[OutboxRecord]:
        reset: list[OutboxRecord] = []
        for row in await uow.outbox.with_block_from(fork, self._deployment_id):
            if row.block_hash not in stale:
                continue
            reset.append(await uow.outbox.clear_inclusion(row.id))
            found = digests.setdefault(row.run_id, [])
            if row.signed_action_id is not None:
                action = await uow.signed_actions.reset_to_submitted(row.signed_action_id)
                found.append(action.digest)
        return reset

    # -----------------------------------------------------------------------------------------
    # 2. Receipts
    # -----------------------------------------------------------------------------------------

    async def _poll_receipts(self, poll: _Poll) -> None:
        async with self._transactions.unit_of_work() as uow:
            waiting = await uow.outbox.awaiting_receipt(self._deployment_id)
        settled_groups: set[tuple[Address, int]] = set()
        for row in waiting:
            if (row.sender, row.nonce) in settled_groups:
                continue
            receipt = await self._chain.receipt(row.tx_hash)
            if receipt is None or receipt.block_number > poll.head:
                # A block mined after this poll read its head is the next poll's: recorded now, it
                # would sit above the head at depth 0 and be judged against a chain not yet read.
                continue
            settled_groups.add((row.sender, row.nonce))
            await self._record_inclusion(poll, row, receipt)

    async def _record_inclusion(self, poll: _Poll, row: OutboxRecord, receipt: Receipt) -> None:
        revert = None if receipt.succeeded else await self._decode_revert(row, receipt)
        inclusion = Inclusion(
            block_number=receipt.block_number,
            block_hash=receipt.block_hash,
            gas_used=MinorAmount(receipt.gas_used),
            effective_gas_price_wei=MinorAmount(receipt.effective_gas_price),
            included_at=self._clock(),
        )
        async with self._transactions.unit_of_work() as uow:
            poll.dropped.extend(await self._drop_rivals(uow, row))
            included = await uow.outbox.mark_included(row.id, inclusion)
            if revert is not None:
                sentence = self._renderer.execution_failure_sentence(revert)
                included = await uow.outbox.mark_reverted(row.id, revert, sentence)
                poll.reverted.append(included)
            await self._mirror_action(uow, included, revert)
        poll.included.append(included)
        for log in receipt.logs:
            poll.logs[(log.block_hash, log.tx_hash, log.log_index)] = log

    async def _drop_rivals(self, uow: UnitOfWork, row: OutboxRecord) -> list[OutboxRecord]:
        """The mined transaction wins: every other live one at its nonce, or for its signed action,
        can never be mined as well, and the index of one live transaction per action is kept."""
        rivals = list(await uow.outbox.nonce_group(row.sender, row.nonce, self._deployment_id))
        if row.signed_action_id is not None:
            live = await uow.outbox.live_for_signed_action(row.signed_action_id)
            if live is not None:
                rivals.append(live)
        dropped = []
        for rival in rivals:
            if rival.id != row.id and rival.status in (TxStatus.PENDING, TxStatus.SUBMITTED):
                dropped.append(await uow.outbox.update_status(rival.id, TxStatus.DROPPED))
        return dropped

    async def _decode_revert(self, row: OutboxRecord, receipt: Receipt) -> str:
        """Replay the transaction to read its revert data (ADR-056).

        First against the inclusion block, whose timestamp is the one the transaction ran at — a
        deadline revert reproduces there and nowhere earlier — then against its parent, whose
        state is the one before the block. Neither reverting is `Undetermined`.
        """
        tx = await self._chain.transaction(row.tx_hash)
        if tx is None or tx.to is None:
            return UNDETERMINED_REVERT
        request = CallRequest(sender=tx.sender, to=tx.to, data=tx.input, value=tx.value, gas=tx.gas)
        for number in dict.fromkeys((receipt.block_number, max(receipt.block_number - 1, 0))):
            try:
                await self._chain.call(request, number)
            except ExecutionRevertedError as error:
                return self._codec.decode_revert(error.data).name
        return UNDETERMINED_REVERT

    @staticmethod
    async def _mirror_action(uow: UnitOfWork, row: OutboxRecord, revert: str | None) -> None:
        status = _ACTION_STATUS.get(row.status)
        if row.signed_action_id is not None and status is not None:
            await uow.signed_actions.update_status(row.signed_action_id, status, revert)

    # -----------------------------------------------------------------------------------------
    # 3. Log scan
    # -----------------------------------------------------------------------------------------

    async def _scan_logs(self, poll: _Poll) -> None:
        # Everything above the finalized head is read again on every poll. A reorg that removes no
        # row this backend stored is invisible to the reorg check, and a cursor that only rewound
        # on a detected reorg would never read the logs the new fork put at heights it had passed.
        # Re-reading the unfinalized window costs one `eth_getLogs` and records nothing twice.
        start = max(self._start_block, min(self._next_block, poll.finalized + 1))
        while start <= poll.head:
            end = min(start + self._policy.log_chunk_blocks - 1, poll.head)
            for log in await self._chain.logs(self._codec.exchange, start, end):
                poll.logs[(log.block_hash, log.tx_hash, log.log_index)] = log
            start = end + 1

    # -----------------------------------------------------------------------------------------
    # 4. Recording events
    # -----------------------------------------------------------------------------------------

    async def _record_events(self, poll: _Poll) -> list[ChainEventRecord]:
        ordered = sorted(poll.logs.values(), key=lambda log: (log.block_number, log.log_index))
        decoded = [event for log in ordered if (event := self._codec.decode_event(log)) is not None]
        calldata = await self._calldata(decoded)
        first_in_tx: dict[Digest, int] = {}
        for event in decoded:
            first_in_tx.setdefault(event.log.tx_hash, event.log.log_index)

        recorded: list[ChainEventRecord] = []
        async with self._transactions.unit_of_work() as uow:
            for event in decoded:
                document = (
                    calldata.get(event.log.tx_hash)
                    if first_in_tx[event.log.tx_hash] == event.log.log_index
                    else None
                )
                row = await self._record_event(uow, poll, event, document)
                if row is not None:
                    recorded.append(row)
        self._next_block = max(self._next_block, poll.head + 1)
        return recorded

    async def _calldata(self, events: Iterable[DecodedEvent]) -> dict[Digest, dict[str, Any]]:
        documents: dict[Digest, dict[str, Any]] = {}
        for tx_hash in dict.fromkeys(event.log.tx_hash for event in events):
            tx = await self._chain.transaction(tx_hash)
            if tx is None or tx.to is None:
                continue
            call = self._codec.decode_call(tx.to, tx.input)
            if call is not None:
                documents[tx_hash] = call.as_document(tx.to, tx.input)
        return documents

    async def _record_event(
        self,
        uow: UnitOfWork,
        poll: _Poll,
        event: DecodedEvent,
        calldata: dict[str, Any] | None,
    ) -> ChainEventRecord | None:
        session_id = SessionId(event.session_id)
        run = await uow.runs.get_by_session(session_id)
        if run is not None:
            poll.runs[run.id] = run
        earlier = [
            (row.event_name, row.decoded)
            for row in await uow.chain_events.canonical_for_session(session_id)
            if (row.block_number, row.log_index) < (event.log.block_number, event.log.log_index)
        ]
        return await uow.chain_events.add(
            NewChainEvent(
                chain_id=self._chain_id,
                contract_address=self._codec.exchange,
                block_number=event.log.block_number,
                block_hash=event.log.block_hash,
                tx_hash=event.log.tx_hash,
                log_index=event.log.log_index,
                event_name=event.name,
                decoded=event.args,
                run_id=None if run is None else run.id,
                session_id=session_id,
                calldata=calldata,
                confirmations_at_index=max(poll.head - event.log.block_number + 1, 0),
                sentence=self._sentence(event, earlier),
            )
        )

    def _sentence(
        self, event: DecodedEvent, earlier: Sequence[tuple[str, Mapping[str, Any]]]
    ) -> str | None:
        try:
            return self._renderer.event_sentence(event.name, event.args, earlier)
        except ValueError:
            # A session whose opening this indexer never saw cannot be described. Its rows are
            # still recorded: the evidence does not depend on the sentence.
            return None

    # -----------------------------------------------------------------------------------------
    # 5. Depth, finality, terminal events
    # -----------------------------------------------------------------------------------------

    async def _update_depth(self, poll: _Poll) -> tuple[list[OutboxRecord], list[OutboxRecord]]:
        confirmed: list[OutboxRecord] = []
        final: list[OutboxRecord] = []
        async with self._transactions.unit_of_work() as uow:
            for event in await self._depth_watched(uow, poll):
                depth = poll.head - event.block_number + 1
                if event.confirmations_at_index != depth and depth >= 0:
                    await uow.chain_events.set_confirmations(event.id, depth)
            for row in await uow.outbox.in_status(
                TxStatus.INCLUDED, TxStatus.CONFIRMED, deployment_id=self._deployment_id
            ):
                status = await self._settled_status(uow, poll, row)
                if status is not None:
                    updated = await uow.outbox.update_status(row.id, status)
                    await self._mirror_action(uow, updated, None)
                    (final if status == TxStatus.FINALIZED else confirmed).append(updated)
        return confirmed, final

    async def _depth_watched(self, uow: UnitOfWork, poll: _Poll) -> list[ChainEventRecord]:
        """Every event above the finalized head, and every event of a watched run at any height.

        The second half is why an event that passed the finalized head between two polls still
        reaches its run's threshold in `confirmations_at_index` (data model invariant 7).
        """
        events: dict[uuid.UUID, ChainEventRecord] = {}
        for event in await uow.chain_events.canonical_from_block(
            self._chain_id, self._codec.exchange, poll.finalized + 1
        ):
            run = None if event.run_id is None else await self._run(uow, poll, event.run_id)
            # ADR-081: another chain's event has no depth on this one.
            if run is None or run.deployment_id == self._deployment_id:
                events[event.id] = event
        for run_id in poll.watched:
            for event in await uow.chain_events.canonical_for_run(run_id):
                events[event.id] = event
        return list(events.values())

    async def _settled_status(
        self, uow: UnitOfWork, poll: _Poll, row: OutboxRecord
    ) -> TxStatus | None:
        if row.block_number is None:
            return None
        if row.block_number <= poll.finalized:
            return TxStatus.FINALIZED
        depth = poll.head - row.block_number + 1
        threshold = self._threshold(poll, await self._run(uow, poll, row.run_id))
        if row.status == TxStatus.INCLUDED and threshold is not None and depth >= threshold:
            return TxStatus.CONFIRMED
        return None

    async def _confirm_terminal(self, poll: _Poll) -> list[TerminalConfirmed]:
        """Every watched run whose terminal event is at its threshold, on every poll (ADR-058)."""
        due: list[tuple[RunRecord, ChainEventRecord, list[ChainEventRecord]]] = []
        async with self._transactions.unit_of_work() as uow:
            for run in poll.watched.values():
                threshold = self._threshold(poll, run)
                events = await uow.chain_events.canonical_for_run(run.id)
                terminal = next((e for e in events if e.event_name in TERMINAL_EVENT_NAMES), None)
                if threshold is None or terminal is None:
                    continue
                if poll.head - terminal.block_number + 1 < threshold:
                    continue
                if not any(event.event_name == "SessionOpened" for event in events):
                    poll.problem(
                        run.id,
                        "session_opening_missing",
                        "a terminal event is canonical but its session's SessionOpened is not",
                    )
                    continue
                due.append((run, terminal, events))
        return [await self._snapshot_terminal(run, event, events) for run, event, events in due]

    async def _snapshot_terminal(
        self, run: RunRecord, event: ChainEventRecord, events: Sequence[ChainEventRecord]
    ) -> TerminalConfirmed:
        opened = next(row for row in events if row.event_name == "SessionOpened")
        parties = {
            Party.BUYER: Address(str(opened.decoded["buyer"])),
            Party.SELLER: Address(str(opened.decoded["seller"])),
        }
        stages = [(SnapshotStage.TERMINAL, event.block_number)]
        settlement: SettlementCheck | None = None
        if event.event_name == "SettlementCompleted":
            stages = [
                (SnapshotStage.PRE_SETTLEMENT, event.block_number - 1),
                (SnapshotStage.POST_SETTLEMENT, event.block_number),
                *stages,
            ]
            settlement = await self._verify_settlement(event, events, opened, parties)
        async with self._transactions.unit_of_work() as uow:
            taken = {
                (snapshot.stage, snapshot.block_number)
                for snapshot in await uow.balances.canonical_for_run(run.id)
            }
        snapshots = [
            snapshot
            for stage, number in stages
            if (stage, number) not in taken
            for snapshot in await self._balances(run.id, stage, number, parties)
        ]
        if snapshots:
            async with self._transactions.unit_of_work() as uow:
                for snapshot in snapshots:
                    await uow.balances.add(snapshot)
        return TerminalConfirmed(run_id=run.id, event=event, settlement=settlement)

    async def snapshot(
        self, run_id: uuid.UUID, stage: SnapshotStage, number: int, parties: Mapping[Party, Address]
    ) -> list[BalanceSnapshotRecord]:
        """Each party's base, quote and ETH balance at a block, recorded once per stage and block.

        The terminal stages are this module's own; the controller takes `pre_setup` and
        `post_setup` through here (stage 2.4), so every snapshot is read the same way.
        """
        async with self._transactions.unit_of_work() as uow:
            existing = [
                snapshot
                for snapshot in await uow.balances.canonical_for_run(run_id)
                if snapshot.stage == stage and snapshot.block_number == number
            ]
        if existing:
            return existing
        snapshots = await self._balances(run_id, stage, number, parties)
        recorded = []
        async with self._transactions.unit_of_work() as uow:
            for new in snapshots:
                row = await uow.balances.add(new)
                if row is not None:
                    recorded.append(row)
        return recorded

    async def _balances(
        self, run_id: uuid.UUID, stage: SnapshotStage, number: int, parties: Mapping[Party, Address]
    ) -> list[NewBalanceSnapshot]:
        block = await self._chain.block(number)
        if block is None:
            return []
        snapshots = []
        for party, address in parties.items():
            for token, amount in (await self._holdings(address, number)).items():
                snapshots.append(
                    NewBalanceSnapshot(
                        run_id=run_id,
                        stage=stage,
                        party=party,
                        token=token,
                        amount_minor=MinorAmount(amount),
                        block_number=number,
                        block_hash=block.hash,
                    )
                )
        return snapshots

    async def _holdings(self, holder: Address, number: int) -> dict[TokenRole, int]:
        holdings = {TokenRole.ETH: await self._chain.eth_balance(holder, number)}
        for token, address in (
            (TokenRole.BASE, self._codec.base_token),
            (TokenRole.QUOTE, self._codec.quote_token),
        ):
            data = self._codec.encode_balance_of(holder)
            result = await self._chain.call(
                CallRequest(sender=holder, to=address, data=data), number
            )
            holdings[token] = self._codec.decode_uint(result)
        return holdings

    async def _verify_settlement(
        self,
        event: ChainEventRecord,
        events: Sequence[ChainEventRecord],
        opened: ChainEventRecord,
        parties: Mapping[Party, Address],
    ) -> SettlementCheck:
        receipt = await self._chain.receipt(event.tx_hash)
        if receipt is None:
            missing = ("the settlement receipt is no longer available",)
            return SettlementCheck(event.tx_hash, missing)
        offer_hash = str(event.decoded["offerHash"])
        signed = next(
            (
                int(row.decoded["quoteAmount"])
                for row in events
                if row.event_name == "OfferRecorded" and row.decoded["offerHash"] == offer_hash
            ),
            None,
        )
        return check_settlement(
            receipt,
            self._codec,
            buyer=parties[Party.BUYER],
            seller=parties[Party.SELLER],
            base_amount=int(opened.decoded["baseAmount"]),
            signed_quote_amount=signed,
        )
