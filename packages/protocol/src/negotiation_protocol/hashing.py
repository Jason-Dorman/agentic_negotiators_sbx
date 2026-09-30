"""Canonical JSON and its SHA-256, for `observation_hash`, `mandate_hash` and `request_hash`.

Both services hash the same documents — the backend records `turns.observation_hash` and the agent
reports the `observation_hash` its decision was made from (docs/api_contract.md section 6) — so the
definition lives here, once. Two implementations that each produced "a SHA-256 of the JSON" would
agree until the day one of them sorted keys and the other did not.

The canonical form is the one `json.dumps` produces with sorted keys, no insignificant whitespace,
UTF-8 rather than `\\u` escapes, and **no floats at all**. Floats are refused rather than
formatted: every number these documents carry is an integer or a string by design (amounts are
strings, docs/api_contract.md section 1), a float would be a bug, and float formatting is where
canonical-JSON schemes in different languages disagree.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def _refuse_floats(value: Any) -> None:
    if isinstance(value, float):
        raise TypeError("canonical JSON refuses floats; amounts are strings and counts are ints")
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"canonical JSON keys must be strings, got {type(key).__name__}")
            _refuse_floats(item)
    elif isinstance(value, list | tuple):
        for item in value:
            _refuse_floats(item)


def canonical_json(value: Any) -> bytes:
    """The canonical UTF-8 encoding of a JSON-compatible value."""
    _refuse_floats(value)
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


def json_sha256(value: Any) -> str:
    """`0x` + the SHA-256 of `canonical_json(value)`, lowercase hex."""
    return "0x" + hashlib.sha256(canonical_json(value)).hexdigest()
