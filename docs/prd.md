# Product Requirements Document: Two-Agent Negotiation and Settlement Sandbox

| | |
|---|---|
| **Version** | 0.1.0 |
| **Date** | 19 September 2026 |
| **Status** | Draft for build |
| **Source** | [System Spec v0.1](Agent-Negotiation-Sandbox-System-Spec-v0.1.md) |
| **Related** | [architecture.md](architecture.md), [protocol.md](protocol.md), [api_contract.md](api_contract.md), [data_model.md](data_model.md), [test_strategy.md](test_strategy.md), [open_questions.md](open_questions.md) |

This document restates the system spec as testable product requirements. Where it adds detail the spec leaves open, the addition is marked **[assumption]** and tracked in [open_questions.md](open_questions.md). Where the two conflict, the spec wins until this PRD is amended.

---

## 1. Summary

Build a small, observable experiment in which two independently instructed AI agents negotiate the price of a fixed quantity of a mock asset, record every valid public action on an Ethereum testnet, and settle the agreed exchange atomically with test tokens.

The human operator sets each agent's private mandate and starts a run. The agents decide whether to offer, counter, accept, or walk away. Ordinary software (a policy signer) enforces each mandate before anything is signed. A non-upgradeable smart contract records authorized actions and moves both token legs only when both parties have signed the same terms.

The deliverable is a runnable repository, a React interface, a persistent run history with deterministic replay and JSON evidence export, a deterministic baseline policy, and a bounded batch evaluation mode.

## 2. Problem and motivation

We want evidence about whether model-driven agents can conduct a genuine bilateral negotiation under enforced authority limits, with an auditable public record and atomic settlement. Existing demos tend to script the outcome, hide the authority boundary, or rely on a trusted intermediary to move assets. This sandbox makes each of those three things explicit and testable.

### 2.1 What a successful v0.1 proves

1. Two agents pursue separate objectives without seeing each other's private limits.
2. Offers and counteroffers are genuine model decisions, not a scripted final price.
3. The public negotiation can be reconstructed from contract events and calldata alone.
4. Both parties authorize the exact economic terms of the exchange.
5. Settlement transfers both token legs atomically, or neither.
6. Walk-away, expiry, operator intervention, and infrastructure failure remain distinguishable outcomes.
7. A presenter can replay any run and explain the agreement, the authority chain, and the resulting balances.

### 2.2 What it does not prove

A successful demo does **not** establish customer demand, superior trading performance, credit underwriting, production security, or independence from the operator who controls both agents. In a fixed-quantity price-only trade, every feasible price yields the same total reservation surplus; negotiation only changes its distribution and the probability of completing the trade. All copy, exports, and reports must say so.

## 3. Users and personas

| Persona | Needs | Primary surface |
|---|---|---|
| **Operator / presenter** | Configure a scenario, start and control a run, reveal private mandates during a presentation, replay and export evidence, explain outcomes from the public record | React UI |
| **Evaluator / researcher** | Run reproducible batches on the local chain, compare model policies against a deterministic baseline, export complete records including private inputs, compute metrics with uncertainty | CLI batch runner, export |
| **Developer** | Run the full stack locally, extend policies or scenarios, verify invariants with tests, deploy to Sepolia from a manifest | Repository, Compose, Foundry, runbook |

In v0.1 the same person typically plays all three roles. Nothing in the design assumes adversarial separation between the operators of the two agents.

## 4. Scope

### 4.1 In scope (v0.1)

- Exactly two counterparties (buyer, seller) and one active run at a time.
- One mock ERC-20 asset `mASSET` and one mock settlement token `mUSD`, both with 6 decimals.
- Fixed asset quantity per run; the total mUSD payment is negotiated.
- Independent agent context, mandate, and signing identity for each side.
- Public on-chain recording of valid offers, acceptance, closure, expiry, abort, and settlement.
- Local EVM (Anvil, chain ID 31337) and Ethereum Sepolia (chain ID 11155111).
- React interface: run setup, live timeline, settlement panel, observer-only mandate reveal, metric strip, replay, export.
- Persistent run history, deterministic replay, JSON evidence export.
- Two policy kinds per side: deterministic baseline and live model.
- Bounded batch evaluation mode on the local chain with baseline comparison and metrics.
- Operator runbook covering local startup, testnet funding, recovery, replay, and export.

### 4.2 Out of scope (deferred)

Real money, production wallets, external asset prices, bridging, lending, guarantees, insurance, provider procurement, real GPU rental, arbitrary tool execution by agents, multi-party matching, partial fills, quantity negotiation, autonomous strategy training, a general-purpose agent marketplace, free-form chat between agents, and the compute-negotiation experiment in spec Appendix A.

### 4.3 Explicit non-goals

- The sandbox is not a permissionless exchange. Operator power to create and abort sessions is centralized and disclosed.
- The controller never chooses a final price or substitutes a scripted agreement for a failed negotiation.
- The system never silently alters a model's proposed price.

## 5. Default scenario

All values are manufactured experiment inputs. Scenario files live in `scenarios/` and are validated against a JSON schema in `packages/protocol/`.

| Parameter | Default |
|---|---|
| Asset quantity | 10 mASSET (`10000000` minor units) |
| Buyer reservation price and hard spending limit | 100 mUSD total |
| Seller reservation price | 90 mUSD total |
| Buyer initial inventory | 250 mUSD, 0 mASSET |
| Seller initial inventory | 25 mASSET, 0 mUSD |
| Seller minimum remaining inventory | 10 mASSET |
| Token decimals | 6 for both tokens |
| First proposer | Buyer |
| Maximum recorded offers | 8, including counteroffers |
| Session duration | 1,800 s from creation on Sepolia |
| Offer lifetime | up to 600 s, capped by session expiry |
| Model request timeout | 45 s |
| Model repair attempts | 1 after an invalid response |
| Testnet confirmation threshold | 2 |
| Local confirmation threshold | 1 |
| Per-run model-call ceiling | 20 |
| Per-run model-spend ceiling | USD 2.00 |
| Buyer allowance to exchange | 250 mUSD |
| Seller allowance to exchange | 25 mASSET |

The feasible interval is 90 to 100 mUSD. Only the offline evaluator may compute it. An **infeasible clone** with seller minimum 105 mUSD demonstrates a correct no-deal outcome.

## 6. Functional requirements

Requirement IDs are stable and referenced from [test_strategy.md](test_strategy.md). "Must" requirements gate the v0.1 release.

### 6.1 Run setup (FR-S)

| ID | Requirement | Spec ref |
|---|---|---|
| FR-S1 | The operator must be able to choose a scenario, execution environment (local or Sepolia), a policy for each side (deterministic or model), a model identifier, maximum offers, session duration, and offer lifetime. | 3.1 |
| FR-S2 | Each mandate must be edited in a separate editor and stored as a versioned record accessible only to the operator and to the relevant agent service. | 3.1, 9.1 |
| FR-S3 | **Validate setup** must check configuration, test funds and allowances, signer availability, chain ID, contract addresses and code hashes against the deployment manifest, RPC access, and availability of the configured model. It must not reveal one mandate to the other agent and must not reject an economically infeasible scenario. | 3.1 |
| FR-S4 | The run controller must refuse to start unless chain ID is 31337 or 11155111 and token and exchange bytecode match the deployment manifest. | 8 |
| FR-S5 | Mandates must be immutable during an active run. Changing any limit must create a new run with a new session ID and new mandate versions via **Clone with changes**; the previous run stays intact. | 3.3 |
| FR-S6 | Each new run must use fresh test-only participant wallets funded by the operator with mock balances and enough test ETH for approvals. Allowances must be finite and granted only to the deployed exchange. | 8 |

### 6.2 Run control (FR-C)

| ID | Requirement | Spec ref |
|---|---|---|
| FR-C1 | **Start** prepares the chain session if needed and schedules automatic execution. | 3.1, 10.1 |
| FR-C2 | **Step** prepares the chain session if needed, advances exactly one agent decision and its resulting transaction through the confirmation threshold, and leaves automatic execution paused. It is unavailable while a turn is running. | 3.3 |
| FR-C3 | **Pause** stops requesting new decisions. It does not stop chain time or undo a broadcast transaction, and the UI must disclose that offer and session expiry continue. | 3.3, 9.4 |
| FR-C4 | **Resume** reconciles canonical chain state and continues. | 10.1 |
| FR-C5 | **Abort** stops decisions and requests on-chain abort if the session is still open and before its deadline. An elapsed deadline is recorded as expiry, not abort. | 3.3, 7.4 |
| FR-C6 | Only one run may be active at a time. Only one signed action may be pending per session. | 2.1, 8 |
| FR-C7 | All mutation routes must accept idempotency keys, and long-running actions must return an operation ID immediately. | 10.1 |

### 6.3 Agent decisions and authority (FR-A)

| ID | Requirement | Spec ref |
|---|---|---|
| FR-A1 | An agent may only: propose a payment, counter the current offer, accept the current unexpired opposing offer, or close. | 5.1 |
| FR-A2 | An agent's observation must contain only: its own mandate and balances, the public session configuration, confirmed structured offers and closures, remaining offer opportunities, current chain time, and its own previous decisions. | 5.2 |
| FR-A3 | An agent must never receive the opponent's mandate, private validation errors, model explanations, prompts, decision seed, or credentials, and must have no filesystem, browser, SQL, shell, HTTP, or contract-call tools. | 5.2 |
| FR-A4 | The model must return exactly one of three decision shapes (`offer`, `accept`, `walk_away`) with amounts as base-10 integer strings in minor units. Extra fields are rejected. An optional operator-facing explanation is carried in a separate envelope field, never inside the decision and never shown to the counterparty. **[assumption]** | 5.4, 3.2 |
| FR-A5 | The policy signer must validate role, session configuration, chain, contract, sequence, action legality, positive integer quantities, expiry, private reservation limits, available balances, and minimum remaining inventory before signing any action, including offers. | 5.3 |
| FR-A6 | The signer must construct the typed message itself from validated state and never sign a model-supplied payload or destination. | 5.3, 5.4 |
| FR-A7 | A mandate-violating model response must be rejected with private feedback and allowed exactly one repair. After that the run is classified as a model failure and the session aborted with reason `model_failure`. It must never be counted as a walk-away. | 5.3, 9.4 |
| FR-A8 | Walk-away reasons are limited to `terms_unacceptable` (1), `inventory_constraint` (2), and `no_further_concession` (3). They are recorded as the agent's statement, not fact. | 5.4 |
| FR-A9 | Per-run model-call ceiling (default 20) and model-spend ceiling (default USD 2.00) must be checked before each call using a conservative token bound and an operator-maintained price table. Unknown cost is displayed as unknown, never zero. | 8 |

### 6.4 Negotiation protocol and settlement (FR-P)

See [protocol.md](protocol.md) for the normative definition.

| ID | Requirement | Spec ref |
|---|---|---|
| FR-P1 | The buyer has the first turn; the initial offer has sequence 1; each subsequent signed action uses sequence + 1. | 6.1 |
| FR-P2 | Once an offer exists, only the counterparty may counter or accept. Either party may close while the session is open. | 6.1 |
| FR-P3 | A counteroffer replaces the current offer; earlier offers can never be accepted. Self-acceptance is invalid. | 6.1 |
| FR-P4 | After `maxOffers` recorded offers, the counterparty may still accept the last offer or close, but may not offer. | 6.1 |
| FR-P5 | Acceptance and both token transfers execute in one transaction; there is no accepted-but-unfunded state. | 6.1, 7.5 |
| FR-P6 | Offer `validUntil = min(latest_chain_timestamp + offer_lifetime, session.expiresAt)`. Recording and accepting require `block.timestamp < validUntil <= expiresAt`. | 6.2 |
| FR-P7 | Session expiry is recorded on-chain by anyone via `expireSession` after the deadline. No signature revives an expired session. | 6.2 |
| FR-P8 | Settlement reverts entirely if either transfer fails. Final token deltas equal the agreed amounts; the submitter receives nothing. | 7.5 |
| FR-P9 | Duplicate actions, stale digests, wrong domain, wrong chain or contract, wrong participant, and post-terminal actions must be rejected by the contract. | 7.5, 12 |

### 6.5 Persistence, recovery, and evidence (FR-E)

| ID | Requirement | Spec ref |
|---|---|---|
| FR-E1 | All records in spec 9.1 must be durable in PostgreSQL with unique identifiers preventing duplicate action insertion and duplicate outbox submission. | 9.1 |
| FR-E2 | Signed intents and outbox records must be persisted before broadcast. A missing RPC response must trigger receipt lookup and rebroadcast of the stored transaction, never a new model decision. | 9.2, 9.4 |
| FR-E3 | Transaction status must be displayed as Submitted, Included, Confirmed (at threshold), and Finalized separately. Finalized requires an RPC finalized-head indication. | 9.3 |
| FR-E4 | On a detected reorganization, dependent projections and balance snapshots are invalidated, new decisions pause, and the projection rebuilds from the last common block. | 9.3 |
| FR-E5 | After a backend restart, the controller must recover lease, outbox, receipts, and canonical state before continuing (acceptance A13). | 9.4 |
| FR-E6 | An unresolved RPC outage is `RECOVERY_REQUIRED`, never a no-deal result. An execution failure followed by abort is an operational failure, never a bargaining outcome. | 9.4 |
| FR-E7 | Replay must reproduce the recorded timeline and balances from saved decisions and canonical events with zero model calls and zero transactions. A fresh model rerun is a new run. | 3.3, 11.3 |
| FR-E8 | JSON evidence export must omit private mandates, private observations, seeds, and credentials by default and include them only on explicit operator request. | 10.1, 11.3 |
| FR-E9 | The public negotiation and accepted terms must be reconstructible from events and calldata without the application database (A15). | 7.6, 12 |
| FR-E10 | Every record needed for reproducibility in spec 11.3 (scenario inputs, mandate versions, policy version, prompt-template version, model ID, effort or sampling settings, token counts, timestamps, raw decisions, deployment manifest) must be stored per run. | 11.3 |

### 6.6 Interface (FR-U)

| ID | Requirement | Spec ref |
|---|---|---|
| FR-U1 | Two agent panels show role, public holdings, current public action, and decision status. | 3.2 |
| FR-U2 | A negotiation timeline shows offer amount, actor, reference offer, sequence, transaction status, and explorer link. | 3.2 |
| FR-U3 | A settlement panel shows current offer, expiry, both authorizations, and before/after balances. | 3.2 |
| FR-U4 | An observer-only control reveals private mandates. The data must never enter the opposing agent's context and the request must carry explicit operator intent. | 3.2, 10.1 |
| FR-U5 | A metric strip shows recorded offers, elapsed decision time, chain wait time, model cost (estimated and reported separately), and outcome. | 3.2, 8 |
| FR-U6 | Structured actions render as plain sentences, for example "Buyer offers 94 mUSD for 10 mASSET." | 3.2 |
| FR-U7 | The UI must label all balances as test assets, all economics as simulated, whether decisions are live or replayed, whether a run used a deterministic fixture, and that the operator holds centralized abort power. | 3.1, 8, 12 |
| FR-U8 | Invalid raw model responses must be labelled as private operational records that were never authorized offers. | 7.6 |
| FR-U9 | The browser never signs trades and never holds private keys or model credentials. | 4.3, 8 |

### 6.7 Evaluation (FR-V)

| ID | Requirement | Spec ref |
|---|---|---|
| FR-V1 | A deterministic policy implementing the linear concession rule in spec 11.1 must be available for either side and must receive exactly the same public information as a model policy. | 11.1 |
| FR-V2 | The batch runner must execute the four pairings (det/det, model/det, det/model, model/model) over a pre-generated scenario population on the local chain and preserve every run, including failures. | 11.1, 11.2 |
| FR-V3 | All metrics in spec 11.2 must be computed and reported with distributions and uncertainty, with simulated mUSD utility and real USD model charges reported separately. | 11.2 |
| FR-V4 | The initial research population is at least 30 overlap and 30 non-overlap scenarios with three model repetitions per pairing. | 11.2 |

## 7. Non-functional requirements

| ID | Requirement |
|---|---|
| NFR-1 | **Integrity over availability.** When in doubt about chain state, the system pauses and requires operator action rather than guessing. |
| NFR-2 | **Integer arithmetic** for all token amounts end to end; conversion to decimal display only at the UI boundary. |
| NFR-3 | **Field-name alignment** across Python, TypeScript, and Solidity via versioned schemas in `packages/protocol/`. |
| NFR-4 | **Secrets** (private keys, model credentials) live only in server-side configuration and never appear in prompts, browser bundles, exports, or ordinary logs. |
| NFR-5 | **Footprint.** Runs on a 2 to 4 vCPU, 8 GB host or a developer workstation. No local GPU. |
| NFR-6 | **Local-only binding** by default. Remote exposure requires operator authentication and TLS. |
| NFR-7 | **Reproducible dependencies.** Lockfiles committed for Python, Node, and Foundry. Compiler and optimizer settings recorded in the deployment manifest. |
| NFR-8 | **Observability.** Structured logs with run ID and turn ID on every line; secrets redacted at the logger. |
| NFR-9 | **Latency budget.** A local-chain turn (decision, sign, broadcast, 1 confirmation) completes in under 90 s at the 45 s model timeout. Not a hard limit; measured and reported. |
| NFR-10 | **Code quality** per [engineering-principles.md](engineering-principles.md) and [contributing.md](contributing.md). |

## 8. Success metrics

Reported per batch and per run. Definitions are normative in spec 11.2.

- Feasible-scenario settlement rate (with infrastructure failures reported separately and in the all-run denominator).
- Infeasible-scenario trade rate, target zero.
- Mandate violations, target zero.
- Buyer and seller reservation utility, captured surplus, and efficiency ratio (undefined when feasible surplus is zero).
- Negotiation cost (calls, tokens, USD estimated and reported, decision time).
- Chain cost (gas, test-ETH fee, inclusion and confirmation wait; setup shown separately).
- Operational failure rate by cause.
- Audit completeness: every authorized action and final balance reconciles with canonical chain evidence.

## 9. Release criteria for v0.1

All of the following, verified per [test_strategy.md](test_strategy.md):

1. Acceptance tests A01 through A17 pass.
2. A genuine live-model settlement and a genuine live-model no-deal run are demonstrated on the local chain, and a selected set on Sepolia.
3. Replay and export preserve the evidence of those runs.
4. The operator runbook covers local startup, testnet funding, recovery of pending transactions, replay, and export.
5. No hardcoded agreement exists anywhere in the codebase to make the live demo pass. A model that cannot settle reliably is a reported result.

## 10. Risks and mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Model returns malformed or out-of-mandate decisions frequently | Runs abort as model failures; demo looks broken | Structured outputs with a strict schema, one repair with validation feedback, deterministic fallback pairing for the presentation, report the failure rate honestly |
| Sepolia congestion or RPC instability | Turns stall; offers expire | Durable outbox with rebroadcast, generous session duration, local chain for batches, `RECOVERY_REQUIRED` state with runbook procedure |
| Reorg on Sepolia | Displayed success disappears | Block-hash tracking, canonical flag, projection rebuild, A14 simulation on Anvil |
| Information leak between agents | Invalidates the core claim | Allowlisted observation schema, separate processes and credentials, A12 outbound-context assertion tests |
| Signature or replay bug in the contract | Assets move on wrong terms | OpenZeppelin primitives, EIP-712 with in-protocol replay prevention, fuzz and invariant tests, no custom crypto |
| Model provider API drift | Agent service breaks | Pin SDK version, isolate the model client behind an interface, record model ID per run |
| Testnet reset | Public evidence lost | Preserve exports; exports are the archive of record |

## 11. Glossary

| Term | Meaning |
|---|---|
| **Run** | One application-level negotiation attempt with fixed scenario, mandates, policies, and a single on-chain session. |
| **Session** | The on-chain record identified by `sessionId`, created by the operator and terminated by settlement, closure, expiry, or abort. |
| **Mandate** | A party's private instructions and limits: reservation price, inventory floor, and strategy guidance. Never shared with the counterparty. |
| **Policy** | The component that produces a decision from an observation: deterministic baseline or live model. |
| **Policy signer** | The component inside an agent service that validates a decision against mandate and public state, constructs the typed message, and signs it. |
| **Observation** | The allowlisted public-plus-own-private view supplied to a policy for one turn. |
| **Decision** | The structured output of a policy: `offer`, `accept`, or `walk_away`. |
| **Signed action** | An EIP-712 typed message (Offer, Accept, Close) plus signature, identified by its digest. |
| **Offer digest / offerHash** | The full EIP-712 digest of a recorded Offer including domain; the reference an Accept points to. |
| **Sequence** | The strictly increasing per-session counter consumed by every participant-signed action. |
| **Relay** | The backend module that submits signed actions with its own gas-paying key. Cannot create agent signatures. |
| **Indexer** | The backend module that reads receipts and logs, tracks canonicality, and projects chain events into the database. |
| **Outbox** | The durable table of raw signed transactions awaiting or undergoing broadcast. |
| **Operational state** | The application's lifecycle state of a run (setup, deciding, pending signature, pending tx, confirming, paused, recovery). |
| **Economic outcome** | The contract's terminal financial result: settled, closed, expired, aborted. Never inferred from operational state. |
| **Deployment manifest** | The recorded chain ID, addresses, code hashes, compiler settings, and operator address for a deployment. |
| **Replay** | Rendering a saved run from recorded decisions and canonical events without model calls or transactions. |
| **Minor units** | Integer token amounts at 6 decimals; 94 mUSD is `94000000`. |
