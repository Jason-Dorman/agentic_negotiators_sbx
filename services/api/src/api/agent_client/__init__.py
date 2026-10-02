"""The authenticated HTTP client to one agent service instance (HMAC per api_contract.md section 6,
ADR-041), and the request bodies built from the run's records.

Built in stage 2.4 of docs/build_plan.md; see docs/architecture.md section 3.2. Failures leave as
unavailable or refused, the distinction the controller acts on (ADR-064).
"""

from api.agent_client.bodies import (
    LIMIT_KEYS,
    approve_session_body,
    provision_body,
    reprovision_body,
    setup_approval_body,
)
from api.agent_client.client import (
    AgentClient,
    AgentError,
    AgentHealth,
    AgentRefusedError,
    AgentUnavailableError,
    DecisionPayload,
    HttpAgentClient,
    Provisioned,
    SessionApproval,
    SetupApproval,
    SignedActionPayload,
    TurnFailure,
    TurnResponse,
)

__all__ = [
    "LIMIT_KEYS",
    "AgentClient",
    "AgentError",
    "AgentHealth",
    "AgentRefusedError",
    "AgentUnavailableError",
    "DecisionPayload",
    "HttpAgentClient",
    "Provisioned",
    "SessionApproval",
    "SetupApproval",
    "SignedActionPayload",
    "TurnFailure",
    "TurnResponse",
    "approve_session_body",
    "provision_body",
    "reprovision_body",
    "setup_approval_body",
]
