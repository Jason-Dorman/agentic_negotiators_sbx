"""`MandateValidator`: structural then economic validation, producing private feedback.

docs/architecture.md section 3.3. Every decision a policy makes passes through here before the
signer sees it, and the signer accepts only the `Valid*` types this package produces.
"""

from agent.validation.codes import HOLDINGS_CODES, Code
from agent.validation.decisions import (
    Proposal,
    ProposedAccept,
    ProposedOffer,
    ProposedWalkAway,
    ValidAccept,
    ValidDecision,
    ValidOffer,
    ValidWalkAway,
)
from agent.validation.mandate import MandateValidator, Validation
from agent.validation.structure import ParseResult, Refusal, parse_decision

__all__ = [
    "HOLDINGS_CODES",
    "Code",
    "MandateValidator",
    "ParseResult",
    "Proposal",
    "ProposedAccept",
    "ProposedOffer",
    "ProposedWalkAway",
    "Refusal",
    "ValidAccept",
    "ValidDecision",
    "ValidOffer",
    "ValidWalkAway",
    "Validation",
    "parse_decision",
]
