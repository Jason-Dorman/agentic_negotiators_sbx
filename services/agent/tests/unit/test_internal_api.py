"""The agent internal API over HTTP, in process (docs/api_contract.md section 6).

Driven through the real application — routes, HMAC, parsing, service, key holder — with httpx's
ASGI transport, so every request is a real request with a real MAC. docs/test_strategy.md section 6
names four things to show: HMAC rejection, provisioning idempotency, approve-session mismatch
detection and release discarding state. Each has its own section, beside the refusals the contract
lists for each route.
"""

from __future__ import annotations

import io
import json
from collections.abc import AsyncIterator, Iterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
import structlog
from agent_observations import BALANCES, FIXTURE, MANDATES, observation

from agent.keys import KeyDerivation
from agent.keys.derivation import derive_run_key
from agent.logs import configure_logging
from agent.main import create_app
from agent.settings import AgentSettings
from negotiation_protocol import (
    AUTH_HEADER,
    REQUEST_ID_HEADER,
    SessionConfig,
    agent_request_mac,
    json_sha256,
    to_hex32,
)

SECRET = "a-shared-secret-of-at-least-thirty-two-characters"
ROOT = "0x" + "11" * 32
RUN = "6f1c2a4e-9d7b-4c3a-8e2f-1a2b3c4d5e6f"
OTHER_RUN = "00000000-0000-4000-8000-000000000002"
COUNTERPARTY = "0x" + "c0" * 20
SESSION_ID = "0x" + "5e" * 32
OPENED_AT = 1_760_000_000 - 60
EXPIRES_AT = OPENED_AT + 1800
CHAIN_TIME = 1_760_000_000
BUYER_DERIVATION = KeyDerivation(31337, "buyer", UUID(RUN))


def settings(**overrides: Any) -> AgentSettings:
    values: dict[str, Any] = {
        "role": "buyer",
        "instance": "agent-a",
        "root_key_ref": "env:BUYER_ROOT_KEY",
        "shared_secret": SECRET,
        "port": 8101,
    }
    values.update(overrides)
    return AgentSettings(**values)


class Agent:
    """A signed-request client for one in-process agent instance."""

    def __init__(self, client: httpx.AsyncClient, secret: str = SECRET) -> None:
        self._client = client
        self._secret = secret.encode()

    async def call(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        mac: str | None = None,
        request_id: str | None = "req-1",
        raw: bytes | None = None,
    ) -> httpx.Response:
        content = raw if raw is not None else (b"" if body is None else json.dumps(body).encode())
        headers = {AUTH_HEADER: mac or agent_request_mac(self._secret, method, path, content)}
        if request_id is not None:
            headers[REQUEST_ID_HEADER] = request_id
        return await self._client.request(method, path, content=content, headers=headers)

    async def post(
        self, run_id: str, route: str, body: Any = None, **kwargs: Any
    ) -> httpx.Response:
        return await self.call("POST", f"/internal/runs/{run_id}/{route}", body, **kwargs)


@pytest.fixture
async def agent() -> AsyncIterator[Agent]:
    app = create_app(settings(), environ={"BUYER_ROOT_KEY": ROOT})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent-a") as client:
        yield Agent(client)


def provision_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "role": "buyer",
        "policy": "deterministic",
        "model_id": None,
        "effort": None,
        "limits": {
            "model_call_ceiling": 20,
            "model_spend_ceiling_usd": "2.00",
            "model_timeout_s": 45,
            "repair_attempts": 1,
        },
        "expected_session": {
            "chain_id": 31337,
            "exchange_address": FIXTURE["domain"]["verifying_contract"],
            "base_token": FIXTURE["deployment"]["base_token"],
            "quote_token": FIXTURE["deployment"]["quote_token"],
            "base_amount_minor": "10000000",
            "max_offers": 8,
            "session_duration_s": 1800,
            "offer_lifetime_s": 600,
        },
        "key_ref": "env:BUYER_ROOT_KEY",
        "expected_address": None,
        "mandate_version_id": "11111111-2222-4333-8444-555555555555",
        "mandate": dict(MANDATES["buyer"]),
        "initial_balances": dict(BALANCES["buyer"]),
        "allowance_minor": "250000000",
    }
    body.update(overrides)
    return body


def opened_body(buyer: str, **overrides: Any) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "session_id": SESSION_ID,
        "buyer": buyer,
        "seller": COUNTERPARTY,
        "base_token": FIXTURE["deployment"]["base_token"],
        "quote_token": FIXTURE["deployment"]["quote_token"],
        "base_amount_minor": "10000000",
        "expires_at_ts": EXPIRES_AT,
        "max_offers": 8,
        "opened_at_ts": OPENED_AT,
    }
    fields.update(overrides)
    if "config_hash" not in fields:
        config = SessionConfig(
            session_id=bytes.fromhex(fields["session_id"][2:]),
            buyer=fields["buyer"],
            seller=fields["seller"],
            base_amount=int(fields["base_amount_minor"]),
            expires_at=fields["expires_at_ts"],
            max_offers=fields["max_offers"],
        )
        hashed = config.config_hash(fields["base_token"], fields["quote_token"])
        fields["config_hash"] = to_hex32(hashed)
    return fields


async def provisioned(agent: Agent, run_id: str = RUN) -> str:
    response = await agent.post(run_id, "provision", provision_body())
    assert response.status_code == 200, response.text
    address: str = response.json()["my_address"]
    return address


async def approved(agent: Agent, run_id: str = RUN) -> str:
    address = await provisioned(agent, run_id)
    response = await agent.post(run_id, "approve-session", opened_body(address))
    assert response.status_code == 200, response.text
    return address


def turn_body(address: str, turn: int = 1, run_id: str = RUN, **changes: Any) -> dict[str, Any]:
    document = observation("buyer")
    del document["mandate"]
    document["run_id"] = run_id
    document["my_address"] = address
    document["session"].update(
        {"session_id": SESSION_ID, "expires_at": EXPIRES_AT, "config_hash": _config_hash(address)}
    )
    document.update(changes)
    return {**document, "turn": turn, "deadline_at": "2099-09-30T12:00:45.000Z"}


def _config_hash(address: str) -> str:
    return str(opened_body(address)["config_hash"])


def error_of(response: httpx.Response) -> dict[str, Any]:
    envelope = response.json()
    assert set(envelope) == {"error"}
    error: dict[str, Any] = envelope["error"]
    assert set(error) == {"code", "message", "details", "request_id"}
    return error


# --------------------------------------------------------------------------------------
# HMAC (ADR-041)
# --------------------------------------------------------------------------------------


async def test_a_correctly_signed_health_check_is_answered(agent: Agent) -> None:
    response = await agent.call("GET", "/internal/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "role": "buyer",
        "instance": "agent-a",
        "policy_kinds": ["deterministic"],
        "model_ok": False,
        "model_mode": None,
        "signer_ok": True,
    }
    assert response.headers[REQUEST_ID_HEADER] == "req-1"


@pytest.mark.parametrize(
    "mac",
    ["", "0" * 64, agent_request_mac(b"another-secret", "GET", "/internal/health", b"")],
    ids=["empty", "wrong", "another instance's secret"],
)
async def test_a_request_without_a_valid_mac_is_refused(agent: Agent, mac: str) -> None:
    response = await agent.call("GET", "/internal/health", mac=mac or "missing")
    assert response.status_code == 401
    assert error_of(response)["code"] == "unauthorized"


async def test_a_health_checks_mac_does_not_release_a_run(agent: Agent) -> None:
    """The hole ADR-041 closed: both bodies are empty."""
    await provisioned(agent)
    health_mac = agent_request_mac(SECRET.encode(), "GET", "/internal/health", b"")
    response = await agent.post(RUN, "release", mac=health_mac)
    assert response.status_code == 401
    assert (await agent.post(RUN, "setup-approval", _fees())).status_code == 200


async def test_a_mac_for_one_run_is_refused_for_another(agent: Agent) -> None:
    body = json.dumps(provision_body()).encode()
    mac = agent_request_mac(SECRET.encode(), "POST", f"/internal/runs/{RUN}/provision", body)
    response = await agent.post(OTHER_RUN, "provision", raw=body, mac=mac)
    assert response.status_code == 401


async def test_a_tampered_body_is_refused(agent: Agent) -> None:
    body = json.dumps(provision_body()).encode()
    mac = agent_request_mac(SECRET.encode(), "POST", f"/internal/runs/{RUN}/provision", body)
    tampered = body.replace(b'"100000000"', b'"900000000"')
    response = await agent.post(RUN, "provision", raw=tampered, mac=mac)
    assert response.status_code == 401


async def test_authentication_comes_before_the_path_is_parsed(agent: Agent) -> None:
    response = await agent.post("not-a-uuid", "provision", provision_body(), mac="0" * 64)
    assert response.status_code == 401


async def test_a_signed_request_without_a_request_id_is_a_bad_request(agent: Agent) -> None:
    response = await agent.call("GET", "/internal/health", request_id=None)
    assert response.status_code == 400
    assert error_of(response)["code"] == "bad_request"


async def test_the_docs_and_openapi_routes_do_not_exist(agent: Agent) -> None:
    for path in ("/docs", "/redoc", "/openapi.json", "/internal/nothing"):
        response = await agent.call("GET", path)
        assert response.status_code == 404
        assert error_of(response)["code"] == "not_found"


# --------------------------------------------------------------------------------------
# Provision (ADR-039)
# --------------------------------------------------------------------------------------


async def test_provisioning_returns_the_derived_address_and_its_public_derivation(
    agent: Agent,
) -> None:
    response = await agent.post(RUN, "provision", provision_body())
    assert response.status_code == 200
    body = response.json()
    assert body["provisioned"] is True
    assert body["key_derivation"] == {
        "scheme": "agent-negotiation-sandbox/participant-key/v1",
        "chain_id": 31337,
        "role": "buyer",
        "run_id": RUN,
    }
    assert body["policy_version"] == "det-1.0.0"
    assert body["prompt_template_version"] is None
    assert body["my_address"].startswith("0x") and len(body["my_address"]) == 42


async def test_provisioning_is_idempotent(agent: Agent) -> None:
    first = await agent.post(RUN, "provision", provision_body())
    second = await agent.post(RUN, "provision", provision_body())
    assert second.status_code == 200
    assert second.json() == first.json()


async def test_a_reprovisioning_with_the_stored_address_is_the_same_provisioning(
    agent: Agent,
) -> None:
    address = await provisioned(agent)
    again = await agent.post(RUN, "provision", provision_body(expected_address=address))
    assert again.status_code == 200
    assert again.json()["my_address"] == address


async def test_a_restarted_instance_reproduces_the_stored_address(agent: Agent) -> None:
    """Architecture 11: the controller re-provisions a restarted agent from mandate_versions."""
    address = await provisioned(agent)
    app = create_app(settings(), environ={"BUYER_ROOT_KEY": ROOT})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent-a"
    ) as client:
        restarted = Agent(client)
        response = await restarted.post(RUN, "provision", provision_body(expected_address=address))
        assert response.status_code == 200
        assert response.json()["my_address"] == address


async def test_a_derivation_that_misses_the_stored_address_is_refused_and_not_kept(
    agent: Agent,
) -> None:
    wrong = "0x" + "ee" * 20
    response = await agent.post(RUN, "provision", provision_body(expected_address=wrong))
    assert response.status_code == 409
    error = error_of(response)
    assert error["code"] == "address_mismatch"
    assert error["details"]["expected"].lower() == wrong
    assert (await agent.post(RUN, "setup-approval", _fees())).status_code == 409


async def test_the_same_run_with_a_different_body_is_an_idempotency_conflict(
    agent: Agent,
) -> None:
    await provisioned(agent)
    changed = provision_body(allowance_minor="100000000")
    response = await agent.post(RUN, "provision", changed)
    assert response.status_code == 409
    assert error_of(response)["code"] == "idempotency_conflict"


async def test_another_instances_root_is_refused(agent: Agent) -> None:
    response = await agent.post(RUN, "provision", provision_body(key_ref="env:SELLER_ROOT_KEY"))
    assert response.status_code == 409
    error = error_of(response)
    assert error["code"] == "key_ref_mismatch"
    assert error["details"] == {"expected": "env:BUYER_ROOT_KEY", "received": "env:SELLER_ROOT_KEY"}


async def test_the_other_role_is_refused(agent: Agent) -> None:
    response = await agent.post(RUN, "provision", provision_body(role="seller"))
    assert response.status_code == 422
    assert error_of(response)["details"]["fields"] == {"role": "this instance is the buyer"}


async def test_a_policy_this_instance_does_not_offer_is_refused(agent: Agent) -> None:
    response = await agent.post(RUN, "provision", provision_body(policy="model"))
    assert response.status_code == 422
    assert error_of(response)["details"]["fields"] == {"policy": "one of: deterministic"}


@pytest.mark.parametrize(
    ("mandate", "field"),
    [
        (
            {**MANDATES["buyer"], "reservation_price_minor": 100000000},
            "mandate/reservation_price_minor",
        ),
        ({**MANDATES["buyer"], "leak": "x"}, "mandate"),
        (
            {**MANDATES["buyer"], "reservation_price_minor": "0100000000"},
            "mandate/reservation_price_minor",
        ),
    ],
    ids=["amount as a number", "an extra field", "leading zero"],
)
async def test_a_malformed_mandate_is_refused_without_echoing_it(
    agent: Agent, mandate: dict[str, Any], field: str
) -> None:
    response = await agent.post(RUN, "provision", provision_body(mandate=mandate))
    assert response.status_code == 422
    assert field in error_of(response)["details"]["fields"]
    assert "100000000" not in response.text
    assert MANDATES["buyer"]["instructions"] not in response.text


async def test_a_mandate_amount_with_a_trailing_newline_is_refused(agent: Agent) -> None:
    """The schema's loose `$` admits it; the value object does not."""
    mandate = {**MANDATES["buyer"], "reservation_price_minor": "100000000\n"}
    response = await agent.post(RUN, "provision", provision_body(mandate=mandate))
    assert response.status_code == 422
    assert "100000000" not in response.text


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"allowance_minor": 250000000}, "allowance_minor"),
        ({"surplus": True}, "surplus"),
        ({"mandate_version_id": "not-a-uuid"}, "mandate_version_id"),
    ],
)
async def test_the_body_is_parsed_strictly(
    agent: Agent, change: dict[str, Any], field: str
) -> None:
    response = await agent.post(RUN, "provision", provision_body(**change))
    assert response.status_code == 422
    assert field in error_of(response)["details"]["fields"]


async def test_a_same_token_session_cannot_be_provisioned(agent: Agent) -> None:
    session = provision_body()["expected_session"]
    session["quote_token"] = session["base_token"]
    response = await agent.post(RUN, "provision", provision_body(expected_session=session))
    assert response.status_code == 422
    assert "ADR-037" in response.text


async def test_a_body_that_is_not_json_is_a_bad_request(agent: Agent) -> None:
    response = await agent.post(RUN, "provision", raw=b"{not json")
    assert response.status_code == 400


async def test_a_run_id_that_is_not_a_uuid_is_refused(agent: Agent) -> None:
    response = await agent.post("run-1", "provision", provision_body())
    assert response.status_code == 422
    assert error_of(response)["details"]["fields"] == {"run_id": "a UUID"}


# --------------------------------------------------------------------------------------
# Approve session (docs/protocol.md section 3, ADR-044)
# --------------------------------------------------------------------------------------


async def test_a_matching_session_is_approved_with_its_config_hash(agent: Agent) -> None:
    address = await provisioned(agent)
    response = await agent.post(RUN, "approve-session", opened_body(address))
    assert response.status_code == 200
    assert response.json() == {"approved": True, "config_hash": _config_hash(address)}


async def test_approving_the_same_session_again_is_idempotent(agent: Agent) -> None:
    address = await approved(agent)
    again = await agent.post(RUN, "approve-session", opened_body(address))
    assert again.status_code == 200


async def test_a_session_that_differs_from_the_expectation_is_named_field_by_field(
    agent: Agent,
) -> None:
    address = await provisioned(agent)
    response = await agent.post(RUN, "approve-session", opened_body(address, max_offers=9))
    assert response.status_code == 409
    error = error_of(response)
    assert error["code"] == "session_mismatch"
    assert error["details"]["fields"]["max_offers"] == {"expected": 8, "received": 9}


async def test_a_session_whose_hash_is_not_the_recomputed_one_is_refused(agent: Agent) -> None:
    address = await provisioned(agent)
    forged = opened_body(address, config_hash="0x" + "44" * 32)
    response = await agent.post(RUN, "approve-session", forged)
    assert set(error_of(response)["details"]["fields"]) == {"config_hash"}


async def test_a_second_different_session_for_an_approved_run_is_refused(agent: Agent) -> None:
    address = await approved(agent)
    other = opened_body(address, session_id="0x" + "77" * 32)
    response = await agent.post(RUN, "approve-session", other)
    assert response.status_code == 409
    fields = error_of(response)["details"]["fields"]
    assert set(fields) == {"session_id", "config_hash"}


async def test_a_session_cannot_be_approved_before_provisioning(agent: Agent) -> None:
    response = await agent.post(RUN, "approve-session", opened_body(COUNTERPARTY))
    assert response.status_code == 409
    assert error_of(response)["details"] == {
        "state": "unprovisioned",
        "allowed_from": ["provisioned", "approved"],
    }


# --------------------------------------------------------------------------------------
# Setup approval (ADR-040, ADR-042)
# --------------------------------------------------------------------------------------


def _fees(**overrides: Any) -> dict[str, Any]:
    fees: dict[str, Any] = {
        "nonce": 0,
        "gas_limit": 70000,
        "max_fee_per_gas_wei": "2000000000",
        "max_priority_fee_per_gas_wei": "1000000000",
    }
    fees.update(overrides)
    return fees


async def test_the_setup_approval_is_the_buyers_quote_token_from_its_run_address(
    agent: Agent,
) -> None:
    address = await provisioned(agent)
    response = await agent.post(RUN, "setup-approval", _fees())
    assert response.status_code == 200
    body = response.json()
    assert body["from"] == address
    assert body["token"] == FIXTURE["deployment"]["quote_token"]
    assert body["spender"] == FIXTURE["domain"]["verifying_contract"]
    assert body["amount_minor"] == "250000000"
    assert body["nonce"] == 0
    assert (await agent.post(RUN, "setup-approval", _fees())).json() == body


async def test_a_gas_limit_above_the_bound_is_refused(agent: Agent) -> None:
    await provisioned(agent)
    response = await agent.post(RUN, "setup-approval", _fees(gas_limit=100_001))
    assert response.status_code == 422
    assert set(error_of(response)["details"]["fields"]) == {"gas_limit"}


async def test_the_gas_bound_is_configurable() -> None:
    app = create_app(settings(setup_gas_limit_max=60_000), environ={"BUYER_ROOT_KEY": ROOT})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent-a"
    ) as client:
        agent = Agent(client)
        await provisioned(agent)
        response = await agent.post(RUN, "setup-approval", _fees(gas_limit=70_000))
        assert response.status_code == 422


async def test_a_fee_given_as_a_number_is_refused(agent: Agent) -> None:
    await provisioned(agent)
    response = await agent.post(RUN, "setup-approval", _fees(max_fee_per_gas_wei=2000000000))
    assert response.status_code == 422


async def test_no_setup_approval_before_provisioning(agent: Agent) -> None:
    response = await agent.post(RUN, "setup-approval", _fees())
    assert response.status_code == 409
    assert error_of(response)["code"] == "invalid_state"


# --------------------------------------------------------------------------------------
# Turn (ADR-012)
# --------------------------------------------------------------------------------------


async def test_the_first_turn_signs_the_buyers_opening_offer(agent: Agent) -> None:
    address = await approved(agent)
    response = await agent.post(RUN, "turn", turn_body(address))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "signed"
    assert body["signed_action"]["kind"] == "offer"
    assert body["signed_action"]["signer"] == address
    assert body["signed_action"]["typed_message"]["quoteAmount"] == "80000000"
    assert body["signed_action"]["typed_message"]["sessionId"] == SESSION_ID
    assert body["failure"] is None


async def test_the_decision_records_the_hash_of_the_observation_with_the_mandate(
    agent: Agent,
) -> None:
    address = await approved(agent)
    request = turn_body(address)
    body = (await agent.post(RUN, "turn", request)).json()
    document = {k: v for k, v in request.items() if k not in ("turn", "deadline_at")}
    document["mandate"] = MANDATES["buyer"]
    assert body["decisions"][0]["observation_hash"] == json_sha256(document)


async def test_the_same_turn_is_answered_once(agent: Agent) -> None:
    address = await approved(agent)
    first = await agent.post(RUN, "turn", turn_body(address))
    second = await agent.post(RUN, "turn", turn_body(address))
    assert second.json() == first.json()


async def test_the_same_turn_for_a_different_observation_is_a_conflict(agent: Agent) -> None:
    address = await approved(agent)
    await agent.post(RUN, "turn", turn_body(address))
    changed = turn_body(address, chain_time=CHAIN_TIME + 5)
    response = await agent.post(RUN, "turn", changed)
    assert response.status_code == 409
    assert error_of(response)["code"] == "idempotency_conflict"


async def test_no_turn_before_the_session_is_approved(agent: Agent) -> None:
    address = await provisioned(agent)
    response = await agent.post(RUN, "turn", turn_body(address))
    assert response.status_code == 409
    assert error_of(response)["details"] == {"state": "provisioned", "allowed_from": ["approved"]}


async def test_a_turn_that_carries_a_mandate_is_refused(agent: Agent) -> None:
    """The backend sends a mandate once, at provisioning; one arriving with a turn is a leak."""
    address = await approved(agent)
    body = {**turn_body(address), "mandate": MANDATES["seller"]}
    response = await agent.post(RUN, "turn", body)
    assert response.status_code == 422
    assert set(error_of(response)["details"]["fields"]) == {"mandate"}
    assert MANDATES["seller"]["reservation_price_minor"] not in response.text


@pytest.mark.parametrize(
    ("changes", "field"),
    [
        ({"run_id": OTHER_RUN}, "run_id"),
        ({"role": "seller"}, "role"),
        ({"my_address": COUNTERPARTY}, "my_address"),
    ],
)
async def test_an_observation_of_another_run_or_party_is_refused(
    agent: Agent, changes: dict[str, Any], field: str
) -> None:
    address = await approved(agent)
    body = turn_body(address, **changes)
    if field == "role":
        body["my_balances"] = BALANCES["seller"]
    response = await agent.post(RUN, "turn", body)
    assert response.status_code == 409
    assert field in error_of(response)["details"]["fields"]


async def test_an_observation_of_another_session_is_refused(agent: Agent) -> None:
    address = await approved(agent)
    body = turn_body(address)
    body["session"]["max_offers"] = 7
    response = await agent.post(RUN, "turn", body)
    assert response.status_code == 409
    assert set(error_of(response)["details"]["fields"]) == {"session.max_offers"}


async def test_a_turn_at_the_session_deadline_is_refused_before_any_decision(agent: Agent) -> None:
    address = await approved(agent)
    response = await agent.post(RUN, "turn", turn_body(address, chain_time=EXPIRES_AT))
    assert response.status_code == 409
    assert error_of(response)["details"]["state"] == "session_deadline_passed"


async def test_an_observation_outside_the_allowlist_is_refused(agent: Agent) -> None:
    address = await approved(agent)
    body = turn_body(address)
    body["session"]["opponent_reservation_minor"] = "90000000"
    response = await agent.post(RUN, "turn", body)
    assert response.status_code == 422
    assert error_of(response)["details"]["fields"] == {"session": "fails additionalProperties"}
    assert "90000000" not in response.text


async def test_an_amount_the_schema_admits_but_its_type_refuses_is_refused(agent: Agent) -> None:
    address = await approved(agent)
    body = turn_body(address, my_balances={"base_minor": "0", "quote_minor": "250000000\n"})
    response = await agent.post(RUN, "turn", body)
    assert response.status_code == 422
    assert "250000000" not in response.text


@pytest.mark.parametrize(
    ("changes", "fields"),
    [
        ({"turn": 0}, {"turn"}),
        ({"turn": True}, {"turn"}),
        ({"deadline_at": "2026-09-30T12:00:45"}, {"deadline_at"}),
        ({"deadline_at": "yesterdayZ"}, {"deadline_at"}),
        ({"turn": "1", "deadline_at": 5}, {"turn", "deadline_at"}),
    ],
)
async def test_the_turn_envelope_is_checked(
    agent: Agent, changes: dict[str, Any], fields: set[str]
) -> None:
    address = await approved(agent)
    response = await agent.post(RUN, "turn", {**turn_body(address), **changes})
    assert response.status_code == 422
    assert set(error_of(response)["details"]["fields"]) == fields


async def test_a_turn_body_that_is_not_an_object_is_refused(agent: Agent) -> None:
    await approved(agent)
    response = await agent.post(RUN, "turn", [1, 2])
    assert response.status_code == 422


# --------------------------------------------------------------------------------------
# Release
# --------------------------------------------------------------------------------------


async def test_release_discards_the_run_and_refuses_it_from_then_on(agent: Agent) -> None:
    address = await approved(agent)
    response = await agent.post(RUN, "release")
    assert response.status_code == 200
    assert response.json() == {"released": True}
    for route, body in (
        ("turn", turn_body(address)),
        ("setup-approval", _fees()),
        ("approve-session", opened_body(address)),
        ("provision", provision_body()),
    ):
        refused = await agent.post(RUN, route, body)
        assert refused.status_code == 409, route
        assert error_of(refused)["details"]["state"] == "released"


async def test_release_is_idempotent_and_accepts_an_empty_object(agent: Agent) -> None:
    await provisioned(agent)
    assert (await agent.post(RUN, "release")).status_code == 200
    assert (await agent.post(RUN, "release", {})).status_code == 200
    assert (await agent.post(OTHER_RUN, "release")).status_code == 200


async def test_release_takes_no_body(agent: Agent) -> None:
    response = await agent.post(RUN, "release", {"reason": "done"})
    assert response.status_code == 422


# --------------------------------------------------------------------------------------
# A signer that did not load
# --------------------------------------------------------------------------------------


@pytest.fixture
async def unsigned_agent() -> AsyncIterator[Agent]:
    app = create_app(settings(), environ={})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent-a"
    ) as client:
        yield Agent(client)


async def test_a_missing_root_is_reported_by_health_and_refused_by_provision(
    unsigned_agent: Agent,
) -> None:
    health = await unsigned_agent.call("GET", "/internal/health")
    assert health.json()["signer_ok"] is False
    response = await unsigned_agent.post(RUN, "provision", provision_body())
    assert response.status_code == 503
    error = error_of(response)
    assert error["code"] == "dependency_unavailable"
    assert error["details"] == {
        "dependency": "signer",
        "reason": "the environment variable BUYER_ROOT_KEY is not set",
    }


# --------------------------------------------------------------------------------------
# Nothing private reaches a log line
# --------------------------------------------------------------------------------------


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    configure_logging(level="DEBUG", instance="agent-a", role="buyer", stream=stream)
    yield stream
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


@pytest.fixture
async def logged_agent(log_stream: io.StringIO) -> AsyncIterator[Agent]:
    """An agent created AFTER logging is configured, so its start-up lines are captured too.

    The adversarial review found the first version of the scan below asked for `agent` before
    `log_stream`: the app had already written its start-up line to an unconfigured logger, and an
    agent that logged its root there passed every test.
    """
    app = create_app(settings(), environ={"BUYER_ROOT_KEY": ROOT})
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent-a"
    ) as client:
        yield Agent(client)


PRIVATE_VALUES = (
    MANDATES["buyer"]["reservation_price_minor"],
    MANDATES["buyer"]["instructions"],
    SECRET,
    ROOT[2:],
    ROOT[2:].upper(),
    "quote_amount_minor",
)


async def test_a_whole_run_logs_no_mandate_value_no_secret_and_no_body(
    logged_agent: Agent, log_stream: io.StringIO
) -> None:
    agent = logged_agent
    address = await approved(agent)
    await agent.post(RUN, "setup-approval", _fees())
    await agent.post(RUN, "turn", turn_body(address))
    await agent.post(RUN, "provision", provision_body(mandate={**MANDATES["buyer"], "x": 1}))
    await agent.post(RUN, "release")
    text = log_stream.getvalue()
    lines = [json.loads(line) for line in text.splitlines() if line.strip()]
    assert any(line.get("event") == "started" for line in lines), "start-up was not captured"
    assert any(line.get("event") == "request" and line.get("status") == 200 for line in lines)
    for value in PRIVATE_VALUES:
        assert value not in text
    request_lines = [line for line in lines if line.get("event") == "request"]
    assert {line["run_id"] for line in request_lines} == {RUN}
    assert all(
        set(line) >= {"request_id", "status", "path", "latency_ms"} for line in request_lines
    )


# --------------------------------------------------------------------------------------
# An unexpected failure
# --------------------------------------------------------------------------------------


async def test_an_unexpected_error_is_a_500_that_says_nothing_else(
    logged_agent: Agent, log_stream: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent.service import AgentService

    def explode(self: AgentService) -> dict[str, Any]:
        raise RuntimeError("reservation_price_minor=100000000")

    monkeypatch.setattr(AgentService, "health", explode)
    response = await logged_agent.call("GET", "/internal/health", request_id="req-9")
    assert response.status_code == 500
    error = error_of(response)
    assert error["code"] == "internal_error"
    assert error["request_id"] == "req-9"
    assert "100000000" not in response.text
    logged = log_stream.getvalue()
    assert '"error_type": "RuntimeError"' in logged
    assert "100000000" not in logged


# --------------------------------------------------------------------------------------
# What the adversarial review of stage 2.2 found at this layer
# --------------------------------------------------------------------------------------


async def test_an_inconsistent_observation_is_its_own_refusal(agent: Agent) -> None:
    """ADR-046: well-formed but self-contradictory, so the controller rebuilds and retries."""
    address = await approved(agent)
    response = await agent.post(RUN, "turn", turn_body(address, expected_sequence=2))
    assert response.status_code == 422
    error = error_of(response)
    assert error["code"] == "observation_inconsistent"
    assert error["details"]["fields"] == {
        "expected_sequence": "is not the sequence after the last history entry"
    }


@pytest.mark.parametrize(
    "key_ref",
    [f"env:BUYER_ROOT_KEY={ROOT}", f"env:{ROOT}", f"keystore:{ROOT}"],
    ids=["pasted after the name", "pasted as the name", "pasted as the path"],
)
async def test_a_key_pasted_where_its_reference_belongs_is_refused_unrepeated(
    agent: Agent, key_ref: str
) -> None:
    response = await agent.post(RUN, "provision", provision_body(key_ref=key_ref))
    assert response.status_code == 422
    assert "key_ref" in error_of(response)["details"]["fields"]
    assert ROOT[2:] not in response.text


async def test_a_value_objects_own_message_is_not_returned(agent: Agent) -> None:
    """Its text quotes the input; the rule is returned instead."""
    broken = "0xF39fd6e51aad88F6F4ce6aB8827279cffFb92266"  # one letter's case flipped
    response = await agent.post(RUN, "provision", provision_body(expected_address=broken))
    assert response.status_code == 422
    assert error_of(response)["details"]["fields"] == {
        "expected_address": "is not a valid value of this field's type"
    }
    assert broken not in response.text


async def test_a_reprovisioning_whose_address_differs_is_refused(agent: Agent) -> None:
    await provisioned(agent)
    response = await agent.post(RUN, "provision", provision_body(expected_address="0x" + "ee" * 20))
    assert response.status_code == 409
    assert error_of(response)["code"] == "address_mismatch"


async def test_authentication_comes_before_the_request_id(agent: Agent) -> None:
    response = await agent.call("GET", "/internal/health", mac="0" * 64, request_id=None)
    assert response.status_code == 401


async def test_a_wrong_method_is_method_not_allowed(agent: Agent) -> None:
    response = await agent.call("GET", f"/internal/runs/{RUN}/provision")
    assert response.status_code == 405
    assert error_of(response)["code"] == "method_not_allowed"


async def test_a_trailing_slash_is_not_redirected(agent: Agent) -> None:
    response = await agent.call("GET", "/internal/health/")
    assert response.status_code == 404


async def test_a_body_nested_too_deeply_is_a_bad_request(agent: Agent) -> None:
    response = await agent.post(RUN, "provision", raw=b"[" * 100_000 + b"]" * 100_000)
    assert response.status_code == 400


@pytest.mark.parametrize(
    ("route", "body_of", "field"),
    [
        (
            "approve-session",
            lambda a: opened_body(a, expires_at_ts=2**64, config_hash="0x" + "11" * 32),
            "expires_at_ts",
        ),
        ("approve-session", lambda a: opened_body(a, opened_at_ts=2**64), "opened_at_ts"),
        ("setup-approval", lambda a: _fees(nonce=2**64 - 1), "nonce"),
    ],
)
async def test_integers_beyond_what_a_struct_or_transaction_holds_are_refused(
    agent: Agent, route: str, body_of: Any, field: str
) -> None:
    address = await provisioned(agent)
    response = await agent.post(RUN, route, body_of(address))
    assert response.status_code == 422
    assert field in error_of(response)["details"]["fields"]


async def test_a_chain_id_beyond_uint64_is_refused(agent: Agent) -> None:
    session = provision_body()["expected_session"]
    session["chain_id"] = 2**64
    response = await agent.post(RUN, "provision", provision_body(expected_session=session))
    assert response.status_code == 422


async def test_a_zero_base_amount_cannot_be_provisioned(agent: Agent) -> None:
    session = provision_body()["expected_session"]
    session["base_amount_minor"] = "0"
    response = await agent.post(RUN, "provision", provision_body(expected_session=session))
    assert response.status_code == 422
    assert "base_amount_minor must be positive" in response.text


async def test_a_deadline_with_an_offset_instead_of_z_is_refused(agent: Agent) -> None:
    address = await approved(agent)
    body = {**turn_body(address), "deadline_at": "2026-09-30T12:00:45+00:00"}
    response = await agent.post(RUN, "turn", body)
    assert set(error_of(response)["details"]["fields"]) == {"deadline_at"}


async def test_a_re_approval_names_a_changed_opening_time_and_amounts_as_strings(
    agent: Agent,
) -> None:
    address = await approved(agent)
    changed = opened_body(address, opened_at_ts=OPENED_AT + 1)
    fields = error_of(await agent.post(RUN, "approve-session", changed))["details"]["fields"]
    assert fields == {"opened_at_ts": {"expected": OPENED_AT, "received": OPENED_AT + 1}}


async def test_a_mismatched_amount_is_reported_as_a_decimal_string(agent: Agent) -> None:
    address = await provisioned(agent)
    other = opened_body(address, base_amount_minor="20000000")
    fields = error_of(await agent.post(RUN, "approve-session", other))["details"]["fields"]
    assert fields["base_amount_minor"] == {"expected": "10000000", "received": "20000000"}


def test_the_run_id_constant_is_a_uuid() -> None:
    assert str(UUID(RUN)) == RUN


# --------------------------------------------------------------------------------------
# The model policy (stage 3.2): fixture and live instances, ADR-088 and ADR-089
# --------------------------------------------------------------------------------------

MODEL_FIXTURES = Path(__file__).resolve().parents[4] / "tests" / "fixtures" / "model_responses"
MODEL_KEY = "sk-ant-api03-" + "m" * 40


@asynccontextmanager
async def instance(environ: dict[str, str] | None = None, **overrides: Any) -> AsyncIterator[Agent]:
    app = create_app(settings(**overrides), environ={"BUYER_ROOT_KEY": ROOT, **(environ or {})})
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent-a") as client:
        yield Agent(client)


def model_body(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {"model_id": "claude-sonnet-5-5", "effort": "high", **overrides}
    return provision_body(policy="model", **values)


async def health(agent: Agent) -> dict[str, Any]:
    response = await agent.call("GET", "/internal/health")
    assert response.status_code == 200
    body: dict[str, Any] = response.json()
    return body


async def test_a_fixture_instance_offers_the_model_and_says_it_is_canned() -> None:
    async with instance(model_fixtures=MODEL_FIXTURES / "default-overlap-settles") as agent:
        body = await health(agent)
    assert body["policy_kinds"] == ["deterministic", "model"]
    assert body["model_ok"] is True
    assert body["model_mode"] == "fixture"


async def test_a_live_instance_with_its_key_reports_live_without_calling_anyone() -> None:
    async with instance(
        {"BUYER_ANTHROPIC_API_KEY": MODEL_KEY}, model_key_ref="env:BUYER_ANTHROPIC_API_KEY"
    ) as agent:
        body = await health(agent)
    assert body["policy_kinds"] == ["deterministic", "model"]
    assert (body["model_ok"], body["model_mode"]) == (True, "live")


@pytest.mark.parametrize(
    ("environ", "overrides", "mode"),
    [
        ({}, {"model_key_ref": "env:BUYER_ANTHROPIC_API_KEY"}, "live"),
        ({}, {"model_fixtures": Path("/nonexistent/fixtures")}, "fixture"),
        (
            {"BUYER_ANTHROPIC_API_KEY": MODEL_KEY},
            {
                "model_key_ref": "env:BUYER_ANTHROPIC_API_KEY",
                "model_price_table": Path("/nonexistent/prices.json"),
            },
            "live",
        ),
    ],
    ids=["key_unset", "fixtures_missing", "price_table_missing"],
)
async def test_a_model_that_did_not_load_is_reported_and_refused_at_provisioning(
    environ: dict[str, str], overrides: dict[str, Any], mode: str
) -> None:
    async with instance(environ, **overrides) as agent:
        body = await health(agent)
        assert body["policy_kinds"] == ["deterministic", "model"]
        assert (body["model_ok"], body["model_mode"]) == (False, mode)
        response = await agent.post(RUN, "provision", model_body())
        assert response.status_code == 503
        assert error_of(response)["details"]["dependency"] == "model"
        # The deterministic policy is unaffected.
        assert (await agent.post(OTHER_RUN, "provision", provision_body())).status_code == 200


async def test_an_instance_with_no_model_refuses_a_model_run() -> None:
    async with instance() as agent:
        response = await agent.post(RUN, "provision", model_body())
    assert response.status_code == 422
    assert error_of(response)["details"]["fields"] == {"policy": "one of: deterministic"}


async def test_a_model_run_names_its_model_and_effort() -> None:
    async with instance(model_fixtures=MODEL_FIXTURES / "default-overlap-settles") as agent:
        response = await agent.post(RUN, "provision", model_body(model_id=None, effort=None))
    assert response.status_code == 422
    assert set(error_of(response)["details"]["fields"]) == {"model_id", "effort"}


def test_an_instance_is_live_or_fixture_never_both() -> None:
    with pytest.raises(ValueError, match="never both"):
        settings(
            model_key_ref="env:BUYER_ANTHROPIC_API_KEY",
            model_fixtures=MODEL_FIXTURES / "default-overlap-settles",
        )


async def test_a_fixture_model_turn_is_signed_with_its_usage_and_cost_recorded() -> None:
    async with instance(model_fixtures=MODEL_FIXTURES / "default-overlap-settles") as agent:
        provisioned_response = await agent.post(RUN, "provision", model_body())
        assert provisioned_response.status_code == 200
        address = provisioned_response.json()["my_address"]
        assert provisioned_response.json()["policy_version"] == "model-1.0.0"
        template_version = provisioned_response.json()["prompt_template_version"]
        assert template_version.startswith("v1.0.0+")
        assert (await agent.post(RUN, "approve-session", opened_body(address))).status_code == 200
        response = await agent.post(RUN, "turn", turn_body(address, active_offer=None))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "signed"
    assert body["signed_action"]["typed_message"]["quoteAmount"] == "90000000"
    [decision] = body["decisions"]
    assert decision["usage"] == {
        "input_tokens": 620,
        "output_tokens": 180,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 2150,
    }
    # 620 x $2 + 180 x $10 + 2,150 x $2.50 a million; the bound: 2,770 x $4 + 16,000 x $10.
    assert decision["cost_reported_usd"] == "0.008415"
    assert decision["cost_estimated_usd"] == "0.171080"
    assert decision["prompt_template_version"] == template_version
    assert decision["stop_reason"] == "end_turn"


async def test_a_model_turn_past_its_deadline_calls_nothing_and_fails_timeout() -> None:
    async with instance(model_fixtures=MODEL_FIXTURES / "default-overlap-settles") as agent:
        response = await agent.post(RUN, "provision", model_body())
        address = response.json()["my_address"]
        await agent.post(RUN, "approve-session", opened_body(address))
        body = turn_body(address, active_offer=None)
        body["deadline_at"] = "2020-01-01T00:00:00.000Z"
        response = await agent.post(RUN, "turn", body)
    assert response.status_code == 200
    assert response.json()["status"] == "model_failed"
    assert response.json()["failure"]["code"] == "timeout"
    assert response.json()["decisions"] == []


# --------------------------------------------------------------------------------------
# What the adversarial review of stage 3.2 found at this layer
# --------------------------------------------------------------------------------------


async def test_an_answer_that_cannot_be_encoded_is_logged_as_the_500_it_is(
    logged_agent: Agent, log_stream: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The request line was written with status 200 before the body failed to render."""
    from agent.service import AgentService

    def unencodable(self: AgentService) -> dict[str, Any]:
        return {"status": "ok", "note": "\ud800"}

    monkeypatch.setattr(AgentService, "health", unencodable)
    response = await logged_agent.call("GET", "/internal/health", request_id="req-10")
    assert response.status_code == 500
    request_lines = [
        json.loads(line)
        for line in log_stream.getvalue().splitlines()
        if '"event": "request"' in line and "req-10" in line
    ]
    assert [line["status"] for line in request_lines] == [500]


async def test_a_model_run_without_its_model_is_refused_422_before_a_missing_model_503() -> None:
    """api_contract 6's order: the malformed request first, then the instance's dependency."""
    async with instance(model_fixtures=Path("/nonexistent/fixtures")) as agent:
        response = await agent.post(RUN, "provision", model_body(model_id=None))
    assert response.status_code == 422
    assert set(error_of(response)["details"]["fields"]) == {"model_id"}


async def test_a_prompt_template_that_does_not_load_leaves_the_model_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from agent.prompting import PromptTemplate, PromptTemplateError

    def broken(*args: Any, **kwargs: Any) -> Any:
        raise PromptTemplateError("prompt template v1.0.0 has an unknown placeholder")

    monkeypatch.setattr(PromptTemplate, "load", broken)
    async with instance(model_fixtures=MODEL_FIXTURES / "default-overlap-settles") as agent:
        body = await health(agent)
        response = await agent.post(RUN, "provision", model_body())
    assert (body["model_ok"], body["model_mode"]) == (False, "fixture")
    assert response.status_code == 503
    assert error_of(response)["details"] == {
        "dependency": "model",
        "reason": "prompt template v1.0.0 has an unknown placeholder",
    }


def test_an_empty_fixture_directory_setting_is_unset() -> None:
    """infra/.env.example ships `AGENT_MODEL_FIXTURES=`; that is no fixtures, never `Path(".")`."""
    assert settings(model_fixtures="").model_fixtures is None


# --------------------------------------------------------------------------------------
# The outbound-context assertion over HTTP (stage 3.3, ADR-092)
# --------------------------------------------------------------------------------------


@asynccontextmanager
async def observed(
    fixtures: Path, seen: list[Mapping[str, Any]], **overrides: Any
) -> AsyncIterator[Agent]:
    """A fixture instance whose model requests, once they pass the check, land in `seen`."""
    app = create_app(
        settings(model_fixtures=fixtures, **overrides),
        environ={"BUYER_ROOT_KEY": ROOT},
        model_request_observer=seen.append,
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://agent-a") as client:
        yield Agent(client)


async def model_run(agent: Agent, mandate: Mapping[str, Any] | None = None) -> str:
    body = model_body(mandate=dict(mandate or MANDATES["buyer"]))
    response = await agent.post(RUN, "provision", body)
    assert response.status_code == 200, response.text
    address: str = response.json()["my_address"]
    assert (await agent.post(RUN, "approve-session", opened_body(address))).status_code == 200
    return address


async def test_a_model_request_carrying_the_shared_secret_is_refused_and_never_sent(
    log_stream: io.StringIO,
) -> None:
    """The exit condition's own-secret case: the operator pasted the secret into instructions."""
    seen: list[Mapping[str, Any]] = []
    mandate = {**MANDATES["buyer"], "instructions": f"Remember {SECRET} for later."}
    async with observed(MODEL_FIXTURES / "default-overlap-settles", seen) as agent:
        address = await model_run(agent, mandate)
        first = await agent.post(RUN, "turn", turn_body(address, active_offer=None))
        again = await agent.post(RUN, "turn", turn_body(address, active_offer=None))

    for response in (first, again):  # not answered, so not cached: asked again, refused again
        assert response.status_code == 422
        error = error_of(response)
        assert error["code"] == "outbound_context_refused"
        assert error["details"] == {"kind": "shared_secret"}
    assert seen == []
    lines = [json.loads(line) for line in log_stream.getvalue().splitlines() if line.strip()]
    refused = [line for line in lines if line["event"] == "outbound_context_refused"]
    assert [line["kind"] for line in refused] == ["shared_secret", "shared_secret"]
    assert SECRET not in log_stream.getvalue()


@pytest.mark.parametrize(
    ("planted", "kind"),
    [
        (lambda: derive_run_key(bytes.fromhex(ROOT[2:]), BUYER_DERIVATION).hex(), "run_key"),
        (lambda: ROOT.upper(), "instance_key"),
        (lambda: "-----BEGIN PRIVATE" + " KEY-----", "pem_private_key"),
    ],
    ids=["run_key", "root_upper_case", "pem_header"],
)
async def test_an_observation_carrying_key_material_is_refused_by_its_kind(
    planted: Any, kind: str
) -> None:
    """The backend's observation is checked too: a builder that put a key in it is caught."""
    seen: list[Mapping[str, Any]] = []
    async with observed(MODEL_FIXTURES / "default-overlap-settles", seen) as agent:
        address = await model_run(agent)
        previous = {
            "turn": 1,
            "decision": {"action": "offer", "quote_amount_minor": "1"},
            "result": "rejected",
            "feedback": f"leaked: {planted()}",
        }
        body = turn_body(address, active_offer=None, my_previous_decisions=[previous])
        response = await agent.post(RUN, "turn", body)
    assert response.status_code == 422, response.text
    assert error_of(response)["details"] == {"kind": kind}
    assert seen == []


@pytest.mark.parametrize(
    "planted",
    [
        # Split, so the repository's secret scan does not read a test value as a key.
        "sk-ant-" + "api03-written_by_the_model",
        # The stage 3.3 review: a name the feedback's own quotes turn into a keystore key.
        'ciphertext":',
    ],
    ids=["anthropic_key_shaped", "keystore_key_completed_by_quoting"],
)
async def test_a_field_the_model_names_like_a_key_is_masked_and_the_repair_goes_out(
    tmp_path: Path, planted: str
) -> None:
    """Q92: the model cannot trip the check through its own repair message."""
    usage = {"input_tokens": 600, "output_tokens": 100}
    buyer = {
        "fixture_version": "1",
        "description": "an extra field named like a key, then a valid offer",
        "responses": [
            {
                "text": json.dumps(
                    {"decision": {"action": "offer", "quote_amount_minor": "90000000"}, planted: 1}
                ),
                "usage": usage,
            },
            {
                "answer": {"decision": {"action": "offer", "quote_amount_minor": "90000000"}},
                "usage": usage,
            },
        ],
    }
    for role in ("buyer", "seller"):
        (tmp_path / f"{role}.json").write_text(json.dumps(buyer), encoding="utf-8")
    seen: list[Mapping[str, Any]] = []
    async with observed(tmp_path, seen) as agent:
        address = await model_run(agent)
        response = await agent.post(RUN, "turn", turn_body(address, active_offer=None))

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "signed"
    first = body["decisions"][0]["validation"]
    assert first["code"] == "extra_fields"
    assert planted not in first["feedback"] and "[credential-shaped text]" in first["feedback"]
    assert len(seen) == 2
    repair = seen[1]["messages"][0]["content"][1]["text"]
    assert "[credential-shaped text]" in repair and planted not in json.dumps(seen)
