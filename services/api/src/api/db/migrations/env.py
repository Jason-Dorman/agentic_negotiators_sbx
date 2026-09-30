"""Alembic environment for the backend schema (docs/data_model.md section 8).

The database URL comes, in order, from the caller (`api.db.migrate` passes it as a config
attribute, so a password containing `%` survives — the ini file's interpolation would mangle it),
from `sqlalchemy.url` in the ini file, or from `DATABASE_URL`. A caller that already holds a
connection passes that instead, which is how the tests run a migration inside an async fixture.

`compare_type` is on so that autogenerate — and `test_migrations.py`, which runs the same
comparison as a test — reports a column whose type drifted, not only one that went missing.
"""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from api.db.models import Base

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    url = config.attributes.get("database_url") or config.get_main_option("sqlalchemy.url")
    url = url or os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "No database URL. Set DATABASE_URL, or run migrations through "
            "`python -m api.db.migrate`."
        )
    return str(url)


def _configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=False,
        transaction_per_migration=True,
    )


def _run_with(connection: Connection) -> None:
    _configure(connection)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(_database_url())
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_with)
            await connection.commit()
    finally:
        await engine.dispose()


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_with(connection)
        return
    asyncio.run(_run_async())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
