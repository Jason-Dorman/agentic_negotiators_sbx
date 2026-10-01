"""The typed-message signer: an EIP-712 message built from validated state, and its signature.

The rule this module exists for (CLAUDE.md, docs/protocol.md section 11): never construct a signed
message from model-supplied bytes. Every field of every message comes from one of four places —

- the approved session: `sessionId`, `configHash`, and the domain's chain and contract;
- the validated decision: the quote amount, the accepted digest or the close reason;
- the observation's `expected_sequence`, and its `chain_time` for `validUntil`;
- the run's own signer: `proposer` or `actor`, which is always this instance's derived address.

`sign_decision` accepts only the `Valid*` types `MandateValidator` produces, and refuses at run time
anything else that reaches it. It also refuses without an approved session, at or after the
session's deadline, and with a signer that is not the approved session's party for its role — each
of which the turn executor checks first, and each of which would otherwise produce a signature for
an action the contract must refuse.

`typed_message` carries the Solidity field names, because it is stored and exported verbatim and
`export.v1.json` restricts its keys to those names. Amounts are decimal strings, as everywhere in
JSON; `uint64` and `uint8` fields are integers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from agent.errors import InvalidStateError, SessionMismatchError
from agent.keys import RunSigner
from agent.signing.session import ApprovedSession
from agent.validation import ValidAccept, ValidDecision, ValidOffer, ValidWalkAway
from negotiation_protocol import Accept, Close, Digest, Offer, Sequence, to_hex32

ActionKind = Literal["offer", "accept", "close"]


@dataclass(frozen=True, slots=True)
class SignedAction:
    kind: ActionKind
    typed_message: dict[str, Any]
    digest: Digest
    signature: str
    signer: str

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "typed_message": self.typed_message,
            "digest": str(self.digest),
            "signature": self.signature,
            "signer": self.signer,
        }


def sign_decision(
    decision: ValidDecision,
    *,
    approval: ApprovedSession | None,
    sequence: Sequence,
    chain_time: int,
    signer: RunSigner,
) -> SignedAction:
    session = _signable_session(approval, chain_time, signer)
    kind, message, typed_message = _build(decision, session, sequence, chain_time, signer)
    digest = message.digest(session.domain())
    signature = signer.sign_digest(digest)
    return SignedAction(
        kind=kind,
        typed_message=typed_message,
        digest=Digest(digest),
        signature="0x" + signature.hex(),
        signer=str(signer.address),
    )


def _signable_session(
    approval: ApprovedSession | None, chain_time: int, signer: RunSigner
) -> ApprovedSession:
    if approval is None:
        raise InvalidStateError(
            "no session has been approved for this run, so nothing can be signed",
            state="provisioned",
            allowed_from=["approved"],
        )
    if chain_time >= approval.expires_at:
        raise InvalidStateError(
            f"the session deadline {approval.expires_at} has passed at chain time {chain_time}",
            state="session_deadline_passed",
            allowed_from=["approved"],
        )
    role = signer.derivation.role
    if approval.party(role) != signer.address:
        raise SessionMismatchError(
            f"this run's signer is not the session's {role}",
            {"fields": {role: {"expected": str(signer.address), "received": approval.party(role)}}},
        )
    return approval


def _build(
    decision: ValidDecision,
    session: ApprovedSession,
    sequence: Sequence,
    chain_time: int,
    signer: RunSigner,
) -> tuple[ActionKind, Offer | Accept | Close, dict[str, Any]]:
    session_id = session.session_id.to_bytes()
    config_hash = session.config_hash.to_bytes()
    seq = int(sequence)
    me = str(signer.address)
    bound: dict[str, Any] = {
        "sessionId": str(session.session_id),
        "configHash": str(session.config_hash),
        "sequence": seq,
    }
    if isinstance(decision, ValidOffer):
        valid_until = min(chain_time + session.offer_lifetime_s, session.expires_at)
        amount = int(decision.quote_amount)
        offer = Offer(session_id, config_hash, seq, me, amount, valid_until)
        fields = {"proposer": me, "quoteAmount": str(amount), "validUntil": valid_until}
        return "offer", offer, {**bound, **fields}
    if isinstance(decision, ValidAccept):
        offer_hash = decision.offer_hash.to_bytes()
        accept = Accept(session_id, config_hash, seq, me, offer_hash)
        return "accept", accept, {**bound, "actor": me, "offerHash": to_hex32(offer_hash)}
    if isinstance(decision, ValidWalkAway):
        close = Close(session_id, config_hash, seq, me, decision.reason_code)
        return "close", close, {**bound, "actor": me, "reason": decision.reason_code}
    raise TypeError(f"only a validated decision can be signed, not {type(decision).__name__}")
