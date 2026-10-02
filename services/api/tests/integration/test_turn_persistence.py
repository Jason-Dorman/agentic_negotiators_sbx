"""The turn executor's two guarantees that a green negotiation cannot show (spec 9.2, protocol 12).

**Persist before broadcast.** The process dies inside the relay, after the turn's decision records
and signed action are committed and before any transaction exists. A new controller resumes the
turn from the database: it hands the stored action to the relay, and never asks the agent for a new
decision (FR-E2).

**The observation reads one mandate.** Building the observation for the party that acts next reads
`mandate_versions` once, through `get_for_party`, for that party — never the counterparty's row and
never both (data model section 3.4). The builder runs against a database whose mandate repository
refuses anything else.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from api_chain import AnvilChain
from api_controller import ControllerHarness
from api_faults import ProcessKilledError
from web3 import Web3

from api.chain import CallRequest, Web3ChainAdapter
from api.db import Database, Party, RunRecord, RunState, TurnState, TxKind
from api.observation import ObservationBuilder
from negotiation_protocol import json_sha256, validate

RECORD_OFFER = bytes(
    Web3.keccak(text="recordOffer((bytes32,bytes32,uint64,address,uint256,uint64),bytes)")[:4]
)


class DiesBeforeTheOffersTransaction(Web3ChainAdapter):
    """The relay estimates gas before it persists the transaction: dying there leaves the signed
    action committed and no outbox row."""

    def __init__(self, rpc_url: str) -> None:
        super().__init__(rpc_url)
        self.armed = True

    async def estimate_gas(self, request: CallRequest) -> int:
        if self.armed and request.data[:4] == RECORD_OFFER:
            self.armed = False
            raise ProcessKilledError("killed after the turn was persisted, before the relay")
        return await super().estimate_gas(request)


async def test_a_turn_is_persisted_before_its_broadcast_and_resumed_without_a_new_decision(
    database: Database, chain: AnvilChain
) -> None:
    dying = ControllerHarness(
        database,
        chain,
        background=False,
        holder="process-1",
        adapter=DiesBeforeTheOffersTransaction(chain.rpc_url),
    )
    run = await dying.validated()
    await dying.controller.step(run.id)
    with pytest.raises(ProcessKilledError):
        await dying.tick_until(run.id, lambda _: _never())

    async with database.unit_of_work() as uow:
        (turn,) = await uow.turns.list_for_run(run.id)
        (action,) = await uow.signed_actions.list_for_run(run.id)
        decisions = await uow.decisions.list_for_run(run.id)
        offers = [r for r in await uow.outbox.list_for_run(run.id) if r.kind == TxKind.RECORD_OFFER]
    assert turn.state == TurnState.BROADCASTING
    assert [d.authorized for d in decisions] == [True]
    assert offers == [], "no transaction yet: the action was persisted first"
    asked = list(dying.agents.requests[Party.BUYER])

    later = datetime.now(UTC) + timedelta(minutes=5)
    restarted = ControllerHarness(
        database,
        chain,
        agents=dying.agents,
        background=False,
        holder="process-2",
        clock=lambda: later,
    )
    try:
        await restarted.controller.recover()

        async def stepped(run: RunRecord) -> bool:
            return run.state == RunState.PAUSED and run.state_cause == "step_complete"

        await restarted.tick_until(run.id, stepped)
    finally:
        await restarted.aclose()
        await dying.aclose()

    assert dying.agents.requests[Party.BUYER] == asked, "the agent was not asked again"
    async with database.unit_of_work() as uow:
        (turn,) = await uow.turns.list_for_run(run.id)
        offers = [r for r in await uow.outbox.list_for_run(run.id) if r.kind == TxKind.RECORD_OFFER]
    assert turn.state == TurnState.CONFIRMED
    assert [o.signed_action_id for o in offers] == [action.id]


async def _never() -> bool:
    return False


class OnlyOneMandate:
    """`mandate_versions`, refusing every read but one party's row through `get_for_party`."""

    def __init__(self, real: Any, reads: list[Party]) -> None:
        self._real = real
        self._reads = reads

    async def get_for_party(self, run_id: Any, party: Party) -> Any:
        self._reads.append(party)
        return await self._real.get_for_party(run_id, party)

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"the observation builder read mandate_versions through {name}")


class Spied:
    def __init__(self, database: Database, reads: list[Party]) -> None:
        self._database = database
        self._reads = reads

    @asynccontextmanager
    async def unit_of_work(self) -> AsyncIterator[Any]:
        async with self._database.unit_of_work() as uow:
            uow.mandates = OnlyOneMandate(uow.mandates, self._reads)
            yield uow


@pytest.fixture
async def harness(database: Database, chain: AnvilChain) -> AsyncIterator[ControllerHarness]:
    harness = ControllerHarness(database, chain, background=False)
    yield harness
    await harness.aclose()


async def test_the_observation_reads_only_the_acting_partys_mandate(
    harness: ControllerHarness, chain: AnvilChain
) -> None:
    run = await harness.validated()
    await harness.controller.step(run.id)

    async def stepped(run: RunRecord) -> bool:
        return run.state_cause == "step_complete"

    await harness.tick_until(run.id, stepped)

    reads: list[Party] = []
    builder = ObservationBuilder(Spied(harness.database, reads), default_threshold=1)
    built = await builder.build(run.id, chain.chain_time())

    assert built.party == Party.SELLER
    assert reads == [Party.SELLER]
    async with harness.database.unit_of_work() as uow:
        mandates = await uow.mandates.get_both(run.id)
    seller, buyer = mandates[Party.SELLER], mandates[Party.BUYER]
    assert built.document["mandate"] == seller.as_document()
    assert "mandate" not in built.body
    published = repr(built.document)
    assert buyer.instructions not in published
    assert built.observation_hash == json_sha256(built.document)
    validate(built.document, "observation.v1.json")
    assert built.body["expected_sequence"] == 2
    assert built.body["history"][0]["status"] == "active"
    assert built.body["active_offer"]["sequence"] == 1

    assert built.body["my_previous_decisions"] == [], "the buyer's decision is not the seller's"

    late = await builder.build(run.id, built.body["active_offer"]["valid_until"])
    assert late.body["active_offer"] is None, "an expired offer does not stand (ADR-046)"
    assert late.body["history"][0]["status"] == "expired"

    await harness.controller.step(run.id)
    await harness.tick_until(run.id, stepped)
    for_buyer = await builder.build(run.id, chain.chain_time())
    assert for_buyer.party == Party.BUYER
    assert [entry["turn"] for entry in for_buyer.body["my_previous_decisions"]] == [1]
    assert for_buyer.body["my_previous_decisions"][0]["result"] == "recorded"
    assert reads[-1] == Party.BUYER and set(reads) == {Party.SELLER, Party.BUYER}
