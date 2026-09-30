"""Shared machinery for the SQL repositories: one session, savepointed writes, error translation.

Every write runs inside a savepoint. When the database refuses a row, only the savepoint rolls back,
so the caller can catch the translated error and carry on inside the same unit of work — which is
what the relay does when a duplicate submission turns out to be an already-recorded action (A06),
and what a test does when it asserts one refusal and then inspects the table.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql import Executable, Select

from api.db.errors import (
    CheckViolationError,
    ConstraintViolationError,
    DuplicateError,
    ForeignKeyViolationError,
    ImmutableRowError,
    NotFoundError,
)

# PostgreSQL SQLSTATEs for the integrity class (Appendix A of the PostgreSQL manual).
_UNIQUE_VIOLATION = "23505"
_CHECK_VIOLATION = "23514"
_FOREIGN_KEY_VIOLATION = "23503"
_RESTRICT_VIOLATION = "23001"

_BY_SQLSTATE: dict[str, type[ConstraintViolationError]] = {
    _UNIQUE_VIOLATION: DuplicateError,
    _CHECK_VIOLATION: CheckViolationError,
    _FOREIGN_KEY_VIOLATION: ForeignKeyViolationError,
    _RESTRICT_VIOLATION: ImmutableRowError,
}


def translate(error: IntegrityError) -> ConstraintViolationError:
    """Map the driver's error to the domain's, keeping the constraint name.

    SQLAlchemy wraps asyncpg's exception; the SQLSTATE and constraint name live on the asyncpg
    exception, which is the adapter's `__cause__`.
    """
    driver_error: Any = getattr(error.orig, "__cause__", None) or error.orig
    sqlstate = str(getattr(driver_error, "sqlstate", "") or "")
    constraint = getattr(driver_error, "constraint_name", None)
    message = str(getattr(driver_error, "message", "") or driver_error)
    error_class = _BY_SQLSTATE.get(sqlstate, ConstraintViolationError)
    return error_class(constraint, message)


class SqlRepository:
    """Writes are Core statements with RETURNING, so they bypass the ORM identity map.

    Every read therefore asks for `populate_existing`: without it, a row loaded earlier in the same
    unit of work would come back as it was before the write, not as it is now.
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _get[M](self, model: type[M], key: object) -> M | None:
        return await self._session.get(model, key, populate_existing=True)

    async def _one_or_none[M](self, statement: Select[tuple[M]]) -> M | None:
        result = await self._session.execute(statement.execution_options(populate_existing=True))
        return result.scalar_one_or_none()

    async def _all[M](self, statement: Select[tuple[M]]) -> list[M]:
        result = await self._session.execute(statement.execution_options(populate_existing=True))
        return list(result.scalars().all())

    async def _write_scalar(self, statement: Executable) -> Any:
        """Execute a write that returns one ORM row, inside a savepoint, translating refusals."""
        try:
            async with self._session.begin_nested():
                result = await self._session.execute(statement)
                return result.scalar_one_or_none()
        except IntegrityError as error:
            raise translate(error) from error

    async def _write(self, statement: Executable) -> None:
        try:
            async with self._session.begin_nested():
                await self._session.execute(statement)
        except IntegrityError as error:
            raise translate(error) from error

    async def _write_all(self, statement: Executable) -> list[Any]:
        try:
            async with self._session.begin_nested():
                result = await self._session.execute(statement)
                return list(result.scalars().all())
        except IntegrityError as error:
            raise translate(error) from error


def required[T](row: T | None, what: str) -> T:
    """A row an update targeted must exist; an update of nothing is a bug, not a no-op."""
    if row is None:
        raise NotFoundError(f"no {what}")
    return row
