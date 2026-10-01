"""The session this instance will sign for, and the check that earns it that status.

docs/protocol.md section 3: "Each agent service recomputes `configHash` from its own approved run
configuration and refuses to sign if the on-chain `SessionOpened` event does not match." The backend
decodes the event and sends its fields; this module compares them with what the instance was
provisioned to expect, recomputes the hash with the *provisioned* token addresses rather than the
event's, and reports every field that differs at once.

The expiry window is ADR-044. The operator computes `expiresAt` from the chain head before its
transaction lands, so the block that opens the session is at or after that head: the session may
be shorter than provisioned by the inclusion delay, never longer, and never already expired.

Once approved, the session is the only source of the fields a typed message is bound to. A turn's
observation of the session must match it field for field (`mismatches_with`); the signer never reads
a session id or a config hash from an observation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.errors import SessionMismatchError
from agent.observation import Role, SessionView
from negotiation_protocol import (
    Address,
    Digest,
    Domain,
    MinorAmount,
    SessionConfig,
    SessionId,
)

ZERO_ADDRESS = Address("0x" + "00" * 20)


@dataclass(frozen=True, slots=True)
class ExpectedSession:
    """`expected_session` from provisioning (docs/api_contract.md section 6)."""

    chain_id: int
    exchange_address: Address
    base_token: Address
    quote_token: Address
    base_amount: MinorAmount
    max_offers: int
    session_duration_s: int
    offer_lifetime_s: int


@dataclass(frozen=True, slots=True)
class SessionOpened:
    """The decoded `SessionOpened` event, and the timestamp of the block that emitted it."""

    session_id: SessionId
    buyer: Address
    seller: Address
    base_token: Address
    quote_token: Address
    base_amount: MinorAmount
    expires_at: int
    max_offers: int
    config_hash: Digest
    opened_at: int

    def to_json(self) -> dict[str, Any]:
        """The approve-session body's own field names (docs/api_contract.md section 6)."""
        return {
            "session_id": str(self.session_id),
            "buyer": str(self.buyer),
            "seller": str(self.seller),
            "base_token": str(self.base_token),
            "quote_token": str(self.quote_token),
            "base_amount_minor": self.base_amount.to_json(),
            "expires_at_ts": self.expires_at,
            "max_offers": self.max_offers,
            "config_hash": str(self.config_hash),
            "opened_at_ts": self.opened_at,
        }


@dataclass(frozen=True, slots=True)
class ApprovedSession:
    chain_id: int
    exchange_address: Address
    base_token: Address
    quote_token: Address
    session_id: SessionId
    config_hash: Digest
    buyer: Address
    seller: Address
    base_amount: MinorAmount
    expires_at: int
    max_offers: int
    offer_lifetime_s: int

    def domain(self) -> Domain:
        return Domain(chain_id=self.chain_id, verifying_contract=self.exchange_address)

    def party(self, role: Role) -> Address:
        return self.buyer if role == "buyer" else self.seller

    def mismatches_with(self, view: SessionView) -> dict[str, dict[str, Any]]:
        """Every field of an observed session that is not this one, keyed as the observation is."""
        pairs: dict[str, tuple[Any, Any]] = {
            "session_id": (self.session_id, view.session_id),
            "config_hash": (self.config_hash, view.config_hash),
            "chain_id": (self.chain_id, view.chain_id),
            "exchange_address": (self.exchange_address, view.exchange_address),
            "base_token": (self.base_token, view.base_token),
            "quote_token": (self.quote_token, view.quote_token),
            "base_amount_minor": (self.base_amount, view.base_amount),
            "expires_at": (self.expires_at, view.expires_at),
            "max_offers": (self.max_offers, view.max_offers),
        }
        return _differences(pairs)


def _differences(pairs: dict[str, tuple[Any, Any]]) -> dict[str, dict[str, Any]]:
    return {
        name: {"expected": _plain(expected), "received": _plain(received)}
        for name, (expected, received) in pairs.items()
        if expected != received
    }


def _plain(value: Any) -> Any:
    """JSON-ready: amounts as decimal strings, everything else as its plain value."""
    if isinstance(value, MinorAmount):
        return value.to_json()
    return str(value) if isinstance(value, str) else value


def approve_session(
    expected: ExpectedSession, opened: SessionOpened, role: Role, my_address: Address
) -> ApprovedSession:
    """Approve `opened` for `role`, or raise `SessionMismatchError` naming every differing field."""
    fields = _differences(
        {
            "base_token": (expected.base_token, opened.base_token),
            "quote_token": (expected.quote_token, opened.quote_token),
            "base_amount_minor": (expected.base_amount, opened.base_amount),
            "max_offers": (expected.max_offers, opened.max_offers),
            "config_hash": (_recompute_config_hash(expected, opened), opened.config_hash),
        }
    )
    fields.update(_party_faults(opened, role, my_address))
    fields.update(_expiry_fault(expected, opened))
    if fields:
        raise SessionMismatchError(
            "the opened session is not the one this instance was provisioned for",
            {"fields": fields},
        )
    return ApprovedSession(
        chain_id=expected.chain_id,
        exchange_address=expected.exchange_address,
        base_token=expected.base_token,
        quote_token=expected.quote_token,
        session_id=opened.session_id,
        config_hash=opened.config_hash,
        buyer=opened.buyer,
        seller=opened.seller,
        base_amount=opened.base_amount,
        expires_at=opened.expires_at,
        max_offers=opened.max_offers,
        offer_lifetime_s=expected.offer_lifetime_s,
    )


def _recompute_config_hash(expected: ExpectedSession, opened: SessionOpened) -> Digest:
    config = SessionConfig(
        session_id=opened.session_id.to_bytes(),
        buyer=opened.buyer,
        seller=opened.seller,
        base_amount=opened.base_amount,
        expires_at=opened.expires_at,
        max_offers=opened.max_offers,
    )
    return Digest(config.config_hash(expected.base_token, expected.quote_token))


def _party_faults(
    opened: SessionOpened, role: Role, my_address: Address
) -> dict[str, dict[str, Any]]:
    """This instance in its own slot; a real, different counterparty in the other."""
    mine, theirs = ("buyer", "seller") if role == "buyer" else ("seller", "buyer")
    slots = {"buyer": opened.buyer, "seller": opened.seller}
    faults: dict[str, dict[str, Any]] = {}
    if slots[mine] != my_address:
        faults[mine] = {"expected": str(my_address), "received": str(slots[mine])}
    if slots[theirs] in (my_address, ZERO_ADDRESS):
        faults[theirs] = {
            "expected": "a counterparty other than this instance and not the zero address",
            "received": str(slots[theirs]),
        }
    return faults


def _expiry_fault(expected: ExpectedSession, opened: SessionOpened) -> dict[str, dict[str, Any]]:
    latest = opened.opened_at + expected.session_duration_s
    if opened.opened_at < opened.expires_at <= latest:
        return {}
    return {
        "expires_at_ts": {
            "expected": f"after {opened.opened_at} and at most {latest}",
            "received": opened.expires_at,
        }
    }
