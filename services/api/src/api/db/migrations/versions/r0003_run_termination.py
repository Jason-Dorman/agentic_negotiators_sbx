"""A run records a termination it owes before it sends it.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-02

Stage 2.4 (docs/build_plan.md), two nullable columns on `runs` (ADR-068):

- `termination_cause`: why the run's session is being ended early — `model_failure`,
  `execution_failure`, `abort_requested`, `session_refused`, `session_deadline`, … — written in the
  same unit of work as whatever decided it, so the termination is sent after a crash or an outage as
  surely as before one, and a fault crossing it cannot overwrite it.
- `termination_code`: the abort reason code it will be sent with (protocol 10), or null for an
  expiry, which carries none. A code is only ever written with a cause.

Like 0001 and 0002, this imports nothing from the application and wraps every name in `op.f()`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TERMINATION_CODE = (
    "termination_code IS NULL "
    "OR (termination_cause IS NOT NULL AND termination_code BETWEEN 1 AND 4)"
)


def upgrade() -> None:
    op.add_column("runs", sa.Column("termination_cause", sa.Text(), nullable=True))
    op.add_column("runs", sa.Column("termination_code", sa.SmallInteger(), nullable=True))
    op.create_check_constraint(op.f("ck_runs_termination_code_valid"), "runs", TERMINATION_CODE)


def downgrade() -> None:
    op.drop_constraint(op.f("ck_runs_termination_code_valid"), "runs")
    op.drop_column("runs", "termination_code")
    op.drop_column("runs", "termination_cause")
