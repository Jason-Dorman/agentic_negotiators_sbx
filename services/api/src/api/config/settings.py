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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import AfterValidator, Field, SecretStr, ValidationError
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


#: The agent's own default bound on a setup approval's gas limit (ADR-042). The backend never asks
#: for more, so a request it builds is never refused for its gas limit (ADR-065).
DEFAULT_SETUP_GAS_LIMIT_MAX: Final = 100_000


@dataclass(frozen=True, slots=True)
class AgentEndpoint:
    """How the backend reaches one agent instance, and the root reference that instance holds.

    `root_key_ref` is sent at provisioning and stored in `wallets.key_ref` (ADR-039); it is a
    reference, never a key. The shared secret is bytes because the MAC is computed over bytes, and
    it never appears in a `repr`.
    """

    base_url: str
    root_key_ref: str
    shared_secret: bytes = field(repr=False)


@dataclass(frozen=True, slots=True)
class ControllerPolicy:
    """The run controller's timing, each a product owner's decision or a stated default."""

    #: How often the indexer is polled, and the relay's stuck transactions looked at, while a run
    #: is driven (build_plan stage 2.4).
    poll_interval_s: float = 1.0
    #: ADR-063: an RPC or agent outage that lasts this long is `RECOVERY_REQUIRED`.
    outage_limit_s: float = 60.0
    #: How long a run's lease lasts unless renewed. The driver renews it at a third of this.
    lease_ttl_s: float = 30.0
    #: ADR-046: a turn refused as `observation_inconsistent` is rebuilt and retried this often.
    observation_retries: int = 5
    #: ADR-065: the most gas the backend asks an agent to sign a setup approval for.
    setup_gas_limit_max: int = DEFAULT_SETUP_GAS_LIMIT_MAX
    #: How long a call to an agent other than a turn may take. A turn's limit comes from the run's
    #: own `model_timeout_s` and repair attempts.
    agent_timeout_s: float = 10.0


class ControllerSettings(BaseSettings):
    """The controller's configuration: the two agents, and its timing. In `infra/.env.example`."""

    model_config = SettingsConfigDict(extra="ignore", frozen=True)

    agent_a_url: Annotated[str, Field(min_length=1)]
    agent_b_url: Annotated[str, Field(min_length=1)]
    agent_a_shared_secret: Annotated[SecretStr, Field(min_length=32)]
    agent_b_shared_secret: Annotated[SecretStr, Field(min_length=32)]
    buyer_root_key_ref: Annotated[str, AfterValidator(check_key_reference)]
    seller_root_key_ref: Annotated[str, AfterValidator(check_key_reference)]
    rpc_outage_limit_s: Annotated[float, Field(gt=0)] = 60.0
    run_lease_ttl_s: Annotated[float, Field(gt=0)] = 30.0
    agent_timeout_s: Annotated[float, Field(gt=0)] = 10.0
    agent_setup_gas_limit_max: Annotated[int, Field(ge=21_000)] = DEFAULT_SETUP_GAS_LIMIT_MAX
    software_version: Annotated[str, Field(min_length=1)] = "0.1.0"

    def endpoints(self) -> dict[str, AgentEndpoint]:
        """Agent A is the buyer's instance and agent B the seller's (`infra/.env.example`)."""
        return {
            "buyer": AgentEndpoint(
                self.agent_a_url,
                self.buyer_root_key_ref,
                self.agent_a_shared_secret.get_secret_value().encode("utf-8"),
            ),
            "seller": AgentEndpoint(
                self.agent_b_url,
                self.seller_root_key_ref,
                self.agent_b_shared_secret.get_secret_value().encode("utf-8"),
            ),
        }

    def policy(self, chain: ChainSettings) -> ControllerPolicy:
        return ControllerPolicy(
            poll_interval_s=chain.indexer_poll_interval_s,
            outage_limit_s=self.rpc_outage_limit_s,
            lease_ttl_s=self.run_lease_ttl_s,
            setup_gas_limit_max=self.agent_setup_gas_limit_max,
            agent_timeout_s=self.agent_timeout_s,
        )


class ApiSettings(BaseSettings):
    """The HTTP service's own configuration (stage 2.5). In `infra/.env.example`.

    `OPERATOR_TOKEN` is the static bearer token of ADR-028: unset or empty, no route asks for one,
    which is right only while the service is bound to localhost. An empty variable is unset, as
    `infra/.env.example` leaves the optional ones.
    """

    model_config = SettingsConfigDict(extra="ignore", frozen=True, env_ignore_empty=True)

    database_url: Annotated[str, Field(min_length=1)]
    #: The deployment manifest this backend serves, loaded and upserted at start-up.
    deployment_manifest: Path
    #: The scenario templates, each validated and upserted at start-up (data model 3.1).
    scenarios_dir: Path = Path("scenarios")
    operator_token: SecretStr | None = None
    #: Which entry of the RPC price table prices this deployment's requests (ADR-061).
    rpc_provider: Annotated[str, Field(min_length=1)] = "anvil"
    #: An operator's own price table; the packaged one when unset.
    rpc_price_table: Path | None = None
    #: Apply the migrations at start-up: the local profile does, a Sepolia host by hand
    #: (data model section 8).
    auto_migrate: bool = False
    api_host: str = "127.0.0.1"
    api_port: Annotated[int, Field(ge=1, le=65535)] = 8000
    log_level: str = "INFO"
    #: How often an SSE stream and an operation look for new run events.
    event_poll_interval_s: Annotated[float, Field(gt=0)] = 0.25
    #: api_contract section 3: a keepalive comment after this long without an event.
    sse_keepalive_s: Annotated[float, Field(gt=0)] = 15.0
    #: api_contract section 1: an idempotency key is kept this long.
    idempotency_retention_h: Annotated[float, Field(gt=0)] = 24.0
    #: ADR-078 as amended (Q63): a key claimed by a request that never answered — the process died,
    #: the client went — is released when next seen this long after the claim.
    idempotency_claim_timeout_s: Annotated[float, Field(gt=0)] = 60.0

    def token(self) -> str | None:
        """The operator token, or None when none is configured: an empty value is none."""
        if self.operator_token is None:
            return None
        value = self.operator_token.get_secret_value()
        return value or None


class SettingsError(Exception):
    """The environment does not configure the backend. The message quotes no value."""


def _problems(error: ValidationError) -> str:
    return "\n  ".join(
        f"{'_'.join(str(part) for part in item['loc']).upper()}: {item['msg']}"
        for item in error.errors(include_input=False, include_url=False, include_context=False)
    )


def load_chain_settings() -> ChainSettings:
    """Read the settings, naming each problem by variable and rule and never by value.

    Pydantic's own message quotes the input, and the likely mistake is a key pasted where its
    reference belongs (the stage 2.2 lesson, docs/contributing.md section 3).
    """
    try:
        return ChainSettings()  # type: ignore[call-arg]  # reason: every field comes from the environment
    except ValidationError as error:
        raise SettingsError("invalid chain configuration:\n  " + _problems(error)) from None


def load_api_settings() -> ApiSettings:
    """As `load_chain_settings`: the operator token is named, never repeated."""
    try:
        return ApiSettings()  # type: ignore[call-arg]  # reason: every field comes from the environment
    except ValidationError as error:
        raise SettingsError("invalid API configuration:\n  " + _problems(error)) from None


def load_controller_settings() -> ControllerSettings:
    """As `load_chain_settings`: a shared secret pasted short, or a key in place of a root's
    reference, is named by variable and rule and never repeated."""
    try:
        return ControllerSettings()  # type: ignore[call-arg]  # reason: every field comes from the environment
    except ValidationError as error:
        raise SettingsError("invalid controller configuration:\n  " + _problems(error)) from None
