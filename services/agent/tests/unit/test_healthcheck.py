"""`python -m agent.healthcheck`, the container probe: against the real agent application served
by uvicorn, it passes only when the instance answers with its signer loaded, and only with the
instance's own shared secret."""

from __future__ import annotations

import asyncio
import secrets
import socket
from collections.abc import AsyncIterator

import pytest
import uvicorn
from pydantic import SecretStr

from agent import healthcheck
from agent.main import create_app
from agent.settings import AgentSettings

SECRET = secrets.token_hex(32)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


async def _serve(environ: dict[str, str]) -> AsyncIterator[int]:
    port = _free_port()
    settings = AgentSettings(
        role="buyer",
        instance="agent-a",
        root_key_ref="env:BUYER_ROOT_KEY",
        shared_secret=SecretStr(SECRET),
        port=port,
    )
    server = uvicorn.Server(
        uvicorn.Config(create_app(settings, environ=environ), port=port, log_config=None)
    )
    task = asyncio.create_task(server.serve())
    while not server.started:  # noqa: ASYNC110  reason: uvicorn offers a flag, no event
        await asyncio.sleep(0.01)
    try:
        yield port
    finally:
        server.should_exit = True
        await task


@pytest.fixture
async def loaded() -> AsyncIterator[int]:
    async for port in _serve({"BUYER_ROOT_KEY": "0x" + secrets.token_hex(32)}):
        yield port


@pytest.fixture
async def unloaded() -> AsyncIterator[int]:
    async for port in _serve({}):
        yield port


async def test_a_signing_instance_is_healthy_with_its_own_secret(loaded: int) -> None:
    assert await asyncio.to_thread(healthcheck.probe, loaded, SECRET.encode())
    assert not await asyncio.to_thread(healthcheck.probe, loaded, b"x" * 64)


async def test_an_instance_whose_signer_did_not_load_is_unhealthy(unloaded: int) -> None:
    assert not await asyncio.to_thread(healthcheck.probe, unloaded, SECRET.encode())


def test_nothing_listening_is_unhealthy(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENT_PORT", str(_free_port()))
    monkeypatch.setenv("AGENT_SHARED_SECRET", SECRET)
    assert healthcheck.main() == 1
    monkeypatch.delenv("AGENT_SHARED_SECRET")
    assert healthcheck.main() == 1
