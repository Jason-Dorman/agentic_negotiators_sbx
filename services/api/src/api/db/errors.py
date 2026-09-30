"""What the database refused, in terms the rest of the backend can act on without SQLAlchemy.

Nothing outside `api.db` imports SQLAlchemy (`.importlinter`, `sessions-stay-in-db`), so the
repositories translate the driver's integrity errors into these. They keep the constraint's name,
because the constraint is the fact that matters: "`uq_signed_actions_run_id_sequence` refused this"
means a duplicate action, which the relay reconciles as already complete (A06), while
"`uq_tx_outbox_signed_action_id_live` refused this" means a second live transaction for one action.
"""

from __future__ import annotations


class PersistenceError(Exception):
    """Base of every error this package raises for a refused write."""


class ConstraintViolationError(PersistenceError):
    """The database refused a write under a named constraint."""

    def __init__(self, constraint: str | None, message: str) -> None:
        super().__init__(f"{constraint or 'unnamed constraint'}: {message}")
        self.constraint = constraint


class DuplicateError(ConstraintViolationError):
    """A uniqueness constraint refused the row. SQLSTATE 23505."""


class CheckViolationError(ConstraintViolationError):
    """A check constraint refused the row. SQLSTATE 23514."""


class ForeignKeyViolationError(ConstraintViolationError):
    """The row refers to one that does not exist. SQLSTATE 23503."""


class ImmutableRowError(ConstraintViolationError):
    """An immutability trigger refused an UPDATE or DELETE (docs/data_model.md section 8).

    SQLSTATE 23001, raised by the triggers the initial migration installs.
    """


class NotFoundError(PersistenceError):
    """A row the caller required does not exist."""


class LeaseLostError(NotFoundError):
    """The caller no longer holds the run's lease: another holder took it after it expired."""
