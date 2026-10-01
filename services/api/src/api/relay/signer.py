"""The backend's two transaction signers: the relay's gas-paying key and the operator's key.

Both reach the backend as references (`RELAY_KEY_REF`, `OPERATOR_KEY_REF`), resolved by the same
grammar and resolver as an agent's root (`negotiation_protocol.key_refs`, ADR-049). The discipline
is the agent key holder's, applied here: nothing public returns key material, a signer cannot be
pickled or copied, and its `repr` names the address and nothing else.

Neither key can produce a participant signature, and nothing here signs a typed message: the relay
signs *transactions*, whose calldata carries a participant's signature that an agent made
(docs/protocol.md section 1, docs/security_and_trust_boundaries.md section 4).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, NoReturn, Protocol

from eth_account import Account

from negotiation_protocol import Address, Digest
from negotiation_protocol.key_refs import KeyReferenceError, resolve_key_reference


@dataclass(frozen=True, slots=True)
class SignedTransaction:
    raw_transaction: bytes
    tx_hash: Digest


class TransactionSigner(Protocol):
    """A key that signs transactions. It has an address and can sign; it has no key to give."""

    @property
    def address(self) -> Address: ...

    def sign_transaction(self, transaction: Mapping[str, Any]) -> SignedTransaction: ...


class LocalTransactionSigner(TransactionSigner):
    __slots__ = ("_account", "_label")

    def __init__(self, key: bytes, label: str) -> None:
        self._account = Account.from_key(key)
        self._label = label

    @classmethod
    def from_reference(
        cls, key_ref: str, environ: Mapping[str, str], label: str
    ) -> LocalTransactionSigner:
        """Raises `KeyReferenceError`, whose message names the reference and never the key."""
        key = resolve_key_reference(key_ref, environ)
        try:
            return cls(key, label)
        except ValueError:
            # 32 bytes that are not a valid secp256k1 key: zero, or above the group order.
            raise KeyReferenceError(f"{key_ref} does not hold a valid private key") from None

    @property
    def address(self) -> Address:
        return Address(self._account.address)

    def sign_transaction(self, transaction: Mapping[str, Any]) -> SignedTransaction:
        signed = self._account.sign_transaction(dict(transaction))
        return SignedTransaction(bytes(signed.raw_transaction), Digest(bytes(signed.hash)))

    def __repr__(self) -> str:
        return f"TransactionSigner({self._label}, address={self.address})"

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        raise TypeError("a transaction signer cannot be serialised or copied: it holds a key")
