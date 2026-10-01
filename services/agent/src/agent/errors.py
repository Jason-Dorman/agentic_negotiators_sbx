"""The agent service's domain exceptions, one hierarchy (docs/contributing.md section 2.1).

Each carries the API error code and HTTP status of docs/api_contract.md section 7, so the route
layer maps an exception to a response in one place and a failure kind never has to be re-derived
from a message string. `details` is returned to the caller, which is the backend, so it must never
hold a mandate value, a key or a secret: the backend logs error responses.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, ClassVar


class AgentError(Exception):
    """Base of every error the agent service reports to its caller."""

    code: ClassVar[str] = "internal_error"
    status: ClassVar[int] = 500

    def __init__(self, message: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details or {})


class BadRequestError(AgentError):
    code = "bad_request"
    status = 400


class UnauthorizedError(AgentError):
    code = "unauthorized"
    status = 401


class InvalidStateError(AgentError):
    """The run's state does not allow this call. `details.state` and `details.allowed_from`."""

    code = "invalid_state"
    status = 409

    def __init__(
        self, message: str, *, state: str, allowed_from: Sequence[str], **extra: Any
    ) -> None:
        super().__init__(message, {"state": state, "allowed_from": list(allowed_from), **extra})


class IdempotencyConflictError(AgentError):
    """The same idempotent operation — one run's provisioning, one turn — with a different body."""

    code = "idempotency_conflict"
    status = 409


class SessionMismatchError(AgentError):
    """The session, or an observation of it, differs from what this instance approved."""

    code = "session_mismatch"
    status = 409


class KeyRefMismatchError(AgentError):
    code = "key_ref_mismatch"
    status = 409


class AddressMismatchError(AgentError):
    code = "address_mismatch"
    status = 409


class RequestValidationError(AgentError):
    """Field-level errors, in `details.fields`: a path mapped to what is wrong with it."""

    code = "validation_error"
    status = 422

    def __init__(self, message: str, fields: Mapping[str, str]) -> None:
        super().__init__(message, {"fields": dict(fields)})


class ObservationInconsistentError(AgentError):
    """The observation contradicts itself or the approved session (ADR-046).

    Distinct from `validation_error`: the body is well-formed, but our own backend built it wrong or
    from a stale chain read. The controller rebuilds the observation from the chain and retries, up
    to five times, then moves the run to `RECOVERY_REQUIRED`. `details.fields` names each
    contradiction by location and rule.
    """

    code = "observation_inconsistent"
    status = 422

    def __init__(self, message: str, fields: Mapping[str, str]) -> None:
        super().__init__(message, {"fields": dict(fields)})


class DependencyUnavailableError(AgentError):
    """A dependency the call needs is not working: in stage 2.2, only the signer."""

    code = "dependency_unavailable"
    status = 503
