"""The FastAPI application: routers, the error envelope, the request id and the operator token.

`create_app` takes the settings, and the services when they are already built — by a fixture in a
test; `api.main` builds them in the application's lifespan and sets `app.state.services` there — so
the application is the same object either way. Nothing imports this package (`.importlinter`,
`routes-are-a-leaf`).

**The operator token (ADR-028).** When `OPERATOR_TOKEN` is set, every route but `GET /v1/health`
requires `Authorization: Bearer <token>`, compared in constant time, and anything else is
`401 unauthorized`. The token is a single shared credential for one operator on one host; exposing
the API beyond the host needs the review in docs/security_and_trust_boundaries.md section 8.1 first.

**Logging.** One line per request, a failed one included: method, route, status, duration and
request id. Never a body, a header value or a query string — a query can carry `include_private`, a
body a mandate. An unhandled exception is caught here and answered `500 internal_error` with the
request id; its log line names its type and frames, never its message (`api.logs`).
"""

from __future__ import annotations

import hmac
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from typing import Final

import structlog
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse

from api.config import ApiSettings
from api.routes import errors, runs, sse, system
from api.routes.services import Services

_log = structlog.get_logger(component="routes")

API_VERSION: Final = "0.1.0"
_OPEN_PATHS: Final = frozenset({"/v1/health"})
_REQUEST_ID: Final = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _request_id(request: Request) -> str:
    """The caller's `X-Request-Id` when it is a plain identifier, else a fresh one."""
    offered = request.headers.get("X-Request-Id", "")
    return offered if _REQUEST_ID.match(offered) else str(uuid.uuid4())


def _authorised(request: Request, token: str | None) -> bool:
    if token is None or request.url.path in _OPEN_PATHS:
        return True
    scheme, _, offered = request.headers.get("Authorization", "").partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(
        offered.strip().encode(), token.encode()
    )


def create_app(
    settings: ApiSettings,
    services: Services | None = None,
    *,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    app = FastAPI(
        title="Agent Negotiation Sandbox — operator API",
        version=API_VERSION,
        description=(
            "The operator API of docs/api_contract.md section 2. Test assets and simulated "
            "economics only."
        ),
        lifespan=lifespan,
    )
    if services is not None:
        app.state.services = services
    errors.install(app)
    token = settings.token()

    @app.middleware("http")
    async def frame_request(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        request.state.request_id = _request_id(request)
        started = time.monotonic()
        if not _authorised(request, token):
            response: Response = JSONResponse(
                errors.envelope("unauthorized", "a valid operator token is required", request),
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        else:
            try:
                response = await call_next(request)
            except Exception:
                # The top of the request (docs/contributing.md section 2.1): logged here, with
                # the exception rendered by type and frames only (api.logs), and answered with the
                # envelope and the request id, so neither uvicorn nor Starlette logs it again.
                _log.exception("request.unhandled", request_id=request.state.request_id)
                response = JSONResponse(
                    errors.envelope("internal_error", "internal error", request), status_code=500
                )
        response.headers["X-Request-Id"] = request.state.request_id
        _log.info(
            "request",
            method=request.method,
            route=request.url.path,
            status=response.status_code,
            duration_ms=int((time.monotonic() - started) * 1000),
            request_id=request.state.request_id,
        )
        return response

    app.include_router(system.router)
    app.include_router(runs.router)
    app.include_router(sse.router)
    return app
