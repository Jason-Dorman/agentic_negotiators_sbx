"""Validation codes, as recorded in `decisions.validation_code` (docs/protocol.md section 11.1).

A closed set. The code says which rule refused a decision; the feedback beside it says so in words
to the agent that made it, and to no one else.
"""

from __future__ import annotations

from enum import StrEnum


class Code(StrEnum):
    # Structure: agent_decision.v1.json, and the uint256 bound the pattern cannot express.
    SCHEMA_ERROR = "schema_error"
    EXTRA_FIELDS = "extra_fields"
    INVALID_AMOUNT = "invalid_amount"
    ZERO_AMOUNT = "zero_amount"
    INVALID_OFFER_HASH = "invalid_offer_hash"
    INVALID_REASON = "invalid_reason"
    # Protocol legality, from the observation's public state (docs/protocol.md section 5).
    NOT_YOUR_TURN = "not_your_turn"
    OFFER_LIMIT_REACHED = "offer_limit_reached"
    NO_ACTIVE_OFFER = "no_active_offer"
    SELF_ACCEPTANCE = "self_acceptance"
    STALE_OFFER_HASH = "stale_offer_hash"
    OFFER_EXPIRED = "offer_expired"
    # The mandate and the holdings: the complete trade (ADR-004).
    ABOVE_RESERVATION = "above_reservation"
    BELOW_RESERVATION = "below_reservation"
    INSUFFICIENT_BALANCE = "insufficient_balance"
    INVENTORY_FLOOR = "inventory_floor"


#: The codes that mean "my own holdings rule this out", which the deterministic policy walks away
#: over with `inventory_constraint` (ADR-043).
HOLDINGS_CODES = frozenset({Code.INSUFFICIENT_BALANCE, Code.INVENTORY_FLOOR})
