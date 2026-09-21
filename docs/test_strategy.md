# Test Strategy and Acceptance Plan

| | |
|---|---|
| **Version** | 0.1.0 |
| **Date** | 19 September 2026 |
| **Status** | Draft for build |
| **Source** | Spec section 12; [engineering-principles.md](engineering-principles.md) testing mindset |
| **Related** | [prd.md](prd.md), [protocol.md](protocol.md), [security_and_trust_boundaries.md](security_and_trust_boundaries.md), [build_plan.md](build_plan.md) |

---

## 1. Principles

1. **Tests first where behavior is specified.** The protocol, contract, and policy signer are fully specified. Write their tests before the implementation.
2. **Verification concentrates on authority, isolation, state transitions, transfers, and recovery.** UI polish is verified by a short browser pass, not an exhaustive suite.
3. **Fakes over mocks.** `ModelClient`, `ChainAdapter`, `KeyHolder`, and repositories have in-memory fakes. HTTP is not mocked inside unit tests.
4. **A fixture is labelled a fixture.** Deterministic-policy runs and canned model responses are never presented as autonomous model behavior, in tests or in demos.
5. **Every acceptance criterion maps to at least one automated test**, except A17 which is a manual manifest check on Sepolia plus an automated manifest comparison.
6. **Every regression gets a test** before the fix.

## 2. Test layers

| Layer | Tooling | Location | Runs on | Speed target |
|---|---|---|---|---|
| Contract unit | Foundry `forge test` | `contracts/test/unit/` | every commit | < 30 s |
| Contract fuzz and invariant | Foundry fuzz, invariant handlers | `contracts/test/invariant/` | every commit (short), nightly (long) | < 2 min short |
| Protocol fixtures | pytest + vitest + forge reading the same JSON | `packages/protocol/tests/` | every commit | < 10 s |
| Python unit | pytest, fakes | `services/*/tests/unit/` | every commit | < 60 s |
| Python integration | pytest, PostgreSQL (testcontainers or compose), Anvil | `services/api/tests/integration/` | every commit | < 5 min |
| Isolation and leakage | pytest, scanners over captured requests, logs, SSE, exports | `services/api/tests/isolation/` | every commit | < 1 min |
| Web unit | vitest, React Testing Library | `apps/web/src/**/*.test.tsx` | every commit | < 60 s |
| End-to-end browser | Playwright against local compose profile | `apps/web/e2e/` | on merge to main and before release | < 10 min |
| Sepolia demonstration | Manual runbook plus automated manifest check | `docs/runbook.md` | before release | n/a |
| Batch evaluation | CLI over Anvil | `services/api/eval` | stage 6 | hours |

## 3. Acceptance criteria mapping

| ID | Criterion (spec 12) | Layer | Test location | Notes |
|---|---|---|---|---|
| A01 | Standard overlap scenario: settle or leave; any settlement respects both mandates and exact deltas | Integration + live model | `integration/test_a01_overlap.py` | Runs det/det automatically; model/model run is recorded evidence, asserted by the invariant checker over its export |
| A02 | Deterministic policies settle a known feasible scenario end to end | Integration | `integration/test_a02_det_settlement.py` | Also the smoke test for the whole stack |
| A03 | Buyer 100, seller 105: no settlement, proper terminal reason | Integration | `integration/test_a03_infeasible.py` | Assert `outcome_kind in (closed, expired)` and never `settled`; assert reason code recorded |
| A04 | Out-of-bound model proposal refused; no broadcast | Unit (agent) + integration | `agent/tests/unit/test_validator.py`, `integration/test_a04_out_of_bound.py` | Fake model returns 85 for a seller with floor 90; assert no outbox row, one repair, then abort reason 2 |
| A05 | Counteroffer then accept of old offer rejected | Contract | `contracts/test/unit/Accept.t.sol` | `StaleOfferDigest` |
| A06 | Duplicate signature/action and second settlement attempt | Contract + integration | `Replay.t.sol`, `integration/test_a06_duplicate.py` | Contract: `SequenceMismatch` / `SessionNotOpen`; app: outbox uniqueness reconciles as already complete |
| A07 | Wrong chain, contract, session, configHash rejected | Contract | `Domain.t.sol` | Sign against a second deployment and a forged domain |
| A08 | Self-acceptance or wrong participant rejected | Contract | `Roles.t.sol` | `SelfAcceptance`, `NotParticipant`, `BadSignature` |
| A09 | Expired offer/session; offer-count boundary | Contract | `Expiry.t.sol`, `OfferLimit.t.sol` | `vm.warp`; eighth offer acceptable, ninth reverts `OfferLimitReached` |
| A10 | Second transfer fails: whole settlement reverts | Contract | `Atomicity.t.sol` | Revoke seller allowance after offer; assert both balances unchanged and status still Open |
| A11 | Close or abort before acceptance: later settlement rejected | Contract + integration | `Terminal.t.sol`, `integration/test_a11_abort_race.py` | Integration races abort and accept on Anvil with automine off |
| A12 | Prompt and context isolation | Isolation | `isolation/test_a12_leakage.py` | Capture every outbound model request via fake client; scan for opponent mandate values, feedback, keys, credentials; also scan SSE and default export |
| A13 | Crash after broadcast, before receipt persistence | Integration | `integration/test_a13_crash_recovery.py` | Kill the turn coroutine between `send_raw_transaction` and receipt persist; restart controller; assert one settlement, one tx |
| A14 | Local reorg simulation | Integration | `integration/test_a14_reorg.py` | Anvil `evm_snapshot`, index a settlement, `evm_revert`, mine a different block; assert projection rollback, run paused, UI state removes success |
| A15 | Event and calldata reconstruction | Protocol tool + integration | `packages/protocol/tests/test_reconstruct.py` | Run reconstruction against Anvil with the database dropped; compare to the export |
| A16 | Replay: identical timeline and balances, no model calls or txs | Integration + web | `integration/test_a16_replay.py`, `e2e/replay.spec.ts` | Fake model call counter and fake RPC send counter must both be zero |
| A17 | Public demo identity matches manifest | Manual + automated | `validation/test_manifest.py`, runbook step | Automated: code hash and chain ID check refuses mismatched deployment. Manual: explorer links open the manifest addresses |

## 4. Contract test plan

### 4.1 Unit

- `createSession`: each validation error; duplicate `sessionId`; event fields; `configHash` equals the reference fixture.
- `recordOffer`: buyer first; sequence rules; turn rule after offer and after expired offer; `quoteAmount == 0`; `validUntil` bounds; offer count boundary; event fields; digest equals `hashOffer` and the off-chain fixture.
- `acceptAndSettle`: happy path with exact deltas; stale digest; expired offer; self-acceptance; wrong sequence; wrong config hash; wrong domain; non-participant; second call reverts `SessionNotOpen`; submitter receives nothing; third-party submitter produces identical result.
- `closeSession`: either party at any turn; reason codes 1..3; undefined code; after terminal.
- `expireSession`: before deadline reverts; after deadline succeeds; idempotent revert after; blocks close and abort after deadline.
- `abortSession`: only operator; codes 1..4; after deadline reverts.
- Atomicity: revoke allowance or drain balance between offer and accept; both legs unchanged.

### 4.2 Fuzz and invariant

Handler-based invariant test with random valid and invalid actions across many sessions:

- Status is monotonic: once non-Open, never changes.
- `sequence` strictly increases by exactly 1 per successful participant action.
- `offerCount <= maxOffers`.
- Sum of buyer and seller balances per token is constant across settlement (conservation).
- If Settled, exactly one settlement transfer pair happened and its amounts equal the last recorded offer's `quoteAmount` and `baseAmount`.
- A digest that was ever replaced is never accepted.
- Random signature mutation (bit flips, wrong key, wrong domain) never succeeds.

### 4.3 Gas snapshot

`forge snapshot` committed; CI fails on more than 10 percent regression without a decision-log note.

## 5. Protocol fixtures

`packages/protocol/fixtures/` holds JSON with: a session config and its expected `configHash`; an Offer, Accept, and Close with private key, expected digest, and signature; the domain. Three test suites read the same file:

- Foundry computes `hashOffer` and compares.
- Python (`eth-account`) signs and compares digest and signature.
- TypeScript (`viem` or `ethers`) computes the digest and compares.

A mismatch in any language fails the build. This is the guard for field-name and type alignment.

## 6. Agent service tests

- `MandateValidator`: table-driven cases for every validation code: schema error, extra fields, non-integer amount, zero amount, below or above reservation, insufficient balance, inventory floor, wrong turn, offer limit, stale `offer_hash`, accept with no active offer, self-accept, invalid reason string. Each case asserts the private feedback text and that no signing occurred.
- `DeterministicPolicy`: exact expected quotes for `maxOffers` in {1,2,3,8,32} for both sides; acceptance when incoming is within bound; walk-away when no opportunities remain; rounding direction; positive floor.
- `ModelPolicy` with fake `ModelClient`: valid first attempt; invalid then valid repair; invalid twice → `model_failed`; timeout; `refusal` stop reason; `max_tokens` stop reason; provider error. Assert the repair message contains only that agent's feedback.
- `BudgetGuard`: call ceiling; spend ceiling with a price table; unknown price → call refused unless operator sets `allow_unknown_price`; estimated versus reported separation.
- `Signer`: reconstructs typed message from validated state only; refuses to sign when session approval is missing; digest matches fixture.
- Internal API: HMAC rejection; provisioning idempotency; `approve-session` mismatch detection; `release` discards state.

## 7. Backend tests

- Controller state machine: every transition in [architecture.md](architecture.md) section 6.1, including illegal transitions returning `invalid_state`.
- Idempotency: same key same body replays; same key different body conflicts.
- Turn executor with fake agent client and fake chain: the eight steps of spec 9.2 in order; persist-before-broadcast asserted by injecting a crash between them.
- Relay: nonce serialization; rebroadcast with same raw tx; gas replacement creates a new outbox row linked by `replaces_id` and the same `signed_action_id`; partial unique index enforced.
- Indexer: decode all seven events; canonical flag; reorg detection with block-hash mismatch; rebuild.
- Projection: never reads non-canonical rows (grep-level test that all queries go through the filtering repository).
- Observation builder: output validates against `observation.v1.json`; contains only the acting party's mandate; `history` contains only canonical confirmed events.
- Export: default export snapshot contains no private fields (schema test with `additionalProperties: false`); private export contains them only when requested.
- Metrics: hand-computed expectations for a settled run, a closed run, an infeasible run, an aborted run; efficiency undefined when surplus is zero.
- Sentence renderer: one case per timeline kind.

## 8. Web tests

- Unit: amount formatting from minor units; sentence display; state badges; live versus replay labels; test-asset disclaimer present on every route.
- E2E (Playwright, local profile, deterministic policies): create run, validate, step twice, observe two timeline entries with confirmed status, pause notice mentions expiry, start and run to settlement, before and after balances match, open replay and confirm no network calls to model or RPC send, download export and validate against schema, clone with a changed mandate creates a new run with a new session ID and the old run unchanged.

## 9. Live model evidence

Live model runs are not deterministic and are not CI gates. They are **recorded evidence** checked by the invariant checker:

- The batch evaluator writes every run's export.
- `services/api/eval/check_invariants.py` verifies, for every export: no mandate violation, exact deltas on settlement, contiguous sequences, canonical terminal event, no infeasible settlement, and classification of failures.
- The release requires one live settlement and one live no-deal export in `docs/evidence/` that pass the checker and replay in the UI.

## 10. CI gates

| Gate | Blocks |
|---|---|
| Format and lint (ruff, mypy strict, eslint, prettier, forge fmt) | merge |
| Contract unit and short invariant | merge |
| Protocol fixtures in all three languages | merge |
| Python unit and integration (Anvil + PostgreSQL in CI) | merge |
| Isolation and leakage suite | merge |
| Secret scan | merge |
| Web unit | merge |
| Playwright E2E | merge to main |
| Gas snapshot regression | merge, override by decision-log entry |
| Coverage: contracts 100 percent lines on `NegotiationExchange`; agent validator and signer 100 percent branches; backend 85 percent lines | merge |

## 11. Test data and fixtures policy

- Scenario populations for batches are generated from a seed and committed as JSON so results are reproducible.
- Canned model responses used in tests live under `tests/fixtures/model_responses/` and are labelled `fixture` in any run they produce.
- No test may hardcode a final price into application code. A grep test fails the build if any non-test module contains the literal default reservation values in a comparison.
