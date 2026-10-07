"""One agent instance's configuration, from the environment (docs/architecture.md section 8).

Every variable is `AGENT_`-prefixed, so two instances on one host — or one container's environment
leaking into another's — cannot pick up each other's values by accident. The two exceptions are the
root secret itself, whose variable name is whatever `AGENT_ROOT_KEY_REF` points at
(`env:BUYER_ROOT_KEY`), and `KEYSTORE_PASSWORD`; both are read by the key holder, not held here.

The model's API key is not held here either: `AGENT_MODEL_KEY_REF` names its variable, in the `env:`
form only (ADR-086), and `agent.model.resolve_model_key` reads it. `AGENT_MODEL_MAX_TOKENS` caps
every model call's output, thinking included, and is the output bound `BudgetGuard` prices before
the call (ADR-087). `AGENT_MODEL_PRICE_TABLE` names an operator's own price table in place of the
packaged one, and `AGENT_ALLOW_UNKNOWN_PRICE` lets a run call a model the table does not price,
with only the call ceiling to stop it (FR-A9).

The shared secret is the one secret held here, as a `SecretStr` so that it never appears in a repr,
and it must be at least 32 characters: an empty or short secret would make the internal API's HMAC
decorative (docs/api_contract.md section 6).
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Final

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    SecretStr,
    ValidationError,
    ValidationInfo,
    field_validator,
)
from pydantic_settings import BaseSettings, SettingsConfigDict

from agent.keys.references import check_key_reference
from agent.model.credentials import check_model_key_reference
from agent.observation import Role
from agent.signing.setup import DEFAULT_SETUP_GAS_LIMIT_MAX, DEFAULT_SETUP_MAX_COST_WEI

#: ADR-087: room for adaptive thinking at high effort as well as the envelope.
DEFAULT_MODEL_MAX_TOKENS: Final = 16_000


def _empty_is_unset(value: object) -> object:
    return None if value == "" else value


class AgentSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="AGENT_", extra="ignore", frozen=True)

    role: Role
    instance: Annotated[str, Field(min_length=1, pattern=r"^[a-z0-9][a-z0-9-]*$")]
    root_key_ref: Annotated[str, AfterValidator(check_key_reference)]
    shared_secret: Annotated[SecretStr, Field(min_length=32)]
    port: Annotated[int, Field(ge=1, le=65535)]
    host: str = "127.0.0.1"
    setup_gas_limit_max: Annotated[int, Field(gt=0)] = DEFAULT_SETUP_GAS_LIMIT_MAX
    setup_max_cost_wei: Annotated[int, Field(gt=0)] = DEFAULT_SETUP_MAX_COST_WEI
    log_level: str = "INFO"
    #: Unset on an instance that runs no model: it then offers the deterministic policy alone.
    model_key_ref: Annotated[str, AfterValidator(check_model_key_reference)] | None = None
    model_max_tokens: Annotated[int, Field(ge=1, le=128_000)] = DEFAULT_MODEL_MAX_TOKENS
    #: Empty, as `infra/.env.example` leaves it, is unset: the packaged table, never `Path(".")`.
    model_price_table: Annotated[Path | None, BeforeValidator(_empty_is_unset)] = None
    allow_unknown_price: bool = False

    @field_validator("model_key_ref")
    @classmethod
    def _not_the_root(cls, value: str | None, info: ValidationInfo) -> str | None:
        """The variable the root reference names holds the signing root, never a model key."""
        if value is not None and value == info.data.get("root_key_ref"):
            raise ValueError(
                "names the same variable as AGENT_ROOT_KEY_REF, which holds the signing root"
            )
        return value


class SettingsError(Exception):
    """The environment does not configure a valid instance. The message quotes no value."""


def load_settings() -> AgentSettings:
    """Read the settings, reporting each problem by variable and rule and never by value.

    Pydantic's own error text quotes the input, and the most likely mistake here is pasting a key
    where its reference belongs — which would print the key on the way out.
    """
    try:
        return AgentSettings()  # type: ignore[call-arg]  # reason: every field comes from the environment
    except ValidationError as error:
        problems = [
            f"AGENT_{'_'.join(str(part) for part in item['loc']).upper()}: {item['msg']}"
            for item in error.errors(include_input=False, include_url=False, include_context=False)
        ]
        raise SettingsError("invalid agent configuration:\n  " + "\n  ".join(problems)) from None
