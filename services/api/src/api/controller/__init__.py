"""The run lifecycle state machine of docs/architecture.md section 6.1: the operations, the lease
and the single active run, setup, pause and resume, abort, expiry, and recovery after a restart.

Built in stage 2.4 of docs/build_plan.md. `controller.py` has the operations and their states,
`driver.py` what drives an active run between them, `setup.py` the path from a validated run to an
approved session, and `states.py` the transitions as data.
"""

from api.controller.controller import AgentProvisioningError, RunController, merge_patch
from api.controller.driver import Outage, Progress, RunDriver, RunMetricsSink, RunStates
from api.controller.errors import (
    AnotherRunActiveError,
    ControllerError,
    InvalidStateError,
    RunNotFoundError,
    RunRequestError,
    TurnInProgressError,
)
from api.controller.requests import RunRequest
from api.controller.setup import ApprovalTerms, SessionSetup, SetupStatus
from api.controller.states import (
    ACTIVE,
    FINISHED,
    TRANSITIONS,
    InvalidTransitionError,
    allowed,
    require_transition,
)

__all__ = [
    "ACTIVE",
    "FINISHED",
    "TRANSITIONS",
    "AgentProvisioningError",
    "AnotherRunActiveError",
    "ApprovalTerms",
    "ControllerError",
    "InvalidStateError",
    "InvalidTransitionError",
    "Outage",
    "Progress",
    "RunController",
    "RunDriver",
    "RunMetricsSink",
    "RunNotFoundError",
    "RunRequest",
    "RunRequestError",
    "RunStates",
    "SessionSetup",
    "SetupStatus",
    "TurnInProgressError",
    "allowed",
    "merge_patch",
    "require_transition",
]
