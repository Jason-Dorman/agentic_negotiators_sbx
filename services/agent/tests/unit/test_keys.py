"""`KeyHolder`: the root from its reference, a per-run key by ADR-039, and signing only.

The derivation is recomputed here from ADR-039's own text — HMAC-SHA256 over the `abi.encode` of the
scheme label, chain ID, role, run ID and counter — rather than through the module under test, so the
scheme cannot drift with both sides agreeing. What is compared is the *address*: the tests never
need the derived key out of the holder, which is the property the holder exists to have.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from uuid import UUID

import pytest
from eth_abi.abi import encode as abi_encode
from eth_account import Account

from agent.keys import (
    SCHEME,
    KeyDerivation,
    KeyRefError,
    RootKeyHolder,
    check_key_reference,
    is_key_reference,
)
from negotiation_protocol import Digest, recover_signer
from negotiation_protocol.eip712 import SECP256K1_ORDER

ROOT = bytes.fromhex("11" * 32)
ROOT_HEX = "0x" + ROOT.hex()
RUN = UUID("6f1c2a4e-9d7b-4c3a-8e2f-1a2b3c4d5e6f")
BUYER_RUN = KeyDerivation(chain_id=31337, role="buyer", run_id=RUN)
ENV = {"BUYER_ROOT_KEY": ROOT_HEX}


def reference_address(root: bytes, chain_id: int, role: str, run_id: UUID) -> str:
    """ADR-039, spelled out: the first counter whose candidate lies in 1 .. n-1."""
    for counter in range(256):
        message = abi_encode(
            ["string", "uint256", "string", "bytes16", "uint8"],
            [SCHEME, chain_id, role, run_id.bytes, counter],
        )
        candidate = int.from_bytes(hmac.new(root, message, hashlib.sha256).digest(), "big")
        if 0 < candidate < SECP256K1_ORDER:
            return str(Account.from_key(candidate.to_bytes(32, "big")).address)
    raise AssertionError("unreachable")


def holder(environ: dict[str, str] | None = None) -> RootKeyHolder:
    return RootKeyHolder.from_ref("env:BUYER_ROOT_KEY", ENV if environ is None else environ)


# --------------------------------------------------------------------------------------
# The derivation
# --------------------------------------------------------------------------------------


def test_the_scheme_label_is_the_one_adr_039_names() -> None:
    assert SCHEME == "agent-negotiation-sandbox/participant-key/v1"


def test_the_derived_address_is_adr_039s() -> None:
    signer = holder().signer_for(BUYER_RUN)
    assert signer.address == reference_address(ROOT, 31337, "buyer", RUN)


def test_the_same_inputs_derive_the_same_key_after_a_restart() -> None:
    """Re-provisioning after an agent restart must reproduce the stored address (ADR-039)."""
    assert holder().signer_for(BUYER_RUN).address == holder().signer_for(BUYER_RUN).address


@pytest.mark.parametrize(
    "other",
    [
        KeyDerivation(chain_id=31337, role="seller", run_id=RUN),
        KeyDerivation(chain_id=11155111, role="buyer", run_id=RUN),
        KeyDerivation(chain_id=31337, role="buyer", run_id=UUID(int=RUN.int + 1)),
    ],
    ids=["another role", "another chain", "another run"],
)
def test_each_input_separates_the_domain(other: KeyDerivation) -> None:
    """One root configured for both roles or both profiles still yields distinct keys."""
    keys = holder()
    assert keys.signer_for(other).address != keys.signer_for(BUYER_RUN).address
    assert keys.signer_for(other).address == reference_address(
        ROOT, other.chain_id, other.role, other.run_id
    )


def test_the_derived_key_is_not_the_root() -> None:
    assert holder().signer_for(BUYER_RUN).address != Account.from_key(ROOT).address


def test_an_out_of_range_candidate_moves_to_the_next_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The BIP-32-style rejection rule, forced: counters 0 and 1 give 0 and n, counter 2 is used."""
    import agent.keys.derivation as derivation

    real = derivation._candidate
    forced = {0: 0, 1: SECP256K1_ORDER}

    def candidate(root: bytes, spec: KeyDerivation, counter: int) -> int:
        return forced.get(counter, real(root, spec, counter))

    monkeypatch.setattr(derivation, "_candidate", candidate)
    expected = Account.from_key(real(ROOT, BUYER_RUN, 2).to_bytes(32, "big")).address
    assert holder().signer_for(BUYER_RUN).address == expected


def test_a_derivation_that_never_lands_in_range_fails_rather_than_looping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import agent.keys.derivation as derivation

    monkeypatch.setattr(derivation, "_candidate", lambda root, spec, counter: 0)
    with pytest.raises(KeyRefError, match="no key in range"):
        holder().signer_for(BUYER_RUN)


def test_the_derivation_metadata_is_the_public_inputs_only() -> None:
    assert BUYER_RUN.to_json() == {
        "scheme": SCHEME,
        "chain_id": 31337,
        "role": "buyer",
        "run_id": str(RUN),
    }


# --------------------------------------------------------------------------------------
# Signing, and nothing else
# --------------------------------------------------------------------------------------


def test_a_signed_digest_recovers_to_the_run_address_and_not_to_the_root() -> None:
    signer = holder().signer_for(BUYER_RUN)
    digest = bytes.fromhex("ab" * 32)
    signature = signer.sign_digest(digest)
    assert len(signature) == 65
    assert recover_signer(digest, signature) == signer.address
    assert recover_signer(digest, signature) != Account.from_key(ROOT).address


def test_signing_is_deterministic() -> None:
    """RFC 6979 nonces: the same digest signs to the same bytes, so a turn is idempotent."""
    signer = holder().signer_for(BUYER_RUN)
    digest = bytes.fromhex("cd" * 32)
    assert signer.sign_digest(digest) == signer.sign_digest(digest)


def test_a_signed_transaction_is_from_the_run_address() -> None:
    signer = holder().signer_for(BUYER_RUN)
    transaction = {
        "type": 2,
        "chainId": 31337,
        "nonce": 0,
        "to": "0x5FbDB2315678afecb367f032d93F642f64180aa3",
        "value": 0,
        "data": "0x",
        "gas": 70_000,
        "maxFeePerGas": 2_000_000_000,
        "maxPriorityFeePerGas": 1_000_000_000,
    }
    signed = signer.sign_transaction(transaction)
    assert Account.recover_transaction(signed.raw_transaction) == signer.address
    assert isinstance(signed.tx_hash, Digest)


def test_the_holder_exposes_signing_and_no_key_material() -> None:
    """An exact set, not a denylist: a new public attribute is a deliberate edit to this test."""
    keys = holder()
    signer = keys.signer_for(BUYER_RUN)
    assert {name for name in dir(keys) if not name.startswith("_")} == {
        "from_ref",
        "key_ref",
        "signer_for",
    }
    assert {name for name in dir(signer) if not name.startswith("_")} == {
        "address",
        "derivation",
        "sign_digest",
        "sign_transaction",
    }


def test_no_key_appears_in_a_repr() -> None:
    keys = holder()
    signer = keys.signer_for(BUYER_RUN)
    for text in (repr(keys), str(keys), repr(signer), str(signer)):
        assert ROOT.hex() not in text.lower()
    assert "env:BUYER_ROOT_KEY" in repr(keys)


@pytest.mark.parametrize("make", [lambda: holder(), lambda: holder().signer_for(BUYER_RUN)])
def test_neither_can_be_pickled_or_copied(make: object) -> None:
    import copy
    import pickle

    target = make()  # type: ignore[operator]  # reason: the parametrised lambdas are callables
    with pytest.raises(TypeError, match="cannot be"):
        pickle.dumps(target)
    with pytest.raises(TypeError, match="cannot be"):
        copy.copy(target)


# --------------------------------------------------------------------------------------
# Resolving a reference: env:
# --------------------------------------------------------------------------------------


def test_an_env_root_may_omit_the_0x_prefix() -> None:
    bare = RootKeyHolder.from_ref("env:BUYER_ROOT_KEY", {"BUYER_ROOT_KEY": ROOT.hex()})
    assert bare.signer_for(BUYER_RUN).address == holder().signer_for(BUYER_RUN).address


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        ({}, "BUYER_ROOT_KEY is not set"),
        ({"BUYER_ROOT_KEY": ""}, "BUYER_ROOT_KEY is not set"),
        ({"BUYER_ROOT_KEY": "0x" + "11" * 31}, "32 bytes of hex"),
        ({"BUYER_ROOT_KEY": "0x" + "zz" * 32}, "32 bytes of hex"),
    ],
    ids=["unset", "empty", "31 bytes", "not hex"],
)
def test_a_bad_env_root_is_refused_without_echoing_it(
    environ: dict[str, str], message: str
) -> None:
    with pytest.raises(KeyRefError, match=message) as refused:
        holder(environ)
    assert "11" * 31 not in str(refused.value)


@pytest.mark.parametrize("ref", ["BUYER_ROOT_KEY", "vault:buyer", "env:", "keystore:", ROOT_HEX])
def test_a_reference_that_is_not_env_or_keystore_is_refused(ref: str) -> None:
    with pytest.raises(KeyRefError, match=r"env:NAME or keystore:/path\.json") as refused:
        RootKeyHolder.from_ref(ref, ENV)
    assert ROOT.hex() not in str(refused.value)


# --------------------------------------------------------------------------------------
# Resolving a reference: keystore: (ADR-023)
# --------------------------------------------------------------------------------------


def write_keystore(path: Path, root: bytes, password: str) -> Path:
    # pbkdf2 with few iterations keeps the test fast; the key holder reads either KDF.
    keystore = Account.encrypt(root, password, kdf="pbkdf2", iterations=2)
    path.write_text(json.dumps(keystore), encoding="utf-8")
    return path


def test_a_keystore_root_derives_the_same_keys_as_the_same_root_from_env(tmp_path: Path) -> None:
    path = write_keystore(tmp_path / "buyer-root.json", ROOT, "correct horse")
    keys = RootKeyHolder.from_ref(f"keystore:{path}", {"KEYSTORE_PASSWORD": "correct horse"})
    assert keys.key_ref == f"keystore:{path}"
    assert keys.signer_for(BUYER_RUN).address == holder().signer_for(BUYER_RUN).address


@pytest.mark.parametrize(
    ("environ", "message"),
    [
        ({}, "KEYSTORE_PASSWORD is not set"),
        ({"KEYSTORE_PASSWORD": "wrong"}, "could not be decrypted"),
    ],
)
def test_a_keystore_without_its_password_is_refused(
    tmp_path: Path, environ: dict[str, str], message: str
) -> None:
    path = write_keystore(tmp_path / "buyer-root.json", ROOT, "correct horse")
    with pytest.raises(KeyRefError, match=message) as refused:
        RootKeyHolder.from_ref(f"keystore:{path}", environ)
    assert "correct horse" not in str(refused.value)


def test_a_missing_keystore_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(KeyRefError, match="cannot be read"):
        RootKeyHolder.from_ref(f"keystore:{tmp_path / 'absent.json'}", {"KEYSTORE_PASSWORD": "x"})


def test_a_keystore_that_is_not_json_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "buyer-root.json"
    path.write_text("not json", encoding="utf-8")
    with pytest.raises(KeyRefError, match="not a keystore"):
        RootKeyHolder.from_ref(f"keystore:{path}", {"KEYSTORE_PASSWORD": "x"})


# --------------------------------------------------------------------------------------
# The reference grammar (ADR-049): a key pasted where its reference belongs is refused, unrepeated
# --------------------------------------------------------------------------------------

PASTED = ROOT.hex()


@pytest.mark.parametrize(
    "ref",
    [
        "env:BUYER_ROOT_KEY",
        "env:_X",
        "keystore:/run/secrets/buyer-root.json",
        "keystore:/tmp/pytest-of-ci/pytest-3/test_x0/seller-root.json",
    ],
)
def test_references_in_the_grammar_are_accepted(ref: str) -> None:
    assert is_key_reference(ref)
    assert check_key_reference(ref) == ref


@pytest.mark.parametrize(
    "ref",
    [
        f"env:BUYER_ROOT_KEY=0x{PASTED}",
        f"env:0x{PASTED}",
        f"env:{PASTED.upper()}",
        f"keystore:0x{PASTED}",
        f"keystore:/run/secrets/{PASTED}.json",
        "env:buyer_root_key",
        "env:",
        "keystore:relative/buyer-root.json",
        "keystore:/run/secrets/../../etc/passwd.json",
        "keystore:/run/secrets/buyer-root",
        "vault:buyer",
        "BUYER_ROOT_KEY",
    ],
)
def test_anything_else_is_refused_without_being_repeated(ref: str) -> None:
    assert not is_key_reference(ref)
    with pytest.raises(ValueError) as refused:
        check_key_reference(ref)
    assert str(refused.value) == (
        "must be a key reference, env:NAME or keystore:/path.json; the value is not repeated"
    )
    with pytest.raises(KeyRefError) as unresolved:
        RootKeyHolder.from_ref(ref, {"BUYER_ROOT_KEY": ROOT_HEX})
    assert str(unresolved.value) == (
        "a root key reference must be env:NAME or keystore:/path.json; it is not repeated here"
    )


def test_a_keystore_that_is_not_utf8_is_refused_rather_than_crashing(tmp_path: Path) -> None:
    """A binary file where a keystore belongs: the instance must start degraded, not die."""
    path = tmp_path / "buyer-root.json"
    path.write_bytes(b"\xff\xfe\x00binary")
    with pytest.raises(KeyRefError, match="not a keystore"):
        RootKeyHolder.from_ref(f"keystore:{path}", {"KEYSTORE_PASSWORD": "x"})
