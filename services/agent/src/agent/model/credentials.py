"""The model provider's API key, read through a reference and never held as configuration (ADR-086).

`AGENT_MODEL_KEY_REF` names the variable that holds this instance's key —
`env:BUYER_ANTHROPIC_API_KEY` — in the `env:` form of the key-reference grammar (ADR-049). The
`keystore:` form is refused: a keystore holds an Ethereum key, not an API key. Each instance has its
own variable, so one agent's credential and spend are never the other's.

A reference may not name a variable that holds one of this deployment's other secrets — an
`AGENT_` setting (the shared secret among them), a root, a private key, a shared secret or the
keystore password — and `AgentSettings` also refuses the variable its own root reference names:
a one-word slip would otherwise send a signing key to the provider as `x-api-key` (ADR-086, Q79).
The value must look like an Anthropic API key, `sk-ant-…`, for the same reason.

The key is returned as a `SecretStr`, which no `repr` or log line shows, and every message here
names the variable and never its value. The client is then given the key explicitly, which stops
the SDK reading `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or a login profile instead, and drops
the headers of `ANTHROPIC_CUSTOM_HEADERS`: the reference is the only way a key reaches the provider.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

from pydantic import SecretStr

from agent.keys.references import is_key_reference

MODEL_KEY_REF_FORM: Final = "env:NAME"
#: The shape of an Anthropic API key; the value is checked against it and never quoted.
_KEY_PREFIX: Final = "sk-ant-"
#: Variable names that hold another secret of this deployment (infra/.env.example).
_OTHER_SECRETS: Final = ("ROOT_KEY", "PRIVATE_KEY", "SHARED_SECRET", "KEYSTORE_PASSWORD")


class ModelKeyError(Exception):
    """The model key could not be read. The message names the variable, never a value."""


def check_model_key_reference(text: str) -> str:
    """A Pydantic after-validator: an `env:` key reference. The message never repeats the value."""
    if not is_key_reference(text) or not text.startswith("env:"):
        raise ValueError(
            f"must be a key reference of the form {MODEL_KEY_REF_FORM}; the value is not repeated"
        )
    name = text.removeprefix("env:")
    if name.startswith("AGENT_") or any(secret in name for secret in _OTHER_SECRETS):
        raise ValueError(
            f"names the variable {name}, which holds one of this deployment's other secrets, "
            "not a model API key"
        )
    return text


def resolve_model_key(key_ref: str, environ: Mapping[str, str]) -> SecretStr:
    """The key the reference points at. Raises `ModelKeyError`, quoting no value."""
    try:
        check_model_key_reference(key_ref)
    except ValueError as error:
        raise ModelKeyError(f"AGENT_MODEL_KEY_REF {error}") from None
    name = key_ref.removeprefix("env:")
    value = environ.get(name, "").strip()
    if not value:
        raise ModelKeyError(f"the model key variable {name} is unset or empty")
    if not value.startswith(_KEY_PREFIX):
        raise ModelKeyError(
            f"the model key variable {name} does not hold an Anthropic API key "
            f"({_KEY_PREFIX}…); its value is not repeated"
        )
    return SecretStr(value)
