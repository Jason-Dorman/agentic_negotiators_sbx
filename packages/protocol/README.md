# `packages/protocol`

The machine-readable form of [docs/protocol.md](../../docs/protocol.md), consumed by Python,
TypeScript and Solidity so that a field renamed in one language fails the build in the others
([contributing.md](../../docs/contributing.md) section 2.4).

| Directory | Contents | Arrives |
|---|---|---|
| `schemas/` | JSON Schema for observation, agent decision, mandate, scenario and export | Stage 1 |
| `abi/` | ABI artefacts emitted by the Foundry build | Stage 1 |
| `fixtures/` | EIP-712 fixtures with known digests and signatures | Stage 1 |
| `tools/` | `reconstruct.py`, which reads chain data only (A15) | Stage 1 |
| `src/negotiation_protocol/` | Python package | Stage 1 |
| `src/index.ts` | TypeScript package (`@negotiation/protocol`) | Stage 1 |
| `tests/` | Fixture tests run by pytest, Vitest and Forge against the same JSON | Stage 1 |

`web3` is an optional extra (`negotiation-protocol[tools]`) used only by the reconstruction
tool. Installing this package must never give the agent service an RPC client
([architecture.md](../../docs/architecture.md) section 3.3).
