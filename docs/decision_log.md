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
**Status:** accepted (confirmed by the product owner, 21 September 2026)
**Decision:** Local profile uses `env:` key refs generated per run. Sepolia profile uses web3 keystore JSON files with a password from env, present from stage 0 rather than retrofitted at stage 5. Neither is production custody.
**Consequences:** `infra/secrets/` is git-ignored and the secret scan rejects keystore JSON. The keystore path reaches the key holder as a `keystore:` ref; the password reaches it from env; neither is ever a value in the database, a log, or an export. The local profile keeps `env:` refs so a developer needs no password to run the suite.

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
