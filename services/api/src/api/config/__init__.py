"""Pydantic settings, loaded from the environment and injected; secrets arrive as references.

Built in stage 2.3 of docs/build_plan.md with the chain settings the relay and the indexer need; see
docs/architecture.md sections 3.2 and 8. The bottom layer of the backend: it imports nothing from
the rest of `api`.
"""

from api.config.settings import (
    FEE_BUMP_DENOMINATOR,
    FEE_BUMP_NUMERATOR,
    GWEI,
    ChainSettings,
    IndexerPolicy,
    InvalidRunConfigError,
    RelayPolicy,
    SettingsError,
    confirmation_threshold,
    load_chain_settings,
)

__all__ = [
    "FEE_BUMP_DENOMINATOR",
    "FEE_BUMP_NUMERATOR",
    "GWEI",
    "ChainSettings",
    "IndexerPolicy",
    "InvalidRunConfigError",
    "RelayPolicy",
    "SettingsError",
    "confirmation_threshold",
    "load_chain_settings",
]
