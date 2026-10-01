"""The signer: EIP-712 messages from validated state, and the participant's setup approval.

docs/architecture.md section 3.3. Session approval lives here too, because the approved session is
what every signed message is bound to and its check is the signer's refusal to sign for any other
(docs/protocol.md section 3). The whole package is under the 100 percent branch gate
(docs/test_strategy.md section 10).
"""

from agent.signing.messages import ActionKind, SignedAction, sign_decision
from agent.signing.session import (
    ApprovedSession,
    ExpectedSession,
    SessionOpened,
    approve_session,
)
from agent.signing.setup import (
    APPROVE_SELECTOR,
    DEFAULT_SETUP_GAS_LIMIT_MAX,
    DEFAULT_SETUP_MAX_COST_WEI,
    FeeTerms,
    SetupApproval,
    SetupBounds,
    build_setup_approval,
)

__all__ = [
    "APPROVE_SELECTOR",
    "DEFAULT_SETUP_GAS_LIMIT_MAX",
    "DEFAULT_SETUP_MAX_COST_WEI",
    "ActionKind",
    "ApprovedSession",
    "ExpectedSession",
    "FeeTerms",
    "SessionOpened",
    "SetupApproval",
    "SetupBounds",
    "SignedAction",
    "approve_session",
    "build_setup_approval",
    "sign_decision",
]
