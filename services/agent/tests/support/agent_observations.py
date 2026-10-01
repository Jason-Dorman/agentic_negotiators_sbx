"""Observation documents for the agent's unit tests, built to satisfy `observation.v1.json`.

Every builder returns the JSON document a backend would send (with the mandate injected, as the
service injects it), and `typed()` runs it through the schema before building the typed view — so a
test cannot quietly exercise the validator or a policy on an observation the service itself would
have refused at the door.

The session values are the EIP-712 fixture's (`packages/protocol/fixtures/eip712.v1.json`), so a
signature made from one of these observations can be compared with the fixture's own.

Imported by bare name: `services/agent/tests/support` is on pytest's `pythonpath` and mypy's path.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from agent.observation import Observation, Role
from agent.signing import ApprovedSession
from negotiation_protocol import (
    Address,
    Digest,
    MinorAmount,
    Offer,
    SessionId,
    load_fixture,
    validate,
)

FIXTURE: Final = load_fixture("eip712.v1.json")

RUN_ID: Final = "6f1c2a4e-9d7b-4c3a-8e2f-1a2b3c4d5e6f"
CHAIN_TIME: Final = int(FIXTURE["chain_time"])
EXPIRES_AT: Final = int(FIXTURE["session_config"]["expires_at"])
BASE_AMOUNT: Final = 10_000_000

ADDRESSES: Final[dict[Role, str]] = {
    "buyer": FIXTURE["participants"]["buyer"]["address"],
    "seller": FIXTURE["participants"]["seller"]["address"],
}

# The default scenario's mandates (scenarios/default-overlap.json). Test values, not application
# constants: docs/test_strategy.md section 11 forbids them in application code, not here.
MANDATES: Final[dict[Role, dict[str, str]]] = {
    "buyer": {
        "reservation_price_minor": "100000000",
        "min_remaining_inventory_minor": "0",
        "instructions": "Buy 10 mASSET as cheaply as you can.",
    },
    "seller": {
        "reservation_price_minor": "90000000",
        "min_remaining_inventory_minor": "10000000",
        "instructions": "Sell 10 mASSET for as much as you can.",
    },
}

BALANCES: Final[dict[Role, dict[str, str]]] = {
    "buyer": {"base_minor": "0", "quote_minor": "250000000"},
    "seller": {"base_minor": "25000000", "quote_minor": "0"},
}


def digest_for(sequence: int) -> str:
    """A distinct, well-formed digest per sequence. Not a real EIP-712 digest."""
    return "0x" + f"{sequence:064x}"


def session(max_offers: int = 8, expires_at: int = EXPIRES_AT) -> dict[str, Any]:
    return {
        "session_id": FIXTURE["session_config"]["session_id"],
        "config_hash": FIXTURE["config_hash"],
        "chain_id": FIXTURE["domain"]["chain_id"],
        "exchange_address": FIXTURE["domain"]["verifying_contract"],
        "base_token": FIXTURE["deployment"]["base_token"],
        "quote_token": FIXTURE["deployment"]["quote_token"],
        "base_amount_minor": str(BASE_AMOUNT),
        "expires_at": expires_at,
        "max_offers": max_offers,
        "token_decimals": 6,
    }


def offer_entry(
    sequence: int,
    actor: Role,
    quote: int,
    *,
    valid_until: int = CHAIN_TIME + 600,
    status: str = "replaced",
) -> dict[str, Any]:
    return {
        "sequence": sequence,
        "actor": actor,
        "kind": "offer",
        "quote_amount_minor": str(quote),
        "valid_until": valid_until,
        "offer_hash": digest_for(sequence),
        "status": status,
    }


def alternating_offers(*quotes: int) -> list[dict[str, Any]]:
    """Offers in protocol order — buyer first, then alternating — the last one active."""
    roles: tuple[Role, Role] = ("buyer", "seller")
    entries = [
        offer_entry(index + 1, roles[index % 2], quote) for index, quote in enumerate(quotes)
    ]
    if entries:
        entries[-1]["status"] = "active"
    return entries


def active_from(entry: dict[str, Any]) -> dict[str, Any]:
    return {
        "offer_hash": entry["offer_hash"],
        "proposer": entry["actor"],
        "quote_amount_minor": entry["quote_amount_minor"],
        "valid_until": entry["valid_until"],
        "sequence": entry["sequence"],
    }


def opportunities(role: Role, max_offers: int) -> int:
    """docs/protocol.md section 13: the buyer gets the odd one."""
    return (max_offers + 1) // 2 if role == "buyer" else max_offers // 2


def observation(
    role: Role = "buyer",
    *,
    history: Sequence[dict[str, Any]] = (),
    active: dict[str, Any] | bool | None = True,
    balances: dict[str, str] | None = None,
    mandate: dict[str, str] | None = None,
    max_offers: int = 8,
    chain_time: int = CHAIN_TIME,
    offers_remaining: int | None = None,
) -> dict[str, Any]:
    """One observation for `role`.

    `active=True` derives the active offer from the last history entry, as a backend would while it
    is unexpired; `None` says there is none; a dict is used as given.
    """
    own = sum(1 for entry in history if entry["kind"] == "offer" and entry["actor"] == role)
    if active is True:
        active_offer = active_from(history[-1]) if history else None
    elif active is False:
        active_offer = None
    else:
        active_offer = active
    remaining = offers_remaining
    if remaining is None:
        remaining = max(opportunities(role, max_offers) - own, 0)
    return {
        "schema_version": "1",
        "run_id": RUN_ID,
        "role": role,
        "my_address": ADDRESSES[role],
        "session": session(max_offers=max_offers),
        "chain_time": chain_time,
        "expected_sequence": len(history) + 1,
        "offers_remaining_for_me": remaining,
        "active_offer": active_offer,
        "history": list(history),
        "my_balances": dict(balances or BALANCES[role]),
        "my_previous_decisions": [],
        "mandate": dict(mandate or MANDATES[role]),
    }


def typed(document: dict[str, Any]) -> Observation:
    """The typed view, after the same schema check the service applies."""
    validate(document, "observation.v1.json")
    return Observation.from_json(document)


# --------------------------------------------------------------------------------------
# Consistent observations: real digests, so `agent.consistency` accepts them (ADR-046)
# --------------------------------------------------------------------------------------


def fixture_approval() -> ApprovedSession:
    """The session the fixture describes, as an agent that approved it would hold it."""
    config = FIXTURE["session_config"]
    return ApprovedSession(
        chain_id=FIXTURE["domain"]["chain_id"],
        exchange_address=Address(FIXTURE["domain"]["verifying_contract"]),
        base_token=Address(FIXTURE["deployment"]["base_token"]),
        quote_token=Address(FIXTURE["deployment"]["quote_token"]),
        session_id=SessionId(config["session_id"]),
        config_hash=Digest(FIXTURE["config_hash"]),
        buyer=Address(config["buyer"]),
        seller=Address(config["seller"]),
        base_amount=MinorAmount(int(config["base_amount"])),
        expires_at=int(config["expires_at"]),
        max_offers=int(config["max_offers"]),
        offer_lifetime_s=600,
    )


def real_digest(sequence: int, actor: Role, quote: int, valid_until: int) -> str:
    approval = fixture_approval()
    offer = Offer(
        approval.session_id.to_bytes(),
        approval.config_hash.to_bytes(),
        sequence,
        ADDRESSES[actor],
        quote,
        valid_until,
    )
    return str(Digest(offer.digest(approval.domain())))


def recorded_offers(
    *quotes: int, valid_until: int = CHAIN_TIME + 600, chain_time: int = CHAIN_TIME
) -> list[dict[str, Any]]:
    """Offers as the chain recorded them, buyer first, with the statuses section 12 implies."""
    roles: tuple[Role, Role] = ("buyer", "seller")
    entries = []
    for index, quote in enumerate(quotes):
        actor = roles[index % 2]
        entries.append(
            {
                "sequence": index + 1,
                "actor": actor,
                "kind": "offer",
                "quote_amount_minor": str(quote),
                "valid_until": valid_until,
                "offer_hash": real_digest(index + 1, actor, quote, valid_until),
                "status": "replaced",
            }
        )
    if entries:
        entries[-1]["status"] = "active" if valid_until > chain_time else "expired"
    return entries


def consistent_observation(
    role: Role,
    *quotes: int,
    chain_time: int = CHAIN_TIME,
    valid_until: int = CHAIN_TIME + 600,
    **kwargs: Any,
) -> dict[str, Any]:
    history = recorded_offers(*quotes, valid_until=valid_until, chain_time=chain_time)
    live = bool(history) and valid_until > chain_time
    return observation(
        role,
        history=history,
        active=active_from(history[-1]) if live else None,
        chain_time=chain_time,
        **kwargs,
    )
