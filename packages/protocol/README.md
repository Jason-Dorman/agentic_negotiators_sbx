# `packages/protocol`

The machine-readable form of [docs/protocol.md](../../docs/protocol.md), consumed by Python,
TypeScript and Solidity so that a field renamed in one language fails the build in the others
([contributing.md](../../docs/contributing.md) section 2.4).

| Directory | Contents |
|---|---|
| `schemas/` | JSON Schema for observation, agent decision, mandate, scenario, export and the deployment manifest |
| `abi/` | ABI artefacts, **generated** from the Foundry build by `tools/export_abi.py` |
| `fixtures/` | The EIP-712 digest fixture (**generated**) and the reason-code table |
| `tools/` | The two generators, and `reconstruct.py`, which reads chain data only (A15) |
| `src/negotiation_protocol/` | Python package |
| `src/index.ts` | TypeScript package (`@negotiation/protocol`) |
| `tests/` | Fixture tests run by pytest and Vitest; the Solidity third lives in `contracts/test/unit/Fixtures.t.sol` |

## The two generated files

`abi/*.json` and `fixtures/eip712.v1.json` are committed and generated. A stale copy is worse than
an absent one — the indexer would decode events against an ABI the contract no longer has — so both
generators are deterministic and CI regenerates and diffs them (`make artefacts`).

```
make abi        # re-export the ABIs after a contract change
make fixtures   # regenerate the EIP-712 fixture
```

**Regenerating the fixture is a protocol event, not a chore.** Every value in it is determined by the
EIP-712 domain, the three type strings and the `configHash` encoding. If the output changes, one of
those changed, and that is a protocol version bump and a new deployment
([protocol.md](../../docs/protocol.md) section 15).

## Four implementations, one fixture

The fixture is the arbiter between four independent EIP-712 implementations. Four rather than three:
the generator computes digests by hand from the document, and the three suites check it with
libraries that share none of that code.

| Implementation | Checked by |
|---|---|
| `negotiation_protocol.eip712` — keccak and `abi.encode` from the document | the generator |
| `eth_account.messages.encode_typed_data` | `tests/test_eip712_fixtures.py` |
| `viem`'s `hashTypedData` | `tests/eip712.test.ts` |
| OpenZeppelin's `EIP712._hashTypedDataV4` | `contracts/test/unit/Fixtures.t.sol` |

The Foundry side does not stop at digest equality. It places the contracts at the fixture's own
addresses with `vm.deployCodeTo` and `vm.chainId` — the domain separator includes both, so a digest
can only be reproduced by a contract that actually lives there — and then replays the fixture's Offer
and Accept **with the fixture's own signatures**, asserting the settlement moved the signed amounts.
A fixture whose digests matched but whose signatures the contract refused would be a fixture of an
action that cannot happen.

## The schemas

`additionalProperties: false` at every level, which is what turns two of this project's claims from
sentences into checks: that the observation allowlist in
[protocol.md](../../docs/protocol.md) section 12 is exhaustive (A12), and that the default evidence
export carries no private field. Each schema is tested against a valid instance *and* against the
specific mutations it exists to refuse — a counterparty mandate inside an observation, a float
amount, a raw model response in the public decision records.

The schemas cross-reference each other by file name (`mandate.v1.json`, not an absolute `$id`), so a
consumer loading them from disk needs a registry rather than a default resolver. `resources.py`
builds one; `validate(instance, "observation.v1.json")` is the whole Python API.

## The reconstruction tool

```
uv run python packages/protocol/tools/reconstruct.py \
    --rpc-url http://127.0.0.1:8545 \
    --manifest docs/deployments/local-2026-09-24-01.json \
    --session-id 0x…
```

Acceptance A15, following [protocol.md](../../docs/protocol.md) section 14. Its only inputs are an
RPC URL and a deployment manifest: no database, no export, no run record. It exits non-zero if any
check fails, so it works as a gate, and it reports every check individually so a partial
reconstruction is never presented as a whole one.

Beyond the six documented steps it asserts what must *not* be there — that the settlement moved
nothing besides the two legs, that no log in that transaction came from an address the manifest does
not name, that no signature recovers to the relay key, and that a session which did not settle moved
no tokens at all.

## Packaging

`web3` is an optional extra (`negotiation-protocol[tools]`) used only by the reconstruction tool.
Installing this package must never give the agent service an RPC client
([architecture.md](../../docs/architecture.md) section 3.3, [ADR-035](../../docs/decision_log.md)).

`schemas/`, `fixtures/` and `abi/` sit beside `src/` because three languages read them from those
paths, so `pyproject.toml` force-includes them into the wheel under the import package.
`resources.py` looks in the packaged location first and falls back to the repository layout, so an
editable checkout and an installed wheel behave the same.
