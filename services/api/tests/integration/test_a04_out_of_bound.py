"""A04: an out-of-bound model proposal is refused and never broadcast (stage 3.2).

End to end on Anvil through the operator API, with both agents in fixture mode (ADR-088): the
buyer's model opens at 80; the seller's model proposes 85 against its floor of 90, is refused,
is repaired once with its own feedback, proposes 85 again and is refused again. Nothing of the
seller's reaches the outbox, and the run is aborted with reason 2, `model_failure` — a recorded
failure, never an economic walk-away. The canned answers go through the real `ModelPolicy`,
validator and signer, and the run is labelled `fixture` wherever it appears.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
from api_chain import AnvilChain
from api_controller import MODEL_FIXTURES, Agents, ControllerHarness
from api_http import ApiHarness

from api.db import (
    ActionKind,
    Database,
    OutcomeKind,
    Party,
    RunMode,
    RunState,
    TurnState,
    TxKind,
)

MODEL = "claude-sonnet-5-5"
#: The transactions a participant's signed action becomes.
NEGOTIATION = frozenset({TxKind.RECORD_OFFER, TxKind.ACCEPT_AND_SETTLE, TxKind.CLOSE_SESSION})


@pytest.fixture
async def api(database: Database, chain: AnvilChain) -> AsyncIterator[ApiHarness]:
    agents = Agents(model_fixtures=MODEL_FIXTURES / "a04-seller-below-floor")
    harness = ApiHarness(ControllerHarness(database, chain, agents=agents))
    yield harness
    await harness.aclose()


async def test_a04_a_seller_below_its_floor_is_refused_twice_and_the_run_aborts(
    api: ApiHarness,
) -> None:
    run_id = await api.validated(model_id=MODEL)
    response = await api.client.post(f"/v1/runs/{run_id}/start")
    assert response.status_code == 202, response.text
    await api.settle()

    run = await api.controller.run(uuid.UUID(run_id))
    assert (run.state, run.state_cause) == (RunState.TERMINAL, "model_failure")
    assert (run.outcome_kind, run.outcome_reason_code) == (OutcomeKind.ABORTED, 2)
    assert run.mode == RunMode.FIXTURE

    async with api.database.unit_of_work() as uow:
        turns = sorted(await uow.turns.list_for_run(run.id), key=lambda t: t.turn)
        decisions = await uow.decisions.list_for_run(run.id)
        actions = await uow.signed_actions.list_for_run(run.id)
        outbox = await uow.outbox.list_for_run(run.id)

    buyer_turn, seller_turn = turns
    assert buyer_turn.party == Party.BUYER and buyer_turn.state != TurnState.MODEL_FAILED
    assert (seller_turn.party, seller_turn.state, seller_turn.failure_code) == (
        Party.SELLER,
        TurnState.MODEL_FAILED,
        "repair_exhausted",
    )

    seller = [d for d in decisions if d.turn_id == seller_turn.id]
    assert [d.attempt for d in seller] == [1, 2]
    assert [d.validation_code for d in seller] == ["below_reservation", "below_reservation"]
    assert [d.raw_response["decision"]["quote_amount_minor"] for d in seller] == [
        "85000000",
        "85000000",
    ]
    assert not any(d.authorized for d in seller)
    # Each refused attempt is a model call, recorded with its usage and both costs.
    assert all(d.usage is not None and d.cost_reported_usd is not None for d in seller)
    assert all(d.prompt_template_version is not None for d in seller)

    # Only the buyer's opening offer was signed; nothing of the seller's reached the outbox.
    assert [(a.kind, int(a.typed_message["quoteAmount"])) for a in actions] == [
        (ActionKind.OFFER, 80_000_000)
    ]
    negotiation = [row for row in outbox if row.kind in NEGOTIATION]
    assert [(row.kind, row.signed_action_id) for row in negotiation] == [
        (TxKind.RECORD_OFFER, actions[0].id)
    ]
    assert [row.kind for row in outbox if row.kind == TxKind.ABORT_SESSION] == [
        TxKind.ABORT_SESSION
    ]

    resource = await api.run(run_id)
    assert resource["mode"] == "fixture"
    assert resource["labels"]["fixture"] is True
