"""The EIP-712 domain, the three typed messages, and `configHash`.

docs/protocol.md sections 3 and 4, in Python. One implementation, used by the fixture generator,
the reconstruction tool and — from stage 2 — the agent service's signer, so there is exactly one
place in Python that knows how a digest is built.

`tests/test_eip712_fixtures.py` checks this module's output against `eth_account`'s own EIP-712
implementation, viem's and OpenZeppelin's. Sharing code between the generator and the tool is safe
precisely because the check is against three implementations that share none of it.

Two conventions, both deliberate:

**Messages are frozen dataclasses with no defaults.** Every field must be supplied. The production
rule is that the signer constructs a message from validated state and never from model-supplied
bytes (CLAUDE.md), and a dataclass with a default `sequence` would be one where a forgotten field
still produces a signable message.

**Amounts are `int` here and strings at the JSON boundary.** A quote amount crosses into this
module as a Python integer of arbitrary precision; it is never a float, and `uint256` overflow is
checked rather than wrapped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from eth_abi.abi import encode as abi_encode
from eth_account import Account
from eth_utils.address import to_checksum_address
from eth_utils.crypto import keccak

DOMAIN_NAME: Final = "AgentNegotiationSandbox"
DOMAIN_VERSION: Final = "1"

#: Byte-exact type strings (docs/protocol.md section 4). Changing one changes every digest, so it
#: is a protocol version bump and a new deployment (section 15).
EIP712_DOMAIN_TYPE: Final = (
    "EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"
)
OFFER_TYPE: Final = (
    "Offer(bytes32 sessionId,bytes32 configHash,uint64 sequence,"
    "address proposer,uint256 quoteAmount,uint64 validUntil)"
)
ACCEPT_TYPE: Final = (
    "Accept(bytes32 sessionId,bytes32 configHash,uint64 sequence,address actor,bytes32 offerHash)"
)
CLOSE_TYPE: Final = (
    "Close(bytes32 sessionId,bytes32 configHash,uint64 sequence,address actor,uint8 reason)"
)

_UINT256_MAX: Final = 2**256 - 1

#: The secp256k1 group order. Its halfway point is the boundary OpenZeppelin's `ECDSA.tryRecover`
#: enforces against signature malleability.
SECP256K1_ORDER: Final = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141


class ProtocolValueError(ValueError):
    """A value outside the range its Solidity type can hold.

    Raised rather than truncated. Solidity would wrap or revert; Python would silently keep a
    number no chain could store, and the resulting digest would be for a message that cannot
    exist.
    """


def to_bytes32(value: str | bytes) -> bytes:
    """Normalise a `bytes32` from either `0x`-prefixed hex or raw bytes."""
    raw = bytes.fromhex(value.removeprefix("0x")) if isinstance(value, str) else value
    if len(raw) != 32:
        raise ProtocolValueError(f"expected 32 bytes, got {len(raw)}")
    return raw


def to_hex32(value: bytes) -> str:
    return "0x" + to_bytes32(value).hex()


def _uint(value: int, bits: int, field: str) -> int:
    limit = _UINT256_MAX if bits == 256 else (1 << bits) - 1
    if not 0 <= value <= limit:
        raise ProtocolValueError(f"{field}={value} does not fit uint{bits}")
    return value


@dataclass(frozen=True, slots=True)
class Domain:
    """The EIP-712 domain. `chain_id` and `verifying_contract` come from the deployment manifest.

    Nothing derives them from a message: that is what binds a signature to one chain and one
    contract, so a signature made for Anvil cannot be replayed on Sepolia (A07).
    """

    chain_id: int
    verifying_contract: str

    name: str = DOMAIN_NAME
    version: str = DOMAIN_VERSION

    def separator(self) -> bytes:
        return keccak(
            abi_encode(
                ["bytes32", "bytes32", "bytes32", "uint256", "address"],
                [
                    keccak(text=EIP712_DOMAIN_TYPE),
                    keccak(text=self.name),
                    keccak(text=self.version),
                    _uint(self.chain_id, 256, "chain_id"),
                    to_checksum_address(self.verifying_contract),
                ],
            )
        )

    def digest(self, struct_hash: bytes) -> bytes:
        """`keccak(0x19 0x01 || domainSeparator || hashStruct(message))`, per EIP-712."""
        return keccak(b"\x19\x01" + self.separator() + to_bytes32(struct_hash))


@dataclass(frozen=True, slots=True)
class SessionConfig:
    """docs/protocol.md section 3.

    The two token addresses are absent on purpose: they are exchange immutables and enter
    `configHash` from there, so a caller cannot bind a session to a token the exchange does not
    trade. `config_hash` therefore takes them as arguments, read from the manifest.
    """

    session_id: bytes
    buyer: str
    seller: str
    base_amount: int
    expires_at: int
    max_offers: int

    def config_hash(self, base_token: str, quote_token: str) -> bytes:
        """`abi.encode`, never concatenation, never JSON hashing.

        Every field is padded to 32 bytes, so no two configurations can collide by running
        together — which string concatenation of a session id and an address would permit.
        """
        return keccak(
            abi_encode(
                [
                    "bytes32",
                    "address",
                    "address",
                    "address",
                    "address",
                    "uint256",
                    "uint64",
                    "uint16",
                ],
                [
                    to_bytes32(self.session_id),
                    to_checksum_address(self.buyer),
                    to_checksum_address(self.seller),
                    to_checksum_address(base_token),
                    to_checksum_address(quote_token),
                    _uint(self.base_amount, 256, "base_amount"),
                    _uint(self.expires_at, 64, "expires_at"),
                    _uint(self.max_offers, 16, "max_offers"),
                ],
            )
        )


@dataclass(frozen=True, slots=True)
class Offer:
    """A signed proposal. An active offer is real authority (ADR-004)."""

    session_id: bytes
    config_hash: bytes
    sequence: int
    proposer: str
    quote_amount: int
    valid_until: int

    def struct_hash(self) -> bytes:
        return keccak(
            abi_encode(
                ["bytes32", "bytes32", "bytes32", "uint64", "address", "uint256", "uint64"],
                [
                    keccak(text=OFFER_TYPE),
                    to_bytes32(self.session_id),
                    to_bytes32(self.config_hash),
                    _uint(self.sequence, 64, "sequence"),
                    to_checksum_address(self.proposer),
                    _uint(self.quote_amount, 256, "quote_amount"),
                    _uint(self.valid_until, 64, "valid_until"),
                ],
            )
        )

    def digest(self, domain: Domain) -> bytes:
        return domain.digest(self.struct_hash())


@dataclass(frozen=True, slots=True)
class Accept:
    """A signed acceptance of one specific offer digest.

    `offer_hash` is the full Offer digest, domain included, so both signatures authorise the same
    trade on the same chain and the same contract (docs/protocol.md section 4).
    """

    session_id: bytes
    config_hash: bytes
    sequence: int
    actor: str
    offer_hash: bytes

    def struct_hash(self) -> bytes:
        return keccak(
            abi_encode(
                ["bytes32", "bytes32", "bytes32", "uint64", "address", "bytes32"],
                [
                    keccak(text=ACCEPT_TYPE),
                    to_bytes32(self.session_id),
                    to_bytes32(self.config_hash),
                    _uint(self.sequence, 64, "sequence"),
                    to_checksum_address(self.actor),
                    to_bytes32(self.offer_hash),
                ],
            )
        )

    def digest(self, domain: Domain) -> bytes:
        return domain.digest(self.struct_hash())


@dataclass(frozen=True, slots=True)
class Close:
    """A signed walk-away. `reason` is one of the participant codes 1..3 (section 10)."""

    session_id: bytes
    config_hash: bytes
    sequence: int
    actor: str
    reason: int

    def struct_hash(self) -> bytes:
        return keccak(
            abi_encode(
                ["bytes32", "bytes32", "bytes32", "uint64", "address", "uint8"],
                [
                    keccak(text=CLOSE_TYPE),
                    to_bytes32(self.session_id),
                    to_bytes32(self.config_hash),
                    _uint(self.sequence, 64, "sequence"),
                    to_checksum_address(self.actor),
                    _uint(self.reason, 8, "reason"),
                ],
            )
        )

    def digest(self, domain: Domain) -> bytes:
        return domain.digest(self.struct_hash())


def recover_signer(digest: bytes, signature: str | bytes) -> str:
    """Recover the address that signed `digest`, as the contract's `ECDSA.recover` would.

    Step 5 of the verification order (docs/protocol.md section 8.1), off-chain: the caller then
    compares the result with the address the message names. This function does not make that
    comparison, because "who signed" and "who was allowed to sign" are separate questions and the
    reconstruction tool reports them separately.
    """
    raw = bytes.fromhex(signature.removeprefix("0x")) if isinstance(signature, str) else signature
    if len(raw) != 65:
        raise ProtocolValueError(f"expected a 65-byte signature, got {len(raw)}")
    return to_checksum_address(Account._recover_hash(digest, signature=raw))


def signature_is_canonical(signature: str | bytes) -> bool:
    """True when `s` is in the lower half of the curve order, as OpenZeppelin requires.

    ECDSA signatures are malleable: `(r, s)` and `(order - s)` both verify over the same digest.
    OpenZeppelin's `ECDSA.tryRecover` rejects the high half, so a signature can recover to the
    right address here and still be refused on-chain. Reconstruction reports that as its own
    finding rather than as an unexplained `BadSignature`.
    """
    raw = bytes.fromhex(signature.removeprefix("0x")) if isinstance(signature, str) else signature
    if len(raw) != 65:
        return False
    s = int.from_bytes(raw[32:64], "big")
    return 0 < s <= SECP256K1_ORDER // 2
