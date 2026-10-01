"""What a key reference may look like, checked before anything reads it (ADR-049).

A reference is a pointer to a secret, and the code around it treats it as safe to log, store and
return. That is only true if a secret cannot pass for one. The first version accepted anything
after `env:` or `keystore:`, so a root pasted into the wrong place — `env:BUYER_ROOT_KEY=0x…`,
which `generate_keys.py`'s two adjacent output lines invite — was accepted as a reference, failed
to resolve, and was printed in the error, the start-up log line and every provisioning refusal.

The grammar:

- `env:NAME`, where `NAME` is an environment variable name: an upper-case letter or underscore, then
  upper-case letters, digits and underscores, at most 64 characters;
- `keystore:/path.json`, an absolute path to a JSON file, of letters, digits, `.`, `_`, `-` and `/`;

and in either form, no part that looks like a key: no run of 32 or more hexadecimal characters.
"""

from __future__ import annotations

import re
from typing import Final

KEY_REF_FORMS: Final = "env:NAME or keystore:/path.json"

_ENV_NAME = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")
_KEYSTORE_PATH = re.compile(r"/[A-Za-z0-9._/-]+\.json")
_KEY_LIKE = re.compile(r"[0-9a-fA-F]{32,}")


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
