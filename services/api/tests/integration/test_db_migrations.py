"""The migration and the models describe the same schema, and the migration can be undone.

Runs in a scratch database of its own, created and dropped by this module, because it takes the
schema down to nothing and back — which the session-wide test database, shared by every other
integration test, must never see halfway through.

Two different claims are checked, and neither implies the other:

- **Models and migration agree.** Alembic's autogenerate comparison between `Base.metadata` and the
  migrated database must be empty. It compares tables, columns, types, nullability, foreign keys,
  unique constraints and indexes; it does *not* compare check constraints or triggers, which is
  why `test_db_schema_matches_data_model.py` exists and why the constraint tests assert each one by
  name.
- **Downgrade is real.** `downgrade base` must leave no table, no enum type and no function behind.
  A downgrade that dropped the tables but left the enum types would make the next `upgrade` fail on
  `CREATE TYPE`, which is the kind of thing nobody finds until they need to roll back.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import Connection, make_url
from sqlalchemy.ext.asyncio import create_async_engine

from api.db.migrate import downgrade, upgrade
from api.db.models import Base

SCRATCH_DATABASE = "agent_negotiation_migrations_scratch"


async def _admin(url: str, statement: str) -> None:
    engine = create_async_engine(url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            await connection.execute(text(statement))
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def scratch_url(pg_url: str) -> Iterator[str]:
    asyncio.run(_admin(pg_url, f"DROP DATABASE IF EXISTS {SCRATCH_DATABASE}"))
    asyncio.run(_admin(pg_url, f"CREATE DATABASE {SCRATCH_DATABASE}"))
    url = make_url(pg_url).set(database=SCRATCH_DATABASE).render_as_string(hide_password=False)
    yield url
    asyncio.run(_admin(pg_url, f"DROP DATABASE IF EXISTS {SCRATCH_DATABASE} WITH (FORCE)"))


async def _query(url: str, sql: str) -> list[tuple[object, ...]]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            return [tuple(row) for row in (await connection.execute(text(sql))).all()]
    finally:
        await engine.dispose()


def _diff(url: str) -> list[object]:
    async def compare() -> list[object]:
        engine = create_async_engine(url)
        try:
            async with engine.connect() as connection:

                def run(sync: Connection) -> list[object]:
                    context = MigrationContext.configure(sync, opts={"compare_type": True})
                    return list(compare_metadata(context, Base.metadata))

                return await connection.run_sync(run)
        finally:
            await engine.dispose()

    return asyncio.run(compare())


PUBLIC_OBJECTS = """
    SELECT 'table', tablename FROM pg_tables
        WHERE schemaname = 'public' AND tablename <> 'alembic_version'
    UNION ALL
    SELECT 'type', t.typname FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
        WHERE n.nspname = 'public' AND t.typtype = 'e'
    UNION ALL
    SELECT 'function', p.proname FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
        WHERE n.nspname = 'public'
"""


def test_upgrade_matches_the_models_exactly(scratch_url: str) -> None:
    upgrade(scratch_url)
    try:
        assert _diff(scratch_url) == []
    finally:
        downgrade(scratch_url)


def test_downgrade_leaves_nothing_behind_and_upgrade_works_again(scratch_url: str) -> None:
    upgrade(scratch_url)
    created = asyncio.run(_query(scratch_url, PUBLIC_OBJECTS))
    assert len([kind for kind, _ in created if kind == "table"]) == len(Base.metadata.tables)
    assert len([kind for kind, _ in created if kind == "type"]) == 14
    assert len([kind for kind, _ in created if kind == "function"]) == 3

    downgrade(scratch_url)
    assert asyncio.run(_query(scratch_url, PUBLIC_OBJECTS)) == []
    assert asyncio.run(_query(scratch_url, "SELECT * FROM alembic_version")) == []

    # And back up again: a downgrade that left an enum type behind would fail here, on CREATE TYPE.
    upgrade(scratch_url)
    assert asyncio.run(_query(scratch_url, "SELECT version_num FROM alembic_version")) == [
        ("0005",)
    ]
    downgrade(scratch_url)


def test_migrate_cli_refuses_to_run_without_a_database_url(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from api.db.migrate import main

    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["upgrade"]) == 2
    assert "DATABASE_URL is not set" in capsys.readouterr().err


def test_migrate_cli_upgrades_and_downgrades(
    scratch_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from api.db.migrate import main

    monkeypatch.setenv("DATABASE_URL", scratch_url)
    assert main(["upgrade"]) == 0
    assert asyncio.run(_query(scratch_url, "SELECT version_num FROM alembic_version")) == [
        ("0005",)
    ]
    assert main(["downgrade", "base"]) == 0
    assert asyncio.run(_query(scratch_url, PUBLIC_OBJECTS)) == []
