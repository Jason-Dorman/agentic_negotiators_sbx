"""A decision record that had a NUL character stored as the text `\\u0000`, and says so.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-06

Stage 3.2 (docs/build_plan.md), Q71, ADR-090. PostgreSQL's JSONB and TEXT refuse the NUL
character, and a model's answer can hold one — in its explanation, or in a field name the
validator's feedback then quotes. The repository stores each NUL in `raw_response` and
`validation_feedback` as the six characters `\\u0000` and sets `raw_response_escaped`, so the record
is kept, the turn's unit of work succeeds, and anyone reading the record knows the stored text is
not byte for byte what the model wrote. False for every row written before this migration, which
held no NUL: it could not have been stored.

Like 0001 to 0004, this imports nothing from the application and wraps every name in `op.f()`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "decisions",
        sa.Column("raw_response_escaped", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("decisions", "raw_response_escaped")
