# Open Questions

Decisions the governance documents could not settle from the spec. Each gets a provisional answer so work can proceed; the product owner confirms or overrides it, and the matching decision-log entry is updated when they do.

Three questions are open: Q20, which the product owner deferred to stage 5; Q40, the Sepolia RPC plan, which stage 5 needs; and Q41, whether stage 6's batch evaluation stays in scope. Every other question raised so far has been answered, and the [Resolved](#resolved) table is the record. The reasoning behind the answers that needed discussion is kept below, because the reasoning is the part that matters when one of them is revisited.

## Open

| # | Question | Status | Stage 2 behaviour until answered | Touches |
|---|---|---|---|---|
| Q40 | Which Alchemy plan the Sepolia profile runs on, and with what usage limit. Alchemy's free tier caps `eth_getLogs` at 10 blocks, so the indexer's re-read of the unfinalized window does not work on it with the default range. | Asked on 1 October 2026; the product owner is on Pay As You Go and wants no large bill. | Recommended: Pay As You Go, the default `INDEXER_LOG_CHUNK_BLOCKS` of 2,000, and an Alchemy usage limit of about $5 a month (about 9.5M compute units, around 45 hours of active polling at about $0.10 an hour), with the 2.4 controller polling only while a run is active. Reasoning in ADR-053's correction and runbook section 7. | ADR-053, runbook 7, `infra/.env.example`, build_plan stage 5 |
| Q20 | A reorg deeper than the confirmation threshold can remove a terminal event after the run has reached `TERMINAL`, which [architecture.md](architecture.md) section 6.1 makes final. What should happen? Possible on Sepolia at threshold 2. | **Deferred to stage 5** by the product owner, 25 September 2026, where finalized-head tracking is verified against a real RPC. | `TERMINAL` stays final and the indexer stops watching a terminal run. A14 exercises a reorg *before* the threshold — confirmation threshold 2 on Anvil — which is the case [test_strategy.md](test_strategy.md) describes. The alternative put to the product owner: keep watching until the terminal block is finalized, and on a reorg move the run to `RECOVERY_REQUIRED` with its outcome reset to pending. | architecture 6.1 and 5.5, data_model 5 and 6, build_plan stage 5 |
| Q41 | Does stage 6's batch evaluation stay in scope? The product owner is unsure it is wanted and asked for the case for it, while deciding the settlement-approval feature ([ADR-062](decision_log.md)). | Asked on 1 October 2026. | Stage 6 stays in the plan; nothing before it depends on the answer. The case for it: a rate needs a population — one run cannot show a feasible-scenario settlement *rate*, an infeasible-scenario trade rate of zero, a mandate-violation count of zero beyond anecdote, or a comparison against the deterministic baseline ([ADR-008](decision_log.md), FR-V1 to FR-V4), and release criterion 1 reads A01's evidence over the full population. The case against: the three model pairings cost real USD over hundreds of runs, and PRD 2.1's claims 1 to 7 are each demonstrable on single runs. A smaller population than FR-V4's is a middle path that keeps the rates and shrinks the bill. If stage 6 is dropped, the automated-run case for making settlement approval per-run configurable disappears with it. | PRD 6.7, 8 and 9, test_strategy A01, build_plan stage 6, ADR-008, ADR-062 |

Add new questions here as they arise, with a provisional answer and the documents the answer touches, rather than deciding silently in code.

---

# Reasoning behind the answers that needed discussion

Q6, Q14, Q15 and Q16 were decided on 21 September 2026; Q17 on 24 September 2026; Q18 and Q19 on 25 September 2026; Q21 to Q24 on 30 September 2026, when building the agent service raised them; Q25 to Q28 the same day, when its adversarial review did; Q29 to Q33 the same day again, before stage 2.3's relay and indexer were written; Q34 to Q39 on 1 October 2026, when its adversarial review raised them.

## Q18: fresh participant wallets per run

**The gap.** Spec section 8 and FR-S6 require fresh participant wallets for every run, and an address is never reused. ADR-023 reached participant keys through `env:` and `keystore:` references to fixed variables and files, regenerated "per run by a script" — a manual step and two agent restarts before every run. The stage 6 batches run hundreds of runs, and the backend, which has to store each wallet's address and fund it, cannot learn an address without holding the key.

**The options put to the product owner.** Derive each run's key inside the agent from one root secret; regenerate keys by hand per run as documented; generate an ephemeral key in agent memory at provisioning; or drop the fresh-wallet rule for v0.1. Only derivation meets every requirement at once: no manual step, keys that never leave the agent, a run that survives an agent restart, and no stored per-run key. An ephemeral key is lost on restart, which would turn a routine agent restart into an aborted run.

**Decision (accepted): derive per run inside the agent**, with one addition from the product owner: the derivation must be domain-separated by environment, role and run, so that a buyer key cannot collide with a seller key nor a local key with a Sepolia one. The database keeps the derived address and the derivation metadata, never a key. The scheme, and why the chain ID stands for the environment, are in [ADR-039](decision_log.md).

## Q19: who signs a participant's setup approval

**The gap.** Setup needs each participant wallet to sign an ERC-20 `approve` of the exchange, the key lives only in that participant's agent process, and the internal API had no endpoint for it.

**Decision (accepted): the agent builds the approval itself** from provisioned state — its own role's token, the provisioned exchange as spender, the provisioned allowance — and the backend supplies nothing but nonce, gas limit and fee caps. The rejected alternative was to let the backend load participant keys for setup, which breaks the one boundary the security document states without exception. [ADR-040](decision_log.md).

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

## Q17: same-token deployment guard

**What was true.** `NegotiationExchange`'s constructor checked only that its three addresses were non-zero. Nothing stopped a deployment that passed the same token as both legs, and such a session would settle by transferring `quoteAmount` from buyer to seller and `baseAmount` back in the same token — a net payment at a price neither party signed, recorded by the seven events as an ordinary settlement.

**Why it was not simply fixed.** [protocol.md](protocol.md) section 2 described two mock tokens as the deployment's topology; it stated no constructor precondition, and section 8.3 defined no error for this. Adding a guard meant adding an error name to the protocol's error table, which is a protocol change and needs an ADR. The verifiers were right that the code as written diverged from nothing.

**Decision (accepted): add the guard.** The counter-argument was weighed rather than dismissed — the deploy script controls both addresses, the setup validator compares them against the manifest, and A17 would catch a mis-wired deployment before any run — but each of those is a procedure and the guard is structural, which is the preference CLAUDE.md states. Recorded as [ADR-037](decision_log.md), which also notes that this is not a protocol *version* bump: an added error is not a type string, a `configHash` encoding, a reason code or an event field, so no digest changes.

**A second guard came free.** The reconstruction tool catches the same mis-wiring from the other direction and after the fact: a manifest whose token pair does not match the chain fails the `configHash` recomputation, because the two token addresses are inside that hash. There is a test for it.

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
| Q17 | Same-token deployment guard | Add the guard: `baseToken == quoteToken` reverts with a new `InvalidTokenPair()` error. The reasoning is in [ADR-037](decision_log.md); the short form is that the cost is one comparison and one error name, while the failure it prevents is silent and appears in the evidence as a successful run. | ADR-037, protocol 2 and 8.3 | 24 September 2026 |
| Q16 | ENS naming | Adopted, display-only: one Sepolia name with subnames for the exchange and both mock tokens, resolved once at deployment and pinned into the manifest. Nothing resolves ENS at run time. Reasoning [above](#q16-ens-naming). | ADR-030, api_contract deployments, data_model 3.2, PRD FR-U10, test_strategy A17, build_plan stage 5 | 21 September 2026 |
| Q18 | Fresh participant wallets per run | Each agent derives the run's key from one root secret with HMAC-SHA256, domain-separated by chain ID, role and run ID; the database stores the address and the derivation metadata, never a key. Reasoning [above](#q18-fresh-participant-wallets-per-run). | ADR-039, ADR-023 amended, api_contract 6, data_model 3.5, security 7, architecture 3.3 and 5.1 | 25 September 2026 |
| Q19 | Signing a participant's setup approval | The agent builds and signs the ERC-20 `approve` itself from provisioned state; the backend supplies only nonce, gas limit and fee caps. Reasoning [above](#q19-who-signs-a-participants-setup-approval). | ADR-040, api_contract 6, architecture 5.1 | 25 September 2026 |
| Q21 | What the agent internal API's HMAC covers | Method, path and body, not the body alone: a body-only MAC was the same for every empty body, so a captured health check authorised any run's `release`. | ADR-041, api_contract 6, security 5 | 30 September 2026 |
| Q22 | The setup approval's gas-limit bound | 100,000 by default, configurable as `AGENT_SETUP_GAS_LIMIT_MAX`; about twice what an OpenZeppelin `approve` costs. | ADR-042, api_contract 6 | 30 September 2026 |
| Q23 | The deterministic policy's walk-away reason when its own holdings block it | `inventory_constraint`, when its balance or inventory floor refuses the move it would otherwise make; section 13's reasons otherwise. | ADR-043, protocol 13 | 30 September 2026 |
| Q24 | How `approve-session` checks the expiry window | The backend sends the opening block's timestamp; the agent requires `opened_at_ts < expires_at_ts <= opened_at_ts + session_duration_s`. | ADR-044, api_contract 6 | 30 September 2026 |
| Q25 | What a buyer's inventory floor means | Capital: each party's floor applies to the token it gives up — mASSET for the seller, mUSD for the buyer. | ADR-045, protocol 11.1, mandate schema | 30 September 2026 |
| Q26 | What the agent does with a self-contradictory observation, and the order of `history` | The agent refuses with `422 observation_inconsistent`; the controller rebuilds from the chain and retries up to five times, then `RECOVERY_REQUIRED`. History is ascending by sequence; an expired offer is not active. | ADR-046, protocol 12, api_contract 6 and 7, architecture 6.1 | 30 September 2026 |
| Q27 | What bounds the setup approval's fees | The worst-case cost, gas limit times fee cap, at most `AGENT_SETUP_MAX_COST_WEI`, default 0.01 ETH. | ADR-047, ADR-042 corrected | 30 September 2026 |
| Q28 | A release during a turn, and restart recovery | Release cancels the signature of a turn in flight; after an agent restart the controller re-provisions, re-approves the session from the canonical event, and retries. | ADR-048, api_contract 6, architecture 11 | 30 September 2026 |
| Q29 | When the relay replaces a stuck transaction | Automatically: after `RELAY_REPLACE_AFTER_BLOCKS` (3) blocks without inclusion, both fee caps up 12.5 percent, never above `RELAY_MAX_FEE_PER_GAS_WEI` (100 gwei); at the ceiling it stops and waits. Never for a transaction an agent signed. | ADR-050, data_model 3.10, runbook 6 | 30 September 2026 |
| Q30 | Where an execution failure's sentence lives, since a revert emits no event | A seventh timeline kind, `execution_failure`, with its sentence stored on the `tx_outbox` row. | ADR-051, data_model 3.10, api_contract 2.2 and 4, export schema | 30 September 2026 |
| Q31 | Who writes the economic outcome, given that a constraint couples it to the run state | The controller, in stage 2.4. The indexer confirms and verifies the terminal event; the projection derives the outcome. | ADR-052, data_model 5 | 30 September 2026 |
| Q32 | How long the indexer re-checks block hashes | Until the RPC's finalized head covers them, for runs not yet terminal. | ADR-053, architecture 5.5 | 30 September 2026 |
| Q33 | What the relay does with an action the node predicts will revert | Broadcasts it at a fallback gas limit (500,000), so the contract decides and the failure is on chain. | ADR-054 | 30 September 2026 |
| Q34 | An invalidated chain event seen again in the same block | Made canonical again (same row, same key); a rewind invalidates only rows whose block hash the chain no longer has. | ADR-055, data_model 3.11 and 3.12 | 1 October 2026 |
| Q35 | A revert reason the replay cannot reproduce | Replay at the inclusion block, then its parent; `Undetermined` when neither reverts. | ADR-056, runbook 6 | 1 October 2026 |
| Q36 | A node refusing a resend during recovery | A seventh outcome, `refused`, with the node's reason recorded and logged. | ADR-057, runbook 6 | 1 October 2026 |
| Q37 | How reorg and terminal signals survive a crash | A `chain.reorg` run event written in the rewind's transaction; terminal events reported on every poll until the run is terminal. | ADR-058, api_contract 3 | 1 October 2026 |
| Q38 | An invalid `confirmation_threshold` in a run's configuration | Refused, back to a person; one reading shared by indexer and projection. | ADR-059 | 1 October 2026 |
| Q39 | web3's built-in retries | Off: `RPC_TIMEOUT_S` is the real limit. | ADR-060 | 1 October 2026 |
