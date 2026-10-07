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
| Python integration | pytest, PostgreSQL (testcontainers or compose), Anvil | `services/api/tests/integration/`, `services/agent/tests/integration/` | every commit | < 5 min |
| Isolation and leakage | pytest, scanners over captured requests, logs, SSE, exports | `services/api/tests/isolation/` | every commit | < 1 min |
| API contract | pytest: the generated OpenAPI document against a committed snapshot, the served routes against api_contract section 2 | `services/api/tests/contract/` | every commit | < 10 s |
| Web unit | vitest, React Testing Library | `apps/web/src/**/*.test.tsx` | every commit | < 60 s |
| End-to-end browser | Playwright against local compose profile | `apps/web/e2e/` | on merge to main and before release | < 10 min |
| Sepolia demonstration | Manual runbook plus automated manifest check | `docs/runbook.md` | before release | n/a |
| Batch evaluation | CLI over Anvil | `services/api/eval` | stage 6 | hours |

## 3. Acceptance criteria mapping

| ID | Criterion (spec 12) | Layer | Test location | Notes |
|---|---|---|---|---|
| A01 | Standard overlap scenario: settle or leave; any settlement respects both mandates and exact deltas | Integration + live model | `integration/test_a01_overlap.py` | Runs det/det automatically; model/model run is recorded evidence, asserted by the invariant checker over its export |
| A02 | Deterministic policies settle a known feasible scenario end to end | Integration | `services/api/tests/integration/test_a02_det_settlement.py` (stage 2.4, in-process through the controller) | Also the smoke test for the whole stack: 80, 108, 86.666666, 102, 93.333333, accepted; the settlement's balance deltas exactly the two legs; rows in every table the run touches; each wallet funded with exactly its approval's worst case (ADR-065) |
| A03 | Buyer 100, seller 105: no settlement, proper terminal reason | Integration | `services/api/tests/integration/test_a03_infeasible.py` (stage 2.4) | Assert `outcome_kind in (closed, expired)` and never `settled`; assert reason code recorded. The deterministic pair uses all eight offers and the buyer closes with `terms_unacceptable`; no token moves |
| A04 | Out-of-bound model proposal refused; no broadcast | Unit (agent) + integration | `services/agent/tests/unit/test_validator.py` and `test_turns.py` (stage 2.2), `integration/test_a04_out_of_bound.py` | Fake model returns 85 for a seller with floor 90; assert no outbox row, one repair, then abort reason 2 |
| A05 | Counteroffer then accept of old offer rejected | Contract | `contracts/test/unit/Accept.t.sol` | `StaleOfferDigest` |
| A06 | Duplicate signature/action and second settlement attempt | Contract + integration | `Replay.t.sol`; chain layer (stage 2.3) `services/api/tests/integration/test_chain_recovery.py`; run level `services/api/tests/integration/test_a06_duplicate.py` (stage 2.4: a turn advanced again in flight asks for no decision and sends nothing; a settled run driven or resubmitted again sends nothing) | Contract: `SequenceMismatch` / `SessionNotOpen`; app: a second submission resumes the first transaction, a second row for the digest is refused by the database, two racing submitters produce one transaction, and the same signature relayed by anyone else reverts with no transfer |
| A07 | Wrong chain, contract, session, configHash rejected | Contract | `Domain.t.sol` | Sign against a second deployment and a forged domain |
| A08 | Self-acceptance or wrong participant rejected | Contract | `Roles.t.sol` | `SelfAcceptance`, `NotParticipant`, `BadSignature` |
| A09 | Expired offer/session; offer-count boundary | Contract | `Expiry.t.sol`, `OfferLimit.t.sol` | `vm.warp`; eighth offer acceptable, ninth reverts `OfferLimitReached` |
| A10 | Second transfer fails: whole settlement reverts | Contract | `Atomicity.t.sol` | Revoke seller allowance after offer; assert both balances unchanged and status still Open |
| A11 | Close or abort before acceptance: later settlement rejected | Contract + integration | `Terminal.t.sol`, `integration/test_a11_abort_race.py` | Integration races abort and accept on Anvil with automine off |
| A12 | Prompt and context isolation | Isolation | `isolation/test_a12_leakage.py` | Capture every outbound model request via fake client; scan for opponent mandate values, feedback, keys, credentials; also scan SSE and default export |
| A13 | Crash after broadcast, before receipt persistence | Integration | Chain layer (stage 2.3) `services/api/tests/integration/test_chain_recovery.py`; run level `services/api/tests/integration/test_a13_crash_recovery.py` (stage 2.4) | A `BaseException` raised inside the real adapter right after the node accepts the bytes; a new relay recovers with only the database: `mined`, nothing sent, one transaction, one settlement. Also a crash before the send (rebroadcast once) and with the transaction still pooled (left there). Stage 2.4 does the same with the controller restarted: a new process takes the expired lease, `recover` finds the settlement mined, sends nothing, and records one settlement; a live lease is not taken over. `test_turn_persistence.py` kills the process after a turn is persisted and before its transaction exists, and the restarted controller relays the stored action without asking for a new decision |
| A14 | Local reorg simulation | Integration | Chain layer (stage 2.3) `services/api/tests/integration/test_chain_reorg.py`; run level `services/api/tests/integration/test_a14_reorg.py` (stage 2.4) | Anvil `evm_snapshot`, index an offer at threshold 2, `evm_revert`, mine different blocks; assert the event non-canonical, the inclusion cleared, the projection rolled back, the transaction rebroadcast and re-indexed in a new block. Also a settlement's balance snapshots invalidated, and a reorg that removed none of the backend's rows still rescanned. Stage 2.4 adds the run paused with cause `reorg`, acted on once, the offer sent again by reconcile and confirmed while paused, and resume carrying the run to settlement, with the unreverted run as its control |
| A15 | Event and calldata reconstruction | Protocol tool + integration | `packages/protocol/tests/test_reconstruct.py`; run level `services/api/tests/integration/test_api_export.py` (stage 2.5) | Run reconstruction against Anvil with the database dropped; compare to the export. Stage 2.5 takes a real settled run's export over HTTP, validates it with `private: null`, and has the tool, reading only the chain, agree with it on every action's sequence, kind, digest, signer, transaction and typed message, on the outcome and its transaction, on the balance deltas, on the session's events and on each action's calldata |
| A16 | Replay: identical timeline and balances, no model calls or txs | Integration + web | `integration/test_a16_replay.py`, `e2e/replay.spec.ts` | Fake model call counter and fake RPC send counter must both be zero. Stage 4, with the replay route ([ADR-074](decision_log.md)) |
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

Stage 2.2 built the first five items below, in `services/agent/tests/`; stage 3.1 built
`BudgetGuard` and the model client, and `ModelPolicy` arrives in stage 3.2.

- `MandateValidator` (`unit/test_validator.py`): one table, `tests/support/agent_validation_cases.py`, with a row per way a decision can be refused — every code in [protocol.md](protocol.md) section 11.1, the boundaries beside each, and the order of checks where several apply. Each row asserts its code and its private feedback text word for word. `unit/test_turns.py` runs the same table through the turn executor and asserts that nothing was signed and the turn ended `model_failed` with `repair_exhausted`. The structural half is also checked against `agent_decision.v1.json` itself: every schema-valid response parses, and every structural refusal is one the schema makes too, except the two rows where the validator is knowingly stricter.
- `DeterministicPolicy` (`unit/test_deterministic_policy.py`): exact expected quotes for `maxOffers` in {1,2,3,8,32} for both sides, worked by hand rather than recomputed; acceptance when incoming is within bound; walk-away when no opportunities remain, with each of the three reasons ([ADR-043](decision_log.md)); rounding direction; positive floor; clamping to its own bound; every decision it makes passes the validator.
- `ModelPolicy` with fake `ModelClient` (stage 3): valid first attempt; invalid then valid repair; invalid twice → `model_failed`; timeout; `refusal` stop reason; `max_tokens` stop reason; provider error. Assert the repair message contains only that agent's feedback.
- `BudgetGuard` (stage 3.1, `unit/test_budget_guard.py`): call ceiling; spend ceiling with a price table, at the ceiling exactly and a micro-dollar over; unknown price → call refused unless operator sets `allow_unknown_price`, and then only the call ceiling holds; estimated versus reported separation, the spend so far counting reported cost and the estimate of a call that reported no usage ([ADR-085](decision_log.md)). The model price table's arithmetic, worked by hand from the packaged rates, its rounding up, and every way a table file is refused by location.
- The model client (stage 3.1, `unit/test_model_client.py`), against a local HTTP server that answers as the Messages and token-counting endpoints do (`tests/support/fake_anthropic.py`), as the RPC adapter's errors are tested in stage 2.3: every `ModelOutcome` from a real exchange — a decision kept as written, in the model's own key order, a refusal, a truncation at `max_tokens`, eight answers that are not an envelope, a timeout the server causes by not answering and the run's timeout rather than the SDK's, nine error statuses each sent exactly once though the server asks for a retry, eighteen success bodies that are not a message — not JSON, not UTF-8, content of the wrong shape, impossible counts — a redirect not followed, a refused connection, eight failed token counts that send no call, and a call the budget refuses before it is sent — each with its usage and both costs as the guard saw them; cache writes priced at their two durations; a cancelled call charged its estimate; retries off whatever SDK client is given. The request asserted field by field, the schema sent asserted literally — each `action` a one-value `enum`, the close reasons, no `null`, no `default`, no `const` — and what the request must not carry: tools, prefill, fallbacks, a beta header, a key, header or URL from `ANTHROPIC_*` variables, `ANTHROPIC_CUSTOM_HEADERS` included. At `DEBUG`, no log line carries the key, the prompt or the answer, and every logger under the SDK is held at `WARNING`. The decision envelope admits every response the decision schema admits and refuses every structural refusal the schema makes. The key reference — refused when it names another secret or the root, or holds no `sk-ant-` key — and the model settings in `unit/test_model_settings.py`. One opt-in call against the real API, `make smoke-model` (runbook 9), is never a CI gate.
- Signer (`unit/test_signing.py`): reconstructs the typed message from validated state only and refuses an unvalidated proposal at run time too; refuses without an approved session, at the session deadline, and with a key that is not the session's party; the offer, accept and close each reproduce the EIP-712 fixture's digest *and signature*; session approval names every differing field, recomputes `configHash` from the provisioned tokens, and enforces the expiry window of [ADR-044](decision_log.md); the setup approval is the role's token to the provisioned exchange for the provisioned allowance, with the gas bound of [ADR-042](decision_log.md).
- `KeyHolder` (`unit/test_keys.py`): ADR-039's derivation recomputed from the ADR's own text; domain separation by role, chain and run; the rejection-sampling counter forced; both reference forms, and every way each can fail without echoing the secret; the exact public surface of the holder and the run signer, which cannot be pickled or copied.
- Internal API (`unit/test_internal_api.py`, in process over ASGI): HMAC rejection, including a health check's MAC refused on `release` and a provisioning MAC refused on another run ([ADR-041](decision_log.md)); provisioning idempotency and re-provisioning after a restart; `approve-session` mismatch detection; `release` discards state and refuses the run from then on; a scan of every log line of a whole run for mandate values, secrets and bodies.
- Observation consistency (`unit/test_consistency.py`, [ADR-046](decision_log.md)): observations built with real EIP-712 digests are accepted, and each kind of contradiction — order, gaps, a terminal kind, alternation, a missing offer field, a digest that does not hash from its fields, a status, too many offers, `expected_sequence`, `offers_remaining_for_me`, an `active_offer` omitted, expired, or not the last — is named by location; at the service and HTTP layers it is `observation_inconsistent` and the policy is never called.
- The order of checks: one test per adjacent pair in [protocol.md](protocol.md) section 11.1's order, and the deterministic-policy scenario in which the order decides the signed close reason.
- The stage 2.2 exit test (`integration/test_negotiation_on_anvil.py`): two agents started as real processes through `python -m agent`, one on an `env:` root and one on a `keystore:` root, driven over HTTP by a stand-in for the backend and the relay (`tests/support/agent_harness.py`), negotiate `default-overlap` and `infeasible-clone` on Anvil through the real contract. It asserts the exact sequence of moves, the settlement at 93.333333 mUSD and the buyer's Close with `terms_unacceptable`, that every signature recovers to the run's derived address and none to a root or the relay, that every digest equals the contract's own `hashOffer`, `hashAccept` or `hashClose`, fresh wallets per run, and that the reconstruction tool (A15) agrees with what the agents signed. A third run pushes chain time past an offer's `validUntil` before its counterparty acts, so the expired-offer path runs through a real observation: the seller counters at 96 instead of accepting, and the buyer accepts. Every line both processes wrote, start-up included, is searched for each one's root, shared secret and keystore password.

## 7. Backend tests

- Persistence (stage 2.1), against a real PostgreSQL: the migrated schema against a hand transcription of [data_model.md](data_model.md) sections 3, 4 and 8 — tables, column types and nullability, enum values in order, unique constraints, check-constraint names, triggers; the migration against the models by Alembic's autogenerate comparison; a downgrade that leaves no table, enum type or function behind; every refusal the schema exists for, asserted by constraint name, each beside the legitimate neighbour it must still allow; and the repositories' own guarantees — canonical rows only, gapless run-event cursors and non-repeating relay nonces under concurrent writers, a lease that survives a takeover with its nonce.
- Controller state machine (stage 2.4): the transition table compared with the diagram of [architecture.md](architecture.md) section 6.1 itself, and every one of the 64 pairs of states allowed exactly when drawn (`unit/test_controller_states.py`); operations refused outside their states with `invalid_state` and its `allowed_from`, `another_run_active`, `turn_in_progress`; and every path through the machine driven on Anvil with real agents (`integration/test_controller_operations.py`, `test_controller_outage.py`): step, pause and resume, abort, abort past the deadline as expiry, the deadline reached while running, an agent restarted (ADR-048), an agent unreachable past the limit, an observation refused five times (ADR-046), a model failure (abort 2), an execution failure (abort 4), a session refused during setup (ADR-066), a validation that no longer passes, and an RPC outage waited out, past the limit in negotiation, and past it in setup with setup carried on and nothing minted or funded twice (ADR-063). Since the stage 2.4 adversarial review, one test per defect it confirmed, each shown to fail with its fix undone: terminations made durable — an RPC blip or a crash after a model failure still aborts with reason 2 and asks no second decision, a budget ceiling aborts with 3, abort before any session ends the run `failed_setup` with no RPC needed, an abort while the session is being created aborts it once it opens, a replaced abort whose successor reverts falls back to expiry, a decision arriving after an abort is never signed, two aborts send one transaction and nothing resumes meanwhile, a fault crossing a termination keeps its cause (`integration/test_controller_terminations.py`); setup under faults — an agent restarted during setup, a crash right after `createSession` is sent, a refused broadcast resent after resume, an approval whose bytes or description are not what was asked, a setup transaction that reverts, a stuck approval re-signed by its agent at higher fees with the wallet topped up exactly, terms quoted afresh on resume, `post_setup` at the `SessionOpened` block (`integration/test_controller_setup_faults.py`); act-phase RPC failures counting toward the outage limit, a fresh window on resume, the agent window waited out and no longer (`test_controller_outage.py`); a reorg of a confirmed action asking for no second decision, and a second reorg acted on once (`test_a14_reorg.py`); the lease waited for and never shared, a graceful shutdown handing over at once, start-up reconcile on its own, a refused resend at recovery (`test_a13_crash_recovery.py`); and in `test_controller_operations.py` the top-level handler, a stale observation not asked again, a half-finished restore, a refused attempt before a signed one, an answer for another turn, the scheduling race, a stale-read state change, the validator's agent and key-holder checks, unknown scenarios and deployments, and a lost provisioning answer released.
- Idempotency (stage 2.5, `integration/test_api_idempotency_and_access.py`, [ADR-078](decision_log.md)): same key same body replays, with the header, whatever the key order; same key different body conflicts; a key is scoped to its run; a refused request keeps no key; a long-running replay is the operation as it stands and takes no second turn; a key past 24 hours is reusable; four racing requests with one key create one run.
- Since the stage 2.5 adversarial review: a run on a restarted chain settles beside a run stranded on the old one, which gives the active run up and is refused every operation, its chain record untouched; health tells the chains apart by genesis; start-up refuses a manifest the chain does not hold and a deployment id reused on another chain (`integration/test_chain_scope.py`, ADR-081); the real lifespan under uvicorn — migrations, the chain check, scenarios, interrupted operations failed and their keys freed, an event stream ended by a shutdown — and its refusals of another chain, a missing scenarios directory and a scenario that fails its schema, none quoting what it holds (`integration/test_api_startup.py`); an unhandled exception carrying mandate values logged by type and frames only, with a 500 that has its request id; a NUL refused 422; a float in a keyed body refused rather than failed (`integration/test_api_errors_and_logs.py`); two racing steps, one refused; a clone's copied fields; the run resource's party state (`test_api_operations.py`); a dead claim released after its timeout, a long-running replay in flight, health with a wrong-role or unsigned agent (`test_api_idempotency_and_access.py`); an operation watcher outliving database errors (`unit/test_api_plumbing.py`).
- The operator API (stage 2.5), through the real application against the real stack: A02 and A03 end to end over HTTP, with rows in every table (`integration/test_api_end_to_end.py`); step, pause, resume and abort and the operation each records ([ADR-073](decision_log.md)), an operation whose run needs recovery failed, an abort sent from recovery succeeding; the error envelope, refusals naming fields and never values, the run list's pages and filters, clone (`test_api_operations.py`); the operator token, the reveal header and its log line, the public views scanned for mandate values, health degraded by an agent (`test_api_idempotency_and_access.py`); the event stream from a real uvicorn server — every event once, a replay from `Last-Event-ID`, a keepalive, nothing private, and a type the contract does not list never streamed (`test_api_sse.py`); and the OpenAPI snapshot and route set (`contract/test_openapi.py`). The pure pieces — the operation verdicts, the log redaction, the start-up loaders — in `unit/test_api_plumbing.py`.
- Turn executor (stage 2.4): persist-before-broadcast asserted by a crash between the two (`integration/test_turn_persistence.py`); every leg of the signed-action check alone — the tampered message signed again with the party's own derived key so that only that leg can fail, a malleable high-s signature among them — with an untampered control (`integration/test_controller_checks.py`); the eight steps of spec 9.2 against real agents and a real chain in every negotiation test, an agent's failures stood in for by a transport that answers in its place rather than by mocking HTTP. The agent client against the real agent application served in-process: requests it accepts, responses parsed strictly, failures sorted into unavailable and refused, nothing repeated from a malformed answer (`unit/test_agent_client.py`).
- Relay (stage 2.3, `integration/test_chain_recovery.py` and `test_chain_relay_and_indexer.py`, against Anvil): rebroadcast of the same raw tx and every recovery outcome — mined, pooled, rebroadcast, superseded, nonce conflict, unreachable; the RPC timing out on the broadcast itself; gas replacement creating a new outbox row linked by `replaces_id` with the same `signed_action_id` and nonce and both fees up an eighth, at the trigger and not one block before, stopping at the ceiling, never for an agent-signed transaction, and the replaced original mined after all; the partial unique index deciding a real insert race; an agent-signed setup approval relayed by its own bytes. Fee arithmetic, the signer and the settings in `unit/test_relay_fees_and_signer.py`.
- Indexer (stage 2.3): all seven events decoded, only from the exchange's address, and nothing else from its ABI (`unit/test_chain_codec.py`, every protocol error too); the canonical flag; reorg detection by block hash and the rewind; confirmations to the threshold and finality exactly at the finalized head; a revert replayed and decoded; a third party's expiry found by the log scan; nothing recorded twice by a second poll or a restart; a terminal run no longer watched; the settlement verification of architecture 5.3 (`unit/test_settlement_check.py`). And, since the stage 2.3 adversarial review, one test per defect it confirmed, each shown to fail with its fix undone (`integration/test_chain_faults.py`): an RPC fault is not a reorg, an invalidated row comes back, a retried action sends nothing after a superseded successor, a run's fault is reported without stopping the poll, a refused or unanswered resend is reported as such, depth keeps growing past the finalized head, a reorg is a durable run event and a terminal event is reported every poll, an invalid threshold is refused, a deadline revert is named. The adapter's error translation runs against a local JSON-RPC server that answers as a misbehaving hosted RPC does (`unit/test_chain_adapter_errors.py`).
- Projection (stage 2.3): the session view compared field by field with the contract's `getSession`; the timeline, outcome and balances over synthetic rows (`unit/test_projection.py`) and after real negotiations; never reads non-canonical rows — a source-level test that no projection module calls `history_for_export`.
- Observation builder (stage 2.4, `integration/test_turn_persistence.py`): built against a database whose mandate repository refuses every read but `get_for_party` for the acting party; output validates against `observation.v1.json`; contains only the acting party's mandate, and the body sent contains none; an expired offer is null in `active_offer` and `expired` in `history` (ADR-046). Every negotiation test then has the agent's own consistency check accept every observation, and the turn executor refuses a decision whose `observation_hash` is not the stored one.
- Export: default export snapshot contains no private fields (schema test with `additionalProperties: false`); private export contains them only when requested. Since stage 2.5 against real runs (`integration/test_api_export.py`): the default export validates and carries no mandate value, the private one validates, holds both mandates and each agent's observation with only its own, and is refused without the header; and after a reorg the export keeps the removed offer, non-canonical, beside its replacement (`test_a14_reorg.py`). The schema refuses a mandate riding in the RPC method counts and admits a null failure class and an unknown RPC cost (`packages/protocol/tests/test_export_schema.py`).
- Metrics: hand-computed expectations for a settled run, a closed run, an infeasible run, an aborted run; efficiency undefined when surplus is zero. Since stage 2.5 (`unit/test_metrics_figures.py`): feasibility of both scenarios and with ADR-045's capital and inventory bounds, the deterministic settlement's utilities summing to the surplus and the capital-bounded ones of ADR-083 never above it, violations by offer and by acceptance, chain cost and wait with a replacement and a revert, model cost with unknown kept unknown and cached input tokens counted, the failure class of every cause ADR-080 names, and the audit failing on each thing it checks — the threshold, a missing event, a wrong digest, a sequence gap, an event with no action, the terminal kind, the opening, each of the four settlement legs, the terminal snapshots, a pending run, the outcome's transaction — with its control holding, and a close audited without legs; through the API, hand-worked figures for the deterministic settlement and the infeasible close, a failed setup capturing zero, the metrics recorded after every turn and once when the run ended, and each figure computed from its own source — the outbox's two kinds of transaction, the decisions' latencies (`integration/test_api_end_to_end.py`); RPC counting against a real JSON-RPC server, by method, per task and thread, and the price table's arithmetic, rounding up and unknown-never-zero (`unit/test_rpc_cost.py`); a recomputation never overwriting the RPC counts (`integration/test_db_repositories.py`); and the metrics route on a real run, utilities null without the header (`integration/test_api_export.py`).
- Sentence renderer: one case per timeline kind, the execution failure included (`unit/test_projection.py`), and the stored sentences of real negotiations in `integration/test_chain_negotiation.py`.

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
| Coverage: contracts 100 percent lines on `NegotiationExchange` (live from stage 1); backend 85 percent lines (live from stage 2.1); agent validator and signer 100 percent branches (live from stage 2.2) | merge |
| Import boundaries (`lint-imports`, the contracts in `.importlinter`) | merge (live from stage 2.1) |
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
100 percent rather than lines alone. The other two thresholds were turned on with the code they
measure: the backend's in stage 2.1, the agent's in stage 2.2, where the rule covers every file in
`agent/validation/` and `agent/signing/` — session approval and the setup approval included, because
both are part of the signer's refusal to sign for anything but what it approved. The Python assertion is
`infra/scripts/check_python_coverage.py`, tested like the contract one, and a rule whose glob matches
no file fails rather than passing over nothing — the coverage version of `all([])`.

**Integration tests fail in CI rather than skipping.** Locally, a Python integration test skips with
its reason when PostgreSQL or Anvil is absent. CI and `make ci` set `REQUIRE_INTEGRATION=1`, which
turns the PostgreSQL skip into a failure, so the Python gate cannot go green without having run its
database suite.

The coverage thresholds were confirmed by the product owner on 21 September 2026. They are deliberately uneven: the contract and the two components that convert a model's words into authority are the places where a missed branch is an incorrect transfer, while the backend's remaining 15 percent is mostly error plumbing that integration tests exercise end to end.

## 11. Test data and fixtures policy

- Scenario populations for batches are generated from a seed and committed as JSON so results are reproducible.
- Canned model responses used in tests live under `tests/fixtures/model_responses/` and are labelled `fixture` in any run they produce.
- No test may hardcode a final price into application code. A grep test fails the build if any non-test module contains the literal default reservation values in a comparison.
