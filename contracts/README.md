# `contracts`

Foundry project holding `MockERC20` (two deployments) and `NegotiationExchange`. The normative
specification is [protocol.md](../docs/protocol.md): typed messages and hashing (section 4),
sequence and time rules (5 and 6), the contract state machine (7), the interface and its
verification order (8), events (9) and reason codes (10).

Sources landed in stage 1 of [build_plan.md](../docs/build_plan.md), tests first
([test_strategy.md](../docs/test_strategy.md) section 4).

| Path | What is there |
|---|---|
| `src/` | `MockERC20`, `NegotiationExchange`, `interfaces/INegotiationExchange` |
| `script/Deploy.s.sol` | Deploys the pair and the exchange, and writes the deployment manifest ([ADR-038](../docs/decision_log.md)) |
| `test/unit/` | The unit suite, including `Fixtures.t.sol`, the Solidity third of the cross-language digest check |
| `test/invariant/` | The handler-based invariant suite and the stateless fuzz properties |
| `.gas-snapshot` | Committed, over the deterministic unit tests only (`make snapshot`) |

## Deploying

From inside `contracts/`:

```
DEPLOYMENT_ID=local-2026-09-24-01 \
OPERATOR_ADDRESS=0x… RELAY_ADDRESS=0x… \
forge script script/Deploy.s.sol:Deploy \
    --rpc-url "$ANVIL_RPC_URL" --broadcast --private-key "$DEPLOYER_PRIVATE_KEY"
```

From the repository root the script path is `contracts/script/Deploy.s.sol:Deploy` **and**
`--root contracts` is required: the path is resolved against the working directory, while `--root`
is what `fs_permissions` and the `out/` read resolve against.

`EXPLORER_BASE_URL` and `MANIFEST_DIR` are optional. The manifest is written to
`docs/deployments/<DEPLOYMENT_ID>.json`; local ones are git-ignored, Sepolia ones are committed. The
script does not mint, approve or resolve ENS — funding belongs to a run and names are added to the
manifest once, at registration. [runbook.md](../docs/runbook.md) section 4 has the full procedure.

## Rules that apply to every file here

- `// SPDX-License-Identifier: Apache-2.0` is the first line. The identifier is compiled into
  contract metadata, so it is part of the deployed artefact ([ADR-032](../docs/decision_log.md)).
- OpenZeppelin 5.x only: `ERC20`, `EIP712`, `ECDSA`, `SafeERC20`, `ReentrancyGuard`. No custom
  cryptography, no other dependency.
- Custom errors, never `require` strings, named per [protocol.md](../docs/protocol.md) 8.3.
- Checks, effects, interactions. Effects before any external call. `nonReentrant` on
  `acceptAndSettle`.
- No `selfdestruct`, no `delegatecall`, no upgradeability, no owner transfer, no pause.
- NatSpec on every external function naming the protocol section it implements.
- `forge fmt` and a committed `forge snapshot`. `forge lint` is clean; the three suppressions in
  `foundry.toml` each carry their reason, and `block.timestamp` read across a `vm.warp` is written
  as `vm.getBlockTimestamp()` so the environment-read lint stays quiet for a real reason.
- **Two cheatcode traps that produce tests which pass for the wrong reason.** `vm.expectRevert` must
  immediately precede the call under test: an external call in the argument list, including any
  helper that reads from the contract, consumes the expectation. `vm.prank` is consumed the same
  way, so a `balanceOf` inside a pranked call's arguments redirects the real call to the test
  contract. Both have already caused silent failures here.
- **`vm.serializeJson(objectKey, value)` sets an object's whole contents rather than adding to
  them.** Composing the deployment manifest with it produced a one-key file that was still valid
  JSON. Nested objects and explicit nulls go in with `vm.writeJson(value, path, ".key")` instead.

## Dependencies

`lib/` holds two submodules, pinned and recorded in the committed `foundry.lock`: forge-std v1.16.2
and OpenZeppelin v5.7.0 ([ADR-033](../docs/decision_log.md)). Clone with
`git submodule update --init --recursive`.

## Gates

Coverage on `NegotiationExchange` is 100 percent of lines, statements, branches **and** functions,
enforced in CI from stage 1 ([test_strategy.md](../docs/test_strategy.md) section 10). But coverage
is the weaker of the two claims the suite makes:

**A green suite is evidence only against the mutations it has been shown.** Before marking a test
deliverable done, break the thing the test claims to protect and confirm the suite goes red. Eight
mutations to `NegotiationExchange` are recorded in test_strategy 4.2 and each one fails the invariant
suite on its own. Three of them survived the suite's first version while coverage was already at
100 percent, and in every case the fault was in how the handler explored rather than in what the
invariants asserted.
