"""Policies: what an agent decides, never what it may do (docs/architecture.md section 3.3).

`DeterministicPolicy` is the baseline of docs/protocol.md section 13. `ModelPolicy` arrives in
stage 3 behind the same protocol; adding it is a class and a registry entry in the composition root,
not a change here (docs/architecture.md section 9, open/closed).
"""

from agent.policy.base import Policy, PolicyKind, PolicyResponse, Repair
from agent.policy.deterministic import (
    VERSION as DETERMINISTIC_VERSION,
)
from agent.policy.deterministic import (
    DeterministicPolicy,
    concession_price,
    opportunities,
)

__all__ = [
    "DETERMINISTIC_VERSION",
    "DeterministicPolicy",
    "Policy",
    "PolicyKind",
    "PolicyResponse",
    "Repair",
    "concession_price",
    "opportunities",
]
