"""The backend's agent client against the real agent application, served in-process.

No HTTP is mocked (docs/contributing.md section 3): the other end is the agent service's own
`create_app`, verifying with the same `agent_auth` the client signs with (ADR-041). What is checked
is the client's half of the contract: requests the agent accepts, responses parsed strictly, and
failures sorted into the two kinds the controller acts on (ADR-064).
"""

from __future__ import annotations

import json
import secrets
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from eth_utils.address import to_checksum_address
from pydantic import SecretStr

from agent.main import create_app
from agent.settings import AgentSettings
from api.agent_client import AgentRefusedError, AgentUnavailableError, HttpAgentClient
from api.config import AgentEndpoint

ROOT = "0x" + "11" * 32
SECRET = secrets.token_hex(32)
EXCHANGE = "0x" + "aa" * 20
BASE = "0x" + "bb" * 20
QUOTE = "0x" + "cc" * 20


def app() -> Any:
    settings = AgentSettings(
        role="buyer",
        instance="agent-a",
        root_key_ref="env:BUYER_ROOT_KEY",
        shared_secret=SecretStr(SECRET),
        port=8101,
    )
    return create_app(settings, environ={"BUYER_ROOT_KEY": ROOT})


def client(transport: httpx.AsyncBaseTransport, secret: str = SECRET) -> HttpAgentClient:
    endpoint = AgentEndpoint("http://agent-a", "env:BUYER_ROOT_KEY", secret.encode())
    return HttpAgentClient(endpoint, timeout_s=5, transport=transport)


@pytest.fixture
async def agent() -> AsyncIterator[HttpAgentClient]:
    instance = client(httpx.ASGITransport(app=app()))
    yield instance
    await instance.aclose()


def provision_body() -> dict[str, Any]:
    return {
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
            "exchange_address": to_checksum_address(EXCHANGE),
            "base_token": to_checksum_address(BASE),
            "quote_token": to_checksum_address(QUOTE),
            "base_amount_minor": "10000000",
            "max_offers": 8,
            "session_duration_s": 1800,
            "offer_lifetime_s": 600,
        },
        "key_ref": "env:BUYER_ROOT_KEY",
        "expected_address": None,
        "mandate_version_id": str(uuid.uuid4()),
        "mandate": {
            "reservation_price_minor": "100000000",
            "min_remaining_inventory_minor": "0",
            "instructions": "private",
        },
        "initial_balances": {"base_minor": "0", "quote_minor": "250000000"},
        "allowance_minor": "250000000",
    }


async def test_health_and_provisioning_are_accepted_and_parsed(agent: HttpAgentClient) -> None:
    health = await agent.health()
    assert (health.role, health.signer_ok, health.policy_kinds) == (
        "buyer",
        True,
        ["deterministic"],
    )
    run_id, body = uuid.uuid4(), provision_body()
    provisioned = await agent.provision(run_id, body)
    assert provisioned.key_derivation.run_id == str(run_id)
    again = await agent.provision(run_id, body | {"expected_address": provisioned.my_address})
    assert again.my_address == provisioned.my_address


async def test_a_wrong_secret_is_a_refusal(agent: HttpAgentClient) -> None:
    forged = client(httpx.ASGITransport(app=app()), secret=secrets.token_hex(32))
    with pytest.raises(AgentRefusedError) as refused:
        await forged.health()
    assert (refused.value.status, refused.value.code) == (401, "unauthorized")
    await forged.aclose()


async def test_a_forgotten_run_is_unprovisioned(agent: HttpAgentClient) -> None:
    with pytest.raises(AgentRefusedError) as refused:
        await agent.setup_approval(
            uuid.uuid4(),
            {
                "nonce": 0,
                "gas_limit": 70_000,
                "max_fee_per_gas_wei": "1",
                "max_priority_fee_per_gas_wei": "1",
            },
        )
    assert refused.value.unprovisioned
    assert not refused.value.inconsistent and not refused.value.deadline_passed


async def test_release_is_idempotent_even_for_an_unknown_run(agent: HttpAgentClient) -> None:
    await agent.release(uuid.uuid4())


class Answers(httpx.AsyncBaseTransport):
    def __init__(self, status: int, content: bytes = b"", error: Exception | None = None) -> None:
        self.status, self.content, self.error = status, content, error

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if self.error is not None:
            raise self.error
        return httpx.Response(self.status, content=self.content)


@pytest.mark.parametrize(
    "transport",
    [
        Answers(0, error=httpx.ConnectError("refused")),
        Answers(0, error=httpx.ReadTimeout("slow")),
        Answers(
            503, json.dumps({"error": {"code": "dependency_unavailable", "message": "x"}}).encode()
        ),
    ],
    ids=["refused-connection", "timeout", "503"],
)
async def test_no_usable_answer_is_unavailable(transport: httpx.AsyncBaseTransport) -> None:
    instance = client(transport)
    with pytest.raises(AgentUnavailableError):
        await instance.health()
    await instance.aclose()


@pytest.mark.parametrize(
    ("status", "content", "code"),
    [
        (200, b"not json", "malformed_response"),
        (200, b'{"status": "ok"}', "malformed_response"),
        (500, b"<html>", "malformed_response"),
        (
            422,
            b'{"error": {"code": "observation_inconsistent", "message": "m", "details": {}}}',
            "observation_inconsistent",
        ),
    ],
)
async def test_an_answer_outside_the_contract_is_a_refusal(
    status: int, content: bytes, code: str
) -> None:
    instance = client(Answers(status, content))
    with pytest.raises(AgentRefusedError) as refused:
        await instance.health()
    assert refused.value.code == code
    await instance.aclose()


async def test_a_malformed_body_is_reported_by_location_only() -> None:
    leaked = {
        "status": "ok",
        "role": "buyer",
        "instance": "a",
        "policy_kinds": [],
        "model_ok": False,
        "model_mode": None,
        "signer_ok": "secret-value",
    }
    instance = client(Answers(200, json.dumps(leaked).encode()))
    with pytest.raises(AgentRefusedError) as refused:
        await instance.health()
    assert refused.value.details == {"fields": ["signer_ok"]}
    assert "secret-value" not in str(refused.value)
    await instance.aclose()
