"""The model client: the Anthropic SDK behind `ModelClient` (docs/architecture.md 3.3 and 7).

`ModelPolicy` (stage 3.2) is its only user. The client never decides what an outcome means for a
turn, never retries and never alters what the model wrote.
"""

from agent.model.client import (
    DEFAULT_BASE_URL,
    AnthropicModelClient,
    ModelCallConfig,
    ModelClient,
    anthropic_sdk,
)
from agent.model.credentials import (
    MODEL_KEY_REF_FORM,
    ModelKeyError,
    check_model_key_reference,
    resolve_model_key,
)
from agent.model.envelope import AcceptDecision, DecisionEnvelope, OfferDecision, WalkAwayDecision
from agent.model.result import ModelOutcome, ModelResult, ProviderError

__all__ = [
    "DEFAULT_BASE_URL",
    "MODEL_KEY_REF_FORM",
    "AcceptDecision",
    "AnthropicModelClient",
    "DecisionEnvelope",
    "ModelCallConfig",
    "ModelClient",
    "ModelKeyError",
    "ModelOutcome",
    "ModelResult",
    "OfferDecision",
    "ProviderError",
    "WalkAwayDecision",
    "anthropic_sdk",
    "check_model_key_reference",
    "resolve_model_key",
]
