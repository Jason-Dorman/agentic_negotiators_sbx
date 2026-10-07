"""The model client: the Anthropic SDK behind `ModelClient` (docs/architecture.md 3.3 and 7).

`ModelPolicy` is its only user. The client never decides what an outcome means for a turn, never
retries and never alters what the model wrote. `FixtureModelClient` answers in its place from a
canned script, on an instance configured for it (ADR-088). Either sits behind
`CheckedModelClient`, the outbound-context assertion (ADR-092).
"""

from agent.model.checked import CheckedModelClient, RequestObserver
from agent.model.client import (
    DEFAULT_BASE_URL,
    AnthropicModelClient,
    ModelCallConfig,
    ModelClient,
    anthropic_sdk,
    request_body,
    sort_answer,
)
from agent.model.credentials import (
    MODEL_KEY_REF_FORM,
    ModelKeyError,
    check_model_key_reference,
    resolve_model_key,
)
from agent.model.envelope import AcceptDecision, DecisionEnvelope, OfferDecision, WalkAwayDecision
from agent.model.fixtures import (
    FixtureError,
    FixtureModelClient,
    FixtureScript,
    load_fixture_scripts,
)
from agent.model.result import ModelOutcome, ModelResult, ProviderError
from agent.model.runtime import ClientMaker, ModelMode, ModelRuntime, RunModelLimits

__all__ = [
    "DEFAULT_BASE_URL",
    "MODEL_KEY_REF_FORM",
    "AcceptDecision",
    "AnthropicModelClient",
    "CheckedModelClient",
    "ClientMaker",
    "DecisionEnvelope",
    "FixtureError",
    "FixtureModelClient",
    "FixtureScript",
    "ModelCallConfig",
    "ModelClient",
    "ModelKeyError",
    "ModelMode",
    "ModelOutcome",
    "ModelResult",
    "ModelRuntime",
    "OfferDecision",
    "ProviderError",
    "RequestObserver",
    "RunModelLimits",
    "WalkAwayDecision",
    "anthropic_sdk",
    "check_model_key_reference",
    "load_fixture_scripts",
    "request_body",
    "resolve_model_key",
    "sort_answer",
]
