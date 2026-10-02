"""What the backend tells an agent about a run besides its turns: the session, and a restart.

Session approval is built from the canonical `SessionOpened` row and the timestamp of the block that
emitted it (ADR-044), never from what the backend meant to open. After an agent restart the run is
re-provisioned from what was stored — the mandate version, the wallet's root reference and address —
and the session approved again the same way (ADR-048). Each reads its own party's mandate row and no
other.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping

import structlog

from api.agent_client import (
    AgentClient,
    AgentError,
    SessionApproval,
    approve_session_body,
    reprovision_body,
)
from api.chain import ChainAdapter, RpcUnavailableError
from api.db.enums import Party
from api.db.protocols import Transactions
from api.db.records import ChainEventRecord, DeploymentRecord

_log = structlog.get_logger(component="turns.agents")


class SessionNotOpenedError(Exception):
    """The run has no canonical `SessionOpened` to approve from."""


class AgentSessions:
    def __init__(
        self,
        transactions: Transactions,
        agents: Mapping[Party, AgentClient],
        chain: ChainAdapter,
        deployment: DeploymentRecord,
    ) -> None:
        self._transactions = transactions
        self._agents = agents
        self._chain = chain
        self._deployment = deployment

    async def approve(self, run_id: uuid.UUID, party: Party) -> SessionApproval:
        opened = await self._opened(run_id)
        block = await self._chain.block(opened.block_number)
        if block is None:
            # Above the head: the event's block has gone. Not an answer to act on.
            raise RpcUnavailableError(f"block {opened.block_number} is not on the chain")
        body = approve_session_body(opened, block.timestamp)
        return await self._agents[party].approve_session(run_id, body)

    async def restore(self, run_id: uuid.UUID, party: Party) -> None:
        """ADR-048: re-provision with the stored address, then approve the session again."""
        await self.reprovision(run_id, party)
        await self.approve(run_id, party)
        _log.info("agent.restored", run_id=str(run_id), party=party.value)

    async def reprovision(self, run_id: uuid.UUID, party: Party) -> None:
        """The provisioning half of a restore: all an agent restarted during setup needs, before
        any session exists to approve. The agent refuses unless its derivation reproduces the
        stored address (ADR-039)."""
        async with self._transactions.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
            mandate = await uow.mandates.get_for_party(run_id, party)
            wallet = await uow.wallets.get(run_id, party)
        if run is None or mandate is None or wallet is None:
            raise SessionNotOpenedError(f"run {run_id} was never provisioned for the {party}")
        await self._agents[party].provision(
            run_id, reprovision_body(run, self._deployment, mandate, wallet)
        )

    async def release(self, run_id: uuid.UUID) -> None:
        """Both agents discard the run (api_contract section 6). Best effort: the run has ended on
        chain whatever an agent answers, and a released or restarted agent signs nothing for it."""
        for party, agent in self._agents.items():
            try:
                await agent.release(run_id)
            except AgentError as error:
                _log.warning(
                    "agent.release_failed", run_id=str(run_id), party=party.value, error=str(error)
                )

    async def _opened(self, run_id: uuid.UUID) -> ChainEventRecord:
        async with self._transactions.unit_of_work() as uow:
            events = await uow.chain_events.canonical_for_run(run_id)
        opened = next((event for event in events if event.event_name == "SessionOpened"), None)
        if opened is None:
            raise SessionNotOpenedError(f"run {run_id} has no canonical SessionOpened")
        return opened
