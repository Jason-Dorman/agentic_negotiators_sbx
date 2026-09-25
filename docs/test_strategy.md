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
| A17 | Public demo identity matches manifest | Manual + automated | `validation/test_manifest.py`, runbook step | Automated: code hash and chain ID check refuses mismatched deployment; where `manifest.ens` is present, each recorded name is resolved once at deployment time and compared with its manifest address, a mismatch producing a manifest warning and never a run failure. Manual: explorer links open the manifest addresses |

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

Handler-based invariant test with random valid and invalid actions across many sessions, in
`contracts/test/invariant/`. Alongside it, `Fuzz.t.sol` holds the stateless properties: the unit
suite asks whether `maxOffers = 33` reverts, the fuzz suite asks whether *any* value outside 1..32
does not.

Three things about the handler are load-bearing rather than incidental, and each of them was
learned by watching a mutation survive:

- **It is right about three quarters of the time.** A handler that chose the proposer and the actor
  by coin flip halved the chance of each subsequent step being legal, and a 32-call run then
  essentially never reached a settlement — so invariant 5's *settled* branch was never evaluated and
  the suite passed on its unsettled branch alone. Deviating a quarter of the time keeps the illegal
  paths covered while letting runs get deep enough to trade. Depth is 64 for the same reason.
- **Ghost state moves only on success, and a terminal status is written once.** Following the chain
  instead is how a mutation escaped: with the status check removed from `expireSession`, an
  already-settled session could be re-marked Expired, the handler dutifully recorded Expired, and
  the monotonicity invariant compared the chain against itself and passed.
- **Sessions have uneven and deliberately tiny offer limits (1, 2, 8).** With `maxOffers = 8`
  everywhere, reaching `OfferLimitReached` needs nine accepted offers in one session, which a
  64-call run over six sessions does not produce; invariant 3 was unfalsifiable and deleting the
  limit check survived.

Each session owns its own wallet pair, so a balance delta is attributable to one settlement.

A05 gets a dedicated adversarial action (`submitStaleAccept`) rather than a branch of the ordinary
acceptance, because as a branch it was unreachable in practice: the valid acceptance is drawn three
times as often and settles the session first.

**Two states the campaign cannot be trusted to reach are seeded in the handler's constructor**, and
both were added because a mutation was caught on some campaigns and not others. A mutation test that
passes four times in five is worse than none, because it will be believed.

| Seed | The state | The mutation it makes falsifiable |
|---|---|---|
| Slot 2 records two offers | one **displaced** digest exists from call one | deleting the `StaleOfferDigest` check |
| Slot 6 is opened with a 120 s life and closed at once | a session that is **terminal** and, after one time advance, **past its deadline** | deleting `expireSession`'s status check |

The second is the sharper illustration. To catch that mutation unaided, a campaign had to terminate
a session, push chain time past that session's expiry, *and* then call `expireSession` on that same
session — a conjunction the fuzzer reached about five times in six. Seeded, the mutation fails the
invariant suite on every run.

That is the general shape of every handler property above: the fuzzer explores, and the
*reachability of the interesting state* is arranged deliberately rather than hoped for.

The properties:

- Status is monotonic: once non-Open, never changes.
- `sequence` strictly increases by exactly 1 per successful participant action.
- `offerCount <= maxOffers`.
- Sum of buyer and seller balances per token is constant across settlement (conservation).
- If Settled, exactly one settlement transfer pair happened and its amounts equal the last recorded offer's `quoteAmount` and `baseAmount`.
- A digest that was ever replaced is never accepted.
- Random signature mutation never succeeds. Six kinds, all relative to the key the message
  actually names: a flipped bit in `r`, a flipped bit in `s`, a swapped `v`, a stranger's key, the
  counterparty's key, and a signature over a forged domain separator for another chain and
  contract. The first version of this helper always mutated the buyer's signature, so for an
  acceptance — where the seller names itself — the "counterparty key" case handed back a perfectly
  valid signature, and the invariant correctly reported that a forgery had settled a session.

**The suite is confirmed by mutation, not by coverage.** Eight mutations to `NegotiationExchange`
were applied one at a time and the invariant suite alone run against each: settlement not terminal,
the settlement legs' **amounts** swapped, the stale-digest check deleted, the signature check
deleted, the sequence advanced by two, the offer limit not enforced, the active offer left set after
settlement, and the status check removed from `expireSession`. All eight fail it. Three survived the
suite's first version, and the three handler properties above are what closed them.

Two limits of that claim, both worth stating because a reader would otherwise over-read it.

*The invariant suite cannot distinguish a broken settlement from a campaign that never settled.*
"Legs swapped" was verified for the amounts. Swapping the legs' **direction** or their **token** is
caught by `Accept.t.sol`, which asserts exact deltas deterministically, rather than reliably by the
invariant suite — whose settled branch is only evaluated in runs where a settlement happened. This
is the same seed-dependence that makes a liveness assertion in `afterInvariant` unacceptable, and it
is why the deterministic unit suite is not redundant with the property suite. The division of labour
is worth stating: **the unit suite is what catches a mutation every time; the invariant suite is what
catches one nobody thought to write a unit test for.** Where only the second would notice something,
the state it needs is seeded rather than left to the seed.

*Two further mutations were found by the stage 1 adversarial review and are now caught by the unit
suite rather than the invariant one:* emitting a constant `SessionClosed.reason` (no test asserted
the emitted reason for codes 2 or 3), and adding an arbitrarily named escape-hatch function such as
`adminSettle` (the ABI suite asserted a fifteen-name denylist rather than an exact function set).
Both are now pinned by exact assertions, and both were verified to fail by mutation.

### 4.3 Gas snapshot

`contracts/.gas-snapshot` committed; CI fails on more than 10 percent regression without a
decision-log note (`forge snapshot --check --tolerance 10`). The snapshot covers the deterministic
unit tests only (`--no-match-path "test/invariant/*"`): fuzz and invariant gas figures move with the
seed, so including them would make the gate report noise as a regression and train people to
override it. `make snapshot` rewrites it.

## 5. Protocol fixtures

`packages/protocol/fixtures/eip712.v1.json` holds: the domain and its separator, the three
byte-exact type strings and their type hashes, a session config and its expected `configHash`, and
an Offer, Accept and Close each with its signing key, expected digest and expected signature. It is
written by `packages/protocol/tools/generate_eip712_fixtures.py`, which computes every value by hand
from [protocol.md](protocol.md) sections 3 and 4.

**Four independent EIP-712 implementations are checked against it**, not three:

| Implementation | Where | How |
|---|---|---|
| The generator's own | `negotiation_protocol.eip712` | keccak and `abi.encode` from the document |
| `eth_account` | `packages/protocol/tests/test_eip712_fixtures.py` | `encode_typed_data`, then sign and recover |
| `viem` | `packages/protocol/tests/eip712.test.ts` | `hashTypedData`, `hashDomain`, `recoverTypedDataAddress` |
| OpenZeppelin | `contracts/test/unit/Fixtures.t.sol` | the contract's own `hashOffer`, `hashAccept`, `hashClose` |

A mismatch in any language fails the build. This is the guard for field-name and type alignment, and
each suite goes past digest equality in its own way. Python derives the typed-data structure *from
the fixture's own type strings*, so a field renamed on one side leaves a name with no counterpart
rather than a digest that quietly differs. Foundry places the contracts at the fixture's own
addresses with `vm.deployCodeTo` and `vm.chainId` — the separator includes both, so a digest can
only be reproduced by a contract that actually lives there — and then **replays the fixture's Offer
and Accept with the fixture's own signatures** and asserts the settlement moved the signed amounts.
A fixture whose digests matched but whose signatures the contract refused would be a fixture of an
action that cannot happen.

Every value in the file is determined by the domain, the type strings and the `configHash`
encoding, so **a change to it is a protocol version bump** ([protocol.md](protocol.md) section 15),
never a regeneration. CI regenerates it and fails on any diff; `make fixtures` is what a deliberate
bump runs.

The reason-code tables get the same treatment. `packages/protocol/fixtures/reason_codes.v1.json` is
the single source, and the Python and TypeScript tables are each checked against it, so a code added
in one language alone fails the build rather than producing a timeline sentence that describes a
different walk-away than the chain recorded.

The committed ABI artefacts in `packages/protocol/abi/` are checked the same way
(`export_abi.py --check`): a stale copy would have the indexer decoding events against an ABI the
contract no longer has. `packages/protocol/tests/test_abi.py` additionally asserts every event's
field order and `indexed` flags, every error's parameter types, and the **exact function set**
against the text of [protocol.md](protocol.md) — an exact set rather than a denylist, because a
denylist of fifteen names let an escape hatch called anything else through.

The observation schema gets the same treatment from the other direction. `test_schemas.py` asserts
that its field names *are* section 12's, transcribed by hand from the document rather than derived
from the schema, and that every object in it closes its properties recursively. Every other schema
test asks whether a *value* is refused; these ask whether the field list is the document's, which is
a different question — and the one that caught a `reason_code` in `historyEntry` that section 12 does
not list, inside the schema that declares itself that section's exhaustive form.

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
| Coverage: contracts 100 percent lines on `NegotiationExchange` (live from stage 1); agent validator and signer 100 percent branches, backend 85 percent lines (stage 2) | merge |
| Protocol artefacts match their generators: committed ABIs and the EIP-712 fixture | merge |

`make ci` runs **every** gate in this table that exists yet, including the gas snapshot and the
contract coverage threshold. It did not, briefly, and the consequence was the thing this table is
supposed to prevent: the gas-snapshot job was enabled in stage 1 while no local command ran it, so
it went unnoticed that the snapshot predated `Fixtures.t.sol` and that
`forge snapshot --check … --root contracts` resolves the snapshot path against the *working
directory* rather than against `--root` — reading a non-existent file at the repository root. The
gate would have failed on the branch that introduced it. Both callers now name
`contracts/.gas-snapshot` explicitly, and `make ci` depends on `make gates`.

The coverage threshold's assertion lives in `infra/scripts/check_contract_coverage.py` with its own
tests, not in a heredoc inside the workflow, for the same reason: it is the gate's only real
assertion, and the failure that matters is a *missing* row reading as a pass. Its `forge coverage`
output is redirected rather than piped, because `run:` is `bash -e` without `-o pipefail` and a pipe
would hand the step `tee`'s exit status.

The contract half of the coverage gate is enforced from stage 1, when the contract exists, and
checks all four figures `forge coverage` reports — lines, statements, branches and functions — at
100 percent rather than lines alone. The other two thresholds are turned on with the code they
measure.

The coverage thresholds were confirmed by the product owner on 21 September 2026. They are deliberately uneven: the contract and the two components that convert a model's words into authority are the places where a missed branch is an incorrect transfer, while the backend's remaining 15 percent is mostly error plumbing that integration tests exercise end to end.

## 11. Test data and fixtures policy

- Scenario populations for batches are generated from a seed and committed as JSON so results are reproducible.
- Canned model responses used in tests live under `tests/fixtures/model_responses/` and are labelled `fixture` in any run they produce.
- No test may hardcode a final price into application code. A grep test fails the build if any non-test module contains the literal default reservation values in a comparison.
