"""Idempotency keys (api_contract section 1, Q57), the operator token (ADR-028), the observer reveal
header (ADR-018) and health (section 2.1), through the HTTP API against the real stack."""

from __future__ import annotations

import asyncio
import io
import json
import logging
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import timedelta

import httpx
import pytest
import structlog
from api_chain import AnvilChain
from api_controller import ControllerHarness
from api_http import ApiHarness, run_body

from api.db import Database, Party
from api.logs import configure_logging


@pytest.fixture
async def api(database: Database, chain: AnvilChain) -> AsyncIterator[ApiHarness]:
    harness = ApiHarness(ControllerHarness(database, chain))
    yield harness
    await harness.aclose()


def key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid.uuid4())}


# ---------------------------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------------------------


async def test_the_same_key_and_body_replays_the_stored_response(api: ApiHarness) -> None:
    await api.prepare()
    headers = key()
    body = run_body("default-overlap", api.deployment_id)
    first = await api.client.post("/v1/runs", json=body, headers=headers)
    # Key order and spacing do not make it a different request.
    again = await api.client.post(
        "/v1/runs",
        content=json.dumps(body, indent=4, sort_keys=True).encode(),
        headers={**headers, "Content-Type": "application/json"},
    )
    assert (first.status_code, again.status_code) == (201, 201)
    assert again.headers["Idempotent-Replayed"] == "true"
    assert "Idempotent-Replayed" not in first.headers
    assert again.json() == first.json()
    runs = (await api.client.get("/v1/runs")).json()["runs"]
    assert len(runs) == 1, "the replay created nothing"


async def test_the_same_key_with_another_body_conflicts(api: ApiHarness) -> None:
    await api.prepare()
    headers = key()
    body = run_body("default-overlap", api.deployment_id)
    assert (await api.client.post("/v1/runs", json=body, headers=headers)).status_code == 201
    body["name"] = "something else"
    conflict = await api.client.post("/v1/runs", json=body, headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


async def test_a_key_is_scoped_to_its_route_and_run(api: ApiHarness) -> None:
    first, second = await api.validated(), await api.validated()
    headers = key()
    one = await api.client.post(f"/v1/runs/{first}/validate", headers=headers)
    two = await api.client.post(f"/v1/runs/{second}/validate", headers=headers)
    assert (one.status_code, two.status_code) == (200, 200)
    assert "Idempotent-Replayed" not in two.headers


async def test_a_refused_request_keeps_no_key(api: ApiHarness) -> None:
    run = await api.create()
    headers = key()
    refused = await api.client.post(f"/v1/runs/{run['run_id']}/start", headers=headers)
    assert refused.status_code == 409  # a draft cannot start
    await api.client.post(f"/v1/runs/{run['run_id']}/validate")
    # The same key now does the work, rather than replaying the refusal.
    accepted = await api.client.post(f"/v1/runs/{run['run_id']}/start", headers=headers)
    assert accepted.status_code == 202, accepted.text
    assert "Idempotent-Replayed" not in accepted.headers
    await api.settle()


async def test_a_long_running_replay_is_the_operation_as_it_stands(api: ApiHarness) -> None:
    run_id = await api.validated()
    headers = key()
    first = await api.client.post(f"/v1/runs/{run_id}/step", headers=headers)
    await api.settle()
    again = await api.client.post(f"/v1/runs/{run_id}/step", headers=headers)
    assert again.status_code == 202
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.json()["operation_id"] == first.json()["operation_id"]
    assert again.json()["status"] == "succeeded"
    assert again.headers["Location"] == first.headers["Location"]
    timeline = (await api.run(run_id))["timeline"]
    assert len(timeline) == 1, "the replay took no second turn"


async def test_a_key_past_its_retention_may_be_used_again(api: ApiHarness) -> None:
    await api.prepare()
    headers = key()
    body = run_body("default-overlap", api.deployment_id)
    first = await api.client.post("/v1/runs", json=body, headers=headers)
    later = api.services.idempotency
    later._clock = lambda: api.services.clock() + timedelta(hours=24, seconds=1)
    body["name"] = "a new request under an old key"
    second = await api.client.post("/v1/runs", json=body, headers=headers)
    assert second.status_code == 201, second.text
    assert second.json()["run_id"] != first.json()["run_id"]


async def test_two_racing_requests_with_one_key_do_the_work_once(api: ApiHarness) -> None:
    await api.prepare()
    headers = key()
    body = run_body("default-overlap", api.deployment_id)
    answers = await asyncio.gather(
        *(api.client.post("/v1/runs", json=body, headers=headers) for _ in range(4))
    )
    statuses = sorted(answer.status_code for answer in answers)
    assert statuses.count(201) >= 1
    assert set(statuses) <= {201, 409}
    created = [answer.json() for answer in answers if answer.status_code == 201]
    assert all(isinstance(body, dict) and body.get("run_id") for body in created), created
    assert len({body["run_id"] for body in created}) == 1, "every 201 is the one run"
    for answer in answers:
        if answer.status_code == 409:
            assert answer.json()["error"]["code"] == "idempotency_conflict"
    assert len((await api.client.get("/v1/runs")).json()["runs"]) == 1


async def test_a_claim_whose_request_never_answered_is_released(api: ApiHarness) -> None:
    """Q63: a request killed between claiming its key and answering left the key held; within
    the claim timeout a retry is told the first is still being answered, after it the work is done
    and the dead claim is recorded `interrupted`."""
    from api.db import NewOperation, OperationStatus
    from negotiation_protocol import json_sha256

    await api.prepare()
    body = run_body("default-overlap", api.deployment_id)
    key = str(uuid.uuid4())
    async with api.database.unit_of_work() as uow:
        dead = await uow.operations.create(
            NewOperation(
                kind="create_run",
                route="POST /v1/runs",
                request_hash=json_sha256(body),
                idempotency_key=key,
            )
        )
    headers = {"Idempotency-Key": key}
    busy = await api.client.post("/v1/runs", json=body, headers=headers)
    assert (busy.status_code, busy.json()["error"]["code"]) == (409, "idempotency_conflict")

    later = api.services.idempotency
    later._clock = lambda: api.services.clock() + timedelta(seconds=61)
    done = await api.client.post("/v1/runs", json=body, headers=headers)
    assert done.status_code == 201, done.text
    async with api.database.unit_of_work() as uow:
        record = await uow.operations.get(dead.id)
    assert record is not None and record.status == OperationStatus.FAILED
    assert record.error is not None and record.error["code"] == "interrupted"


async def test_a_long_running_replay_in_flight_is_the_live_operation(api: ApiHarness) -> None:
    """ADR-078 as amended (Q63): while the first start is still running, the same key gets its
    live operation, 202, not 409."""
    run_id = await api.validated()
    headers = key()
    first = await api.client.post(f"/v1/runs/{run_id}/start", headers=headers)
    again = await api.client.post(f"/v1/runs/{run_id}/start", headers=headers)
    assert (first.status_code, again.status_code) == (202, 202)
    assert again.headers["Idempotent-Replayed"] == "true"
    assert again.json()["operation_id"] == first.json()["operation_id"]
    assert again.json()["status"] in ("running", "succeeded")
    await api.settle()


async def test_an_idempotency_key_must_be_a_uuid(api: ApiHarness) -> None:
    response = await api.client.post(
        f"/v1/runs/{uuid.uuid4()}/start", headers={"Idempotency-Key": "abc"}
    )
    assert (response.status_code, response.json()["error"]["code"]) == (404, "not_found")
    run = await api.create()
    response = await api.client.post(
        f"/v1/runs/{run['run_id']}/start", headers={"Idempotency-Key": "abc"}
    )
    assert (response.status_code, response.json()["error"]["code"]) == (400, "bad_request")


# ---------------------------------------------------------------------------------------------
# The operator token and the reveal header
# ---------------------------------------------------------------------------------------------


async def test_with_a_token_every_route_but_health_requires_it(
    database: Database, chain: AnvilChain
) -> None:
    api = ApiHarness(ControllerHarness(database, chain), operator_token="s3cret-operator-token")
    try:
        await api.prepare()
        assert (await api.client.get("/v1/health")).status_code == 200
        bare = await api.client.get("/v1/runs")
        assert (bare.status_code, bare.json()["error"]["code"]) == (401, "unauthorized")
        wrong = await api.client.get("/v1/runs", headers={"Authorization": "Bearer nope"})
        assert wrong.status_code == 401
        missing = await api.client.get(f"/v1/runs/{uuid.uuid4()}")
        assert missing.status_code == 401, "an unknown route says nothing before the token"
        good = {"Authorization": "Bearer s3cret-operator-token"}
        assert (await api.client.get("/v1/runs", headers=good)).status_code == 200
        assert "s3cret-operator-token" not in bare.text
    finally:
        await api.aclose()


@pytest.fixture
def captured_logs() -> Iterator[io.StringIO]:
    config = structlog.get_config()
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream)
    yield stream
    structlog.configure(**config)
    structlog.contextvars.clear_contextvars()
    root.handlers, root.level = handlers, level


async def test_private_routes_need_the_reveal_header_and_are_logged(
    api: ApiHarness, captured_logs: io.StringIO
) -> None:
    run = await api.create()
    run_id = run["run_id"]
    private = [
        f"/v1/runs/{run_id}/mandates",
        f"/v1/runs/{run_id}/decisions",
        "/v1/scenarios/default-overlap",
        f"/v1/runs/{run_id}/export?include_private=true",
    ]
    for path in private:
        refused = await api.client.get(path)
        assert (refused.status_code, refused.json()["error"]["code"]) == (403, "reveal_required")
        assert "100000000" not in refused.text
    reveal = {"X-Observer-Reveal": "true"}
    for path in private:
        assert (await api.client.get(path, headers=reveal)).status_code == 200, path

    lines = [json.loads(line) for line in captured_logs.getvalue().splitlines()]
    reveals = [line for line in lines if line.get("event") == "observer.reveal"]
    assert len(reveals) == len(private)
    assert {line["run_id"] for line in reveals} == {run_id, None}
    # The log records the access, never what it revealed.
    for secret in ("100000000", "90000000", "Pay as little"):
        assert secret not in captured_logs.getvalue()


async def test_the_public_views_hold_no_mandate(api: ApiHarness) -> None:
    run = await api.create()
    scenarios = (await api.client.get("/v1/scenarios")).json()["scenarios"]
    assert [s["feasibility_hint"] for s in scenarios] == [None, None]
    public = [
        (await api.client.get("/v1/scenarios")).text,
        (await api.client.get(f"/v1/runs/{run['run_id']}")).text,
        (await api.client.get("/v1/runs")).text,
        (await api.client.get(f"/v1/runs/{run['run_id']}/metrics")).text,
        (await api.client.get(f"/v1/runs/{run['run_id']}/export")).text,
    ]
    for text in public:
        document = json.loads(text)
        # Not 10000000: the seller's inventory floor equals the public base amount.
        for secret in ("100000000", "90000000"):
            assert not _contains_number(document, secret), (secret, text[:200])
        assert "Pay as little" not in text and "Obtain as much" not in text


def _contains_number(value: object, number: str) -> bool:
    """Whole-number matching: a substring would find 100000000 inside an ETH balance (stage 2.4)."""
    if isinstance(value, dict):
        return any(_contains_number(item, number) for item in value.values())
    if isinstance(value, list):
        return any(_contains_number(item, number) for item in value)
    return str(value) == number


# ---------------------------------------------------------------------------------------------
# Health and the system routes
# ---------------------------------------------------------------------------------------------


async def test_health_reports_each_dependency_and_503_when_one_is_down(api: ApiHarness) -> None:
    healthy = await api.client.get("/v1/health")
    assert healthy.status_code == 200
    assert healthy.json() == {
        "status": "ok",
        "version": "0.1.0+test",
        "chain_id": 31337,
        "rpc_ok": True,
        "db_ok": True,
        "agent_a_ok": True,
        "agent_b_ok": True,
    }
    api.controller.agents.intercept[Party.SELLER] = lambda request: httpx.Response(503)
    degraded = await api.client.get("/v1/health")
    assert degraded.status_code == 503
    assert (degraded.json()["status"], degraded.json()["agent_b_ok"]) == ("degraded", False)
    assert degraded.json()["agent_a_ok"] is True

    # An agent that answers as the other role is not this one's agent; one whose signer did not
    # load cannot sign.
    del api.controller.agents.intercept[Party.SELLER]
    api.controller.agents.rewrite[Party.BUYER] = lambda path, body: {**body, "role": "seller"}
    assert (await api.client.get("/v1/health")).json()["agent_a_ok"] is False
    api.controller.agents.rewrite[Party.BUYER] = lambda path, body: {**body, "signer_ok": False}
    assert (await api.client.get("/v1/health")).json()["agent_a_ok"] is False


async def test_deployments_and_scenarios_are_listed(api: ApiHarness) -> None:
    await api.prepare()
    (deployment,) = (await api.client.get("/v1/deployments")).json()["deployments"]
    assert deployment["deployment_id"] == api.deployment_id
    assert deployment["chain_id"] == 31337
    assert deployment["explorer_base_url"] is None and deployment["ens"] is None
    assert deployment["deployed_at"].endswith("Z")
    scenario = await api.client.get(
        "/v1/scenarios/default-overlap", headers={"X-Observer-Reveal": "true"}
    )
    assert scenario.json()["seller"]["mandate"]["reservation_price_minor"] == "90000000"
    missing = await api.client.get("/v1/scenarios/nope", headers={"X-Observer-Reveal": "true"})
    assert missing.status_code == 404
