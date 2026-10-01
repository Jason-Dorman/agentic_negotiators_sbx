"""The backend's chain settings, from the environment (docs/architecture.md section 8).

Secrets never live here: the relay and operator keys arrive as references (`RELAY_KEY_REF`,
`OPERATOR_KEY_REF`), checked against the grammar of ADR-049 so that a key pasted where its
reference belongs is refused at start-up rather than carried around as one. The relay resolves the
reference itself, and no setting, error or `repr` here ever holds key material.

The numbers are policy rather than plumbing, and each one is a product owner's decision recorded
in the decision log: the replacement trigger, bump and fee ceiling (ADR-050), the fallback gas limit
for an action the node predicts will revert (ADR-054), the confirmation threshold (Q14). Each is a
setting so an operator can change it without a code change, and each default is the decided value.

`RelayPolicy` and `IndexerPolicy` are what the relay and the indexer are given — plain frozen
values, so a test builds one directly and nothing below the composition root reads the environment.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Annotated, Any, Final

from pydantic import AfterValidator, Field, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from negotiation_protocol.key_refs import check_key_reference

GWEI: Final = 10**9

#: ADR-050. A replacement raises both fee caps by an eighth, 12.5 percent, comfortably above the 10
#: percent a node requires before it will replace a pooled transaction.
FEE_BUMP_NUMERATOR: Final = 9
FEE_BUMP_DENOMINATOR: Final = 8


@dataclass(frozen=True, slots=True)
class RelayPolicy:
    """How the relay prices, bounds and replaces its transactions."""

    #: ADR-050: replace a transaction not included this many blocks after its first broadcast.
    replace_after_blocks: int = 3
    #: ADR-050: no fee cap the relay signs is ever above this; at it, replacement stops.
    max_fee_per_gas_wei: int = 100 * GWEI
    #: ADR-054: the gas limit used when the node predicts a revert, so the revert lands on chain.
    fallback_gas_limit: int = 500_000
    #: The estimate is multiplied by this numerator over 4: a 25 percent margin (ADR-054).
    gas_margin_quarters: int = 5


@dataclass(frozen=True, slots=True)
class IndexerPolicy:
    """How far the indexer reads at once, and the threshold for events no run claims."""

    #: Confirmations for an event whose session belongs to no run (Q14: 1 locally, 2 on Sepolia).
    #: A run's own threshold, from its public configuration, always takes precedence.
    default_confirmation_threshold: int = 1
    #: The widest `eth_getLogs` range asked for in one call; hosted RPCs cap it.
    log_chunk_blocks: int = 2_000


class InvalidRunConfigError(ValueError):
    """A run's public configuration holds a value this backend refuses to guess about (ADR-059).

    Raised rather than defaulted: the run goes back to a person, and nothing is recorded as
    confirmed under a threshold nobody chose.
    """


def confirmation_threshold(public_config: Mapping[str, Any], default: int) -> int:
    """A run's confirmation threshold (Q14), read the same way by the indexer and the projection.

    Absent, it is the deployment's `CONFIRMATION_THRESHOLD`: 1 locally, 2 on Sepolia. Present, it
    must be an integer of at least 1, and anything else is refused (ADR-059).
    """
    if "confirmation_threshold" not in public_config:
        return default
    value = public_config["confirmation_threshold"]
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidRunConfigError("confirmation_threshold must be an integer of at least 1")
    return value


class ChainSettings(BaseSettings):
    """The backend's chain configuration. Variables are documented in `infra/.env.example`."""

    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    chain_rpc_url: Annotated[str, Field(min_length=1)]
    relay_key_ref: Annotated[str, AfterValidator(check_key_reference)]
    operator_key_ref: Annotated[str, AfterValidator(check_key_reference)]
    rpc_timeout_s: Annotated[float, Field(gt=0)] = 10.0
    confirmation_threshold: Annotated[int, Field(ge=1)] = 1
    indexer_poll_interval_s: Annotated[float, Field(gt=0)] = 1.0
    indexer_log_chunk_blocks: Annotated[int, Field(ge=1)] = 2_000
    relay_replace_after_blocks: Annotated[int, Field(ge=1)] = 3
    relay_max_fee_per_gas_wei: Annotated[int, Field(gt=0)] = 100 * GWEI
    relay_fallback_gas_limit: Annotated[int, Field(ge=21_000)] = 500_000

    def relay_policy(self) -> RelayPolicy:
        return RelayPolicy(
            replace_after_blocks=self.relay_replace_after_blocks,
            max_fee_per_gas_wei=self.relay_max_fee_per_gas_wei,
            fallback_gas_limit=self.relay_fallback_gas_limit,
        )

    def indexer_policy(self) -> IndexerPolicy:
        return IndexerPolicy(
            default_confirmation_threshold=self.confirmation_threshold,
            log_chunk_blocks=self.indexer_log_chunk_blocks,
        )


class SettingsError(Exception):
    """The environment does not configure the backend. The message quotes no value."""


def load_chain_settings() -> ChainSettings:
    """Read the settings, naming each problem by variable and rule and never by value.

    Pydantic's own message quotes the input, and the likely mistake is a key pasted where its
    reference belongs (the stage 2.2 lesson, docs/contributing.md section 3).
    """
    try:
        return ChainSettings()  # type: ignore[call-arg]  # reason: every field comes from the environment
    except ValidationError as error:
        problems = [
            f"{'_'.join(str(part) for part in item['loc']).upper()}: {item['msg']}"
            for item in error.errors(include_input=False, include_url=False, include_context=False)
        ]
        raise SettingsError("invalid chain configuration:\n  " + "\n  ".join(problems)) from None
