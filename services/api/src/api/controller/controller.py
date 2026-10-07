"""The run controller: the operations of architecture 6.1, and the one run they drive.

Each operation checks the run's state, makes its transition under the run's row lock, and leaves
the chain work to the run's driver (`driver.py`), which runs in the background so that an operation
returns while the run goes on — the route of stage 2.5 answers `202` with an operation record.

One run is active at a time (ADR-019): `start` and `step` claim the single `active_run` row and the
run's lease, and the run keeps both until it is `terminal` or `failed_setup`. A run another process
holds is refused as `another_run_active`.

- `create_run`: a `draft` run, both agents provisioned (ADR-039).
- `validate`, from `draft` or `validated`: `validated` when every check passes, else `draft`.
- `start`, from `validated`: `preparing`, then `running`. From `paused`, as `resume`.
- `step`, from `validated`: `preparing`, then `paused` and one turn. From `paused`: one turn,
  `paused` again, and `409 turn_in_progress` while one is running.
- `pause`, from `running`: `paused`, cause `operator_pause`; a turn in flight completes.
- `resume`, from `paused`: reconcile, then `running`, or `recovery_required` if reconcile fails.
  From `recovery_required`: reconcile and poll; `terminal` if the chain is, else `paused` — or
  `preparing` when setup is unfinished (ADR-063).
- `abort`, from `running`, `paused` or `recovery_required`: `abortSession`, or `expireSession` past
  the deadline; `terminal` once it is canonical.
- `clone`, from any state: a new `draft` run from the parent's request with a JSON merge patch
  applied, its mandates re-versioned (api_contract section 2.2). The parent is untouched.

`recover` is what a starting process runs (architecture 5.4): reclaim the active run's lease once
it has expired, reconcile its outbox, and drive it again if it was driven.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Final

import structlog

from api.agent_client import (
    AgentClient,
    AgentError,
    AgentRefusedError,
    AgentUnavailableError,
    provision_body,
)
from api.chain import ChainAdapter, ChainError, ExchangeCodec, RpcCounter, RpcUnavailableError
from api.config import ControllerPolicy, RelayPolicy
from api.controller import events
from api.controller.driver import RunDriver, RunMetricsSink
from api.controller.errors import (
    AnotherRunActiveError,
    ControllerError,
    InvalidStateError,
    RunNotFoundError,
    RunRequestError,
    TurnInProgressError,
)
from api.controller.requests import PartyConfig, RunRequest
from api.controller.setup import SessionSetup
from api.controller.states import (
    DRAFT,
    FINISHED,
    PAUSED,
    PREPARING,
    RECOVERY_REQUIRED,
    RUNNING,
    VALIDATED,
)
from api.db.enums import OutcomeKind, Party, PolicyKind, RunMode, RunState, SnapshotStage
from api.db.protocols import Transactions, UnitOfWork
from api.db.records import (
    DeploymentRecord,
    MandateVersionRecord,
    NewMandate,
    NewRun,
    NewWallet,
    RunRecord,
    WalletRecord,
)
from api.indexer import Indexer
from api.observation import ObservationBuilder
from api.projection import TimelineSentences
from api.relay import Relay
from api.turns import AgentSessions, TurnExecutor
from api.validation import SetupValidator, ValidationReport
from negotiation_protocol import Address, MinorAmount, UnknownReasonCodeError, abort_reason_code

_log = structlog.get_logger(component="controller")

PARTIES: Final = (Party.BUYER, Party.SELLER)


class AgentProvisioningError(ControllerError):
    """An agent did not provision the run; nothing of it was kept."""

    code = "dependency_unavailable"
    status = 503


class RunController:
    def __init__(
        self,
        transactions: Transactions,
        chain: ChainAdapter,
        codec: ExchangeCodec,
        deployment: DeploymentRecord,
        relay: Relay,
        indexer: Indexer,
        agents: Mapping[Party, AgentClient],
        root_key_refs: Mapping[Party, str],
        *,
        policy: ControllerPolicy,
        relay_policy: RelayPolicy,
        default_threshold: int,
        holder: str,
        software_version: str,
        background: bool = True,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        counter: RpcCounter | None = None,
        metrics: RunMetricsSink | None = None,
        db_ping: Callable[[], Awaitable[bool]] | None = None,
    ) -> None:
        self._transactions = transactions
        self._db_ping = db_ping
        self._chain = chain
        self._deployment = deployment
        self._relay = relay
        self._agents = agents
        self._root_key_refs = root_key_refs
        self._software_version = software_version
        self._background = background
        self._holder = holder
        self._sessions = AgentSessions(transactions, agents, chain, deployment)
        builder = ObservationBuilder(transactions, default_threshold=default_threshold)
        self._turns = TurnExecutor(
            transactions,
            builder,
            agents,
            self._sessions,
            relay,
            chain,
            deployment,
            TimelineSentences(),
            observation_retries=policy.observation_retries,
            clock=clock,
        )
        setup = SessionSetup(
            transactions,
            chain,
            relay,
            indexer,
            codec,
            deployment,
            agents,
            self._sessions,
            relay_policy=relay_policy,
            setup_gas_limit_max=policy.setup_gas_limit_max,
            default_threshold=default_threshold,
            clock=clock,
        )
        self._validator = SetupValidator(
            transactions, chain, agents, deployment, default_threshold=default_threshold
        )
        self.driver = RunDriver(
            transactions,
            chain,
            codec,
            relay,
            indexer,
            self._turns,
            setup,
            self._sessions,
            explorer_base_url=deployment.explorer_base_url,
            holder=holder,
            policy=policy,
            default_threshold=default_threshold,
            clock=clock,
            sleep=sleep,
            counter=counter,
            metrics=metrics,
        )
        self._states = self.driver.states
        self._sleep = sleep
        self._lease_ttl_s = policy.lease_ttl_s
        self._tasks: dict[uuid.UUID, asyncio.Task[None]] = {}
        self._pending: set[uuid.UUID] = set()

    # -----------------------------------------------------------------------------------------
    # Creating and validating
    # -----------------------------------------------------------------------------------------

    async def create_run(
        self, request: RunRequest, *, mandate_versions: Mapping[Party, int] | None = None
    ) -> RunRecord:
        """A run in `draft`, its mandates, and its two wallets, which the agents derive (ADR-039).

        Provisioning happens inside the run's own unit of work: if either agent does not provision
        it, nothing of the run is kept, and an agent that did provision it is told to release it.
        A clone's mandates carry the version after its parent's (`mandate_versions`); a new run's
        are version 1.
        """
        if request.deployment_id != self._deployment.deployment_id:
            raise RunRequestError("unknown deployment", {"fields": {"deployment_id": "unknown"}})
        try:
            async with self._transactions.unit_of_work() as uow:
                if (
                    request.scenario_id is not None
                    and await uow.scenarios.get(request.scenario_id) is None
                ):
                    raise RunRequestError(
                        "unknown scenario", {"fields": {"scenario_id": "unknown"}}
                    )
                run = await uow.runs.add(self._new_run(request))
                await self._provision(uow, run, request, mandate_versions or {})
                run = await uow.runs.get(run.id) or run
                await uow.run_events.append(run.id, "run.state", events.run_state(run))
        except AgentError as error:
            raise _provisioning_error(error) from None
        _log.info("run.created", run_id=str(run.id))
        return run

    def _new_run(self, request: RunRequest) -> NewRun:
        return NewRun(
            name=request.name,
            deployment_id=request.deployment_id,
            public_config=request.public_config_document(),
            limits=request.limits.model_dump(mode="json"),
            buyer_policy=PolicyKind(request.buyer.policy),
            seller_policy=PolicyKind(request.seller.policy),
            software_version=self._software_version,
            mode=RunMode.LIVE,
            scenario_id=request.scenario_id,
            parent_run_id=request.parent_run_id,
            buyer_model_id=request.buyer.model_id,
            seller_model_id=request.seller.model_id,
            buyer_effort=request.buyer.effort,
            seller_effort=request.seller.effort,
        )

    async def _provision(
        self,
        uow: UnitOfWork,
        run: RunRecord,
        request: RunRequest,
        versions: Mapping[Party, int],
    ) -> None:
        policy_versions: dict[str, str] = {}
        prompt_versions: dict[str, str | None] = {}
        provisioned: list[Party] = []
        party = PARTIES[0]
        try:
            for party in PARTIES:
                config = request.buyer if party == Party.BUYER else request.seller
                mandate = await uow.mandates.add(
                    _mandate(run.id, party, config, versions.get(party, 1))
                )
                body = provision_body(
                    run,
                    self._deployment,
                    party,
                    mandate,
                    key_ref=self._root_key_refs[party],
                    initial_balances=config.initial_balances.model_dump(),
                    allowance_minor=config.allowance_minor,
                    expected_address=None,
                )
                response = await self._agents[party].provision(run.id, body)
                provisioned.append(party)
                await uow.wallets.add(
                    _wallet(
                        run.id,
                        party,
                        config,
                        self._root_key_refs[party],
                        response.my_address,
                        response.key_derivation.model_dump(),
                    )
                )
                policy_versions[party.value] = response.policy_version
                prompt_versions[party.value] = response.prompt_template_version
        except AgentError as error:
            if isinstance(error, AgentUnavailableError):
                # Its answer may have been lost after it provisioned the run: release it too.
                provisioned.append(party)
            for released in provisioned:
                await _release_quietly(self._agents[released], run.id)
            raise
        await uow.runs.set_versions(run.id, policy_versions, prompt_versions)

    async def clone(self, parent_id: uuid.UUID, patch: Any) -> RunRecord:
        """A fresh `draft` run: the parent's own request, with `patch` applied as a JSON merge
        patch (RFC 7386), mandates included unless the patch changes them, each re-versioned one
        past the parent's. The parent is untouched (api_contract section 2.2)."""
        if not isinstance(patch, dict):
            raise RunRequestError(
                "a clone patch is a JSON object", {"fields": {"<root>": "object"}}
            )
        if "parent_run_id" in patch:
            raise RunRequestError(
                "a clone's parent is the run it is cloned from",
                {"fields": {"parent_run_id": "set by clone"}},
            )
        async with self._transactions.unit_of_work() as uow:
            parent = await uow.runs.get(parent_id)
            if parent is None:
                raise RunNotFoundError(f"no run {parent_id}")
            mandates = await uow.mandates.get_both(parent_id)
            wallets = {wallet.party: wallet for wallet in await uow.wallets.list_for_run(parent_id)}
        document = merge_patch(_original_request(parent, mandates, wallets), patch)
        document["parent_run_id"] = str(parent.id)
        request = RunRequest.parse(document)
        versions = {party: mandates[party].version + 1 for party in mandates}
        return await self.create_run(request, mandate_versions=versions)

    async def validate(self, run_id: uuid.UUID) -> ValidationReport:
        run = await self._require(run_id, "validate", (DRAFT, VALIDATED))
        report = await self._validator.validate(run)
        if report.mode != run.mode:
            # ADR-088: the agents say whether the run's model decisions are canned; every surface
            # labels the run from this column.
            async with self._transactions.unit_of_work() as uow:
                await uow.runs.set_mode(run_id, report.mode)
        target = VALIDATED if report.ok else DRAFT
        if target != run.state:
            await self._states.move(
                run_id,
                target,
                None if report.ok else "validation_failed",
                expect=(run.state,),
                operation="validate",
            )
        return report

    # -----------------------------------------------------------------------------------------
    # Starting, stepping, pausing, resuming
    # -----------------------------------------------------------------------------------------

    async def start(self, run_id: uuid.UUID) -> RunRecord:
        run = await self._require(run_id, "start", (VALIDATED, PAUSED))
        if run.state == PAUSED:
            return await self.resume(run_id)
        return await self._prepare(run, "start")

    async def step(self, run_id: uuid.UUID) -> RunRecord:
        run = await self._require(run_id, "step", (VALIDATED, PAUSED))
        _refuse_while_terminating("step", run)
        if run.state == VALIDATED:
            return await self._prepare(run, "step")
        if run.state_cause == "step" or await self._turns.open_turn(run_id) is not None:
            raise TurnInProgressError("a turn is already running")
        updated = await self._states.begin_step(run_id)
        self._schedule(run_id)
        return updated

    async def _prepare(self, run: RunRecord, mode: str) -> RunRecord:
        async with self._transactions.unit_of_work() as uow:
            if not await uow.leases.claim_active_run(run.id):
                raise AnotherRunActiveError("another run is active")
        if not await self.driver.hold(run.id):
            raise AnotherRunActiveError("another process holds this run's lease")
        updated = await self._states.move(
            run.id, PREPARING, mode, expect=(VALIDATED,), operation=mode
        )
        self._schedule(run.id)
        return updated

    async def pause(self, run_id: uuid.UUID) -> RunRecord:
        run = await self._require(run_id, "pause", (RUNNING, PAUSED))
        if run.state == PAUSED:
            return run
        updated = await self._states.move(
            run_id, PAUSED, "operator_pause", expect=(RUNNING,), operation="pause"
        )
        async with self._transactions.unit_of_work() as uow:
            await uow.run_events.append(
                run_id, "notice", events.notice("info", events.PAUSE_NOTICE)
            )
        return updated

    async def resume(self, run_id: uuid.UUID) -> RunRecord:
        run = await self._require(run_id, "resume", (PAUSED, RECOVERY_REQUIRED))
        _refuse_while_terminating("resume", run)
        if not await self.driver.hold(run_id):
            raise AnotherRunActiveError("another process holds this run's lease")
        # A fresh outage window, and fresh setup approval terms (ADR-063, ADR-072).
        self.driver.reset_outages()
        self.driver.setup.forget(run_id)
        if run.state == RECOVERY_REQUIRED:
            return await self._recover_run(run)
        if await self.driver.reconcile(run_id):
            await self._states.move(run_id, RUNNING, None, expect=(PAUSED,), operation="resume")
        self._schedule(run_id)
        return await self._get(run_id)

    async def _recover_run(self, run: RunRecord) -> RunRecord:
        """The operator's recovery (architecture 5.4): reconcile, poll, decide from the chain."""
        if not await self.driver.reconcile(run.id):
            return await self._get(run.id)
        try:
            await self.driver.sync(run)
        except RpcUnavailableError:
            await self._states.move(
                run.id, RECOVERY_REQUIRED, "rpc_timeout", expect=(RECOVERY_REQUIRED,)
            )
            return await self._get(run.id)
        current = await self._get(run.id)
        if current.state != RECOVERY_REQUIRED:
            return current  # the chain was terminal, or the poll found another problem
        async with self._transactions.unit_of_work() as uow:
            snapshots = await uow.balances.canonical_for_run(run.id)
        set_up = any(snapshot.stage == SnapshotStage.POST_SETUP for snapshot in snapshots)
        updated = await self._states.move(
            run.id,
            PAUSED if set_up else PREPARING,
            "recovered",
            expect=(RECOVERY_REQUIRED,),
            operation="resume",
        )
        self._schedule(run.id)
        return updated

    # -----------------------------------------------------------------------------------------
    # Aborting
    # -----------------------------------------------------------------------------------------

    async def abort(self, run_id: uuid.UUID, reason: str = "operator_request") -> RunRecord:
        """Stop decisions and end the session on chain: `abortSession` before the deadline,
        `expireSession` at or after it; a run with no session is `failed_setup` at once
        (ADR-067). The termination is recorded on the run here and sent by the driver
        (ADR-068), so two aborts send one, and a decision arriving after it is never signed into
        the run (ADR-070). A settlement that lands first wins (spec 9.4)."""
        try:
            code = abort_reason_code(reason)
        except UnknownReasonCodeError:
            raise RunRequestError(
                "unknown abort reason", {"fields": {"reason": "not an abort reason"}}
            ) from None
        await self._require(run_id, "abort", (PREPARING, RUNNING, PAUSED, RECOVERY_REQUIRED))
        if not await self.driver.hold(run_id):
            raise AnotherRunActiveError("another process holds this run's lease")
        await self.driver.request_termination(run_id, "abort_requested", code)
        self._schedule(run_id)
        return await self._get(run_id)

    # -----------------------------------------------------------------------------------------
    # Health
    # -----------------------------------------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        """`GET /v1/health`'s dependencies: the database answers, the RPC answers with the
        deployment's chain, and each agent answers as its role with its signer loaded. A request
        made here belongs to no run's metrics (ADR-061)."""
        agents = {party: await self._agent_ok(party) for party in PARTIES}
        return {
            "chain_id": self._deployment.chain_id,
            "rpc_ok": await self._chain_ok(),
            "db_ok": True if self._db_ping is None else await self._db_ping(),
            "agent_a_ok": agents[Party.BUYER],
            "agent_b_ok": agents[Party.SELLER],
        }

    async def _chain_ok(self) -> bool:
        """The RPC answers with the deployment's chain: its ID, and its genesis block where the
        deployment recorded one (ADR-081) — so a restarted Anvil is not taken for the old one."""
        try:
            if await self._chain.chain_id() != self._deployment.chain_id:
                return False
            if self._deployment.genesis_hash is None:
                return True
            genesis = await self._chain.block(0)
        except ChainError:
            return False
        return genesis is not None and genesis.hash == self._deployment.genesis_hash

    async def _agent_ok(self, party: Party) -> bool:
        try:
            health = await self._agents[party].health()
        except AgentError:
            return False
        return health.role == party.value and health.signer_ok

    # -----------------------------------------------------------------------------------------
    # Start-up recovery and the background driver
    # -----------------------------------------------------------------------------------------

    async def recover(self, *, wait: bool = True) -> uuid.UUID | None:
        """At process start (architecture 5.4): the active run is taken over once the old
        process's lease has expired — waited for, retrying at a third of the lease's length — then
        reconciled and driven again if it was being driven. With `wait=False`, None while
        another live holder has it."""
        async with self._transactions.unit_of_work() as uow:
            run_id = await uow.leases.active_run_id()
            run = None if run_id is None else await uow.runs.get(run_id)
            if run is not None and run.state in FINISHED:
                await uow.leases.release_active_run(run.id)
                return run.id
        if run is None:
            return None
        if run.deployment_id != self._deployment.deployment_id:
            await self._strand(run)
            return run.id
        while not await self.driver.hold(run.id):
            if not wait:
                return None
            await self._sleep(self._lease_ttl_s / 3)
        self.driver.reset_outages()
        self.driver.setup.forget(run.id)
        if run.state in (PREPARING, RUNNING, PAUSED) and not await self.driver.reconcile(run.id):
            return run.id
        _log.info("run.recovered", run_id=str(run.id), state=run.state.value)
        self._schedule(run.id)
        return run.id

    async def _strand(self, run: RunRecord) -> None:
        """ADR-081: the active run is on a chain this backend does not serve — a restarted Anvil's
        predecessor. Nothing can reach its session again, so it is `recovery_required`, cause
        `chain_unavailable`, its outcome still pending, and it gives up the active-run slot so a
        run on this chain can start. Every later operation on it is refused."""
        _log.error("run.stranded", run_id=str(run.id), deployment_id=run.deployment_id)
        if run.state != RECOVERY_REQUIRED or run.state_cause != "chain_unavailable":
            await self._states.move(
                run.id, RECOVERY_REQUIRED, "chain_unavailable", expect=(run.state,)
            )
        async with self._transactions.unit_of_work() as uow:
            await uow.leases.release_active_run(run.id)

    def _schedule(self, run_id: uuid.UUID) -> None:
        """Make sure the run is driven. A task that is about to finish is told to look again, so
        an operation arriving as it exits is not lost."""
        if not self._background:
            return
        self._pending.add(run_id)
        task = self._tasks.get(run_id)
        if task is None or task.done():
            self._tasks[run_id] = asyncio.create_task(
                self._drive_until_quiet(run_id), name=f"run-{run_id}"
            )

    async def _drive_until_quiet(self, run_id: uuid.UUID) -> None:
        while True:
            self._pending.discard(run_id)
            await self.driver.drive(run_id)
            if run_id not in self._pending:
                return

    async def wait(self) -> None:
        """Until every background driver has nothing left to drive. A failure is raised here."""
        while live := [task for task in self._tasks.values() if not task.done()]:
            for task in live:
                await task
        for task in self._tasks.values():
            task.result()

    async def drive(self, run_id: uuid.UUID) -> None:
        """Drive the run in the caller's task, until there is nothing left to drive."""
        await self.driver.drive(run_id)

    async def aclose(self) -> None:
        """Stop driving and give up the lease, so a process started after this one takes the run
        over at once rather than after `RUN_LEASE_TTL_S`."""
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        async with self._transactions.unit_of_work() as uow:
            active = await uow.leases.active_run_id()
            if active is not None:
                await uow.leases.release(active, self._holder)

    # -----------------------------------------------------------------------------------------

    async def _get(self, run_id: uuid.UUID) -> RunRecord:
        async with self._transactions.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
        if run is None:
            raise RunNotFoundError(f"no run {run_id}")
        return run

    async def _require(
        self, run_id: uuid.UUID, operation: str, allowed_from: tuple[RunState, ...]
    ) -> RunRecord:
        run = await self._get(run_id)
        if run.deployment_id != self._deployment.deployment_id:
            raise InvalidStateError(
                operation, run.state, allowed_from, deployment=run.deployment_id
            )
        if run.state not in allowed_from:
            raise InvalidStateError(operation, run.state, allowed_from)
        return run


def _refuse_while_terminating(operation: str, run: RunRecord) -> None:
    """ADR-070: nothing resumes a run whose session is being ended."""
    if run.termination_cause is not None and run.outcome_kind == OutcomeKind.PENDING:
        raise InvalidStateError(operation, run.state, (), termination=run.termination_cause)


def _mandate(run_id: uuid.UUID, party: Party, config: PartyConfig, version: int) -> NewMandate:
    return NewMandate(
        run_id=run_id,
        party=party,
        version=version,
        reservation_price_minor=MinorAmount.parse(config.mandate.reservation_price_minor),
        min_remaining_inventory_minor=MinorAmount.parse(
            config.mandate.min_remaining_inventory_minor
        ),
        instructions=config.mandate.instructions,
    )


def merge_patch(target: Any, patch: Any) -> Any:
    """RFC 7386: an object patch merges member by member, null removes, anything else replaces."""
    if not isinstance(patch, dict):
        return patch
    merged = dict(target) if isinstance(target, dict) else {}
    for key, value in patch.items():
        if value is None:
            merged.pop(key, None)
        else:
            merged[key] = merge_patch(merged.get(key), value)
    return merged


def _original_request(
    run: RunRecord,
    mandates: Mapping[Party, MandateVersionRecord],
    wallets: Mapping[Party, WalletRecord],
) -> dict[str, Any]:
    """The `POST /v1/runs` body a run was created from, rebuilt from what was stored of it."""

    def party(role: Party) -> dict[str, Any]:
        wallet, mandate = wallets[role], mandates[role]
        is_buyer = role == Party.BUYER
        return {
            "policy": (run.buyer_policy if is_buyer else run.seller_policy).value,
            "model_id": run.buyer_model_id if is_buyer else run.seller_model_id,
            "effort": run.buyer_effort if is_buyer else run.seller_effort,
            "initial_balances": {
                "base_minor": wallet.initial_base_minor.to_json(),
                "quote_minor": wallet.initial_quote_minor.to_json(),
            },
            "allowance_minor": wallet.allowance_minor.to_json(),
            "mandate": mandate.as_document(),
        }

    return {
        "name": run.name,
        "scenario_id": run.scenario_id,
        "deployment_id": run.deployment_id,
        "public_config": dict(run.public_config),
        "buyer": party(Party.BUYER),
        "seller": party(Party.SELLER),
        "limits": dict(run.limits),
    }


def _wallet(
    run_id: uuid.UUID,
    party: Party,
    config: PartyConfig,
    key_ref: str,
    address: str,
    derivation: dict[str, object],
) -> NewWallet:
    return NewWallet(
        run_id=run_id,
        party=party,
        address=Address(address),
        key_ref=key_ref,
        key_derivation=dict(derivation),
        initial_base_minor=MinorAmount.parse(config.initial_balances.base_minor),
        initial_quote_minor=MinorAmount.parse(config.initial_balances.quote_minor),
        allowance_minor=MinorAmount.parse(config.allowance_minor),
    )


async def _release_quietly(agent: AgentClient, run_id: uuid.UUID) -> None:
    try:
        await agent.release(run_id)
    except AgentError:
        _log.warning("agent.release_failed", run_id=str(run_id))


def _provisioning_error(error: AgentError) -> ControllerError:
    if isinstance(error, AgentRefusedError):
        return RunRequestError(
            "an agent refused to provision the run",
            {"agent_code": error.code, "fields": error.details.get("fields", {})},
        )
    if isinstance(error, AgentUnavailableError):
        return AgentProvisioningError("an agent did not answer", {"dependency": "agent"})
    return AgentProvisioningError(str(error))
