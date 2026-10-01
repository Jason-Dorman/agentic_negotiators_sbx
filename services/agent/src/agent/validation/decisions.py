"""What a policy proposed, and what the validator let through.

Two families of types, and the difference between them is the point. A `Proposed*` value is a
decision that parsed: its fields have the right shapes, and nothing more is known. A `Valid*` value
exists only once `MandateValidator.validate` has checked the whole trade against the mandate, the
holdings and the public state. The signer accepts `Valid*` and nothing else, so a decision cannot
reach a signature by skipping the economic checks — mypy refuses the call.

Neither family carries the model's `explanation`, and neither has a default for any field: a
forgotten field is a construction error, never a signable value (the same rule as the protocol's
message dataclasses).
"""

from __future__ import annotations

from dataclasses import dataclass

from negotiation_protocol import CloseReason, Digest, MinorAmount


@dataclass(frozen=True, slots=True)
class ProposedOffer:
    quote_amount: MinorAmount


@dataclass(frozen=True, slots=True)
class ProposedAccept:
    offer_hash: Digest


@dataclass(frozen=True, slots=True)
class ProposedWalkAway:
    reason: CloseReason
    reason_code: int


Proposal = ProposedOffer | ProposedAccept | ProposedWalkAway


@dataclass(frozen=True, slots=True)
class ValidOffer:
    quote_amount: MinorAmount


@dataclass(frozen=True, slots=True)
class ValidAccept:
    offer_hash: Digest


@dataclass(frozen=True, slots=True)
class ValidWalkAway:
    reason: CloseReason
    reason_code: int


ValidDecision = ValidOffer | ValidAccept | ValidWalkAway
