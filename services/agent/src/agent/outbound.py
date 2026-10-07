"""The outbound-context assertion: nothing private to this instance leaves in a model request (A12).

Every model request is checked before it leaves (`agent.model.checked`), whichever client would
send it, live or fixture, and a hit refuses the call: nothing is counted, nothing is sent, and the
turn is answered `422 outbound_context_refused`, which the backend treats as any agent refusal —
the run waits in `recovery_required` for the operator (ADR-092). The log line names the kind of hit
and never the text that matched.

The denylist is what this instance holds and nothing else (Q88):

- **Its own secrets, matched exactly**: the internal API's shared secret, the model provider's key,
  and the keystore password when the root comes from a keystore — compiled once, at start-up.
  Each is also looked for as JSON would write it inside a string, escaped once and twice, with
  and without `ensure_ascii`, because the observation reaches the request as JSON text: a secret
  holding a `"`, a `\\` or a non-ASCII character is still found there (stage 3.3 review, Q95).
- **Its key material, matched in hex in any case, with or without `0x`**: the run's own derived
  key, reported as `run_key`; then the root or any other run's key the instance holds, reported as
  `instance_key`. None leaves `agent.keys`: the run signer and the key holder each answer
  `appears_in(text)`, whether a text holds a whole key, and give out nothing else.
- **Credential-shaped text that public data never contains** (`CREDENTIAL_PATTERNS`): an Anthropic
  key, a PEM private key block, a web3 keystore document.

It deliberately has no rule for "any 64 hex digits" (Q89): every observation carries session ids,
config hashes and offer digests of exactly that shape, and they are public. Nor does it hold the
counterparty's mandate, which this instance never receives: that boundary is structural — the
allowlisted observation, the builder that reads one mandate — and the isolation suite checks it
from outside (docs/security_and_trust_boundaries.md section 3).

The validator quotes a model's unexpected field names back to it, so it masks credential-shaped
text in the quoted list with `mask_credentials` (Q92): a model cannot make its own repair message,
or its next observation, trip this check. JSON escaping cannot create a match that the raw text
lacks — it only adds backslashes, which the keystore pattern already allows for — so feedback that
is clean as written is clean wherever it travels.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from typing import Any, Final, Protocol

from pydantic import SecretStr

#: Text no request has a reason to carry. Each name is the `kind` a refusal reports.
CREDENTIAL_PATTERNS: Final[Mapping[str, re.Pattern[str]]] = {
    "anthropic_key_shaped": re.compile(r"sk-ant-[A-Za-z0-9_-]+"),
    "pem_private_key": re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    # A keystore's `"ciphertext":` or `"kdfparams":`, also as it reads once JSON-escaped.
    "keystore_document": re.compile(r'\\*"(?:ciphertext|kdfparams)\\*"\s*:'),
}
CREDENTIAL_MASK: Final = "[credential-shaped text]"


class KeyProbe(Protocol):
    """Something holding key material that can say whether a text contains it, and nothing more."""

    def appears_in(self, text: str) -> bool: ...


def credential_kind(text: str) -> str | None:
    for kind, pattern in CREDENTIAL_PATTERNS.items():
        if pattern.search(text):
            return kind
    return None


def mask_credentials(text: str) -> str:
    """`text` with each credential-shaped run replaced by `CREDENTIAL_MASK`."""
    for pattern in CREDENTIAL_PATTERNS.values():
        text = pattern.sub(CREDENTIAL_MASK, text)
    return text


class OutboundCheck:
    """One instance's denylist, and a run's once `for_run` adds the run's signer."""

    __slots__ = ("_forms", "_probes", "_secrets")

    def __init__(
        self,
        secrets: Mapping[str, SecretStr] | None = None,
        probes: Mapping[str, KeyProbe] | None = None,
    ) -> None:
        # An empty value would be "in" every text.
        self._secrets = {
            kind: value for kind, value in (secrets or {}).items() if value.get_secret_value()
        }
        self._forms = {
            kind: _escaped_forms(value.get_secret_value()) for kind, value in self._secrets.items()
        }
        self._probes = dict(probes or {})

    def with_secret(self, kind: str, value: SecretStr) -> OutboundCheck:
        return OutboundCheck({**self._secrets, kind: value}, self._probes)

    def with_probe(self, kind: str, probe: KeyProbe) -> OutboundCheck:
        return OutboundCheck(self._secrets, {**self._probes, kind: probe})

    def for_run(self, run_key: KeyProbe) -> OutboundCheck:
        """A run's check: its own key asked first, so a hit on it is named `run_key`."""
        return OutboundCheck(self._secrets, {"run_key": run_key, **self._probes})

    @property
    def kinds(self) -> tuple[str, ...]:
        """What this check looks for, by name: the secrets, the key material, the patterns."""
        return (*self._secrets, *self._probes, *CREDENTIAL_PATTERNS)

    def violation(self, document: Any) -> str | None:
        """The kind of the first private thing in any string of `document` — a key or a value
        anywhere in a JSON-shaped request — or None. Never the text that matched."""
        texts = list(_strings(document))
        for kind, forms in self._forms.items():
            if any(form in text for text in texts for form in forms):
                return kind
        for kind, probe in self._probes.items():
            if any(probe.appears_in(text) for text in texts):
                return kind
        for text in texts:
            found = credential_kind(text)
            if found is not None:
                return found
        return None

    def __repr__(self) -> str:
        return f"OutboundCheck(kinds={list(self.kinds)})"


def _escaped_forms(value: str) -> frozenset[str]:
    """`value` as written, and as JSON writes it inside a string: escaped once or twice, with and
    without `ensure_ascii`. For a value with nothing to escape these are all the value itself."""
    forms = {value}
    for ascii_only in (False, True):
        once = json.dumps(value, ensure_ascii=ascii_only)[1:-1]
        forms |= {once, json.dumps(once, ensure_ascii=ascii_only)[1:-1]}
    return frozenset(forms)


def _strings(document: Any) -> Iterator[str]:
    """Every string in a JSON-shaped value, object keys included, as decoded rather than escaped."""
    if isinstance(document, str):
        yield document
    elif isinstance(document, Mapping):
        for key, value in document.items():
            yield str(key)
            yield from _strings(value)
    elif isinstance(document, list | tuple):
        for item in document:
            yield from _strings(item)


__all__ = [
    "CREDENTIAL_MASK",
    "CREDENTIAL_PATTERNS",
    "KeyProbe",
    "OutboundCheck",
    "credential_kind",
    "mask_credentials",
]
