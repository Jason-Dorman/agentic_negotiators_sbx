# API Contract

| | |
|---|---|
| **Version** | 0.1.0 (URL prefix `/v1`) |
| **Date** | 19 September 2026 |
| **Status** | Built in stage 2.5 (`services/api/src/api/routes/`). The FastAPI OpenAPI document is authoritative and must match this contract: it is snapshotted in `services/api/tests/contract/openapi.snapshot.json`, and the contract test also checks the routes against the headings of section 2 (section 8) |
| **Source** | Spec sections 3, 5.4, 10.1 |
| **Related** | [protocol.md](protocol.md), [data_model.md](data_model.md), [architecture.md](architecture.md) |

Two APIs are defined: the **operator API** served by the backend to the browser and CLI, and the **agent internal API** served by each agent service to the backend only.

---

## 1. Conventions

| Concern | Rule |
|---|---|
| Base URL | `http://localhost:8000/v1` in development |
| Content type | `application/json; charset=utf-8` for requests and responses, except SSE |
| Identifiers | `run_id`, `operation_id`, `batch_id`, `decision_id`: UUID v4 strings. `session_id`, `config_hash`, `offer_hash`, tx hashes: `0x` + 64 lowercase hex. Addresses: `0x` + 40 hex, EIP-55 checksummed in responses. |
| Amounts | Base-10 integer strings in minor units, never JSON numbers. Field names end in `_minor`. |
| Timestamps | Application timestamps: ISO 8601 UTC with `Z`, field names end in `_at`. Chain timestamps: integer Unix seconds, field names end in `_ts` or are named `expires_at_ts`. |
| Enums | Lowercase snake_case strings. Integer codes appear only where the protocol defines them (`reason_code`). |
| Idempotency | Every `POST` accepts `Idempotency-Key` (UUID; anything else is `400 bad_request`). A repeated key with the same body returns the stored response with `Idempotent-Replayed: true`. Same key with a different body returns `409 idempotency_conflict`. Keys are scoped per route — the concrete path, `POST /v1/runs/<run id>/start`, so per run — and retained 24 h. Since stage 2.5 ([ADR-078](decision_log.md)): the key is claimed before the work, so of two racing requests one does it; only success is stored and replayed, so a refused request — a cancelled one too — frees its key and a retry with it does the work again; the same key while the first request is still being answered is `409 idempotency_conflict` for a request answered at once, and for a long-running route its live operation, `202` with `Idempotent-Replayed: true` (ADR-078 as amended); a long-running route's replay is always its operation record as it stands now. A claim whose request never answered — the process died — is released `IDEMPOTENCY_CLAIM_TIMEOUT_S` (60 s) after the claim when the key is next seen, and at start-up, its operation recorded `failed` with code `interrupted`. "Same body" means the same `json_sha256` of the JSON, so key order and whitespace do not matter. |
| Operations | Any route that may take more than one second returns `202` with an operation record and `Location: /v1/operations/{operation_id}`: `start`, `step`, `resume` and `abort`. The record is `running` once the controller has made the run's transition, and records how that ended (section 1.2). |
| Authentication | None on localhost by default. When `OPERATOR_TOKEN` is configured, every route except `/health` requires `Authorization: Bearer <token>`. Remote exposure additionally requires TLS termination in front of the backend, and exposing the API on a network the operator does not control requires the STRIDE review in [security_and_trust_boundaries.md](security_and_trust_boundaries.md) section 8.1 first ([ADR-028](decision_log.md)). |
| Privacy headers | Routes that reveal private inputs require `X-Observer-Reveal: true`. Without it they return `403 reveal_required`. The header is a deliberate friction, not a security boundary. |
| Pagination | List routes take `limit` (default 50, max 200) and `cursor`; responses carry `next_cursor` or `null`. A cursor is opaque; one that does not decode as one of this API's is `400 bad_request`. It is not signed, so a well-formed hand-made cursor is read as a position. |
| Versioning | Breaking changes bump the URL prefix. Additive fields are non-breaking. Clients must ignore unknown fields. |

### 1.1 Error envelope

```json
{
  "error": {
    "code": "mandate_immutable",
    "message": "Mandates cannot change during an active run. Clone the run instead.",
    "details": { "run_id": "…", "state": "running" },
    "request_id": "…"
  }
}
```

HTTP status follows the code table in section 7.

### 1.2 Operation record

```json
{
  "operation_id": "…",
  "kind": "start_run",
  "run_id": "…",
  "status": "pending" | "running" | "succeeded" | "failed",
  "created_at": "…",
  "updated_at": "…",
  "result": { … } | null,
  "error": { … } | null
}
```

`kind` is `start_run`, `step_run`, `resume_run` or `abort_run` for the long-running routes, and `create_run`, `validate_run`, `pause_run` or `clone_run` for a request answered at once that carried an idempotency key, whose `result` is the stored response. A long-running operation is decided by the run's state changes ([ADR-073](decision_log.md)):

| `kind` | `succeeded` when the run is | `failed` when the run is |
|---|---|---|
| `start_run` | `running`, or `terminal` if it ended first | `recovery_required` or `failed_setup` |
| `step_run` | `paused` with cause `step_complete`, or `terminal` | `recovery_required` or `failed_setup`; for a start or a step also `paused` with cause `reorg` or `session_open`, short of what it asked for (Q64) |
| `resume_run` | `running`; `paused` or `preparing` with cause `recovered`; or `terminal` | `recovery_required` or `failed_setup` |
| `abort_run` | `terminal` or `failed_setup` — the outcome says whether the abort or a settlement won | `recovery_required`, reached after the abort was sent; the state an abort was sent from does not count |

`result` is the deciding state change, `{ "state", "state_cause", "outcome" }`; `error` is `{ "code": <the state>, "message", "details": <the same> }`. A request the controller refuses is answered with its error, and its operation is recorded `failed` with that error's code. An operation whose request died with its process before it was accepted is `failed` with code `interrupted` at the next start-up; an accepted one is tracked again from the events written since. Each status change is also an `operation` event on the run's stream (section 3).

---

## 2. Operator API

### 2.1 System

#### `GET /v1/health`

```json
{ "status": "ok", "version": "0.1.0", "chain_id": 31337, "rpc_ok": true, "db_ok": true, "agent_a_ok": true, "agent_b_ok": true }
```

`503` with the same shape, `status` `"degraded"`, when any dependency is down. `version` is the backend's `SOFTWARE_VERSION`. `rpc_ok` means the RPC answered with the deployment's chain ID and, since stage 2.5's review, its genesis block, so a restarted Anvil is not taken for the chain it replaced ([ADR-081](decision_log.md)); `agent_*_ok` that the agent answered as its role with its signer loaded. Never behind the operator token.

#### `GET /v1/deployments`

Lists deployment manifests known to the backend. The backend serves the one named by `DEPLOYMENT_MANIFEST`, which it validates and upserts at start-up; any earlier one stored stays listed. At start-up it checks the RPC is the manifest's chain — its chain ID, the exchange's code at its address, and for a deployment loaded before the same genesis block — and refuses to start otherwise; the deployment is stored with its chain's genesis hash ([ADR-081](decision_log.md)). Runs belong to their deployment: an operation on a run of a deployment this backend does not serve is `409 invalid_state` with `details.deployment`.

`explorer_base_url` is `https://sepolia.etherscan.io` for chain 11155111 and `null` for the local chain, which has no explorer. Every transaction the backend records on a chain with an explorer base URL exposes a resolvable `explorer_url`, so an observer can open any recorded action on Etherscan without leaving the evidence trail.

`ens` is display metadata and `null` where no names were registered. The names were resolved once, when the deploy script wrote the manifest, and the backend serves them verbatim from that row. **No route resolves ENS, and no client may treat a name as identifying a contract.** The address fields are authoritative; a client that renders a name renders the address with it ([ADR-030](decision_log.md), PRD FR-U10).

`deployed_at` is an ISO-8601 timestamp rendered from the manifest's integer `deployed_at_ts`, which is the chain time the deploy script observed rather than its own clock ([ADR-038](decision_log.md)). The manifest itself is served under `deployment_manifest` in the export document (section 5) and validates against `packages/protocol/schemas/deployment_manifest.v1.json`.

```json
{
  "deployments": [
    {
      "deployment_id": "local-2026-09-19-01",
      "chain_id": 31337,
      "protocol_version": "1",
      "exchange_address": "0x…",
      "base_token_address": "0x…",
      "quote_token_address": "0x…",
      "operator_address": "0x…",
      "relay_address": "0x…",
      "code_hashes": { "exchange": "0x…", "base_token": "0x…", "quote_token": "0x…" },
      "compiler": { "solc": "0.8.x", "optimizer": true, "runs": 200, "evm_version": "…" },
      "explorer_base_url": "https://sepolia.etherscan.io" | null,
      "ens": { "root": "agentnegotiation.eth", "exchange": "exchange.agentnegotiation.eth", "base_token": "masset.agentnegotiation.eth", "quote_token": "musd.agentnegotiation.eth", "resolved_at": "…" } | null,
      "deployed_at": "…"
    }
  ]
}
```

#### `GET /v1/scenarios`

Lists scenario templates from `scenarios/`, each validated against `scenario.v1.json` and upserted by `scenario_id` at start-up.

```json
{ "scenarios": [ { "scenario_id": "default-overlap", "name": "…", "description": "…", "feasibility_hint": null } ] }
```

`feasibility_hint` is always `null` here. Feasibility is computed only by the evaluator, never exposed on setup routes.

#### `GET /v1/scenarios/{scenario_id}`

Returns the full scenario including both mandates. Requires `X-Observer-Reveal: true`.

### 2.2 Runs

#### `POST /v1/runs`

Validates and saves the public scenario plus separately classified mandates. Does not touch the chain.

Request:

```json
{
  "name": "Default overlap, model vs model",
  "scenario_id": "default-overlap",
  "deployment_id": "local-2026-09-19-01",
  "public_config": {
    "base_amount_minor": "10000000",
    "max_offers": 8,
    "session_duration_s": 1800,
    "offer_lifetime_s": 600,
    "confirmation_threshold": 1,
    "first_proposer": "buyer"
  },
  "buyer": {
    "policy": "model" | "deterministic",
    "model_id": "claude-sonnet-5-5",
    "effort": "high",
    "initial_balances": { "base_minor": "0", "quote_minor": "250000000" },
    "allowance_minor": "250000000",
    "mandate": { "reservation_price_minor": "100000000", "min_remaining_inventory_minor": "0", "instructions": "…" }
  },
  "seller": {
    "policy": "model",
    "model_id": "claude-sonnet-5-5",
    "effort": "high",
    "initial_balances": { "base_minor": "25000000", "quote_minor": "0" },
    "allowance_minor": "25000000",
    "mandate": { "reservation_price_minor": "90000000", "min_remaining_inventory_minor": "10000000", "instructions": "…" }
  },
  "limits": {
    "model_call_ceiling": 20,
    "model_spend_ceiling_usd": "2.00",
    "model_timeout_s": 45,
    "repair_attempts": 1
  },
  "parent_run_id": null
}
```

Response `201`: the run resource (section 2.3) in state `draft`. Mandates are stored as `mandate_versions` and are absent from the response.

Both agents are provisioned before the response, inside the run's own unit of work (stage 2.4, [ADR-039](decision_log.md)): each derives the run's wallet and reports its address. If either does not answer, the response is `503 dependency_unavailable`; if either refuses — a policy it cannot run, say — `422 validation_error` with the agent's `agent_code`. Either way nothing of the run is kept, and an agent that did provision it is told to release it. The request body is parsed by `api.controller.RunRequest`; every amount through `MinorAmount`, so one with a trailing newline is refused, and no refusal repeats a value.

Validation errors (`422 validation_error`) include: unknown scenario or deployment, `max_offers` outside 1..32, non-integer amount strings, `model_id` missing for a model policy, `first_proposer` other than `buyer`, and a NUL character in any free-text field ([ADR-084](decision_log.md)).

#### `GET /v1/runs`

Query: `state`, `outcome`, `batch_id`, `limit`, `cursor`. Returns `{ "runs": [ …run summaries… ], "next_cursor": … }`, newest first. A run summary is the run's identity, state and outcome, and nothing derived from the chain:

```json
{ "run_id": "…", "name": "…", "parent_run_id": null, "batch_id": null, "scenario_id": "default-overlap", "deployment_id": "…", "created_at": "…", "state": "terminal", "state_cause": null, "mode": "live", "outcome": { … } }
```

#### `GET /v1/runs/{run_id}`

Public configuration, timeline summary, and status. Never includes mandates, private validation feedback, prompts, or credentials.

```json
{
  "run_id": "…",
  "name": "…",
  "parent_run_id": null,
  "created_at": "…",
  "state": "running",
  "state_cause": null,
  "mode": "live" | "fixture" | "replay",
  "outcome": { "kind": "pending" | "settled" | "closed" | "expired" | "aborted", "reason_code": null, "reason": null, "actor": null },
  "deployment": { "deployment_id": "…", "chain_id": 31337, "exchange_address": "0x…", "explorer_base_url": null },
  "session": {
    "session_id": "0x…", "config_hash": "0x…", "buyer_address": "0x…", "seller_address": "0x…",
    "base_amount_minor": "10000000", "expires_at_ts": 1758300000, "max_offers": 8,
    "status": "open", "sequence": 3, "offer_count": 3,
    "active_offer": { "offer_hash": "0x…", "proposer": "seller", "quote_amount_minor": "97000000", "valid_until_ts": 1758299400, "sequence": 3 } | null,
    "opened_tx_hash": "0x…"
  } | null,
  "parties": {
    "buyer": { "address": "0x…", "policy": "model", "model_id": "claude-sonnet-5-5", "effort": "high", "balances": { "base_minor": "0", "quote_minor": "250000000" }, "current_action": "deciding" | "idle" | "signing" | "awaiting_confirmation", "decision_status": null },
    "seller": { … }
  },
  "timeline": [
    {
      "sequence": 1, "kind": "offer" | "accept" | "close" | "expire" | "abort" | "settle" | "execution_failure",
      "actor": "buyer" | "seller" | "operator" | "anyone",
      "quote_amount_minor": "92000000", "valid_until_ts": 1758299300, "offer_hash": "0x…", "references_offer_hash": null,
      "reason_code": null, "reason": null,
      "tx": { "tx_hash": "0x…", "status": "confirmed", "block_number": 12, "block_hash": "0x…", "confirmations": 1, "explorer_url": null },
      "sentence": "Buyer offers 92 mUSD for 10 mASSET.",
      "recorded_at": "…"
    }
  ],
  "metrics": {
    "recorded_offers": 3, "decision_time_ms": 41234, "chain_wait_ms": 6100,
    "model_calls": 4, "model_cost_estimated_usd": "0.31", "model_cost_reported_usd": "0.28" | null,
    "gas_used": 412000, "fee_wei": "…",
    "rpc_requests": 184, "rpc_cost_estimated_usd": "0.0001" | null
  },
  "labels": { "test_assets": true, "simulated_economics": true, "fixture": false, "operator_abort_power": true }
}
```

Each party's `balances` are its latest canonical snapshot, and `"0"` before setup's: a fresh wallet holds nothing until start funds it. `current_action` comes from the party's open turn — `deciding` while it is observed, decided or repaired, `signing`, then `awaiting_confirmation` while its action is broadcast and confirmed — and is `idle` otherwise; `decision_status` is the party's latest turn's state. The `metrics` strip's `gas_used` and `fee_wei` include setup's; `GET /metrics` and the export keep them apart. `mode` is `live`, or `fixture` for a run on canned model responses.

`state` values: `draft`, `validated`, `preparing`, `running`, `paused`, `recovery_required`, `terminal`, `failed_setup`. `state_cause` is a short string. Since stage 2.4: `start` or `step` on `preparing`, the mode setup was started in; `step` while a requested step is under way and `step_complete` after it; `session_open` for a run whose setup was carried on and which waits to be resumed; `operator_pause`; `reorg`; `recovered`; `validation_failed`; when a run ends after its session was ended early, the termination's cause — `abort_requested`, `model_failure`, `budget_exhausted`, `execution_failure`, `session_deadline` or `session_refused` ([ADR-066](decision_log.md), [ADR-068](decision_log.md)) — whatever fault crossed it; on `failed_setup`, also `<kind>_reverted` for a setup transaction that reverted (`mint_reverted`, `fund_eth_reverted`, `approve_reverted`, `create_session_reverted`); and on `recovery_required`, the fault — `rpc_timeout`, `agent_unavailable`, `observation_inconsistent`, `nonce_conflict`, `unreachable`, `refused`, `settlement_check_failed`, `termination_reverted`, `internal_error`, `chain_unavailable` for a run whose chain went away — stranded at start-up by a backend serving another ([ADR-081](decision_log.md)) — an indexer problem's code, `agent_<code>` for an agent refusal, or `observation_<code>` for an observation the backend could not build ([ADR-063](decision_log.md), [ADR-064](decision_log.md)). [runbook.md](runbook.md) section 6 says what each asks of an operator.

#### `POST /v1/runs/{run_id}/validate`

Runs the setup validation in spec 3.1. Synchronous, may take a few seconds. Response `200`:

```json
{
  "ok": false,
  "checks": [
    { "check": "deployment", "ok": true, "detail": "local-2026-09-19-01" },
    { "check": "confirmation_threshold", "ok": true, "detail": "1" },
    { "check": "rpc", "ok": true, "detail": "latest block 41" },
    { "check": "chain_id", "ok": true, "detail": "31337" },
    { "check": "exchange_code_hash", "ok": true },
    { "check": "base_token_code_hash", "ok": true },
    { "check": "quote_token_code_hash", "ok": true },
    { "check": "relay_eth_balance", "ok": true, "detail": "10000000000000000000000 wei" },
    { "check": "operator_eth_balance", "ok": true, "detail": "9999990000000000000000 wei" },
    { "check": "buyer_agent", "ok": false, "detail": "its signer did not load" },
    { "check": "buyer_agent_model_available", "ok": true, "detail": "live" },
    { "check": "buyer_wallet", "ok": true, "detail": "0x…" },
    { "check": "seller_agent", "ok": true, "detail": "agent-b" },
    { "check": "seller_wallet", "ok": true, "detail": "0x…" }
  ]
}
```

Every check is reported, passing or not (stage 2.4, `api.validation`). The chain ID must be the manifest's and one of 31337 and 11155111; each contract's runtime bytecode must hash to the manifest's `code_hashes`; a `confirmation_threshold` that is not an integer of at least 1 is refused ([ADR-059](decision_log.md)); each agent must answer as its role with its signer loaded and the run's policy among its `policy_kinds`, and with `model_ok` for a model policy, whose check's `detail` is the agent's `model_mode`. A run with a party on the model policy whose agent reports `model_mode: "fixture"` is recorded with `mode: "fixture"` and labelled so on every surface; otherwise it is `live` ([ADR-088](decision_log.md)). The participant wallets are fresh and hold nothing until start funds them, which is not a failure. Never reports feasibility. Never includes a mandate value. On success the run moves to `validated`; a `validated` run whose validation no longer passes goes back to `draft`, cause `validation_failed`.

#### `POST /v1/runs/{run_id}/start`

Prepares the chain session if needed and schedules automatic execution. `202` with operation `start_run`. Allowed from `validated` or `paused`.

#### `POST /v1/runs/{run_id}/step`

Prepares the session if needed, then schedules exactly one turn when idle and leaves the run `paused`. `202` with operation `step_run`. `409 turn_in_progress` when a turn is already running.

#### `POST /v1/runs/{run_id}/pause`

Stops requesting new decisions. `200` with the run resource. A pending broadcast or confirmation completes; the response `state_cause` says `operator_pause` and the UI must show that expiry continues.

#### `POST /v1/runs/{run_id}/resume`

Reconciles canonical chain state, then continues automatic execution. `202` with operation `resume_run`. From `recovery_required` this performs the recovery procedure and lands in `paused` if successful.

#### `POST /v1/runs/{run_id}/abort`

Request:

```json
{ "reason": "operator_request" }
```

Stops decisions and requests on-chain abort if the session is open and before its deadline. If the deadline has elapsed, submits `expireSession` instead and the outcome is `expired`. `202` with operation `abort_run`. Allowed from `preparing`, `running`, `paused` and `recovery_required`, so it is always the way out of a run that cannot go on (stage 2.4, [ADR-067](decision_log.md)). The termination is recorded on the run before anything is sent ([ADR-068](decision_log.md)): a second abort sends nothing more, and the first termination recorded — a model failure's, say — is the one kept. A run with no session — none recorded, or its `createSession` reverted — is `failed_setup` at once, cause `abort_requested`, and the active run is freed; one whose session is still being created is aborted once it opens. Otherwise the run keeps its state until the termination is canonical at the threshold, and ends `terminal`, or `failed_setup` when setup had not finished. Abort cannot reverse a settlement that lands first; the operation result reports which won. `reason` is one of the four abort codes' names (protocol section 10); any other is `422`.

While a termination is recorded and the run has not ended, `start`, `step` and `resume` are `409 invalid_state` with `details.termination` naming it, and a decision an agent returns meanwhile is kept only as a private decision record — nothing of it is signed into the run or sent ([ADR-070](decision_log.md)).

#### `POST /v1/runs/{run_id}/clone`

Creates a fresh run in `draft` with specified changes. The body is a JSON merge patch over the original `POST /v1/runs` body; omitted fields copy from the parent, including mandates (which are re-versioned). `201` with the new run resource. The parent is untouched.

```json
{ "name": "Infeasible clone", "seller": { "mandate": { "reservation_price_minor": "105000000" } } }
```

The merge patch is RFC 7386: an object merges member by member and `null` removes a member, so `{"limits": null}` is a request without limits, refused `422`. The result is parsed exactly as a `POST /v1/runs` body is. `parent_run_id` is set to the parent and a patch naming it is `422`. Each mandate's `version` is one past the parent's, and both agents provision the clone afresh, so its wallets are new (ADR-039). Since stage 2.5.

#### `GET /v1/runs/{run_id}/mandates`

Observer-only private view. Requires `X-Observer-Reveal: true`.

```json
{
  "run_id": "…",
  "buyer": { "mandate_version_id": "…", "version": 1, "reservation_price_minor": "100000000", "min_remaining_inventory_minor": "0", "instructions": "…" },
  "seller": { … },
  "evaluator": { "feasible": true, "feasible_interval_minor": ["90000000", "100000000"], "feasible_surplus_minor": "10000000" }
}
```

This route is not available to agent services under any circumstances. Access is logged.

#### `GET /v1/runs/{run_id}/decisions`

Private operational records. Requires `X-Observer-Reveal: true`.

```json
{
  "decisions": [
    {
      "decision_id": "…", "turn": 2, "party": "seller", "attempt": 1,
      "policy": "model", "model_id": "claude-sonnet-5-5", "prompt_template_version": "v1.0.0+bb64137d436ca688", "effort": "high",
      "observation_hash": "0x…",
      "raw_response": { "decision": { "action": "offer", "quote_amount_minor": "85000000" }, "explanation": "…" },
      "validation": { "ok": false, "code": "below_reservation", "feedback": "Your quote is below your reservation price." },
      "stop_reason": "end_turn",
      "usage": { "input_tokens": 680, "output_tokens": 180, "cache_read_input_tokens": 2150, "cache_creation_input_tokens": 0 },
      "cost_estimated_usd": "0.171320", "cost_reported_usd": "0.003590",
      "latency_ms": 3120, "requested_at": "…",
      "status": "invalid", "request_hash": null,
      "authorized": false,
      "raw_response_escaped": false,
      "label": "private_operational_record_not_an_authorized_offer"
    }
  ]
}
```

`raw_response_escaped` is true when the response or its feedback held a NUL character, which PostgreSQL cannot store: each one is stored as the six characters `\u0000` ([ADR-090](decision_log.md)). An attempt whose model call was sent but ended without an answer — a timeout, a provider fault — has `validation` `{ "ok": false, "code": null, "feedback": null }` and a `raw_response` of `{ "error": { "outcome", "status", "type", "message", "request_id" } }`, the provider's message private like the model's text; a call that was never sent has no record ([ADR-089](decision_log.md)).

#### `GET /v1/runs/{run_id}/events`

Server-sent events. Supports `Last-Event-ID` for replay from a cursor. `Content-Type: text/event-stream`. See section 3.

#### `GET /v1/runs/{run_id}/replay`

Returns the complete public run resource plus the ordered list of timeline frames the UI steps through. `mode` is `replay`. Zero model calls, zero transactions.

**Not served yet**: the frame shape is designed with the replay interface that steps through it, in stage 4 ([ADR-074](decision_log.md)). The export below carries everything a replay is built from.

#### `GET /v1/runs/{run_id}/export`

Query: `include_private=true` requires `X-Observer-Reveal: true`. Returns the evidence export document (section 5) as `application/json` with `Content-Disposition: attachment; filename="run-<run id>.export.json"`. The run's metrics are recomputed first.

#### `GET /v1/runs/{run_id}/metrics`

Per-run metrics per spec 11.2, plus the RPC request count by method and estimated RPC cost of [ADR-061](decision_log.md), recomputed when asked. Reservation utilities require private inputs and are included only with `X-Observer-Reveal: true`; otherwise those fields are `null`. `rpc_cost_estimated_usd` is `null` — displayed as unknown, never zero — when the RPC price table has no entry for the deployment's provider.

```json
{
  "run_id": "…",
  "recorded_offers": 5, "decision_time_ms": 42, "chain_wait_ms": 6100, "setup_chain_wait_ms": 4100,
  "model_calls": 0, "input_tokens": 0, "output_tokens": 0,
  "model_cost_estimated_usd": "0.000000", "model_cost_reported_usd": "0.000000",
  "gas_used_negotiation": "412000", "gas_used_setup": "380000", "fee_wei_negotiation": "…", "fee_wei_setup": "…",
  "settled_quote_minor": "93333333", "mandate_violations": 0, "failure_class": "none", "audit_complete": true,
  "rpc_requests": 184, "rpc_requests_by_method": { "eth_getLogs": 40, "…": 0 }, "rpc_cost_estimated_usd": "0.000000",
  "rpc_price_last_verified": "2026-10-02",
  "buyer_utility_minor": "6666667" | null, "seller_utility_minor": "3333333" | null, "captured_surplus_minor": "10000000" | null,
  "feasible": true | null, "feasible_surplus_minor": "10000000" | null, "efficiency_ratio": "1.000000" | null
}
```

The definitions are [data_model.md](data_model.md) section 3.13's. `rpc_price_last_verified` is the price table entry's date, shown beside the estimate, and null when the table does not price the provider. `efficiency_ratio` is the captured surplus over the feasible surplus, and null — undefined — when the feasible surplus is zero (spec 11.2). A model cost is `"0.000000"` for a run that called no model, and null when a call's price is unknown.

### 2.3 Batches (local profile only)

#### `POST /v1/batches`

```json
{
  "name": "baseline-comparison-1",
  "population": { "overlap_count": 30, "non_overlap_count": 30, "seed": 42 },
  "pairings": ["det_det", "model_det", "det_model", "model_model"],
  "repetitions": 3,
  "template_run": { …same shape as POST /v1/runs minus mandates… }
}
```

`202` with operation `run_batch`. Runs execute sequentially because only one run may be active. `409 batch_not_local` on a Sepolia deployment.

#### `GET /v1/batches/{batch_id}`

Progress and the list of `run_id`s with outcomes.

#### `GET /v1/batches/{batch_id}/report`

Aggregated metrics with distributions and bootstrap confidence intervals. Requires `X-Observer-Reveal: true` because it uses private inputs.

### 2.4 Operations

#### `GET /v1/operations/{operation_id}`

Returns the operation record. Clients poll or, preferably, watch the run's SSE stream for `operation` events.

---

## 3. Server-sent events

Each event: `id: <cursor>` (monotonic integer per run), `event: <type>`, `data: <json>`. A `: keepalive` comment every 15 s without an event. Reconnect with `Last-Event-ID` replays every event after that cursor from `run_events`; a `Last-Event-ID` that is not an integer from 0 to 2^63 − 1 is `400 bad_request`, and an unknown run `404`. When the backend begins to shut down every stream ends, and the client reconnects to whichever process serves next ([ADR-082](decision_log.md)). Only the event types in this table are ever sent: a `run_events` row of any other type is skipped (stage 2.5).

| Event type | Data | When |
|---|---|---|
| `run.state` | `{ state, state_cause, outcome }` | any state or outcome change |
| `turn.started` | `{ turn, party, expected_sequence }` | observation sent |
| `turn.decision` | `{ turn, party, attempt, status: "valid" \| "invalid" \| "model_failed", action, sentence }` | after validation; no private feedback, no raw response. `action` and `sentence` are the signed attempt's — its decision, and the sentence its event will have — and null for a refused one, which was never an offer (FR-U8) |
| `turn.signed` | `{ turn, party, kind, digest }` | signed action persisted |
| `tx.status` | `{ digest, tx_hash, status, block_number, confirmations, explorer_url }` | each status transition |
| `chain.event` | timeline entry (section 2.2) | canonical event indexed, or an execution failure recorded |
| `chain.reorg` | `{ from_block, to_block, invalidated_digests: [] }` | reorg detected; appended by the indexer in the same transaction as the rewind ([ADR-058](decision_log.md)) |
| `balances` | `{ stage, buyer: { base_minor, quote_minor, eth_wei }, seller: {…}, block_number }` | snapshot taken: `pre_setup` and `post_setup` during setup, the terminal stages when the outcome is recorded |
| `metrics` | the run resource's metric strip (section 2.2) | after each turn, and when the run ends |
| `operation` | operation record | operation status change (section 1.2) |
| `notice` | `{ level, message }` | operator-facing notices such as "Paused; offer and session expiry continue." |

Never emitted on SSE: mandates, raw model responses, validation feedback, prompts, keys.

---

## 4. Timeline sentence rules

Rendering is done server-side once and stored on the timeline entry so UI, replay, and export agree.

| Kind | Sentence |
|---|---|
| offer, sequence 1 | `Buyer offers 92 mUSD for 10 mASSET.` |
| offer, later | `Seller counters at 97 mUSD for 10 mASSET.` |
| accept | `Buyer accepts 95 mUSD for 10 mASSET.` |
| settle | `Settled: 10 mASSET to buyer, 95 mUSD to seller.` |
| close | `Seller walks away (no further concession).` |
| expire | `Session expired at 12:34:56 UTC.` |
| abort | `Operator aborted the session (model failure).` |
| execution failure | `Transaction reverted: SequenceMismatch. No trade occurred.` |

Amounts are rendered from minor units with 6 decimals, trailing zeros trimmed. The expiry time is the session's `expiresAt` in UTC; a close or abort names its reason code's string with underscores as spaces.

Since stage 2.3 the renderer is `api.projection.TimelineSentences`, and the indexer applies it as it records each event, storing the sentence on the `chain_events` row ([ADR-024](decision_log.md)). `SessionOpened` has no sentence and is not a timeline entry. An **execution failure** — a transaction whose receipt has status 0, which emits no event — is a timeline entry of its own, of kind `execution_failure`, whose sentence is rendered when the indexer records the receipt and stored on the transaction's `tx_outbox` row, with the decoded protocol error as its `reason` ([ADR-051](decision_log.md)). Its actor is the party that signed the action it carried, or `operator` or `anyone` for a lifecycle transaction, and it follows every event of its block.

A timeline entry's `tx.status` is the status of the backend's own outbox row for that transaction. For an event from a transaction the backend did not send — an expiry someone else called — it is `confirmed` at the run's threshold and `included` below it, and never `finalized`, which needs the outbox's finalized-head check.

---

## 5. Evidence export document

`packages/protocol/schemas/export.v1.json`, with `additionalProperties: false` at every level. That
is what makes the privacy claim testable rather than asserted: the default export must validate
against the schema with `private: null`, so a mandate, a raw model response, a validation feedback
string or a utility figure appearing anywhere in it is a schema failure rather than something a
reviewer has to notice ([test_strategy.md](test_strategy.md) section 7).

Several slots that look permissive are not. `reproducibility`'s four per-party maps are closed to
`buyer` and `seller` with scalar values, and `signed_actions[].typed_message`,
`chain_events[].decoded` and `calldata[].decoded_args` restrict their **property names** to the
fields the protocol defines — the struct fields of section 4, the event fields of section 9, and the
ABI parameter names of section 8. Their value shapes vary, so the names are what can be pinned, and
every private field this project has is named something not in those lists. As bare objects they
were each a slot in which a mandate could have ridden out of the server inside this document.

Top-level:

```json
{
  "export_version": "1",
  "exported_at": "…",
  "includes_private": false,
  "disclaimer": "Test assets and simulated economics. Public testnets may be reset; this export is the archive of record.",
  "run": { …public run resource… },
  "deployment_manifest": { … },
  "reproducibility": {
    "scenario_id": "…", "policy_versions": {…}, "prompt_template_versions": {…}, "model_ids": {…}, "effort": {…},
    "seed": null, "seed_supported": false, "protocol_version": "1", "software_version": "0.1.0+gitsha"
  },
  "signed_actions": [ { "sequence", "kind", "typed_message", "digest", "signer", "signature", "tx_hash" } ],
  "chain_events": [ { "event", "block_number", "block_hash", "tx_hash", "log_index", "canonical", "decoded" } ],
  "calldata": [ { "tx_hash", "to", "input", "decoded_function", "decoded_args" } ],
  "balance_snapshots": [ … ],
  "decisions_public": [ { "turn", "party", "attempt", "status", "action", "usage", "latency_ms", "cost_estimated_usd", "cost_reported_usd", "label" } ],
  "metrics": { … },
  "private": null
}
```

With `include_private=true`, `private` contains `mandates` (both, as `mandate.v1.json` shapes them), `decisions` — every attempt in full, raw responses and validation feedback included, as `GET /decisions` gives them — `observations`, each as its own agent was sent it, and the evaluator's feasibility result. Credentials and private keys are never exported under any option.

`metrics` carries `rpc_requests`, `rpc_requests_by_method` and `rpc_cost_estimated_usd` ([ADR-061](decision_log.md)); the method map's keys are pinned to JSON-RPC namespaces and its values to counts. `failure_class` is null while the run goes on. `signed_actions[].tx_hash` is the transaction that carried the action — the mined one, else the one still live — and `chain_events` and `balance_snapshots` include rows a reorg invalidated, with `canonical: false`.

---

## 6. Agent internal API

Served by each agent service on its own port. Only the backend may call it. Implemented in stage 2.2 (`services/agent/`); run an instance with `python -m agent` ([runbook.md](runbook.md) section 3).

**Authentication ([ADR-041](decision_log.md)).** Every request carries `X-Agent-Auth`: the lowercase-hex HMAC-SHA256, under the instance's shared secret, of the request's method, a newline, its URL path, a newline and its raw body. The layout is `negotiation_protocol.agent_auth`, which both sides import. For every request that reaches a route, the agent authenticates before it parses the run id or the body, and before it looks at `X-Request-Id`; a missing or wrong MAC is `401 unauthorized`. A path or method that matches no route is answered `404 not_found` or `405 method_not_allowed` without authentication, because there is nothing to authenticate for: such an answer says only that the route does not exist, which this section publishes anyway. Paths are never redirected, so a trailing slash is a 404, not a hop past the MAC. A MAC is therefore valid for one method, one route, one run and one body — a health check's MAC does not authorise a `release`, although both bodies are empty. Every request also carries `X-Request-Id`, which is required after authentication (`400 bad_request` without it) and echoed on the response.

**Errors** use the envelope of section 1.1, with `request_id` set from `X-Request-Id`. `details` never holds a mandate value, a key or a secret: schema failures name the failing location and the schema keyword it broke (`{"session": "fails additionalProperties"}`), and a value a value object refuses is reported as `is not a valid value of this field's type`: no error text quotes the offending value, because error responses reach the backend's logs. An unexpected failure is `500 internal_error` with the request id alone. The interactive docs and OpenAPI routes are disabled: they would be unauthenticated.

**Run states on an instance.** `unprovisioned` → `provisioned` → `approved`, and `released` after `release`. A call the state does not allow is `409 invalid_state` with `details.state` and `details.allowed_from`. State is in memory: a restarted instance knows no runs and answers `409 invalid_state` with `state = "unprovisioned"`. The controller then re-provisions the run with its stored address as `expected_address`, re-sends approve-session built from the canonical `SessionOpened` row and that block's timestamp read from the chain, and retries the call ([ADR-048](decision_log.md), [architecture.md](architecture.md) section 11).

**Idempotency.** An identical request returns the first response. The same operation with different content is refused rather than answered twice: `409 idempotency_conflict` for provisioning and turns, `409 session_mismatch` for approve-session.

#### `GET /internal/health`

```json
{ "status": "ok", "role": "buyer", "instance": "agent-a", "policy_kinds": ["deterministic", "model"], "model_ok": true, "model_mode": "live" | "fixture" | null, "signer_ok": true }
```

`policy_kinds` lists what this instance can run: `deterministic` always, and `model` when the instance is configured for a model — a key reference (`AGENT_MODEL_KEY_REF`) or a fixture directory (`AGENT_MODEL_FIXTURES`), never both. `model_mode` says which: `live`, `fixture` — canned responses, by the instance's own configuration and never by a run request ([ADR-088](decision_log.md)) — or null with neither. `model_ok` is `true` when that configuration loaded: the key, the model price table, the fixture script and the prompt template. An instance whose model did not load still lists `model`, so a model run's validation fails with a reason, and its log says why. `signer_ok` is `false` when the instance's root could not be resolved at start-up — an unset variable, an unreadable keystore, a wrong password — and the reason is in its log; the instance still answers, so "the agent is down" and "the agent's key is misconfigured" stay distinguishable.

#### `POST /internal/runs/{run_id}/provision`

Idempotent. Delivers the mandate and configuration this instance needs for one run.

```json
{
  "role": "buyer",
  "policy": "model",
  "model_id": "claude-sonnet-5-5",
  "effort": "high",
  "limits": { "model_call_ceiling": 20, "model_spend_ceiling_usd": "2.00", "model_timeout_s": 45, "repair_attempts": 1 },
  "expected_session": { "chain_id": 31337, "exchange_address": "0x…", "base_token": "0x…", "quote_token": "0x…", "base_amount_minor": "10000000", "max_offers": 8, "session_duration_s": 1800, "offer_lifetime_s": 600 },
  "key_ref": "env:BUYER_ROOT_KEY" | "keystore:/run/secrets/buyer-root.json",
  "expected_address": "0x…" | null,
  "mandate_version_id": "…",
  "mandate": { "reservation_price_minor": "100000000", "min_remaining_inventory_minor": "0", "instructions": "…" },
  "initial_balances": { "base_minor": "0", "quote_minor": "250000000" },
  "allowance_minor": "250000000"
}
```

Every field is required and parsed strictly: an unknown field, a number where an amount string belongs, or a float where an integer belongs is `422 validation_error`. `model_id` and `effort` are `null` for a deterministic run and required for a model run. A model run gets its own client and its own budget over `limits.model_call_ceiling` and `limits.model_spend_ceiling_usd`, its calls bounded by `limits.model_timeout_s`, and its system prompt rendered from its role and the mandate's `instructions`. `limits.repair_attempts` is the number of repairs after a refused decision, for either policy. The mandate is validated against `mandate.v1.json` and then as value objects, so an amount the schema's pattern admits but a value object refuses — `"100000000\n"` — is refused too. `base_token` equal to `quote_token` is refused ([ADR-037](decision_log.md)), as is any chain integer beyond `uint64`. `key_ref` must be a key reference in the grammar of [ADR-049](decision_log.md) — `env:NAME` with an upper-case variable name, or `keystore:/path.json` — with no run of 32 or more hexadecimal characters anywhere in it, so that a key pasted where its reference belongs is refused and never repeated.

Response `200`: `{ "provisioned": true, "my_address": "0x…", "key_derivation": { "scheme": "agent-negotiation-sandbox/participant-key/v1", "chain_id": 31337, "role": "buyer", "run_id": "…" }, "policy_version": "det-1.0.0" | "model-1.0.0", "prompt_template_version": "v1.0.0+bb64137d436ca688" | null }`. A model run's `prompt_template_version` is the template's directory and a hash of its text ([ADR-029](decision_log.md) as built); a deterministic run's is null.

The run's participant key is **derived**, not supplied ([ADR-039](decision_log.md)). `key_ref` names the instance's root secret; the agent refuses it with `409 key_ref_mismatch` unless it is the root this instance is configured with, so a request routed to the wrong instance cannot make it sign as the other party. The agent derives the key from the root, the chain ID, its role and the run ID, and returns the address as `my_address`; the backend stores that address and `key_derivation` in `wallets` and never sees a key. `expected_address` is null on first provisioning. On a re-provisioning — after an agent restart, for instance — the backend sends the address it stored, and the agent refuses with `409 address_mismatch` if its derivation does not reproduce it; a refused provisioning is not kept.

Refusals, in the order they are checked: a malformed body, `400 bad_request` or `422 validation_error`; `409 invalid_state` for a released run; for a run already provisioned, `409 idempotency_conflict` if the body differs (with `expected_address` left out of the comparison, so a first provisioning and a re-provisioning are the same) or `409 address_mismatch`; `503 dependency_unavailable` with `details.dependency = "signer"` and the reason, when the root did not load; `409 key_ref_mismatch`; `422 validation_error` for a `role` that is not this instance's or a `policy` not in `policy_kinds`; for a model run, `422 validation_error` naming `model_id` or `effort` when either is null, and `503 dependency_unavailable` with `details.dependency = "model"` and the reason when the instance's model did not load; `409 address_mismatch`.

#### `POST /internal/runs/{run_id}/approve-session`

The backend sends the decoded `SessionOpened` fields and the timestamp of the block that emitted the event ([ADR-044](decision_log.md)):

```json
{ "session_id": "0x…", "buyer": "0x…", "seller": "0x…", "base_token": "0x…", "quote_token": "0x…", "base_amount_minor": "10000000", "expires_at_ts": 1760001740, "max_offers": 8, "config_hash": "0x…", "opened_at_ts": 1759999940 }
```

The service recomputes `configHash` from the event's session fields and its own *provisioned* token addresses, and checks: the tokens, `base_amount_minor` and `max_offers` against the provisioned expectation; its own derived address in its own role's slot; a counterparty that is neither itself nor the zero address; and the expiry window `opened_at_ts < expires_at_ts <= opened_at_ts + session_duration_s` — shorter than provisioned by the inclusion delay, never longer, never already expired.

Response `200`: `{ "approved": true, "config_hash": "0x…" }`. Otherwise `409 session_mismatch` with every differing field at once in `details.fields`, each as `{ "expected": …, "received": … }`. Approving the same session again returns the same response; a different session for a run that already approved one is `409 session_mismatch` naming the fields that differ from it. Allowed from `provisioned` and `approved`.

#### `POST /internal/runs/{run_id}/setup-approval`

The participant's ERC-20 `approve` of the exchange, needed once during setup ([ADR-040](decision_log.md)). The agent builds the whole transaction from provisioned state — the token is its role's (quote for the buyer, base for the seller), the spender is `expected_session.exchange_address`, the amount is `allowance_minor`, the chain is `expected_session.chain_id` — and signs it with the run's derived key as an EIP-1559 transaction. The caller supplies only what it must know about the chain:

```json
{ "nonce": 0, "gas_limit": 70000, "max_fee_per_gas_wei": "2000000000", "max_priority_fee_per_gas_wei": "1000000000" }
```

Response `200`:

```json
{ "raw_tx": "0x…", "tx_hash": "0x…", "from": "0x…", "token": "0x…", "spender": "0x…", "amount_minor": "250000000", "nonce": 0 }
```

`422 validation_error` for a gas limit above the agent's bound — `AGENT_SETUP_GAS_LIMIT_MAX`, 100,000 by default ([ADR-042](decision_log.md)) — or whose worst-case cost, `gas_limit` times `max_fee_per_gas_wei`, is above `AGENT_SETUP_MAX_COST_WEI`, 10^16 wei (0.01 ETH) by default ([ADR-047](decision_log.md)), or with a priority fee above the fee cap or a nonce beyond `2^64 - 2`; the fees are decimal strings like every other amount. `409 invalid_state` before provisioning or after release. Deterministic: the same request returns the same transaction.

#### `POST /internal/runs/{run_id}/turn`

Body: the observation from [protocol.md](protocol.md) section 12 without the `mandate` field (the service injects its own), plus `turn` (an integer of at least 1) and `deadline_at` (ISO 8601 UTC ending in `Z`, by which the service must answer). A body that carries a `mandate` is `422`: the backend sends a mandate to an agent once, at provisioning.

Before any policy is asked, the service validates the observation — with its own mandate injected — against `observation.v1.json` and then as value objects (`422 validation_error`), and checks it against what this instance approved: `run_id`, `role` and `my_address` must be its own and every `session` field must equal the approved session's (`409 session_mismatch`, with session fields named `session.<field>`), and `chain_time` must be before the session's `expiresAt` (`409 invalid_state`, `details.state = "session_deadline_passed"`). Then it checks the observation against itself ([ADR-046](decision_log.md), [protocol.md](protocol.md) section 12): history in ascending sequence order from 1, alternating from the buyer, each offer with all its fields and its digest recomputed from them under the approved session, the statuses, `expected_sequence`, `offers_remaining_for_me`, and `active_offer` as the last offer while it stands and null once it has expired. A contradiction is `422 observation_inconsistent` with every one named in `details.fields`, and nothing is signed; the controller rebuilds the observation from the chain and retries, up to five times, before moving the run to `RECOVERY_REQUIRED`. Allowed from `approved` only. The same `turn` with the same observation returns the first response; the same `turn` with a different observation is `409 idempotency_conflict`. `deadline_at` is the time by which the service answers ([ADR-089](decision_log.md)): each attempt is given the time left less one second, for the answer to travel, and a model call — its token count included — still unanswered then is a `timeout`; an attempt with no time left is not made. The deterministic policy answers at once.

Response `200`:

```json
{
  "turn": 3,
  "status": "signed" | "model_failed" | "budget_exhausted",
  "signed_action": {
    "kind": "offer" | "accept" | "close",
    "typed_message": { …exact struct fields… },
    "digest": "0x…",
    "signature": "0x…",
    "signer": "0x…"
  } | null,
  "decisions": [
    { "attempt": 1, "raw_response": {…}, "validation": { "ok": false, "code": "…", "feedback": "…" }, "stop_reason": "…", "usage": {…}, "latency_ms": 0, "cost_estimated_usd": "…", "cost_reported_usd": null, "prompt_template_version": "…", "observation_hash": "0x…", "requested_at": "…" }
  ],
  "failure": { "code": "repair_exhausted" | "timeout" | "refusal" | "provider_error" | "budget_exhausted", "detail": "…" } | null
}
```

`typed_message` carries the Solidity field names, because `export.v1.json` restricts its keys to them: `{ "sessionId", "configHash", "sequence", "proposer", "quoteAmount", "validUntil" }` for an offer, `{ …, "actor", "offerHash" }` for an accept, `{ …, "actor", "reason" }` for a close. Amounts are decimal strings; `sequence`, `validUntil` and `reason` are integers. Every field comes from the approved session, the validated decision, the observation's `expected_sequence` and `chain_time`, or the run's own address — never from the policy's output ([protocol.md](protocol.md) section 11).

`decisions` has one record per attempt: the first, then at most `limits.repair_attempts` repairs, each given only this agent's own feedback from the attempt before. `validation.code` is one of the codes in [protocol.md](protocol.md) section 11.1. `observation_hash` is `json_sha256` of the observation the decision was made from, mandate included, so it equals the backend's `turns.observation_hash` exactly when the two agree on the mandate. When every attempt is refused, `status` is `model_failed`, `failure.code` is `repair_exhausted` — or `refusal` when the last attempt was the provider's safeguards declining to answer — and `failure.detail` names only the number of attempts: the codes and the feedback are private and live in the decision records.

Every answer the model gives is judged by the validator, whatever its stop reason: text that is not an envelope is a refused attempt like any other, and repaired, and so is a `refusal` or `max_tokens` answer whose text is not a valid decision; one whose text is a valid, in-mandate decision is signed like any other ([ADR-089](decision_log.md)). Text that could not travel as JSON — nested deeper than 32 levels, or holding `NaN`, `Infinity`, a number too large for a double or a lone UTF-16 surrogate — is kept as text, which the validator refuses; a lone surrogate in text, a stop reason or a provider's error is written as its escape, `\ud800`. A model call that ends without an answer ends the turn at once, with no repair ([ADR-089](decision_log.md)):

| The call | `status` | `failure.code` | Recorded |
|---|---|---|---|
| timed out, or reached `deadline_at` | `model_failed` | `timeout` | when sent |
| refused with 429, another 4xx or a 3xx, failed with 5xx or 529, lost its connection, or answered with a body that is not a message | `model_failed` | `provider_error` | when sent |
| was refused by the run's budget: the call ceiling, the spend ceiling, or an unpriced model | `budget_exhausted` | `budget_exhausted` | never |

"When sent" means the budget admitted the call, so it went, or may have gone, to the provider and is charged its estimate; its record has `validation` `{ "ok": false, "code": null, "feedback": null }` and a private `raw_response` of `{ "error": { "outcome", "status", "type", "message", "request_id" } }`. A call that never left — refused by the budget, or whose token count failed — has no record, so the run's model calls are the calls actually made. `failure.detail` names the kind of fault, the HTTP status and the provider's error type, or the budget's counts and amounts, and never the provider's message.

The backend persists `decisions` verbatim as private records and `signed_action` as a `signed_actions` row. The service never calls this endpoint's result back; the backend supplies the confirmed outcome in the next observation's `history`.

#### `POST /internal/runs/{run_id}/release`

Called after the run reaches a terminal state. The body is empty or `{}`. The service discards the mandate and the signer for that run, and refuses every later call for it with `409 invalid_state`, `details.state = "released"`, for the rest of the process's life. A turn already deciding when the release arrives signs nothing: it checks again before signing and answers the same `409` ([ADR-048](decision_log.md)). Idempotent, including for a run the instance never knew. Response `200`: `{ "released": true }`.

---

## 7. Error codes

| HTTP | `code` | Meaning |
|---|---|---|
| 400 | `bad_request` | Malformed JSON or headers: an `Idempotency-Key` that is not a UUID, a `Last-Event-ID` that is not a cursor, a list `cursor` this API did not issue |
| 401 | `unauthorized` | Missing or invalid operator token or agent HMAC |
| 403 | `reveal_required` | Private route without `X-Observer-Reveal: true` |
| 404 | `not_found` | Unknown run, batch, operation, scenario, deployment |
| 405 | `method_not_allowed` | Agent internal API: a method the route does not serve |
| 409 | `idempotency_conflict` | Same key, different body |
| 409 | `invalid_state` | Action not allowed in current run state; `details.state` and `details.allowed_from`; `details.termination` while a termination is recorded; `details.deployment` for a run on a deployment this backend does not serve |
| 409 | `turn_in_progress` | Step or start while a turn is running |
| 409 | `another_run_active` | A different run holds the lease |
| 409 | `mandate_immutable` | Attempt to change a mandate after start |
| 409 | `batch_not_local` | Batch requested on a non-local deployment |
| 409 | `session_mismatch` | Agent service refused the on-chain session, or a turn whose observation is of another run, party or session |
| 409 | `key_ref_mismatch` | Agent service was asked to provision with a root key reference that is not its own ([ADR-039](decision_log.md)) |
| 409 | `address_mismatch` | Agent service's derivation did not reproduce the address stored for the run ([ADR-039](decision_log.md)) |
| 422 | `validation_error` | Field-level errors in `details.fields` |
| 422 | `observation_inconsistent` | Agent service: the observation contradicts itself or the approved session; the controller rebuilds it and retries ([ADR-046](decision_log.md)) |
| 422 | `deployment_mismatch` | Chain ID or code hash differs from manifest |
| 503 | `dependency_unavailable` | RPC, database, agent service, or model provider down; an agent service whose signer did not load, with `details.dependency = "signer"` |
| 500 | `internal_error` | Unhandled; `request_id` for correlation, also in the `X-Request-Id` header. Its log line names the exception's type and frames, never its message |

Contract reverts are surfaced as `tx.status = reverted` with `revert_error` set to the decoded custom error name from [protocol.md](protocol.md) section 8.3, never as an HTTP error.

---

## 8. Contract change control

- Any change to this document requires a matching change to the OpenAPI snapshot test in `services/api/tests/contract/` and the generated TypeScript client. The test compares the generated document with `openapi.snapshot.json` and the served routes with the headings of section 2, less the routes it defers (the batches, stage 6; replay, stage 4); `make openapi` rewrites the snapshot after an intended change. The TypeScript client is generated from it in stage 4.
- Field additions are allowed in a minor version. Renames, removals, type changes, and enum removals require a new `/v2` prefix or an explicit decision-log entry approving a breaking change before release.
- The agent internal API is versioned with the backend and is not a public contract; both sides deploy together.
