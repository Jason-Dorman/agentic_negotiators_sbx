"""What the service refuses to ask a policy, which the HTTP-level tests cannot see.

Two of the service's checks exist to keep a policy from being called at all: a turn whose session
deadline has passed, and a turn already answered. With the deterministic policy, removing either
leaves every response unchanged — the signer refuses the late turn with the same error, and the
baseline answers a repeated observation identically — so the HTTP tests stay green. With a model
policy it is a paid call for an action nothing can sign, or a second, different decision for a
retried turn. A policy that counts its calls is what makes both visible.
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any
from uuid import UUID

import pytest
from agent_fakes import KeyedKeyHolder, ScriptedPolicy
from agent_observations import FIXTURE, MANDATES, observation

from agent.errors import InvalidStateError, ObservationInconsistentError, SessionMismatchError
from agent.policy import Policy
from agent.service import AgentService
from agent.signing import ExpectedSession, SessionOpened, SetupBounds
from agent.state import Provisioning, RunRegistry
from agent.turns import SystemClock, TurnExecutor
from agent.validation import MandateValidator
from negotiation_protocol import Address, Digest, MinorAmount, SessionId

RUN = UUID("6f1c2a4e-9d7b-4c3a-8e2f-1a2b3c4d5e6f")
CONFIG = FIXTURE["session_config"]
EXPIRES_AT = int(CONFIG["expires_at"])


def service(policy: ScriptedPolicy) -> AgentService:
    def factory(request: Provisioning) -> Policy:
        return policy

    return AgentService(
        role="buyer",
        instance="agent-a",
        keys=KeyedKeyHolder(),
        signer_problem=None,
        policies={"deterministic": factory},
        executor=TurnExecutor(MandateValidator(), SystemClock()),
        registry=RunRegistry(),
        setup_bounds=SetupBounds(100_000, 10**16),
    )


EXPECTED = ExpectedSession(
    chain_id=31337,
    exchange_address=Address(FIXTURE["domain"]["verifying_contract"]),
    base_token=Address(FIXTURE["deployment"]["base_token"]),
    quote_token=Address(FIXTURE["deployment"]["quote_token"]),
    base_amount=MinorAmount(10_000_000),
    max_offers=8,
    session_duration_s=1800,
    offer_lifetime_s=600,
)


def provisioning() -> Provisioning:
    return Provisioning(
        role="buyer",
        policy="deterministic",
        model_id=None,
        effort=None,
        repair_attempts=1,
        model_call_ceiling=20,
        model_spend_ceiling_usd=Decimal("2.00"),
        model_timeout_s=45,
        expected=EXPECTED,
        key_ref="env:BUYER_ROOT_KEY",
        mandate_version_id=UUID(int=1),
        mandate_document=MANDATES["buyer"],
        initial_base=MinorAmount(0),
        initial_quote=MinorAmount(250_000_000),
        allowance=MinorAmount(250_000_000),
        fingerprint="0x" + "01" * 32,
    )


def opened() -> SessionOpened:
    return SessionOpened(
        session_id=SessionId(CONFIG["session_id"]),
        buyer=Address(CONFIG["buyer"]),
        seller=Address(CONFIG["seller"]),
        base_token=EXPECTED.base_token,
        quote_token=EXPECTED.quote_token,
        base_amount=EXPECTED.base_amount,
        expires_at=EXPIRES_AT,
        max_offers=8,
        config_hash=Digest(FIXTURE["config_hash"]),
        opened_at=EXPIRES_AT - 1800,
    )


def ready(policy: ScriptedPolicy) -> AgentService:
    agent = service(policy)
    agent.provision(RUN, provisioning(), None)
    agent.approve_session(RUN, opened())
    return agent


def turn_body(**changes: Any) -> dict[str, Any]:
    document = observation("buyer", **changes)
    del document["mandate"]
    return {**document, "turn": 1, "deadline_at": "2099-09-30T12:00:45.000Z"}


def offer(amount: str) -> dict[str, Any]:
    return {"decision": {"action": "offer", "quote_amount_minor": amount}}


async def test_a_turn_past_the_deadline_never_reaches_the_policy() -> None:
    policy = ScriptedPolicy(offer("80000000"))
    agent = ready(policy)
    with pytest.raises(InvalidStateError, match="deadline"):
        await agent.turn(RUN, turn_body(chain_time=EXPIRES_AT))
    assert policy.calls == []


async def test_an_observation_of_another_session_never_reaches_the_policy() -> None:
    policy = ScriptedPolicy(offer("80000000"))
    agent = ready(policy)
    body = turn_body()
    body["session"]["max_offers"] = 7
    with pytest.raises(SessionMismatchError):
        await agent.turn(RUN, body)
    assert policy.calls == []


async def test_a_turn_answered_once_is_not_decided_again() -> None:
    """The second policy answer would differ; a retried turn must get the first one."""
    policy = ScriptedPolicy(offer("80000000"), offer("81000000"))
    agent = ready(policy)
    first = await agent.turn(RUN, turn_body())
    second = await agent.turn(RUN, turn_body())
    assert len(policy.calls) == 1
    assert second == first
    assert first["signed_action"]["typed_message"]["quoteAmount"] == "80000000"


async def test_the_provisioned_repair_count_is_the_number_of_repairs() -> None:
    policy = ScriptedPolicy(offer("100000001"))
    agent = ready(policy)
    response = await agent.turn(RUN, turn_body())
    assert len(policy.calls) == 2  # the first attempt and repair_attempts = 1
    assert response["status"] == "model_failed"


class GatedPolicy(ScriptedPolicy):
    """Decides only once its gate opens, as a model call would after its network round trip."""

    def __init__(self, *responses: Any) -> None:
        super().__init__(*responses)
        self.gate = asyncio.Event()

    async def decide(self, observation: Any, repair: Any, *, time_left_s: float | None) -> Any:
        await self.gate.wait()
        return await super().decide(observation, repair, time_left_s=time_left_s)


async def test_a_run_released_while_its_turn_decides_signs_nothing() -> None:
    """ADR-048: release is the end of the run, including for a decision already under way."""
    policy = GatedPolicy(offer("80000000"))
    agent = ready(policy)
    keys = agent._keys  # the fake holder records every signature its signers make
    turn = asyncio.create_task(agent.turn(RUN, turn_body()))
    await asyncio.sleep(0)
    agent.release(RUN)
    policy.gate.set()
    with pytest.raises(InvalidStateError) as refused:
        await turn
    assert refused.value.details["state"] == "released"
    assert isinstance(keys, KeyedKeyHolder) and keys.signatures_made == 0


async def test_two_requests_for_one_turn_decide_once() -> None:
    """The turn lock: a retry that arrives while the first is still deciding waits for it."""
    policy = GatedPolicy(offer("80000000"), offer("81000000"))
    agent = ready(policy)
    first = asyncio.create_task(agent.turn(RUN, turn_body()))
    second = asyncio.create_task(agent.turn(RUN, turn_body()))
    await asyncio.sleep(0)
    policy.gate.set()
    answers = await asyncio.gather(first, second)
    assert len(policy.calls) == 1
    assert answers[0] == answers[1]


async def test_an_inconsistent_observation_never_reaches_the_policy() -> None:
    policy = ScriptedPolicy(offer("80000000"))
    agent = ready(policy)
    with pytest.raises(ObservationInconsistentError) as refused:
        await agent.turn(RUN, turn_body(offers_remaining=2))
    assert set(refused.value.details["fields"]) == {"offers_remaining_for_me"}
    assert policy.calls == []


def test_a_provisioning_repr_shows_no_mandate() -> None:
    text = repr(provisioning())
    assert MANDATES["buyer"]["reservation_price_minor"] not in text
    assert MANDATES["buyer"]["instructions"] not in text
