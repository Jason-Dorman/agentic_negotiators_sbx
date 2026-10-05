"""What the evidence module is handed rather than imports.

`projection` and `metrics` are siblings of `evidence` under `.importlinter`, so neither may be
imported here. The composition root hands the evidence module the projector and the metrics
calculator through these two protocols, as it hands the indexer the projection's sentence renderer.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol

from api.db.records import RunMetricsRecord


class ProjectionView(Protocol):
    """The chain-derived parts of the run resource (`api.projection.RunProjection`)."""

    @property
    def session(self) -> dict[str, Any] | None: ...

    @property
    def timeline(self) -> list[dict[str, Any]]: ...

    @property
    def balances(self) -> dict[str, dict[str, str]]: ...


class RunProjections(Protocol):
    async def project(self, run_id: uuid.UUID) -> ProjectionView: ...


class RunMetrics(Protocol):
    """`api.metrics.MetricsCalculator`."""

    async def refresh(self, run_id: uuid.UUID) -> RunMetricsRecord: ...
    async def evaluator(self, run_id: uuid.UUID) -> dict[str, Any]: ...

    @staticmethod
    def summary(record: RunMetricsRecord | None) -> dict[str, Any]: ...

    @staticmethod
    def public(record: RunMetricsRecord | None) -> dict[str, Any]: ...

    @staticmethod
    def private(record: RunMetricsRecord | None) -> dict[str, Any]: ...
