"""The operator API's run operations and their records (api_contract sections 1 and 2.2).

Step, pause, resume and abort through HTTP, each long-running one answered `202` with an operation
that records how it ended (Q54); the error envelope for each refusal; the run list and its cursor;
and clone. Against the real stack, as `test_api_end_to_end.py`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator

import pytest
from api_chain import AnvilChain
from api_controller import ControllerHarness
from api_http import ApiHarness, run_body

from api.db import Database, Party


@pytest.fixture
async def api(database: Database, chain: AnvilChain) -> AsyncIterator[ApiHarness]:
    harness = ApiHarness(ControllerHarness(database, chain))
    yield harness
    await harness.aclose()


async def _events(api: ApiHarness, run_id: str, kind: str) -> list[dict[str, object]]:
    async with api.database.unit_of_work() as uow:
        events = await uow.run_events.after(uuid.UUID(run_id), 0, limit=10_000)
    return [event.data for event in events if event.event_type == kind]


# ---------------------------------------------------------------------------------------------
# Step, pause, resume, abort
# ---------------------------------------------------------------------------------------------


async def test_a_step_operation_succeeds_when_its_turn_is_done(api: ApiHarness) -> None:
    run_id = await api.validated()
    first = (await api.client.post(f"/v1/runs/{run_id}/step")).json()
    await api.settle()
    assert (await api.operation(first["operation_id"]))["status"] == "succeeded"
    assert (await api.operation(first["operation_id"]))["result"]["state_cause"] == "step_complete"

    second = await api.client.post(f"/v1/runs/{run_id}/step")
    assert second.status_code == 202
    await api.settle()
    record = await api.operation(second.json()["operation_id"])
    assert (record["status"], record["result"]["state"]) == ("succeeded", "paused")
    run = await api.run(run_id)
    assert [entry["quote_amount_minor"] for entry in run["timeline"]] == ["80000000", "108000000"]

    # Every status change was streamed as an `operation` event, in order.
    statuses = [
        (event["operation_id"], event["status"])
        for event in await _events(api, run_id, "operation")
    ]
    assert statuses == [
        (first.get("operation_id"), "running"),
        (first.get("operation_id"), "succeeded"),
        (second.json()["operation_id"], "running"),
        (second.json()["operation_id"], "succeeded"),
    ]


async def test_pause_then_resume_then_an_abort_that_wins(api: ApiHarness) -> None:
    run_id = await api.validated()
    await api.client.post(f"/v1/runs/{run_id}/step")
    await api.settle()

    paused = await api.client.post(f"/v1/runs/{run_id}/pause")
    assert paused.status_code == 200  # already paused: pause leaves it so
    assert paused.json()["state"] == "paused"

    resumed = await api.client.post(f"/v1/runs/{run_id}/resume")
    assert resumed.status_code == 202, resumed.text
    paused = await api.client.post(f"/v1/runs/{run_id}/pause")
    assert paused.status_code == 200, paused.text
    assert paused.json()["state_cause"] == "operator_pause"
    await api.settle()
    assert (await api.operation(resumed.json()["operation_id"]))["status"] == "succeeded"
    notices = await _events(api, run_id, "notice")
    assert notices[-1]["message"] == "Paused; offer and session expiry continue."

    aborted = await api.client.post(f"/v1/runs/{run_id}/abort", json={"reason": "operator_request"})
    assert aborted.status_code == 202, aborted.text
    await api.settle()
    record = await api.operation(aborted.json()["operation_id"])
    assert record["status"] == "succeeded"
    assert record["result"]["outcome"]["kind"] == "aborted"
    assert record["result"]["outcome"]["reason"] == "operator_request"
    run = await api.run(run_id)
    assert (run["state"], run["outcome"]["kind"]) == ("terminal", "aborted")
    assert run["timeline"][-1]["sentence"] == "Operator aborted the session (operator request)."


async def test_an_abort_without_a_body_is_an_operator_request(api: ApiHarness) -> None:
    run_id = await api.validated()
    await api.client.post(f"/v1/runs/{run_id}/step")
    await api.settle()
    response = await api.client.post(f"/v1/runs/{run_id}/abort")
    assert response.status_code == 202, response.text
    await api.settle()
    assert (await api.run(run_id))["outcome"]["reason"] == "operator_request"


async def test_an_operation_whose_run_needs_recovery_fails(api: ApiHarness) -> None:
    """A start whose setup meets an agent that refuses what it should never see ends
    `recovery_required`; the operation says so rather than staying `running` forever."""
    run_id = await api.validated()
    import httpx

    api.controller.agents.intercept[Party.SELLER] = lambda request: (
        httpx.Response(
            409, json={"error": {"code": "invalid_state", "message": "x", "details": {}}}
        )
        if request.url.path.endswith("/setup-approval")
        else None
    )
    response = await api.client.post(f"/v1/runs/{run_id}/start")
    await api.settle()
    record = await api.operation(response.json()["operation_id"])
    assert record["status"] == "failed"
    assert record["error"]["code"] == "recovery_required"
    assert record["error"]["details"]["state"] == "recovery_required"

    # An abort is the way out of recovery, and is sent from it: the state it starts in is not its
    # failure. With no session yet, the run ends `failed_setup`, which for an abort is success.
    aborted = await api.client.post(f"/v1/runs/{run_id}/abort")
    assert aborted.status_code == 202, aborted.text
    assert aborted.json()["status"] == "running"
    await api.settle()
    record = await api.operation(aborted.json()["operation_id"])
    assert (record["status"], record["result"]["state"]) == ("succeeded", "failed_setup")


# ---------------------------------------------------------------------------------------------
# Refusals: the envelope, and nothing quoted back
# ---------------------------------------------------------------------------------------------


async def test_refusals_carry_the_error_envelope(api: ApiHarness) -> None:
    unknown = uuid.uuid4()
    response = await api.client.get(f"/v1/runs/{unknown}", headers={"X-Request-Id": "req-1"})
    assert response.status_code == 404
    assert response.json() == {
        "error": {
            "code": "not_found",
            "message": f"no run {unknown}",
            "details": {},
            "request_id": "req-1",
        }
    }
    assert response.headers["X-Request-Id"] == "req-1"

    run = await api.create()
    refused = await api.client.post(f"/v1/runs/{run['run_id']}/start")
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "invalid_state"
    assert refused.json()["error"]["details"] == {
        "state": "draft",
        "allowed_from": ["validated", "paused"],
    }

    step = await api.client.post(f"/v1/runs/{unknown}/step")
    assert step.status_code == 404
    assert (await api.client.get("/v1/nothing")).json()["error"]["code"] == "not_found"
    assert (await api.client.delete("/v1/health")).json()["error"]["code"] == "method_not_allowed"


async def test_a_refused_run_request_names_fields_and_quotes_no_value(api: ApiHarness) -> None:
    await api.prepare()
    body = run_body("default-overlap", api.deployment_id)
    body["seller"]["mandate"]["reservation_price_minor"] = "90000000\n"
    body["public_config"]["confirmation_threshold"] = "2"
    body["public_config"]["max_offers"] = 33
    response = await api.client.post("/v1/runs", json=body)
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "validation_error"
    assert set(error["details"]["fields"]) == {
        "seller.mandate.reservation_price_minor",
        "public_config.confirmation_threshold",
        "public_config.max_offers",
    }
    assert "90000000" not in response.text

    malformed = await api.client.post(
        "/v1/runs", content=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert (malformed.status_code, malformed.json()["error"]["code"]) == (400, "bad_request")

    body = run_body("default-overlap", api.deployment_id)
    body["deployment_id"] = "nowhere"
    unknown = await api.client.post("/v1/runs", json=body)
    assert unknown.status_code == 422
    assert unknown.json()["error"]["details"]["fields"] == {"deployment_id": "unknown"}

    bad_abort = await api.client.post(f"/v1/runs/{uuid.uuid4()}/abort", json={"reason": "whim"})
    assert bad_abort.status_code == 404


async def test_an_unknown_abort_reason_is_refused(api: ApiHarness) -> None:
    run_id = await api.validated()
    response = await api.client.post(f"/v1/runs/{run_id}/abort", json={"reason": "whim"})
    assert response.status_code == 422
    assert response.json()["error"]["details"] == {"fields": {"reason": "not an abort reason"}}


# ---------------------------------------------------------------------------------------------
# Listing and cloning
# ---------------------------------------------------------------------------------------------


async def test_runs_are_listed_newest_first_a_page_at_a_time(api: ApiHarness) -> None:
    created = [(await api.create())["run_id"] for _ in range(3)]
    first = (await api.client.get("/v1/runs", params={"limit": 2})).json()
    assert [run["run_id"] for run in first["runs"]] == created[::-1][:2]
    assert first["next_cursor"] is not None
    second = (
        await api.client.get("/v1/runs", params={"limit": 2, "cursor": first["next_cursor"]})
    ).json()
    assert [run["run_id"] for run in second["runs"]] == [created[0]]
    assert second["next_cursor"] is None
    assert set(first["runs"][0]) == {
        "run_id",
        "name",
        "parent_run_id",
        "batch_id",
        "scenario_id",
        "deployment_id",
        "created_at",
        "state",
        "state_cause",
        "mode",
        "outcome",
    }

    drafts = (await api.client.get("/v1/runs", params={"state": "draft"})).json()["runs"]
    assert len(drafts) == 3
    assert (await api.client.get("/v1/runs", params={"state": "terminal"})).json()["runs"] == []
    assert (await api.client.get("/v1/runs", params={"limit": 201})).status_code == 422
    bad = await api.client.get("/v1/runs", params={"cursor": "not-a-cursor"})
    assert (bad.status_code, bad.json()["error"]["code"]) == (400, "bad_request")


async def test_a_clone_is_a_fresh_draft_and_the_parent_is_untouched(api: ApiHarness) -> None:
    parent = await api.create()
    patch = {
        "name": "Infeasible clone",
        "seller": {"mandate": {"reservation_price_minor": "105000000"}},
    }
    response = await api.client.post(f"/v1/runs/{parent['run_id']}/clone", json=patch)
    assert response.status_code == 201, response.text
    clone = response.json()
    assert (clone["state"], clone["name"], clone["parent_run_id"]) == (
        "draft",
        "Infeasible clone",
        parent["run_id"],
    )
    # Fresh wallets: an address is never reused across runs (ADR-039).
    for role in ("buyer", "seller"):
        assert clone["parties"][role]["address"] != parent["parties"][role]["address"]

    headers = {"X-Observer-Reveal": "true"}
    cloned = (await api.client.get(f"/v1/runs/{clone['run_id']}/mandates", headers=headers)).json()
    original = (
        await api.client.get(f"/v1/runs/{parent['run_id']}/mandates", headers=headers)
    ).json()
    assert cloned["seller"]["reservation_price_minor"] == "105000000"
    assert (
        cloned["buyer"]["reservation_price_minor"] == original["buyer"]["reservation_price_minor"]
    )
    assert (cloned["buyer"]["version"], cloned["seller"]["version"]) == (2, 2)
    assert original["seller"]["reservation_price_minor"] == "90000000"
    assert original["seller"]["version"] == 1
    assert cloned["evaluator"]["feasible"] is False
    assert original["evaluator"] == {
        "feasible": True,
        "feasible_interval_minor": ["90000000", "100000000"],
        "feasible_surplus_minor": "10000000",
    }

    refused = await api.client.post(
        f"/v1/runs/{parent['run_id']}/clone", json={"parent_run_id": str(uuid.uuid4())}
    )
    assert refused.status_code == 422
    removed = await api.client.post(f"/v1/runs/{parent['run_id']}/clone", json={"limits": None})
    assert removed.status_code == 422
    assert json.loads(removed.text)["error"]["details"]["fields"] == {"limits": "missing"}


# ---------------------------------------------------------------------------------------------
# Since the stage 2.5 review
# ---------------------------------------------------------------------------------------------


async def test_of_two_racing_steps_one_is_accepted_and_one_is_turn_in_progress(
    api: ApiHarness,
) -> None:
    """The step's cause is decided under the run's row lock: the loser is refused, not given an
    operation that succeeds for a turn it did not take (api_contract 2.2)."""
    import asyncio

    run_id = await api.validated()
    await api.client.post(f"/v1/runs/{run_id}/step")
    await api.settle()
    answers = await asyncio.gather(
        api.client.post(f"/v1/runs/{run_id}/step"), api.client.post(f"/v1/runs/{run_id}/step")
    )
    assert sorted(answer.status_code for answer in answers) == [202, 409]
    (refused,) = [answer for answer in answers if answer.status_code == 409]
    assert refused.json()["error"]["code"] == "turn_in_progress"
    await api.settle()
    assert len((await api.run(run_id))["timeline"]) == 2


async def test_a_clone_copies_what_the_patch_does_not_change(api: ApiHarness) -> None:
    parent = await api.create()
    patch = {"seller": {"initial_balances": {"base_minor": "30000000"}}}
    clone = (await api.client.post(f"/v1/runs/{parent['run_id']}/clone", json=patch)).json()
    async with api.database.unit_of_work() as uow:
        old = {w.party: w for w in await uow.wallets.list_for_run(uuid.UUID(parent["run_id"]))}
        new = {w.party: w for w in await uow.wallets.list_for_run(uuid.UUID(clone["run_id"]))}
        old_run = await uow.runs.get(uuid.UUID(parent["run_id"]))
        new_run = await uow.runs.get(uuid.UUID(clone["run_id"]))
    assert old_run is not None and new_run is not None
    assert int(new[Party.SELLER].initial_base_minor) == 30_000_000
    for party in (Party.BUYER, Party.SELLER):
        assert new[party].allowance_minor == old[party].allowance_minor
        assert new[party].initial_quote_minor == old[party].initial_quote_minor
    assert int(new[Party.BUYER].initial_base_minor) == int(old[Party.BUYER].initial_base_minor)
    assert (new_run.scenario_id, new_run.public_config, new_run.limits) == (
        old_run.scenario_id,
        old_run.public_config,
        old_run.limits,
    )
    assert (new_run.buyer_policy, new_run.seller_effort) == (
        old_run.buyer_policy,
        old_run.seller_effort,
    )


async def test_the_run_resource_shows_each_partys_public_state(api: ApiHarness) -> None:
    """What the stage 4 panels render (FR-U1, FR-U7)."""
    created = await api.create()
    assert created["parties"]["buyer"]["balances"] == {"base_minor": "0", "quote_minor": "0"}
    assert created["parties"]["buyer"]["current_action"] == "idle"
    assert created["parties"]["buyer"]["decision_status"] is None
    assert created["labels"]["fixture"] is False
    assert created["metrics"]["model_cost_estimated_usd"] == "0.000000"
    assert created["metrics"]["rpc_cost_estimated_usd"] == "0.000000"

    run_id = created["run_id"]
    await api.client.post(f"/v1/runs/{run_id}/validate")
    await api.client.post(f"/v1/runs/{run_id}/step")
    await api.settle()
    run = await api.run(run_id)
    buyer, seller = run["parties"]["buyer"], run["parties"]["seller"]
    assert (buyer["decision_status"], seller["decision_status"]) == ("confirmed", None)
    assert buyer["current_action"] == seller["current_action"] == "idle"
    assert buyer["balances"] == {"base_minor": "0", "quote_minor": "250000000"}
    assert seller["balances"] == {"base_minor": "25000000", "quote_minor": "0"}
