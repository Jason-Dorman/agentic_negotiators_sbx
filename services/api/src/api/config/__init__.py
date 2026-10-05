"""Pydantic settings, loaded from the environment and injected; secrets arrive as references.

Built in stage 2.3 of docs/build_plan.md with the chain settings the relay and the indexer need, and
extended in stage 2.4 with the controller's: the two agents and the run's timing, and in stage 2.5
with the HTTP service's own and the RPC price table (ADR-061). See
docs/architecture.md sections 3.2 and 8. The bottom layer of the backend: it imports nothing from
the rest of `api`.
"""

from api.config.prices import (
    DEFAULT_RPC_PRICE_TABLE,
    PriceTableError,
    ProviderPrice,
    RpcPriceTable,
)
from api.config.settings import (
    DEFAULT_SETUP_GAS_LIMIT_MAX,
    FEE_BUMP_DENOMINATOR,
    FEE_BUMP_NUMERATOR,
    GWEI,
    AgentEndpoint,
    ApiSettings,
    ChainSettings,
    ControllerPolicy,
    ControllerSettings,
    IndexerPolicy,
    InvalidRunConfigError,
    RelayPolicy,
    SettingsError,
    confirmation_threshold,
    load_api_settings,
    load_chain_settings,
    load_controller_settings,
)

__all__ = [
    "DEFAULT_RPC_PRICE_TABLE",
    "DEFAULT_SETUP_GAS_LIMIT_MAX",
    "FEE_BUMP_DENOMINATOR",
    "FEE_BUMP_NUMERATOR",
    "GWEI",
    "AgentEndpoint",
    "ApiSettings",
    "ChainSettings",
    "ControllerPolicy",
    "ControllerSettings",
    "IndexerPolicy",
    "InvalidRunConfigError",
    "PriceTableError",
    "ProviderPrice",
    "RelayPolicy",
    "RpcPriceTable",
    "SettingsError",
    "confirmation_threshold",
    "load_api_settings",
    "load_chain_settings",
    "load_controller_settings",
]
