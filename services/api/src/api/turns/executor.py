"""One turn, spec section 9.2, as small steps the controller advances (docs/architecture.md 5.2).

`TurnExecutor.advance` moves the run's current turn as far as it can without waiting, and says
where it stopped. The controller polls the indexer between calls; nothing here waits on the chain.

The steps, and where each leaves its record:

1. **Observe.** The allowlisted observation for whoever acts next (protocol 5, rule 3), built from
   confirmed canonical events, is stored on a new `turns` row with its hash before it is sent.
2. **Decide.** The agent decides, validates and signs in one call (ADR-012). An unreachable agent is
   asked again with the same turn and the same stored observation — the agent answers an identical
   request with its first response — and never for a fresh decision (FR-E2).
3. **Persist before broadcast.** The decision records, the signed action and the turn's state are
   written in one unit of work, and only then is the action handed to the relay (step 5 of 9.2).
4. **Broadcast** through the relay, which persists the transaction before it sends it and resumes
   rather than re-signs on a second call.
5. **Confirm.** The turn is `confirmed` when the indexer has confirmed its action at the run's
   threshold, and `execution_failed` when its transaction reverted (architecture 6.2).

The agent's signed action is checked before it is persisted — its signer is the party's wallet, its
sequence is the one the observation expected, its session is the run's, and its digest recomputes
from its own fields and recovers to its signer — because a signature the contract would refuse is
an execution failure and an abort, and a mismatch here is a defect to stop on instead.

What comes back from an agent refusal follows ADR-046, ADR-048 and ADR-064: an inconsistent
observation is rebuilt and retried, up to `observation_retries` refusals in a row; a restarted agent
is restored and asked again; a deadline is the controller's to record as expiry; anything else is a
defect, and `RECOVERY_REQUIRED`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any, Final, Protocol

import structlog

from api.agent_client import (
    AgentClient,
    AgentRefusedError,
    AgentUnavailableError,
    DecisionPayload,
    SignedActionPayload,
    TurnResponse,
)
from api.chain import ChainAdapter
from api.db.enums import ActionKind, ActionStatus, Party, PolicyKind, TurnState
from api.db.protocols import Transactions, UnitOfWork
from api.db.records import (
    ChainEventRecord,
    DeploymentRecord,
    NewDecision,
    NewSignedAction,
    NewTurn,
    OutboxRecord,
    RunRecord,
    SignedActionRecord,
    TurnRecord,
)
from api.observation import ObservationBuilder, ObservationError
from api.turns.agents import AgentSessions
from negotiation_protocol import (
    Accept,
    Address,
    Close,
    Digest,
    Domain,
    Offer,
    ProtocolValueError,
    close_reason_name,
    recover_signer,
    signature_is_canonical,
)

_log = structlog.get_logger(component="turns")

INCONSISTENT: Final = "observation_inconsistent"
#: Slack beyond the model's own time limit before a turn's answer is overdue.
TURN_SLACK_S: Final = 15

_FINISHED: Final = frozenset(
    {TurnState.CONFIRMED, TurnState.MODEL_FAILED, TurnState.EXECUTION_FAILED}
)
_CONFIRMED: Final = frozenset({ActionStatus.CONFIRMED, ActionStatus.FINALIZED})
#: An agent that has lost the run, or holds it provisioned but not approved — a restore that
#: stopped half-way (ADR-048): either is restored, then asked once more.
_FORGOTTEN: Final = frozenset({"unprovisioned", "provisioned"})
#: Abort reason codes (protocol 10) for the endings the executor records (ADR-068).
MODEL_FAILURE: Final = 2
BUDGET_EXHAUSTED: Final = 3
EXECUTION_FAILURE: Final = 4


class TurnStatus(StrEnum):
    #: An action is signed and with the relay; its confirmation is awaited.
    IN_FLIGHT = "in_flight"
    #: The turn's action is confirmed at the threshold.
    CONFIRMED = "confirmed"
    #: The chain has not settled what came before; poll and try again.
    NOT_READY = "not_ready"
    #: The agent refused the observation as inconsistent; poll, rebuild and try again (ADR-046).
    RETRY = "retry"
    #: The agent did not answer; the same turn is asked again (ADR-064).
    AGENT_UNAVAILABLE = "agent_unavailable"
    #: Chain time has reached the session's deadline: expiry, not a turn.
    DEADLINE_PASSED = "deadline_passed"
    #: Every attempt was refused, or the model failed (abort reason 2).
    MODEL_FAILED = "model_failed"
    #: The agent's spend or call ceiling stopped it (abort reason 3).
    BUDGET_EXHAUSTED = "budget_exhausted"
    #: The action's transaction reverted (abort reason 4, spec 9.4).
    EXECUTION_FAILED = "execution_failed"
    #: A termination was recorded while the agent decided: the decision is kept as a private
    #: record and nothing is signed into the run (ADR-070).
    ABANDONED = "abandoned"
    #: A defect or a fault a person must look at; `cause` says which.
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True, slots=True)
class TurnStep:
    status: TurnStatus
    cause: str | None = None
    turn: int | None = None


class ActionRelay(Protocol):
    async def submit_action(
        self, run_id: uuid.UUID, signed_action_id: uuid.UUID
    ) -> OutboxRecord: ...


class DecisionSentences(Protocol):
    """The timeline renderer (`api.projection.TimelineSentences`), so a decision reads as the
    event it will become (ADR-024)."""

    def event_sentence(
        self, name: str, args: Mapping[str, Any], earlier: Sequence[tuple[str, Mapping[str, Any]]]
    ) -> str | None: ...


class TurnExecutor:
    def __init__(
        self,
        transactions: Transactions,
        builder: ObservationBuilder,
        agents: Mapping[Party, AgentClient],
        sessions: AgentSessions,
        relay: ActionRelay,
        chain: ChainAdapter,
        deployment: DeploymentRecord,
        sentences: DecisionSentences,
        *,
        observation_retries: int = 5,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._transactions = transactions
        self._builder = builder
        self._agents = agents
        self._sessions = sessions
        self._relay = relay
        self._chain = chain
        self._domain = Domain(deployment.chain_id, deployment.exchange_address)
        self._explorer = deployment.explorer_base_url
        self._sentences = sentences
        self._retries = observation_retries
        self._clock = clock

    async def open_turn(self, run_id: uuid.UUID) -> TurnRecord | None:
        """The run's latest turn while it is unfinished, else None."""
        async with self._transactions.unit_of_work() as uow:
            turns = await uow.turns.list_for_run(run_id)
        latest = max(turns, key=lambda turn: turn.turn, default=None)
        return None if latest is None or _finished(latest) else latest

    async def close(self, run: RunRecord) -> None:
        """The session has ended: give the open turn, if any, its final state. An action confirmed
        or reverted ends as such; anything else — an action the end overtook, or a decision never
        signed — ends with `session_ended`."""
        current = await self.open_turn(run.id)
        if current is None:
            return
        async with self._transactions.unit_of_work() as uow:
            action = _action_for(await uow.signed_actions.list_for_run(run.id), current)
        if action is not None and action.status in _CONFIRMED:
            await self._finish(current, TurnState.CONFIRMED, None, None)
        elif action is not None and action.status == ActionStatus.REVERTED:
            await self._finish(current, TurnState.EXECUTION_FAILED, "reverted", action.revert_error)
        else:
            await self._finish(current, TurnState(current.state), "session_ended", None)

    async def advance(self, run: RunRecord) -> TurnStep:
        current = await self.open_turn(run.id)
        if current is not None:
            async with self._transactions.unit_of_work() as uow:
                action = _action_for(await uow.signed_actions.list_for_run(run.id), current)
            if action is not None:
                return await self._follow(run, current, action)
            if await self._stale(current):
                await self._finish(current, TurnState(current.state), "observation_stale", None)
                return TurnStep(TurnStatus.RETRY, "observation_stale", current.turn)
            return await self._ask(run, current)
        return await self._begin(run)

    async def _stale(self, turn: TurnRecord) -> bool:
        """ADR-071: a stored observation is asked again only while chain time has not passed its
        active offer's `valid_until` or its session's `expires_at`."""
        now = (await self._chain.head()).timestamp
        observation = turn.observation
        offer = observation.get("active_offer")
        if offer is not None and now >= int(offer["valid_until"]):
            return True
        return now >= int(observation["session"]["expires_at"])

    # -----------------------------------------------------------------------------------------
    # 1. Observe
    # -----------------------------------------------------------------------------------------

    async def _begin(self, run: RunRecord) -> TurnStep:
        head = await self._chain.head()
        if run.session_expires_at_ts is not None and head.timestamp >= run.session_expires_at_ts:
            return TurnStep(TurnStatus.DEADLINE_PASSED)
        try:
            built = await self._builder.build(run.id, head.timestamp)
        except ObservationError as error:
            if error.code in ("not_confirmed", "session_ended"):
                return TurnStep(TurnStatus.NOT_READY, error.code)
            return TurnStep(TurnStatus.RECOVERY_REQUIRED, f"observation_{error.code}")
        async with self._transactions.unit_of_work() as uow:
            number = 1 + max(
                (turn.turn for turn in await uow.turns.list_for_run(run.id)), default=0
            )
            turn = await uow.turns.add(
                NewTurn(
                    run_id=run.id,
                    turn=number,
                    party=built.party,
                    expected_sequence=built.expected_sequence,
                    state=TurnState.OBSERVING,
                    observation=built.document,
                    observation_hash=built.observation_hash,
                )
            )
            await uow.run_events.append(
                run.id,
                "turn.started",
                {
                    "turn": number,
                    "party": built.party.value,
                    "expected_sequence": built.expected_sequence,
                },
            )
        return await self._ask(run, turn)

    # -----------------------------------------------------------------------------------------
    # 2. Decide
    # -----------------------------------------------------------------------------------------

    async def _ask(self, run: RunRecord, turn: TurnRecord, *, restored: bool = False) -> TurnStep:
        agent = self._agents[turn.party]
        if turn.state == TurnState.OBSERVING:
            async with self._transactions.unit_of_work() as uow:
                turn = await uow.turns.update_state(turn.id, TurnState.DECIDING)
        try:
            response = await agent.turn(
                run.id, self._request(run, turn), timeout_s=_turn_timeout(run)
            )
        except AgentUnavailableError:
            return TurnStep(TurnStatus.AGENT_UNAVAILABLE, "agent_unavailable", turn.turn)
        except AgentRefusedError as refusal:
            if refusal.state in _FORGOTTEN and not restored:
                return await self._restore_and_ask(run, turn)
            return await self._refused(run, turn, refusal)
        return await self._record(run, turn, response)

    def _request(self, run: RunRecord, turn: TurnRecord) -> dict[str, Any]:
        body = {key: value for key, value in turn.observation.items() if key != "mandate"}
        deadline = self._clock() + timedelta(seconds=_turn_timeout(run))
        body["turn"] = turn.turn
        body["deadline_at"] = (
            deadline.astimezone(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        )
        return body

    async def _restore_and_ask(self, run: RunRecord, turn: TurnRecord) -> TurnStep:
        """ADR-048: the agent restarted and forgot the run, or a restore stopped between its
        provisioning and its approval. Restore it, then ask once more."""
        try:
            await self._sessions.restore(run.id, turn.party)
        except AgentUnavailableError:
            return TurnStep(TurnStatus.AGENT_UNAVAILABLE, "agent_unavailable", turn.turn)
        except AgentRefusedError as refusal:
            return await self._finish_refused(run, turn, f"restore_{refusal.code}")
        return await self._ask(run, turn, restored=True)

    async def _refused(
        self, run: RunRecord, turn: TurnRecord, refusal: AgentRefusedError
    ) -> TurnStep:
        if refusal.inconsistent:
            await self._finish(turn, TurnState(turn.state), INCONSISTENT, _fields(refusal))
            if await self._inconsistent_in_a_row(run.id) >= self._retries:
                return TurnStep(TurnStatus.RECOVERY_REQUIRED, INCONSISTENT, turn.turn)
            return TurnStep(TurnStatus.RETRY, INCONSISTENT, turn.turn)
        if refusal.deadline_passed:
            await self._finish(turn, TurnState(turn.state), "session_deadline_passed", None)
            return TurnStep(TurnStatus.DEADLINE_PASSED, None, turn.turn)
        cause = refusal.state if refusal.code == "invalid_state" and refusal.state else refusal.code
        return await self._finish_refused(run, turn, cause)

    async def _finish_refused(self, run: RunRecord, turn: TurnRecord, cause: str) -> TurnStep:
        _log.error("turn.agent_refused", run_id=str(run.id), turn=turn.turn, cause=cause)
        await self._finish(turn, TurnState(turn.state), cause, None)
        return TurnStep(TurnStatus.RECOVERY_REQUIRED, f"agent_{cause}", turn.turn)

    async def _inconsistent_in_a_row(self, run_id: uuid.UUID) -> int:
        async with self._transactions.unit_of_work() as uow:
            turns = sorted(await uow.turns.list_for_run(run_id), key=lambda t: t.turn)
        count = 0
        for turn in reversed(turns):
            if turn.failure_code != INCONSISTENT:
                break
            count += 1
        return count

    # -----------------------------------------------------------------------------------------
    # 3. Persist before broadcast
    # -----------------------------------------------------------------------------------------

    async def _record(self, run: RunRecord, turn: TurnRecord, response: TurnResponse) -> TurnStep:
        if response.turn != turn.turn:
            return await self._finish_refused(run, turn, "wrong_turn")
        if any(item.observation_hash != turn.observation_hash for item in response.decisions):
            # The agent decided on another observation — another mandate, most likely, since the
            # hash covers the one it injected (api_contract section 6). Nothing of it is kept.
            return await self._finish_refused(run, turn, "observation_hash_mismatch")
        if response.status == "signed":
            return await self._record_signed(run, turn, response)
        failure = response.failure
        budget = response.status == "budget_exhausted"
        cause, code = (
            ("budget_exhausted", BUDGET_EXHAUSTED) if budget else ("model_failure", MODEL_FAILURE)
        )
        async with self._transactions.unit_of_work() as uow:
            await self._add_decisions(uow, run, turn, response.decisions, authorized=None)
            await uow.turns.update_state(
                turn.id,
                TurnState.MODEL_FAILED,
                finished_at=self._clock(),
                failure_code=None if failure is None else failure.code,
                failure_detail=None if failure is None else failure.detail,
            )
            await self._decision_events(uow, run, turn, response, None, None)
            # In the turn's own unit of work: the abort this failure owes outlives any crash or
            # outage before it is sent (ADR-068).
            await uow.runs.request_termination(run.id, cause, code)
        status = TurnStatus.BUDGET_EXHAUSTED if budget else TurnStatus.MODEL_FAILED
        return TurnStep(status, cause, turn.turn)

    async def _record_signed(
        self, run: RunRecord, turn: TurnRecord, response: TurnResponse
    ) -> TurnStep:
        payload = response.signed_action
        authorised = _authorised_attempt(response.decisions)
        async with self._transactions.unit_of_work() as uow:
            wallet = await uow.wallets.get(run.id, turn.party)
            opened = _opened(await uow.chain_events.canonical_for_run(run.id))
        problem = (
            "no signed action"
            if payload is None or authorised is None
            else self._check(payload, turn, wallet.address if wallet else None, opened)
        )
        if problem is not None or payload is None or authorised is None:
            _log.error("turn.signed_action_refused", run_id=str(run.id), problem=problem)
            return await self._finish_refused(run, turn, "signed_action_mismatch")

        async with self._transactions.unit_of_work() as uow:
            # Under the run's row lock, which an abort takes to record itself: a termination
            # committed first always wins, and nothing of this decision is signed into the run
            # beyond its private record (ADR-070).
            current = await uow.runs.get_for_update(run.id)
            if current is not None and current.termination_cause is not None:
                await self._add_decisions(uow, run, turn, response.decisions, authorized=None)
                await uow.turns.update_state(
                    turn.id,
                    TurnState(turn.state),
                    finished_at=self._clock(),
                    failure_code="termination_requested",
                )
                return TurnStep(TurnStatus.ABANDONED, "termination_requested", turn.turn)
            decisions = await self._add_decisions(
                uow, run, turn, response.decisions, authorized=authorised.attempt
            )
            action = await uow.signed_actions.add(
                NewSignedAction(
                    run_id=run.id,
                    turn_id=turn.id,
                    decision_id=decisions[authorised.attempt],
                    sequence=turn.expected_sequence,
                    kind=ActionKind(payload.kind),
                    typed_message=dict(payload.typed_message),
                    digest=Digest(payload.digest),
                    signer=Address(payload.signer),
                    signature=payload.signature,
                )
            )
            await uow.turns.update_state(turn.id, TurnState.BROADCASTING)
            await self._decision_events(uow, run, turn, response, payload, authorised.attempt)
            await uow.run_events.append(
                run.id,
                "turn.signed",
                {
                    "turn": turn.turn,
                    "party": turn.party.value,
                    "kind": payload.kind,
                    "digest": str(action.digest),
                },
            )
        return await self._submit(run, turn, action)

    def _check(
        self,
        payload: SignedActionPayload,
        turn: TurnRecord,
        address: Address | None,
        opened: ChainEventRecord | None,
    ) -> str | None:
        """Why this signed action is not the one the turn asked for, or None if it is.

        Everything the contract would check that the backend can check first: a signature the
        contract refuses is an execution failure and an abort with reason 4, which would record an
        agent's defect on chain as the run's outcome (ADR-064)."""
        message = payload.typed_message
        if address is None or opened is None:
            return "the run has no wallet or no opened session for this party"
        try:
            checks = {
                "fields": set(message) == _MESSAGE_FIELDS[payload.kind],
                "signer": Address(payload.signer) == address,
                "message signer": Address(str(message[_SIGNER_FIELD[payload.kind]])) == address,
                "sequence": message["sequence"] == turn.expected_sequence,
                "session": message["sessionId"] == opened.decoded["sessionId"]
                and message["configHash"] == opened.decoded["configHash"],
                "terms": _terms_hold(payload.kind, message, turn.observation),
            }
            digest = _digest(payload.kind, message, self._domain)
            checks["digest"] = Digest(digest) == Digest(payload.digest)
            checks["signature"] = (
                signature_is_canonical(payload.signature)
                and Address(recover_signer(digest, payload.signature)) == address
            )
        except (KeyError, TypeError, ValueError, ProtocolValueError) as error:
            return f"unreadable: {type(error).__name__}"
        failed = sorted(name for name, ok in checks.items() if not ok)
        return None if not failed else "mismatched " + ", ".join(failed)

    async def _add_decisions(
        self,
        uow: UnitOfWork,
        run: RunRecord,
        turn: TurnRecord,
        decisions: Sequence[DecisionPayload],
        *,
        authorized: int | None,
    ) -> dict[int, uuid.UUID]:
        policy, model_id, effort = _party_policy(run, turn.party)
        ids = {}
        for item in decisions:
            record = await uow.decisions.add(
                NewDecision(
                    turn_id=turn.id,
                    run_id=run.id,
                    party=turn.party,
                    attempt=item.attempt,
                    policy=policy,
                    model_id=model_id,
                    effort=effort,
                    prompt_template_version=item.prompt_template_version,
                    raw_response=item.raw_response,
                    stop_reason=item.stop_reason,
                    validation_ok=item.validation.ok,
                    validation_code=item.validation.code,
                    validation_feedback=item.validation.feedback,
                    usage=item.usage,
                    cost_estimated_usd=_usd(item.cost_estimated_usd),
                    cost_reported_usd=_usd(item.cost_reported_usd),
                    latency_ms=item.latency_ms,
                    requested_at=item.requested_at,
                    authorized=item.attempt == authorized,
                )
            )
            ids[item.attempt] = record.id
        return ids

    async def _decision_events(
        self,
        uow: UnitOfWork,
        run: RunRecord,
        turn: TurnRecord,
        response: TurnResponse,
        payload: SignedActionPayload | None,
        authorised: int | None,
    ) -> None:
        """`turn.decision` per attempt: public fields only (api_contract section 3). The signed
        attempt's action is built from the signed message itself — what will be on chain — never
        from the agent's raw response; every other attempt's action is a private record that was
        never an offer (FR-U8), so it is null."""
        sentence = await self._sentence(uow, run, payload)
        last = response.decisions[-1].attempt if response.decisions else None
        for item in response.decisions:
            status = "valid" if item.validation.ok else "invalid"
            if response.status != "signed" and item.attempt == last:
                status = "model_failed"
            signed = payload is not None and item.attempt == authorised
            await uow.run_events.append(
                run.id,
                "turn.decision",
                {
                    "turn": turn.turn,
                    "party": turn.party.value,
                    "attempt": item.attempt,
                    "status": status,
                    "action": _public_action(payload) if signed and payload else None,
                    "sentence": sentence if signed else None,
                },
            )

    async def _sentence(
        self, uow: UnitOfWork, run: RunRecord, payload: SignedActionPayload | None
    ) -> str | None:
        if payload is None:
            return None
        events = await uow.chain_events.canonical_for_run(run.id)
        earlier = [(event.event_name, event.decoded) for event in events]
        name, args = _as_event(payload)
        try:
            return self._sentences.event_sentence(name, args, earlier)
        except ValueError:
            return None

    # -----------------------------------------------------------------------------------------
    # 4 and 5. Broadcast and confirm
    # -----------------------------------------------------------------------------------------

    async def _submit(
        self, run: RunRecord, turn: TurnRecord, action: SignedActionRecord
    ) -> TurnStep:
        row = await self._relay.submit_action(run.id, action.id)
        async with self._transactions.unit_of_work() as uow:
            await uow.turns.update_state(turn.id, TurnState.CONFIRMING)
            await uow.run_events.append(
                run.id,
                "tx.status",
                tx_status_event(row, action.digest, explorer_base_url=self._explorer),
            )
        return TurnStep(TurnStatus.IN_FLIGHT, None, turn.turn)

    async def _follow(
        self, run: RunRecord, turn: TurnRecord, action: SignedActionRecord
    ) -> TurnStep:
        if action.status == ActionStatus.SIGNED:
            # Persisted and never handed over, or handed over and never sent: the relay resumes.
            return await self._submit(run, turn, action)
        if action.status in _CONFIRMED:
            await self._finish(turn, TurnState.CONFIRMED, None, None)
            return TurnStep(TurnStatus.CONFIRMED, None, turn.turn)
        if action.status == ActionStatus.REVERTED:
            await self._finish(
                turn,
                TurnState.EXECUTION_FAILED,
                "reverted",
                action.revert_error,
                termination=("execution_failure", EXECUTION_FAILURE),
            )
            return TurnStep(TurnStatus.EXECUTION_FAILED, action.revert_error, turn.turn)
        if turn.state != TurnState.CONFIRMING:
            async with self._transactions.unit_of_work() as uow:
                await uow.turns.update_state(turn.id, TurnState.CONFIRMING)
        return TurnStep(TurnStatus.IN_FLIGHT, None, turn.turn)

    async def _finish(
        self,
        turn: TurnRecord,
        state: TurnState,
        code: str | None,
        detail: str | None,
        *,
        termination: tuple[str, int] | None = None,
    ) -> None:
        async with self._transactions.unit_of_work() as uow:
            await uow.turns.update_state(
                turn.id, state, finished_at=self._clock(), failure_code=code, failure_detail=detail
            )
            if termination is not None:
                await uow.runs.request_termination(turn.run_id, *termination)


# ---------------------------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------------------------


def _finished(turn: TurnRecord) -> bool:
    return turn.state in _FINISHED or turn.finished_at is not None


def _action_for(
    actions: Sequence[SignedActionRecord], turn: TurnRecord
) -> SignedActionRecord | None:
    return next((action for action in actions if action.turn_id == turn.id), None)


def _opened(events: Sequence[ChainEventRecord]) -> ChainEventRecord | None:
    return next((event for event in events if event.event_name == "SessionOpened"), None)


def _turn_timeout(run: RunRecord) -> float:
    """The agent's own limit for every attempt it may make, and some slack."""
    attempts = 1 + int(run.limits.get("repair_attempts", 1))
    return float(int(run.limits.get("model_timeout_s", 45)) * attempts + TURN_SLACK_S)


def _party_policy(run: RunRecord, party: Party) -> tuple[PolicyKind, str | None, str | None]:
    if party == Party.BUYER:
        return run.buyer_policy, run.buyer_model_id, run.buyer_effort
    return run.seller_policy, run.seller_model_id, run.seller_effort


def _authorised_attempt(decisions: Sequence[DecisionPayload]) -> DecisionPayload | None:
    """The attempt a signed action came from: the last, which passed validation."""
    valid = [item for item in decisions if item.validation.ok]
    return valid[-1] if valid else None


def _usd(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _fields(refusal: AgentRefusedError) -> str | None:
    fields = refusal.details.get("fields")
    return None if not isinstance(fields, Mapping) else ", ".join(sorted(map(str, fields)))


def _digest(kind: str, message: Mapping[str, Any], domain: Domain) -> bytes:
    session_id = bytes.fromhex(str(message["sessionId"])[2:])
    config_hash = bytes.fromhex(str(message["configHash"])[2:])
    sequence = int(message["sequence"])
    if kind == "offer":
        return Offer(
            session_id,
            config_hash,
            sequence,
            str(message["proposer"]),
            int(message["quoteAmount"]),
            int(message["validUntil"]),
        ).digest(domain)
    if kind == "accept":
        offer_hash = bytes.fromhex(str(message["offerHash"])[2:])
        return Accept(session_id, config_hash, sequence, str(message["actor"]), offer_hash).digest(
            domain
        )
    return Close(
        session_id, config_hash, sequence, str(message["actor"]), int(message["reason"])
    ).digest(domain)


def _as_event(payload: SignedActionPayload) -> tuple[str, dict[str, Any]]:
    """The event a signed action becomes, as far as its sentence needs."""
    message = payload.typed_message
    if payload.kind == "offer":
        return "OfferRecorded", {
            "sequence": message["sequence"],
            "proposer": message["proposer"],
            "quoteAmount": message["quoteAmount"],
        }
    if payload.kind == "accept":
        return "AcceptanceRecorded", {"actor": message["actor"], "offerHash": message["offerHash"]}
    return "SessionClosed", {"actor": message["actor"], "reason": message["reason"]}


def _public_action(payload: SignedActionPayload) -> dict[str, Any]:
    """The decision as the signed message states it, in the agent decision schema's shape."""
    message = payload.typed_message
    if payload.kind == "offer":
        return {"action": "offer", "quote_amount_minor": str(message["quoteAmount"])}
    if payload.kind == "accept":
        return {"action": "accept", "offer_hash": str(message["offerHash"])}
    return {"action": "walk_away", "reason": close_reason_name(int(message["reason"]))}


_MESSAGE_FIELDS: Final = {
    "offer": {"sessionId", "configHash", "sequence", "proposer", "quoteAmount", "validUntil"},
    "accept": {"sessionId", "configHash", "sequence", "actor", "offerHash"},
    "close": {"sessionId", "configHash", "sequence", "actor", "reason"},
}
_SIGNER_FIELD: Final = {"offer": "proposer", "accept": "actor", "close": "actor"}


def _terms_hold(kind: str, message: Mapping[str, Any], observation: Mapping[str, Any]) -> bool:
    """What the contract checks of an action's own terms (protocol 8.2), against the observation
    the decision was made on: an offer valid from its chain time to no later than the session's
    deadline; an accept of the active offer; a close with one of the three reasons."""
    chain_time = int(observation["chain_time"])
    expires_at = int(observation["session"]["expires_at"])
    if kind == "offer":
        valid_until = int(message["validUntil"])
        return chain_time < valid_until <= expires_at and int(message["quoteAmount"]) > 0
    if kind == "accept":
        active = observation.get("active_offer")
        return active is not None and str(message["offerHash"]) == str(active["offer_hash"])
    return int(message["reason"]) in (1, 2, 3)


def tx_status_event(
    row: OutboxRecord,
    digest: Digest | None,
    *,
    head_block: int | None = None,
    explorer_base_url: str | None = None,
) -> dict[str, Any]:
    """`tx.status` (api_contract section 3). Confirmations are counted against the head of the poll
    that reported the status; one the relay reports, before any block, has none."""
    confirmations = 0
    if head_block is not None and row.block_number is not None:
        confirmations = max(head_block - row.block_number + 1, 0)
    explorer = None
    if explorer_base_url:
        explorer = f"{explorer_base_url.rstrip('/')}/tx/{row.tx_hash}"
    return {
        "digest": None if digest is None else str(digest),
        "tx_hash": str(row.tx_hash),
        "status": row.status.value,
        "block_number": row.block_number,
        "confirmations": confirmations,
        "explorer_url": explorer,
    }
