"""ADR-081: a restarted chain is a deployment of its own, and the runs of the old one stay theirs.

The stage 2.5 review found that after an Anvil restart with the database kept — what `make down`
then `make stack` does — the relay took its nonces from the history of every chain the database had
seen, so the new chain's setup transactions queued behind nonces that would never come and the run
hung in `preparing` for good. Here a run is left in flight on one Anvil, and a backend serving a
second, fresh Anvil starts against the same database: the stranded run is `recovery_required`,
cause `chain_unavailable`, and gives up the active run; a new run on the new chain settles; nothing
of the old run's chain record is touched; and a backend whose deployment is the old chain's reports
the new RPC as not its chain.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from anvil_chain import deploy, running_anvil
from api_chain import AnvilChain, deployment_from_manifest
from api_controller import ControllerHarness
from api_seed import Seeder, fresh_digest

from api.chain import Web3ChainAdapter
from api.controller import InvalidStateError
from api.db import Database, DuplicateError, OutcomeKind, RunState, TxKind
from api.main import StartupError, check_chain


@pytest.fixture
def second_chain() -> Iterator[AnvilChain]:
    """A fresh Anvil with its own deployment: what a restarted Compose Anvil is."""
    with running_anvil() as url:
        manifest = deploy(url, "local-api-test-second")
        yield AnvilChain(url, manifest)


async def _events_of(database: Database, run_id: uuid.UUID) -> list[tuple[str, bool, Any]]:
    async with database.unit_of_work() as uow:
        events = await uow.chain_events.history_for_export(run_id)
    return [(e.event_name, e.canonical, e.confirmations_at_index) for e in events]


async def test_a_run_on_a_restarted_chain_settles_and_the_old_run_is_stranded(
    database: Database, chain: AnvilChain, second_chain: AnvilChain
) -> None:
    first = ControllerHarness(database, chain)
    try:
        stranded = await first.validated()
        await first.controller.step(stranded.id)
        await first.settle()
        assert (await first.run(stranded.id)).state == RunState.PAUSED
        before = await _events_of(database, stranded.id)
    finally:
        await first.aclose()

    second = ControllerHarness(database, second_chain)
    try:
        await second.prepare()
        assert await second.controller.recover() == stranded.id
        run = await second.run(stranded.id)
        assert (run.state, run.state_cause, run.outcome_kind) == (
            RunState.RECOVERY_REQUIRED,
            "chain_unavailable",
            OutcomeKind.PENDING,
        )
        async with database.unit_of_work() as uow:
            assert await uow.leases.active_run_id() is None, "the stranded run gave the slot up"
        for operation in (second.controller.resume, second.controller.abort):
            with pytest.raises(InvalidStateError) as refused:
                await operation(stranded.id)
            assert refused.value.details["deployment"] == first.deployment.deployment_id

        # The new chain's operator nonces start from the new chain's, whatever the database
        # recorded for the same key on the old one: before ADR-081 the first mint took the old
        # chain's next nonce, queued behind a gap, and the run never left `preparing`.
        async with database.unit_of_work() as uow:
            old = [
                r.nonce for r in await uow.outbox.list_for_run(stranded.id) if r.kind == TxKind.MINT
            ]
        assert max(old) + 1 > second_chain.w3.eth.get_transaction_count(
            second_chain.operator.address
        ), "the old chain's history is ahead of the new chain, so the old rule would have stuck"
        fresh = await second.validated()
        await second.controller.start(fresh.id)
        await second.settle()
        settled = await second.run(fresh.id)
        assert (settled.state, settled.outcome_kind) == (RunState.TERMINAL, OutcomeKind.SETTLED)

        # The old run's record of its own chain is as it was: never re-verified against this one.
        assert await _events_of(database, stranded.id) == before
    finally:
        await second.aclose()


async def test_a_deployment_reports_another_chain_as_not_its_own(
    database: Database, chain: AnvilChain, second_chain: AnvilChain
) -> None:
    """Health's `rpc_ok` checks the genesis block, not only the chain ID, which both share."""
    own = ControllerHarness(database, chain)
    other = ControllerHarness(database, chain, adapter=Web3ChainAdapter(second_chain.rpc_url))
    try:
        assert (await own.controller.health())["rpc_ok"] is True
        assert (await other.controller.health())["rpc_ok"] is False
    finally:
        await own.aclose()
        await other.aclose()


async def test_start_up_refuses_a_chain_that_is_not_the_manifests(
    chain: AnvilChain, second_chain: AnvilChain
) -> None:
    deployment = deployment_from_manifest(chain.manifest)
    own = Web3ChainAdapter(chain.rpc_url)
    checked = await check_chain(own, deployment, None)
    assert checked.genesis_hash is not None and str(checked.genesis_hash) == chain.genesis_hash

    # A manifest whose exchange this chain does not hold at that address.
    from dataclasses import replace

    from negotiation_protocol import Address

    elsewhere = replace(deployment, exchange_address=Address("0x" + "12" * 20))
    with pytest.raises(StartupError, match="not on this chain"):
        await check_chain(own, elsewhere, None)
    # Two fresh Anvils from one deployer hold the same code at the same addresses, so only the
    # genesis tells a restarted chain from its predecessor: with no stored row, it is accepted
    # and stored with its own genesis.
    restarted = await check_chain(Web3ChainAdapter(second_chain.rpc_url), deployment, None)
    assert str(restarted.genesis_hash) == second_chain.genesis_hash != chain.genesis_hash
    # The same deployment id stored against another genesis: a reset chain, reusing the id.
    reset = deployment_from_manifest(chain.manifest, "0x" + "ab" * 32)
    with pytest.raises(StartupError, match="loaded against another chain"):
        await check_chain(own, deployment, reset)


async def test_two_chains_may_hold_one_address_but_one_chain_may_not(
    database: Database, seed: Seeder
) -> None:
    """The uniqueness migration 0004 changes: chain ID, genesis and exchange together."""
    from dataclasses import replace

    from api_seed import deployment_record

    base = replace(deployment_record("local-a"), genesis_hash=fresh_digest())
    again = replace(base, deployment_id="local-b", genesis_hash=fresh_digest())
    async with database.unit_of_work() as uow:
        await uow.deployments.upsert(base)
        await uow.deployments.upsert(again)
    with pytest.raises(DuplicateError):
        async with database.unit_of_work() as uow:
            await uow.deployments.upsert(replace(base, deployment_id="local-c"))


@pytest.fixture
def seed(database: Database) -> Seeder:
    return Seeder(database)
