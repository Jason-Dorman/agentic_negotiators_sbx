"""The PostgreSQL half of the integration harness (docs/test_strategy.md section 2).

Every test here runs against a real PostgreSQL 16 — the test database the Compose profile creates
beside the development one (`infra/postgres/init`), migrated once per session with the real
migrations and emptied before each test. A fake would test the fake: the constraints and triggers
these tests exist to exercise live in the database, not in Python.

**Skipping is a local convenience, not a CI behaviour.** Without PostgreSQL a developer running the
Python suite gets a clear skip. With `REQUIRE_INTEGRATION=1` — set by CI and by `make ci` — the same
absence is a failure, because a gate that quietly skips is a gate that reports green without having
run (docs/contributing.md section 3).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest
from anvil_chain import deploy, running_anvil
from api_chain import AnvilChain, Backend
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine

from api.db import Database
from api.db.migrate import downgrade, upgrade
from api.db.models import Base

#: The Compose defaults (infra/compose.local.yaml): role, password and database all
#: `agent_negotiation`, the test database suffixed `_test`, the host port 55432.
DEFAULT_TEST_DATABASE_URL = (
    "postgresql+asyncpg://agent_negotiation:agent_negotiation@127.0.0.1:55432/"
    "agent_negotiation_test"
)

INTEGRATION_DIR = Path(__file__).resolve().parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if INTEGRATION_DIR in Path(str(item.fspath)).resolve().parents:
            item.add_marker(pytest.mark.integration)


def _integration_required() -> bool:
    return os.environ.get("REQUIRE_INTEGRATION", "").lower() in {"1", "true", "yes"}


async def _reachable(url: str) -> str | None:
    """None when a query succeeds, otherwise the reason it did not."""
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
    except Exception as error:
        return f"{type(error).__name__}: {error}"
    finally:
        await engine.dispose()
    return None


@pytest.fixture(scope="session")
def pg_url() -> str:
    # Empty counts as unset: `make` exports a variable it knows of but that infra/.env never
    # defined as an empty string, and "" is not a database.
    url = os.environ.get("TEST_DATABASE_URL") or DEFAULT_TEST_DATABASE_URL
    problem = asyncio.run(_reachable(url))
    if problem is not None:
        message = (
            f"PostgreSQL is not reachable at the test database URL ({problem}). "
            "Start it with `make up`, or set TEST_DATABASE_URL."
        )
        if _integration_required():
            pytest.fail(message + " REQUIRE_INTEGRATION is set, so this is a failure.")
        pytest.skip(message)
    return url


async def _reset_schema(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("DROP SCHEMA public CASCADE"))
            await connection.execute(text("CREATE SCHEMA public"))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def migrated_url(pg_url: str) -> Iterator[str]:
    """An empty schema, migrated to head with the real migrations, once per session.

    The schema is dropped first, so a run that died halfway through a previous session, or a
    migration test that left the database at `base`, cannot leak into this one.
    """
    asyncio.run(_reset_schema(pg_url))
    upgrade(pg_url)
    yield pg_url
    # The last test's rows go first: a downgrade narrows constraints — migration 0004's deployment
    # uniqueness among them — that rows written under the wider ones need not satisfy.
    asyncio.run(_truncate(pg_url))
    downgrade(pg_url)


#: Every table the models declare, children first, for TRUNCATE.
ALL_TABLES = [table.name for table in reversed(Base.metadata.sorted_tables)]


async def _truncate(url: str) -> None:
    engine = create_async_engine(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(f"TRUNCATE {', '.join(ALL_TABLES)} RESTART IDENTITY CASCADE")
            )
    finally:
        await engine.dispose()


@pytest.fixture
async def database(migrated_url: str) -> AsyncIterator[Database]:
    """A `Database` over an emptied schema.

    TRUNCATE does not fire the row-level immutability triggers, which is what lets a test start
    from nothing without disabling the protections it may be about to test.
    """
    engine = create_async_engine(migrated_url)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(f"TRUNCATE {', '.join(ALL_TABLES)} RESTART IDENTITY CASCADE")
            )
    finally:
        await engine.dispose()

    db = Database(migrated_url, pool_size=2)
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture
async def raw_sql(migrated_url: str) -> AsyncIterator[AsyncConnection]:
    """A plain connection, autocommitting, for statements a repository would never issue.

    Tests use it to attempt what the schema must refuse — an UPDATE of an immutable row, an INSERT
    that breaks a check — without going through the code whose protections are under test.
    """
    engine = create_async_engine(migrated_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            yield connection
    finally:
        await engine.dispose()


# ---------------------------------------------------------------------------------------------
# The chain half (stage 2.3): one throwaway Anvil and one real deployment per session
# ---------------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def anvil_rpc() -> Iterator[str]:
    """A fresh Anvil on a free port, torn down with the session (`anvil_chain`)."""
    with running_anvil() as url:
        yield url


@pytest.fixture(scope="session")
def manifest(anvil_rpc: str) -> dict[str, Any]:
    """The real deploy script, run against the throwaway chain."""
    return deploy(anvil_rpc, "local-api-test")


@pytest.fixture
def chain(anvil_rpc: str, manifest: dict[str, Any]) -> Iterator[AnvilChain]:
    """The chain, with every control a test may touch put back afterwards.

    A test that turns automine off or lowers the block gas limit would otherwise leave the chain
    unusable for the next one.
    """
    anvil = AnvilChain(anvil_rpc, manifest)
    yield anvil
    anvil.automine(True)
    anvil.block_gas_limit(30_000_000)
    anvil.mine()


@pytest.fixture
def backend(database: Database, chain: AnvilChain) -> Backend:
    """The relay, indexer and projector, wired as the stage 2.4 controller will wire them."""
    return Backend(database, chain)
