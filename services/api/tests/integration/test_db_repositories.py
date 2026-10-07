"""What each repository does with real rows in a real PostgreSQL.

The constraint tests show what the database refuses. These show what the repositories return: that
a projection never sees a non-canonical row (data model invariant 6), that the lease and the relay
nonce are taken atomically and survive a takeover, that run-event cursors are gapless under
concurrent appends, and that every read reflects writes made earlier in the same unit of work.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from api_seed import Seeder, deployment_record, fresh_address, fresh_digest, now, scenario_record

from api.db import (
    ActionStatus,
    Database,
    DuplicateError,
    Inclusion,
    LeaseLostError,
    NewBalanceSnapshot,
    NewChainEvent,
    NewDecision,
    NewOperation,
    NotFoundError,
    OperationStatus,
    OutcomeKind,
    Party,
    PolicyKind,
    RunMetricsRecord,
    RunMode,
    RunState,
    SnapshotStage,
    TokenRole,
    TurnState,
    TxStatus,
)
from negotiation_protocol import Address, Digest, MinorAmount, SessionId, json_sha256


@pytest.fixture
def seed(database: Database) -> Seeder:
    return Seeder(database)


class TestUnitOfWork:
    async def test_everything_in_a_failed_unit_of_work_is_rolled_back(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        with pytest.raises(RuntimeError, match="abandoned"):
            async with database.unit_of_work() as uow:
                await seed.mandates(uow, run.id)
                raise RuntimeError("abandoned")
        async with database.unit_of_work() as uow:
            assert await uow.mandates.get_both(run.id) == {}

    async def test_a_refused_write_does_not_poison_the_rest_of_the_unit_of_work(
        self, database: Database, seed: Seeder
    ) -> None:
        # Writes run in savepoints, so the relay can catch a duplicate and carry on (A06).
        address = fresh_address()
        first, second = await seed.run(), await seed.run()
        async with database.unit_of_work() as uow:
            await uow.wallets.add(seed.wallet(first.id, Party.BUYER, address))
            with pytest.raises(DuplicateError):
                await uow.wallets.add(seed.wallet(second.id, Party.BUYER, address))
            await uow.wallets.add(seed.wallet(second.id, Party.BUYER))
        async with database.unit_of_work() as uow:
            assert len(await uow.wallets.list_for_run(second.id)) == 1

    async def test_ping(self, database: Database) -> None:
        assert await database.ping() is True

    async def test_ping_reports_an_unreachable_database_as_false(self) -> None:
        unreachable = Database("postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none", pool_size=1)
        try:
            assert await unreachable.ping() is False
        finally:
            await unreachable.dispose()


class TestReferenceData:
    async def test_upsert_inserts_then_replaces(self, database: Database) -> None:
        scenario = scenario_record()
        async with database.unit_of_work() as uow:
            await uow.scenarios.upsert(scenario)
            await uow.scenarios.upsert(replace(scenario, name="Renamed", source_hash="0x01"))
            stored = await uow.scenarios.get(scenario.scenario_id)
            assert await uow.scenarios.get("no-such-scenario") is None
            assert [s.scenario_id for s in await uow.scenarios.list_all()] == ["default-overlap"]
        assert stored is not None
        assert (stored.name, stored.source_hash) == ("Renamed", "0x01")

    async def test_deployments_round_trip_with_their_value_objects(
        self, database: Database
    ) -> None:
        deployment = deployment_record("local-a")
        async with database.unit_of_work() as uow:
            await uow.deployments.upsert(deployment)
            await uow.deployments.upsert(replace(deployment, start_block=7))
            await uow.deployments.upsert(deployment_record("local-b"))
            stored = await uow.deployments.get("local-a")
            listed = await uow.deployments.list_all()
            assert await uow.deployments.get("local-z") is None
        assert stored is not None
        assert stored.start_block == 7
        assert type(stored.exchange_address) is Address
        assert stored.exchange_address == deployment.exchange_address
        assert [d.deployment_id for d in listed] == ["local-a", "local-b"]


class TestRuns:
    async def test_a_new_run_starts_in_draft_with_nothing_decided(self, seed: Seeder) -> None:
        run = (await seed.run()).run
        assert run.state is RunState.DRAFT
        assert run.outcome_kind is OutcomeKind.PENDING
        assert run.outcome_actor is None and run.outcome_tx_hash is None
        assert run.mode is RunMode.FIXTURE
        assert run.buyer_policy is PolicyKind.DETERMINISTIC
        assert run.policy_versions == {} and run.prompt_template_versions == {}
        assert run.created_at.tzinfo is not None

    async def test_state_session_and_versions_are_updated_in_place(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        session_id = SessionId(fresh_digest())
        config_hash = fresh_digest()
        started = now()
        async with database.unit_of_work() as uow:
            locked = await uow.runs.get_for_update(run.id)
            assert locked is not None and locked.id == run.id
            await uow.runs.update_state(run.id, RunState.PREPARING, "start")
            await uow.runs.set_session(run.id, session_id, config_hash, 1_800_000_000, started)
            await uow.runs.set_versions(run.id, {"buyer": "det-1.0.0"}, {"buyer": None})
            # The same unit of work sees its own writes, not the row it loaded before them.
            current = await uow.runs.get(run.id)
        assert current is not None
        assert (current.state, current.state_cause) == (RunState.PREPARING, "start")
        assert current.session_id == session_id and type(current.session_id) is SessionId
        assert current.config_hash == config_hash
        assert current.session_expires_at_ts == 1_800_000_000
        assert current.policy_versions == {"buyer": "det-1.0.0"}
        assert current.updated_at >= run.run.updated_at

    async def test_updating_a_run_that_does_not_exist_is_an_error(self, database: Database) -> None:
        async with database.unit_of_work() as uow:
            with pytest.raises(NotFoundError, match="run"):
                await uow.runs.update_state(uuid.uuid4(), RunState.VALIDATED)
            assert await uow.runs.get(uuid.uuid4()) is None
            assert await uow.runs.get_for_update(uuid.uuid4()) is None


class TestMandates:
    async def test_each_party_reads_its_own_row_and_the_hash_covers_the_document(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        async with database.unit_of_work() as uow:
            await seed.mandates(uow, run.id)
            buyer = await uow.mandates.get_for_party(run.id, Party.BUYER)
            both = await uow.mandates.get_both(run.id)
        assert buyer is not None
        assert buyer.party is Party.BUYER
        assert buyer.reservation_price_minor == 100_000_000
        assert type(buyer.reservation_price_minor) is MinorAmount
        assert buyer.mandate_hash == json_sha256(buyer.as_document())
        assert buyer.as_document() == {
            "reservation_price_minor": "100000000",
            "min_remaining_inventory_minor": "0",
            "instructions": "buyer instructions",
        }
        assert set(both) == {Party.BUYER, Party.SELLER}
        assert both[Party.SELLER].min_remaining_inventory_minor == 10_000_000

    async def test_a_second_mandate_for_the_same_party_is_refused(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        async with database.unit_of_work() as uow:
            await seed.mandates(uow, run.id)
            with pytest.raises(DuplicateError) as refused:
                await seed.mandates(uow, run.id)
        assert refused.value.constraint == "uq_mandate_versions_run_id_party"


class TestWallets:
    async def test_add_read_and_track_setup(self, database: Database, seed: Seeder) -> None:
        run = await seed.run()
        approve = fresh_digest()
        async with database.unit_of_work() as uow:
            await uow.wallets.add(seed.wallet(run.id, Party.SELLER))
            await uow.wallets.add(seed.wallet(run.id, Party.BUYER))
            await uow.wallets.set_setup_nonce(run.id, Party.BUYER, 1)
            await uow.wallets.record_funding_tx(run.id, Party.BUYER, "fund_eth", fresh_digest())
            updated = await uow.wallets.record_funding_tx(run.id, Party.BUYER, "approve", approve)
            listed = await uow.wallets.list_for_run(run.id)
            assert await uow.wallets.get(run.id, Party.SELLER) is not None
        assert [w.party for w in listed] == [Party.BUYER, Party.SELLER]
        assert updated.setup_nonce_next == 1
        assert set(updated.funded_tx_hashes) == {"fund_eth", "approve"}
        assert updated.funded_tx_hashes["approve"] == approve
        assert updated.key_derivation["role"] == "buyer"

    async def test_setup_changes_to_a_missing_wallet_are_errors(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        async with database.unit_of_work() as uow:
            with pytest.raises(NotFoundError):
                await uow.wallets.set_setup_nonce(run.id, Party.BUYER, 1)
            with pytest.raises(NotFoundError):
                await uow.wallets.record_funding_tx(run.id, Party.BUYER, "x", fresh_digest())
            assert await uow.wallets.get(run.id, Party.BUYER) is None


class TestTheActiveRunAndTheLease:
    async def test_one_active_run_at_a_time(self, database: Database, seed: Seeder) -> None:
        first, second = await seed.run(), await seed.run()
        async with database.unit_of_work() as uow:
            assert await uow.leases.active_run_id() is None
            assert await uow.leases.claim_active_run(first.id) is True
            assert await uow.leases.claim_active_run(first.id) is True, "re-claiming is idempotent"
            assert await uow.leases.claim_active_run(second.id) is False
            await uow.leases.release_active_run(second.id)  # not the holder: no effect
            assert await uow.leases.active_run_id() == first.id
            await uow.leases.release_active_run(first.id)
            assert await uow.leases.claim_active_run(second.id) is True

    async def test_a_live_lease_is_refused_to_another_holder_and_taken_over_once_expired(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        t0 = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
        ttl = timedelta(seconds=30)
        async with database.unit_of_work() as uow:
            lease = await uow.leases.acquire(run.id, "proc-a", ttl, t0, relay_nonce_floor=5)
            assert lease is not None and lease.relay_nonce_next == 5
            renewed = await uow.leases.acquire(run.id, "proc-a", ttl, t0 + ttl / 2, 0)
            assert renewed is not None and renewed.expires_at == t0 + ttl / 2 + ttl
            assert await uow.leases.acquire(run.id, "proc-b", ttl, t0 + ttl, 0) is None
            assert await uow.leases.expired(t0 + ttl) == []

            later = t0 + 3 * ttl
            assert [lease.run_id for lease in await uow.leases.expired(later)] == [run.id]
            taken = await uow.leases.acquire(run.id, "proc-b", ttl, later, relay_nonce_floor=0)
        assert taken is not None and taken.holder == "proc-b"
        # The nonce counter belongs to the relay key, not to the holder: a takeover keeps it.
        assert taken.relay_nonce_next == 5

    async def test_relay_nonces_are_the_larger_of_stored_and_chain_and_never_repeat(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        async with database.unit_of_work() as uow:
            await uow.leases.acquire(run.id, "proc", timedelta(seconds=30), now(), 3)
            taken = [
                await uow.leases.take_relay_nonce(run.id, "proc", chain_nonce=0),
                await uow.leases.take_relay_nonce(run.id, "proc", chain_nonce=0),
                # Something outside this process used the key: the chain is ahead.
                await uow.leases.take_relay_nonce(run.id, "proc", chain_nonce=10),
                await uow.leases.take_relay_nonce(run.id, "proc", chain_nonce=10),
            ]
        assert taken == [3, 4, 10, 11]

    async def test_only_the_holder_can_take_a_nonce(self, database: Database, seed: Seeder) -> None:
        run = await seed.run()
        async with database.unit_of_work() as uow:
            await uow.leases.acquire(run.id, "proc-a", timedelta(seconds=30), now(), 0)
            with pytest.raises(LeaseLostError):
                await uow.leases.take_relay_nonce(run.id, "proc-b", chain_nonce=0)
            await uow.leases.release(run.id, "proc-a")
            with pytest.raises(LeaseLostError):
                await uow.leases.take_relay_nonce(run.id, "proc-a", chain_nonce=0)

    async def test_concurrent_nonce_takers_never_share_a_nonce(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        async with database.unit_of_work() as uow:
            await uow.leases.acquire(run.id, "proc", timedelta(minutes=5), now(), 0)

        async def take() -> int:
            async with database.unit_of_work() as uow:
                return await uow.leases.take_relay_nonce(run.id, "proc", chain_nonce=0)

        nonces = await asyncio.gather(*(take() for _ in range(8)))
        assert sorted(nonces) == list(range(8))


class TestTurnsAndDecisions:
    async def test_turns_in_order_and_their_state_moves(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        finished = now()
        async with database.unit_of_work() as uow:
            second = await seed.turn(uow, run.id, number=2, party=Party.SELLER)
            first = await seed.turn(uow, run.id, number=1)
            await uow.turns.update_state(first.id, TurnState.CONFIRMED, finished_at=finished)
            failed = await uow.turns.update_state(
                second.id,
                TurnState.MODEL_FAILED,
                finished_at=finished,
                failure_code="repair_exhausted",
                failure_detail="two invalid responses",
            )
            ordered = await uow.turns.list_for_run(run.id)
            by_number = await uow.turns.get_by_number(run.id, 1)
            assert await uow.turns.get(first.id) is not None
            assert await uow.turns.get_by_number(run.id, 9) is None
            with pytest.raises(NotFoundError):
                await uow.turns.update_state(uuid.uuid4(), TurnState.CONFIRMED)
        assert [turn.turn for turn in ordered] == [1, 2]
        assert by_number is not None and by_number.state is TurnState.CONFIRMED
        assert by_number.finished_at == finished
        assert (failed.failure_code, failed.failure_detail) == (
            "repair_exhausted",
            "two invalid responses",
        )

    async def test_decisions_by_run_and_by_party_in_turn_and_attempt_order(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        async with database.unit_of_work() as uow:
            buyer_turn = await seed.turn(uow, run.id, number=1)
            seller_turn = await seed.turn(uow, run.id, number=2, party=Party.SELLER)
            await seed.decision(uow, seller_turn)
            repair = await seed.decision(uow, buyer_turn, attempt=2)
            await seed.decision(uow, buyer_turn, attempt=1, validation_ok=False)
            authorised = await uow.decisions.mark_authorized(repair.id)
            everything = await uow.decisions.list_for_run(run.id)
            buyers = await uow.decisions.list_for_party(run.id, Party.BUYER)
            with pytest.raises(NotFoundError):
                await uow.decisions.mark_authorized(uuid.uuid4())
        assert [(d.party, d.attempt) for d in everything] == [
            (Party.BUYER, 1),
            (Party.BUYER, 2),
            (Party.SELLER, 1),
        ]
        assert [d.attempt for d in buyers] == [1, 2]
        assert buyers[0].validation_feedback == "Your quote is above your limit."
        assert authorised.authorized is True

    async def test_a_nul_is_stored_as_text_and_the_record_says_so(
        self, database: Database, seed: Seeder
    ) -> None:
        """Q71, ADR-090: JSONB and TEXT refuse a NUL; the record keeps it as `\\u0000`, with each
        backslash doubled so that the escape is reversible."""
        run = await seed.run()
        raw = {
            "decision": {"action": "offer", "quote_amount_minor": "95000000", "x\x00": 1},
            "explanation": "Opening\x00 high.",
            "notes": ["\x00", 7, None],
        }
        async with database.unit_of_work() as uow:
            turn = await seed.turn(uow, run.id, number=1)
            stored = await uow.decisions.add(
                self._decision(turn, raw, feedback='... remove "x\x00".')
            )
            plain = await seed.decision(uow, turn, attempt=2)
            [read, _] = await uow.decisions.list_for_run(run.id)
        assert read.raw_response == {
            "decision": {"action": "offer", "quote_amount_minor": "95000000", "x\\u0000": 1},
            "explanation": "Opening\\u0000 high.",
            "notes": ["\\u0000", 7, None],
        }
        assert read.validation_feedback == '... remove "x\\u0000".'
        assert stored.raw_response_escaped is read.raw_response_escaped is True
        assert plain.raw_response_escaped is False

    @pytest.mark.parametrize(
        ("raw", "stop_reason", "feedback"),
        [
            ({"k\x00": 1}, None, None),
            ({"notes": [1, "\x00"]}, None, None),
            ({"x": 1}, "end\x00turn", None),
            ({"x": 1}, None, "fb\x00"),
            ("plain text\x00", None, None),
        ],
        ids=["key_only", "list_item_only", "stop_reason_only", "feedback_only", "text_only"],
    )
    async def test_any_one_nul_escapes_the_record(
        self,
        database: Database,
        seed: Seeder,
        raw: Any,
        stop_reason: str | None,
        feedback: str | None,
    ) -> None:
        run = await seed.run()
        async with database.unit_of_work() as uow:
            turn = await seed.turn(uow, run.id, number=1)
            stored = await uow.decisions.add(
                self._decision(turn, raw, stop_reason=stop_reason, feedback=feedback)
            )
        assert stored.raw_response_escaped is True
        stored_text = json.dumps(
            [stored.raw_response, stored.stop_reason, stored.validation_feedback]
        )
        assert "\\u0000" in stored_text and "\x00" not in stored_text

    async def test_the_escape_keeps_a_nul_and_the_literal_text_apart(
        self, database: Database, seed: Seeder
    ) -> None:
        """A key with a NUL and a key that already reads `\\u0000` stay two keys."""
        run = await seed.run()
        async with database.unit_of_work() as uow:
            turn = await seed.turn(uow, run.id, number=1)
            stored = await uow.decisions.add(
                self._decision(turn, {"x\x00": 1, "x\\u0000": 2, "path": "C:\\dir"})
            )
        assert stored.raw_response == {"x\\u0000": 1, "x\\\\u0000": 2, "path": "C:\\\\dir"}

    async def test_a_turns_failure_with_a_nul_is_stored_escaped(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        async with database.unit_of_work() as uow:
            turn = await seed.turn(uow, run.id, number=1)
            failed = await uow.turns.update_state(
                turn.id,
                TurnState.MODEL_FAILED,
                failure_code="provider_error",
                failure_detail="the model call failed: rejected (HTTP 400 bad\x00type)",
            )
        assert failed.failure_detail == "the model call failed: rejected (HTTP 400 bad\\u0000type)"
        assert failed.failure_code == "provider_error"

    @staticmethod
    def _decision(
        turn: Any, raw: Any, *, stop_reason: str | None = None, feedback: str | None = None
    ) -> NewDecision:
        return NewDecision(
            turn_id=turn.id,
            run_id=turn.run_id,
            party=Party.BUYER,
            attempt=1,
            policy=PolicyKind.MODEL,
            raw_response=raw,
            validation_ok=False,
            validation_code="schema_error",
            validation_feedback=feedback,
            stop_reason=stop_reason,
            requested_at=now(),
        )


class TestSignedActions:
    async def test_found_by_digest_listed_by_sequence_and_status_moves(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        async with database.unit_of_work() as uow:
            second_turn = await seed.turn(uow, run.id, number=2, party=Party.SELLER)
            second = await seed.signed_action(
                uow, second_turn, await seed.decision(uow, second_turn)
            )
            first_turn = await seed.turn(uow, run.id, number=1)
            first = await seed.signed_action(uow, first_turn, await seed.decision(uow, first_turn))
            await uow.signed_actions.update_status(first.id, ActionStatus.CONFIRMED)
            listed = await uow.signed_actions.list_for_run(run.id)
            # Digests are stored lowercase; a lookup in upper case finds the same row.
            found = await uow.signed_actions.get_by_digest(Digest("0x" + second.digest[2:].upper()))
            assert await uow.signed_actions.get(first.id) is not None
            assert await uow.signed_actions.get_by_digest(fresh_digest()) is None
            with pytest.raises(NotFoundError):
                await uow.signed_actions.update_status(uuid.uuid4(), ActionStatus.REVERTED)
        assert [action.sequence for action in listed] == [1, 2]
        assert listed[0].status is ActionStatus.CONFIRMED
        assert found is not None and found.id == second.id


class TestOutbox:
    async def test_the_lifecycle_of_one_transaction(self, database: Database, seed: Seeder) -> None:
        run = await seed.run()
        action, tx = await seed.action_with_tx(run.id)
        first_seen = now()
        block_hash = fresh_digest()
        async with database.unit_of_work() as uow:
            assert tx.status is TxStatus.PENDING and tx.attempts == 0
            submitted = await uow.outbox.mark_submitted(tx.id, first_seen)
            resubmitted = await uow.outbox.mark_submitted(tx.id, first_seen + timedelta(seconds=9))
            await uow.outbox.record_attempt(tx.id, error="connection reset")
            included = await uow.outbox.mark_included(
                tx.id,
                Inclusion(
                    block_number=12,
                    block_hash=block_hash,
                    gas_used=MinorAmount(91_000),
                    effective_gas_price_wei=MinorAmount(1_000_000_007),
                    included_at=now(),
                ),
            )
            assert [row.id for row in await uow.outbox.unfinished(run.id)] == [tx.id]
            reorged = await uow.outbox.clear_inclusion(tx.id)
            confirmed = await uow.outbox.update_status(tx.id, TxStatus.CONFIRMED)
            assert await uow.outbox.unfinished(run.id) == []
            by_hash = await uow.outbox.get_by_hash(tx.tx_hash)
            live = await uow.outbox.live_for_signed_action(action.id)
        assert submitted.status is TxStatus.SUBMITTED and submitted.attempts == 1
        # The first submission time is kept; a rebroadcast does not rewrite it.
        assert resubmitted.submitted_at == first_seen and resubmitted.attempts == 2
        assert included.block_hash == block_hash and included.gas_used == 91_000
        assert included.last_error == "connection reset"
        assert reorged.status is TxStatus.SUBMITTED and reorged.block_number is None
        assert confirmed.status is TxStatus.CONFIRMED
        assert by_hash is not None and by_hash.raw_tx == tx.raw_tx
        assert live is not None and live.id == tx.id

    async def test_a_reverted_transaction_is_no_longer_live_for_its_action(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        action, tx = await seed.action_with_tx(run.id)
        async with database.unit_of_work() as uow:
            reverted = await uow.outbox.update_status(
                tx.id, TxStatus.REVERTED, last_error="OfferExpired"
            )
            assert await uow.outbox.live_for_signed_action(action.id) is None
        assert reverted.last_error == "OfferExpired"

    async def test_nonces_per_sender_and_listing(self, database: Database, seed: Seeder) -> None:
        run = await seed.run()
        sender = fresh_address()
        async with database.unit_of_work() as uow:
            deployment = run.run.deployment_id
            assert await uow.outbox.max_nonce(sender, deployment) is None
            for nonce in (0, 2, 1):
                await seed.outbox_tx(uow, run.id, sender=sender, nonce=nonce)
            await seed.outbox_tx(uow, run.id, sender=fresh_address(), nonce=40)
            assert await uow.outbox.max_nonce(sender, deployment) == 2
            # ADR-081: another deployment's history does not count.
            assert await uow.outbox.max_nonce(sender, "elsewhere") is None
            assert len(await uow.outbox.list_for_run(run.id)) == 4
            assert await uow.outbox.get(uuid.uuid4()) is None
            assert await uow.outbox.get_by_hash(fresh_digest()) is None
            with pytest.raises(NotFoundError):
                await uow.outbox.update_status(uuid.uuid4(), TxStatus.DROPPED)


def _event(
    run_id: uuid.UUID,
    exchange: Address,
    block: int,
    *,
    log_index: int = 0,
    session_id: SessionId | None = None,
    tx_hash: Digest | None = None,
    block_hash: Digest | None = None,
) -> NewChainEvent:
    return NewChainEvent(
        run_id=run_id,
        chain_id=31337,
        contract_address=exchange,
        session_id=session_id,
        block_number=block,
        block_hash=block_hash or fresh_digest(),
        tx_hash=tx_hash or fresh_digest(),
        log_index=log_index,
        event_name="OfferRecorded",
        decoded={"sequence": block},
        sentence=f"event at {block}",
    )


class TestChainEventsAreCanonicalOnly:
    """Data model invariant 6: a projection never sees a non-canonical row."""

    async def test_invalidated_events_vanish_from_every_projection_read(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        exchange = run.deployment.exchange_address
        session = SessionId(fresh_digest())
        async with database.unit_of_work() as uow:
            for block in (3, 5, 7):
                await uow.chain_events.add(_event(run.id, exchange, block, session_id=session))
            removed = await uow.chain_events.invalidate_from_block(31337, exchange, 5, at=now())
            by_run = await uow.chain_events.canonical_for_run(run.id)
            by_session = await uow.chain_events.canonical_for_session(session)
            from_block = await uow.chain_events.canonical_from_block(31337, exchange, 0)
            history = await uow.chain_events.history_for_export(run.id)
        assert [event.block_number for event in removed] == [5, 7]
        assert all(event.invalidated_at is not None for event in removed)
        for projection in (by_run, by_session, from_block):
            assert [event.block_number for event in projection] == [3]
        # The export shows the reorg rather than hiding it.
        assert [(event.block_number, event.canonical) for event in history] == [
            (3, True),
            (5, False),
            (7, False),
        ]

    async def test_the_same_log_in_a_new_block_is_a_new_row(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        exchange = run.deployment.exchange_address
        tx_hash, original_block = fresh_digest(), fresh_digest()
        async with database.unit_of_work() as uow:
            first = await uow.chain_events.add(
                _event(run.id, exchange, 9, tx_hash=tx_hash, block_hash=original_block)
            )
            again = await uow.chain_events.add(
                _event(run.id, exchange, 9, tx_hash=tx_hash, block_hash=original_block)
            )
            await uow.chain_events.invalidate_from_block(31337, exchange, 9, at=now())
            reindexed = await uow.chain_events.add(_event(run.id, exchange, 9, tx_hash=tx_hash))
            canonical = await uow.chain_events.canonical_for_run(run.id)
        assert first is not None and again is None, "the same log in the same block is a no-op"
        assert reindexed is not None and reindexed.id != first.id
        assert [event.id for event in canonical] == [reindexed.id]

    async def test_events_come_back_in_chain_order_with_confirmations(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        exchange = run.deployment.exchange_address
        async with database.unit_of_work() as uow:
            later = await uow.chain_events.add(_event(run.id, exchange, 8, log_index=0))
            await uow.chain_events.add(_event(run.id, exchange, 4, log_index=2))
            await uow.chain_events.add(_event(run.id, exchange, 4, log_index=1))
            assert later is not None
            await uow.chain_events.set_confirmations(later.id, 3)
            ordered = await uow.chain_events.canonical_for_run(run.id)
            with pytest.raises(NotFoundError):
                await uow.chain_events.set_confirmations(uuid.uuid4(), 1)
        assert [(e.block_number, e.log_index) for e in ordered] == [(4, 1), (4, 2), (8, 0)]
        assert ordered[-1].confirmations_at_index == 3
        assert ordered[-1].sentence == "event at 8"


class TestBalanceSnapshots:
    async def test_snapshots_are_canonical_only_except_for_the_export(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()

        def snapshot(stage: SnapshotStage, block: int, block_hash: Digest) -> NewBalanceSnapshot:
            return NewBalanceSnapshot(
                run_id=run.id,
                stage=stage,
                party=Party.BUYER,
                token=TokenRole.QUOTE,
                amount_minor=MinorAmount(250_000_000),
                block_number=block,
                block_hash=block_hash,
            )

        setup_hash = fresh_digest()
        async with database.unit_of_work() as uow:
            assert await uow.balances.add(snapshot(SnapshotStage.POST_SETUP, 4, setup_hash))
            assert await uow.balances.add(snapshot(SnapshotStage.POST_SETUP, 4, setup_hash)) is None
            await uow.balances.add(snapshot(SnapshotStage.PRE_SETTLEMENT, 9, fresh_digest()))
            removed = await uow.balances.invalidate_from_block(run.id, 9)
            canonical = await uow.balances.canonical_for_run(run.id)
            history = await uow.balances.history_for_export(run.id)
        assert [s.stage for s in removed] == [SnapshotStage.PRE_SETTLEMENT]
        assert [s.stage for s in canonical] == [SnapshotStage.POST_SETUP]
        assert canonical[0].amount_minor == 250_000_000
        assert [(s.stage, s.canonical) for s in history] == [
            (SnapshotStage.POST_SETUP, True),
            (SnapshotStage.PRE_SETTLEMENT, False),
        ]


class TestMetricsEventsAndOperations:
    async def test_metrics_upsert(self, database: Database, seed: Seeder) -> None:
        run = await seed.run()
        async with database.unit_of_work() as uow:
            assert await uow.metrics.get(run.id) is None
            await uow.metrics.upsert(RunMetricsRecord(run_id=run.id, computed_at=now()))
            await uow.metrics.upsert(
                RunMetricsRecord(
                    run_id=run.id,
                    computed_at=now(),
                    recorded_offers=5,
                    settled_quote_minor=MinorAmount(93_333_333),
                    buyer_utility_minor=-1,
                    failure_class="none",
                    audit_complete=True,
                )
            )
            stored = await uow.metrics.get(run.id)
        assert stored is not None
        assert stored.recorded_offers == 5
        assert stored.settled_quote_minor == 93_333_333
        # A utility is signed: a mandate violation makes it negative.
        assert stored.buyer_utility_minor == -1
        assert stored.gas_used_setup == 0 and type(stored.gas_used_setup) is MinorAmount

    async def test_rpc_counts_accumulate_and_a_recomputation_never_overwrites_them(
        self, database: Database, seed: Seeder
    ) -> None:
        """ADR-061: only `add_rpc_requests` writes the counts, so a recomputation that read them a
        moment before more were added cannot write the smaller figure back."""
        run = await seed.run()
        async with database.unit_of_work() as uow:
            await uow.metrics.add_rpc_requests(run.id, {"eth_getLogs": 2}, now())
            await uow.metrics.add_rpc_requests(run.id, {"eth_getLogs": 1, "eth_call": 4}, now())
            await uow.metrics.upsert(
                RunMetricsRecord(run_id=run.id, computed_at=now(), recorded_offers=3)
            )
            stored = await uow.metrics.get(run.id)
        assert stored is not None
        assert stored.recorded_offers == 3
        assert stored.rpc_requests == 7
        assert stored.rpc_requests_by_method == {"eth_getLogs": 3, "eth_call": 4}

    async def test_the_last_cursor_overall_and_before_a_moment(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        async with database.unit_of_work() as uow:
            assert await uow.run_events.last_cursor(run.id) == 0
            first = await uow.run_events.append(run.id, "run.state", {})
        async with database.unit_of_work() as uow:
            await uow.run_events.append(run.id, "run.state", {})
            assert await uow.run_events.last_cursor(run.id) == 2
            assert await uow.run_events.last_cursor(run.id, first.created_at) == 0
            later = first.created_at + timedelta(microseconds=1)
            assert await uow.run_events.last_cursor(run.id, later) == 1

    async def test_runs_page_newest_first_with_filters(
        self, database: Database, seed: Seeder
    ) -> None:
        runs = [await seed.run(name=f"run {n}") for n in range(3)]
        async with database.unit_of_work() as uow:
            page = await uow.runs.list_page(limit=2)
            rest = await uow.runs.list_page(limit=2, before=(page[-1].created_at, page[-1].id))
            drafts = await uow.runs.list_page(limit=10, state=RunState.DRAFT)
            settled = await uow.runs.list_page(limit=10, outcome=OutcomeKind.SETTLED)
        assert [r.id for r in page + rest] == [r.id for r in reversed(runs)]
        assert len(drafts) == 3 and settled == []

    async def test_an_operations_key_can_be_released_and_taken_again(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        key = str(uuid.uuid4())
        new = NewOperation(
            kind="start_run",
            route="POST /x",
            request_hash="0x1",
            run_id=run.id,
            idempotency_key=key,
        )
        async with database.unit_of_work() as uow:
            first = await uow.operations.create(new)
            assert [op.id for op in await uow.operations.unfinished()] == [first.id]
            released = await uow.operations.release_key(first.id)
            again = await uow.operations.create(new)
            await uow.operations.update(again.id, OperationStatus.SUCCEEDED, result={})
            unfinished = await uow.operations.unfinished()
            found = await uow.operations.get_by_key("POST /x", key)
        assert released.idempotency_key is None
        assert found is not None and found.id == again.id
        assert [op.id for op in unfinished] == [first.id]

    async def test_run_event_cursors_are_per_run_and_gapless(
        self, database: Database, seed: Seeder
    ) -> None:
        first, second = await seed.run(), await seed.run()
        async with database.unit_of_work() as uow:
            cursors = [
                (await uow.run_events.append(first.id, "run.state", {"n": n})).cursor
                for n in range(3)
            ]
            other = await uow.run_events.append(second.id, "run.state", {"n": 0})
            replayed = await uow.run_events.after(first.id, cursor=1)
            with pytest.raises(NotFoundError):
                await uow.run_events.append(uuid.uuid4(), "run.state", {})
        assert cursors == [1, 2, 3]
        assert other.cursor == 1
        assert [(event.cursor, event.data) for event in replayed] == [(2, {"n": 1}), (3, {"n": 2})]

    async def test_concurrent_appends_never_share_a_cursor(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()

        async def append(n: int) -> int:
            async with database.unit_of_work() as uow:
                return (await uow.run_events.append(run.id, "notice", {"n": n})).cursor

        cursors = await asyncio.gather(*(append(n) for n in range(10)))
        assert sorted(cursors) == list(range(1, 11))

    async def test_operations_are_idempotent_per_route_and_key(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        new = NewOperation(
            kind="start_run",
            route="POST /v1/runs/{run_id}/start",
            request_hash="0xabc",
            run_id=run.id,
            idempotency_key="key-1",
        )
        async with database.unit_of_work() as uow:
            created = await uow.operations.create(new)
            with pytest.raises(DuplicateError) as refused:
                await uow.operations.create(new)
            # The same key on another route is another operation, and keyless ones never collide.
            await uow.operations.create(replace(new, route="POST /v1/runs/{run_id}/step"))
            await uow.operations.create(replace(new, idempotency_key=None))
            await uow.operations.create(replace(new, idempotency_key=None))
            found = await uow.operations.get_by_key(new.route, "key-1")
            done = await uow.operations.update(
                created.id, OperationStatus.SUCCEEDED, result={"state": "running"}
            )
            assert await uow.operations.get(created.id) is not None
            assert await uow.operations.get_by_key(new.route, "key-2") is None
            with pytest.raises(NotFoundError):
                await uow.operations.update(uuid.uuid4(), OperationStatus.FAILED)
        assert refused.value.constraint == "uq_operations_route_idempotency_key"
        assert found is not None and found.id == created.id
        assert created.status is OperationStatus.PENDING
        assert done.status is OperationStatus.SUCCEEDED and done.result == {"state": "running"}
