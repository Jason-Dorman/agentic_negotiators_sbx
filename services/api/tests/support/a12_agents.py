"""Two real agent processes in fixture mode, in the seat the controller harness gives its agents.

The isolation suite (A12) needs what the in-process `Agents` cannot give it: each agent's own log
lines, written by its own process through its own redaction, and every model request it would have
sent. So each agent here is `a12_agent.py` — the real entry point plus a capture file — on its own
port, configured as Compose configures one (`AGENT_*` variables, an `env:` root reference, a fixture
directory), and the backend reaches it over real HTTP with the real HMAC.

`plant` lets a test rewrite the body the backend sends to one agent — the observation of a turn —
and re-sign it with that agent's shared secret: a deliberately planted leak, standing in for a
backend that put the counterparty's data into an observation.
"""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Final

import httpx
from anvil_chain import free_port
from api_controller import Agents

from api.db import Party
from negotiation_protocol import AUTH_HEADER, REQUEST_ID_HEADER, agent_request_mac

LAUNCHER: Final = Path(__file__).resolve().parent / "a12_agent.py"
#: A rewrite of the body sent to an agent: given the path and the decoded body, the body to send.
Plant = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


class ProcessAgents(Agents):
    """`Agents`' seat, filled by two processes. `start` before use and `stop` after."""

    def __init__(self, directory: Path, model_fixtures: Path) -> None:
        # Deliberately not `super().__init__()`: that builds two in-process apps this class
        # replaces. The attributes the controller harness reads are set here.
        self.model_fixtures = model_fixtures
        self.directory = directory
        self.roots = {party: "0x" + secrets.token_hex(32) for party in Party}
        self.secrets = {party: secrets.token_hex(32) for party in Party}
        self.apps = {}
        self.intercept = {}
        self.rewrite = {}
        self.requests = {party: [] for party in Party}
        self.plant: dict[Party, Plant] = {}
        self.ports = {party: free_port() for party in Party}
        self._processes: dict[Party, subprocess.Popen[bytes]] = {}

    def capture_path(self, party: Party) -> Path:
        return self.directory / f"{party.value}.requests.jsonl"

    def log_path(self, party: Party) -> Path:
        return self.directory / f"{party.value}.log"

    def plant_repair_path(self, party: Party) -> Path:
        return self.directory / f"{party.value}.plant-repair.txt"

    def start(self) -> None:
        for party in Party:
            self._processes[party] = self._spawn(party)
        for party in Party:
            self._wait_for_health(party)

    def stop(self) -> None:
        for process in self._processes.values():
            process.terminate()
        for process in self._processes.values():
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover  reason: only on a wedged agent
                process.kill()

    def captured(self, party: Party) -> list[dict[str, Any]]:
        """Every model request the party's agent let through, in order."""
        path = self.capture_path(party)
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def log_text(self, party: Party) -> str:
        return self.log_path(party).read_text(encoding="utf-8")

    def transports(self) -> dict[Party, httpx.AsyncBaseTransport]:
        return {party: _ToProcess(self, party) for party in Party}

    def app(self, party: Party) -> Any:  # pragma: no cover  reason: no in-process app here
        raise NotImplementedError("the agents are processes")

    def restart(self, party: Party) -> None:  # pragma: no cover  reason: as above
        raise NotImplementedError("the agents are processes")

    def _spawn(self, party: Party) -> subprocess.Popen[bytes]:
        variable = f"{party.value.upper()}_ROOT_KEY"
        inherited = {
            name: value
            for name, value in os.environ.items()
            if not name.startswith(("AGENT_", "A12_", "ANTHROPIC_"))
            and not name.endswith("_ROOT_KEY")
            and name != "KEYSTORE_PASSWORD"
        }
        env = {
            **inherited,
            "AGENT_ROLE": party.value,
            "AGENT_INSTANCE": "agent-a" if party == Party.BUYER else "agent-b",
            "AGENT_ROOT_KEY_REF": f"env:{variable}",
            variable: self.roots[party],
            "AGENT_SHARED_SECRET": self.secrets[party],
            "AGENT_PORT": str(self.ports[party]),
            "AGENT_HOST": "127.0.0.1",
            "AGENT_LOG_LEVEL": "DEBUG",
            "AGENT_MODEL_FIXTURES": str(self.model_fixtures),
            "A12_CAPTURE": str(self.capture_path(party)),
            "A12_PLANT_REPAIR": str(self.plant_repair_path(party)),
        }
        with self.log_path(party).open("wb") as log:
            return subprocess.Popen(
                [sys.executable, str(LAUNCHER)], env=env, stdout=log, stderr=subprocess.STDOUT
            )

    def _wait_for_health(self, party: Party, timeout_s: float = 30.0) -> None:
        process = self._processes[party]
        path = "/internal/health"
        deadline = time.monotonic() + timeout_s
        with httpx.Client(base_url=f"http://127.0.0.1:{self.ports[party]}") as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise AssertionError(f"the agent exited:\n{self.log_text(party)}")
                headers = {
                    AUTH_HEADER: agent_request_mac(self.secrets[party].encode(), "GET", path, b""),
                    REQUEST_ID_HEADER: "a12-health",
                }
                try:
                    response = client.get(path, headers=headers)
                except httpx.TransportError:
                    time.sleep(0.1)
                    continue
                health = response.json()
                assert health["signer_ok"] and health["model_ok"], health
                assert health["model_mode"] == "fixture", health
                return
        raise AssertionError(f"the agent did not answer in {timeout_s}s:\n{self.log_text(party)}")


class _ToProcess(httpx.AsyncBaseTransport):
    """The backend's requests to one agent, over real HTTP, with a planted rewrite if any."""

    def __init__(self, agents: ProcessAgents, party: Party) -> None:
        self._agents = agents
        self._party = party
        self._http = httpx.AsyncHTTPTransport()

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self._agents.requests[self._party].append(request.url.path)
        content = await request.aread()
        headers = httpx.Headers(request.headers)
        plant = self._agents.plant.get(self._party)
        if plant is not None and content:
            body = await plant(request.url.path, json.loads(content))
            content = json.dumps(body).encode("utf-8")
            secret = self._agents.secrets[self._party].encode()
            headers[AUTH_HEADER] = agent_request_mac(
                secret, request.method, request.url.path, content
            )
            headers["content-length"] = str(len(content))
        url = request.url.copy_with(
            scheme="http", host="127.0.0.1", port=self._agents.ports[self._party]
        )
        forwarded = httpx.Request(request.method, url, headers=headers, content=content)
        response = await self._http.handle_async_request(forwarded)
        answer = await response.aread()
        return httpx.Response(response.status_code, headers=response.headers, content=answer)

    async def aclose(self) -> None:
        await self._http.aclose()
