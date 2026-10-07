"""Model-versus-model runs on canned responses, end to end on Anvil through the API (stage 3.2).

Both agents run the real `ModelPolicy` in fixture mode (ADR-088): each model call is answered from
its role's script, priced by the run's own `BudgetGuard`, judged by the validator and signed, if at
all, from validated state. What these show is the plumbing a live run will use — decision records
holding usage and cost, the metrics and the export reporting them, a spend ceiling ending the run
with reason 3, a NUL in a model's answer stored rather than stranding the run (ADR-090) — and
never anything about how a model negotiates: every run here is labelled `fixture`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from api_chain import AnvilChain
from api_controller import MODEL_FIXTURES, Agents, ControllerHarness
from api_http import ApiHarness

from api.db import Database, OutcomeKind, Party, RunMode, RunState, TurnState
from negotiation_protocol import validate

MODEL = "claude-sonnet-5-5"
REVEAL = {"X-Observer-Reveal": "true"}


def harness_for(database: Database, chain: AnvilChain, fixtures: Path) -> ApiHarness:
    agents = Agents(model_fixtures=fixtures)
    return ApiHarness(ControllerHarness(database, chain, agents=agents))


async def run_to_the_end(api: ApiHarness, **kw: Any) -> str:
    run_id = await api.validated(model_id=MODEL, **kw)
    response = await api.client.post(f"/v1/runs/{run_id}/start")
    assert response.status_code == 202, response.text
    await api.settle()
    return run_id


# --------------------------------------------------------------------------------------
# A settlement, with usage and cost on every surface
# --------------------------------------------------------------------------------------


@pytest.fixture
async def settling(database: Database, chain: AnvilChain) -> AsyncIterator[ApiHarness]:
    api = harness_for(database, chain, MODEL_FIXTURES / "default-overlap-settles")
    yield api
    await api.aclose()


async def test_a_fixture_model_run_settles_with_usage_and_cost_recorded_and_reported(
    settling: ApiHarness,
) -> None:
    api = settling
    run_id = await run_to_the_end(api)

    run = await api.controller.run(uuid.UUID(run_id))
    assert run.state == RunState.TERMINAL
    assert run.outcome_kind == OutcomeKind.SETTLED
    assert run.mode == RunMode.FIXTURE
    assert run.policy_versions == {"buyer": "model-1.0.0", "seller": "model-1.0.0"}
    versions = set(run.prompt_template_versions.values())
    assert len(versions) == 1 and next(iter(versions)).startswith("v1.0.0+")

    # Every attempt, as stored: buyer 90, seller 98, buyer 94, seller accepts.
    records = (await api.client.get(f"/v1/runs/{run_id}/decisions", headers=REVEAL)).json()
    decisions = records["decisions"]
    assert [(d["turn"], d["party"], d["status"]) for d in decisions] == [
        (1, "buyer", "valid"),
        (2, "seller", "valid"),
        (3, "buyer", "valid"),
        (4, "seller", "valid"),
    ]
    assert [d["raw_response"]["decision"]["action"] for d in decisions] == [
        "offer",
        "offer",
        "offer",
        "accept",
    ]
    # The first call of each agent wrote the system prompt to the cache; the second read it.
    assert [d["usage"] for d in decisions[:2]] == [
        {
            "input_tokens": 620,
            "output_tokens": 180,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 2150,
        }
    ] * 2
    # Hand-worked at the default model's rates, $2 / $10 a million, cache writes $2.50, reads
    # $0.20: 620 x 2 + 180 x 10 + 2,150 x 2.50 = 8,415 micro-dollars; and the second call's
    # 680 x 2 + 180 x 10 + 2,150 x 0.20 = 3,590. Each estimate is its prompt at the dearest input
    # rate, $4, and 16,000 output tokens at $10: 2,770 tokens is $0.171080, 2,830 is $0.171320.
    assert [d["cost_reported_usd"] for d in decisions] == [
        "0.008415",
        "0.008415",
        "0.003590",
        "0.003590",
    ]
    assert [d["cost_estimated_usd"] for d in decisions] == [
        "0.171080",
        "0.171080",
        "0.171320",
        "0.171320",
    ]
    assert all(d["model_id"] == MODEL and d["effort"] == "high" for d in decisions)
    assert all(d["raw_response_escaped"] is False for d in decisions)

    metrics = (await api.client.get(f"/v1/runs/{run_id}/metrics")).json()
    assert metrics["model_calls"] == 4
    assert metrics["model_cost_reported_usd"] == "0.024010"
    assert metrics["model_cost_estimated_usd"] == "0.684800"
    assert metrics["input_tokens"] == 2 * (2_770 + 2_830)
    assert metrics["output_tokens"] == 4 * 180

    export = (await api.client.get(f"/v1/runs/{run_id}/export")).json()
    validate(export, "export.v1.json")
    assert export["run"]["mode"] == "fixture"
    assert export["run"]["labels"]["fixture"] is True
    assert [d["cost_reported_usd"] for d in export["decisions_public"]] == [
        "0.008415",
        "0.008415",
        "0.003590",
        "0.003590",
    ]
    assert all(d["usage"] is not None for d in export["decisions_public"])

    outcome = export["run"]["outcome"]
    assert outcome["kind"] == "settled"
    settled = [e for e in export["chain_events"] if e["event"] == "SettlementCompleted"]
    assert len(settled) == 1


# --------------------------------------------------------------------------------------
# The spend ceiling: reason 3
# --------------------------------------------------------------------------------------


@pytest.fixture
async def spending(database: Database, chain: AnvilChain) -> AsyncIterator[ApiHarness]:
    api = harness_for(database, chain, MODEL_FIXTURES / "spend-ceiling")
    yield api
    await api.aclose()


async def test_a_run_whose_spend_ceiling_is_crossed_is_aborted_with_reason_3(
    spending: ApiHarness,
) -> None:
    api = spending
    run_id = await run_to_the_end(api, limits={"model_spend_ceiling_usd": "0.40"})

    run = await api.controller.run(uuid.UUID(run_id))
    assert (run.state, run.state_cause) == (RunState.TERMINAL, "budget_exhausted")
    assert (run.outcome_kind, run.outcome_reason_code) == (OutcomeKind.ABORTED, 3)

    async with api.database.unit_of_work() as uow:
        turns = sorted(await uow.turns.list_for_run(run.id), key=lambda t: t.turn)
        decisions = await uow.decisions.list_for_run(run.id)
    # Buyer, seller, buyer, seller, and the buyer's third call refused before it was sent.
    assert [t.party for t in turns] == [Party.BUYER, Party.SELLER] * 2 + [Party.BUYER]
    last = turns[-1]
    assert (last.state, last.failure_code) == (TurnState.MODEL_FAILED, "budget_exhausted")
    assert last.failure_detail is not None and "spend_ceiling" in last.failure_detail
    # The refused call was never made, so it left no decision record.
    assert [d for d in decisions if d.turn_id == last.id] == []
    buyer = [d for d in decisions if d.party == Party.BUYER]
    spent = sum((d.cost_reported_usd or Decimal(0)) for d in buyer)
    # $0.156615 and $0.151790 reported; the next call's $0.171320 bound would pass $0.40.
    assert spent == Decimal("0.308405")


# --------------------------------------------------------------------------------------
# Q71: a NUL in a model's answer (ADR-090)
# --------------------------------------------------------------------------------------


def nul_fixtures(directory: Path) -> Path:
    """The settling scripts, with a NUL in the buyer's first explanation."""
    for role in ("buyer", "seller"):
        script = json.loads(
            (MODEL_FIXTURES / "default-overlap-settles" / f"{role}.json").read_text()
        )
        if role == "buyer":
            script["responses"][0]["answer"]["explanation"] = "Opening low.\u0000 Room to concede."
        (directory / f"{role}.json").write_text(json.dumps(script), encoding="utf-8")
    return directory


@pytest.fixture
async def nul(database: Database, chain: AnvilChain, tmp_path: Path) -> AsyncIterator[ApiHarness]:
    api = harness_for(database, chain, nul_fixtures(tmp_path))
    yield api
    await api.aclose()


async def test_a_nul_in_a_models_answer_is_stored_escaped_and_the_run_goes_on(
    nul: ApiHarness,
) -> None:
    api = nul
    run_id = await run_to_the_end(api)

    run = await api.controller.run(uuid.UUID(run_id))
    assert run.state == RunState.TERMINAL
    assert run.outcome_kind == OutcomeKind.SETTLED

    records = (await api.client.get(f"/v1/runs/{run_id}/decisions", headers=REVEAL)).json()
    first = records["decisions"][0]
    assert first["raw_response"]["explanation"] == "Opening low.\\u0000 Room to concede."
    assert first["raw_response_escaped"] is True
    assert [d["raw_response_escaped"] for d in records["decisions"][1:]] == [False] * 3


async def test_a_deterministic_run_on_fixture_agents_is_still_live(settling: ApiHarness) -> None:
    """Only a party that runs the model policy can make a run canned (ADR-088)."""
    run_id = await settling.validated()
    run = await settling.controller.run(uuid.UUID(run_id))
    assert run.mode == RunMode.LIVE
    report = (await settling.client.post(f"/v1/runs/{run_id}/validate")).json()
    assert not any(check["check"].endswith("_model_available") for check in report["checks"])


async def test_the_validation_report_names_each_model_agents_mode(settling: ApiHarness) -> None:
    run_id = await settling.validated(model_id=MODEL)
    report = (await settling.client.post(f"/v1/runs/{run_id}/validate")).json()
    model_checks = [c for c in report["checks"] if c["check"].endswith("_model_available")]
    assert model_checks == [
        {"check": "buyer_agent_model_available", "ok": True, "detail": "fixture"},
        {"check": "seller_agent_model_available", "ok": True, "detail": "fixture"},
    ]


# --------------------------------------------------------------------------------------
# What the adversarial review of stage 3.2 found
# --------------------------------------------------------------------------------------


def reporting_live(party: Party) -> Any:
    """The party's agent, its health answered as a live model instance would answer it."""

    def answer(request: httpx.Request) -> httpx.Response | None:
        if request.url.path != "/internal/health":
            return None
        health = {
            "status": "ok",
            "role": party.value,
            "instance": "agent-a" if party == Party.BUYER else "agent-b",
            "policy_kinds": ["deterministic", "model"],
            "model_ok": True,
            "model_mode": "live",
            "signer_ok": True,
        }
        return httpx.Response(200, content=json.dumps(health).encode())

    return answer


@pytest.mark.parametrize("live_party", [Party.BUYER, Party.SELLER])
async def test_one_fixture_party_makes_the_run_fixture_whichever_it_is(
    settling: ApiHarness, live_party: Party
) -> None:
    """ADR-088: a run with any canned decision is not evidence of autonomous behaviour."""
    settling.controller.agents.intercept[live_party] = reporting_live(live_party)
    run_id = await settling.validated(model_id=MODEL)
    run = await settling.controller.run(uuid.UUID(run_id))
    assert run.mode == RunMode.FIXTURE


async def test_a_run_revalidated_on_live_agents_is_live_again(settling: ApiHarness) -> None:
    run_id = await settling.validated(model_id=MODEL)
    assert (await settling.controller.run(uuid.UUID(run_id))).mode == RunMode.FIXTURE
    for party in (Party.BUYER, Party.SELLER):
        settling.controller.agents.intercept[party] = reporting_live(party)
    response = await settling.client.post(f"/v1/runs/{run_id}/validate")
    assert response.status_code == 200 and response.json()["ok"]
    assert (await settling.controller.run(uuid.UUID(run_id))).mode == RunMode.LIVE
    assert (await settling.run(run_id))["labels"]["fixture"] is False


def surrogate_fixtures(directory: Path) -> Path:
    """The settling scripts, the buyer's first answer preceded by one whose explanation is a lone
    surrogate escape: valid JSON to `json.loads`, unencodable as UTF-8."""
    for role in ("buyer", "seller"):
        script = json.loads(
            (MODEL_FIXTURES / "default-overlap-settles" / f"{role}.json").read_text()
        )
        if role == "buyer":
            first = script["responses"][0]
            text = json.dumps(first["answer"]).replace(
                '"explanation": "', '"explanation": "\\ud800'
            )
            script["responses"].insert(0, {"text": text, "usage": first["usage"]})
        (directory / f"{role}.json").write_text(json.dumps(script), encoding="utf-8")
    return directory


@pytest.fixture
async def surrogate(
    database: Database, chain: AnvilChain, tmp_path: Path
) -> AsyncIterator[ApiHarness]:
    api = harness_for(database, chain, surrogate_fixtures(tmp_path))
    yield api
    await api.aclose()


async def test_a_lone_surrogate_in_a_models_answer_is_refused_and_the_run_goes_on(
    surrogate: ApiHarness,
) -> None:
    """Before the fix, the turn was signed, the agent answered 500 on every ask, and the run was
    stranded in recovery_required with its billed call unrecorded."""
    api = surrogate
    run_id = await run_to_the_end(api)

    run = await api.controller.run(uuid.UUID(run_id))
    assert run.state == RunState.TERMINAL
    assert run.outcome_kind == OutcomeKind.SETTLED

    records = (await api.client.get(f"/v1/runs/{run_id}/decisions", headers=REVEAL)).json()
    first, repaired = records["decisions"][:2]
    assert (first["turn"], first["attempt"], first["status"]) == (1, 1, "invalid")
    assert first["validation"]["code"] == "schema_error"
    assert isinstance(first["raw_response"], str) and "\\ud800" in first["raw_response"]
    assert first["cost_reported_usd"] is not None
    assert (repaired["turn"], repaired["attempt"], repaired["status"]) == (1, 2, "valid")
    metrics = (await api.client.get(f"/v1/runs/{run_id}/metrics")).json()
    assert metrics["model_calls"] == 5
