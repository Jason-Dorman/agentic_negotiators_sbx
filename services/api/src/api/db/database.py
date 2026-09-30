"""The engine's one owner, and the unit of work it hands out.

`Database` is constructed once, in a composition root, from an injected URL (no module-level state,
docs/contributing.md section 2.1). Everything else receives a `UnitOfWork` for the length of one
transaction and sees repositories, never a session: that is the `sessions-stay-in-db` import
contract made concrete.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from api.db.protocols import UnitOfWork
from api.db.repositories.chain import (
    SqlBalanceSnapshotRepository,
    SqlChainEventRepository,
    SqlOutboxRepository,
)
from api.db.repositories.operational import (
    SqlOperationRepository,
    SqlRunEventRepository,
    SqlRunMetricsRepository,
)
from api.db.repositories.reference import SqlDeploymentRepository, SqlScenarioRepository
from api.db.repositories.runs import (
    SqlLeaseRepository,
    SqlMandateRepository,
    SqlRunRepository,
    SqlWalletRepository,
)
from api.db.repositories.turns import (
    SqlDecisionRepository,
    SqlSignedActionRepository,
    SqlTurnRepository,
)


class SqlUnitOfWork(UnitOfWork):
    """Every repository, bound to one session and therefore one transaction."""

    def __init__(self, session: AsyncSession) -> None:
        self.scenarios = SqlScenarioRepository(session)
        self.deployments = SqlDeploymentRepository(session)
        self.runs = SqlRunRepository(session)
        self.mandates = SqlMandateRepository(session)
        self.wallets = SqlWalletRepository(session)
        self.leases = SqlLeaseRepository(session)
        self.turns = SqlTurnRepository(session)
        self.decisions = SqlDecisionRepository(session)
        self.signed_actions = SqlSignedActionRepository(session)
        self.outbox = SqlOutboxRepository(session)
        self.chain_events = SqlChainEventRepository(session)
        self.balances = SqlBalanceSnapshotRepository(session)
        self.metrics = SqlRunMetricsRepository(session)
        self.run_events = SqlRunEventRepository(session)
        self.operations = SqlOperationRepository(session)


class Database:
    def __init__(self, url: str, *, pool_size: int = 5, echo: bool = False) -> None:
        self._engine = create_async_engine(url, pool_size=pool_size, pool_pre_ping=True, echo=echo)
        # Records are built before the session closes, so nothing needs its attributes after
        # commit; `expire_on_commit=False` just stops SQLAlchemy doing work that would be discarded.
        self._sessions = async_sessionmaker(self._engine, expire_on_commit=False, autoflush=False)

    @asynccontextmanager
    async def unit_of_work(self) -> AsyncIterator[UnitOfWork]:
        """One transaction: committed when the block exits, rolled back when it raises."""
        async with self._sessions() as session, session.begin():
            yield SqlUnitOfWork(session)

    async def ping(self) -> bool:
        """For `GET /v1/health`: can a query be answered at all."""
        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except (SQLAlchemyError, OSError):
            return False
        return True

    async def dispose(self) -> None:
        await self._engine.dispose()
