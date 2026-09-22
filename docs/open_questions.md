# Open Questions

Decisions the governance documents could not settle from the spec. Each gets a provisional answer so work can proceed; the product owner confirms or overrides it, and the matching decision-log entry is updated when they do.

Every question raised while producing the governance set has now been answered. The [Resolved](#resolved) table is the record. The reasoning behind the four answers decided on 21 September 2026 after discussion is kept below, because the reasoning is the part that matters when one of them is revisited.

## Open

None. Add new questions here as they arise, with a provisional answer and the documents the answer touches, rather than deciding silently in code.

---

# Reasoning behind the answers of 21 September 2026

## Q6: mandate storage at rest

**What encryption at rest would and would not buy.** The mandate is private because leakage to the other agent invalidates PRD claim 1. Every control that prevents that leakage is structural and unaffected by encryption: the allowlisted observation schema, the import-boundary test that stops the observation builder touching the opponent's row, process and credential isolation, and the outbound-context scan. Column-level encryption defends a different threat — someone who obtains a database file or a backup — and in this deployment the decryption key would sit in the same `.env` on the same host as the database, so it defends that threat only against an attacker who gets the dump and not the host.

**What it would cost.** `reservation_price_minor` and `min_remaining_inventory_minor` are `NUMERIC(78,0)`. Encrypting them makes them `BYTEA`, which removes range queries and comparison in SQL and pushes the offline evaluator's feasible-interval computation and the metrics calculator into application code. `mandate_hash` still has to be computed over the plaintext canonical JSON, so the hash chain is unchanged and gains nothing. Migrations and fixtures both get more awkward.

**Decision (accepted).** Keep plaintext, and make the assumption explicit rather than papering over it: mandate confidentiality in v0.1 rests on process isolation and repository access control on a host the operator fully trusts, and the security document says so in those words. If the goal is to show judgment rather than to change the threat surface, the higher-value moves are already cheap and partly present — the data classification table, the export default that excludes private data, a `--redact-mandates` export flag, and a documented purge command with a retention statement.

**Middle path, considered and not taken.** Encrypt only `mandate_versions.instructions` with pgcrypto. That is the free-text field most likely to contain something you would not want in a screenshot, and it is never compared or aggregated, so nothing downstream breaks. The numeric mandate fields stay queryable. Cost is roughly one migration, one repository change, and one key reference in `.env.example`. This remains the cheapest upgrade path if the stance changes.

## Q14: confirmation threshold on Sepolia

**The arithmetic.** Sepolia produces a block roughly every 12 s, so two confirmations add roughly 24 s to each on-chain action. A negotiation that records four offers and one acceptance performs five actions, so confirmation waiting contributes about 2 minutes. The session duration is 1,800 s, so there is a wide margin; the constraint is the audience's patience in a live demo, not the protocol.

**Why 2 is worth the wait.** The transaction lifecycle is displayed as four distinct states — Submitted, Included, Confirmed at threshold, Finalized — and PRD FR-E3 requires them to be separate. At a threshold of 1, Included and Confirmed become the same instant and the distinction on screen becomes decorative, which undercuts the point the display exists to make. Depth also has to be a number the operator chooses for the reorg machinery (A14, block-hash tracking, non-canonical marking) to read as a deliberate policy rather than an accident.

**Decision (accepted).** Keep 2 as the default on Sepolia and 1 on the local chain, both already configurable per run and recorded in the run's export. If a live demonstration drags, the lever is setting `confirmation_threshold` to 1 for that run, which is visible in the evidence, rather than a code change. Two confirmations are never labelled finality; the finalized-head indication remains separate.

## Q15: license

**Why a license matters here.** A public repository with no license grants no rights. A reviewer who wants to clone and run it is technically not permitted to, and some employers' open-source policies flag unlicensed repositories. Adding one is a two-minute action that removes an avoidable question mark.

**The choice.** MIT is the shortest and most familiar. Apache-2.0 adds an explicit patent grant and a `NOTICE` convention, is the common choice for infrastructure and contract code, and reads as the more deliberate of the two — which suits a project whose point is deliberateness. Either is safe. **Decision (accepted): Apache-2.0**, with a `NOTICE` file carrying the copyright line and the manufactured-assets disclaimer.

**The deadline.** Solidity emits a warning without an `SPDX-License-Identifier` comment on every source file, and the identifier is compiled into the metadata. Stage 1 creates `MockERC20` and `NegotiationExchange`, so the answer is needed before stage 1 begins or those files carry a placeholder that has to be rewritten later. `LICENSE` at the repository root, the SPDX identifier in each `.sol` file, and a line in `README.md` are the whole change.

## Q16: ENS naming

**What is actually possible.** ENS is deployed on Sepolia, and `.eth` names can be registered there with test ETH, so the demo can have names at no real cost. Those names exist only on Sepolia: a reader who visits the ENS app on mainnet will not see them. A mainnet name is a separate, real purchase (order of USD 5 per year for names of five characters or more, considerably more for short ones) and it cannot make a Sepolia contract address resolve on mainnet in any way a reviewer would check. Reverse resolution — address to name, the "primary name" — is per-chain, so a Sepolia address gets its primary name from the Sepolia reverse registrar.

**Decision (accepted): this shape.** Register one name on Sepolia, for example `agentnegotiation.eth`, and give the deployment subnames: `exchange.agentnegotiation.eth` for `NegotiationExchange`, plus one for each mock token. Set the Sepolia primary name for the exchange so explorers show it. The UI displays the name beside the address.

**The constraint that must hold.** ENS names are display only. They are resolved once, at deployment, and written into the deployment manifest next to the address they resolved to. Nothing at run time resolves ENS: not the indexer, not the setup validator, not the signer, not the reconstruction tool. Every identity check continues to compare the manifest address against the chain. A name is mutable state owned by whoever controls the registration, and the project's rule is that canonical chain events are the only source of economic outcome; a name must never become part of that chain of evidence. Acceptance A17 would additionally assert, at deployment time, that each recorded name still resolves to its manifest address, and treat a mismatch as a warning on the deployment manifest rather than a failure of the run.

**Cost and risk, accepted.** Roughly half a day: registration, subnames, a manifest field, an API field, a UI chip, one test. The risk is scope creep into stage 5, which is already the stage with the most moving parts. It is not needed for any acceptance criterion, and stage 5 must not slip for it: if the registration or the manifest field is not ready, the deployment ships without names and they are added afterwards.

**On the job-application motivation.** A mainnet `yourname.eth` with a text record pointing at the repository does more for a résumé than Sepolia subnames do, and it is unrelated to this codebase. Treat the two separately.

---

## Resolved

| # | Question | Resolution | Recorded in | Date |
|---|---|---|---|---|
| — | Diagram format | All diagrams are Mermaid. | ADR-026 | 19 September 2026 |
| Q1 | Model provider and default model | Anthropic Claude, `claude-opus-5` for both sides and all modes, for the first run. Cross-provider pairings, for example Claude against a non-Anthropic model, are an explicitly wanted follow-on experiment, not v0.1. | ADR-014, ADR-027, architecture 7, PRD 5 and 10 | 21 September 2026 |
| Q2 | Repository and version control | Single monorepo, initialized, layout per spec 10.2. The license question it raised is Q15. | build_plan stage 0 | 21 September 2026 |
| Q3 | Sepolia key custody | Encrypted web3 keystore files with an env-provided password, from the start, for the Sepolia profile; `env:` refs for local. The ENS question it raised is Q16. | ADR-023, security 7 | 21 September 2026 |
| Q4 | Sepolia RPC provider | Alchemy, using a new application created for this project rather than an existing one, so its rate limits and usage are attributable to this project alone. Indexer poll interval 4 s. | architecture 4 and 8, build_plan stage 5 | 21 September 2026 |
| Q5 | Operator authentication for remote demos | Static bearer token, bound to localhost by default. Exposing the UI beyond the host requires a STRIDE-structured review of the threat model before it happens. | ADR-028, api_contract 1, security 8 | 21 September 2026 |
| Q6 | Mandate storage at rest | Plaintext. Confidentiality rests on process isolation, repository access control, and the classification table, on a host the operator fully trusts; the security document states this rather than implying encryption. Reasoning [above](#q6-mandate-storage-at-rest). | ADR-031, data_model 3.4, security 7 | 21 September 2026 |
| Q7 | Explanation field | Envelope with a separate `explanation`, as proposed. | ADR-013, protocol 11 | 21 September 2026 |
| Q8 | Scenario file format | JSON. | ADR-022 | 21 September 2026 |
| Q9 | Batch concurrency | Sequential in v0.1; parallelism noted as a follow-on requiring per-run relay keys. | ADR-019, build_plan stage 6 | 21 September 2026 |
| Q10 | Explorer | Etherscan Sepolia. Every recorded transaction must produce a link an observer can open. | api_contract deployments, PRD FR-U2 | 21 September 2026 |
| Q11 | Prompt content ownership | Versioned file under `services/agent/prompts/`. Mandate `instructions` are appended as the agent's private guidance and cannot override protocol rules or the output schema. | ADR-029, architecture 7 | 21 September 2026 |
| Q12 | Runbook location and format | `docs/runbook.md` is a living document, completed in stage 5. Originally to start in stage 2; the product owner brought it forward to stage 0 on 22 September 2026, where it now covers local startup, database isolation and the ports, and key generation. | build_plan stages 0, 2 and 5 | 21 September 2026, amended 22 September 2026 |
| Q13 | Coverage thresholds | As proposed: 100 percent lines on `NegotiationExchange`, 100 percent branches on the agent validator and signer, 85 percent lines on the backend. | test_strategy 10 | 21 September 2026 |
| Q14 | Confirmation threshold on Sepolia | Keep 2, configurable per run and recorded in the export. Reasoning [above](#q14-confirmation-threshold-on-sepolia). | PRD 5, architecture 6 | 21 September 2026 |
| Q15 | License | Apache-2.0, with a `NOTICE` file. `SPDX-License-Identifier: Apache-2.0` at the top of every Solidity source, in place before stage 1. Reasoning [above](#q15-license). | ADR-032, contributing, build_plan stage 0 | 21 September 2026 |
| Q16 | ENS naming | Adopted, display-only: one Sepolia name with subnames for the exchange and both mock tokens, resolved once at deployment and pinned into the manifest. Nothing resolves ENS at run time. Reasoning [above](#q16-ens-naming). | ADR-030, api_contract deployments, data_model 3.2, PRD FR-U10, test_strategy A17, build_plan stage 5 | 21 September 2026 |
