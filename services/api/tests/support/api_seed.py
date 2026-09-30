"""Build valid rows for integration tests, so each test states only what it is about.

Every helper writes through the real repositories, never around them: a seeder that inserted with
raw SQL could produce rows the application can never produce, and a test built on those would be
testing a database nobody uses. Values are fresh per call — addresses, digests, session ids —
because several constraints under test are uniqueness constraints and a fixed value would collide
with itself.
"""

from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from api.db import (
    ActionKind,
    Database,
    DecisionRecord,
    DeploymentRecord,
    NewDecision,
    NewMandate,
    NewOutboxTx,
    NewRun,
    NewSignedAction,
    NewTurn,
    NewWallet,
    OutboxRecord,
    Party,
    PolicyKind,
    RunMode,
    RunRecord,
    ScenarioRecord,
    SignedActionRecord,
    TurnRecord,
    TurnState,
    TxKind,
    UnitOfWork,
)
from negotiation_protocol import Address, Digest, MinorAmount

#: The default-overlap scenario's public configuration (spec section 2.3).
PUBLIC_CONFIG: dict[str, Any] = {
    "base_amount_minor": "10000000",
    "max_offers": 8,
    "session_duration_s": 1800,
    "offer_lifetime_s": 600,
    "confirmation_threshold": 1,
    "first_proposer": "buyer",
}

LIMITS: dict[str, Any] = {
    "model_call_ceiling": 20,
    "model_spend_ceiling_usd": "2.00",
    "model_timeout_s": 45,
    "repair_attempts": 1,
}


def fresh_address() -> Address:
    return Address("0x" + secrets.token_hex(20))


def fresh_digest() -> Digest:
    return Digest(secrets.token_bytes(32))


def now() -> datetime:
    return datetime.now(UTC)


def deployment_record(
    deployment_id: str = "local-test-01", chain_id: int = 31337
) -> DeploymentRecord:
    exchange = fresh_address()
    manifest = {"deployment_id": deployment_id, "chain_id": chain_id}
    return DeploymentRecord(
        deployment_id=deployment_id,
        chain_id=chain_id,
        protocol_version="1",
        exchange_address=exchange,
        base_token_address=fresh_address(),
        quote_token_address=fresh_address(),
        operator_address=fresh_address(),
        relay_address=fresh_address(),
        code_hashes={
            "exchange": fresh_digest(),
            "base_token": fresh_digest(),
            "quote_token": fresh_digest(),
        },
        compiler={
            "solc": "0.8.28",
            "optimizer": True,
            "runs": 200,
            "evm_version": "cancun",
            "bytecode_hash": "none",
        },
        explorer_base_url=None,
        ens=None,
        manifest=manifest,
        start_block=0,
        deployed_at=now(),
    )


def scenario_record(scenario_id: str = "default-overlap") -> ScenarioRecord:
    return ScenarioRecord(
        scenario_id=scenario_id,
        name="Default overlap",
        description="Test scenario.",
        public_config=dict(PUBLIC_CONFIG),
        buyer_template={"mandate": {"reservation_price_minor": "100000000"}},
        seller_template={"mandate": {"reservation_price_minor": "90000000"}},
        source_hash="0x" + "00" * 32,
    )


@dataclass
class SeededRun:
    """A run and the rows every other table hangs off."""

    run: RunRecord
    deployment: DeploymentRecord

    @property
    def id(self) -> uuid.UUID:
        return self.run.id


class Seeder:
    def __init__(self, database: Database) -> None:
        self.database = database

    async def run(self, *, deployment_id: str | None = None, name: str = "test run") -> SeededRun:
        deployment = deployment_record(deployment_id or f"local-test-{secrets.token_hex(3)}")
        async with self.database.unit_of_work() as uow:
            await uow.deployments.upsert(deployment)
            await uow.scenarios.upsert(scenario_record())
            run = await uow.runs.add(
                NewRun(
                    name=name,
                    scenario_id="default-overlap",
                    deployment_id=deployment.deployment_id,
                    public_config=dict(PUBLIC_CONFIG),
                    limits=dict(LIMITS),
                    buyer_policy=PolicyKind.DETERMINISTIC,
                    seller_policy=PolicyKind.DETERMINISTIC,
                    software_version="0.1.0+test",
                    mode=RunMode.FIXTURE,
                )
            )
        return SeededRun(run=run, deployment=deployment)

    @staticmethod
    async def mandates(uow: UnitOfWork, run_id: uuid.UUID) -> None:
        for party, reservation, floor in (
            (Party.BUYER, 100_000_000, 0),
            (Party.SELLER, 90_000_000, 10_000_000),
        ):
            await uow.mandates.add(
                NewMandate(
                    run_id=run_id,
                    party=party,
                    version=1,
                    reservation_price_minor=MinorAmount(reservation),
                    min_remaining_inventory_minor=MinorAmount(floor),
                    instructions=f"{party} instructions",
                )
            )

    @staticmethod
    def wallet(run_id: uuid.UUID, party: Party, address: Address | None = None) -> NewWallet:
        return NewWallet(
            run_id=run_id,
            party=party,
            address=address or fresh_address(),
            key_ref=f"env:{party.upper()}_ROOT_KEY",
            key_derivation={
                "scheme": "agent-negotiation-sandbox/participant-key/v1",
                "chain_id": 31337,
                "role": str(party),
                "run_id": str(run_id),
            },
            initial_base_minor=MinorAmount(0 if party is Party.BUYER else 25_000_000),
            initial_quote_minor=MinorAmount(250_000_000 if party is Party.BUYER else 0),
            allowance_minor=MinorAmount(250_000_000 if party is Party.BUYER else 25_000_000),
        )

    @staticmethod
    async def turn(
        uow: UnitOfWork, run_id: uuid.UUID, number: int = 1, party: Party = Party.BUYER
    ) -> TurnRecord:
        observation = {"schema_version": "1", "role": str(party), "turn": number}
        return await uow.turns.add(
            NewTurn(
                run_id=run_id,
                turn=number,
                party=party,
                expected_sequence=number,
                state=TurnState.DECIDING,
                observation=observation,
                observation_hash="0x" + secrets.token_hex(32),
            )
        )

    @staticmethod
    async def decision(
        uow: UnitOfWork,
        turn: TurnRecord,
        *,
        attempt: int = 1,
        validation_ok: bool = True,
        authorized: bool = False,
    ) -> DecisionRecord:
        return await uow.decisions.add(
            NewDecision(
                turn_id=turn.id,
                run_id=turn.run_id,
                party=turn.party,
                attempt=attempt,
                policy=PolicyKind.DETERMINISTIC,
                raw_response={"decision": {"action": "offer", "quote_amount_minor": "80000000"}},
                validation_ok=validation_ok,
                validation_code=None if validation_ok else "above_reservation",
                validation_feedback=None if validation_ok else "Your quote is above your limit.",
                requested_at=now(),
                latency_ms=3,
                authorized=authorized,
            )
        )

    @staticmethod
    async def signed_action(
        uow: UnitOfWork,
        turn: TurnRecord,
        decision: DecisionRecord,
        *,
        sequence: int | None = None,
        digest: Digest | None = None,
    ) -> SignedActionRecord:
        return await uow.signed_actions.add(
            NewSignedAction(
                run_id=turn.run_id,
                turn_id=turn.id,
                decision_id=decision.id,
                sequence=turn.expected_sequence if sequence is None else sequence,
                kind=ActionKind.OFFER,
                typed_message={
                    "sessionId": fresh_digest(),
                    "sequence": turn.expected_sequence,
                    "quoteAmount": "80000000",
                },
                digest=digest or fresh_digest(),
                signer=fresh_address(),
                signature="0x" + "11" * 65,
            )
        )

    @staticmethod
    async def outbox_tx(
        uow: UnitOfWork,
        run_id: uuid.UUID,
        *,
        signed_action_id: uuid.UUID | None = None,
        sender: Address | None = None,
        nonce: int = 0,
        kind: TxKind = TxKind.RECORD_OFFER,
        replaces_id: uuid.UUID | None = None,
    ) -> OutboxRecord:
        return await uow.outbox.add(
            NewOutboxTx(
                run_id=run_id,
                kind=kind,
                sender=sender or fresh_address(),
                nonce=nonce,
                raw_tx=secrets.token_bytes(120),
                tx_hash=fresh_digest(),
                signed_action_id=signed_action_id,
                replaces_id=replaces_id,
            )
        )

    async def action_with_tx(self, run_id: uuid.UUID) -> tuple[SignedActionRecord, OutboxRecord]:
        """One turn, its authorised decision, the signed action, and the transaction carrying it."""
        async with self.database.unit_of_work() as uow:
            turn = await self.turn(uow, run_id)
            decision = await self.decision(uow, turn, authorized=True)
            action = await self.signed_action(uow, turn, decision)
            tx = await self.outbox_tx(uow, run_id, signed_action_id=action.id)
        return action, tx
