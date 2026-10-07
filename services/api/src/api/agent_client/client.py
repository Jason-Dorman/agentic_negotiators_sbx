"""The backend's side of the agent internal API (docs/api_contract.md section 6).

Every request is signed with `negotiation_protocol.agent_auth` — the function the agent verifies
with (ADR-041) — over the method, the exact path and the exact bytes sent, so the body is serialised
once and those bytes are both signed and sent. Responses are parsed strictly: an agent is an
external boundary (docs/contributing.md section 2.1), and a field the contract does not name is a
fault to report, not data to carry.

Failures leave as one of two kinds, because the controller treats them differently (ADR-064):

- **Unavailable** (`AgentUnavailableError`): no answer, a timeout, or `503` — the agent is down or
  its signer did not load. Nothing is known; the same request is tried again later.
- **Refused** (`AgentRefusedError`): the agent answered and said no, or answered with something the
  contract does not allow. Its `code` is the contract's error code. Some refusals are part of the
  protocol and the controller acts on them — `observation_inconsistent` (ADR-046), a restarted
  instance's `unprovisioned` (ADR-048), a session past its deadline — and the rest are defects.

Nothing here logs a body. A refusal's `details` names fields and rules, never values (api_contract
section 6), and that is all that is kept of it.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final, Literal, Protocol

import httpx
import structlog
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from api.config import AgentEndpoint
from negotiation_protocol import AUTH_HEADER, REQUEST_ID_HEADER, agent_request_mac

_log = structlog.get_logger(component="agent_client")

UNPROVISIONED: Final = "unprovisioned"
RELEASED: Final = "released"
SESSION_DEADLINE_PASSED: Final = "session_deadline_passed"


class AgentError(Exception):
    """Base of the agent client's errors."""


class AgentUnavailableError(AgentError):
    """No usable answer: transport failure, timeout, or `503 dependency_unavailable`."""


class AgentRefusedError(AgentError):
    """The agent answered with an error, or with a body the contract does not allow."""

    def __init__(
        self, status: int, code: str, message: str, details: Mapping[str, Any] | None = None
    ) -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.details: dict[str, Any] = dict(details or {})

    @property
    def state(self) -> str | None:
        """For `invalid_state`, the run's state on the instance."""
        if self.code != "invalid_state":
            return None
        state = self.details.get("state")
        return state if isinstance(state, str) else None

    @property
    def unprovisioned(self) -> bool:
        """A restarted instance has forgotten the run (ADR-048)."""
        return self.state == UNPROVISIONED

    @property
    def deadline_passed(self) -> bool:
        return self.state == SESSION_DEADLINE_PASSED

    @property
    def inconsistent(self) -> bool:
        return self.code == "observation_inconsistent"


# ---------------------------------------------------------------------------------------------
# Responses, as api_contract section 6 states them
# ---------------------------------------------------------------------------------------------


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class AgentHealth(_Strict):
    status: Literal["ok"]
    role: Literal["buyer", "seller"]
    instance: str
    policy_kinds: list[str]
    model_ok: bool
    #: ADR-088: `fixture` when the instance answers its model runs from canned responses.
    model_mode: Literal["live", "fixture"] | None
    signer_ok: bool


class KeyDerivation(_Strict):
    scheme: str
    chain_id: int
    role: Literal["buyer", "seller"]
    run_id: str


class Provisioned(_Strict):
    provisioned: Literal[True]
    my_address: str
    key_derivation: KeyDerivation
    policy_version: str
    prompt_template_version: str | None


class SessionApproval(_Strict):
    approved: Literal[True]
    config_hash: str


class SetupApproval(_Strict):
    raw_tx: str
    tx_hash: str
    from_: str = Field(alias="from")
    token: str
    spender: str
    amount_minor: str
    nonce: int


class Validation(_Strict):
    ok: bool
    code: str | None
    feedback: str | None


class DecisionPayload(_Strict):
    """One attempt. **Private**: `raw_response` and `validation.feedback` never leave the server
    except under explicit reveal (docs/data_model.md section 7)."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)

    attempt: int = Field(ge=1)
    raw_response: Any
    validation: Validation
    stop_reason: str | None
    usage: dict[str, int] | None
    latency_ms: int | None = Field(ge=0)
    cost_estimated_usd: str | None
    cost_reported_usd: str | None
    prompt_template_version: str | None
    observation_hash: str
    requested_at: datetime


class SignedActionPayload(_Strict):
    kind: Literal["offer", "accept", "close"]
    typed_message: dict[str, Any]
    digest: str
    signature: str
    signer: str


class TurnFailure(_Strict):
    code: str
    detail: str


class TurnResponse(_Strict):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=False)

    turn: int = Field(ge=1)
    status: Literal["signed", "model_failed", "budget_exhausted"]
    signed_action: SignedActionPayload | None
    decisions: list[DecisionPayload]
    failure: TurnFailure | None


class Released(_Strict):
    released: Literal[True]


# ---------------------------------------------------------------------------------------------
# The client
# ---------------------------------------------------------------------------------------------


class AgentClient(Protocol):
    """One agent instance. The controller and the turn executor depend on this, so their tests can
    stand a fake in for it without mocking HTTP (docs/contributing.md section 3)."""

    async def health(self) -> AgentHealth: ...

    async def provision(self, run_id: uuid.UUID, body: Mapping[str, Any]) -> Provisioned: ...

    async def approve_session(
        self, run_id: uuid.UUID, body: Mapping[str, Any]
    ) -> SessionApproval: ...

    async def setup_approval(self, run_id: uuid.UUID, body: Mapping[str, Any]) -> SetupApproval: ...

    async def turn(
        self, run_id: uuid.UUID, body: Mapping[str, Any], *, timeout_s: float
    ) -> TurnResponse: ...

    async def release(self, run_id: uuid.UUID) -> None: ...


class HttpAgentClient(AgentClient):
    """The real client. `transport` lets a test serve the real agent application in-process."""

    def __init__(
        self,
        endpoint: AgentEndpoint,
        *,
        timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._secret = endpoint.shared_secret
        self._timeout_s = timeout_s
        self._http = httpx.AsyncClient(
            base_url=endpoint.base_url, transport=transport, follow_redirects=False
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def health(self) -> AgentHealth:
        return _parse(AgentHealth, await self._call("GET", "/internal/health", None))

    async def provision(self, run_id: uuid.UUID, body: Mapping[str, Any]) -> Provisioned:
        return _parse(Provisioned, await self._call("POST", _path(run_id, "provision"), body))

    async def approve_session(self, run_id: uuid.UUID, body: Mapping[str, Any]) -> SessionApproval:
        payload = await self._call("POST", _path(run_id, "approve-session"), body)
        return _parse(SessionApproval, payload)

    async def setup_approval(self, run_id: uuid.UUID, body: Mapping[str, Any]) -> SetupApproval:
        payload = await self._call("POST", _path(run_id, "setup-approval"), body)
        return _parse(SetupApproval, payload)

    async def turn(
        self, run_id: uuid.UUID, body: Mapping[str, Any], *, timeout_s: float
    ) -> TurnResponse:
        payload = await self._call("POST", _path(run_id, "turn"), body, timeout_s=timeout_s)
        return _parse(TurnResponse, payload)

    async def release(self, run_id: uuid.UUID) -> None:
        _parse(Released, await self._call("POST", _path(run_id, "release"), None))

    async def _call(
        self,
        method: str,
        path: str,
        body: Mapping[str, Any] | None,
        *,
        timeout_s: float | None = None,
    ) -> Any:
        content = b"" if body is None else json.dumps(dict(body)).encode("utf-8")
        headers = {
            AUTH_HEADER: agent_request_mac(self._secret, method, path, content),
            REQUEST_ID_HEADER: str(uuid.uuid4()),
            "content-type": "application/json",
        }
        try:
            response = await self._http.request(
                method,
                path,
                content=content,
                headers=headers,
                timeout=timeout_s or self._timeout_s,
            )
        except httpx.TransportError as error:
            _log.warning("agent.unavailable", path=path, error=type(error).__name__)
            raise AgentUnavailableError(f"{method} {path}: {type(error).__name__}") from None
        if response.status_code == 200:
            try:
                return response.json()
            except ValueError:
                raise AgentRefusedError(200, "malformed_response", "not JSON") from None
        raise _refusal(method, path, response)


def _path(run_id: uuid.UUID, route: str) -> str:
    return f"/internal/runs/{run_id}/{route}"


def _refusal(method: str, path: str, response: httpx.Response) -> AgentError:
    try:
        error = response.json()["error"]
        code, message = str(error["code"]), str(error["message"])
        details = error.get("details") or {}
    except (ValueError, KeyError, TypeError):
        code, message, details = "malformed_response", "no error envelope", {}
    _log.warning("agent.refused", path=path, status=response.status_code, code=code)
    if response.status_code == 503:
        return AgentUnavailableError(f"{method} {path}: {code}")
    return AgentRefusedError(response.status_code, code, message, details)


def _parse[T: BaseModel](model: type[T], payload: Any) -> T:
    try:
        return model.model_validate(payload)
    except ValidationError as error:
        # The locations only: a payload can carry a mandate-derived value (a raw response).
        fields = sorted({".".join(str(part) for part in item["loc"]) for item in error.errors()})
        raise AgentRefusedError(
            200,
            "malformed_response",
            f"{model.__name__} does not match the contract",
            {"fields": fields},
        ) from None
