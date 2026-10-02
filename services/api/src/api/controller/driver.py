"""Driving one active run: poll, record, act, wait — until there is nothing left to drive.

One `tick` is one iteration:

1. **Renew the lease** (ADR-019). A process that has lost it stops; the relay's nonces are taken
   from it.
2. **Poll** the indexer, and have the relay replace what is stuck (ADR-050) and send again what a
   node refused to accept (ADR-057). A failure to reach the RPC anywhere in the tick is an outage,
   not a result; one that outlasts `outage_limit_s` is `RECOVERY_REQUIRED` (ADR-063). The window is
   cleared only when a whole tick succeeds.
3. **Record** what the poll reported, as the run events only the controller writes (ADR-058): each
   transaction status — replacements and dropped rivals included — and each newly indexed timeline
   entry. Then act on the chain's state:
   - a `RunProblem` is `RECOVERY_REQUIRED` (ADR-059);
   - a `chain.reorg` not yet acted on pauses the run with cause `reorg`, and the relay reconciles
     before resume is permitted — a `nonce_conflict`, `unreachable` or `refused` reconciliation is
     `RECOVERY_REQUIRED` (ADR-057, ADR-058);
   - a terminal event confirmed at the threshold is the outcome, recorded with `terminal` — or
     `failed_setup` for a session ended before setup finished (ADR-067) — and for a settlement
     only with a passing settlement check (ADR-052). The report repeats every poll, so recording it
     is idempotent.
4. **Act**: a termination recorded on the run comes first (ADR-068); otherwise setup advances, or
   a turn does.

A run is driven while it is `preparing` or `running`; while `paused` only to finish what is in
flight — a turn's action, a requested step — and while `paused` or `recovery_required` whenever a
termination is recorded. A paused run with nothing in flight is not polled: resume reconciles and
polls again before anything continues, and polling only a run that needs it keeps a hosted RPC's
bill bounded (Q40).

An exception nothing here expects is caught at the top of `drive`, logged, and the run moved to
`RECOVERY_REQUIRED` with cause `internal_error` (docs/contributing.md section 2.1), so a run is
never left `running` with nothing driving it.

Every state change goes through `RunStates.move`, which checks architecture 6.1 under the run's row
lock and appends the `run.state` event in the same unit of work.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable, Collection
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Final

import structlog

from api.chain import ChainAdapter, ExchangeCodec, RpcUnavailableError
from api.config import ControllerPolicy, confirmation_threshold
from api.controller import events
from api.controller.errors import InvalidStateError
from api.controller.setup import SessionSetup, SetupStatus, SetupStep
from api.controller.states import (
    ACTIVE,
    FAILED_SETUP,
    PAUSED,
    PREPARING,
    RECOVERY_REQUIRED,
    RUNNING,
    TERMINAL,
    allowed,
    require_transition,
)
from api.db.enums import (
    TX_STATUSES_NOT_LIVE,
    OutcomeKind,
    RunState,
    SnapshotStage,
    TxKind,
    TxStatus,
)
from api.db.protocols import Transactions
from api.db.records import OutboxRecord, RunRecord
from api.indexer import Indexer, PollReport, TerminalConfirmed
from api.projection import (
    TERMINAL_EVENTS,
    TIMELINE_KIND,
    TimelineContext,
    build_timeline,
    derive_outcome,
)
from api.relay import Reconciliation, Relay
from api.turns import AgentSessions, TurnExecutor, TurnStatus, TurnStep, tx_status_event

_log = structlog.get_logger(component="controller")

#: ADR-057: a reconciliation that leaves the run to a person.
BAD_RECONCILIATIONS: Final = frozenset(
    {Reconciliation.NONCE_CONFLICT, Reconciliation.UNREACHABLE, Reconciliation.REFUSED}
)
TERMINATIONS: Final = frozenset({TxKind.ABORT_SESSION, TxKind.EXPIRE_SESSION})
#: `replaced` is not live either: its successor is, and when the successor reverts, nothing of the
#: termination is left in flight.
_NOT_LIVE: Final = frozenset(TX_STATUSES_NOT_LIVE)
_TERMINAL_STAGES: Final = frozenset(
    {SnapshotStage.PRE_SETTLEMENT, SnapshotStage.POST_SETTLEMENT, SnapshotStage.TERMINAL}
)
#: The abort code for a session an agent refused during setup (ADR-066).
EXECUTION_FAILURE: Final = 4


class Progress(StrEnum):
    #: Something happened; tick again at once.
    ADVANCED = "advanced"
    #: Waiting on the chain or an agent; tick again after the poll interval.
    WAITING = "waiting"
    #: Nothing left to drive.
    IDLE = "idle"


class Outage:
    """How long something has been failing, by wall clock from the first failure (ADR-063)."""

    def __init__(self, limit_s: float, clock: Callable[[], datetime]) -> None:
        self._limit = timedelta(seconds=limit_s)
        self._clock = clock
        self._since: datetime | None = None

    def failed(self) -> bool:
        """Record a failure; True once the outage has lasted the whole limit."""
        now = self._clock()
        if self._since is None:
            self._since = now
        return now - self._since >= self._limit

    def clear(self) -> None:
        self._since = None


class RunStates:
    """State changes, checked against architecture 6.1, each with its `run.state` event."""

    def __init__(self, transactions: Transactions) -> None:
        self._transactions = transactions

    async def move(
        self,
        run_id: uuid.UUID,
        target: RunState,
        cause: str | None,
        *,
        expect: Collection[RunState] | None = None,
        operation: str = "transition",
        force: bool = False,
    ) -> RunRecord:
        """`target` with `cause`, decided again under the run's row lock: `expect` names the
        states the caller decided from, and a run that has moved on since is refused rather than
        overwritten. The same state again is a change of cause, not a transition; `force` records
        it even when nothing changed, as acting on a reorg must."""
        async with self._transactions.unit_of_work() as uow:
            run = await uow.runs.get_for_update(run_id)
            if run is None:
                raise LookupError(f"no run {run_id}")
            if expect is not None and run.state not in expect:
                raise InvalidStateError(operation, run.state, expect)
            if run.state != target:
                require_transition(run.state, target)
            if run.state == target and run.state_cause == cause and not force:
                return run
            updated = await uow.runs.update_state(run_id, target, cause)
            await uow.run_events.append(run_id, "run.state", events.run_state(updated))
            return updated

    async def cause(self, run_id: uuid.UUID, cause: str) -> RunRecord:
        """A new cause for the state the run is in, whatever that is now."""
        async with self._transactions.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
        if run is None:
            raise LookupError(f"no run {run_id}")
        return await self.move(run_id, run.state, cause, expect=(run.state,))

    async def recovery(self, run_id: uuid.UUID, cause: str) -> None:
        """`RECOVERY_REQUIRED` from any active state; a run already there takes the new cause."""
        async with self._transactions.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
        if run is None or run.state not in ACTIVE:
            return
        _log.error("run.recovery_required", run_id=str(run_id), cause=cause)
        with contextlib.suppress(InvalidStateError):
            await self.move(run_id, RECOVERY_REQUIRED, cause, expect=ACTIVE)


class RunDriver:
    def __init__(
        self,
        transactions: Transactions,
        chain: ChainAdapter,
        codec: ExchangeCodec,
        relay: Relay,
        indexer: Indexer,
        turns: TurnExecutor,
        setup: SessionSetup,
        sessions: AgentSessions,
        *,
        explorer_base_url: str | None,
        holder: str,
        policy: ControllerPolicy,
        default_threshold: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._transactions = transactions
        self._chain = chain
        self._codec = codec
        self._relay = relay
        self._indexer = indexer
        self._turns = turns
        self._setup = setup
        self._sessions = sessions
        self._explorer = explorer_base_url
        self._holder = holder
        self._policy = policy
        self._default_threshold = default_threshold
        self._clock = clock
        self._sleep = sleep
        self.states = RunStates(transactions)
        self._rpc = Outage(policy.outage_limit_s, clock)
        self._agent = Outage(policy.outage_limit_s, clock)

    @property
    def turns(self) -> TurnExecutor:
        return self._turns

    @property
    def relay(self) -> Relay:
        return self._relay

    @property
    def setup(self) -> SessionSetup:
        return self._setup

    def reset_outages(self) -> None:
        """An operator's resume or a restart begins a fresh outage window (ADR-063)."""
        self._rpc.clear()
        self._agent.clear()

    # -----------------------------------------------------------------------------------------
    # The loop
    # -----------------------------------------------------------------------------------------

    async def drive(self, run_id: uuid.UUID) -> None:
        """Tick until there is nothing left to drive. The lease is renewed alongside, so a turn
        that waits on a model for longer than the lease lasts does not lose it."""
        heartbeat = asyncio.create_task(self._heartbeat(run_id))
        try:
            while (progress := await self._guarded_tick(run_id)) != Progress.IDLE:
                if progress == Progress.WAITING:
                    await self._sleep(self._policy.poll_interval_s)
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat

    async def _guarded_tick(self, run_id: uuid.UUID) -> Progress:
        try:
            return await self.tick(run_id)
        except Exception:
            _log.exception("run.driver_failed", run_id=str(run_id))
            try:
                await self.states.recovery(run_id, "internal_error")
            except Exception:
                _log.exception("run.driver_failed_to_record", run_id=str(run_id))
            return Progress.IDLE

    async def tick(self, run_id: uuid.UUID) -> Progress:
        run = await self._load(run_id)
        if not await self.driven(run) or not await self.hold(run_id):
            return Progress.IDLE
        if run.termination_cause is not None and await self._sessionless(run):
            await self._end_without_session(run)
            return Progress.IDLE
        try:
            await self.sync(run)
            if await self._resend_refused(run):
                return Progress.IDLE
            run = await self._load(run_id)
            if not await self.driven(run):
                return Progress.IDLE
            progress = await self._act(run)
        except RpcUnavailableError as error:
            _log.warning("rpc.unavailable", run_id=str(run_id), error=str(error))
            if self._rpc.failed():
                await self.states.recovery(run_id, "rpc_timeout")
                return Progress.IDLE
            return Progress.WAITING
        self._rpc.clear()
        return progress

    async def driven(self, run: RunRecord) -> bool:
        if run.outcome_kind != OutcomeKind.PENDING:
            return False
        if run.state in (PREPARING, RUNNING):
            return True
        if run.state not in (PAUSED, RECOVERY_REQUIRED):
            return False
        if run.termination_cause is not None:
            return True
        if run.state == RECOVERY_REQUIRED:
            return False
        return run.state_cause == "step" or await self._turns.open_turn(run.id) is not None

    async def hold(self, run_id: uuid.UUID) -> bool:
        """Take or renew the run's lease. False when another live holder has it."""
        now = self._clock()
        ttl = timedelta(seconds=self._policy.lease_ttl_s)
        async with self._transactions.unit_of_work() as uow:
            lease = await uow.leases.acquire(run_id, self._holder, ttl, now, relay_nonce_floor=0)
        if lease is None:
            _log.warning("lease.held_elsewhere", run_id=str(run_id), holder=self._holder)
        return lease is not None

    async def _heartbeat(self, run_id: uuid.UUID) -> None:
        while True:
            await asyncio.sleep(self._policy.lease_ttl_s / 3)
            run = await self._load(run_id)
            if run.state not in ACTIVE:
                return
            await self.hold(run_id)

    # -----------------------------------------------------------------------------------------
    # Poll and record
    # -----------------------------------------------------------------------------------------

    async def sync(self, run: RunRecord) -> None:
        report = await self._indexer.poll()
        successors = await self._relay.replace_stuck(run.id)
        await self._record_report(run, report, successors)
        problem = next((p for p in report.problems if p.run_id == run.id), None)
        if problem is not None:
            await self.states.recovery(run.id, problem.code)
            return
        await self._handle_reorg(await self._load(run.id))
        for terminal in report.terminal:
            if terminal.run_id == run.id:
                await self._record_terminal(await self._load(run.id), terminal)

    async def _record_report(
        self, run: RunRecord, report: PollReport, successors: Collection[OutboxRecord]
    ) -> None:
        reset = report.reorg.reset_transactions if report.reorg is not None else ()
        async with self._transactions.unit_of_work() as uow:
            replaced = [
                original
                for successor in successors
                if successor.replaces_id is not None
                and (original := await uow.outbox.get(successor.replaces_id)) is not None
            ]
        changed: dict[tuple[uuid.UUID, TxStatus], OutboxRecord] = {}
        for row in (
            *replaced,
            *successors,
            *report.dropped,
            *report.included,
            *report.reverted,
            *report.confirmed,
            *report.finalized,
            *reset,
        ):
            if row.run_id == run.id:
                changed.setdefault((row.id, row.status), row)
        indexed = [
            event
            for event in report.indexed
            if event.run_id == run.id and event.event_name in TIMELINE_KIND
        ]
        reverted = {str(row.tx_hash) for row in report.reverted if row.run_id == run.id}
        if not changed and not indexed and not reverted:
            return
        async with self._transactions.unit_of_work() as uow:
            actions = await uow.signed_actions.list_for_run(run.id)
            digests = {action.id: action.digest for action in actions}
            for row in changed.values():
                digest = None if row.signed_action_id is None else digests.get(row.signed_action_id)
                data = tx_status_event(
                    row, digest, head_block=report.head_block, explorer_base_url=self._explorer
                )
                await uow.run_events.append(run.id, "tx.status", data)
            wanted = {(str(e.tx_hash), TIMELINE_KIND[e.event_name]) for e in indexed}
            wanted |= {(tx_hash, "execution_failure") for tx_hash in reverted}
            timeline = build_timeline(
                await uow.chain_events.canonical_for_run(run.id),
                await uow.outbox.list_for_run(run.id),
                actions,
                TimelineContext(self._threshold(run), self._explorer),
            )
            for entry in timeline:
                if (entry["tx"]["tx_hash"], entry["kind"]) in wanted:
                    await uow.run_events.append(run.id, "chain.event", entry)

    async def _handle_reorg(self, run: RunRecord) -> None:
        """A `chain.reorg` the controller has not acted on: one with no `run.state` event of cause
        `reorg` after it. Acting appends that event even when the run is already paused for an
        earlier reorg, so each reorg is acted on once, durably."""
        if run.state not in (PREPARING, RUNNING, PAUSED):
            return
        async with self._transactions.unit_of_work() as uow:
            reorg = await uow.run_events.latest(run.id, "chain.reorg")
            later = [] if reorg is None else await uow.run_events.after(run.id, reorg.cursor)
        if reorg is None or any(
            event.event_type == "run.state" and event.data.get("state_cause") == "reorg"
            for event in later
        ):
            return
        _log.warning("run.reorg", run_id=str(run.id), from_block=reorg.data.get("from_block"))
        target = PREPARING if run.state == PREPARING else PAUSED
        await self.states.move(run.id, target, "reorg", expect=(run.state,), force=True)
        await self.reconcile(run.id)

    async def reconcile(self, run_id: uuid.UUID) -> bool:
        """The relay's recovery for every unfinished transaction. False, with the run moved to
        `RECOVERY_REQUIRED`, when any outcome needs a person (ADR-057)."""
        for result in await self._relay.reconcile(run_id):
            if result.outcome in BAD_RECONCILIATIONS:
                await self.states.recovery(run_id, result.outcome.value)
                return False
        return True

    async def _resend_refused(self, run: RunRecord) -> bool:
        """A transaction persisted but never accepted by a node — refused, or unanswered — is
        reconciled: looked up, then sent again. A refusal is a person's (ADR-057); no answer is an
        outage, counted in the RPC window. True when the run went to `RECOVERY_REQUIRED`."""
        async with self._transactions.unit_of_work() as uow:
            unsent = [
                row
                for row in await uow.outbox.unfinished(run.id)
                if row.status == TxStatus.PENDING and row.attempts > 0
            ]
        if not unsent:
            return False
        for result in await self._relay.reconcile(run.id):
            if result.outcome == Reconciliation.UNREACHABLE:
                raise RpcUnavailableError("a pending transaction could not be sent again")
            if result.outcome in BAD_RECONCILIATIONS:
                await self.states.recovery(run.id, result.outcome.value)
                return True
        return False

    async def _record_terminal(self, run: RunRecord, terminal: TerminalConfirmed) -> None:
        if run.outcome_kind != OutcomeKind.PENDING:
            return
        async with self._transactions.unit_of_work() as uow:
            outcome = derive_outcome(
                await uow.chain_events.canonical_for_run(run.id), self._threshold(run)
            )
        set_up = await self._set_up(run.id)
        if outcome is None:
            return
        if outcome.kind == OutcomeKind.SETTLED and (
            terminal.settlement is None or not terminal.settlement.ok
        ):
            await self.states.recovery(run.id, "settlement_check_failed")
            return
        # A session ended before setup finished leaves the run's setup failed (ADR-067).
        target = TERMINAL if set_up and run.state != PREPARING else FAILED_SETUP
        if not allowed(run.state, target):
            _log.error("run.terminal_unrecordable", run_id=str(run.id), state=run.state.value)
            return
        await self._turns.close(run)
        async with self._transactions.unit_of_work() as uow:
            current = await uow.runs.get_for_update(run.id)
            if current is None or current.outcome_kind != OutcomeKind.PENDING:
                return
            require_transition(current.state, target)
            # The final cause is the termination's where there is one (ADR-068): a fault that
            # crossed it says nothing about why the session ended.
            cause = current.termination_cause or current.state_cause
            updated = await uow.runs.record_outcome(run.id, outcome, self._clock(), target, cause)
            await uow.run_events.append(run.id, "run.state", events.run_state(updated))
            snapshots = [
                snapshot
                for snapshot in await uow.balances.canonical_for_run(run.id)
                if snapshot.stage in _TERMINAL_STAGES
            ]
            for data in events.balances(snapshots):
                await uow.run_events.append(run.id, "balances", data)
        await self.finish(run.id)
        _log.info("run.outcome", run_id=str(run.id), outcome=outcome.kind.value)

    async def finish(self, run_id: uuid.UUID) -> None:
        """The run will never touch the chain again: release the active run, the lease and both
        agents' hold on it."""
        async with self._transactions.unit_of_work() as uow:
            await uow.leases.release_active_run(run_id)
            await uow.leases.release(run_id, self._holder)
        await self._sessions.release(run_id)

    # -----------------------------------------------------------------------------------------
    # Act
    # -----------------------------------------------------------------------------------------

    async def _act(self, run: RunRecord) -> Progress:
        if run.termination_cause is not None:
            return await self._terminating(run)
        if run.state == PREPARING:
            return await self._on_setup(run, await self._setup.advance(run))
        return await self._on_turn(run, await self._turns.advance(run))

    async def _on_setup(self, run: RunRecord, step: SetupStep) -> Progress:
        if step.status != SetupStatus.AGENT_UNAVAILABLE:
            self._agent.clear()
        if step.status == SetupStatus.PROGRESSED:
            return Progress.ADVANCED
        if step.status == SetupStatus.WAITING:
            return Progress.WAITING
        if step.status == SetupStatus.DONE:
            await self._setup_done(run)
            return Progress.ADVANCED
        if step.status == SetupStatus.FAILED:
            await self.states.move(run.id, FAILED_SETUP, step.cause, expect=(PREPARING,))
            await self.finish(run.id)
            return Progress.IDLE
        if step.status == SetupStatus.SESSION_REFUSED:
            await self.request_termination(run.id, "session_refused", EXECUTION_FAILURE)
            return Progress.ADVANCED
        return await self._fault(run, step.status == SetupStatus.AGENT_UNAVAILABLE, step.cause)

    async def _setup_done(self, run: RunRecord) -> None:
        """`start` runs on; a step — or setup carried on after a reorg or a recovery — pauses, and
        a step then takes its one turn."""
        if run.state_cause == "start":
            await self.states.move(run.id, RUNNING, None, expect=(PREPARING,))
        else:
            cause = "step" if run.state_cause == "step" else "session_open"
            await self.states.move(run.id, PAUSED, cause, expect=(PREPARING,))

    async def _on_turn(self, run: RunRecord, step: TurnStep) -> Progress:
        if step.status != TurnStatus.AGENT_UNAVAILABLE:
            self._agent.clear()
        if step.status in (TurnStatus.IN_FLIGHT, TurnStatus.NOT_READY, TurnStatus.RETRY):
            return Progress.WAITING
        if step.status == TurnStatus.CONFIRMED:
            if run.state == PAUSED and run.state_cause == "step":
                with contextlib.suppress(InvalidStateError):
                    await self.states.move(run.id, PAUSED, "step_complete", expect=(PAUSED,))
            return Progress.ADVANCED
        if step.status == TurnStatus.DEADLINE_PASSED:
            await self.request_termination(run.id, "session_deadline", None)
            return Progress.ADVANCED
        if step.status in _ENDINGS:
            # The executor recorded the termination in the turn's own unit of work (ADR-068).
            return Progress.ADVANCED
        return await self._fault(run, step.status == TurnStatus.AGENT_UNAVAILABLE, step.cause)

    async def _fault(self, run: RunRecord, unavailable: bool, cause: str | None) -> Progress:
        """An agent that does not answer is waited for within the outage limit; anything else
        reaching here is a defect (ADR-064)."""
        if unavailable and not self._agent.failed():
            return Progress.WAITING
        await self.states.recovery(run.id, cause or "agent_unavailable")
        return Progress.IDLE

    # -----------------------------------------------------------------------------------------
    # Ending a session early (ADR-067, ADR-068)
    # -----------------------------------------------------------------------------------------

    async def request_termination(self, run_id: uuid.UUID, cause: str, code: int | None) -> None:
        """Record that the run's session must end: durable before anything is sent, and the first
        one recorded is kept."""
        async with self._transactions.unit_of_work() as uow:
            await uow.runs.request_termination(run_id, cause, code)
        _log.info("run.termination_requested", run_id=str(run_id), cause=cause)

    async def _terminating(self, run: RunRecord) -> Progress:
        """Send the recorded termination and see it through: `abortSession` from the operator
        before the deadline, `expireSession` from the relay at or after it (api_contract 2.2,
        spec 9.4). One in flight is waited for; one that reverted is followed by expiry once the
        deadline has passed, and otherwise is a person's to look at."""
        rows = await self._termination_rows(run.id)
        if any(row.status not in _NOT_LIVE for row in rows):
            return Progress.WAITING
        async with self._transactions.unit_of_work() as uow:
            names = {event.event_name for event in await uow.chain_events.canonical_for_run(run.id)}
        if names & set(TERMINAL_EVENTS):
            return Progress.WAITING  # the session has ended; the poll records how
        if "SessionOpened" not in names:
            return Progress.WAITING  # its createSession is in flight: abort it once it opens
        head = await self._chain.head()
        expired = (
            run.session_expires_at_ts is not None and head.timestamp >= run.session_expires_at_ts
        )
        if rows and not (expired and not any(r.kind == TxKind.EXPIRE_SESSION for r in rows)):
            await self.states.recovery(run.id, "termination_reverted")
            return Progress.IDLE
        if expired:
            await self._send(run, TxKind.EXPIRE_SESSION, None)
        elif run.termination_code is not None:
            await self._send(run, TxKind.ABORT_SESSION, run.termination_code)
        else:
            return Progress.WAITING  # an expiry the chain has not reached yet
        return Progress.ADVANCED

    async def _send(self, run: RunRecord, kind: TxKind, code: int | None) -> None:
        session_id = str(run.session_id)
        if kind == TxKind.EXPIRE_SESSION:
            data, as_operator = self._codec.encode_expire_session(session_id), False
        else:
            data, as_operator = self._codec.encode_abort_session(session_id, code or 1), True
        row = await self._relay.submit_call(
            run.id, kind, self._codec.exchange, data, as_operator=as_operator
        )
        async with self._transactions.unit_of_work() as uow:
            await uow.run_events.append(
                run.id, "tx.status", tx_status_event(row, None, explorer_base_url=self._explorer)
            )
        _log.info("run.terminating", run_id=str(run.id), kind=kind.value)

    async def _sessionless(self, run: RunRecord) -> bool:
        """No session exists or can come to exist: no session id was recorded, or no
        `createSession` for it is live and none opened. Decided from the database alone, so an
        abort ends such a run even while the RPC is down (ADR-067)."""
        if run.session_id is None:
            return True
        async with self._transactions.unit_of_work() as uow:
            creating = any(
                row.kind == TxKind.CREATE_SESSION and row.status not in _NOT_LIVE
                for row in await uow.outbox.list_for_run(run.id)
            )
            opened = any(
                event.event_name == "SessionOpened"
                for event in await uow.chain_events.canonical_for_run(run.id)
            )
        return not creating and not opened

    async def _end_without_session(self, run: RunRecord) -> None:
        """ADR-067: nothing on chain to end, so the run's setup has failed, with the termination's
        cause, and it lets go of the active run."""
        with contextlib.suppress(InvalidStateError):
            await self.states.move(
                run.id,
                FAILED_SETUP,
                run.termination_cause,
                expect=(PREPARING, RECOVERY_REQUIRED),
            )
            await self.finish(run.id)

    async def _termination_rows(self, run_id: uuid.UUID) -> list[OutboxRecord]:
        async with self._transactions.unit_of_work() as uow:
            return [
                row for row in await uow.outbox.list_for_run(run_id) if row.kind in TERMINATIONS
            ]

    # -----------------------------------------------------------------------------------------

    async def _load(self, run_id: uuid.UUID) -> RunRecord:
        async with self._transactions.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
        if run is None:
            raise LookupError(f"no run {run_id}")
        return run

    async def _set_up(self, run_id: uuid.UUID) -> bool:
        async with self._transactions.unit_of_work() as uow:
            snapshots = await uow.balances.canonical_for_run(run_id)
        return self._setup.complete(snapshots)

    def _threshold(self, run: RunRecord) -> int:
        return confirmation_threshold(run.public_config, self._default_threshold)


_ENDINGS: Final = frozenset(
    {
        TurnStatus.MODEL_FAILED,
        TurnStatus.BUDGET_EXHAUSTED,
        TurnStatus.EXECUTION_FAILED,
        TurnStatus.ABANDONED,
    }
)
