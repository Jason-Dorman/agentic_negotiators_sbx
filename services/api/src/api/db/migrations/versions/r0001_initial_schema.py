"""Initial schema: every table of docs/data_model.md section 3.

Revision ID: 0001
Revises:
Create Date: 2026-09-25

Written by hand from the models and checked against them by `test_migrations.py`, which runs
Alembic's autogenerate comparison and fails on any difference. The migration imports nothing from
the application: a migration describes the schema as it was when it was written, and importing
today's column types would let a later change rewrite history.

Beyond the tables themselves, three things here are structural rather than cosmetic:

- **Uniqueness that makes double execution a database error** (data model principle 4):
  `signed_actions (run_id, sequence)` and `(digest)`, `tx_outbox (tx_hash)`, the partial index that
  allows one live transaction per signed action, and `wallets (address)`, which is "addresses are
  never reused across runs" as a constraint rather than a convention (ADR-039).
- **Checks that encode the data model's rules**: an economic outcome needs a terminal transaction
  and admits only its own reason codes and actor (section 5), an authorised decision was a valid one
  (invariant 4), a key reference is a reference and never a key.
- **Immutability triggers** (section 8): mandate versions and run events refuse UPDATE and DELETE,
  and the columns that identify a signed action or evidence a chain event refuse UPDATE.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Data model section 4, in declaration order. Values are only ever added (section 8).
ENUMS: dict[str, tuple[str, ...]] = {
    "party": ("buyer", "seller"),
    "party_or_operator": ("buyer", "seller", "operator", "anyone"),
    "policy_kind": ("deterministic", "model"),
    "run_mode": ("live", "fixture"),
    "run_state": (
        "draft",
        "validated",
        "preparing",
        "running",
        "paused",
        "recovery_required",
        "terminal",
        "failed_setup",
    ),
    "outcome_kind": ("pending", "settled", "closed", "expired", "aborted"),
    "turn_state": (
        "observing",
        "deciding",
        "repairing",
        "signing",
        "broadcasting",
        "confirming",
        "confirmed",
        "model_failed",
        "execution_failed",
    ),
    "action_kind": ("offer", "accept", "close"),
    "action_status": (
        "signed",
        "submitted",
        "included",
        "confirmed",
        "finalized",
        "reverted",
        "superseded",
    ),
    "tx_kind": (
        "record_offer",
        "accept_and_settle",
        "close_session",
        "expire_session",
        "abort_session",
        "create_session",
        "mint",
        "approve",
        "fund_eth",
    ),
    "tx_status": (
        "pending",
        "submitted",
        "included",
        "confirmed",
        "finalized",
        "reverted",
        "replaced",
        "dropped",
    ),
    "snapshot_stage": ("pre_setup", "post_setup", "pre_settlement", "post_settlement", "terminal"),
    "token_role": ("base", "quote", "eth"),
    "operation_status": ("pending", "running", "succeeded", "failed"),
}


# Every constraint and index name below is wrapped in `op.f()`. Inside a migration Alembic applies
# the target metadata's naming convention to explicit names too, and the check-constraint convention
# embeds `%(constraint_name)s`, so a bare `name="ck_runs_x"` is created as `ck_runs_ck_runs_x`.
# Autogenerate cannot catch that (it does not compare check constraints); the constraint tests did.


def enum(name: str) -> postgresql.ENUM:
    return postgresql.ENUM(*ENUMS[name], name=name, create_type=False)


def amount(name: str, *, nullable: bool = False, default: str | None = None) -> sa.Column[Any]:
    """A token amount: NUMERIC(78,0) (ADR-020)."""
    return sa.Column(
        name,
        sa.Numeric(78, 0),
        nullable=nullable,
        server_default=sa.text(default) if default is not None else None,
    )


def usd(name: str) -> sa.Column[Any]:
    return sa.Column(name, sa.Numeric(12, 6), nullable=True)


def jsonb(name: str, *, nullable: bool = False, default: str | None = None) -> sa.Column[Any]:
    return sa.Column(
        name,
        postgresql.JSONB(),
        nullable=nullable,
        server_default=sa.text(f"'{default}'::jsonb") if default is not None else None,
    )


def uuid_pk() -> sa.Column[Any]:
    return sa.Column("id", sa.UUID(), server_default=sa.text("gen_random_uuid()"), nullable=False)


def timestamps() -> list[sa.Column[Any]]:
    return [
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    ]


def non_negative(table: str, *columns: str) -> list[sa.CheckConstraint]:
    return [
        sa.CheckConstraint(f"{column} >= 0", name=op.f(f"ck_{table}_{column}_non_negative"))
        for column in columns
    ]


def text_column(name: str, *, nullable: bool = False) -> sa.Column[Any]:
    return sa.Column(name, sa.Text(), nullable=nullable)


def upgrade() -> None:
    bind = op.get_bind()
    for name in ENUMS:
        enum(name).create(bind, checkfirst=False)

    # --- Reference data ------------------------------------------------------------------------

    op.create_table(
        "scenarios",
        text_column("scenario_id"),
        text_column("name"),
        text_column("description"),
        jsonb("public_config"),
        jsonb("buyer_template"),
        jsonb("seller_template"),
        text_column("source_hash"),
        *timestamps(),
        sa.PrimaryKeyConstraint("scenario_id", name=op.f("pk_scenarios")),
    )

    op.create_table(
        "deployments",
        text_column("deployment_id"),
        sa.Column("chain_id", sa.BigInteger(), nullable=False),
        text_column("protocol_version"),
        text_column("exchange_address"),
        text_column("base_token_address"),
        text_column("quote_token_address"),
        text_column("operator_address"),
        text_column("relay_address"),
        jsonb("code_hashes"),
        jsonb("compiler"),
        text_column("explorer_base_url", nullable=True),
        jsonb("ens", nullable=True),
        jsonb("manifest"),
        sa.Column("start_block", sa.BigInteger(), nullable=False),
        sa.Column("deployed_at", sa.DateTime(timezone=True), nullable=False),
        *timestamps(),
        *non_negative("deployments", "start_block"),
        sa.PrimaryKeyConstraint("deployment_id", name=op.f("pk_deployments")),
        sa.UniqueConstraint(
            "chain_id", "exchange_address", name=op.f("uq_deployments_chain_id_exchange_address")
        ),
    )

    op.create_table(
        "batches",
        uuid_pk(),
        text_column("name"),
        jsonb("population"),
        sa.Column("pairings", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("repetitions", sa.Integer(), nullable=False),
        jsonb("template_run"),
        text_column("status"),
        jsonb("report", nullable=True),
        *timestamps(),
        *non_negative("batches", "repetitions"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_batches")),
    )

    op.create_table(
        "batch_scenarios",
        uuid_pk(),
        sa.Column("batch_id", sa.UUID(), nullable=False),
        sa.Column("index", sa.Integer(), nullable=False),
        sa.Column("feasible", sa.Boolean(), nullable=False),
        jsonb("buyer_mandate"),
        jsonb("seller_mandate"),
        jsonb("public_config"),
        *timestamps(),
        *non_negative("batch_scenarios", "index"),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["batches.id"], name=op.f("fk_batch_scenarios_batch_id_batches")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_batch_scenarios")),
        sa.UniqueConstraint("batch_id", "index", name=op.f("uq_batch_scenarios_batch_id_index")),
    )

    # --- Runs --------------------------------------------------------------------------------

    op.create_table(
        "runs",
        uuid_pk(),
        text_column("name"),
        sa.Column("parent_run_id", sa.UUID(), nullable=True),
        sa.Column("batch_id", sa.UUID(), nullable=True),
        text_column("scenario_id", nullable=True),
        text_column("deployment_id"),
        jsonb("public_config"),
        jsonb("limits"),
        sa.Column("buyer_policy", enum("policy_kind"), nullable=False),
        sa.Column("seller_policy", enum("policy_kind"), nullable=False),
        text_column("buyer_model_id", nullable=True),
        text_column("seller_model_id", nullable=True),
        text_column("buyer_effort", nullable=True),
        text_column("seller_effort", nullable=True),
        jsonb("policy_versions", default="{}"),
        jsonb("prompt_template_versions", default="{}"),
        text_column("software_version"),
        sa.Column("state", enum("run_state"), nullable=False),
        text_column("state_cause", nullable=True),
        sa.Column("mode", enum("run_mode"), server_default="live", nullable=False),
        sa.Column("outcome_kind", enum("outcome_kind"), server_default="pending", nullable=False),
        sa.Column("outcome_reason_code", sa.SmallInteger(), nullable=True),
        sa.Column("outcome_actor", enum("party_or_operator"), nullable=True),
        text_column("outcome_tx_hash", nullable=True),
        text_column("session_id", nullable=True),
        text_column("config_hash", nullable=True),
        sa.Column("session_expires_at_ts", sa.BigInteger(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("terminal_at", sa.DateTime(timezone=True), nullable=True),
        *timestamps(),
        sa.CheckConstraint(
            "outcome_kind = 'pending' OR "
            "(state IN ('terminal', 'failed_setup') AND outcome_tx_hash IS NOT NULL)",
            name=op.f("ck_runs_outcome_requires_terminal_event"),
        ),
        sa.CheckConstraint(
            # `IS NOT NULL` before every comparison: a CHECK passes on NULL, and
            # `'closed' AND NULL BETWEEN 1 AND 3` is NULL.
            "(outcome_kind IN ('pending', 'settled', 'expired') "
            "AND outcome_reason_code IS NULL) OR "
            "(outcome_kind = 'closed' AND outcome_reason_code IS NOT NULL "
            "AND outcome_reason_code BETWEEN 1 AND 3) OR "
            "(outcome_kind = 'aborted' AND outcome_reason_code IS NOT NULL "
            "AND outcome_reason_code BETWEEN 1 AND 4)",
            name=op.f("ck_runs_outcome_reason_matches_kind"),
        ),
        sa.CheckConstraint(
            "(outcome_kind = 'pending' AND outcome_actor IS NULL) OR "
            "(outcome_kind IN ('settled', 'closed') AND outcome_actor IS NOT NULL "
            "AND outcome_actor IN ('buyer', 'seller')) OR "
            "(outcome_kind = 'expired' AND outcome_actor IS NOT NULL "
            "AND outcome_actor = 'anyone') OR "
            "(outcome_kind = 'aborted' AND outcome_actor IS NOT NULL "
            "AND outcome_actor = 'operator')",
            name=op.f("ck_runs_outcome_actor_matches_kind"),
        ),
        *non_negative("runs", "session_expires_at_ts"),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["batches.id"], name=op.f("fk_runs_batch_id_batches")
        ),
        sa.ForeignKeyConstraint(
            ["deployment_id"],
            ["deployments.deployment_id"],
            name=op.f("fk_runs_deployment_id_deployments"),
        ),
        sa.ForeignKeyConstraint(
            ["parent_run_id"], ["runs.id"], name=op.f("fk_runs_parent_run_id_runs")
        ),
        sa.ForeignKeyConstraint(
            ["scenario_id"], ["scenarios.scenario_id"], name=op.f("fk_runs_scenario_id_scenarios")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_runs")),
        sa.UniqueConstraint("session_id", name=op.f("uq_runs_session_id")),
    )
    op.create_index(op.f("ix_runs_batch_id"), "runs", ["batch_id"])
    op.create_index(op.f("ix_runs_created_at_desc"), "runs", [sa.literal_column("created_at DESC")])
    op.create_index(op.f("ix_runs_state"), "runs", ["state"])

    op.create_table(
        "mandate_versions",
        uuid_pk(),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("party", enum("party"), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        amount("reservation_price_minor"),
        amount("min_remaining_inventory_minor"),
        text_column("instructions"),
        jsonb("extra", default="{}"),
        text_column("mandate_hash"),
        *timestamps(),
        sa.CheckConstraint("version >= 1", name=op.f("ck_mandate_versions_version_positive")),
        *non_negative(
            "mandate_versions", "reservation_price_minor", "min_remaining_inventory_minor"
        ),
        sa.CheckConstraint("extra = '{}'::jsonb", name=op.f("ck_mandate_versions_extra_is_empty")),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_mandate_versions_run_id_runs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_mandate_versions")),
        sa.UniqueConstraint("run_id", "party", name=op.f("uq_mandate_versions_run_id_party")),
    )

    op.create_table(
        "wallets",
        uuid_pk(),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("party", enum("party"), nullable=False),
        text_column("address"),
        text_column("key_ref"),
        jsonb("key_derivation"),
        amount("initial_base_minor"),
        amount("initial_quote_minor"),
        amount("allowance_minor"),
        sa.Column("setup_nonce_next", sa.BigInteger(), server_default="0", nullable=False),
        jsonb("funded_tx_hashes", default="{}"),
        *timestamps(),
        sa.CheckConstraint(
            "key_ref ~ '^(env|keystore):.+'", name=op.f("ck_wallets_key_ref_is_a_reference")
        ),
        *non_negative(
            "wallets",
            "initial_base_minor",
            "initial_quote_minor",
            "allowance_minor",
            "setup_nonce_next",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_wallets_run_id_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wallets")),
        sa.UniqueConstraint("address", name=op.f("uq_wallets_address")),
        sa.UniqueConstraint("run_id", "party", name=op.f("uq_wallets_run_id_party")),
    )

    op.create_table(
        "active_run",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        *timestamps(),
        sa.CheckConstraint("id = 1", name=op.f("ck_active_run_single_row")),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_active_run_run_id_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_active_run")),
        sa.UniqueConstraint("run_id", name=op.f("uq_active_run_run_id")),
    )

    op.create_table(
        "run_leases",
        sa.Column("run_id", sa.UUID(), nullable=False),
        text_column("holder"),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("relay_nonce_next", sa.BigInteger(), nullable=False),
        *timestamps(),
        *non_negative("run_leases", "relay_nonce_next"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_run_leases_run_id_runs")),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_run_leases")),
    )

    # --- Turns, decisions, signed actions ---------------------------------------------------

    op.create_table(
        "turns",
        uuid_pk(),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("turn", sa.Integer(), nullable=False),
        sa.Column("party", enum("party"), nullable=False),
        sa.Column("expected_sequence", sa.BigInteger(), nullable=False),
        sa.Column("state", enum("turn_state"), nullable=False),
        jsonb("observation"),
        text_column("observation_hash"),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        text_column("failure_code", nullable=True),
        text_column("failure_detail", nullable=True),
        *timestamps(),
        sa.CheckConstraint("turn >= 1", name=op.f("ck_turns_turn_positive")),
        sa.CheckConstraint(
            "expected_sequence >= 1", name=op.f("ck_turns_expected_sequence_positive")
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_turns_run_id_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_turns")),
        sa.UniqueConstraint("run_id", "turn", name=op.f("uq_turns_run_id_turn")),
    )

    op.create_table(
        "decisions",
        uuid_pk(),
        sa.Column("turn_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("party", enum("party"), nullable=False),
        sa.Column("attempt", sa.SmallInteger(), nullable=False),
        sa.Column("policy", enum("policy_kind"), nullable=False),
        text_column("model_id", nullable=True),
        text_column("effort", nullable=True),
        text_column("prompt_template_version", nullable=True),
        text_column("request_hash", nullable=True),
        jsonb("raw_response"),
        text_column("stop_reason", nullable=True),
        sa.Column("validation_ok", sa.Boolean(), nullable=False),
        text_column("validation_code", nullable=True),
        text_column("validation_feedback", nullable=True),
        jsonb("usage", nullable=True),
        usd("cost_estimated_usd"),
        usd("cost_reported_usd"),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("authorized", sa.Boolean(), server_default="false", nullable=False),
        *timestamps(),
        sa.CheckConstraint("attempt >= 1", name=op.f("ck_decisions_attempt_positive")),
        sa.CheckConstraint(
            "NOT authorized OR validation_ok", name=op.f("ck_decisions_authorized_implies_valid")
        ),
        *non_negative("decisions", "latency_ms"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_decisions_run_id_runs")),
        sa.ForeignKeyConstraint(["turn_id"], ["turns.id"], name=op.f("fk_decisions_turn_id_turns")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_decisions")),
        sa.UniqueConstraint("turn_id", "attempt", name=op.f("uq_decisions_turn_id_attempt")),
    )
    op.create_index(op.f("ix_decisions_run_id"), "decisions", ["run_id"])

    op.create_table(
        "signed_actions",
        uuid_pk(),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("turn_id", sa.UUID(), nullable=False),
        sa.Column("decision_id", sa.UUID(), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("kind", enum("action_kind"), nullable=False),
        jsonb("typed_message"),
        text_column("digest"),
        text_column("signer"),
        text_column("signature"),
        sa.Column("status", enum("action_status"), nullable=False),
        text_column("revert_error", nullable=True),
        *timestamps(),
        sa.CheckConstraint("sequence >= 1", name=op.f("ck_signed_actions_sequence_positive")),
        sa.ForeignKeyConstraint(
            ["decision_id"], ["decisions.id"], name=op.f("fk_signed_actions_decision_id_decisions")
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_signed_actions_run_id_runs")
        ),
        sa.ForeignKeyConstraint(
            ["turn_id"], ["turns.id"], name=op.f("fk_signed_actions_turn_id_turns")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_signed_actions")),
        sa.UniqueConstraint("decision_id", name=op.f("uq_signed_actions_decision_id")),
        sa.UniqueConstraint("digest", name=op.f("uq_signed_actions_digest")),
        sa.UniqueConstraint("run_id", "sequence", name=op.f("uq_signed_actions_run_id_sequence")),
        sa.UniqueConstraint("turn_id", name=op.f("uq_signed_actions_turn_id")),
    )

    # --- The outbox and the chain ---------------------------------------------------------------

    op.create_table(
        "tx_outbox",
        uuid_pk(),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("signed_action_id", sa.UUID(), nullable=True),
        sa.Column("kind", enum("tx_kind"), nullable=False),
        text_column("sender"),
        sa.Column("nonce", sa.BigInteger(), nullable=False),
        sa.Column("raw_tx", sa.LargeBinary(), nullable=False),
        text_column("tx_hash"),
        sa.Column("replaces_id", sa.UUID(), nullable=True),
        sa.Column("status", enum("tx_status"), nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        text_column("last_error", nullable=True),
        sa.Column("block_number", sa.BigInteger(), nullable=True),
        text_column("block_hash", nullable=True),
        amount("gas_used", nullable=True),
        amount("effective_gas_price_wei", nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("included_at", sa.DateTime(timezone=True), nullable=True),
        *timestamps(),
        *non_negative("tx_outbox", "nonce", "attempts", "block_number"),
        sa.ForeignKeyConstraint(
            ["replaces_id"], ["tx_outbox.id"], name=op.f("fk_tx_outbox_replaces_id_tx_outbox")
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_tx_outbox_run_id_runs")),
        sa.ForeignKeyConstraint(
            ["signed_action_id"],
            ["signed_actions.id"],
            name=op.f("fk_tx_outbox_signed_action_id_signed_actions"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tx_outbox")),
        sa.UniqueConstraint(
            "sender", "nonce", "tx_hash", name=op.f("uq_tx_outbox_sender_nonce_tx_hash")
        ),
        sa.UniqueConstraint("tx_hash", name=op.f("uq_tx_outbox_tx_hash")),
    )
    op.create_index(op.f("ix_tx_outbox_run_id"), "tx_outbox", ["run_id"])
    op.create_index(op.f("ix_tx_outbox_status"), "tx_outbox", ["status"])
    op.create_index(
        op.f("uq_tx_outbox_signed_action_id_live"),
        "tx_outbox",
        ["signed_action_id"],
        unique=True,
        postgresql_where=sa.text("status NOT IN ('replaced', 'dropped', 'reverted')"),
    )

    op.create_table(
        "chain_events",
        uuid_pk(),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("chain_id", sa.BigInteger(), nullable=False),
        text_column("contract_address"),
        text_column("session_id", nullable=True),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        text_column("block_hash"),
        text_column("tx_hash"),
        sa.Column("log_index", sa.Integer(), nullable=False),
        text_column("event_name"),
        jsonb("decoded"),
        jsonb("calldata", nullable=True),
        sa.Column("canonical", sa.Boolean(), server_default="true", nullable=False),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("confirmations_at_index", sa.Integer(), nullable=True),
        text_column("sentence", nullable=True),
        *timestamps(),
        sa.CheckConstraint(
            "canonical = (invalidated_at IS NULL)",
            name=op.f("ck_chain_events_canonical_iff_not_invalidated"),
        ),
        *non_negative("chain_events", "block_number", "log_index", "confirmations_at_index"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_chain_events_run_id_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chain_events")),
        sa.UniqueConstraint(
            "block_hash",
            "tx_hash",
            "log_index",
            name=op.f("uq_chain_events_block_hash_tx_hash_log_index"),
        ),
    )
    op.create_index(op.f("ix_chain_events_run_id"), "chain_events", ["run_id"])
    op.create_index(op.f("ix_chain_events_session_id"), "chain_events", ["session_id"])

    op.create_table(
        "balance_snapshots",
        uuid_pk(),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("stage", enum("snapshot_stage"), nullable=False),
        sa.Column("party", enum("party"), nullable=False),
        sa.Column("token", enum("token_role"), nullable=False),
        amount("amount_minor"),
        sa.Column("block_number", sa.BigInteger(), nullable=False),
        text_column("block_hash"),
        sa.Column("canonical", sa.Boolean(), server_default="true", nullable=False),
        *timestamps(),
        *non_negative("balance_snapshots", "amount_minor", "block_number"),
        sa.ForeignKeyConstraint(
            ["run_id"], ["runs.id"], name=op.f("fk_balance_snapshots_run_id_runs")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_balance_snapshots")),
        sa.UniqueConstraint(
            "run_id",
            "stage",
            "party",
            "token",
            "block_hash",
            name=op.f("uq_balance_snapshots_run_id_stage_party_token_block_hash"),
        ),
    )

    # --- Derived and operational records --------------------------------------------------------

    op.create_table(
        "run_metrics",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("recorded_offers", sa.Integer(), server_default="0", nullable=False),
        sa.Column("decision_time_ms", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("chain_wait_ms", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("setup_chain_wait_ms", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("model_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("input_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("output_tokens", sa.Integer(), server_default="0", nullable=False),
        usd("model_cost_estimated_usd"),
        usd("model_cost_reported_usd"),
        amount("gas_used_negotiation", default="0"),
        amount("gas_used_setup", default="0"),
        amount("fee_wei_negotiation", default="0"),
        amount("fee_wei_setup", default="0"),
        amount("settled_quote_minor", nullable=True),
        amount("buyer_utility_minor", nullable=True),
        amount("seller_utility_minor", nullable=True),
        amount("captured_surplus_minor", nullable=True),
        sa.Column("feasible", sa.Boolean(), nullable=True),
        amount("feasible_surplus_minor", nullable=True),
        sa.Column("mandate_violations", sa.Integer(), server_default="0", nullable=False),
        text_column("failure_class", nullable=True),
        sa.Column("audit_complete", sa.Boolean(), server_default="false", nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
        *timestamps(),
        sa.CheckConstraint(
            "failure_class IS NULL OR "
            "failure_class IN ('model', 'signing', 'rpc', 'execution', 'none')",
            name=op.f("ck_run_metrics_failure_class_known"),
        ),
        *non_negative(
            "run_metrics",
            "recorded_offers",
            "decision_time_ms",
            "chain_wait_ms",
            "setup_chain_wait_ms",
            "model_calls",
            "input_tokens",
            "output_tokens",
            "mandate_violations",
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_run_metrics_run_id_runs")),
        sa.PrimaryKeyConstraint("run_id", name=op.f("pk_run_metrics")),
    )

    op.create_table(
        "run_events",
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("cursor", sa.BigInteger(), autoincrement=False, nullable=False),
        text_column("event_type"),
        jsonb("data"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("cursor >= 1", name=op.f("ck_run_events_cursor_positive")),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_run_events_run_id_runs")),
        sa.PrimaryKeyConstraint("run_id", "cursor", name=op.f("pk_run_events")),
    )

    op.create_table(
        "operations",
        uuid_pk(),
        text_column("kind"),
        sa.Column("run_id", sa.UUID(), nullable=True),
        sa.Column("batch_id", sa.UUID(), nullable=True),
        text_column("idempotency_key", nullable=True),
        text_column("route"),
        text_column("request_hash"),
        sa.Column("status", enum("operation_status"), nullable=False),
        jsonb("result", nullable=True),
        jsonb("error", nullable=True),
        *timestamps(),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["batches.id"], name=op.f("fk_operations_batch_id_batches")
        ),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], name=op.f("fk_operations_run_id_runs")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_operations")),
        sa.UniqueConstraint(
            "route", "idempotency_key", name=op.f("uq_operations_route_idempotency_key")
        ),
    )
    op.create_index(op.f("ix_operations_run_id"), "operations", ["run_id"])

    _create_immutability_triggers()


# Data model section 8. `restrict_violation` (SQLSTATE 23001) puts the refusal in the integrity
# constraint class, so the driver reports it as an integrity error like any other constraint and
# the repositories translate it into `ImmutableRowError` by its code.
TRIGGER_FUNCTIONS = {
    "forbid_modification": """
        BEGIN
            RAISE EXCEPTION '% on % refused: rows are immutable', TG_OP, TG_TABLE_NAME
                USING ERRCODE = 'restrict_violation';
        END
    """,
    "signed_actions_identity_is_immutable": """
        BEGIN
            IF (NEW.run_id, NEW.turn_id, NEW.decision_id, NEW.sequence, NEW.kind,
                NEW.typed_message, NEW.digest, NEW.signer, NEW.signature)
               IS DISTINCT FROM
               (OLD.run_id, OLD.turn_id, OLD.decision_id, OLD.sequence, OLD.kind,
                OLD.typed_message, OLD.digest, OLD.signer, OLD.signature)
            THEN
                RAISE EXCEPTION
                    'UPDATE on signed_actions refused: only status and revert_error may change'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END
    """,
    "chain_events_evidence_is_immutable": """
        BEGIN
            IF (NEW.chain_id, NEW.contract_address, NEW.block_number, NEW.block_hash,
                NEW.tx_hash, NEW.log_index, NEW.event_name, NEW.decoded)
               IS DISTINCT FROM
               (OLD.chain_id, OLD.contract_address, OLD.block_number, OLD.block_hash,
                OLD.tx_hash, OLD.log_index, OLD.event_name, OLD.decoded)
            THEN
                RAISE EXCEPTION
                    'UPDATE on chain_events refused: the evidence columns are immutable'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END
    """,
}

#: (trigger, table, timing and events, function)
TRIGGERS = (
    ("mandate_versions_immutable", "mandate_versions", "UPDATE OR DELETE", "forbid_modification"),
    ("run_events_append_only", "run_events", "UPDATE OR DELETE", "forbid_modification"),
    (
        "signed_actions_identity_immutable",
        "signed_actions",
        "UPDATE",
        "signed_actions_identity_is_immutable",
    ),
    (
        "chain_events_evidence_immutable",
        "chain_events",
        "UPDATE",
        "chain_events_evidence_is_immutable",
    ),
)


def _create_immutability_triggers() -> None:
    for name, body in TRIGGER_FUNCTIONS.items():
        op.execute(
            f"CREATE FUNCTION {name}() RETURNS trigger LANGUAGE plpgsql AS $body${body}$body$"
        )
    for trigger, table, events, function in TRIGGERS:
        op.execute(
            f"CREATE TRIGGER {trigger} BEFORE {events} ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {function}()"
        )


def downgrade() -> None:
    for trigger, table, _, _ in TRIGGERS:
        op.execute(f"DROP TRIGGER {trigger} ON {table}")
    for name in TRIGGER_FUNCTIONS:
        op.execute(f"DROP FUNCTION {name}()")

    # Reverse dependency order: every table is dropped after everything that refers to it.
    for table in (
        "operations",
        "run_events",
        "run_metrics",
        "balance_snapshots",
        "chain_events",
        "tx_outbox",
        "signed_actions",
        "decisions",
        "turns",
        "run_leases",
        "active_run",
        "wallets",
        "mandate_versions",
        "runs",
        "batch_scenarios",
        "batches",
        "deployments",
        "scenarios",
    ):
        op.drop_table(table)

    bind = op.get_bind()
    for name in reversed(list(ENUMS)):
        enum(name).drop(bind, checkfirst=False)
