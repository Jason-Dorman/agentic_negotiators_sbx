"""Scenarios and deployments: reference data loaded from files and upserted by id.

Scenarios come from `scenarios/*.json` (data model section 3.1) and deployments from the manifests
the deploy script writes (section 3.2, ADR-038). Both are upserted by their natural id, so reloading
an unchanged file is a no-op and a changed one replaces its row.
"""

from __future__ import annotations

from typing import Any, ClassVar

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from api.db.models import Deployment, Scenario
from api.db.protocols import DeploymentRepository, ScenarioRepository
from api.db.records import DeploymentRecord, ScenarioRecord
from api.db.repositories._base import SqlRepository


def _columns(record: Any, names: tuple[str, ...]) -> dict[str, Any]:
    return {name: getattr(record, name) for name in names}


class SqlScenarioRepository(SqlRepository, ScenarioRepository):
    _FIELDS: ClassVar[tuple[str, ...]] = (
        "scenario_id",
        "name",
        "description",
        "public_config",
        "buyer_template",
        "seller_template",
        "source_hash",
    )

    async def upsert(self, scenario: ScenarioRecord) -> None:
        values = _columns(scenario, self._FIELDS)
        statement = insert(Scenario).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[Scenario.scenario_id],
            # ON CONFLICT DO UPDATE is not an UPDATE statement, so `onupdate` does not fire.
            set_={
                **{name: statement.excluded[name] for name in self._FIELDS[1:]},
                "updated_at": func.now(),
            },
        )
        await self._write(statement)

    async def get(self, scenario_id: str) -> ScenarioRecord | None:
        row = await self._get(Scenario, scenario_id)
        return None if row is None else ScenarioRecord.from_row(row)

    async def list_all(self) -> list[ScenarioRecord]:
        rows = await self._all(select(Scenario).order_by(Scenario.scenario_id))
        return [ScenarioRecord.from_row(row) for row in rows]


class SqlDeploymentRepository(SqlRepository, DeploymentRepository):
    _FIELDS: ClassVar[tuple[str, ...]] = (
        "deployment_id",
        "chain_id",
        "protocol_version",
        "exchange_address",
        "base_token_address",
        "quote_token_address",
        "operator_address",
        "relay_address",
        "code_hashes",
        "compiler",
        "explorer_base_url",
        "ens",
        "manifest",
        "start_block",
        "deployed_at",
    )

    async def upsert(self, deployment: DeploymentRecord) -> None:
        values = _columns(deployment, self._FIELDS)
        statement = insert(Deployment).values(**values)
        statement = statement.on_conflict_do_update(
            index_elements=[Deployment.deployment_id],
            # ON CONFLICT DO UPDATE is not an UPDATE statement, so `onupdate` does not fire.
            set_={
                **{name: statement.excluded[name] for name in self._FIELDS[1:]},
                "updated_at": func.now(),
            },
        )
        await self._write(statement)

    async def get(self, deployment_id: str) -> DeploymentRecord | None:
        row = await self._get(Deployment, deployment_id)
        return None if row is None else DeploymentRecord.from_row(row)

    async def list_all(self) -> list[DeploymentRecord]:
        rows = await self._all(select(Deployment).order_by(Deployment.deployment_id))
        return [DeploymentRecord.from_row(row) for row in rows]
