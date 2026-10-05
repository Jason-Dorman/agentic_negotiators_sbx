"""The error envelope of api_contract section 1.1, and the one place exceptions become responses.

Every domain exception the controller raises carries its API code and status (`ControllerError`), so
this module maps an exception to a response without knowing the operation that raised it. Nothing
here quotes a request value: a run request carries mandates, and FastAPI's own validation messages
repeat their input, so a refusal names the field's location and the rule it broke and nothing more
(docs/contributing.md section 3).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, ClassVar

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from api.config import InvalidRunConfigError
from api.controller import ControllerError
from api.db.errors import NotFoundError

_log = structlog.get_logger(component="routes")


class ApiError(Exception):
    """A refusal the route layer makes itself, with the code of api_contract section 7."""

    code: ClassVar[str] = "internal_error"
    status: ClassVar[int] = 500

    def __init__(self, message: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details or {})


class BadRequestError(ApiError):
    code = "bad_request"
    status = 400


class UnauthorizedError(ApiError):
    code = "unauthorized"
    status = 401


class RevealRequiredError(ApiError):
    code = "reveal_required"
    status = 403


class RouteNotFoundError(ApiError):
    code = "not_found"
    status = 404


class IdempotencyConflictError(ApiError):
    code = "idempotency_conflict"
    status = 409


def request_id(request: Request) -> str:
    return str(getattr(request.state, "request_id", ""))


def envelope(
    code: str, message: str, request: Request, details: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "error": {
            "code": code,
            "message": message,
            "details": dict(details or {}),
            "request_id": request_id(request),
        }
    }


def error_response(
    status: int,
    code: str,
    message: str,
    request: Request,
    details: Mapping[str, Any] | None = None,
) -> JSONResponse:
    return JSONResponse(envelope(code, message, request, details), status_code=status)


def _fields(error: RequestValidationError) -> dict[str, str]:
    """Each failing location and the rule it broke; never the value."""
    fields: dict[str, str] = {}
    for item in error.errors():
        location = [str(part) for part in item.get("loc", ()) if part != "body"]
        fields[".".join(location) or "<root>"] = str(item.get("type", "invalid"))
    return fields


async def _validation(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, RequestValidationError)
    if any(item.get("type") == "json_invalid" for item in error.errors()):
        return error_response(400, "bad_request", "the body is not valid JSON", request)
    return error_response(
        422, "validation_error", "the request is invalid", request, {"fields": _fields(error)}
    )


async def _api_error(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, ApiError | ControllerError)
    if error.status >= 500:
        _log.error("request.failed", code=error.code, request_id=request_id(request))
    return error_response(error.status, error.code, error.message, request, error.details)


async def _not_found(request: Request, error: Exception) -> JSONResponse:
    return error_response(404, "not_found", str(error), request)


async def _invalid_config(request: Request, error: Exception) -> JSONResponse:
    return error_response(
        422,
        "validation_error",
        str(error),
        request,
        {"fields": {"public_config.confirmation_threshold": "invalid"}},
    )


async def _http(request: Request, error: Exception) -> JSONResponse:
    assert isinstance(error, StarletteHTTPException)
    if error.status_code == 405:
        return error_response(405, "method_not_allowed", "method not allowed", request)
    if error.status_code == 404:
        return error_response(404, "not_found", "no such route", request)
    return error_response(error.status_code, "bad_request", str(error.detail), request)


async def _unexpected(request: Request, error: Exception) -> JSONResponse:
    """Docs/contributing.md section 2.1: logged here, at the top; the client learns only the
    request id to quote."""
    _log.exception("request.unhandled", request_id=request_id(request))
    return error_response(500, "internal_error", "internal error", request)


def install(app: FastAPI) -> None:
    app.add_exception_handler(RequestValidationError, _validation)
    app.add_exception_handler(ApiError, _api_error)
    app.add_exception_handler(ControllerError, _api_error)
    app.add_exception_handler(NotFoundError, _not_found)
    app.add_exception_handler(InvalidRunConfigError, _invalid_config)
    app.add_exception_handler(StarletteHTTPException, _http)
    app.add_exception_handler(Exception, _unexpected)
