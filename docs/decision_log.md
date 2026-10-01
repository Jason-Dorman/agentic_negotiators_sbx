# Decision Log

| | |
|---|---|
| **Purpose** | Record architecture and product decisions with their rationale so they are visible, discussable, and changeable |
| **Format** | One entry per decision. Status is `proposed`, `accepted`, `superseded by ADR-n`, or `rejected` |
| **Rule** | A decision that changes protocol, API, data model, or a stated PRD requirement gets an entry before the code merges |

Entries ADR-001 to ADR-010 restate spec Appendix B. Entries from ADR-011 are decisions made while producing the governance set and are marked as assumptions where the spec was silent. Open items are in [open_questions.md](open_questions.md).

---

## ADR-001: Keep the two-agent asset exchange small enough to finish
**Status:** accepted (spec B.1)
**Decision:** v0.1 is exactly two parties, one asset pair, price-only, one run at a time.
**Consequences:** No matching engine, no multi-attribute negotiation, no partial fills. Appendix A compute negotiation waits.

## ADR-002: Language split
**Status:** accepted (spec B.2)
**Decision:** Python for backend and agents, React with TypeScript for the interface, Solidity for on-chain enforcement.
**Consequences:** Three toolchains; cross-language alignment enforced by `packages/protocol/` fixtures.

## ADR-003: Manufactured mandates and test assets
**Status:** accepted (spec B.3)
**Decision:** No live financial exposure. Mock ERC-20s, testnets only.
**Consequences:** Every surface labels test assets and simulated economics.

## ADR-004: A signed offer is real authority
**Status:** accepted (spec B.4)
**Decision:** An active offer authorizes the counterparty to execute that trade. Validation therefore happens before signing an offer, not only before acceptance.
**Consequences:** The policy signer validates the complete trade on every offer.

## ADR-005: Public actions on-chain, private inputs off-chain
**Status:** accepted (spec B.5)
**Decision:** Offers, acceptance, closure, expiry, abort, and settlement are on-chain. Mandates, prompts, raw responses, and feedback are private database records.
**Consequences:** Data classification in [data_model.md](data_model.md) section 7; export defaults exclude private data.

## ADR-006: Three enforcement layers stay distinct
**Status:** accepted (spec B.6)
**Decision:** Mandate enforcement (policy signer), model strategy (policy), and contract enforcement are separate components with separate tests.
**Consequences:** The controller never chooses a price; the contract never knows a mandate.

## ADR-007: Failure kinds are distinct outcomes
**Status:** accepted (spec B.7)
**Decision:** No-deal, expiry, model failure, execution failure, and operator abort are separate recorded results.
**Consequences:** Separate reason enums on-chain; `failure_class` in metrics; `RECOVERY_REQUIRED` is not an outcome.

## ADR-008: Deterministic baseline before economic claims
**Status:** accepted (spec B.8)
**Decision:** Every model result is compared to the linear-concession baseline on the same population.
**Consequences:** Batch evaluator runs four pairings; reports distributions.

## ADR-009: Compute negotiation is a follow-on
**Status:** accepted (spec B.9)
**Decision:** Not in v0.1. Reuse agent interface, mandates, signing, and evidence viewer later with a separate settlement model.

## ADR-010: No business model baked in
**Status:** accepted (spec B.10)
**Decision:** No lender, insurer, or marketplace in v0.1. Results inform the choice.

---

## ADR-011: Single backend process with module boundaries
**Status:** accepted
**Context:** Spec 4.2 permits API, relay, and indexer to share a process and discourages a broker.
**Decision:** One FastAPI process; `controller`, `turns`, `relay`, `indexer`, `projection`, `observation`, `evidence`, `metrics` as separate packages with an import-boundary lint. Background work runs as asyncio tasks owned by the controller under a database lease.
**Consequences:** Simple deployment; a later split into processes is a packaging change, not a rewrite.

## ADR-012: Agent service owns decide, validate, and sign in one call
**Status:** accepted
**Context:** Spec 9.2 lists decision and signing as separate steps; the repair loop needs private feedback that must not leave the agent boundary.
**Decision:** `POST /internal/runs/{id}/turn` performs decide, validate, one repair, and sign inside the agent service and returns the signed action plus decision records. The backend persists the records but never feeds them back.
**Consequences:** Private feedback never transits to the backend before the turn completes as a batch; the same interface serves deterministic and model policies.

## ADR-013: Decision envelope with separate `explanation`
**Status:** accepted (confirmed by the product owner, 21 September 2026)
**Context:** Spec 5.4 says the decision has exactly three shapes and extra fields are rejected. Spec 3.2 allows an optional operator explanation.
**Decision:** The model returns `{ "decision": <one of three shapes>, "explanation"?: string }`. The `decision` object is strict; `explanation` is capped at 280 characters, operator-only, and labelled a self-report.
**Consequences:** Both spec statements hold without ambiguity. Schema in `packages/protocol/schemas/agent_decision.v1.json`.

## ADR-014: Anthropic Claude API as the model provider
**Status:** accepted (confirmed by the product owner, 21 September 2026)
**Context:** Spec does not name a provider or model ID.
**Decision:** Official `anthropic` Python SDK. Default `claude-opus-5`, configurable per run and per side. Structured outputs via `messages.parse` with a Pydantic decision envelope. Adaptive thinking on with `effort` recorded per run. SDK automatic retries set to 0 so the single repair is the only retry and cost accounting is exact.
**Consequences:** "Sampling settings" in spec 11.3 are recorded as `effort` because current models do not accept temperature. Seed is recorded as unsupported. Model client is behind an interface so another provider is an adapter, not a rewrite; see [ADR-027](#adr-027-cross-provider-pairings-are-a-follow-on-experiment).

## ADR-015: No server-side model fallbacks
**Status:** accepted (derived from spec 11.3)
**Context:** The SDK supports routing a refused request to a fallback model.
**Decision:** Disabled. A `refusal` stop reason is treated as an invalid response: one repair, then `model_failure`.
**Rationale:** The recorded `model_id` must be the model that decided; silent substitution would corrupt reproducibility and the model-versus-baseline comparison.

## ADR-016: Turn rule after an expired offer
**Status:** accepted (clarification)
**Context:** Spec 6.2 says the other party may submit a replacement offer when an offer expires.
**Decision:** The next proposer is always the counterparty of the most recent recorded offer's proposer, whether or not that offer expired. Sequence 1 belongs to the buyer.
**Consequences:** Contract stores `activeProposer` even after expiry; no special case.

## ADR-017: Contract custom errors are part of the protocol
**Status:** accepted
**Decision:** The named errors in [protocol.md](protocol.md) 8.3 are stable identifiers decoded by the indexer and surfaced as `revert_error`.
**Consequences:** Renaming an error is a protocol change.

## ADR-018: Observer reveal via explicit header
**Status:** accepted (assumption)
**Decision:** Private routes require `X-Observer-Reveal: true` and are access-logged. This is friction and audit, not a security control; the operator already owns everything.

## ADR-019: One active run enforced in the database
**Status:** accepted (confirmed by the product owner, 21 September 2026)
**Decision:** A single-row `active_run` table plus `run_leases` with expiry. Relay nonces are serialized under the same lease.
**Consequences:** Batches run sequentially. Parallel evaluation is a future change requiring per-run relay keys.

## ADR-020: PostgreSQL enums and NUMERIC(78,0) amounts
**Status:** accepted
**Decision:** Enum types for all state fields; `NUMERIC(78,0)` for token amounts; JSON strings for amounts on the wire.
**Rationale:** Prevents silent overflow and float rounding; keeps the database self-describing.

## ADR-021: Toolchain
**Status:** accepted (recorded by the product owner in `CLAUDE.md`)
**Decision:** Python 3.12 with `uv`, `ruff`, `mypy --strict`, `pytest`; Node LTS with `pnpm`, Vitest, Playwright; Foundry; Alembic; Docker Compose; PostgreSQL 16.
**Consequences:** Exact pins recorded here when the lockfiles are first committed.

## ADR-022: Scenario files are JSON validated by schema
**Status:** accepted (confirmed by the product owner, 21 September 2026)
**Decision:** `scenarios/*.json` validated against `packages/protocol/schemas/scenario.v1.json`. Batch populations are generated JSON committed with their seed.
**Rationale:** One serialization format across the repo; no YAML parser dependency.

## ADR-023: Sepolia participant keys in encrypted keystores
**Status:** accepted (confirmed by the product owner, 21 September 2026); amended by [ADR-039](#adr-039-per-run-participant-keys-are-derived-inside-the-agent-service) on 25 September 2026
**Decision:** Local profile uses `env:` key refs generated per run. Sepolia profile uses web3 keystore JSON files with a password from env, present from stage 0 rather than retrofitted at stage 5. Neither is production custody.
**Consequences:** `infra/secrets/` is git-ignored and the secret scan rejects keystore JSON. The keystore path reaches the key holder as a `keystore:` ref; the password reaches it from env; neither is ever a value in the database, a log, or an export. The local profile keeps `env:` refs so a developer needs no password to run the suite.
**Amendment (ADR-039):** the two reference forms, the password handling and everything under Consequences stand. What they point at changes: each agent instance's reference now resolves to a **root** secret from which the agent derives a fresh key per run, rather than to a participant key regenerated by hand before every run.

## ADR-024: Timeline sentences rendered server-side once
**Status:** accepted
**Decision:** The backend renders the plain sentence for each timeline entry and stores it. UI, replay, and export display the stored sentence.
**Rationale:** Replay and export must match the live view byte for byte.

## ADR-025: Prompt caching of the static system prompt
**Status:** accepted
**Decision:** The per-run system prompt (role, protocol rules, output schema, mandate text) is the cached prefix; the observation follows. Cache read tokens are recorded in `decisions.usage`.
**Note:** The mandate is in the system prompt for the agent's own side only. It is static for the run, so it is safe to cache and cheaper to repeat.

## ADR-026: Diagrams are Mermaid
**Status:** accepted (user instruction, 19 September 2026)
**Decision:** All diagrams in project documents are Mermaid code blocks. No images or external diagram files.

## ADR-027: Cross-provider pairings are a follow-on experiment
**Status:** accepted
**Context:** The product owner wants to pit models from different vendors against each other eventually; the first run is Claude against Claude.
**Decision:** v0.1 fixes the provider to Anthropic. A cross-provider bake-off is a named follow-on, not a v0.1 requirement. The `ModelClient` interface and the per-side `model_id` already admit it: a second provider is an adapter behind the same interface plus a price-table entry.
**Consequences:** Two things must exist before such a comparison means anything, and neither is built in v0.1. First, the pairing matrix in the batch evaluator has to grow beyond the four pairings ADR-008 fixes, because a cross-vendor comparison needs its own baseline column. Second, the prompt is a confound: one versioned prompt tuned against one provider's structured-output behaviour will not be neutral across vendors, so the follow-on needs either a provider-neutral prompt held constant or a recorded per-provider prompt version and an honest statement that prompt and model vary together. The results document states this limitation rather than implying the v0.1 numbers generalize across vendors.

## ADR-028: Operator authentication is a static token, and public exposure is gated on a STRIDE review
**Status:** accepted
**Context:** Spec and API contract left remote authentication open. The demo runs on localhost.
**Decision:** A single static bearer token in `OPERATOR_TOKEN`, required on every route except `/health` when it is set, with TLS terminated by a reverse proxy in front of the backend. Development binds to localhost and sets no token.
**Consequences:** Exposing the UI or backend on a network the operator does not control is a separate decision that requires a STRIDE-structured pass over [security_and_trust_boundaries.md](security_and_trust_boundaries.md) section 5 first, recorded as its own ADR. A static token is a single shared credential with no rotation, no per-user identity, and no revocation short of restarting with a new value; it is adequate for one operator on one host and is not adequate for an audience network. The checklist in security 8 is the minimum, not the review.

## ADR-029: The agent system prompt is a reviewed, versioned file and the mandate cannot override it
**Status:** accepted
**Context:** Spec 5 requires the decision schema and protocol rules to hold regardless of mandate content. Mandate `instructions` are free text supplied per run through the API.
**Decision:** The system prompt lives in versioned files under `services/agent/prompts/` and changes through review like any other source. Its version hash is recorded on every decision. Mandate `instructions` are appended to the prompt in a delimited section introduced as the agent's own private guidance, after the protocol rules and the output schema, and the prompt states that nothing in that section can change the rules above it or the shape of the output.
**Consequences:** Precedence is prompt-structural, not enforced by the model, so it is backed by the layer that does enforce: the policy signer rejects any action outside the legal set and outside the mandate, and the decision schema is strict with `extra="forbid"`. An instruction that tells the agent to emit a fourth decision shape or to reveal its mandate produces an invalid response, one repair, then `model_failure` — a recorded outcome, not a leak. The isolation suite includes a mandate whose `instructions` attempt exactly that.

## ADR-030: ENS names for the Sepolia deployment are display-only
**Status:** accepted (confirmed by the product owner, 21 September 2026)
**Context:** The product owner asked whether the deployment can carry an ENS name. Reasoning in [open_questions.md](open_questions.md) under Q16.
**Decision:** One name registered on Sepolia ENS with subnames for the exchange and both mock tokens. Names are resolved once, at deployment time, and written into the deployment manifest beside the address they resolved to. No component resolves ENS at run time: not the indexer, not the setup validator, not the signer, not the reconstruction tool. Identity checks continue to compare the manifest address against the chain.
**Rationale:** A name is mutable state controlled by whoever holds the registration. Canonical chain events are the only source of economic outcome, and a mutable label must never enter that chain of evidence. Display-only keeps the property that the demo can be verified by someone who ignores the names entirely. Sepolia names are also visible only on Sepolia, so they are a demo affordance and not a public identity.
**Consequences:** A nullable `ens` block in the deployment manifest, the `deployments` API resource, and `deployments.ens` in the database. A name chip beside addresses in the UI, with the address still shown (PRD FR-U10). A17 additionally resolves each recorded name at deployment time and compares it with its manifest address, recording a mismatch as a manifest warning, never as a run failure. Stage 5 does not block on registration: without names, `ens` is null and everything else is unchanged.

## ADR-031: Mandates are stored in plaintext, and the trust assumption is stated
**Status:** accepted (confirmed by the product owner, 21 September 2026)
**Context:** `mandate_versions` holds the private experimental inputs whose leakage would invalidate PRD claim 1. Column-level encryption was considered for v0.1.
**Decision:** No encryption at rest. Mandate confidentiality rests on process and credential isolation, the repository access rule in [data_model.md](data_model.md) 3.4, the import-boundary test that prevents the observation builder reading the opponent's row, and the classification table. The security document states this in those words rather than implying the database is protected.
**Rationale:** Encryption would defend a stolen database file, not the leakage path that matters, and the decryption key would sit in the same `.env` on the same host as the database. The cost is concrete: the two mandate amounts are `NUMERIC(78,0)`, so encrypting them makes them `BYTEA` and moves the feasible-interval and metrics computations out of SQL, while `mandate_hash` must still be computed over plaintext canonical JSON and so gains nothing.
**Consequences:** An exported database dump contains readable mandates, which the retention statement and the export defaults must account for; the default export already excludes private data (ADR-005, FR-E8). If the stance changes, the cheapest upgrade is pgcrypto on `instructions` alone, which is free text and never compared or aggregated, leaving the numeric fields queryable.

## ADR-032: Apache-2.0
**Status:** accepted (confirmed by the product owner, 21 September 2026)
**Context:** The repository was public with no license, which grants a reader no rights at all.
**Decision:** Apache-2.0. `LICENSE` and `NOTICE` at the repository root. `// SPDX-License-Identifier: Apache-2.0` is the first line of every Solidity source.
**Rationale:** Apache-2.0 carries an explicit patent grant and the `NOTICE` convention, and is the usual choice for contract and infrastructure code. MIT would have been equally safe and was rejected only for being less explicit.
**Consequences:** The SPDX identifier is compiled into contract metadata and therefore into the deployed artifact and its verified source on Etherscan, so it must be right before stage 1 rather than corrected later. New dependencies must be license-compatible, and a pull request adding one records its license.

## ADR-033: Stage 0 toolchain pins
**Status:** accepted (confirmed by the product owner, 22 September 2026)
**Context:** [architecture.md](architecture.md) section 10 named the technology choices and deferred the exact versions to "implementation start, pinned in lockfiles, and recorded in the decision log". Stage 0 is that moment.
**Decision:** The versions below, pinned in `uv.lock`, `pnpm-lock.yaml`, `contracts/foundry.toml` and the image tags in `infra/compose.local.yaml`. CI installs the same versions from the same files, so a green local run and a green pipeline mean the same thing.

| Component | Pin | Where | Latest available on 22 September 2026 |
|---|---|---|---|
| Python | 3.12 (3.12.3 resolved) | `.python-version`, `requires-python = ">=3.12,<3.13"` | — |
| uv | 0.12.17 | Installed toolchain; `uv.lock` revision 3 | 0.12.17 |
| Node | 22.20.0 (22 LTS) | `.nvmrc`, `engines.node` | — |
| Corepack | 0.34.0 (bundled with Node 22.20.0) | Node distribution | — |
| pnpm | 11.27.1 | `packageManager`, `engines.pnpm` | 12.5.1 |
| TypeScript | 6.0.3 | root `devDependencies` | 7.0.2 |
| typescript-eslint | 8.70.1 | root `devDependencies` | 8.70.1 |
| ESLint | 10.11.0 | root `devDependencies` | 10.11.0 |
| React / Vite / Vitest | 19.3.0 / 8.3.0 / 5.0.1 | `apps/web/package.json` | same |
| Foundry | v1.8.3 | `FOUNDRY_VERSION` in CI, image tag in Compose | v1.8.3 (latest tagged release) |
| Solidity | 0.8.28, `evm_version = "cancun"` | `contracts/foundry.toml` | — |
| PostgreSQL | 16.15-alpine | `infra/compose.local.yaml` | 16.15 on the 16 line |

**Added in stage 1**, for the cross-language digest check and the reconstruction tool. Licenses recorded here because a reviewer has to see a dependency arrive in order to check one ([contributing.md](contributing.md) section 1).

| Component | Pin | Where | License | Why |
|---|---|---|---|---|
| viem | 2.56.8 | `packages/protocol/devDependencies` | MIT | The TypeScript EIP-712 implementation in the three-language fixture check. [test_strategy.md](test_strategy.md) section 5 leaves the choice between viem and ethers to the implementer; viem was taken for being tree-shakeable and for typing `hashTypedData` against the type definitions rather than against a loose object. A dev dependency only: the web client never signs ([contributing.md](contributing.md) section 2.2), it verifies. |
| eth-abi | 6.0.0 | `negotiation-protocol` | MIT | `abi.encode` in Python, for `configHash` and the three struct hashes. Already present transitively via `eth-account`; declared because it is imported directly. |
| referencing | 0.37.0 | `negotiation-protocol` | MIT | The JSON Schema files cross-reference each other by file name, which needs an explicit registry rather than jsonschema's default resolver. Also already present via `jsonschema`. |
| types-jsonschema | 4.26 | root `dev` group | Apache-2.0 | `mypy --strict` with `disallow_any_unimported` treats an unstubbed import as an error, which is the point: a validator whose return type is `Any` checks nothing as far as the type checker is concerned. |

**Two pins sit behind the newest major. Each is a reproduced incompatibility, not a policy of caution.**

- **pnpm 11.27.1, not 12.5.1.** Reproduced on Node 22.20.0 with its bundled Corepack 0.34.0: `corepack prepare pnpm@12.5.1 --activate` downloads the package, then `pnpm --version` exits non-zero with `Error: Cannot find module '~/.cache/node/corepack/v1/pnpm/12.5.1/bin/pnpm.cjs'`. The unpacked 12.5.1 tree contains `bin/pnpm.mjs` and no `bin/pnpm.cjs`; Corepack 0.34.0 resolves the `.cjs` path. pnpm 11.27.1 and 10.34.5 were both checked and both ship `bin/pnpm.cjs`. Pinning 11.27.1 keeps `corepack enable pnpm` as the entire setup step locally and in CI, with no second installer to hold in step. This is a statement about these two versions on this Node line, not about pnpm 12 generally; revisit when the Corepack bundled with the supported Node LTS can activate it.
- **TypeScript 6.0.3, not 7.0.2.** typescript-eslint 8.70.1 — the current release — declares `peerDependencies.typescript: ">=4.8.4 <6.1.0"`. TypeScript 7.0.2 is the Go rewrite and falls outside that range. Taking it would mean running typescript-eslint unsupported or dropping `strict-type-checked`, which is the rule set [contributing.md](contributing.md) section 2.2 requires, and the type checker is one of the merge gates in [test_strategy.md](test_strategy.md) section 10. 6.0.3 is the newest release inside the supported range. Revisit when typescript-eslint declares support for 7.

**Rule this sets:** a major version is not adopted merely for being newer when doing so breaks a required lint or type-checking gate or leaves the supported toolchain. The gate wins; the pin waits for the ecosystem. Each such pin names the version that was rejected, the version that was taken, and the error that was reproduced, so the next person can retest it in one command rather than re-deriving the reason.

**Consequences:** Solidity 0.8.28 with `evm_version = "cancun"` is valid on both Anvil and Sepolia, and the optimizer settings in `[profile.default]` are recorded in the deployment manifest, so changing anything in that block changes the deployed artefact and is itself a decision-log entry. All three lockfiles are committed with a real text diff rather than marked binary, because a reviewer has to see a new dependency arrive in order to record its license ([contributing.md](contributing.md) section 1).

**`contracts/foundry.lock` is committed** (decided 22 September 2026). It holds nothing sensitive and cannot: 266 bytes naming two dependency paths, two version tags and two commit revisions, all of them public and all of them already implied by `.gitmodules` and the submodule gitlinks. Foundry's own `forge init` generates the file and deliberately omits it from the `.gitignore` it generates alongside, so it is upstream's default that the file is tracked. It does not churn — `forge build --force`, `forge test` and `forge fmt` leave it byte-identical.

The reason to keep it rather than lean on the gitlinks is legibility in review. `git submodule status` renders the OpenZeppelin pin as `v4.8.0-1217-gcab19933`, because `git describe` reaches for the nearest tag in an unrelated lineage; the revision is correct but the name is not what anyone installed. `foundry.lock` records the same revision as `v5.7.0`. A submodule bump shows in a diff as one opaque `Subproject commit` line, while the lock shows `v5.7.0` becoming `v5.8.0` — which is the form a reviewer needs to do the license check contributing.md section 1 requires of them. Practice across public Foundry repositories is mixed, but no repository surveyed gitignores it; the ones without the file predate the feature.

## ADR-034: `src/` layout for the three Python packages
**Status:** accepted
**Context:** [contributing.md](contributing.md) section 1.1 names backend modules by path (`services/api/observation/`), which reads as a flat package at the service root. Stage 0 had to choose the actual layout.
**Decision:** `src/` layout: `services/api/src/api/`, `services/agent/src/agent/`, `packages/protocol/src/negotiation_protocol/`. Import package names are `api`, `agent` and `negotiation_protocol`; distribution names are `negotiation-api`, `negotiation-agent` and `negotiation-protocol`. Section 1.1's paths are updated to match.
**Rationale:** Under a flat layout the repository root is on `sys.path` during a test run, so a test can import a module that the installed package does not actually ship, and `mypy --strict` type-checks a tree that is not the distributed one. That failure mode is quiet and it would surface first in the Compose images. `api` is kept as the import name because [architecture.md](architecture.md) section 3.6 specifies the evaluator entry point as `python -m api.eval`.
**Consequences:** Each package is installed into the workspace environment in editable mode by `uv sync`; `mypy_path` and ruff's `src` setting name the three `src/` directories. `packages/protocol/tools/` stays outside `src/` because it is a script directory, not importable API, as [build_plan.md](build_plan.md) stage 1 specifies.

## ADR-035: `web3` is an optional extra of the protocol package
**Status:** accepted
**Context:** The reconstruction tool (`packages/protocol/tools/reconstruct.py`, A15) reads chain data, so it needs an RPC client. The agent service depends on the protocol package and must have no RPC connection at all ([architecture.md](architecture.md) section 3.3).
**Decision:** `web3` is declared as `negotiation-protocol[tools]`, not a core dependency. The agent service installs `negotiation-protocol` and therefore does not get web3.
**Rationale:** Isolation is meant to be structural rather than procedural. If web3 is present in the agent's environment, "the agent never talks to the chain" is a convention that a future import can break silently; if it is absent, the same mistake is an `ImportError` at start-up. The import-linter contract forbids it as well, so the rule is enforced twice, at different times.
**Consequences:** Anything that runs the reconstruction tool installs the extra explicitly. The `agent-has-no-database-and-no-rpc` contract in `.importlinter` names `web3` alongside `api`, `sqlalchemy`, `asyncpg` and `alembic`.

## ADR-036: The local PostgreSQL is namespaced away from the operator's other projects
**Status:** accepted (directed by the product owner, 22 September 2026)
**Context:** `docker compose --profile local up` failed on the product owner's machine with `ports are not available: exposing port TCP 127.0.0.1:5432`, because another project already published PostgreSQL there. The wider risk is worse than a failed start-up: a stack that reaches a database on a shared default port, under a default name, can silently attach to another project's data instead of failing.
**Decision:** Four separate namespaces, each explicit:

| | Value | Set in |
|---|---|---|
| Compose project | `agent_negotiation` | `name:` in `compose.yaml` |
| Volume | `agent_negotiation_postgres_data` | explicit `name:` under `volumes:`, so the volume is not a generic key under a project prefix |
| Database and role | `agent_negotiation` | `POSTGRES_DB`, `POSTGRES_USER` |
| Published host port | `${POSTGRES_PORT:-55432}` | `ports:` |

The container port stays 5432 and is not configurable. Everything inside the Compose network reaches the database at `postgres:5432` — the service name and the container port. `POSTGRES_PORT` moves the published host side only, and no application code reads it, so a developer may use 5432, 55432 or any free port without a code change. The same host-port versus service-name rule applies to Anvil (`anvil:8545`).
**Rationale:** A failure to connect is a good failure; connecting to the wrong database is a bad one, and the default port under a default name is how the second happens. Naming the database and role for the project rather than `postgres` means a stray connection to the wrong server is refused rather than served. Binding the host port to a non-default value by default makes the documented start-up command work on a machine that already runs PostgreSQL, which is most machines.
**Consequences:** `infra/.env.example` carries a host URL and a container URL and says which caller uses which; [runbook.md](runbook.md) section 2 documents the distinction, how to change the host port, and what the port-conflict error means. `make reset-db` destroys `agent_negotiation_postgres_data` and nothing else. Renaming the Compose project, the database or the role on an existing installation orphans the old volume rather than renaming it, so a rename is a reset and the runbook says so.

## ADR-037: The exchange constructor rejects a same-token deployment
**Status:** accepted (Q17, answered by the product owner, 24 September 2026)
**Context:** The stage 1 adversarial review found that `NegotiationExchange`'s constructor checked only that its three addresses were non-zero. Nothing stopped a deployment that passed the same token as both legs. Such a session would settle by transferring `quoteAmount` from buyer to seller and `baseAmount` back in the same token — a net payment at a price neither party signed — and the seven events would record it as an ordinary settlement. Three of the review's four lenses flagged it independently. It was not fixed on the spot because [protocol.md](protocol.md) section 2 stated no constructor precondition and section 8.3 defined no error for it, so adding a guard meant adding a name to the protocol's error table, which needs a decision.
**Decision:** Add the guard. `baseToken == quoteToken` reverts with a new error `InvalidTokenPair()`. [protocol.md](protocol.md) sections 2 and 8.3, `INegotiationExchange`, the constructor, one unit test and one fuzz property change together.
**Rationale:** The cost is one comparison and one error name. The failure it prevents is silent and looks like a successful run in the evidence, which is the failure mode this project is least willing to accept ([architecture.md](architecture.md) goal 4). The counter-argument was real and was weighed: the deploy script controls both addresses, the stage 2 setup validator compares them against the manifest, and A17 would catch a mis-wired deployment before any run. But each of those is a procedure, and the guard is structural — the preference stated in CLAUDE.md is for the structural one.
**Consequences:** The error table has 23 entries rather than 22, and `packages/protocol/tests/test_abi.py` asserts all of them. This is not a protocol *version* bump: [protocol.md](protocol.md) section 15 bumps the version for a change to a type string, the `configHash` encoding, a reason code or an event field, and an added error is none of those. No digest changes and no signature made before the change becomes invalid. The reconstruction tool catches the same mis-wiring after the fact, from the other direction: a manifest whose token pair does not match the chain fails the `configHash` recomputation, and there is a test for that.

## ADR-038: The deployment manifest, and what it can honestly claim
**Status:** accepted (directed by the product owner, 24 September 2026)
**Context:** Stage 1 owed a deployment script that writes the manifest. [data_model.md](data_model.md) section 3.2 and [api_contract.md](api_contract.md) section 2.1 describe the manifest's contents, but three questions were open once it came to writing one: where a manifest lives and whether it is committed, what a Solidity script can truthfully state about when a deployment happened, and how a consumer tells "no explorer" from "field not written".
**Decision:** Four parts.

| | |
|---|---|
| Location | `docs/deployments/<deployment_id>.json`, written by `contracts/script/Deploy.s.sol` |
| Committed | Sepolia manifests yes; local ones git-ignored by `docs/deployments/local-*.json` |
| Time and block | `deployed_at_ts` (chain seconds) and `start_block`, replacing a formatted `deployed_at` |
| Absent values | Written as explicit JSON `null`, never omitted |

The schema is `packages/protocol/schemas/deployment_manifest.v1.json`, and the manifest carries its own `manifest_version` alongside `protocol_version`.
**Rationale, part by part.**

*Local manifests are not committed.* An Anvil chain lives as long as its container, so a committed local manifest is a record of addresses on a chain that no longer exists — evidence of nothing, and churn on every redeploy. Sepolia manifests are the ones an observer can check, and stage 5 commits them.

*`deployed_at_ts` rather than `deployed_at`.* Chain time is authoritative everywhere else in this system ([protocol.md](protocol.md) section 6). A manifest whose timestamp came from the clock of the machine that ran the script would be the single place it was not, and the discrepancy would be invisible. Recording integer chain seconds also removes a date-formatting routine from Solidity, which would have needed its own tests to earn any trust. The backend renders its `TIMESTAMPTZ` column from this value, so the API response shape does not change.

*`start_block` rather than `deployed_at_block`.* A `forge script` simulates against the current head and broadcasts afterwards, so `block.number` inside the script is the head *before* the deployment transactions land. Naming the field `deployed_at_block` would have claimed a precision it does not have: measured on a fresh Anvil, it reads 0 while all three contracts land in the next block. A lower bound is exactly what a log scan needs, and it is the only figure the script can state honestly, so the field is named for that.

*Explicit nulls.* A consumer that reads a missing key as null cannot distinguish "this chain has no explorer" from "an older script did not write this field". The first is a fact about the deployment and belongs in the file.
**Two preconditions, both checked before anything is broadcast** (added after the stage 1 adversarial review). The script refuses a `DEPLOYMENT_ID` that would not satisfy the schema's own `^[a-z0-9]+(-[a-z0-9]+)*$` pattern, and refuses to write over an existing manifest unless `MANIFEST_OVERWRITE=true` says so. Both were failures of the same kind: a deployment that lands on chain and then cannot be recorded, or that silently replaces the record of an earlier one. Checking after the fact is no use, because the gas is already spent and, on Sepolia, the manifest being overwritten is committed evidence.

A third case cannot be prevented from inside the script and so is documented instead: `_writeManifest` runs during simulation, before Foundry broadcasts, so **a failed broadcast leaves a manifest describing a deployment that never landed**. The script says so on its own output, and [runbook.md](runbook.md) section 4 says to delete the file.

**Consequences:** `contracts/foundry.toml` grants write access to `../docs/deployments` and read access to `../packages/protocol/fixtures`. The reconstruction tool and the stage 2 indexer take their log-scan start from `start_block`. `vm.serializeJson` cannot be used to compose the manifest — it *sets* an object's contents rather than adding to them, which silently reduced the first version of the file to a single key — so nested objects and nulls are written by key with `vm.writeJson(value, path, ".key")`. The integration test that deploys for real is what caught that, and it is why the reconstruction tests drive a real chain rather than a fake.

## ADR-039: Per-run participant keys are derived inside the agent service
**Status:** accepted (Q18, answered by the product owner, 25 September 2026)
**Context:** Spec section 8 and PRD FR-S6 require fresh participant wallets for every run, and [data_model.md](data_model.md) section 3.5 says an address is never reused. ADR-023 reached participant keys through `env:` and `keystore:` references to fixed variables and files, "generated per run by a script". Taken literally that is a manual step and a restart of both agents before every run, which the stage 6 batches — hundreds of runs — cannot take; and it leaves the backend unable to learn a wallet's address, which it must store and fund, without holding the key.
**Decision:** Each agent instance holds one **root secret**, reached through the existing reference forms: `env:BUYER_ROOT_KEY` locally, `keystore:/run/secrets/buyer-root.json` on Sepolia. At provisioning the agent derives that run's signing key and reports only its address:

```text
candidate(c) = HMAC-SHA256(key = root, msg = abi.encode(
    string  "agent-negotiation-sandbox/participant-key/v1",
    uint256 chainId,
    string  role,          // "buyer" or "seller"
    bytes16 runId,         // the run's UUID, as 16 raw bytes
    uint8   c))
key = candidate(c) for the first c = 0, 1, 2, … with 0 < candidate < the secp256k1 group order
```

The derivation is domain-separated by environment, role and run, as the product owner required, so that neither a buyer and seller key nor a local and Sepolia key can collide — even when one root is configured for both roles or both profiles by mistake. The environment is the **chain ID**, not a profile label: it is what distinguishes local (31337) from Sepolia (11155111), and the setup validator checks it against the chain rather than trusting configuration. The label versions the scheme, so a change to it is a new label and never a silent change of every address. The root never signs anything itself.

The database stores the derived address and the derivation metadata — the root's reference, the scheme label and its public inputs — and never a key of either kind.
**Rationale:** It is the only option put to the product owner that meets all four requirements at once: a fresh wallet every run with no manual step, which the batches need; keys that never leave the agent process; a run that survives an agent restart, because re-provisioning re-derives the same key (the mitigation [architecture.md](architecture.md) section 11 already relies on); and no per-run key stored anywhere. An ephemeral key generated at provisioning fails the third, and regenerating keys by hand fails the first. HMAC-SHA256 is a standard pseudo-random function (RFC 2104) and the message is `abi.encode`, which is already how this project hashes structured data, so no new primitive is introduced. The out-of-range retry is the same rejection-sampling rule BIP-32 uses, and at a probability near 2^-128 it exists to make the function total rather than because it will run.
**Consequences:** [api_contract.md](api_contract.md) section 6: provision carries the root's `key_ref`, which the agent refuses unless it is its own configured root, and an optional `expected_address` that a re-provisioning must reproduce; the response returns the derived `my_address`. `wallets.key_ref` holds the root's reference, `wallets.key_derivation` the scheme and its inputs, and `wallets.address` is unique across the whole table, so that "never reused" is a database error rather than a convention. `generate_keys.py` and `infra/.env.example` name the variables `BUYER_ROOT_KEY` and `SELLER_ROOT_KEY` from stage 2.2, when the key holder that reads them lands. Losing a root means losing the ability to sign for every open run derived from it; the recovery is an operator abort, which needs no participant key, and the runbook says so. Relay and operator keys are unchanged: one of each per deployment.

## ADR-040: A participant's setup approval is built and signed by its own agent service
**Status:** accepted (Q19, answered by the product owner, 25 September 2026)
**Context:** Setup needs each participant wallet to sign an ERC-20 `approve` of the exchange — up to 250 mUSD for the buyer and 25 mASSET for the seller (spec section 8) — and [architecture.md](architecture.md) section 5.1 has the backend broadcasting these "participant-signed setup txs". A participant key never leaves its agent process, and the internal API had no endpoint through which the backend could obtain that signature.
**Decision:** `POST /internal/runs/{run_id}/setup-approval`. The agent builds the transaction itself from provisioned state: the token is its role's (quote for the buyer, base for the seller), the spender is the provisioned exchange, the amount is the provisioned `allowance_minor`, and the chain ID is the provisioned chain's. The caller supplies only the nonce, the gas limit and the fee caps. The agent bounds the gas limit, signs with the run's derived key, and returns the raw transaction and its hash; the backend persists it in `tx_outbox` as kind `approve` before broadcasting it, like every other transaction.
**Rationale:** The alternative put to the product owner — the backend loading participant keys for setup only — breaks the one boundary [security_and_trust_boundaries.md](security_and_trust_boundaries.md) section 7 states without exception: a signing key stays inside its agent process. The endpoint also keeps the rule that governs typed messages for transactions: the agent never signs anything whose target, calldata or value came from its caller.
**Consequences:** [api_contract.md](api_contract.md) section 6 gains the endpoint and architecture 5.1 shows the call. The agent encodes an ERC-20 `approve` with `eth-abi` and signs with `eth-account`, which it already has; it still gains no RPC client ([ADR-035](#adr-035-web3-is-an-optional-extra-of-the-protocol-package)). `wallets.setup_nonce_next` tracks the participant's nonce, which for a freshly derived wallet starts at 0.

## ADR-041: The agent internal API's HMAC binds the method, the path and the body
**Status:** accepted (Q21, answered by the product owner, 30 September 2026)
**Context:** [api_contract.md](api_contract.md) section 6 specified `X-Agent-Auth` as the HMAC-SHA256 of the request *body*. Building the internal API in stage 2.2 exposed what that leaves open. `GET /internal/health` and `POST /internal/runs/{run_id}/release` both have empty bodies, so they carry the same MAC: anyone who observed one health check on the container network could release any run, discarding that agent's mandate and signer in the middle of a negotiation. A provisioning body carries no run id, so a captured one could also be replayed against a different run's path. The threat is the one [security_and_trust_boundaries.md](security_and_trust_boundaries.md) section 5 names, "forged observation to an agent", and the control it names, the HMAC, did not cover it.
**Decision:** `X-Agent-Auth` is the lowercase-hex HMAC-SHA256, under the instance's shared secret, of `METHOD`, a newline, the URL path, a newline and the raw body. The layout lives once, in `negotiation_protocol.agent_auth`, which the agent verifies with and the backend's `agent_client` will sign with, so the two cannot drift. The agent authenticates every request before it parses the path or the body, compares in constant time, and requires `X-Request-Id` after authentication. A shared secret shorter than 32 characters stops the instance starting.
**Rationale:** A MAC is only as specific as what it covers. Binding the route and the run id makes a MAC valid for one request shape, which is the property the control was assumed to have. Replay of an *identical* request is left alone deliberately: every agent endpoint is idempotent for an identical request, so replaying one returns what the backend already received.
**Consequences:** api_contract section 6 and security section 5 describe the new layout. The agent's tests show a health-check MAC refused on `release`, a provisioning MAC refused on another run's path and a tampered body refused. A nonce or timestamp against replay is not added; it would need state the idempotent design does not otherwise require.

## ADR-042: The setup approval's gas limit is bounded at 100,000 by default
**Status:** accepted (Q22, answered by the product owner, 30 September 2026)
**Context:** ADR-040 has the agent refuse "a gas limit above the agent's bound" on `setup-approval`, and nothing named the bound. An OpenZeppelin ERC-20 `approve` costs about 46,000 gas the first time an allowance is set; the contract's example request uses 70,000.
**Decision:** `AGENT_SETUP_GAS_LIMIT_MAX`, default 100,000, in each agent's settings. A request above it is `422 validation_error` naming `gas_limit`, and nothing is signed. A priority fee above the fee cap is refused the same way.
**Rationale:** Roughly twice the real cost leaves room for a token implementation that costs a little more. A setting rather than a constant, so an operator who meets a costlier token raises it without a code change.
**Consequences:** api_contract section 6, `infra/.env.example`, runbook section 3.
**Correction (ADR-047, 30 September 2026):** the first version of this entry said the bound stopped the caller making the agent sign a transaction that burns its test ETH. It did not: with the fee cap unbounded, a 100,000-gas transaction could still cost any amount. The adversarial review of stage 2.2 found it; ADR-047 bounds the cost itself.

## ADR-043: The deterministic policy names its holdings when they are what stops it
**Status:** accepted (Q23, answered by the product owner, 30 September 2026)
**Context:** Spec 11.1 says the deterministic policy "closes if it cannot legally accept or offer". [protocol.md](protocol.md) section 13 names `no_further_concession` for "offering is illegal" and `terms_unacceptable` for an incoming offer outside bound with no opportunities left. Neither case covers a policy whose own balance or inventory floor rules out the move it would make, and section 10 defines `inventory_constraint` as "agent cites its inventory floor or balance". Neither default scenario reaches the case; a generated population in stage 6 can.
**Decision:** The deterministic policy asks the `MandateValidator` whether each move it would make is legal: first accepting the active offer, then its scheduled offer. When either is refused for `insufficient_balance` or `inventory_floor` and no other move is left, it walks away with `inventory_constraint`. Otherwise section 13's rule stands: `terms_unacceptable` when an offer from the counterparty stands outside its bound and it has no opportunity left, and `no_further_concession` in every other case.
**Rationale:** Keeping failure kinds distinguishable (architecture goal 4). In the evaluation, a mandate that cannot trade at any price is a different finding from a price impasse, and a single reason for both would hide it. Consulting the validator rather than re-implementing its rules means the baseline and the gate that signs agree on legality by construction, so the baseline never needs a repair.
**Consequences:** protocol section 13 amended. The unit tests cover a seller below its floor facing an acceptable offer, a buyer who can afford neither the offer on the table nor its own next offer, and the case where no opportunity remains and the price, not the floor, is what refuses.

## ADR-044: Session approval checks an expiry window against the opening block's time
**Status:** accepted (Q24, answered by the product owner, 30 September 2026)
**Context:** [api_contract.md](api_contract.md) section 6 has `approve-session` check the session's "expiry window against the provisioned expectation", and the agent has no chain access, so it cannot know when the session opened. The operator computes `expiresAt` from the chain head before its `createSession` lands, so the opening block's timestamp is at or after the one the operator read: an exact equality with `session_duration_s` would fail on any chain that does not mine instantly.
**Decision:** The backend sends `opened_at_ts`, the timestamp of the block that emitted `SessionOpened`, with the event's fields. The agent requires `opened_at_ts < expires_at_ts <= opened_at_ts + session_duration_s`: the session may be shorter than provisioned by the inclusion delay, never longer, and never already expired when opened.
**Rationale:** It is the only form of the documented check the agent can make from what it is told, and it catches the failure that matters: a session left open longer than the mandate's owner provisioned for.
**Consequences:** api_contract section 6 lists the approve-session body with `opened_at_ts` and names `expires_at_ts` as a differing field when the window fails. Chain time remains authoritative; the agent trusts the backend for the block timestamp exactly as it trusts it for the rest of the observation.

## ADR-045: A buyer's inventory floor is capital
**Status:** accepted (Q25, answered by the product owner, 30 September 2026)
**Context:** `min_remaining_inventory_minor` was defined as the base-token balance a party will not go below, for both parties. For a buyer that is close to meaningless — buying only adds base token — and the validator's first rule for it, "base after the trade must reach the floor", refused trades that raised the buyer's holding. The rule had been written into protocol 11.1 without the product owner, which the adversarial review of stage 2.2 flagged. The product owner's reading: inventory includes capital, and a capital floor is useful to have for the buyer even where a scenario does not use it.
**Decision:** A party's floor applies to the token it gives up. The seller keeps at least `min_remaining_inventory_minor` of the base token after delivering the base amount; the buyer keeps at least that much of the quote token after paying the quote amount. The validator refuses a trade that breaches it with `inventory_floor`; the deterministic policy, refused for it, walks away with `inventory_constraint` (ADR-043).
**Rationale:** One field, one meaning — "what I will not trade below" — for both sides, with no schema change. A buyer with 250 mUSD and a floor of 100 may pay at most 150 whatever its reservation price, which is how a budget is usually stated.
**Consequences:** `mandate.v1.json`'s field description, protocol 11.1, data_model 3.4. The default scenarios give the buyer a floor of 0, so nothing they do changes. The stage 6 evaluator's feasible interval must take the buyer's floor into account as well as its reservation price: the buyer's effective bound is the lower of its reservation price and its quote balance less its floor.

## ADR-046: The agent refuses a contradictory observation, and the controller retries
**Status:** accepted (Q26, answered by the product owner, 30 September 2026; Q1 of the same exchange fixed the order of history)
**Context:** The adversarial review of stage 2.2 found that the agent signed on whatever the observation said: an `expected_sequence` that did not follow the history, an `active_offer` that was not the last recorded offer, an offer digest that did not hash from the offer's own fields. Each would produce an action the contract reverts — an execution failure and an abort for what was a bookkeeping error in our own backend. It also found that the turn rule read the most recent offer from the *list order* of `history`, which protocol 12 did not fix.
**Decision:** Four parts.
1. `history` is in ascending sequence order, beginning at 1 (protocol 12).
2. An expired offer does not stand, so `active_offer` is null once chain time reaches its `validUntil`; the offer stays in history marked `expired` (protocol 12, stating what the schema's description already implied).
3. Before any policy is asked, the agent checks the observation against itself and the approved session — ascending, contiguous, alternating offers; every offer field present; each offer digest recomputed from the offer's fields under the approved session, which binds the hash a policy may accept to the amount the mandate was checked against; statuses; `expected_sequence`; `offers_remaining_for_me`; `active_offer` — and refuses a contradiction with `422 observation_inconsistent`, naming each one. Nothing is signed.
4. The controller (stage 2.4), on `observation_inconsistent`, rebuilds the observation from the chain and retries, up to five times; then it moves the run to `RECOVERY_REQUIRED`, an operational fault for the operator, not an abort.
**Rationale:** The product owner's first instinct was to point the contradiction out to the opposing agent and recover the negotiation. The observation is not the opponent's, though: our own backend builds it from the chain, and the agents never talk to each other. A contradiction is a backend bug or a stale read that the opponent cannot fix; the nearest form of "recover rather than fail" is the backend reading the chain again. Five retries absorbs a stale read; a contradiction that survives five fresh reads is a bug a person must look at, and aborting would record our bug on chain as a run outcome.
**Consequences:** api_contract 6 and 7 (a new 422 code), protocol 12, architecture 6.1 (a new path to `RECOVERY_REQUIRED`), build_plan stage 2.4. The exit test's backend stand-in already built consistent observations from chain state, including real digests, and it now has a run in which an offer expires.

## ADR-047: The setup approval's worst-case cost is bounded
**Status:** accepted (Q27, answered by the product owner, 30 September 2026)
**Context:** ADR-042 bounded the gas limit of a participant's setup `approve` and claimed that stopped the caller burning the wallet's test ETH. With the fee cap unbounded it did not.
**Decision:** The agent also refuses a request whose gas limit times `max_fee_per_gas_wei` exceeds `AGENT_SETUP_MAX_COST_WEI`, default 10^16 wei (0.01 ETH — 100 gwei at the full 100,000-gas bound), with `422 validation_error` naming `max_fee_per_gas_wei`.
**Rationale:** The cost itself is the quantity that matters, and bounding it directly needs no assumption about which of gas and price will be high. 100 gwei is far above normal Sepolia or Anvil fees, so the bound never refuses an ordinary request.
**Consequences:** api_contract 6, `infra/.env.example`, runbook 3. ADR-042's rationale is corrected in place.

## ADR-048: Release ends a turn in flight; restart recovery re-provisions and re-approves
**Status:** accepted (Q28, answered by the product owner, 30 September 2026)
**Context:** `release` is the backend's last call to an agent for a run that has ended on chain. A turn already deciding when it arrived — up to 45 seconds with a model from stage 3 — still signed when its decision came back. Separately, a restarted agent has forgotten every run, and the documents said only that the controller re-provisions it; a turn then also needs the session approved again, from data the database does not store as such.
**Decision:** `release` marks the run released at once. A turn in flight checks again after its decision and before signing, and if the run was released it returns `409 invalid_state` with `state = "released"` and signs nothing. After an agent restart, the controller re-provisions the run with its stored address as `expected_address`, re-sends approve-session built from the canonical `SessionOpened` row in `chain_events` and the timestamp of that block read from the chain, and then retries the turn.
**Rationale:** A signature made after the run was released is harmless on chain — the contract refuses every action on a finished session — but it is a signature the agent made for a run it had been told was over, and the evidence should not contain one. Restart recovery needs no new column: the event is canonical evidence already, and its block's timestamp is one RPC read.
**Consequences:** api_contract 6, architecture 11, runbook 3, build_plan stage 2.4.

## ADR-049: A key reference has a grammar
**Status:** accepted (a defect fix from the adversarial review of stage 2.2, 30 September 2026)
**Context:** The agent accepted anything after `env:` or `keystore:` as a reference. A root pasted after the prefix — `env:BUYER_ROOT_KEY=0x…`, which `generate_keys.py`'s two adjacent output lines invite — passed as a reference, failed to resolve, and was printed in the error, the start-up log line and every provisioning refusal. Everything around a reference treats it as safe to log, store and return, which is only true if a secret cannot pass for one.
**Decision:** A key reference is `env:NAME`, where `NAME` is an upper-case environment variable name of at most 64 characters, or `keystore:/path.json`, an absolute path to a JSON file without `..`, and neither form may contain a run of 32 or more hexadecimal characters. `AGENT_ROOT_KEY_REF` and provisioning's `key_ref` are refused otherwise, and the refusal does not repeat the value.
**Rationale:** The check is structural, at the boundary, rather than a promise that every message downstream avoids the value.
**Consequences:** api_contract 6, runbook 3, security 5 and 7. `wallets.key_ref`'s database CHECK (stage 2.1) still accepts any `env:` or `keystore:` text; the backend sends only references the agent accepted, and tightening the CHECK is a migration for the backend's stage.
