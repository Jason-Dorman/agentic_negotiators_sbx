"""The migrated schema is exactly the one docs/data_model.md describes.

Everything below is **transcribed by hand from the document**, not derived from the models or the
migration. That is the point: the stage 1 review found a schema carrying a field its document did
not list, which no value-level test could see because nothing compared the names with the document.
Here the comparison is exact in both directions — a column the document lists and the database
lacks fails, and so does one the database has and the document does not mention.

What is compared: every table, every column's type and nullability, every enum's values in order,
the set of unique constraints and unique indexes, the names of the check constraints, and the
triggers. Plain (non-unique) indexes and foreign keys are compared against the models by Alembic's
autogenerate check in `test_db_migrations.py` instead.

When the data model changes, this file changes with it, by hand, in the same change set.
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

# --- Section 4, verbatim ------------------------------------------------------------------------

ENUMS: dict[str, list[str]] = {
    "party": ["buyer", "seller"],
    "party_or_operator": ["buyer", "seller", "operator", "anyone"],
    "policy_kind": ["deterministic", "model"],
    "run_mode": ["live", "fixture"],
    "run_state": [
        "draft",
        "validated",
        "preparing",
        "running",
        "paused",
        "recovery_required",
        "terminal",
        "failed_setup",
    ],
    "outcome_kind": ["pending", "settled", "closed", "expired", "aborted"],
    "turn_state": [
        "observing",
        "deciding",
        "repairing",
        "signing",
        "broadcasting",
        "confirming",
        "confirmed",
        "model_failed",
        "execution_failed",
    ],
    "action_kind": ["offer", "accept", "close"],
    "action_status": [
        "signed",
        "submitted",
        "included",
        "confirmed",
        "finalized",
        "reverted",
        "superseded",
    ],
    "tx_kind": [
        "record_offer",
        "accept_and_settle",
        "close_session",
        "expire_session",
        "abort_session",
        "create_session",
        "mint",
        "approve",
        "fund_eth",
    ],
    "tx_status": [
        "pending",
        "submitted",
        "included",
        "confirmed",
        "finalized",
        "reverted",
        "replaced",
        "dropped",
    ],
    "snapshot_stage": ["pre_setup", "post_setup", "pre_settlement", "post_settlement", "terminal"],
    "token_role": ["base", "quote", "eth"],
    "operation_status": ["pending", "running", "succeeded", "failed"],
}

# --- Section 3, one table at a time --------------------------------------------------------------

NN = "NOT NULL"
N = "NULL"
AMOUNT = "NUMERIC(78,0)"
USD = "NUMERIC(12,6)"
TS = "TIMESTAMPTZ"

# "Every table has created_at and updated_at, except run_events ... A table whose primary key is
# stated below has no id; every other table has id UUID PRIMARY KEY."
COMMON = {"created_at": f"{TS} {NN}", "updated_at": f"{TS} {NN}"}
ID = {"id": f"UUID {NN}"}

TABLES: dict[str, dict[str, str]] = {
    "scenarios": {
        "scenario_id": f"TEXT {NN}",
        "name": f"TEXT {NN}",
        "description": f"TEXT {NN}",
        "public_config": f"JSONB {NN}",
        "buyer_template": f"JSONB {NN}",
        "seller_template": f"JSONB {NN}",
        "source_hash": f"TEXT {NN}",
        **COMMON,
    },
    "deployments": {
        "deployment_id": f"TEXT {NN}",
        "chain_id": f"BIGINT {NN}",
        "protocol_version": f"TEXT {NN}",
        "exchange_address": f"TEXT {NN}",
        "base_token_address": f"TEXT {NN}",
        "quote_token_address": f"TEXT {NN}",
        "operator_address": f"TEXT {NN}",
        "relay_address": f"TEXT {NN}",
        "code_hashes": f"JSONB {NN}",
        "compiler": f"JSONB {NN}",
        "explorer_base_url": f"TEXT {N}",
        "ens": f"JSONB {N}",
        "manifest": f"JSONB {NN}",
        "start_block": f"BIGINT {NN}",
        "deployed_at": f"{TS} {NN}",
        **COMMON,
    },
    "runs": {
        **ID,
        "name": f"TEXT {NN}",
        "parent_run_id": f"UUID {N}",
        "batch_id": f"UUID {N}",
        "scenario_id": f"TEXT {N}",
        "deployment_id": f"TEXT {NN}",
        "public_config": f"JSONB {NN}",
        "limits": f"JSONB {NN}",
        "buyer_policy": f"policy_kind {NN}",
        "seller_policy": f"policy_kind {NN}",
        "buyer_model_id": f"TEXT {N}",
        "seller_model_id": f"TEXT {N}",
        "buyer_effort": f"TEXT {N}",
        "seller_effort": f"TEXT {N}",
        "policy_versions": f"JSONB {NN}",
        "prompt_template_versions": f"JSONB {NN}",
        "software_version": f"TEXT {NN}",
        "state": f"run_state {NN}",
        "state_cause": f"TEXT {N}",
        "mode": f"run_mode {NN}",
        "outcome_kind": f"outcome_kind {NN}",
        "outcome_reason_code": f"SMALLINT {N}",
        "outcome_actor": f"party_or_operator {N}",
        "outcome_tx_hash": f"TEXT {N}",
        "session_id": f"TEXT {N}",
        "config_hash": f"TEXT {N}",
        "session_expires_at_ts": f"BIGINT {N}",
        "started_at": f"{TS} {N}",
        "terminal_at": f"{TS} {N}",
        **COMMON,
    },
    "mandate_versions": {
        **ID,
        "run_id": f"UUID {NN}",
        "party": f"party {NN}",
        "version": f"INTEGER {NN}",
        "reservation_price_minor": f"{AMOUNT} {NN}",
        "min_remaining_inventory_minor": f"{AMOUNT} {NN}",
        "instructions": f"TEXT {NN}",
        "extra": f"JSONB {NN}",
        "mandate_hash": f"TEXT {NN}",
        **COMMON,
    },
    "wallets": {
        **ID,
        "run_id": f"UUID {NN}",
        "party": f"party {NN}",
        "address": f"TEXT {NN}",
        "key_ref": f"TEXT {NN}",
        "key_derivation": f"JSONB {NN}",
        "initial_base_minor": f"{AMOUNT} {NN}",
        "initial_quote_minor": f"{AMOUNT} {NN}",
        "allowance_minor": f"{AMOUNT} {NN}",
        "setup_nonce_next": f"BIGINT {NN}",
        "funded_tx_hashes": f"JSONB {NN}",
        **COMMON,
    },
    "run_leases": {
        "run_id": f"UUID {NN}",
        "holder": f"TEXT {NN}",
        "expires_at": f"{TS} {NN}",
        "relay_nonce_next": f"BIGINT {NN}",
        **COMMON,
    },
    "active_run": {
        "id": f"SMALLINT {NN}",
        "run_id": f"UUID {NN}",
        **COMMON,
    },
    "turns": {
        **ID,
        "run_id": f"UUID {NN}",
        "turn": f"INTEGER {NN}",
        "party": f"party {NN}",
        "expected_sequence": f"BIGINT {NN}",
        "state": f"turn_state {NN}",
        "observation": f"JSONB {NN}",
        "observation_hash": f"TEXT {NN}",
        "started_at": f"{TS} {NN}",
        "finished_at": f"{TS} {N}",
        "failure_code": f"TEXT {N}",
        "failure_detail": f"TEXT {N}",
        **COMMON,
    },
    "decisions": {
        **ID,
        "turn_id": f"UUID {NN}",
        "run_id": f"UUID {NN}",
        "party": f"party {NN}",
        "attempt": f"SMALLINT {NN}",
        "policy": f"policy_kind {NN}",
        "model_id": f"TEXT {N}",
        "effort": f"TEXT {N}",
        "prompt_template_version": f"TEXT {N}",
        "request_hash": f"TEXT {N}",
        "raw_response": f"JSONB {NN}",
        "stop_reason": f"TEXT {N}",
        "validation_ok": f"BOOLEAN {NN}",
        "validation_code": f"TEXT {N}",
        "validation_feedback": f"TEXT {N}",
        "usage": f"JSONB {N}",
        "cost_estimated_usd": f"{USD} {N}",
        "cost_reported_usd": f"{USD} {N}",
        "latency_ms": f"INTEGER {N}",
        "requested_at": f"{TS} {NN}",
        "authorized": f"BOOLEAN {NN}",
        **COMMON,
    },
    "signed_actions": {
        **ID,
        "run_id": f"UUID {NN}",
        "turn_id": f"UUID {NN}",
        "decision_id": f"UUID {NN}",
        "sequence": f"BIGINT {NN}",
        "kind": f"action_kind {NN}",
        "typed_message": f"JSONB {NN}",
        "digest": f"TEXT {NN}",
        "signer": f"TEXT {NN}",
        "signature": f"TEXT {NN}",
        "status": f"action_status {NN}",
        "revert_error": f"TEXT {N}",
        **COMMON,
    },
    "tx_outbox": {
        **ID,
        "run_id": f"UUID {NN}",
        "signed_action_id": f"UUID {N}",
        "kind": f"tx_kind {NN}",
        "sender": f"TEXT {NN}",
        "nonce": f"BIGINT {NN}",
        "raw_tx": f"BYTEA {NN}",
        "tx_hash": f"TEXT {NN}",
        "replaces_id": f"UUID {N}",
        "status": f"tx_status {NN}",
        "attempts": f"INTEGER {NN}",
        "last_error": f"TEXT {N}",
        "block_number": f"BIGINT {N}",
        "block_hash": f"TEXT {N}",
        "gas_used": f"{AMOUNT} {N}",
        "effective_gas_price_wei": f"{AMOUNT} {N}",
        "submitted_at": f"{TS} {N}",
        "included_at": f"{TS} {N}",
        **COMMON,
    },
    "chain_events": {
        **ID,
        "run_id": f"UUID {N}",
        "chain_id": f"BIGINT {NN}",
        "contract_address": f"TEXT {NN}",
        "session_id": f"TEXT {N}",
        "block_number": f"BIGINT {NN}",
        "block_hash": f"TEXT {NN}",
        "tx_hash": f"TEXT {NN}",
        "log_index": f"INTEGER {NN}",
        "event_name": f"TEXT {NN}",
        "decoded": f"JSONB {NN}",
        "calldata": f"JSONB {N}",
        "canonical": f"BOOLEAN {NN}",
        "invalidated_at": f"{TS} {N}",
        "confirmations_at_index": f"INTEGER {N}",
        "sentence": f"TEXT {N}",
        **COMMON,
    },
    "balance_snapshots": {
        **ID,
        "run_id": f"UUID {NN}",
        "stage": f"snapshot_stage {NN}",
        "party": f"party {NN}",
        "token": f"token_role {NN}",
        "amount_minor": f"{AMOUNT} {NN}",
        "block_number": f"BIGINT {NN}",
        "block_hash": f"TEXT {NN}",
        "canonical": f"BOOLEAN {NN}",
        **COMMON,
    },
    "run_metrics": {
        "run_id": f"UUID {NN}",
        "recorded_offers": f"INTEGER {NN}",
        "decision_time_ms": f"BIGINT {NN}",
        "chain_wait_ms": f"BIGINT {NN}",
        "setup_chain_wait_ms": f"BIGINT {NN}",
        "model_calls": f"INTEGER {NN}",
        "input_tokens": f"INTEGER {NN}",
        "output_tokens": f"INTEGER {NN}",
        "model_cost_estimated_usd": f"{USD} {N}",
        "model_cost_reported_usd": f"{USD} {N}",
        "gas_used_negotiation": f"{AMOUNT} {NN}",
        "gas_used_setup": f"{AMOUNT} {NN}",
        "fee_wei_negotiation": f"{AMOUNT} {NN}",
        "fee_wei_setup": f"{AMOUNT} {NN}",
        "settled_quote_minor": f"{AMOUNT} {N}",
        "buyer_utility_minor": f"{AMOUNT} {N}",
        "seller_utility_minor": f"{AMOUNT} {N}",
        "captured_surplus_minor": f"{AMOUNT} {N}",
        "feasible": f"BOOLEAN {N}",
        "feasible_surplus_minor": f"{AMOUNT} {N}",
        "mandate_violations": f"INTEGER {NN}",
        "failure_class": f"TEXT {N}",
        "audit_complete": f"BOOLEAN {NN}",
        "computed_at": f"{TS} {NN}",
        **COMMON,
    },
    "run_events": {
        "run_id": f"UUID {NN}",
        "cursor": f"BIGINT {NN}",
        "event_type": f"TEXT {NN}",
        "data": f"JSONB {NN}",
        "created_at": f"{TS} {NN}",
    },
    "operations": {
        **ID,
        "kind": f"TEXT {NN}",
        "run_id": f"UUID {N}",
        "batch_id": f"UUID {N}",
        "idempotency_key": f"TEXT {N}",
        "route": f"TEXT {NN}",
        "request_hash": f"TEXT {NN}",
        "status": f"operation_status {NN}",
        "result": f"JSONB {N}",
        "error": f"JSONB {N}",
        **COMMON,
    },
    "batches": {
        **ID,
        "name": f"TEXT {NN}",
        "population": f"JSONB {NN}",
        "pairings": f"TEXT[] {NN}",
        "repetitions": f"INTEGER {NN}",
        "template_run": f"JSONB {NN}",
        "status": f"TEXT {NN}",
        "report": f"JSONB {N}",
        **COMMON,
    },
    "batch_scenarios": {
        **ID,
        "batch_id": f"UUID {NN}",
        "index": f"INTEGER {NN}",
        "feasible": f"BOOLEAN {NN}",
        "buyer_mandate": f"JSONB {NN}",
        "seller_mandate": f"JSONB {NN}",
        "public_config": f"JSONB {NN}",
        **COMMON,
    },
}

#: Every "Unique:" line and every `UNIQUE` column in section 3, plus the partial index of 3.10 and
#: the primary keys, which are unique by definition.
UNIQUE: dict[str, set[tuple[str, ...]]] = {
    "scenarios": {("scenario_id",)},
    "deployments": {("deployment_id",), ("chain_id", "exchange_address")},
    "runs": {("id",), ("session_id",)},
    "mandate_versions": {("id",), ("run_id", "party")},
    "wallets": {("id",), ("run_id", "party"), ("address",)},
    "run_leases": {("run_id",)},
    "active_run": {("id",), ("run_id",)},
    "turns": {("id",), ("run_id", "turn")},
    "decisions": {("id",), ("turn_id", "attempt")},
    "signed_actions": {
        ("id",),
        ("run_id", "sequence"),
        ("digest",),
        ("decision_id",),
        ("turn_id",),
    },
    "tx_outbox": {
        ("id",),
        ("sender", "nonce", "tx_hash"),
        ("tx_hash",),
        ("signed_action_id",),  # partial: WHERE status NOT IN ('replaced','dropped','reverted')
    },
    "chain_events": {("id",), ("block_hash", "tx_hash", "log_index")},
    "balance_snapshots": {("id",), ("run_id", "stage", "party", "token", "block_hash")},
    "run_metrics": {("run_id",)},
    "run_events": {("run_id", "cursor")},
    "operations": {("id",), ("route", "idempotency_key")},
    "batches": {("id",)},
    "batch_scenarios": {("id",), ("batch_id", "index")},
}


def _non_negative(table: str, *columns: str) -> set[str]:
    return {f"ck_{table}_{column}_non_negative" for column in columns}


#: Every CHECK the document names, by the convention section 3 states.
CHECKS: dict[str, set[str]] = {
    "deployments": _non_negative("deployments", "start_block"),
    "runs": {
        "ck_runs_outcome_requires_terminal_event",
        "ck_runs_outcome_reason_matches_kind",
        "ck_runs_outcome_actor_matches_kind",
    }
    | _non_negative("runs", "session_expires_at_ts"),
    "mandate_versions": {
        "ck_mandate_versions_version_positive",
        "ck_mandate_versions_extra_is_empty",
    }
    | _non_negative("mandate_versions", "reservation_price_minor", "min_remaining_inventory_minor"),
    "wallets": {"ck_wallets_key_ref_is_a_reference"}
    | _non_negative(
        "wallets",
        "initial_base_minor",
        "initial_quote_minor",
        "allowance_minor",
        "setup_nonce_next",
    ),
    "run_leases": _non_negative("run_leases", "relay_nonce_next"),
    "active_run": {"ck_active_run_single_row"},
    "turns": {"ck_turns_turn_positive", "ck_turns_expected_sequence_positive"},
    "decisions": {"ck_decisions_attempt_positive", "ck_decisions_authorized_implies_valid"}
    | _non_negative("decisions", "latency_ms"),
    "signed_actions": {"ck_signed_actions_sequence_positive"},
    "tx_outbox": _non_negative("tx_outbox", "nonce", "attempts", "block_number"),
    "chain_events": {"ck_chain_events_canonical_iff_not_invalidated"}
    | _non_negative("chain_events", "block_number", "log_index", "confirmations_at_index"),
    "balance_snapshots": _non_negative("balance_snapshots", "amount_minor", "block_number"),
    "run_metrics": {"ck_run_metrics_failure_class_known"}
    | _non_negative(
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
    "run_events": {"ck_run_events_cursor_positive"},
    "batches": _non_negative("batches", "repetitions"),
    "batch_scenarios": _non_negative("batch_scenarios", "index"),
}

#: Section 8's table.
TRIGGERS = {
    ("mandate_versions", "mandate_versions_immutable"),
    ("run_events", "run_events_append_only"),
    ("signed_actions", "signed_actions_identity_immutable"),
    ("chain_events", "chain_events_evidence_immutable"),
}

# --- The database's view -------------------------------------------------------------------------

_PG_TYPES = {
    "text": "TEXT",
    "uuid": "UUID",
    "bigint": "BIGINT",
    "integer": "INTEGER",
    "smallint": "SMALLINT",
    "jsonb": "JSONB",
    "boolean": "BOOLEAN",
    "bytea": "BYTEA",
    "timestamp with time zone": "TIMESTAMPTZ",
}

COLUMNS_SQL = """
SELECT table_name, column_name, is_nullable, data_type, udt_name, numeric_precision, numeric_scale
FROM information_schema.columns
WHERE table_schema = 'public' AND table_name <> 'alembic_version'
"""

ENUMS_SQL = """
SELECT t.typname, e.enumlabel
FROM pg_type t JOIN pg_enum e ON e.enumtypid = t.oid
JOIN pg_namespace n ON n.oid = t.typnamespace
WHERE n.nspname = 'public'
ORDER BY t.typname, e.enumsortorder
"""

UNIQUE_SQL = """
SELECT t.relname, array_agg(a.attname ORDER BY k.ordinality)
FROM pg_index i
JOIN pg_class t ON t.oid = i.indrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ordinality) ON true
JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
WHERE n.nspname = 'public' AND i.indisunique AND t.relname <> 'alembic_version'
GROUP BY t.relname, i.indexrelid
"""

CHECKS_SQL = """
SELECT t.relname, c.conname
FROM pg_constraint c
JOIN pg_class t ON t.oid = c.conrelid
JOIN pg_namespace n ON n.oid = t.relnamespace
WHERE n.nspname = 'public' AND c.contype = 'c'
"""

TRIGGERS_SQL = """
SELECT event_object_table, trigger_name FROM information_schema.triggers
WHERE trigger_schema = 'public'
"""


def _type(data_type: str, udt_name: str, precision: object, scale: object) -> str:
    if data_type == "USER-DEFINED":
        return udt_name
    if data_type == "numeric":
        return f"NUMERIC({precision},{scale})"
    if data_type == "ARRAY":
        return {"_text": "TEXT[]"}[udt_name]
    return _PG_TYPES[data_type]


async def _read_catalog(url: str) -> dict[str, list[tuple[object, ...]]]:
    engine = create_async_engine(url)
    try:
        async with engine.connect() as connection:
            return {
                name: [tuple(row) for row in (await connection.execute(text(sql))).all()]
                for name, sql in (
                    ("columns", COLUMNS_SQL),
                    ("enums", ENUMS_SQL),
                    ("unique", UNIQUE_SQL),
                    ("checks", CHECKS_SQL),
                    ("triggers", TRIGGERS_SQL),
                )
            }
    finally:
        await engine.dispose()


@pytest.fixture(scope="module")
def catalog(migrated_url: str) -> dict[str, list[tuple[object, ...]]]:
    return asyncio.run(_read_catalog(migrated_url))


def test_every_table_and_no_other(catalog: dict[str, list[tuple[object, ...]]]) -> None:
    tables = {str(row[0]) for row in catalog["columns"]}
    assert tables == set(TABLES)


def test_every_column_with_its_type_and_nullability(
    catalog: dict[str, list[tuple[object, ...]]],
) -> None:
    actual: dict[str, dict[str, str]] = {}
    for table, column, nullable, data_type, udt, precision, scale in catalog["columns"]:
        described = _type(str(data_type), str(udt), precision, scale)
        actual.setdefault(str(table), {})[str(column)] = (
            f"{described} {'NULL' if nullable == 'YES' else 'NOT NULL'}"
        )
    for table, columns in TABLES.items():
        assert actual.get(table) == columns, table


def test_every_enum_with_its_values_in_order(
    catalog: dict[str, list[tuple[object, ...]]],
) -> None:
    actual: dict[str, list[str]] = {}
    for name, label in catalog["enums"]:
        actual.setdefault(str(name), []).append(str(label))
    assert actual == ENUMS


def test_every_uniqueness_rule(catalog: dict[str, list[tuple[object, ...]]]) -> None:
    actual: dict[str, set[tuple[str, ...]]] = {}
    for table, columns in catalog["unique"]:
        assert isinstance(columns, list), "array_agg returns a text[]"
        actual.setdefault(str(table), set()).add(tuple(str(column) for column in columns))
    assert actual == UNIQUE


def test_every_check_constraint_by_name(
    catalog: dict[str, list[tuple[object, ...]]],
) -> None:
    actual: dict[str, set[str]] = {}
    for table, name in catalog["checks"]:
        actual.setdefault(str(table), set()).add(str(name))
    assert actual == CHECKS


def test_every_trigger(catalog: dict[str, list[tuple[object, ...]]]) -> None:
    # information_schema lists a BEFORE UPDATE OR DELETE trigger once per event.
    assert {(str(table), str(name)) for table, name in catalog["triggers"]} == TRIGGERS
