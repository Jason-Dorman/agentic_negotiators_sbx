"""A run's RPC requests, by method, and their estimated cost; a deployment's chain, by genesis.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-02

Stage 2.5 (docs/build_plan.md), three columns on `run_metrics` (ADR-061):

- `rpc_requests`: every JSON-RPC request the chain adapter made while it was attributed to the run —
  while the run was being driven under its lease. Exact and local.
- `rpc_requests_by_method`: the same count, by JSON-RPC method.
- `rpc_cost_estimated_usd`: the counts priced from the operator's RPC price table. Null when the
  table has no price for the deployment's provider, or for one of the methods: unknown, never zero.
  There is no reported column, because no provider reports a per-request cost.

And on `deployments` (ADR-081): `genesis_hash`, the hash of block 0 of the chain the deployment was
loaded against, so a restarted Anvil — the same chain ID and, from the same deployer, the same
addresses — is a deployment of its own and not an overwrite of the one before. The uniqueness of
`(chain_id, exchange_address)` becomes `(chain_id, genesis_hash, exchange_address)`. Null for a row
loaded before this migration: its chain is unknown, so it is no chain this backend serves. The
downgrade refuses, on the narrower constraint, a database in which two chains' deployments share an
exchange address: the older schema cannot describe it.

Like 0001 to 0003, this imports nothing from the application and wraps every name in `op.f()`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "run_metrics",
        sa.Column("rpc_requests", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "run_metrics",
        sa.Column(
            "rpc_requests_by_method",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "run_metrics",
        sa.Column("rpc_cost_estimated_usd", sa.Numeric(12, 6), nullable=True),
    )
    op.create_check_constraint(
        op.f("ck_run_metrics_rpc_requests_non_negative"), "run_metrics", "rpc_requests >= 0"
    )
    op.add_column("deployments", sa.Column("genesis_hash", sa.Text(), nullable=True))
    op.drop_constraint(op.f("uq_deployments_chain_id_exchange_address"), "deployments")
    op.create_unique_constraint(
        op.f("uq_deployments_chain_id_genesis_hash_exchange_address"),
        "deployments",
        ["chain_id", "genesis_hash", "exchange_address"],
    )


def downgrade() -> None:
    op.drop_constraint(op.f("uq_deployments_chain_id_genesis_hash_exchange_address"), "deployments")
    op.create_unique_constraint(
        op.f("uq_deployments_chain_id_exchange_address"),
        "deployments",
        ["chain_id", "exchange_address"],
    )
    op.drop_column("deployments", "genesis_hash")
    op.drop_constraint(op.f("ck_run_metrics_rpc_requests_non_negative"), "run_metrics")
    op.drop_column("run_metrics", "rpc_cost_estimated_usd")
    op.drop_column("run_metrics", "rpc_requests_by_method")
    op.drop_column("run_metrics", "rpc_requests")
