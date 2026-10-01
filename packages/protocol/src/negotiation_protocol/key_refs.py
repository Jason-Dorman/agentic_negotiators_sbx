"""Key references: the grammar a reference must satisfy, and resolving one to its 32 bytes.

Both services hold keys by reference, never by value (ADR-023): each agent instance its root
(ADR-039), and the backend its relay and operator keys. The grammar was the agent's own until stage
2.3 (ADR-049); it moved here when the backend became the second reader, so that the two cannot
disagree about what a reference is.

A reference is a pointer to a secret, and the code around it treats it as safe to log, store and
return. That is only true if a secret cannot pass for one. The first version accepted anything
after `env:` or `keystore:`, so a key pasted into the wrong place — `env:BUYER_ROOT_KEY=0x…`,
which `generate_keys.py`'s two adjacent output lines invite — was accepted as a reference, failed
to resolve, and was printed in the error, the start-up log line and every provisioning refusal.

The grammar:

- `env:NAME`, where `NAME` is an environment variable name: an upper-case letter or underscore, then
  upper-case letters, digits and underscores, at most 64 characters;
- `keystore:/path.json`, an absolute path to a JSON file, of letters, digits, `.`, `_`, `-` and `/`;

and in either form, no part that looks like a key: no run of 32 or more hexadecimal characters.

Resolution reads the variable as 32 bytes of hex, with or without `0x`, or decrypts the web3
keystore with the password in `KEYSTORE_PASSWORD`. Every error message names the reference's target
— safe only because the grammar is checked first — and never the secret or the password.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from eth_account import Account

KEY_REF_FORMS: Final = "env:NAME or keystore:/path.json"

#: The *name* of the variable that holds the keystore password, not a password.
PASSWORD_VARIABLE: Final = "KEYSTORE_PASSWORD"  # noqa: S105  reason: a variable name

_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
_KEYSTORE_PATH = re.compile(r"/[A-Za-z0-9._/-]+\.json")
_KEY_LIKE = re.compile(r"[0-9a-fA-F]{32,}")


class KeyReferenceError(Exception):
    """A key could not be resolved from its reference.

    The message is safe to log: it names the reference's target and what went wrong with it, and
    never contains the secret, the password or any part of either.
    """


def is_key_reference(text: str) -> bool:
    """True when `text` is a reference in the grammar above and holds nothing shaped like a key."""
    if not isinstance(text, str) or _KEY_LIKE.search(text) is not None:
        return False
    scheme, _, target = text.partition(":")
    if scheme == "env":
        return _ENV_NAME.fullmatch(target) is not None
    if scheme == "keystore":
        return _KEYSTORE_PATH.fullmatch(target) is not None and ".." not in target.split("/")
    return False


def check_key_reference(text: str) -> str:
    """A Pydantic after-validator. Its message never repeats the value: it may be a key."""
    if not is_key_reference(text):
        raise ValueError(f"must be a key reference, {KEY_REF_FORMS}; the value is not repeated")
    return text


def resolve_key_reference(key_ref: str, environ: Mapping[str, str]) -> bytes:
    """The 32 bytes a reference points at. Raises `KeyReferenceError`, quoting no secret."""
    # The grammar first: every message below names the reference's target, which is only safe
    # once it is known to be a variable name or a path and not a pasted key (ADR-049).
    if not is_key_reference(key_ref):
        raise KeyReferenceError(f"a key reference must be {KEY_REF_FORMS}; it is not repeated here")
    scheme, _, target = key_ref.partition(":")
    if scheme == "env":
        return _from_environment(target, environ)
    return _from_keystore(Path(target), environ)


def _from_environment(name: str, environ: Mapping[str, str]) -> bytes:
    value = environ.get(name, "")
    if not value:
        raise KeyReferenceError(f"the environment variable {name} is not set")
    try:
        key = bytes.fromhex(value.removeprefix("0x"))
    except ValueError:
        key = b""
    if len(key) != 32:
        raise KeyReferenceError(f"the environment variable {name} is not 32 bytes of hex")
    return key


def _from_keystore(path: Path, environ: Mapping[str, str]) -> bytes:
    password = environ.get(PASSWORD_VARIABLE, "")
    if not password:
        raise KeyReferenceError(
            f"{PASSWORD_VARIABLE} is not set, so the keystore {path} cannot be opened"
        )
    try:
        keystore = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise KeyReferenceError(f"the keystore {path} cannot be read: {error.strerror}") from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise KeyReferenceError(f"{path} is not a keystore: it is not JSON text") from None
    try:
        return bytes(Account.decrypt(keystore, password))
    except (ValueError, KeyError, TypeError):
        raise KeyReferenceError(
            f"the keystore {path} could not be decrypted with {PASSWORD_VARIABLE}"
        ) from None
