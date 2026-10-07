"""Policies: what an agent decides, never what it may do (docs/architecture.md section 3.3).

`DeterministicPolicy` is the baseline of docs/protocol.md section 13. `ModelPolicy` sits behind the
same protocol; adding it was a class and a registry entry in the composition root, not a change
here (docs/architecture.md section 9, open/closed).
"""

from agent.policy.base import FailureCode, Policy, PolicyFailure, PolicyKind, PolicyResponse, Repair
from agent.policy.deterministic import (
    VERSION as DETERMINISTIC_VERSION,
)
from agent.policy.deterministic import (
    DeterministicPolicy,
    concession_price,
    opportunities,
)
from agent.policy.model import VERSION as MODEL_VERSION
from agent.policy.model import ModelPolicy, observation_message, policy_result

__all__ = [
    "DETERMINISTIC_VERSION",
    "MODEL_VERSION",
    "DeterministicPolicy",
    "FailureCode",
    "ModelPolicy",
    "Policy",
    "PolicyFailure",
    "PolicyKind",
    "PolicyResponse",
    "Repair",
    "concession_price",
    "observation_message",
    "opportunities",
    "policy_result",
]
