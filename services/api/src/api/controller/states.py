"""The run's operational state machine, docs/architecture.md section 6.1, as data.

Every transition the controller makes goes through `require_transition`, so a transition the
diagram does not draw is an error rather than a row the database happily accepts. A change of cause
alone — a `RUNNING` run whose model failed, awaiting its abort — is not a transition.

Three transitions beyond the diagram's first version, all the product owner's: an RPC outage
during setup is `PREPARING → RECOVERY_REQUIRED`, and the operator's resume of such a run carries
setup on, `RECOVERY_REQUIRED → PREPARING` (ADR-063); a run whose setup never finished, aborted or
ended from there, is `RECOVERY_REQUIRED → FAILED_SETUP` (ADR-067).
"""

from __future__ import annotations

from collections.abc import Collection
from typing import Final

from api.db.enums import RunState

DRAFT: Final = RunState.DRAFT
VALIDATED: Final = RunState.VALIDATED
PREPARING: Final = RunState.PREPARING
RUNNING: Final = RunState.RUNNING
PAUSED: Final = RunState.PAUSED
RECOVERY_REQUIRED: Final = RunState.RECOVERY_REQUIRED
TERMINAL: Final = RunState.TERMINAL
FAILED_SETUP: Final = RunState.FAILED_SETUP

TRANSITIONS: Final[frozenset[tuple[RunState, RunState]]] = frozenset(
    {
        (DRAFT, VALIDATED),  # validate ok
        (VALIDATED, DRAFT),  # config edited, or validation no longer passes
        (VALIDATED, PREPARING),  # start or step
        (PREPARING, RUNNING),  # session open, auto
        (PREPARING, PAUSED),  # session open, step mode
        (PREPARING, FAILED_SETUP),  # funding or session creation failed; a session refused
        (PREPARING, RECOVERY_REQUIRED),  # ADR-063: an outage during setup
        (RUNNING, PAUSED),  # pause, step complete, reorg
        (PAUSED, RUNNING),  # resume
        (PAUSED, PAUSED),  # step (one turn)
        (RUNNING, RECOVERY_REQUIRED),  # outage, nonce conflict, inconsistent observation 5 times
        (PAUSED, RECOVERY_REQUIRED),  # reconcile failed
        (RECOVERY_REQUIRED, PAUSED),  # operator reconcile ok
        (RECOVERY_REQUIRED, PREPARING),  # ADR-063: operator reconcile ok, setup unfinished
        (RUNNING, TERMINAL),
        (PAUSED, TERMINAL),
        (RECOVERY_REQUIRED, TERMINAL),  # chain already terminal
        (RECOVERY_REQUIRED, FAILED_SETUP),  # ADR-067: ended, or aborted, before setup finished
    }
)

#: States in which the run holds the active-run row and its lease (ADR-019).
ACTIVE: Final = frozenset({PREPARING, RUNNING, PAUSED, RECOVERY_REQUIRED})
#: States after which nothing happens to the run again.
FINISHED: Final = frozenset({TERMINAL, FAILED_SETUP})


class InvalidTransitionError(Exception):
    """A transition architecture 6.1 does not draw."""

    def __init__(self, current: RunState, target: RunState) -> None:
        super().__init__(f"no transition from {current.value} to {target.value}")
        self.current = current
        self.target = target


def allowed(current: RunState, target: RunState) -> bool:
    return (current, target) in TRANSITIONS


def require_transition(current: RunState, target: RunState) -> None:
    if not allowed(current, target):
        raise InvalidTransitionError(current, target)


def sources(target: RunState) -> Collection[RunState]:
    """Every state from which `target` can be reached."""
    return sorted(
        {current for current, to in TRANSITIONS if to == target}, key=list(RunState).index
    )
