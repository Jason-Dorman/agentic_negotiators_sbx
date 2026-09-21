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
**Status:** accepted (assumption)
**Context:** Spec 5.4 says the decision has exactly three shapes and extra fields are rejected. Spec 3.2 allows an optional operator explanation.
**Decision:** The model returns `{ "decision": <one of three shapes>, "explanation"?: string }`. The `decision` object is strict; `explanation` is capped at 280 characters, operator-only, and labelled a self-report.
**Consequences:** Both spec statements hold without ambiguity. Schema in `packages/protocol/schemas/agent_decision.v1.json`.

## ADR-014: Anthropic Claude API as the model provider
**Status:** accepted (assumption)
**Context:** Spec does not name a provider or model ID.
**Decision:** Official `anthropic` Python SDK. Default `claude-opus-5`, configurable per run and per side. Structured outputs via `messages.parse` with a Pydantic decision envelope. Adaptive thinking on with `effort` recorded per run. SDK automatic retries set to 0 so the single repair is the only retry and cost accounting is exact.
**Consequences:** "Sampling settings" in spec 11.3 are recorded as `effort` because current models do not accept temperature. Seed is recorded as unsupported. Model client is behind an interface so another provider is an adapter, not a rewrite.

## ADR-015: No server-side model fallbacks
**Status:** accepted (assumption)
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
**Status:** accepted
**Decision:** A single-row `active_run` table plus `run_leases` with expiry. Relay nonces are serialized under the same lease.
**Consequences:** Batches run sequentially. Parallel evaluation is a future change requiring per-run relay keys.

## ADR-020: PostgreSQL enums and NUMERIC(78,0) amounts
**Status:** accepted
**Decision:** Enum types for all state fields; `NUMERIC(78,0)` for token amounts; JSON strings for amounts on the wire.
**Rationale:** Prevents silent overflow and float rounding; keeps the database self-describing.

## ADR-021: Toolchain
**Status:** accepted (assumption)
**Decision:** Python 3.12 with `uv`, `ruff`, `mypy --strict`, `pytest`; Node LTS with `pnpm`, Vitest, Playwright; Foundry; Alembic; Docker Compose; PostgreSQL 16.
**Consequences:** Exact pins recorded here when the lockfiles are first committed.

## ADR-022: Scenario files are JSON validated by schema
**Status:** accepted (assumption)
**Decision:** `scenarios/*.json` validated against `packages/protocol/schemas/scenario.v1.json`. Batch populations are generated JSON committed with their seed.
**Rationale:** One serialization format across the repo; no YAML parser dependency.

## ADR-023: Sepolia participant keys in encrypted keystores
**Status:** proposed (assumption)
**Decision:** Local profile uses `env:` key refs generated per run. Sepolia profile uses web3 keystore JSON files with a password from env. Neither is production custody.
**Open:** confirm with the operator; see [open_questions.md](open_questions.md).

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
