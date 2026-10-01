"""`KeyHolder`: load the instance's root once, derive each run's key, expose signing only.

The boundary this module holds is the one docs/security_and_trust_boundaries.md section 7 states
without exception: a signing key stays inside the agent process, and the key holder exposes signing,
not the key. Python cannot make an attribute truly private, so the property is kept structurally
instead — nothing public returns key material, neither object can be pickled or copied, neither
`repr` shows a key, and `test_keys.py` asserts the exact public surface so that a new accessor is a
deliberate edit to a test rather than a quiet addition.

A root reaches the holder as a reference, never as a value in configuration (ADR-023):

- `env:NAME` reads the variable `NAME`, 32 bytes of hex with or without `0x`. The local profile.
- `keystore:/path` decrypts a web3 keystore file with the password in `KEYSTORE_PASSWORD`. The
  Sepolia profile, exercised by the tests from stage 2 so that stage 5 adds no new code path.

The root itself never signs anything (ADR-039).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, NoReturn, Protocol

from eth_account import Account

from agent.keys.derivation import KeyDerivation, KeyRefError, derive_run_key
from agent.keys.references import KEY_REF_FORMS, is_key_reference
from negotiation_protocol import Address, Digest
from negotiation_protocol.key_refs import PASSWORD_VARIABLE as PASSWORD_VARIABLE
from negotiation_protocol.key_refs import KeyReferenceError, resolve_key_reference


@dataclass(frozen=True, slots=True)
class SignedTransaction:
    raw_transaction: bytes
    tx_hash: Digest


class RunSigner(Protocol):
    """One run's signing capability. It has an address and can sign; it has no key to give."""

    @property
    def address(self) -> Address: ...

    @property
    def derivation(self) -> KeyDerivation: ...

    def sign_digest(self, digest: bytes) -> bytes:
        """A 65-byte `r || s || v` signature over a 32-byte digest."""
        ...

    def sign_transaction(self, transaction: Mapping[str, Any]) -> SignedTransaction: ...


class KeyHolder(Protocol):
    @property
    def key_ref(self) -> str: ...

    def signer_for(self, derivation: KeyDerivation) -> RunSigner: ...


def _refuse_serialisation(kind: str) -> NoReturn:
    raise TypeError(f"a {kind} cannot be serialised or copied: it would carry key material")


class _LocalRunSigner:
    __slots__ = ("_account", "_derivation")

    def __init__(self, derivation: KeyDerivation, key: bytes) -> None:
        self._derivation = derivation
        self._account = Account.from_key(key)

    @property
    def address(self) -> Address:
        return Address(self._account.address)

    @property
    def derivation(self) -> KeyDerivation:
        return self._derivation

    def sign_digest(self, digest: bytes) -> bytes:
        return bytes(self._account.unsafe_sign_hash(digest).signature)

    def sign_transaction(self, transaction: Mapping[str, Any]) -> SignedTransaction:
        signed = self._account.sign_transaction(dict(transaction))
        return SignedTransaction(bytes(signed.raw_transaction), Digest(bytes(signed.hash)))

    def __repr__(self) -> str:
        return f"RunSigner(address={self.address}, derivation={self._derivation})"

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        _refuse_serialisation("run signer")


class RootKeyHolder:
    """The `KeyHolder` for one agent instance, holding its root for the process lifetime."""

    __slots__ = ("_key_ref", "_root")

    def __init__(self, key_ref: str, root: bytes) -> None:
        self._key_ref = key_ref
        self._root = root

    @classmethod
    def from_ref(cls, key_ref: str, environ: Mapping[str, str]) -> RootKeyHolder:
        return cls(key_ref, _resolve(key_ref, environ))

    @property
    def key_ref(self) -> str:
        return self._key_ref

    def signer_for(self, derivation: KeyDerivation) -> RunSigner:
        return _LocalRunSigner(derivation, derive_run_key(self._root, derivation))

    def __repr__(self) -> str:
        return f"RootKeyHolder(key_ref={self._key_ref!r})"

    def __reduce_ex__(self, protocol: object) -> NoReturn:
        _refuse_serialisation("key holder")


def _resolve(key_ref: str, environ: Mapping[str, str]) -> bytes:
    # The grammar first: the shared resolver's messages name the reference's target, which is only
    # safe once it is known to be a variable name or a path and not a pasted key (ADR-049).
    if not is_key_reference(key_ref):
        raise KeyRefError(f"a root key reference must be {KEY_REF_FORMS}; it is not repeated here")
    try:
        return resolve_key_reference(key_ref, environ)
    except KeyReferenceError as error:
        raise KeyRefError(str(error)) from None
