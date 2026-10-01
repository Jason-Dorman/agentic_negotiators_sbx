"""The turn executor: decide, validate, repair at most as provisioned, sign (ADR-012).

The first test is the one docs/test_strategy.md section 6 asks for by name: every row of the refusal
table, run through the executor with a policy that insists on it, signs nothing — and ends as a
recorded `model_failed` with `repair_exhausted`, never as a walk-away.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from agent_fakes import FIXTURE_KEYS, KeyedRunSigner, ScriptedPolicy
from agent_observations import FIXTURE, alternating_offers, observation, typed
from agent_validation_cases import ALL, Refusal, offer, walk_away

from agent.keys import KeyDerivation
from agent.observation import Role
from agent.policy import DeterministicPolicy
from agent.signing import ApprovedSession
from agent.turns import TurnExecutor, iso_utc
from agent.validation import MandateValidator
from negotiation_protocol import Address, Digest, MinorAmount, SessionId, recover_signer

RUN = UUID("6f1c2a4e-9d7b-4c3a-8e2f-1a2b3c4d5e6f")
START = datetime(2026, 9, 30, 12, 0, 0, 123000, tzinfo=UTC)


class SteppingClock:
    """Each `now` is 250 ms after the last; `monotonic` advances 40 ms per read."""

    def __init__(self) -> None:
        self._now = START
        self._mono = 0.0

    def now(self) -> datetime:
        moment = self._now
        self._now += timedelta(milliseconds=250)
        return moment

    def monotonic(self) -> float:
        self._mono += 0.040
        return self._mono


def approval() -> ApprovedSession:
    config = FIXTURE["session_config"]
    return ApprovedSession(
        chain_id=FIXTURE["domain"]["chain_id"],
        exchange_address=Address(FIXTURE["domain"]["verifying_contract"]),
        base_token=Address(FIXTURE["deployment"]["base_token"]),
        quote_token=Address(FIXTURE["deployment"]["quote_token"]),
        session_id=SessionId(config["session_id"]),
        config_hash=Digest(FIXTURE["config_hash"]),
        buyer=Address(config["buyer"]),
        seller=Address(config["seller"]),
        base_amount=MinorAmount(int(config["base_amount"])),
        expires_at=int(config["expires_at"]),
        max_offers=int(config["max_offers"]),
        offer_lifetime_s=600,
    )


def signer(role: Role) -> KeyedRunSigner:
    return KeyedRunSigner(FIXTURE_KEYS[role], KeyDerivation(chain_id=31337, role=role, run_id=RUN))


async def run(
    document: dict[str, Any],
    policy: Any,
    *,
    attempts: int = 2,
    key: KeyedRunSigner | None = None,
) -> Any:
    executor = TurnExecutor(MandateValidator(), SteppingClock())
    return await executor.run(
        turn=3,
        observation=typed(document),
        observation_hash="0x" + "ab" * 32,
        policy=policy,
        attempts=attempts,
        approval=approval(),
        signer=key or signer(document["role"]),
        still_open=lambda: True,
    )


@pytest.mark.parametrize("case", ALL, ids=lambda case: case.name)
async def test_a_refused_decision_signs_nothing(case: Refusal) -> None:
    key = signer(case.build()["role"])
    outcome = await run(case.build(), ScriptedPolicy(case.response), key=key)
    assert key.signatures_made == 0
    assert outcome.signed_action is None
    assert outcome.status == "model_failed"
    assert outcome.failure == {
        "code": "repair_exhausted",
        "detail": "none of 2 attempt(s) passed validation",
    }
    assert [record.validation.code for record in outcome.decisions] == [case.code, case.code]
    assert outcome.decisions[0].validation.feedback == case.feedback


async def test_the_failure_detail_names_no_private_code_or_feedback() -> None:
    """`failure` is not private; the codes and feedback live in the decision records, which are."""
    outcome = await run(observation("buyer"), ScriptedPolicy(offer("100000001")))
    assert "above_reservation" not in str(outcome.failure)
    assert "100000000" not in str(outcome.failure)


async def test_a_repair_carries_only_this_agents_own_feedback() -> None:
    policy = ScriptedPolicy(offer("100000001"), offer("99000000"))
    outcome = await run(observation("buyer"), policy)
    assert outcome.status == "signed"
    first, second = policy.calls
    assert first[1] is None
    assert second[1] is not None
    assert second[1].code == "above_reservation"
    assert second[1].feedback == outcome.decisions[0].validation.feedback
    assert [record.attempt for record in outcome.decisions] == [1, 2]
    assert [record.validation.ok for record in outcome.decisions] == [False, True]


async def test_the_repaired_price_is_signed_as_proposed_and_never_clamped() -> None:
    """A refused 100000001 is not made 100000000; the policy's own second answer is signed."""
    outcome = await run(observation("buyer"), ScriptedPolicy(offer("100000001"), offer("97500000")))
    assert outcome.signed_action.typed_message["quoteAmount"] == "97500000"


async def test_with_no_repair_provisioned_one_refusal_ends_the_turn() -> None:
    policy = ScriptedPolicy(offer("100000001"))
    outcome = await run(observation("buyer"), policy, attempts=1)
    assert outcome.status == "model_failed"
    assert len(policy.calls) == 1
    assert outcome.failure["detail"] == "none of 1 attempt(s) passed validation"


async def test_a_turn_needs_at_least_one_attempt() -> None:
    with pytest.raises(ValueError, match="at least one attempt"):
        await run(observation("buyer"), ScriptedPolicy(offer("1")), attempts=0)


async def test_a_signed_turn_binds_the_observations_sequence_and_the_approved_session() -> None:
    document = observation(
        "seller", history=alternating_offers(80_000_000, 108_000_000, 95_000_000)
    )
    key = signer("seller")
    outcome = await run(document, DeterministicPolicy(MandateValidator()), key=key)
    action = outcome.signed_action
    assert outcome.status == "signed" and outcome.failure is None
    assert action.kind == "accept"
    assert action.typed_message["sequence"] == document["expected_sequence"] == 4
    assert action.typed_message["sessionId"] == FIXTURE["session_config"]["session_id"]
    assert action.typed_message["configHash"] == FIXTURE["config_hash"]
    assert recover_signer(Digest(action.digest).to_bytes(), action.signature) == key.address
    assert key.signatures_made == 1


async def test_a_walk_away_is_signed_as_a_close_with_its_code() -> None:
    outcome = await run(observation("seller"), ScriptedPolicy(walk_away("no_further_concession")))
    assert outcome.signed_action.kind == "close"
    assert outcome.signed_action.typed_message["reason"] == 3


async def test_each_decision_record_carries_its_accounting_and_timing() -> None:
    policy = ScriptedPolicy(offer("100000001"), offer("90000000"))
    outcome = await run(observation("buyer"), policy)
    records = [record.to_json() for record in outcome.decisions]
    assert records[0] == {
        "attempt": 1,
        "raw_response": offer("100000001"),
        "validation": {
            "ok": False,
            "code": "above_reservation",
            "feedback": outcome.decisions[0].validation.feedback,
        },
        "stop_reason": None,
        "usage": None,
        "latency_ms": 40,
        "cost_estimated_usd": None,
        "cost_reported_usd": None,
        "prompt_template_version": None,
        "observation_hash": "0x" + "ab" * 32,
        "requested_at": "2026-09-30T12:00:00.123Z",
    }
    assert records[1]["validation"] == {"ok": True, "code": None, "feedback": None}
    assert records[1]["requested_at"] == "2026-09-30T12:00:00.373Z"


async def test_the_outcome_serialises_as_the_turn_response() -> None:
    outcome = await run(observation("buyer"), ScriptedPolicy(offer("80000000")))
    body = outcome.to_json()
    assert set(body) == {"turn", "status", "signed_action", "decisions", "failure"}
    assert body["turn"] == 3
    assert body["signed_action"]["kind"] == "offer"
    assert body["failure"] is None
    failed = (await run(observation("buyer"), ScriptedPolicy("nonsense"))).to_json()
    assert failed["signed_action"] is None
    assert failed["failure"]["code"] == "repair_exhausted"


def test_timestamps_are_utc_with_a_z() -> None:
    offset = datetime(2026, 9, 30, 14, 0, 0, tzinfo=UTC).astimezone()
    assert iso_utc(offset) == "2026-09-30T14:00:00.000Z"
