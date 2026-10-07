"""The agent internal API: six routes behind HMAC (docs/api_contract.md section 6, ADR-041).

Every request is authenticated before anything else is done with it — before the run id in its
path is parsed and before its body is read as JSON — by the MAC of its method, path and raw body
under this instance's shared secret. A request that fails is answered `401`. Then `X-Request-Id` is
required, then the body is parsed, then the service decides.

Errors leave in the envelope of section 1.1 with the request id. A route never returns a stack
trace, a request body or anything from a mandate: the details of `agent.errors` are built to be safe
to return, and an unexpected exception is `500 internal_error` with the request id alone. The one
log line per request names the method, path, status, run and latency, and nothing that was said.
"""

from __future__ import annotations

import json
import time
from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal
from typing import Any
from uuid import UUID

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from starlette.exceptions import HTTPException

from agent.errors import AgentError, BadRequestError, RequestValidationError, UnauthorizedError
from agent.observation import Mandate
from agent.routes.bodies import (
    ApproveSessionBody,
    ProvisionBody,
    SetupApprovalBody,
)
from agent.service import AgentService, schema_problems
from agent.signing import ExpectedSession, FeeTerms, SessionOpened
from agent.state import Provisioning
from negotiation_protocol import (
    AUTH_HEADER,
    REQUEST_ID_HEADER,
    ProtocolValueError,
    json_sha256,
    verify_agent_request,
)

Action = Callable[[bytes, UUID | None], Awaitable[Mapping[str, Any]]]


def install_routes(app: FastAPI, service: AgentService, secret: bytes) -> None:
    routes = _Routes(service, secret)
    app.add_api_route("/internal/health", routes.health, methods=["GET"])
    for name in ("provision", "approve-session", "setup-approval", "turn", "release"):
        app.add_api_route(
            f"/internal/runs/{{run_id}}/{name}", routes.handler(name), methods=["POST"]
        )
    app.add_exception_handler(HTTPException, _http_error)


class _Routes:
    def __init__(self, service: AgentService, secret: bytes) -> None:
        self._service = service
        self._secret = secret
        self._log = structlog.get_logger(component="agent.routes")
        self._actions: dict[str, Action] = {
            "provision": self._provision,
            "approve-session": self._approve_session,
            "setup-approval": self._setup_approval,
            "turn": self._turn,
            "release": self._release,
        }

    async def health(self, request: Request) -> JSONResponse:
        async def action(body: bytes, run_id: UUID | None) -> Mapping[str, Any]:
            return self._service.health()

        return await self._serve(request, None, action)

    def handler(self, name: str) -> Callable[[Request, str], Awaitable[JSONResponse]]:
        action = self._actions[name]

        async def endpoint(request: Request, run_id: str) -> JSONResponse:
            return await self._serve(request, run_id, action)

        endpoint.__name__ = name.replace("-", "_")
        return endpoint

    async def _serve(self, request: Request, run_id: str | None, action: Action) -> JSONResponse:
        started = time.monotonic()
        request_id = request.headers.get(REQUEST_ID_HEADER, "")
        status = 500
        with structlog.contextvars.bound_contextvars(request_id=request_id, run_id=run_id):
            try:
                body = await request.body()
                self._authenticate(request, body)
                if not request_id:
                    raise BadRequestError(f"{REQUEST_ID_HEADER} is required")
                payload = await action(body, None if run_id is None else _run_id(run_id))
                response = _respond(200, dict(payload), request_id)
                # Only once the body has rendered: an answer that cannot be encoded is a 500.
                status = 200
                return response
            except AgentError as error:
                status = error.status
                return _respond(error.status, _envelope(error, request_id), request_id)
            except Exception as error:  # the top of the task: log the kind, never the message
                self._log.error("unhandled_error", error_type=type(error).__name__)
                internal = AgentError("an unexpected error occurred")
                return _respond(500, _envelope(internal, request_id), request_id)
            finally:
                self._log.info(
                    "request",
                    method=request.method,
                    path=request.url.path,
                    status=status,
                    latency_ms=round((time.monotonic() - started) * 1000),
                )

    def _authenticate(self, request: Request, body: bytes) -> None:
        presented = request.headers.get(AUTH_HEADER)
        if not verify_agent_request(
            self._secret, request.method, request.url.path, body, presented
        ):
            raise UnauthorizedError(f"missing or invalid {AUTH_HEADER}")

    # ----------------------------------------------------------------------------------
    # Actions
    # ----------------------------------------------------------------------------------

    async def _provision(self, body: bytes, run_id: UUID | None) -> Mapping[str, Any]:
        raw = _json_object(body)
        request = _parse(ProvisionBody, body)
        _check_mandate(request.mandate)
        provisioning = _provisioning(request, raw)
        return self._service.provision(_known(run_id), provisioning, request.expected_address)

    async def _approve_session(self, body: bytes, run_id: UUID | None) -> Mapping[str, Any]:
        request = _parse(ApproveSessionBody, body)
        opened = SessionOpened(
            session_id=request.session_id,
            buyer=request.buyer,
            seller=request.seller,
            base_token=request.base_token,
            quote_token=request.quote_token,
            base_amount=request.base_amount_minor,
            expires_at=request.expires_at_ts,
            max_offers=request.max_offers,
            config_hash=request.config_hash,
            opened_at=request.opened_at_ts,
        )
        return self._service.approve_session(_known(run_id), opened)

    async def _setup_approval(self, body: bytes, run_id: UUID | None) -> Mapping[str, Any]:
        request = _parse(SetupApprovalBody, body)
        terms = FeeTerms(
            nonce=request.nonce,
            gas_limit=request.gas_limit,
            max_fee_per_gas=int(request.max_fee_per_gas_wei),
            max_priority_fee_per_gas=int(request.max_priority_fee_per_gas_wei),
        )
        return self._service.setup_approval(_known(run_id), terms)

    async def _turn(self, body: bytes, run_id: UUID | None) -> Mapping[str, Any]:
        return await self._service.turn(_known(run_id), _json_object(body))

    async def _release(self, body: bytes, run_id: UUID | None) -> Mapping[str, Any]:
        if body.strip() and _json_object(body):
            raise RequestValidationError("release takes no body", {"<root>": "must be empty"})
        return self._service.release(_known(run_id))


# --------------------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------------------


def _run_id(text: str) -> UUID:
    try:
        return UUID(text)
    except ValueError:
        raise RequestValidationError("the run id is not a UUID", {"run_id": "a UUID"}) from None


def _known(run_id: UUID | None) -> UUID:
    if (
        run_id is None
    ):  # every run route has one; the health route passes None and never reaches here
        raise BadRequestError("this route needs a run id")
    return run_id


def _json_object(body: bytes) -> dict[str, Any]:
    try:
        value = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise BadRequestError("the body is not JSON") from None
    except RecursionError:
        raise BadRequestError("the body is nested too deeply to be any request here") from None
    if not isinstance(value, dict):
        raise RequestValidationError("the body must be a JSON object", {"<root>": "an object"})
    return value


def _parse[BodyT: BaseModel](model: type[BodyT], body: bytes) -> BodyT:
    _json_object(body)
    try:
        return model.model_validate_json(body)
    except ValidationError as error:
        fields = {
            ".".join(str(part) for part in item["loc"]) or "<root>": _rule(item)
            for item in error.errors(include_url=False, include_input=False)
        }
        raise RequestValidationError("the body does not match the contract", fields) from None


def _rule(item: Mapping[str, Any]) -> str:
    """Pydantic's message, except where a value object's own text would quote the value.

    `ProtocolValueError` messages name the rejected input — "'0xab…' has a broken EIP-55 checksum"
    — and an error response reaches the backend's logs. Pydantic's built-in messages name the rule.
    """
    error = (item.get("ctx") or {}).get("error")
    if isinstance(error, ProtocolValueError):
        return "is not a valid value of this field's type"
    return str(item["msg"])


def _check_mandate(mandate: Mapping[str, Any]) -> None:
    problems = schema_problems(dict(mandate), "mandate.v1.json")
    if problems:
        fields = {
            "mandate" if path == "<root>" else f"mandate/{path}": rule
            for path, rule in problems.items()
        }
        raise RequestValidationError("the mandate does not match mandate.v1.json", fields)
    try:
        Mandate.from_json(mandate)
    except ValueError:
        raise RequestValidationError(
            "the mandate holds a value its type refuses",
            {"mandate": "an amount is not a canonical base-10 string"},
        ) from None


def _provisioning(request: ProvisionBody, raw: Mapping[str, Any]) -> Provisioning:
    session = request.expected_session
    return Provisioning(
        role=request.role,
        policy=request.policy,
        model_id=request.model_id,
        effort=request.effort,
        repair_attempts=request.limits.repair_attempts,
        model_call_ceiling=request.limits.model_call_ceiling,
        model_spend_ceiling_usd=Decimal(request.limits.model_spend_ceiling_usd),
        model_timeout_s=request.limits.model_timeout_s,
        expected=ExpectedSession(
            chain_id=session.chain_id,
            exchange_address=session.exchange_address,
            base_token=session.base_token,
            quote_token=session.quote_token,
            base_amount=session.base_amount_minor,
            max_offers=session.max_offers,
            session_duration_s=session.session_duration_s,
            offer_lifetime_s=session.offer_lifetime_s,
        ),
        key_ref=request.key_ref,
        mandate_version_id=request.mandate_version_id,
        mandate_document=dict(request.mandate),
        initial_base=request.initial_balances.base_minor,
        initial_quote=request.initial_balances.quote_minor,
        allowance=request.allowance_minor,
        # `expected_address` is null on first provisioning and set on a re-provisioning; it is
        # checked, not fingerprinted, so both are the same provisioning of the same run.
        fingerprint=json_sha256({k: v for k, v in raw.items() if k != "expected_address"}),
    )


# --------------------------------------------------------------------------------------
# Responses
# --------------------------------------------------------------------------------------


def _envelope(error: AgentError, request_id: str) -> dict[str, Any]:
    return {
        "error": {
            "code": error.code,
            "message": error.message,
            "details": error.details,
            "request_id": request_id or None,
        }
    }


def _respond(status: int, content: dict[str, Any], request_id: str) -> JSONResponse:
    headers = {REQUEST_ID_HEADER: request_id} if request_id else None
    return JSONResponse(status_code=status, content=content, headers=headers)


async def _http_error(request: Request, error: Exception) -> JSONResponse:
    """Unknown paths and methods, in the same envelope as every other error.

    These are answered before authentication, because no route matched to authenticate for; they
    say only that the path or method does not exist, which the contract itself publishes.
    """
    status = error.status_code if isinstance(error, HTTPException) else 500
    code = {404: "not_found", 405: "method_not_allowed"}.get(status, "bad_request")
    envelope: dict[str, Any] = {
        "error": {"code": code, "message": "no such route", "details": {}, "request_id": None}
    }
    return JSONResponse(status_code=status, content=envelope)
