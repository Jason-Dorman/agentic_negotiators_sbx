"""A typed, read-only view of one observation (docs/protocol.md section 12).

The observation arrives as JSON and is validated against `observation.v1.json` — the normative and
exhaustive form of the allowlist — before anything reads it. This module then gives the validator,
the policies and the signer typed access to what the schema admitted, so none of them indexes into a
dict or passes a bare `str` for an address or an `int` for an amount.

`from_json` assumes a schema-valid document. It still builds every field through the protocol's
value objects, and those are stricter than the schema in one respect worth knowing: Python's
`jsonschema` evaluates `pattern` with `re.search`, under which `$` also matches before a trailing
newline, so `"94000000\\n"` satisfies the schema's amount pattern. `MinorAmount.parse` matches the
whole string and refuses it, and so a document the schema let through can still fail here with
`ProtocolValueError`. The caller treats that as the malformed request it is.

Only what a policy or the validator reads is typed here. `my_previous_decisions` is not: the
deterministic policy has no use for it, and the model policy of stage 3 reads the observation as the
document it sends.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Final, Literal
from uuid import UUID

from negotiation_protocol import Address, Digest, MinorAmount, Sequence, SessionId

Role = Literal["buyer", "seller"]
ROLES: Final[tuple[Role, Role]] = ("buyer", "seller")


def counterparty(role: Role) -> Role:
    return "seller" if role == "buyer" else "buyer"


def as_role(value: str) -> Role:
    """Narrow a string the schema has already restricted to a role."""
    if value == "buyer":
        return "buyer"
    if value == "seller":
        return "seller"
    raise ValueError(f"{value!r} is not a role")


@dataclass(frozen=True, slots=True)
class Mandate:
    """One party's private limits (mandate.v1.json). Never leaves the agent that holds it."""

    reservation_price: MinorAmount
    min_remaining_inventory: MinorAmount
    instructions: str

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Mandate:
        return cls(
            reservation_price=MinorAmount.parse(data["reservation_price_minor"]),
            min_remaining_inventory=MinorAmount.parse(data["min_remaining_inventory_minor"]),
            instructions=data["instructions"],
        )

    def __repr__(self) -> str:
        # A mandate in a traceback or a debug print is a mandate in a log line.
        return "Mandate(<private>)"


@dataclass(frozen=True, slots=True)
class SessionView:
    session_id: SessionId
    config_hash: Digest
    chain_id: int
    exchange_address: Address
    base_token: Address
    quote_token: Address
    base_amount: MinorAmount
    expires_at: int
    max_offers: int
    token_decimals: int

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> SessionView:
        return cls(
            session_id=SessionId(data["session_id"]),
            config_hash=Digest(data["config_hash"]),
            chain_id=data["chain_id"],
            exchange_address=Address(data["exchange_address"]),
            base_token=Address(data["base_token"]),
            quote_token=Address(data["quote_token"]),
            base_amount=MinorAmount.parse(data["base_amount_minor"]),
            expires_at=data["expires_at"],
            max_offers=data["max_offers"],
            token_decimals=data["token_decimals"],
        )


@dataclass(frozen=True, slots=True)
class ActiveOffer:
    offer_hash: Digest
    proposer: Role
    quote_amount: MinorAmount
    valid_until: int
    sequence: Sequence

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> ActiveOffer:
        return cls(
            offer_hash=Digest(data["offer_hash"]),
            proposer=as_role(data["proposer"]),
            quote_amount=MinorAmount.parse(data["quote_amount_minor"]),
            valid_until=data["valid_until"],
            sequence=Sequence(data["sequence"]),
        )


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One confirmed on-chain action. The schema makes the offer fields optional; for an offer the
    consistency check (`agent.consistency`) requires them all."""

    sequence: Sequence
    actor: str
    kind: str
    quote_amount: MinorAmount | None
    valid_until: int | None
    offer_hash: Digest | None
    status: str | None

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> HistoryEntry:
        quote = data.get("quote_amount_minor")
        offer_hash = data.get("offer_hash")
        return cls(
            sequence=Sequence(data["sequence"]),
            actor=data["actor"],
            kind=data["kind"],
            quote_amount=None if quote is None else MinorAmount.parse(quote),
            valid_until=data.get("valid_until"),
            offer_hash=None if offer_hash is None else Digest(offer_hash),
            status=data.get("status"),
        )


@dataclass(frozen=True, slots=True)
class Balances:
    base: MinorAmount
    quote: MinorAmount

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Balances:
        return cls(
            base=MinorAmount.parse(data["base_minor"]),
            quote=MinorAmount.parse(data["quote_minor"]),
        )


@dataclass(frozen=True, slots=True)
class Observation:
    run_id: UUID
    role: Role
    my_address: Address
    session: SessionView
    chain_time: int
    expected_sequence: Sequence
    offers_remaining_for_me: int
    active_offer: ActiveOffer | None
    history: tuple[HistoryEntry, ...]
    my_balances: Balances
    mandate: Mandate

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Observation:
        active = data["active_offer"]
        return cls(
            run_id=UUID(data["run_id"]),
            role=as_role(data["role"]),
            my_address=Address(data["my_address"]),
            session=SessionView.from_json(data["session"]),
            chain_time=data["chain_time"],
            expected_sequence=Sequence(data["expected_sequence"]),
            offers_remaining_for_me=data["offers_remaining_for_me"],
            active_offer=None if active is None else ActiveOffer.from_json(active),
            history=tuple(HistoryEntry.from_json(entry) for entry in data["history"]),
            my_balances=Balances.from_json(data["my_balances"]),
            mandate=Mandate.from_json(data["mandate"]),
        )

    def offers_recorded(self) -> int:
        return sum(1 for entry in self.history if entry.kind == "offer")

    def own_offers_recorded(self) -> int:
        return sum(
            1 for entry in self.history if entry.kind == "offer" and entry.actor == self.role
        )

    def next_proposer(self) -> Role:
        """docs/protocol.md section 5, rule 3, read from confirmed history.

        The buyer makes the first offer; after that only the counterparty of the most recent
        recorded offer's proposer may offer, even when that offer has expired (ADR-016). History
        rather than `active_offer` is the source, because an expired offer is no longer active but
        still decides whose turn it is. "Most recent" is the last entry because history is in
        ascending sequence order, which protocol section 12 requires and `agent.consistency`
        refuses an observation for breaking (ADR-046).
        """
        offers = [entry for entry in self.history if entry.kind == "offer"]
        if not offers:
            return "buyer"
        return counterparty(as_role(offers[-1].actor))

    def __repr__(self) -> str:
        return f"Observation(run_id={self.run_id}, role={self.role}, <private>)"
