"""The database itself refuses what must never be recorded (docs/data_model.md sections 1, 3, 8).

Stage 2.1's exit condition in one file. Every test here attempts something the schema exists to
refuse — a duplicate action, a second live transaction for one action, a reused wallet address, an
outcome with no terminal transaction, an edit to an immutable row — and asserts the refusal comes
from the database, named by its constraint. Where a constraint also has a legitimate neighbour (a
gas replacement, a status change, a reorg invalidation), the neighbour is shown to be allowed,
because a constraint that also refused the legal case would be as broken as one that refused
nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import replace

import pytest
from api_seed import Seeder, fresh_address, fresh_digest, now
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from api.db import (
    ActionStatus,
    CheckViolationError,
    Database,
    DuplicateError,
    ImmutableRowError,
    NewChainEvent,
    NewOutboxTx,
    Outcome,
    OutcomeKind,
    Party,
    PartyOrOperator,
    RunState,
    TxStatus,
)
from negotiation_protocol import UINT256_MAX, Address, MinorAmount


@pytest.fixture
def seed(database: Database) -> Seeder:
    return Seeder(database)


# ---------------------------------------------------------------------------------------------
# Duplicate execution is a database error (principle 4)
# ---------------------------------------------------------------------------------------------


class TestSignedActionUniqueness:
    async def test_a_second_action_at_the_same_sequence_is_refused(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        async with database.unit_of_work() as uow:
            first_turn = await seed.turn(uow, run.id, number=1)
            await seed.signed_action(uow, first_turn, await seed.decision(uow, first_turn))
            second_turn = await seed.turn(uow, run.id, number=2)
            with pytest.raises(DuplicateError) as refused:
                await seed.signed_action(
                    uow, second_turn, await seed.decision(uow, second_turn), sequence=1
                )
        assert refused.value.constraint == "uq_signed_actions_run_id_sequence"

    async def test_the_same_digest_is_refused_even_under_another_run(
        self, database: Database, seed: Seeder
    ) -> None:
        digest = fresh_digest()
        first, second = await seed.run(), await seed.run()
        async with database.unit_of_work() as uow:
            turn = await seed.turn(uow, first.id)
            await seed.signed_action(uow, turn, await seed.decision(uow, turn), digest=digest)
            other = await seed.turn(uow, second.id)
            with pytest.raises(DuplicateError) as refused:
                await seed.signed_action(uow, other, await seed.decision(uow, other), digest=digest)
        assert refused.value.constraint == "uq_signed_actions_digest"

    async def test_one_decision_authorises_at_most_one_action(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        async with database.unit_of_work() as uow:
            turn = await seed.turn(uow, run.id)
            decision = await seed.decision(uow, turn)
            await seed.signed_action(uow, turn, decision)
            with pytest.raises(DuplicateError) as refused:
                await seed.signed_action(uow, turn, decision, sequence=2)
        # The turn constraint and the decision constraint both describe this row; the database
        # reports whichever it checks first, and either is the refusal this test is about.
        assert refused.value.constraint in {
            "uq_signed_actions_decision_id",
            "uq_signed_actions_turn_id",
        }


class TestOutboxUniqueness:
    async def test_the_same_transaction_hash_is_refused(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        _, tx = await seed.action_with_tx(run.id)
        resubmitted = NewOutboxTx(
            run_id=tx.run_id,
            kind=tx.kind,
            sender=tx.sender,
            nonce=tx.nonce,
            raw_tx=tx.raw_tx,
            tx_hash=tx.tx_hash,
        )
        async with database.unit_of_work() as uow:
            with pytest.raises(DuplicateError) as refused:
                await uow.outbox.add(resubmitted)
        assert refused.value.constraint in {
            "uq_tx_outbox_tx_hash",
            "uq_tx_outbox_sender_nonce_tx_hash",
        }

    async def test_a_second_live_transaction_for_one_action_is_refused(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        action, tx = await seed.action_with_tx(run.id)
        async with database.unit_of_work() as uow:
            with pytest.raises(DuplicateError) as refused:
                await seed.outbox_tx(
                    uow, run.id, signed_action_id=action.id, sender=tx.sender, nonce=tx.nonce + 1
                )
        assert refused.value.constraint == "uq_tx_outbox_signed_action_id_live"

    async def test_a_gas_replacement_is_allowed_once_the_original_is_marked_replaced(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        action, original = await seed.action_with_tx(run.id)
        async with database.unit_of_work() as uow:
            await uow.outbox.update_status(original.id, TxStatus.REPLACED)
            replacement = await seed.outbox_tx(
                uow,
                run.id,
                signed_action_id=action.id,
                sender=original.sender,
                nonce=original.nonce,
                replaces_id=original.id,
            )
            live = await uow.outbox.live_for_signed_action(action.id)
        assert live is not None and live.id == replacement.id
        assert replacement.replaces_id == original.id

    async def test_lifecycle_transactions_have_no_signed_action_and_do_not_collide(
        self, database: Database, seed: Seeder
    ) -> None:
        # createSession, abort, mint and approve carry no signed action; the partial index is on
        # signed_action_id, and NULLs never collide, so any number of them can be live.
        run = await seed.run()
        sender = fresh_address()
        async with database.unit_of_work() as uow:
            for nonce in range(3):
                await seed.outbox_tx(uow, run.id, sender=sender, nonce=nonce)
            assert len(await uow.outbox.unfinished(run.id)) == 3


class TestWalletAddressesAreNeverReused:
    async def test_the_same_address_in_a_second_run_is_refused(
        self, database: Database, seed: Seeder
    ) -> None:
        address = fresh_address()
        first, second = await seed.run(), await seed.run()
        async with database.unit_of_work() as uow:
            await uow.wallets.add(seed.wallet(first.id, Party.BUYER, address))
            with pytest.raises(DuplicateError) as refused:
                await uow.wallets.add(seed.wallet(second.id, Party.BUYER, address))
        assert refused.value.constraint == "uq_wallets_address"

    async def test_a_raw_key_in_key_ref_is_refused(self, database: Database, seed: Seeder) -> None:
        run = await seed.run()
        pasted_key = "0x" + "ab" * 32
        wallet = replace(seed.wallet(run.id, Party.BUYER), key_ref=pasted_key)
        async with database.unit_of_work() as uow:
            with pytest.raises(CheckViolationError) as refused:
                await uow.wallets.add(wallet)
        assert refused.value.constraint == "ck_wallets_key_ref_is_a_reference"


# ---------------------------------------------------------------------------------------------
# The economic outcome can only be what the data model says it can be (section 5)
# ---------------------------------------------------------------------------------------------


class TestOutcomeConstraints:
    async def test_a_settlement_needs_a_terminal_state(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        async with database.unit_of_work() as uow:
            with pytest.raises(CheckViolationError) as refused:
                await uow.runs.record_outcome(
                    run.id,
                    Outcome(OutcomeKind.SETTLED, PartyOrOperator.SELLER, fresh_digest()),
                    terminal_at=now(),
                    state=RunState.RUNNING,
                )
        assert refused.value.constraint == "ck_runs_outcome_requires_terminal_event"

    @pytest.mark.parametrize(
        ("kind", "actor", "reason_code", "constraint"),
        [
            (OutcomeKind.CLOSED, PartyOrOperator.BUYER, 4, "ck_runs_outcome_reason_matches_kind"),
            (
                OutcomeKind.CLOSED,
                PartyOrOperator.BUYER,
                None,
                "ck_runs_outcome_reason_matches_kind",
            ),
            (OutcomeKind.SETTLED, PartyOrOperator.BUYER, 1, "ck_runs_outcome_reason_matches_kind"),
            (
                OutcomeKind.ABORTED,
                PartyOrOperator.OPERATOR,
                5,
                "ck_runs_outcome_reason_matches_kind",
            ),
            (OutcomeKind.ABORTED, PartyOrOperator.BUYER, 2, "ck_runs_outcome_actor_matches_kind"),
            (
                OutcomeKind.EXPIRED,
                PartyOrOperator.OPERATOR,
                None,
                "ck_runs_outcome_actor_matches_kind",
            ),
            (
                OutcomeKind.SETTLED,
                PartyOrOperator.ANYONE,
                None,
                "ck_runs_outcome_actor_matches_kind",
            ),
        ],
    )
    async def test_reason_codes_and_actors_follow_the_outcome(
        self,
        database: Database,
        seed: Seeder,
        kind: OutcomeKind,
        actor: PartyOrOperator,
        reason_code: int | None,
        constraint: str,
    ) -> None:
        run = await seed.run()
        async with database.unit_of_work() as uow:
            with pytest.raises(CheckViolationError) as refused:
                await uow.runs.record_outcome(
                    run.id, Outcome(kind, actor, fresh_digest(), reason_code), terminal_at=now()
                )
        assert refused.value.constraint == constraint

    @pytest.mark.parametrize(
        ("kind", "actor", "reason_code"),
        [
            (OutcomeKind.SETTLED, PartyOrOperator.BUYER, None),
            (OutcomeKind.CLOSED, PartyOrOperator.SELLER, 3),
            (OutcomeKind.EXPIRED, PartyOrOperator.ANYONE, None),
            (OutcomeKind.ABORTED, PartyOrOperator.OPERATOR, 2),
        ],
    )
    async def test_every_legitimate_outcome_is_accepted(
        self,
        database: Database,
        seed: Seeder,
        kind: OutcomeKind,
        actor: PartyOrOperator,
        reason_code: int | None,
    ) -> None:
        run = await seed.run()
        tx_hash = fresh_digest()
        async with database.unit_of_work() as uow:
            recorded = await uow.runs.record_outcome(
                run.id, Outcome(kind, actor, tx_hash, reason_code), terminal_at=now()
            )
        assert (recorded.outcome_kind, recorded.outcome_actor) == (kind, actor)
        assert recorded.outcome_reason_code == reason_code
        assert recorded.outcome_tx_hash == tx_hash
        assert recorded.state is RunState.TERMINAL

    @pytest.mark.parametrize("kind", ["settled", "closed", "expired", "aborted"])
    async def test_an_outcome_with_no_actor_is_refused(
        self, seed: Seeder, raw_sql: AsyncConnection, kind: str
    ) -> None:
        # The repository's `Outcome` cannot express a missing actor, so this goes around it. A CHECK
        # passes when its expression is NULL, and the first version of this constraint was NULL —
        # therefore satisfied — for every outcome whose actor was missing.
        run = await seed.run()
        reason = {"closed": "1", "aborted": "2"}.get(kind, "NULL")
        message = await _refused(
            raw_sql,
            f"UPDATE runs SET outcome_kind = '{kind}', outcome_reason_code = {reason}, "
            f"state = 'terminal', outcome_tx_hash = '{fresh_digest()}' WHERE id = '{run.id}'",
        )
        assert "ck_runs_outcome_actor_matches_kind" in message

    async def test_an_aborted_setup_may_carry_its_outcome_in_failed_setup(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        async with database.unit_of_work() as uow:
            recorded = await uow.runs.record_outcome(
                run.id,
                Outcome(OutcomeKind.ABORTED, PartyOrOperator.OPERATOR, fresh_digest(), 1),
                terminal_at=now(),
                state=RunState.FAILED_SETUP,
                cause="session_mismatch",
            )
        assert recorded.state is RunState.FAILED_SETUP
        assert recorded.state_cause == "session_mismatch"


class TestDecisionConstraints:
    async def test_an_invalid_decision_cannot_be_authorised(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        async with database.unit_of_work() as uow:
            turn = await seed.turn(uow, run.id)
            rejected = await seed.decision(uow, turn, validation_ok=False)
            with pytest.raises(CheckViolationError) as refused:
                await uow.decisions.mark_authorized(rejected.id)
        assert refused.value.constraint == "ck_decisions_authorized_implies_valid"


# ---------------------------------------------------------------------------------------------
# Immutability (section 8)
# ---------------------------------------------------------------------------------------------


async def _refused(connection: AsyncConnection, statement: str) -> str:
    """Run a statement the schema must refuse; return the database's message."""
    with pytest.raises(DBAPIError) as refused:
        await connection.execute(text(statement))
    return str(refused.value.orig)


class TestImmutability:
    async def test_mandate_versions_refuse_update_and_delete(
        self, database: Database, seed: Seeder, raw_sql: AsyncConnection
    ) -> None:
        run = await seed.run()
        async with database.unit_of_work() as uow:
            await seed.mandates(uow, run.id)

        message = await _refused(
            raw_sql, f"UPDATE mandate_versions SET instructions = 'x' WHERE run_id = '{run.id}'"
        )
        assert "UPDATE on mandate_versions refused" in message
        message = await _refused(raw_sql, f"DELETE FROM mandate_versions WHERE run_id = '{run.id}'")
        assert "DELETE on mandate_versions refused" in message

    async def test_run_events_are_append_only(
        self, database: Database, seed: Seeder, raw_sql: AsyncConnection
    ) -> None:
        run = await seed.run()
        async with database.unit_of_work() as uow:
            await uow.run_events.append(run.id, "run.state", {"state": "draft"})

        assert "UPDATE on run_events refused" in await _refused(
            raw_sql, f"UPDATE run_events SET data = '{{}}' WHERE run_id = '{run.id}'"
        )
        assert "DELETE on run_events refused" in await _refused(
            raw_sql, f"DELETE FROM run_events WHERE run_id = '{run.id}'"
        )

    async def test_a_signed_actions_identity_is_frozen_but_its_status_moves(
        self, database: Database, seed: Seeder, raw_sql: AsyncConnection
    ) -> None:
        run = await seed.run()
        action, _ = await seed.action_with_tx(run.id)

        for column, value in (
            ("typed_message", "'{}'::jsonb"),
            ("digest", f"'{fresh_digest()}'"),
            ("signature", "'0x00'"),
            ("sequence", "99"),
        ):
            message = await _refused(
                raw_sql, f"UPDATE signed_actions SET {column} = {value} WHERE id = '{action.id}'"
            )
            assert "only status and revert_error may change" in message, column

        async with database.unit_of_work() as uow:
            moved = await uow.signed_actions.update_status(
                action.id, ActionStatus.REVERTED, revert_error="OfferExpired"
            )
        assert moved.status is ActionStatus.REVERTED
        assert moved.revert_error == "OfferExpired"
        assert moved.typed_message == action.typed_message

    async def test_the_same_refusal_reaches_the_application_as_immutable_row_error(
        self, database: Database, seed: Seeder
    ) -> None:
        # A repository never issues this UPDATE; the point is that if code ever did, the error it
        # got would say what happened rather than surfacing as an anonymous driver exception.
        from sqlalchemy import update

        from api.db.models import SignedAction
        from api.db.repositories.turns import SqlSignedActionRepository

        run = await seed.run()
        action, _ = await seed.action_with_tx(run.id)
        async with database.unit_of_work() as uow:
            repository = uow.signed_actions
            assert isinstance(repository, SqlSignedActionRepository)
            with pytest.raises(ImmutableRowError):
                await repository._write(
                    update(SignedAction)
                    .where(SignedAction.id == action.id)
                    .values(signature="0x" + "22" * 65)
                )

    async def test_chain_event_evidence_is_frozen_but_canonicality_moves(
        self, database: Database, seed: Seeder, raw_sql: AsyncConnection
    ) -> None:
        run = await seed.run()
        async with database.unit_of_work() as uow:
            event = await uow.chain_events.add(_event(run.id, run.deployment.exchange_address, 5))
        assert event is not None

        for column, value in (
            ("decoded", "'{}'::jsonb"),
            ("block_hash", f"'{fresh_digest()}'"),
            ("block_number", "6"),
            ("log_index", "9"),
        ):
            message = await _refused(
                raw_sql, f"UPDATE chain_events SET {column} = {value} WHERE id = '{event.id}'"
            )
            assert "evidence columns are immutable" in message, column

        async with database.unit_of_work() as uow:
            invalidated = await uow.chain_events.invalidate_from_block(
                31337, run.deployment.exchange_address, 5, at=now()
            )
        assert [row.id for row in invalidated] == [event.id]
        assert invalidated[0].canonical is False

    async def test_canonical_and_invalidated_at_cannot_disagree(
        self, database: Database, seed: Seeder, raw_sql: AsyncConnection
    ) -> None:
        run = await seed.run()
        async with database.unit_of_work() as uow:
            event = await uow.chain_events.add(_event(run.id, run.deployment.exchange_address, 5))
        assert event is not None
        message = await _refused(
            raw_sql, f"UPDATE chain_events SET canonical = false WHERE id = '{event.id}'"
        )
        assert "ck_chain_events_canonical_iff_not_invalidated" in message


def _event(run_id: uuid.UUID, exchange: Address, block: int, log_index: int = 0) -> NewChainEvent:
    return NewChainEvent(
        run_id=run_id,
        chain_id=31337,
        contract_address=exchange,
        block_number=block,
        block_hash=fresh_digest(),
        tx_hash=fresh_digest(),
        log_index=log_index,
        event_name="OfferRecorded",
        decoded={"sequence": 1, "quoteAmount": "80000000"},
    )


# ---------------------------------------------------------------------------------------------
# Amounts and single-row tables
# ---------------------------------------------------------------------------------------------


class TestAmountsAndSingletons:
    async def test_the_largest_uint256_round_trips_exactly(
        self, database: Database, seed: Seeder
    ) -> None:
        run = await seed.run()
        wallet = seed.wallet(run.id, Party.SELLER)
        async with database.unit_of_work() as uow:
            await uow.wallets.add(replace(wallet, allowance_minor=MinorAmount(UINT256_MAX)))
        async with database.unit_of_work() as uow:
            stored = await uow.wallets.get(run.id, Party.SELLER)
        assert stored is not None
        assert stored.allowance_minor == UINT256_MAX
        assert type(stored.allowance_minor) is MinorAmount

    async def test_a_negative_amount_is_refused_by_the_column_not_only_the_type(
        self, database: Database, seed: Seeder, raw_sql: AsyncConnection
    ) -> None:
        run = await seed.run()
        async with database.unit_of_work() as uow:
            await seed.mandates(uow, run.id)
        message = await _refused(
            raw_sql,
            "INSERT INTO balance_snapshots (run_id, stage, party, token, amount_minor, "
            f"block_number, block_hash) VALUES ('{run.id}', 'pre_setup', 'buyer', 'quote', -1, 1, "
            f"'{fresh_digest()}')",
        )
        assert "ck_balance_snapshots_amount_minor_non_negative" in message

    async def test_a_mandate_extra_must_be_empty(
        self, database: Database, seed: Seeder, raw_sql: AsyncConnection
    ) -> None:
        run = await seed.run()
        message = await _refused(
            raw_sql,
            "INSERT INTO mandate_versions (run_id, party, version, reservation_price_minor, "
            "min_remaining_inventory_minor, instructions, extra, mandate_hash) VALUES "
            f"('{run.id}', 'buyer', 1, 1, 0, '', '{{\"smuggled\": 1}}', 'h')",
        )
        assert "ck_mandate_versions_extra_is_empty" in message

    async def test_active_run_holds_one_row(
        self, database: Database, seed: Seeder, raw_sql: AsyncConnection
    ) -> None:
        run = await seed.run()
        message = await _refused(
            raw_sql, f"INSERT INTO active_run (id, run_id) VALUES (2, '{run.id}')"
        )
        assert "ck_active_run_single_row" in message
