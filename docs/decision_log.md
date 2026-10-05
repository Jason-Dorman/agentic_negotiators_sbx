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
**Amendment (stage 2.3, 30 September 2026):** the grammar and the resolver moved from the agent service to `negotiation_protocol.key_refs` when the backend became the second service to hold keys by reference — `RELAY_KEY_REF` and `OPERATOR_KEY_REF`. The agent's `agent.keys.references` re-exports it, so the two services cannot disagree about what a reference is.

## ADR-050: The relay replaces a stuck transaction automatically, within a fee ceiling
**Status:** accepted (Q29, answered by the product owner, 30 September 2026)
**Context:** Stage 2.3's relay owed "gas replacement linked by `replaces_id`" ([build_plan.md](build_plan.md)), and [data_model.md](data_model.md) section 3.10 already had the column and the rule that an original is marked `replaced` before its successor is inserted. Nothing said when a replacement happens, by how much the fee rises, or where it stops.
**Decision:** A transaction the relay or the operator key signed that is still not included `RELAY_REPLACE_AFTER_BLOCKS` blocks (default 3) after its first broadcast is re-signed at the same nonce, with the same recipient, calldata and gas limit, and both fee caps raised by an eighth (12.5 percent, rounded up) — above the 10 percent a node requires before it replaces a pooled transaction. No fee cap the relay signs is ever above `RELAY_MAX_FEE_PER_GAS_WEI` (default 100 gwei). A bump the ceiling clips is used only while it still clears the node's 10 percent; below that the relay stops replacing and keeps waiting. Rebroadcast of the stored bytes stays a separate, unconditional recovery step. A transaction an agent signed — its setup approval ([ADR-040](#adr-040-a-participants-setup-approval-is-built-and-signed-by-its-own-agent-service)) — is never replaced: the relay holds no key that could re-sign it.
**Rationale:** A stuck transaction on Sepolia stalls a turn while offers and the session keep expiring; an operator-only procedure would make every congestion spike a manual intervention during a live demonstration. A ceiling keeps the automatic path from spending without bound, and stopping at it, rather than failing, leaves the decision to the operator and the runbook.
**Consequences:** `tx_outbox.submitted_block` (migration 0002), so the count of blocks survives a restart rather than starting again; `RelayPolicy` and the `RELAY_*` settings in `infra/.env.example`; runbook section 6. The replaced original is still polled for a receipt, because it can be the one mined: if it is, it becomes `included` and its successor `dropped`.

## ADR-051: An execution failure is a timeline entry, with its sentence stored on the outbox row
**Status:** accepted (Q30, answered by the product owner, 30 September 2026)
**Context:** [api_contract.md](api_contract.md) section 4 gives an execution failure a sentence — "Transaction reverted: SequenceMismatch. No trade occurred." — and [ADR-024](#adr-024-timeline-sentences-rendered-server-side-once) requires every sentence to be rendered once and stored. A reverted transaction emits no event, so it has no `chain_events` row to store one on, and the export schema's `timelineKind` had no kind for it.
**Decision:** `execution_failure` is a seventh timeline kind. Its sentence is rendered once, when the indexer records the receipt with status 0, and stored in a new nullable `tx_outbox.sentence`; the decoded error name is `tx_outbox.last_error` and, for a signed action, `signed_actions.revert_error`. The entry's actor is the signer of the action it carried, or `operator` or `anyone` for a lifecycle transaction, and it is ordered after every event of the block the transaction was mined in.
**Rationale:** Spec section 7.6 says a reverted transaction "must be shown as an execution failure". As an ordinary timeline entry, it appears in the live view, replay and export by the same path as everything else and cannot be told differently in each.
**Consequences:** Migration 0002; `export.v1.json`'s `timelineKind` gains `execution_failure` (an additive enum value; export version unchanged); data_model 3.10, api_contract 2.2 and 4.

## ADR-052: The controller records the economic outcome; the indexer and the projection supply it
**Status:** accepted (Q31, answered by the product owner, 30 September 2026)
**Context:** [data_model.md](data_model.md) section 5 said `outcome_kind` is "set only by the indexer". A check constraint couples the outcome to the run's state — an outcome needs `terminal` or `failed_setup` — and the run state machine of [architecture.md](architecture.md) section 6.1 is the controller's, built in stage 2.4. An indexer that wrote the outcome would also have to choose between `terminal` and `failed_setup`, and handle a run in `recovery_required`, which is state-machine logic in the wrong module.
**Decision:** Stage 2.3 builds the pieces and writes no outcome. The indexer confirms a terminal event at the run's confirmation threshold, snapshots the balances at its block, and for a settlement verifies the receipt (architecture 5.3), reporting each in its poll report. The projection derives the `Outcome` from canonical events at the threshold. The controller, in stage 2.4, records it with the state it chooses.
**Rationale:** The property that matters — an outcome only ever comes from a canonical terminal event at the threshold — is unchanged; what changes is which module writes the row, and keeping state transitions in one module is what makes the state machine testable as one.
**Consequences:** data_model section 5 and invariant 7 reworded; build_plan stage 2.4 gains the obligation.

## ADR-053: The indexer re-checks block hashes until the RPC's finalized head covers them
**Status:** accepted (Q32, answered by the product owner, 30 September 2026)
**Context:** [architecture.md](architecture.md) section 5.5 has the indexer check "stored block hashes for recent events", and nothing defined "recent". Anvil's finalized head trails its head by 64 blocks; Sepolia's by about two epochs, around 13 minutes.
**Decision:** Every canonical event and every recorded inclusion above the RPC's finalized head is re-checked on every poll, for runs not yet `terminal` — a terminal run is no longer watched, as [Q20](open_questions.md)'s interim answer has it until stage 5. The log scan likewise re-reads everything above the finalized head on every poll, so a reorg that removed nothing the backend had stored, and so is invisible to the hash check, still has its new logs read.
**Rationale:** The finalized head is the chain's own statement of what can no longer change, which a fixed depth only approximates; spec 9.3 already ties "finalized" to it.
**Consequences:** architecture 5.5; the stage 2.3 reorg tests run at threshold 2 on Anvil. On Sepolia a poll re-reads roughly 64 to 100 blocks of the exchange's logs in one `eth_getLogs`.
**Correction (stage 2.3 review, 1 October 2026):** this entry first said the re-read was "well inside a free-tier budget". It is not: Alchemy's free tier caps `eth_getLogs` at 10 blocks, so with the default `INDEXER_LOG_CHUNK_BLOCKS` of 2,000 nothing would be indexed on it. Pay As You Go allows the range. The cost there, at $0.525 per million compute units (Alchemy's PAYG FAQ, 1 October 2026), is about 220 compute units a poll — two block reads, one per stored height above the finalized head, one `eth_getLogs`, the receipts — so about $0.10 per hour of polling at 4 s, and about $75 a month only if polling never stopped. Which plan Sepolia runs on, and with what usage limit, is [Q40](open_questions.md).
**Amendment (stage 2.4, 1 October 2026):** a run in `failed_setup` is no longer watched either. Its session never opened, or was aborted and its outcome recorded ([ADR-066](#adr-066-a-session-an-agent-refuses-during-setup-is-aborted-with-execution_failure)), and watching it would repeat its terminal event on every poll for good. `RunRepository.with_open_sessions` excludes both states.

## ADR-054: An action the node predicts will revert is still broadcast, at a fallback gas limit
**Status:** accepted (Q33, answered by the product owner, 30 September 2026)
**Context:** The relay estimates gas before it signs. An estimate can predict a revert — a stale sequence, an expired offer, a missing allowance — and the relay had to do something with an action whose signed intent was already persisted.
**Decision:** The gas limit is the estimate plus a quarter. When the estimate predicts a revert, the relay persists and broadcasts the transaction anyway, at `RELAY_FALLBACK_GAS_LIMIT` (default 500,000). The contract decides; the revert lands on chain with a receipt, is decoded to its protocol error, and is shown as an execution failure ([ADR-051](#adr-051-an-execution-failure-is-a-timeline-entry-with-its-sentence-stored-on-the-outbox-row)).
**Rationale:** An estimate is a prediction against pending state, which can be wrong in a race; refusing on it would record a failure that exists only in our database and rests on a guess. The relay does not judge an action — the contract is the gate — and a receipt is evidence an observer can check. The cost is relay test ETH.
**Consequences:** `RelayPolicy.fallback_gas_limit`, `RELAY_FALLBACK_GAS_LIMIT`; the stage 2.3 test of a self-acceptance asserts the fallback limit, the status-0 receipt, the decoded `SelfAcceptance` and the timeline entry.

## ADR-055: An invalidated row seen again in the same block is made canonical again; a rewind invalidates by block hash
**Status:** accepted (Q34, answered by the product owner, 1 October 2026)
**Context:** The adversarial review of stage 2.3 found that a `chain_events` row, once invalidated, could never be canonical again. The unique key is `(block_hash, tx_hash, log_index)` and the insert did nothing on conflict, so when the same log was read again in the same, unchanged block — after a reorg that flipped back to the original fork, or after a rewind that should not have happened — the insert collided with the invalidated row and the evidence was lost for good. The same held for balance snapshots. And a rewind invalidated everything from a height up rather than only what the chain no longer had.
**Decision:** A row that comes back under the same key — the same log in the same block — is restored: `canonical` set true and `invalidated_at` cleared, with its depth updated, rather than duplicated or dropped. The same for a balance snapshot of the same block. A rewind marks non-canonical exactly the rows whose block hash the chain no longer has at their height; a height above the head has lost every block stored at it.
**Rationale:** The key already identifies a log in a block; a second row for it would mean two rows for one fact. `canonical` and `invalidated_at` are not evidence columns, so the immutability trigger already allows the change, and the CHECK that couples them holds. Invalidating by hash rather than by height is the same thing on a real reorg — every descendant of a replaced block is replaced too — and is right when a rewind is wrong.
**Consequences:** data_model 3.11 and 3.12; `ChainEventRepository.invalidate_blocks`, `BalanceSnapshotRepository.invalidate_blocks`; the restored rows appear in a poll report's `indexed`. Tested by invalidating a run's rows by hand and polling.

## ADR-056: A revert is replayed at its inclusion block, then its parent, and is `Undetermined` when neither reproduces it
**Status:** accepted (Q35, answered by the product owner, 1 October 2026)
**Context:** The indexer replayed a reverted transaction against its parent block to read the revert data. A deadline revert — `OfferExpired`, `SessionDeadlinePassed`, the likeliest revert on Sepolia — depends on the inclusion block's timestamp, so it did not reproduce at the parent and was recorded, permanently, as `NoRevertData`.
**Decision:** Replay first at the inclusion block, whose timestamp the transaction ran at, then at its parent. A replay that reverts gives the protocol error (or `NoRevertData` when it reverts with no data). When neither replay reverts, the error is recorded as `Undetermined`, never a guess.
**Rationale:** Anvil and geth run an `eth_call` at block N in block N's context, so the timestamp-dependent case reproduces there. `debug_traceTransaction` would be exact but is not offered by every provider, and the evidence should not depend on a premium method.
**Consequences:** `UNDETERMINED_REVERT`; runbook 6. A replay at the inclusion block sees the state after every transaction in that block, so a revert caused by a transaction later in the same block can be misnamed; with one action pending per session, this backend does not produce that case.

## ADR-057: Recovery reports a resend the node refused as `refused`, with the reason, logged
**Status:** accepted (Q36, answered by the product owner, 1 October 2026)
**Context:** The review found that `reconcile` reported `rebroadcast` whenever it tried to resend, including when the RPC timed out or the node refused the bytes — out of gas money, a wrong chain — so the controller could never tell a resend that happened from one that did not.
**Decision:** A seventh recovery outcome, `refused`: neither mined nor pooled, nonce free, and the node refused the stored bytes. The result carries the node's reason, the row's `last_error` keeps it, and the relay logs it (`relay.broadcast_refused`, with the run, row and hash). An unanswered resend is `unreachable`. `rebroadcast` now means the node accepted the bytes. The relay also refuses, before persisting anything, a pre-signed transaction for another chain or bytes it cannot read.
**Rationale:** Failure kinds stay distinguishable (architecture goal 4). Sending refused bytes again will not help; a person has to fund the relay or look at the chain.
**Consequences:** runbook 6's outcome table; build_plan stage 2.4: `refused` moves the run to `RECOVERY_REQUIRED`, like `nonce_conflict` and `unreachable`.

## ADR-058: A reorg is written as a run event when it is found; a terminal event is reported on every poll
**Status:** accepted (Q37, answered by the product owner, 1 October 2026)
**Context:** The indexer reported a reorg and a confirmed terminal event once, in memory, in its poll report. A process that died after the rewind committed, or a controller that missed one report, lost the signal for good — and both are what stage 2.4 acts on.
**Decision:** Two different mechanisms, because the two signals differ. A reorg cannot be read again from the chain afterwards, so the indexer appends a `chain.reorg` run event — `{ from_block, to_block, invalidated_digests }`, the SSE shape of api_contract 3 — for each affected run, in the same transaction as the rewind. A terminal event can be, so it is reported on every poll while its run is watched and at its threshold, its balance snapshots taken idempotently and its settlement check recomputed: the report states the chain's state rather than delivering a message. No schema change.
**Rationale:** The run-event log is already durable, append-only and public, and the reorg's data is public; writing it in the rewind's own transaction means the two cannot disagree. A level-triggered terminal report needs no acknowledgement to be safe.
**Consequences:** The indexer writes the `chain.reorg` run event itself; the other run events stay the controller's (build_plan stage 2.4). `TerminalConfirmed` is repeated until the run is `terminal`, at the cost of one receipt lookup per poll for that run.

## ADR-059: An invalid confirmation threshold is refused and goes back to a person
**Status:** accepted (Q38, answered by the product owner, 1 October 2026)
**Context:** The indexer read `public_config.confirmation_threshold` and fell back to the default on a bad value, while the projection raised: the two disagreed about the same run.
**Decision:** One reading, `api.config.confirmation_threshold`, shared by both. Absent, the threshold is the deployment's `CONFIRMATION_THRESHOLD` (Q14). Present, it must be an integer of at least 1; anything else raises `InvalidRunConfigError`. The indexer then confirms nothing for that run and reports it as a `RunProblem`; the projection raises.
**Rationale:** Nothing should be recorded as confirmed under a threshold nobody chose. One run's bad configuration must not stop the indexer for the others.
**Consequences:** build_plan stage 2.4: the setup validator refuses such a run before it starts, and the controller moves a run with a `RunProblem` to `RECOVERY_REQUIRED`.

## ADR-060: web3's own retries are off; `RPC_TIMEOUT_S` is the real limit
**Status:** accepted (Q39, answered by the product owner, 1 October 2026)
**Context:** web3's HTTP provider retries some failures itself. The review measured a 10 s timeout becoming about 52 s, including for `eth_sendRawTransaction`.
**Decision:** The adapter constructs its provider with `exception_retry_configuration=None`. One call is one request, and `RPC_TIMEOUT_S` is how long it waits.
**Rationale:** The relay and the indexer recover by looking up before they act, and the controller schedules the next attempt; a hidden retry beneath them only hides how long an outage has lasted.
**Consequences:** A unit test counts the requests a failing call makes.

## ADR-061: A run's costs — gas, model and RPC — are recorded per run and surfaced in the UI
**Status:** accepted (requested by the product owner, 1 October 2026)
**Context:** The product owner wants the costs a negotiation incurs visible in the UI when reviewing a run: gas, model tokens, and RPC usage. Gas (`gas_used_*`, `fee_wei_*`) and model cost (`model_calls`, token counts, `model_cost_estimated_usd`, `model_cost_reported_usd`) were already in `run_metrics`, but FR-U5's metric strip named only model cost, and nothing anywhere counted RPC requests. An RPC provider reports no per-request cost in its responses — Alchemy bills compute units visible only on its dashboard — so a *reported* RPC cost does not exist the way a reported model cost does.
**Decision:** The chain adapter counts every RPC request it makes, by JSON-RPC method, attributed to the run holding the lease (requests outside any run, such as health checks, are counted in logs but belong to no run's metrics). Stage 2.5's metrics calculator stores `rpc_requests`, `rpc_requests_by_method` and `rpc_cost_estimated_usd` on `run_metrics` by migration, priced from an operator-maintained RPC price table beside the model price table, with its own `last_verified` date — zero-priced for the local chain, which really is free. There is no reported figure, so the UI shows the estimate labelled as an estimate and shows unknown as unknown, never zero, exactly as model cost already behaves (FR-A9). FR-U5's metric strip shows all three cost groups: model (estimated and reported), chain (gas and test-ETH fee), and RPC (request count and estimated USD).
**Rationale:** The project's rule for model cost — estimate conservatively from an operator-maintained table, keep estimated and reported separate, never display unknown as zero — extends to RPC cost without inventing precision: counts are exact and local, the USD figure is openly an estimate, and the authoritative bill stays with the provider.
**Consequences:** data_model 3.13 (stage 2.5 migration), api_contract 2.2 (`metrics` block gains `rpc_requests` and `rpc_cost_estimated_usd`; the SSE `metrics` event carries them automatically), architecture 3.2 and 8 and 11, PRD FR-U5 and section 8, build_plan stages 2.5 and 4, `export.v1.json`'s `metrics` object (additive) when stage 2.5 lands.
**As built (stage 2.5, 2 October 2026):** migration 0004 adds the three columns. The adapter counts each request as its HTTP provider encodes it, so one call that needs two requests counts two and a failed request counts too; the attribution is a context variable the driver sets while it ticks, reconciles or polls for a run, and the counts are added to `run_metrics` as each finishes. Only `add_rpc_requests` writes them, so a recomputation never overwrites requests counted meanwhile. The price table is ADR-075's. `export.v1.json` gains the three fields in `metrics` and `rpc_requests` and `rpc_cost_estimated_usd` in the run's metric strip, with method names pinned to JSON-RPC namespaces so nothing but a count can ride in the map.

## ADR-062: Settlement waits for human approval — on-chain two-phase, after v0.1
**Status:** accepted as a post-v0.1 feature (decided by the product owner, 1 October 2026)
**Context:** In v0.1 the accepting agent's signed `Accept` settles atomically in one `acceptAndSettle` transaction the relay submits as soon as it is signed (FR-P5, protocol 5 rule 8). The product owner wants the agents to stop there: the agreed terms — the standing offer signed by its proposer and the acceptance signed by the counterparty — are submitted for human review, and settlement executes only on approval. Two designs were put to the product owner: hold the signed acceptance off-chain until approval (no contract change, but the approval window is bounded by the offer's `validUntil`), or change the contract so acceptance is recorded on-chain into a pending state and a second operator-approved transaction executes the transfers.
**Decision:** The on-chain two-phase design, built **after the current build finishes** — v0.1 continues exactly as specified, settles atomically on acceptance, and freezes protocol version 1. The feature is protocol version 2: a new pending-approval session status, a split of `acceptAndSettle` into an acceptance-recording call and an operator-approved settlement call, new events and errors, an EIP-712 domain version bump, and a new deployment. The product owner also decided the rejection semantics: **rejection resumes the negotiation** rather than ending the session.
**Rationale:** Recording the acceptance on-chain makes the pending agreement public evidence in its own right and frees the approval window from the offer lifetime; deferring it keeps the stage-1 contract frozen (build_plan working agreements) and keeps v0.1 finishable.
**Consequences:** PRD 4.2 gains the feature as deferred-but-wanted; build_plan gains a post-v0.1 note; protocol.md is untouched until the feature's own design begins. Design work the feature will need its own ADRs for: resumption must clear or invalidate the accepted offer, or the accepting agent — whose mandate still allows the price — simply re-accepts; the rejection has to reach both agents, which is an addition to the observation allowlist (protocol 12) and an observation schema bump; a resumed negotiation needs offer opportunities left, so a rejection with none remaining ends in close or expiry, not a new price; the accepted-but-unsettled state FR-P5 was written to exclude will exist and needs its own expiry and balance rules; and the review panel shows the run's cost metrics ([ADR-061](#adr-061-a-runs-costs--gas-model-and-rpc--are-recorded-per-run-and-surfaced-in-the-ui)) beside the terms. Whether approval is required on every run or per-run configurable interacts with [Q41](open_questions.md): batch runs cannot wait on a human.

## ADR-063: An RPC outage that outlasts `RPC_OUTAGE_LIMIT_S` is `RECOVERY_REQUIRED`, in setup as in negotiation
**Status:** accepted (Q42, answered by the product owner, 1 October 2026)
**Context:** Spec 9.4 and FR-E6 say an *unresolved* RPC outage is `RECOVERY_REQUIRED`, and ADR-060 made a single call's wait honest, but nothing said how long an outage lasts before it counts as unresolved. A poll that raises `RpcUnavailableError` is an outage, not a result (stage 2.3); one failed poll is not a reason to stop a run. Separately, [architecture.md](architecture.md) section 6.1 had no path from `PREPARING` to `RECOVERY_REQUIRED`: an outage during setup could only be `FAILED_SETUP`, although once `createSession` has been sent nobody can tell whether a session opened on chain.
**Decision:** `RPC_OUTAGE_LIMIT_S`, default 60. The controller retries at the poll interval, and an outage that lasts the whole limit — consecutive failures, measured by wall clock from the first — moves the run to `RECOVERY_REQUIRED` with cause `rpc_timeout`. The same applies during setup, through a new transition `PREPARING → RECOVERY_REQUIRED`; every setup step looks up what it already did before it acts, so the operator's resume carries setup on from where it stopped rather than starting again.
**Rationale:** Integrity over availability (NFR-1), without stopping a run for a hiccup. Sixty seconds rides out a provider's brief faults and is well inside a default 600-second offer lifetime. A setup outage after `createSession` is the case where `FAILED_SETUP` would be a guess.
**Consequences:** architecture 6.1 gains the transition; `api.config` and `infra/.env.example` gain the setting; runbook 6.

## ADR-064: An unreachable agent is retried within the outage limit; an unexpected refusal is `RECOVERY_REQUIRED` at once
**Status:** accepted (Q43, answered by the product owner, 1 October 2026)
**Context:** The architecture names model failure (an abort with reason 2) and an inconsistent observation (five retries, then `RECOVERY_REQUIRED`, ADR-046), but not an agent service that does not answer — refused connection, timeout, a `503` from a signer that did not load — nor one that refuses a request in a way the backend should never provoke: `session_mismatch`, `validation_error` or `idempotency_conflict` on a turn.
**Decision:** An unreachable agent is retried with the same turn number and the same stored observation, which the agent's idempotency makes safe (api_contract 6), until `RPC_OUTAGE_LIMIT_S` (ADR-063) has passed; then the run moves to `RECOVERY_REQUIRED` with cause `agent_unavailable`. An unexpected refusal is a backend defect, so the run moves to `RECOVERY_REQUIRED` at once with the refusal's code as its cause. Neither is an abort.
**Rationale:** ADR-046's reasoning carries over: aborting would record our own fault on chain as the run's outcome, and the operator can still abort from `RECOVERY_REQUIRED`. Reusing one limit keeps the configuration small.
**Consequences:** architecture 6.1's `RECOVERY_REQUIRED` causes; runbook 6.
**As built (stage 2.4):** an answer the backend can check and finds wrong is treated as a refusal it should never see, with the same result. A signed action whose signer, typed signer, sequence or session is not the turn's, whose digest does not recompute from its own fields, or whose signature does not recover to its signer is `agent_signed_action_mismatch`; decision records over an observation hash other than the one the backend stored — the agent decided on another mandate, most likely — are `agent_observation_hash_mismatch`. Nothing of either is kept; the turn is closed with the code.

## ADR-065: A participant wallet is funded with exactly its setup approval's worst-case fee
**Status:** accepted (Q44, answered by the product owner, 1 October 2026)
**Context:** Spec section 8 has the operator fund each fresh wallet with "enough test ETH for setup approvals", and nothing said how much. On Sepolia this is real test ETH from the operator's wallet, and whatever is left over stays in a per-run wallet nobody will use again.
**Decision:** The controller first fixes the approval's gas limit — the node's estimate plus a quarter, never above `AGENT_SETUP_GAS_LIMIT_MAX`'s default of 100,000 — and its fee caps, from one fee quote under the relay's policy; it then funds the wallet with exactly `gas_limit × max_fee_per_gas`, and asks the agent for an approval with those same figures.
**Rationale:** It is the least that guarantees the approval can be included at the fees it was signed with: about 0.0002 ETH per wallet at three gwei on Sepolia, nothing on Anvil.
**Consequences:** `TxKind.FUND_ETH` transactions carry a value, so the relay's lifecycle submission takes one; runbook 7.

## ADR-066: A session an agent refuses during setup is aborted with `execution_failure`
**Status:** accepted (Q45, answered by the product owner, 1 October 2026)
**Context:** [data_model.md](data_model.md) section 3.3 anticipates a session opened during setup and then refused by an agent's `approve-session`: it is aborted and the run's setup failed. No abort code was named for it.
**Decision:** Reason 4, `execution_failure` — a required step that cannot proceed (protocol 10). The run ends `failed_setup` with outcome `aborted`, reason 4, once the abort is canonical at the threshold, and its `state_cause` is `session_refused`.
**Rationale:** The closest of the four codes, and the only one that does not claim a human pressed Abort. Finer causes live in the run record, as protocol 10 says.
**Consequences:** None outside the controller; data_model 3.3's existing constraint already allows `aborted` with `failed_setup`.

## ADR-067: Abort ends a run with no session as `failed_setup`, and frees the active run
**Status:** accepted (Q47, answered by the product owner, 2 October 2026)
**Context:** The adversarial review of stage 2.4 found that abort was refused while a run was `preparing` and did nothing for a `recovery_required` run whose setup had not opened a session, so a setup fault that persisted held the single active run for good — while the runbook called abort "always the way out".
**Decision:** Abort is allowed from `preparing` too. Where no session can exist — no `session_id`, or its `createSession` reverted or was dropped — the run moves at once to `failed_setup`, cause `abort_requested`, and the active run, the lease and both agents are released. Where a `createSession` is in flight, the termination is recorded and the session is aborted once it opens. A session aborted before setup finished (no `post_setup` snapshot) ends `failed_setup`, from `preparing` or from `recovery_required`; architecture 6.1 gains `RECOVERY_REQUIRED → FAILED_SETUP`.
**Rationale:** Abort must always be able to end a run; a run that never opened a session has nothing to abort on chain, and its setup failed.
**Consequences:** architecture 6.1, api_contract 2.2, runbook 6.

## ADR-068: A termination is recorded on the run before it is sent, and its cause is kept apart from the fault cause
**Status:** accepted (Q48, answered by the product owner, 2 October 2026)
**Context:** The review found that the decision to end a session early lived only in memory between closing a failed turn and persisting the abort: one RPC error there made the next tick ask the agent for a new decision, and a run that should have been aborted with reason 2 settled. It also found that a fault crossing a termination in flight overwrote the termination's cause.
**Decision:** `runs` gains `termination_cause TEXT NULL` and `termination_code SMALLINT NULL` (1–4, null for an expiry), written in the same unit of work as whatever decides the session must end: the turn closed as a model or execution failure, the operator's abort, the session refused during setup, the deadline reached. Only the driver sends the termination, and it acts on a recorded one before anything else, so it is sent after a crash or an outage as surely as before. When the outcome is recorded, the run's final `state_cause` is the termination cause where there is one.
**Rationale:** The intent to end a session is a decision with on-chain consequences; like a signed action, it is persisted before it is acted on.
**Consequences:** migration 0003; data_model 3.3; architecture 6.1; api_contract 2.2.

## ADR-069: `post_setup` is snapshotted at the `SessionOpened` block, once confirmed
**Status:** accepted (Q49, answered by the product owner, 2 October 2026)
**Context:** `post_setup` was taken at the head when setup finished — a block not yet at the confirmation threshold — so a one-block reorg could invalidate it and leave a healthy run without the balances every observation reads.
**Decision:** It is taken at the block that emitted `SessionOpened`, after that event is confirmed at the run's threshold. Every setup transaction is confirmed before `createSession` is sent, so that block's balances include all of them.
**Consequences:** data_model 3.12.

## ADR-070: A recorded termination stops decisions; resume and step are refused while one is in flight
**Status:** accepted (Q50, answered by the product owner, 2 October 2026)
**Context:** An abort arriving while an agent was deciding did not stop the decision from being signed into the run and relayed after `abortSession`; resume or step during an abort was accepted and erased the abort's cause.
**Decision:** A decision that comes back after a termination was recorded is kept as a private decision record and nothing else: no signed action, no transaction; its turn closes with `termination_requested`. The check is made under the run's row lock, in the unit of work that would persist the action, so an abort committed first always wins. Resume and step are `409 invalid_state` while a termination is recorded and the run has not ended.
**Consequences:** api_contract 2.2.

## ADR-071: A stored observation that has gone stale is not asked again; its turn closes and a fresh one is built
**Status:** accepted (Q51, answered by the product owner, 2 October 2026)
**Context:** ADR-064 re-asks an unreachable agent with the stored observation and turn number. After a long outage or a restart, that observation's active offer may have expired or its session's deadline passed, and a decision on it would revert on chain and record our own delay as an execution failure.
**Decision:** Before a re-ask, the controller compares the stored observation with chain time: if its `active_offer` has reached `valid_until`, or chain time has reached the session's `expires_at`, the turn closes with `observation_stale` and the next turn is built from the chain. Otherwise ADR-064 stands.
**Consequences:** architecture 5.4, runbook 6.

## ADR-072: Setup approval terms are re-quoted on resume and when an approval is stuck
**Status:** accepted (Q52, answered by the product owner, 2 October 2026)
**Context:** The terms of a participant's setup approval (ADR-065) were cached in memory and survived a refusal and a resume; after a fee rise an agent-signed approval could neither be mined nor replaced, because the relay holds no key for it (ADR-050).
**Decision:** Resume and start-up recovery forget the cached terms, so they are quoted again. An approval still not included `RELAY_REPLACE_AFTER_BLOCKS` after its broadcast is asked of the agent again at the same nonce with both fee caps raised as ADR-050 raises them — the agent signing its own replacement — and the wallet is topped up first when the new worst case exceeds its balance. Funding is never reduced: a wallet already holding more than the new worst case gets nothing more.
**Consequences:** architecture 5.1, runbook 7.

## ADR-073: A long-running operation records how the run's transition ended, not only that it was accepted
**Status:** accepted (Q54, answered by the product owner, 2 October 2026)
**Context:** api_contract section 1.2 defines an operation record's four statuses, and section 2.2 says only that an abort's result "reports which won" — the abort or a settlement that landed first. Nothing said when `start_run`, `step_run`, `resume_run` or `abort_run` is `succeeded` or `failed`.
**Decision:** The route makes the controller's transition and answers `202` with the operation `running`; the operation is then decided by the run's `run.state` events. `start_run` succeeds when the run is `running`, or `terminal` if it ended first; `step_run` when the run is `paused` with cause `step_complete`, or `terminal`; `resume_run` when it is `running`, or `paused` or `preparing` with cause `recovered`, or `terminal`; each of those fails when the run lands in `recovery_required` or `failed_setup`. `abort_run` succeeds when the run is `terminal` or `failed_setup` and fails when it lands in `recovery_required` again — the state it may have started in does not count against it. The result is the deciding event's `{state, state_cause, outcome}`; a failure's `error` is `{code: <state>, message, details: <the same>}`. Every status change is appended as an `operation` run event, and a restarted process tracks every unfinished operation again from the events written since it was created. A request the controller refuses is answered with its error and its operation recorded `failed`.
**Rationale:** An operation that succeeded on acceptance would say nothing the `202` had not, and could not report whether an abort or a settlement won.
**Consequences:** api_contract 1.2 and 2.2; `api.routes.operations`.
**Amended (Q64, 3 October 2026):** a start or a step that a reorg or a recovery leaves `paused` with cause `reorg` or `session_open` — short of what it asked for — is `failed`, its error naming the state and cause; resuming is a new operation. A watcher that meets a database error waits and reads again from where it was. At start-up an accepted operation is tracked again, and any other unfinished one is `failed` with code `interrupted` and its key freed (ADR-078 as amended).

## ADR-074: Replay is built with the interface that steps through it, in stage 4
**Status:** accepted (Q55, answered by the product owner, 2 October 2026)
**Context:** `GET /v1/runs/{run_id}/replay` returns "the ordered list of timeline frames the UI steps through", and the contract never defines a frame; its only consumer is stage 4's replay mode, and A16 is stage 4's exit test.
**Decision:** The route is not served in stage 2.5. Its frame shape is designed with the replay UI in stage 4. The export, which is served, already carries everything a replay is built from.
**Consequences:** api_contract 2.2; build_plan stages 2.5 and 4; the OpenAPI contract test lists the route as deferred.

## ADR-075: The RPC price table prices providers in units per method, and ships with Anvil alone until stage 5
**Status:** accepted (Q56, answered by the product owner, 2 October 2026)
**Context:** ADR-061 calls for an operator-maintained RPC price table, zero for the local chain. A hosted provider bills in its own units — Alchemy in compute units, a different number per JSON-RPC method — and its figures can move before Sepolia is used.
**Decision:** The table is a JSON file: per provider, a price per million units, the units each method costs, a default for a method not listed (or null, which makes an unlisted method's price unknown), a `source` and a `last_verified` date. The packaged table, `api/config/rpc_prices.json`, lists `anvil` at zero; `RPC_PRICE_TABLE` names an operator's own copy, and `RPC_PROVIDER` says which entry prices this deployment. A provider the table does not list prices the run at null — unknown, never zero. Alchemy's entry is added in stage 5, from its unit table as it stands then.
**Rationale:** Counts are exact today; a price written now would be a guess by the time it is used.
**Consequences:** architecture 8; build_plan stage 5; runbook 8; `infra/.env.example`.

## ADR-076: The local Compose profile deploys the contracts with a one-shot service
**Status:** accepted (Q57, answered by the product owner, 2 October 2026)
**Context:** The api container needs a deployment manifest at start-up, and the Compose Anvil keeps no state across a restart, so the contracts must be deployed again whenever it starts.
**Decision:** A `deploy` service in the local profile runs the real deploy script against the Compose Anvil before `api` starts, as the deployer Anvil's first unlocked account, after funding the relay and operator from it; it writes the fixed manifest `local-compose` to a named volume the api reads, and does nothing when that manifest's exchange address still holds code. Anvil's fixed mnemonic and an empty chain give the same addresses each time. `make up` still starts only PostgreSQL and Anvil, which is all the test suites need; `make stack` starts the whole profile.
**Consequences:** architecture 4; runbook 1 and 8; `infra/compose.local.yaml`; `infra/scripts/compose_deploy.sh`.

## ADR-077: Audit completeness is checked against the canonical events the indexer verified
**Status:** accepted (Q58, answered by the product owner, 2 October 2026)
**Context:** `run_metrics.audit_complete` means that "all authorised public actions and final balances reconcile with canonical chain evidence" (spec 11.2). Re-reading the chain for every recomputation would scan logs from the deployment's start block and cost compute units on Sepolia each time.
**Decision:** The metrics calculator checks against the canonical `chain_events` the indexer read and verified: the outcome is the canonical terminal event's at the threshold, and its transaction the recorded one; every mined signed action has its event, offers by digest, in a gapless sequence, and every action event a signed action; both parties' terminal balances are recorded; and a settlement's balances moved by exactly its two legs. It makes no RPC request. The database-free reconstruction from the chain stays the A15 tool's, run by the test suite and the runbook.
**Consequences:** data_model 3.13; `export.v1.json`'s description of the field.

## ADR-078: An idempotency key replays a success, is claimed before the work, and is scoped to its path
**Status:** accepted (Q59, answered by the product owner, 2 October 2026)
**Context:** api_contract section 1 says a repeated key with the same body replays the stored response and a different body is `409 idempotency_conflict`. It did not say what a key does after a refusal or while the first request is still being answered, or what "per route" means for a route with a run id in it.
**Decision:** A request with `Idempotency-Key` claims the key first, as an `operations` row holding the route, the key and `json_sha256` of the body, so that of two racing requests the database's `(route, idempotency_key)` uniqueness lets one do the work. Only success is replayed: a refused request releases its key, and a retry with it runs the work again. The same key while the first request is still being answered is `409 idempotency_conflict`. A long-running route's replay is its operation record as it stands. The route is the concrete path — `POST /v1/runs/<run id>/start` — so a key is scoped to the run it was sent for. A key older than 24 hours is released when next seen. The key must be a UUID (`400 bad_request` otherwise).
**Rationale:** Replaying a refusal would make a client invent a new key after fixing what was refused; a claim before the work is the only way a race does it once.
**Consequences:** api_contract 1; data_model 3.15.
**Amended (Q63, 3 October 2026):** a long-running route's same-key request while the first is still in flight is answered with the live operation, `202` with `Idempotent-Replayed: true` — not `409` — which suits a client polling its operation. A claim whose request never answered — the process died, or the client left and its task was cancelled before the refusal could be recorded — is released when it is next seen `IDEMPOTENCY_CLAIM_TIMEOUT_S` (60 s) after the claim, and at start-up: its operation is `failed` with code `interrupted`, and a retry does the work. A cancelled request records its refusal before it goes.

## ADR-079: Chain wait runs to inclusion; decision time is every attempt's latency
**Status:** accepted (Q60, answered by the product owner, 2 October 2026)
**Context:** Spec 11.2 asks for "time waiting for inclusion/confirmation" and "total elapsed decision time" without saying where either is measured.
**Decision:** `chain_wait_ms` and `setup_chain_wait_ms` sum, per mined transaction, the time from the first broadcast at its sender and nonce — a replaced predecessor's included — until its receipt was seen. `decision_time_ms` sums the latency every agent reported for every attempt, either policy. Measuring to confirmation would need a new `tx_outbox.confirmed_at`; at threshold 1 the two are the same, and on Sepolia at threshold 2 inclusion understates the wait by about a block per action, which is stated rather than hidden.
**Consequences:** data_model 3.13.
**Amended (Q68, 3 October 2026):** `input_tokens` counts every input token the model was sent, the cached ones included: `input_tokens + cache_read_input_tokens + cache_creation_input_tokens` of each attempt's usage.

## ADR-080: The operational failure class follows the cause that ended or stopped the run
**Status:** accepted (Q61, answered by the product owner, 2 October 2026)
**Context:** `run_metrics.failure_class` is one of `model`, `signing`, `rpc`, `execution`, `none`, and nothing said which cause is which.
**Decision:** From the termination's cause where there is one, else the state's: `model_failure` and `budget_exhausted` are `model`; a reverted transaction (`execution_failure`, `<kind>_reverted`), `settlement_check_failed`, `session_refused` and `termination_reverted` are `execution`; `rpc_timeout`, `unreachable`, `refused` and `nonce_conflict` are `rpc`; an unavailable agent and an agent's or the observation's refusals (`agent_*`, `observation_*`, `signed_action_*`) are `signing`. A run in `recovery_required` is classed by its fault even when a termination is recorded. A run that ended without a fault — an operator's abort and a deadline expiry included, which are decisions and no-deal results, not failures (FR-E6) — is `none`. A run still going, and a stop no class describes (`internal_error`), is null; `export.v1.json` admits the null.
**Amended (Q67, 3 October 2026):** the indexer's `session_opening_missing` and `invalid_confirmation_threshold` are `execution`; ADR-081's `chain_unavailable` is `rpc`.
**Consequences:** data_model 3.13; `export.v1.json`.

## ADR-081: A deployment is scoped to its chain by genesis block; a run on a chain that is gone is stranded, not driven
**Status:** accepted (Q62 and Q70, answered by the product owner, 3 October 2026)
**Context:** The stage 2.5 review found that after an Anvil restart with the database kept — what `make down` then `make stack` does — the relay took the operator's next nonce from every transaction the database had ever recorded for that address, on any chain, so the new chain's setup transactions queued behind a gap and the run hung in `preparing` with its operation `running` for good. The same history leaked elsewhere: the indexer watched, polled and re-verified every non-terminal run, whatever chain it was on, and the `local-compose` deployment row was overwritten, so earlier runs would export a manifest deployed after them.
**Decision:** A chain instance is identified by its genesis block hash as well as its ID. `deployments` gains `genesis_hash` (migration 0004), and its uniqueness becomes `(chain_id, genesis_hash, exchange_address)`. At start-up the backend checks the RPC against the manifest — the chain ID, the exchange's code at its address, and for a deployment it loaded before the same genesis — and refuses to start otherwise; it stores the deployment with its genesis. The Compose `deploy` names each deployment `local-compose-<first eight hex digits of the genesis hash>`, so a restarted Anvil is a deployment of its own and the old one, with its runs, is kept. Everything the relay and the indexer read across runs — open sessions, receipts awaited, rows at a height, rows in a status, recorded nonces, event depth — is scoped to the deployment the backend serves. Health's `rpc_ok` checks the genesis too. A run on another deployment is refused every operation, `409 invalid_state` with `details.deployment`; one that holds the active-run slot at start-up — stranded when its chain went away — is moved to `recovery_required`, cause `chain_unavailable`, its outcome left pending, and gives the slot up so a run on the new chain can start. Its rows and its export stay as they were.
**Rationale:** Of the three options put to the product owner — refuse to start until `make reset-db`, scope to the chain, or document a reset — scoping keeps the old runs' evidence and needs no manual step after a routine restart. Freeing the slot is the only way the new chain can be used, and `recovery_required` with its outcome pending says honestly that the run's ending is unknown.
**Consequences:** migration 0004; data_model 3.2 and 3.13; api_contract 2.1 and 2.2; architecture 4 and 8; runbook 6 and 8; `infra/scripts/compose_deploy.sh`. Events of a session no run claims (`run_id` null) are not deployment-scoped and may be re-verified once against a new chain, which can mark such rows non-canonical; they belong to no run, so no run, metric or export changes.

## ADR-082: A shutdown ends the event streams first, so the lease is given up before the process is killed
**Status:** accepted (Q65, answered by the product owner, 3 October 2026)
**Context:** An open SSE stream ended only when its client left, so uvicorn's graceful phase waited on every browser and Docker killed the process after its stop grace; the lifespan's teardown never ran, the lease was not released, and the next process waited the lease's 30 s before it could take the run over — against `api.main`'s promise of "at once".
**Decision:** When a shutdown begins (`SIGTERM`, `SIGINT`), every stream ends at its next look, and the client reconnects with `Last-Event-ID` to whichever process serves next. Uvicorn's graceful phase is bounded at 5 s (`GRACEFUL_SHUTDOWN_S`) for anything else, and the Compose `api` service's stop grace is 15 s, so the teardown — which releases the lease — runs before the container is killed.
**Consequences:** `api.main.make_server`, `api.routes.sse`; runbook 8; `infra/compose.local.yaml`.

## ADR-083: Utilities and the efficiency ratio use the bounds feasibility uses
**Status:** accepted (Q66, answered by the product owner, 3 October 2026)
**Context:** The feasible surplus used ADR-045's capital-bounded buyer cap and the utilities the bare reservation prices, so where the buyer's capital bound its willingness the captured surplus could exceed the feasible surplus and the efficiency ratio exceed 1.
**Decision:** Both use the same bounds: the buyer's utility is the most it could pay — the lesser of its reservation price and its quote balance less its floor — less the price; the seller's is the price less the least it could take, its reservation price and never below 1. The captured surplus of a settlement within both mandates is then exactly the feasible surplus, and efficiency stays within 0 and 1. Where capital does not bind, as in the default scenarios, these are spec 11.2's reservation utilities.
**Consequences:** data_model 3.13. This departs from spec 11.2's literal wording where the buyer's capital binds; the departure is the product owner's.

## ADR-084: A run request's text refuses NUL, and start-up refuses a scenarios directory with nothing in it
**Status:** accepted (Q69, answered by the product owner, 3 October 2026)
**Context:** PostgreSQL cannot store a NUL character; one in a mandate's instructions passed the request's validation and failed at the database as a `500`, whose error text — before ADR-081's companion fix to the logs — quoted the row. A missing or misspelt `SCENARIOS_DIR` started the API silently with no scenarios.
**Decision:** Every free-text field of a run request — name, scenario and deployment ids, model id, effort, instructions — refuses a NUL with `422 validation_error`, a clone's merged request included. A `SCENARIOS_DIR` that is missing or holds no scenario stops start-up with a message naming the directory. A NUL in an agent's raw response, which a stage 3 model's explanation could carry, is Q71, open for stage 3.
**Consequences:** api_contract 2.2; runbook 8; `api.controller.requests`, `api.main`.
