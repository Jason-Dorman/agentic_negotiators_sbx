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
| `make lint` | ruff format and check, `mypy --strict`, `lint-imports`, prettier, eslint, `tsc`, `forge fmt`, SPDX check, secret scan |
| `make test` | `pytest`, `vitest`, `forge test` |
| `make ci` | `make lint`, `make test`, then `make gates`; the same commands the pipeline runs, with `REQUIRE_INTEGRATION=1` as in CI, so it needs `make up` first |
| `make gates` | The gas snapshot check and the contract and Python coverage thresholds — the gates that are neither lint nor test |
| `make migrate` | Apply the backend's migrations to `DATABASE_URL` ([runbook.md](runbook.md) section 2) |
| `make up` / `make down` | The local Compose profile: PostgreSQL and Anvil |
| `make artefacts` | Check the committed ABIs and EIP-712 fixture against their generators (part of `make lint`) |
| `make abi` / `make fixtures` | Regenerate those two. A changed fixture is a protocol version bump, not a chore |
| `make snapshot` | Rewrite `contracts/.gas-snapshot` after an intended gas change |
| `make snapshot-check` / `make coverage-contracts` / `make coverage-python` | The gates individually |

### 1.1 Import boundaries (backend)

Enforced by an `import-linter` contract in CI and in `make lint`. The contract is written out in [`.importlinter`](../.importlinter) at the repository root and runs from stage 2.1: every module it names exists as a package from the first backend code, with a docstring naming the sub-stage that fills it, so the boundary is enforced as the modules fill in rather than switched on afterwards. Each of the six contracts was shown to break on a deliberate violation before the gate was turned on.

- `routes` → `controller`, `evidence`, `metrics`, `db.repositories`, `config`
- `controller` → `turns`, `relay`, `indexer`, `projection`, `observation`, `validation`, `db.repositories`, `agent_client`, `chain`
- `turns` → `observation`, `agent_client`, `relay`, `chain`, `db.repositories` (the relay through a protocol, `ActionRelay`; the timeline renderer through another, `DecisionSentences`)
- `validation` → `agent_client`, `chain`, `db.repositories`
- `relay`, `indexer` → `chain`, `db.repositories`
- `projection`, `observation`, `evidence`, `metrics` → `db.repositories`, `packages/protocol`
- `chain` → `packages/protocol`
- `db` → nothing above it
- Nothing imports `routes`. Nothing outside `db` touches SQLAlchemy sessions directly.
- Above `db`, import its interface modules — `api.db.records`, `api.db.enums`, `api.db.errors`, `api.db.protocols` — never the `api.db` package itself. Its `__init__` imports `Database` and so SQLAlchemy, and `sessions-stay-in-db` counts that transitive import as a violation. Tests and composition roots may import `api.db`.

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
- Value objects for domain primitives: `MinorAmount`, `Address`, `Digest`, `SessionId`, `Sequence`, in `negotiation_protocol.values` because both services pass them. Do not pass `int` or `str` for these across module boundaries. Canonical-JSON hashes (`observation_hash`, `mandate_hash`, `request_hash`) come from `negotiation_protocol.json_sha256` and nowhere else.
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
- **A CHECK constraint passes when its expression is NULL.** The first version of the run-outcome constraints accepted a closed outcome with no reason code and any outcome with no actor, because `'closed' AND NULL BETWEEN 1 AND 3` is NULL, not false. Test `IS NOT NULL` before comparing a nullable column in a CHECK, and test each constraint with the NULL case as well as the wrong value.
- **Inside an Alembic migration, wrap every constraint name in `op.f()`.** Alembic applies the metadata's naming convention to explicit names too, and a convention that embeds `%(constraint_name)s` turns `name="ck_runs_x"` into `ck_runs_ck_runs_x`. Autogenerate's comparison cannot see it — it does not compare check constraints — which is why the constraint tests assert every refusal by constraint name.
- **For a schema that claims to be exhaustive, assert the field list, not only the values.** Every negative-instance test in the observation suite passed while the schema carried a field the protocol document does not list. Nothing was checking the names against the document, and no value-level test could have.
- **An `int` subclass that overrides `__repr__` prints through it.** `int` has no `__str__` of its own, so `str()` and every f-string fall back to `__repr__`: `f"your offer of {amount}"` read "your offer of MinorAmount(94000000)" until `MinorAmount` and `Sequence` gained a `__str__` in stage 2.2. Any text that interpolates a value object — feedback to a model, a timeline sentence — needs a test that reads the text.
- **`jsonschema`'s `pattern` is `re.search`, and `$` matches before a trailing newline.** `"94000000\n"` satisfies `^[1-9][0-9]{0,77}$` as Python evaluates it. A schema check is the first gate, not the last: parse the value through its value object, which matches the whole string, before trusting it.
- **A validation error quotes its input.** Pydantic's message includes the rejected value, and so does a `jsonschema` error. Where the input can be a key, a secret or a mandate — a key pasted where its reference belongs, an observation — report the location and the rule that failed, never the value, and test that the value does not come back out.
- **Never `from conftest import …`.** With more than one `conftest.py` in a session a bare `conftest` is whichever was imported last, so the import works or fails by collection order: stage 2.2's second conftest made `pytest services/agent/tests packages/protocol/tests` fail at collection while the default order passed. Shared test helpers go in a named module on the `pythonpath`.
- **A log scan must be set up before the thing it scans starts.** Stage 2.2's first scan asked for the app fixture before the log-capture fixture, so the start-up line — the one place a key could plausibly be logged — went to an unconfigured logger, and an agent that logged its root passed. Search for every secret the process was given, in every form, not only the values you expected it to mention.
- **A race test has to force the race.** Stage 2.3's first test of two relays submitting one action used `asyncio.gather` and passed, and coverage then showed the branch it was named for — the database refusing the second insert — had never run: one submitter always finished its check before the other began. A barrier inside the adapter now holds both until each has passed the check. If the interleaving is the claim, arrange it; do not hope for it.
- **A test that reverts the chain must revert the thing it is about, and have a control.** The first test that a terminal run is not watched for reorgs took its snapshot after the session was already mined, so reverting changed nothing and it passed whatever the run's state. It now snapshots before the session opens and asserts, as its control, that the same reorg *is* reported once the run is not terminal.
- **A contract's ABI declares more than the protocol does.** OpenZeppelin's `EIP712` adds EIP-5267's `EIP712DomainChanged` event, and its token and guard contracts add their own errors. A decoder driven by the ABI decodes all of them; the indexer would then have stored an event with no `sessionId`. Decode against the protocol's own list, and test that the ABI's extras are refused.
- **Coverage can mark a line missing that ran.** Under Python 3.12 a few lines that follow an `await` inside an `async with` were reported uncovered although the test asserting their effect passed; replacing one with a `raise` made that test fail at once. A "missing" line is a question, not a verdict, in either direction — break the line and see.
- **None is an answer; an error is not.** The first chain adapter returned None for a block whenever the RPC failed — a JSON-RPC error, a null result from a lagging node — and the indexer read None as "this block is gone", which is a reorg. One "header not found" would have invalidated a run's evidence with the chain unchanged. Where None carries a meaning, return it only when that meaning is established, and raise otherwise.
- **A de-duplicating insert must decide what to do about the row it collides with.** `ON CONFLICT DO NOTHING` on `chain_events` meant that a row invalidated by a rewind could never come back: the rescan's insert collided with it and did nothing. Ask what state the existing row can be in, not only whether it exists.
- **An intent with consequences on chain is persisted before it is acted on — not only a signed action.** Stage 2.4's first controller closed a failed turn and only then sent the abort it owed; one RPC error between the two and the next tick asked the agent for a new decision, and a run that should have been aborted settled. A termination is now a column written in the turn's own unit of work. Ask of every "then do X" whether a crash or an outage between the two loses X.
- **A test that moves a clock can hide a wait.** The run-level A13 test gave the restarted process a clock five minutes ahead, so the takeover always found the old lease expired, and nobody noticed that recovery gave up on a live lease rather than waiting for it. Move a clock to stand for time that really passes, and test the case where it has not passed too.
- **A tamper that breaks the signature only tests that something fails.** Each leg of the signed-action check could be deleted with its test green, because every tampered field also invalidated the signature and another leg caught it. Re-sign the tampered message with the right key, so that only the leg under test can fail, and keep an untampered control.
- **A mutation must be the change it is named for.** "The session id written after `createSession`" was listed as killed, but the mutation that ran removed the write altogether; the reordering itself survived every suite. Name the edit you made, and make the edit you name.
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
