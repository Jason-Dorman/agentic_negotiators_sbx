"""`negotiation_protocol.key_refs`: the key-reference grammar of ADR-049 and its resolver.

Moved here from the agent service in stage 2.3, when the backend became the second service to hold
keys by reference. The agent's own tests still exercise it through its key holder; these pin the
shared module directly, including that no refusal ever repeats a secret.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from eth_account import Account

from negotiation_protocol.key_refs import (
    PASSWORD_VARIABLE,
    KeyReferenceError,
    check_key_reference,
    is_key_reference,
    resolve_key_reference,
)

KEY = "11" * 32


@pytest.mark.parametrize(
    "ref",
    [
        "env:RELAY_PRIVATE_KEY",
        "env:_X",
        "keystore:/run/secrets/relay.json",
        "keystore:/a-b/c_d.e.json",
    ],
)
def test_references_in_the_grammar(ref: str) -> None:
    assert is_key_reference(ref)
    assert check_key_reference(ref) == ref


@pytest.mark.parametrize(
    "ref",
    [
        f"env:RELAY_KEY=0x{KEY}",
        f"0x{KEY}",
        "env:relay_key",
        "env:" + "A" * 65,
        "keystore:relative.json",
        "keystore:/run/../secrets/x.json",
        "keystore:/run/secrets/x.txt",
        "file:/x.json",
        "env:1BAD",
    ],
)
def test_anything_else_is_refused_without_being_repeated(ref: str) -> None:
    assert not is_key_reference(ref)
    with pytest.raises(ValueError) as refused:
        check_key_reference(ref)
    assert ref not in str(refused.value)
    with pytest.raises(KeyReferenceError) as unresolved:
        resolve_key_reference(ref, {})
    assert KEY not in str(unresolved.value)


def test_an_env_reference_resolves_with_or_without_0x() -> None:
    assert resolve_key_reference("env:K", {"K": KEY}) == bytes.fromhex(KEY)
    assert resolve_key_reference("env:K", {"K": "0x" + KEY}) == bytes.fromhex(KEY)


@pytest.mark.parametrize(
    ("environ", "message"),
    [({}, "is not set"), ({"K": "xyz"}, "not 32 bytes"), ({"K": "11" * 31}, "not 32 bytes")],
)
def test_an_env_reference_that_cannot_resolve(environ: dict[str, str], message: str) -> None:
    with pytest.raises(KeyReferenceError, match=message) as refused:
        resolve_key_reference("env:K", environ)
    assert "11" * 31 not in str(refused.value)


def test_a_keystore_reference_resolves_and_its_failures_quote_no_password(tmp_path: Path) -> None:
    path = tmp_path / "relay.json"
    path.write_text(__import__("json").dumps(Account.encrypt(bytes.fromhex(KEY), "correct horse")))
    ref = f"keystore:{path}"
    assert resolve_key_reference(ref, {PASSWORD_VARIABLE: "correct horse"}) == bytes.fromhex(KEY)
    for environ, message in (
        ({}, "is not set"),
        ({PASSWORD_VARIABLE: "wrong"}, "could not be decrypted"),
    ):
        with pytest.raises(KeyReferenceError, match=message) as refused:
            resolve_key_reference(ref, environ)
        assert "correct horse" not in str(refused.value)
        assert KEY not in str(refused.value)
    with pytest.raises(KeyReferenceError, match="cannot be read"):
        resolve_key_reference(f"keystore:{tmp_path}/missing.json", {PASSWORD_VARIABLE: "x"})
    garbage = tmp_path / "garbage.json"
    garbage.write_bytes(b"\xff\xfe")
    with pytest.raises(KeyReferenceError, match="not a keystore"):
        resolve_key_reference(f"keystore:{garbage}", {PASSWORD_VARIABLE: "x"})
