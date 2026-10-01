"""The per-run key derivation of ADR-039.

```text
candidate(c) = HMAC-SHA256(key = root, msg = abi.encode(
    string  "agent-negotiation-sandbox/participant-key/v1",
    uint256 chainId,
    string  role,          // "buyer" or "seller"
    bytes16 runId,         // the run's UUID, as 16 raw bytes
    uint8   c))
key = candidate(c) for the first c = 0, 1, 2, … with 0 < candidate < the secp256k1 group order
```

Domain-separated by chain, role and run, so neither a buyer and a seller key nor a local and a
Sepolia key can collide, even with one root configured for both by mistake. The label versions the
scheme: a change to it is a new label, never a silent change to every address. The counter is the
rejection-sampling rule BIP-32 uses, and at a probability near 2^-128 per step it exists to make the
function total rather than because it will run.

The derived key is returned as bytes to the key holder in this package and goes nowhere else.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from eth_abi.abi import encode as abi_encode

from agent.observation import Role
from negotiation_protocol.eip712 import SECP256K1_ORDER

SCHEME: Final = "agent-negotiation-sandbox/participant-key/v1"
_MAX_COUNTER: Final = 255  # uint8


class KeyRefError(Exception):
    """A root could not be resolved from its reference, or no key could be derived from it.

    The message is safe to log and to return to the backend: it names the reference and what went
    wrong with it, and never contains the secret, the password or any part of either.
    """


@dataclass(frozen=True, slots=True)
class KeyDerivation:
    """The public inputs of one derivation, as `wallets.key_derivation` stores them."""

    chain_id: int
    role: Role
    run_id: UUID

    def to_json(self) -> dict[str, object]:
        return {
            "scheme": SCHEME,
            "chain_id": self.chain_id,
            "role": self.role,
            "run_id": str(self.run_id),
        }


def _candidate(root: bytes, spec: KeyDerivation, counter: int) -> int:
    message = abi_encode(
        ["string", "uint256", "string", "bytes16", "uint8"],
        [SCHEME, spec.chain_id, spec.role, spec.run_id.bytes, counter],
    )
    return int.from_bytes(hmac.new(root, message, hashlib.sha256).digest(), "big")


def derive_run_key(root: bytes, spec: KeyDerivation) -> bytes:
    """The run's 32-byte signing key. Private to `agent.keys`."""
    for counter in range(_MAX_COUNTER + 1):
        candidate = _candidate(root, spec, counter)
        if 0 < candidate < SECP256K1_ORDER:
            return candidate.to_bytes(32, "big")
    raise KeyRefError(f"no key in range after {_MAX_COUNTER + 1} counters for this derivation")
