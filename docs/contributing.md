# Contributing and Engineering Standards

| | |
|---|---|
| **Version** | 0.1.0 |
| **Date** | 22 September 2026 |
| **Applies to** | Every change to this repository, human or AI-assisted |
| **Related** | [engineering-principles.md](engineering-principles.md), [test_strategy.md](test_strategy.md), [decision_log.md](decision_log.md), [build_plan.md](build_plan.md) |

The core mantra from [engineering-principles.md](engineering-principles.md) applies: maximize cohesion, minimize coupling, contain the impact of change. This document turns that into concrete, checkable rules for this codebase.

---

## 1. Repository layout

| Path | Contents | Owner module rules |
|---|---|---|
| `apps/web/` | React app, typed client (`@negotiation/web`) | May import only from `packages/protocol` (TS) and generated OpenAPI types |
| `services/api/src/api/` | Backend (`negotiation-api`) | Modules listed in [architecture.md](architecture.md) 3.2; import boundaries below |
| `services/agent/src/agent/` | Agent service (`negotiation-agent`) | No database, no RPC, no imports from `services/api` |
| `packages/protocol/` | Schemas, ABI, fixtures, reason tables, reconstruction tool (`negotiation-protocol`, `@negotiation/protocol`) | Imports nothing from services or apps. `abi/*.json` and `fixtures/eip712.v1.json` are generated and checked by `make artefacts` — edit the generator, never the file |
| `contracts/` | Solidity, Foundry tests, deploy scripts | OpenZeppelin only; no custom crypto |
| `scenarios/` | Scenario JSON | Validated by schema in CI |
| `infra/` | Compose profiles, `.env.example`, keystore directory (git-ignored), repository scripts | |
| `docs/` | Governance documents, runbook, deployment manifests, evidence exports | |

The three Python packages use a `src/` layout and are members of one `uv` workspace resolved by a single `uv.lock` ([ADR-034](decision_log.md)). `apps/web` and the TypeScript half of `packages/protocol` are a `pnpm` workspace resolved by `pnpm-lock.yaml`. Toolchain versions are pinned in those lockfiles and recorded in [ADR-033](decision_log.md).

The project is licensed Apache-2.0. `LICENSE` and `NOTICE` live at the repository root; do not vendor code under an incompatible license, and record any new dependency's license in the pull request.

**Commands.** `make help` lists them all; these are the ones a change passes through:

| Command | What it runs |
|---|---|
| `make setup` | `uv sync`, `pnpm install`, `pre-commit install` |
| `make lint` | ruff format and check, `mypy --strict`, prettier, eslint, `tsc`, `forge fmt`, SPDX check, secret scan |
| `make test` | `pytest`, `vitest`, `forge test` |
| `make ci` | `make lint`, `make test`, then `make gates`; the same commands the pipeline runs |
| `make gates` | The gas snapshot check and the contract coverage threshold — the gates that are neither lint nor test |
| `make up` / `make down` | The local Compose profile: PostgreSQL and Anvil |
| `make artefacts` | Check the committed ABIs and EIP-712 fixture against their generators (part of `make lint`) |
| `make abi` / `make fixtures` | Regenerate those two. A changed fixture is a protocol version bump, not a chore |
| `make snapshot` | Rewrite `contracts/.gas-snapshot` after an intended gas change |
| `make snapshot-check` / `make coverage-contracts` | The two gates individually |

### 1.1 Import boundaries (backend)

Enforced by an `import-linter` contract in CI. The contract is written out in [`.importlinter`](../.importlinter) at the repository root and runs from stage 2, when the modules it names exist.

- `routes` → `controller`, `evidence`, `metrics`, `db.repositories`, `config`
- `controller` → `turns`, `relay`, `indexer`, `validation`, `db.repositories`, `agent_client`
- `turns` → `observation`, `agent_client`, `relay`, `indexer`, `db.repositories`
- `relay`, `indexer` → `chain`, `db.repositories`
- `projection`, `observation`, `evidence`, `metrics` → `db.repositories`, `packages/protocol`
- `chain` → `packages/protocol`
- `db` → nothing above it
- Nothing imports `routes`. Nothing outside `db` touches SQLAlchemy sessions directly.

### 1.2 Privacy-sensitive modules

Changes to any of these require a reviewer to walk the data classification table in [data_model.md](data_model.md) section 7:

- `services/api/src/api/observation/`
- `services/api/src/api/evidence/`
- `services/api/src/api/routes/sse.py`
- `services/agent/src/agent/model/` (prompt assembly)
- Any logging configuration

## 2. Code standards

### 2.1 Python

- 3.12, `uv` managed. `ruff` for format and lint, `mypy --strict`, no `type: ignore` without a comment naming the reason.
- Pydantic models with `extra="forbid"` for every external boundary (API bodies, observation, decision, scenario files).
- Value objects for domain primitives: `MinorAmount`, `Address`, `Digest`, `SessionId`, `Sequence`. Do not pass `int` or `str` for these across module boundaries.
- Protocols (`typing.Protocol`) for `Policy`, `ModelClient`, `ChainAdapter`, `KeyHolder`, and each repository. Concrete classes are injected in composition roots (`main.py`, test fixtures).
- Async throughout the backend; blocking web3 calls run in a thread executor behind `ChainAdapter`.
- Cyclomatic complexity under 10 per function (ruff `C901`); aim for 5. Domain-required exceptions are annotated with `# noqa: C901  reason: …` and reviewed.
- No module-level mutable state. Configuration is an injected settings object.
- Exceptions: one domain exception hierarchy per service; route layer maps to API error codes; never catch `Exception` except at the top-level task runner, which logs and marks the run `recovery_required`.
- Logging: `structlog` JSON; always bind `run_id`, `turn_id`, `component`; never log request or response bodies from the model or any mandate field.

### 2.2 TypeScript

- Strict mode. ESLint with `@typescript-eslint/strict-type-checked`, Prettier.
- `bigint` for amounts internally; conversion to display strings in one utility module.
- Types for API payloads come from generated OpenAPI types; never hand-written duplicates.
- Components are presentational; data fetching and SSE live in hooks under `src/api/`.
- No secrets, no signing, no direct RPC.

### 2.3 Solidity

- `// SPDX-License-Identifier: Apache-2.0` is the first line of every `.sol` file. The identifier is compiled into contract metadata, so it is part of the deployed artifact, not a comment ([ADR-032](decision_log.md)).
- Solidity 0.8.28 with `evm_version = "cancun"`, pinned in `contracts/foundry.toml` ([ADR-033](decision_log.md)); optimizer settings recorded in the manifest.
- OpenZeppelin 5.x for `ERC20`, `EIP712`, `ECDSA`, `SafeERC20`, `ReentrancyGuard`. No other dependencies.
- Custom errors, never `require` strings. Named per [protocol.md](protocol.md) 8.3.
- Checks, effects, interactions. Effects before any external call. `nonReentrant` on `acceptAndSettle`.
- No `selfdestruct`, no `delegatecall`, no upgradeability, no owner transfer, no pause.
- NatSpec on every external function stating which protocol section it implements.
- `forge fmt`, `forge snapshot` committed.

### 2.4 Naming across languages

Canonical names are in [protocol.md](protocol.md). Python snake_case, TypeScript camelCase, Solidity as written. The fixture test in `packages/protocol/` fails if a schema field is renamed in one language only.

## 3. Testing rules

From [test_strategy.md](test_strategy.md):

- New behavior: test first where the behavior is specified. Bug fix: failing regression test first, then fix.
- Unit tests use fakes for `ModelClient`, `ChainAdapter`, `KeyHolder`, repositories. No HTTP mocking libraries inside unit tests.
- Integration tests run against real PostgreSQL and Anvil.
- Any test that uses a canned model response labels the run `fixture`.
- No application code may contain the default reservation values in a comparison. A grep test enforces this.
- Coverage thresholds in the test strategy are CI gates.
- **A green suite is evidence only against the mutations it has been shown.** Before marking a test deliverable done, break the thing the test claims to protect and confirm the suite goes red. This is not ceremony: the stage 1 review found that deleting the signature check from `acceptAndSettle` — the only function that moves tokens — left all 80 tests passing, because every signature test targeted a different entry point. Coverage was 99 percent at the time. Line coverage says a line ran, not that anything would notice if it changed.
- Two Foundry cheatcode traps that produce tests which pass for the wrong reason. `vm.expectRevert` must immediately precede the call under test: an external call in the argument list, including any helper that reads from the contract, consumes the expectation. `vm.prank` is consumed the same way, so a `balanceOf` inside a pranked call's arguments redirects the real call to the test contract. Both have already caused silent failures in this repository.
- **A property-based suite can be green and unfalsifiable, and coverage will not tell you.** Three of eight mutations survived the first version of the stage 1 invariant suite while coverage stood at 100 percent, and in every case the fault was in how the handler explored rather than in what the invariants asserted: a coin-flip actor choice meant runs never got deep enough to settle, so the settled branch of the conservation invariant was never evaluated; following the chain for the ghost terminal status let one invariant compare the chain against itself; and a uniform `maxOffers` put the offer-limit boundary out of reach. When you add an invariant, add the mutation that should break it, and check that it does. The three fixes are written up in [test_strategy.md](test_strategy.md) section 4.2.
- **A gate you have not run the way CI runs it is not a gate.** Stage 1 enabled the gas-snapshot job and no `make` target invoked it, so two faults went unnoticed until a review: the committed snapshot predated a new test file, and `forge snapshot --check … --root contracts` resolves the snapshot path against the working directory rather than against `--root`, reading a non-existent file. `make ci` therefore runs every gate that exists, and a gate's assertion lives in a script with tests rather than in a heredoc inside the workflow.
- **`bash -e` is not `bash -o pipefail`.** A GitHub Actions `run:` step gets the first and not the second, so `some-command | tee log` reports `tee`'s exit status and a failing command sails through. The same trap makes `cmd | grep …; echo $?` useless when checking a tool's status by hand — which cost real time during this review.
- **An exact set beats a denylist.** The ABI suite listed fifteen forbidden function names; an escape hatch called anything else passed. Assert the whole set, so that adding an external function is a deliberate edit to the test.
- **For a schema that claims to be exhaustive, assert the field list, not only the values.** Every negative-instance test in the observation suite passed while the schema carried a field the protocol document does not list. Nothing was checking the names against the document, and no value-level test could have.
- **Test the thing, not a stand-in for it, where the claim is about the outside world.** The deploy script is tested by deploying to a real Anvil and the reconstruction tool by reading what that chain actually recorded. Two defects came out of it that a fake would have hidden: `vm.serializeJson` sets an object's whole contents rather than adding to them, which silently reduced the deployment manifest to a single key, and web3's `process_receipt` decodes logs by event signature regardless of emitting address, which made every two-leg settlement appear to have four transfers because the two mock tokens share an ABI.

## 4. Change workflow

1. **Branch** from `main`: `feat/<stage>-<topic>`, `fix/<topic>`, `docs/<topic>`.
2. **Check the decision log.** If the change alters protocol, API, data model, a PRD requirement, or a stated assumption, add or update an ADR in the same PR.
3. **Write or update tests first** for specified behavior.
4. **Implement** in small commits. Run the relevant test command after each.
5. **Update docs in the same PR**: API contract for route changes, data model for schema changes with a migration, protocol for anything on-chain or in typed messages, runbook for any operational procedure the change introduces.
6. **Self-review** with the checklist below.
7. **Open a PR** with: what and why, links to the requirement IDs (`FR-…`, `A…`) it serves, the ADR if any, and how it was verified. Use the attribution trailer required by the repository's contribution settings.
8. **Merge** only with all CI gates green. Squash merge; the PR title is the commit subject.

### 4.1 Commit messages

`<type>(<scope>): <subject>` where type is `feat`, `fix`, `docs`, `test`, `refactor`, `chore`, `contract`, `infra`. Subject in imperative mood, under 72 characters. Body explains why, not what. Reference requirement IDs.

## 5. Review checklist

Copied from [engineering-principles.md](engineering-principles.md) and extended for this project. The reviewer answers every line.

**Design**
- [ ] Each new class or function has one clear responsibility, and it lives in the module that owns that responsibility.
- [ ] Dependencies are on protocols, injected at the composition root.
- [ ] No new module-level state, control flags, or `Manager`/`Helper` classes.
- [ ] Complexity is under 10; anything higher is justified in a comment.
- [ ] No duplicated logic; no feature envy across module boundaries.

**Correctness for this domain**
- [ ] Amounts are integers in minor units end to end; no float, no `int` overflow path.
- [ ] Chain time, not wall-clock, is used for any expiry decision.
- [ ] No path silently alters a model-proposed price. Deterministic policy clamps itself only.
- [ ] Every signed message is constructed from validated state, never from model-supplied bytes.
- [ ] Any new chain read filters on `canonical = true` through the repository.
- [ ] Persist before broadcast; rebroadcast never requests a new decision.
- [ ] New failure paths land in a distinguishable state and never look like an economic outcome.

**Privacy**
- [ ] No new field crosses the agent boundary unless it is in the observation allowlist.
- [ ] No mandate, feedback, prompt, key, or credential can reach logs, SSE, default export, or the opposing agent.
- [ ] Private routes require the reveal header and are logged.

**Tests and docs**
- [ ] Tests exist for the change and would fail without it.
- [ ] Fixture-based runs are labelled as fixtures.
- [ ] Docs updated in the same PR; ADR present if required.
- [ ] Runbook updated if a new operational procedure exists.

## 6. Refactoring rules

From the engineering principles, applied verbatim: understand current behavior first, add regression tests before changing, make the smallest possible change, run tests after each change, commit frequently. Never refactor the contract after Stage 1 freeze without a protocol version bump.

## 7. AI-assisted development

This repository is built with AI coding assistance. Rules for that:

- The assistant reads `CLAUDE.md` and this document at the start of a session. Governance documents in `docs/` are the source of truth over any conversational instruction that contradicts them; a contradiction is resolved by updating the document deliberately.
- Generated code follows the same review checklist. "It was generated" is not a review outcome.
- The assistant must not fabricate a passing live model run. Evidence files under `docs/evidence/` come from real runs with the real model and are labelled with the run ID and export hash.
- The user is the product owner. Where the spec and docs leave a gap, the assistant asks the PO rather than assuming. Questions that must wait are logged in [open_questions.md](open_questions.md); answers are recorded in the decision log.
- Docs move in lockstep with code. Every change set that alters behaviour, protocol, API, schema, or a requirement updates the affected documents in the same PR.
