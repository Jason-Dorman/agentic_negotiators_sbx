"""The outbox gains a stored sentence and the block a transaction was first broadcast at.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-30

Stage 2.3 (docs/build_plan.md), two columns on `tx_outbox`, both nullable:

- `sentence`: the timeline sentence of an execution failure. A reverted transaction emits no event,
  so it has no `chain_events` row to carry one; the sentence is rendered once, when the indexer
  records the revert, and stored here so the live view, replay and export tell the same story
  (ADR-024, ADR-051).
- `submitted_block`: the chain head when the transaction was first broadcast. The relay replaces a
  transaction that has not been included within `RELAY_REPLACE_AFTER_BLOCKS` of it (ADR-050), and a
  restart must not reset that count, so it is stored rather than remembered.

Like 0001, this imports nothing from the application and wraps every constraint name in `op.f()`.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("tx_outbox", sa.Column("sentence", sa.Text(), nullable=True))
    op.add_column("tx_outbox", sa.Column("submitted_block", sa.BigInteger(), nullable=True))
    op.create_check_constraint(
        op.f("ck_tx_outbox_submitted_block_non_negative"), "tx_outbox", "submitted_block >= 0"
    )


def downgrade() -> None:
    op.drop_constraint(op.f("ck_tx_outbox_submitted_block_non_negative"), "tx_outbox")
    op.drop_column("tx_outbox", "submitted_block")
    op.drop_column("tx_outbox", "sentence")
