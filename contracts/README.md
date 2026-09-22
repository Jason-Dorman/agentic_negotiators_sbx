# `contracts`

Foundry project holding `MockERC20` (two deployments) and `NegotiationExchange`. The normative
specification is [protocol.md](../docs/protocol.md): typed messages and hashing (section 4),
sequence and time rules (5 and 6), the contract state machine (7), the interface and its
verification order (8), events (9) and reason codes (10).

Sources land in stage 1 of [build_plan.md](../docs/build_plan.md), tests first
([test_strategy.md](../docs/test_strategy.md) section 4).

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
- `forge fmt` and a committed `forge snapshot`.

## Dependencies

`lib/` is populated in stage 1:

```
forge install foundry-rs/forge-std
forge install OpenZeppelin/openzeppelin-contracts@v5.x.y
```

Coverage on `NegotiationExchange` is a 100 percent line gate
([test_strategy.md](../docs/test_strategy.md) section 10).
