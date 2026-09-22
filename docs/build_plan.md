# Build Plan

| | |
|---|---|
| **Version** | 0.1.0 |
| **Date** | 19 September 2026 |
| **Source** | Spec section 13 |
| **Related** | [prd.md](prd.md), [test_strategy.md](test_strategy.md), [architecture.md](architecture.md), [contributing.md](contributing.md) |

Stages follow the spec's build sequence with an added stage 0 for repository scaffolding. Each stage has an exit condition that is a demonstrable artifact, not a task list being finished. Stages are sequential because each depends on the previous one's contracts being stable.

```mermaid
flowchart LR
    S0[0 Scaffold] --> S1[1 Protocol and contracts]
    S1 --> S2[2 Deterministic end-to-end]
    S2 --> S3[3 Model decisions]
    S3 --> S4[4 React demonstration]
    S4 --> S5[5 Sepolia run]
    S5 --> S6[6 Evaluation]
    S6 --> R[Release v0.1]
```

---

## Stage 0: Scaffold

**Deliverables**
- Git repository initialized (done, 21 September 2026); directory layout per spec 10.2; `.gitignore`, `.editorconfig`, `.env.example`.
- `LICENSE` (Apache-2.0) and `NOTICE` at the repository root (done, 21 September 2026); `SPDX-License-Identifier: Apache-2.0` as the first line of every `.sol` file from stage 1 ([ADR-032](decision_log.md)).
- `infra/secrets/` git-ignored, with keystore generation and loading in place from the start rather than at stage 5 ([ADR-023](decision_log.md)).
- Toolchains pinned: `uv` workspace for `services/api`, `services/agent`, `packages/protocol` (Python); `pnpm` workspace for `apps/web` and the TypeScript side of `packages/protocol`; Foundry for `contracts/`.
- Docker Compose local profile with PostgreSQL and Anvil; health checks.
- CI pipeline skeleton with the gates in [test_strategy.md](test_strategy.md) section 10, initially running lint and an empty test suite.
- Pre-commit hooks: format, lint, secret scan.
- `CLAUDE.md` and `docs/README.md` index.

**Exit condition:** `docker compose --profile local up` starts PostgreSQL and Anvil; CI passes on an empty commit; a developer can run each toolchain's test command and get zero tests, zero failures.

## Stage 1: Protocol and contracts

**Deliverables**
- `packages/protocol/`: JSON schemas (observation, agent decision, mandate, scenario, export), reason-code tables, EIP-712 fixtures with known digests and signatures.
- `contracts/`: `MockERC20`, `NegotiationExchange`, deployment script writing the manifest, unit tests, fuzz and invariant tests, gas snapshot.
- Reconstruction tool (`packages/protocol/tools/reconstruct.py`) reading only chain data.
- Python and TypeScript fixture tests confirming digests match Foundry.

**Exit condition:** Acceptance A05 through A11 pass in Foundry; fixture tests pass in all three languages; a deployment to Anvil produces a manifest that the reconstruction tool can read.

## Stage 2: Deterministic end-to-end run

**Deliverables**
- Backend: database models and migrations, run controller state machine, turn executor, observation builder, relay with outbox, indexer with canonicality, projection, setup validator, operator API (all routes except export and metrics may be stubbed), SSE.
- Agent service: internal API, `DeterministicPolicy`, `MandateValidator`, signer, key holder, HMAC auth.
- Compose profile runs api, agent-a, agent-b.
- Integration test harness with Anvil and PostgreSQL.
- `docs/runbook.md` created as a living document: local startup, recovering pending transactions. It grows in every subsequent stage and is completed in stage 5.

**Exit condition:** A02 (deterministic settlement) and A03 (infeasible no-deal) complete end to end via the API with evidence rows in every table; A06, A13, A14 integration tests pass; the export route produces a document that validates against the schema and the reconstruction tool agrees with it (A15).

## Stage 3: Model decisions

**Deliverables**
- `ModelPolicy`, `ModelClient` wrapper over the Anthropic SDK with structured outputs, prompt templates with versioning, `BudgetGuard`, price table config, repair loop, refusal and timeout handling.
- Outbound-context assertion and the isolation test suite.
- Decision records with usage and cost.

**Exit condition:** A04 and A12 pass; with live credentials, at least one genuine model-versus-model settlement and one genuine model-versus-model no-deal (on the infeasible clone) complete on Anvil, with exports saved under `docs/evidence/` and passing the invariant checker. No hardcoded agreement anywhere; grep test in place.

## Stage 4: React demonstration

**Deliverables**
- Setup screen with two isolated mandate editors, validation report, controls (Validate, Start, Step, Pause, Resume, Abort, Clone).
- Live view: agent panels, timeline with explorer links, settlement panel with before/after balances, observer reveal control, metric strip, disclosures.
- Replay mode and export download.
- Typed client generated from OpenAPI and protocol schemas.
- Playwright E2E.

**Exit condition:** A16 passes; a presenter can run the Stage 3 evidence as a replay and explain the agreement, authority chain, and balances from the screen alone. E2E passes on main.

## Stage 5: Sepolia run

**Deliverables**
- Sepolia deployment with manifest committed under `docs/deployments/`.
- Compose Sepolia profile; funding script for fresh test wallets. Keystore handling already exists from stage 0; this stage only supplies the Sepolia keystores and password.
- `SEPOLIA_RPC_URL` from an Alchemy application created for this project alone, so rate limits and usage are attributable to it; indexer poll interval 4 s, with the throttling response recorded in the runbook.
- Runbook completed: funding a testnet demonstration, recovery, replay, export.
- Confirmation threshold 2 and finalized-head tracking verified against a real RPC.
- Sepolia ENS names registered and pinned into the deployment manifest, display-only ([ADR-030](decision_log.md)). If registration is not ready, the deployment ships without names and they are added afterwards; stage 5 does not wait on them.

**Exit condition:** A17 passes; selected live scenarios (one settlement, one no-deal, one operator abort) execute on Sepolia, replay in the UI, and their exports are saved. Explorer links resolve to the manifest addresses.

## Stage 6: Evaluation

**Deliverables**
- Batch evaluator CLI: population generation from seed, four pairings, repetitions, sequential execution, per-run exports, invariant checker, report with distributions and bootstrap intervals.
- Metrics per spec 11.2 in the API and the report.
- Results document `docs/results-v0.1.md` reporting failures, costs, utility, and protocol limits honestly.

**Exit condition:** A01 evidence over the full population passes the invariant checker; the report separates simulated mUSD utility from real USD cost; infeasible-scenario trade rate is zero; mandate violations are zero; every run, including failures, is preserved.

## Release v0.1

Checklist from [prd.md](prd.md) section 9. Tag `v0.1.0`. Freeze protocol version 1.

---

## Working agreements for the build

- **One stage's contracts freeze before the next begins.** Changing a Stage 1 type string during Stage 3 is a decision-log entry and a protocol version bump.
- **Tests before implementation** for the contract, validator, signer, deterministic policy, and state machine. See the testing mindset in [engineering-principles.md](engineering-principles.md).
- **Small commits, green CI.** No merge with a red gate. Fixture-labelled runs only in CI.
- **No scripted agreement, ever.** If a live model does not settle, that is a result for Stage 6, not a bug to paper over.
- **Runbook grows with the code.** Every recovery procedure that a test exercises gets a runbook section in the same PR.

## Estimated effort

Rough, single developer with AI assistance, for planning only:

| Stage | Estimate |
|---|---|
| 0 | 1 to 2 days |
| 1 | 4 to 6 days |
| 2 | 8 to 12 days |
| 3 | 4 to 6 days |
| 4 | 6 to 9 days |
| 5 | 2 to 4 days plus testnet wait time |
| 6 | 3 to 5 days plus batch run time |

Stage 2 is the risk concentration: outbox, indexer, reorg, and recovery are where most subtle bugs live. Budget review time there.
