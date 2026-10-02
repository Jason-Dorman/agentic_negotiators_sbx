"""Pydantic settings, loaded from the environment and injected; secrets arrive as references.

Built in stage 2.3 of docs/build_plan.md with the chain settings the relay and the indexer need, and
extended in stage 2.4 with the controller's: the two agents and the run's timing. See
docs/architecture.md sections 3.2 and 8. The bottom layer of the backend: it imports nothing from
the rest of `api`.
"""

from api.config.settings import (
    DEFAULT_SETUP_GAS_LIMIT_MAX,
    FEE_BUMP_DENOMINATOR,
    FEE_BUMP_NUMERATOR,
    GWEI,
    AgentEndpoint,
    ChainSettings,
    ControllerPolicy,
    ControllerSettings,
    IndexerPolicy,
    InvalidRunConfigError,
    RelayPolicy,
    SettingsError,
    confirmation_threshold,
    load_chain_settings,
    load_controller_settings,
)

__all__ = [
    "DEFAULT_SETUP_GAS_LIMIT_MAX",
    "FEE_BUMP_DENOMINATOR",
    "FEE_BUMP_NUMERATOR",
    "GWEI",
    "AgentEndpoint",
    "ChainSettings",
    "ControllerPolicy",
    "ControllerSettings",
    "IndexerPolicy",
    "InvalidRunConfigError",
    "RelayPolicy",
    "SettingsError",
    "confirmation_threshold",
    "load_chain_settings",
    "load_controller_settings",
]
