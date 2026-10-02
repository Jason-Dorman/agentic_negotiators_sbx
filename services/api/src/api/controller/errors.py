"""The controller's domain exceptions, each carrying the API error code of api_contract section 7,
so the route layer of stage 2.5 maps an exception to a response in one place."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any, ClassVar

from api.db.enums import RunState


class ControllerError(Exception):
    code: ClassVar[str] = "internal_error"
    status: ClassVar[int] = 500

    def __init__(self, message: str, details: Mapping[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details or {})


class RunNotFoundError(ControllerError):
    code = "not_found"
    status = 404


class InvalidStateError(ControllerError):
    """The operation is not allowed in the run's state: `details.state`, `details.allowed_from`."""

    code = "invalid_state"
    status = 409

    def __init__(
        self,
        operation: str,
        state: RunState,
        allowed_from: Collection[RunState],
        *,
        termination: str | None = None,
    ) -> None:
        details: dict[str, Any] = {
            "state": state.value,
            "allowed_from": [item.value for item in allowed_from],
        }
        message = f"{operation} is not allowed while the run is {state.value}"
        if termination is not None:
            # ADR-070: the session is being ended, whatever state the run shows meanwhile.
            details["termination"] = termination
            message = f"{operation} is not allowed while the run's session is being ended"
        super().__init__(message, details)


class TurnInProgressError(ControllerError):
    code = "turn_in_progress"
    status = 409


class AnotherRunActiveError(ControllerError):
    code = "another_run_active"
    status = 409


class RunRequestError(ControllerError):
    """A run request the controller refuses: `details.fields` names each problem and its rule."""

    code = "validation_error"
    status = 422
