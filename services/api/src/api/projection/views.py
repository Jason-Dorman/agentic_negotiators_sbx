"""The session view, the timeline, the balances and the economic outcome, from canonical rows only.

Pure functions over records. Each takes what the repositories' canonical methods return, so a
non-canonical event or snapshot cannot reach a view (data model invariant 6), and a reorg that
invalidates rows rolls every view back the next time it is computed (A14): there is no stored view
to forget to invalidate.

The session view mirrors the contract's own `getSession` rather than interpreting it: an offer
stays active after its `validUntil` and after a close, expiry or abort, and is cleared only by
settlement, exactly as the contract stores it. Whether an offer can still be accepted is a question
of chain time, which the observation builder answers for an agent (protocol section 12) and the
interface shows from `valid_until_ts`; the integration suite compares this view with `getSession`
field by field.

The outcome follows docs/data_model.md section 5: only a canonical terminal event at the run's
confirmation threshold yields one, and the controller (stage 2.4) records it with the run state it
chooses (ADR-052).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

from api.db.enums import OutcomeKind, Party, PartyOrOperator, TokenRole, TxKind, TxStatus
from api.db.records import (
    BalanceSnapshotRecord,
    ChainEventRecord,
    OutboxRecord,
    Outcome,
    SignedActionRecord,
)
from negotiation_protocol import Digest, abort_reason_name, close_reason_name

TERMINAL_EVENTS: Final = {
    "SettlementCompleted": OutcomeKind.SETTLED,
    "SessionClosed": OutcomeKind.CLOSED,
    "SessionExpired": OutcomeKind.EXPIRED,
    "SessionAborted": OutcomeKind.ABORTED,
}

_STATUS_AFTER: Final = {
    "SessionOpened": "open",
    "SettlementCompleted": "settled",
    "SessionClosed": "closed",
    "SessionExpired": "expired",
    "SessionAborted": "aborted",
}

_TIMELINE_KIND: Final = {
    "OfferRecorded": "offer",
    "AcceptanceRecorded": "accept",
    "SettlementCompleted": "settle",
    "SessionClosed": "close",
    "SessionExpired": "expire",
    "SessionAborted": "abort",
}

#: Who a failed lifecycle transaction is attributed to when no participant signed it.
_LIFECYCLE_ACTOR: Final = {
    TxKind.EXPIRE_SESSION: "anyone",
    TxKind.ABORT_SESSION: "operator",
    TxKind.CREATE_SESSION: "operator",
    TxKind.MINT: "operator",
    TxKind.FUND_ETH: "operator",
}


class Parties:
    """Who is who in one session, as its `SessionOpened` event says."""

    def __init__(self, opened: ChainEventRecord) -> None:
        self.buyer = str(opened.decoded["buyer"])
        self.seller = str(opened.decoded["seller"])

    def role(self, address: object) -> Party:
        if address == self.buyer:
            return Party.BUYER
        if address == self.seller:
            return Party.SELLER
        raise ValueError("an event names an address that is neither party")


def _opened(events: Sequence[ChainEventRecord]) -> ChainEventRecord | None:
    return next((event for event in events if event.event_name == "SessionOpened"), None)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------------------------------
# Session view (docs/api_contract.md section 2.2, export `session`)
# ---------------------------------------------------------------------------------------------


def session_view(events: Sequence[ChainEventRecord]) -> dict[str, Any] | None:
    """The run resource's `session` block, or None before `SessionOpened` is canonical."""
    opened = _opened(events)
    if opened is None:
        return None
    parties = Parties(opened)
    config = opened.decoded
    status, sequence, offer_count = "open", 0, 0
    active: dict[str, Any] | None = None
    for event in events:
        args = event.decoded
        status = _STATUS_AFTER.get(event.event_name, status)
        if "sequence" in args and event.event_name != "SessionOpened":
            sequence = max(sequence, int(args["sequence"]))
        if event.event_name == "OfferRecorded":
            offer_count += 1
            active = {
                "offer_hash": str(args["offerHash"]),
                "proposer": parties.role(args["proposer"]).value,
                "quote_amount_minor": str(args["quoteAmount"]),
                "valid_until_ts": int(args["validUntil"]),
                "sequence": int(args["sequence"]),
            }
        elif event.event_name == "SettlementCompleted":
            active = None  # the contract zeroes the active offer on settlement, and only then
    return {
        "session_id": str(config["sessionId"]),
        "config_hash": str(config["configHash"]),
        "buyer_address": parties.buyer,
        "seller_address": parties.seller,
        "base_amount_minor": str(config["baseAmount"]),
        "expires_at_ts": int(config["expiresAt"]),
        "max_offers": int(config["maxOffers"]),
        "status": status,
        "sequence": sequence,
        "offer_count": offer_count,
        "active_offer": active,
        "opened_tx_hash": str(opened.tx_hash),
    }


# ---------------------------------------------------------------------------------------------
# Timeline (docs/api_contract.md sections 2.2 and 4, export `timelineEntry`)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TimelineContext:
    threshold: int
    explorer_base_url: str | None


def _explorer_url(context: TimelineContext, tx_hash: str) -> str | None:
    if not context.explorer_base_url:
        return None
    return f"{context.explorer_base_url.rstrip('/')}/tx/{tx_hash}"


def _event_tx(
    event: ChainEventRecord, outbox: Mapping[str, OutboxRecord], context: TimelineContext
) -> dict[str, Any]:
    confirmations = event.confirmations_at_index or 0
    row = outbox.get(str(event.tx_hash))
    if row is not None:
        status = row.status.value
    else:
        # Not a transaction this backend sent — a third party's expiry, say. Its status is what
        # its depth says, and never `finalized`, which needs the RPC's finalized head.
        status = "confirmed" if confirmations >= context.threshold else "included"
    return {
        "tx_hash": str(event.tx_hash),
        "status": status,
        "block_number": event.block_number,
        "block_hash": str(event.block_hash),
        "confirmations": confirmations,
        "explorer_url": _explorer_url(context, str(event.tx_hash)),
    }


def _entry_fields(
    event: ChainEventRecord, parties: Parties, state: dict[str, Any]
) -> dict[str, Any]:
    """The kind-specific fields of one entry. `state` carries the session forward in order."""
    args, name = event.decoded, event.event_name
    fields: dict[str, Any] = {
        "sequence": int(args.get("sequence", state["sequence"])),
        "kind": _TIMELINE_KIND[name],
        "quote_amount_minor": None,
        "valid_until_ts": None,
        "offer_hash": None,
        "references_offer_hash": None,
        "reason_code": None,
        "reason": None,
    }
    if name == "OfferRecorded":
        state["offers"][str(args["offerHash"])] = str(args["quoteAmount"])
        fields |= {
            "actor": parties.role(args["proposer"]).value,
            "quote_amount_minor": str(args["quoteAmount"]),
            "valid_until_ts": int(args["validUntil"]),
            "offer_hash": str(args["offerHash"]),
        }
    elif name == "AcceptanceRecorded":
        state["acceptor"] = parties.role(args["actor"]).value
        fields |= {
            "actor": state["acceptor"],
            "quote_amount_minor": state["offers"].get(str(args["offerHash"])),
            "references_offer_hash": str(args["offerHash"]),
        }
    elif name == "SettlementCompleted":
        fields |= {
            "sequence": state["sequence"],
            "actor": state["acceptor"],
            "quote_amount_minor": str(args["quoteAmount"]),
            "references_offer_hash": str(args["offerHash"]),
        }
    elif name == "SessionClosed":
        code = int(args["reason"])
        fields |= {
            "actor": parties.role(args["actor"]).value,
            "reason_code": code,
            "reason": close_reason_name(code),
        }
    elif name == "SessionExpired":
        fields["actor"] = PartyOrOperator.ANYONE.value
    else:  # SessionAborted
        code = int(args["reason"])
        fields |= {
            "actor": PartyOrOperator.OPERATOR.value,
            "reason_code": code,
            "reason": abort_reason_name(code),
        }
    state["sequence"] = fields["sequence"]
    return fields


def _failure_actor(row: OutboxRecord, action: SignedActionRecord | None, parties: Parties) -> str:
    """The signer of the action it carried; else a party that signed it itself — an agent's setup
    approval (ADR-040) — else who sends a lifecycle transaction of its kind."""
    if action is not None:
        return parties.role(action.signer).value
    if row.sender in (parties.buyer, parties.seller):
        return parties.role(row.sender).value
    return _LIFECYCLE_ACTOR.get(row.kind, "operator")


def _failure_entry(
    row: OutboxRecord,
    action: SignedActionRecord | None,
    parties: Parties,
    sequence: int,
    context: TimelineContext,
) -> dict[str, Any]:
    recorded_at = row.included_at or row.submitted_at
    if recorded_at is None:
        raise ValueError("a reverted transaction is recorded with the time its receipt was seen")
    return {
        # The action's own sequence, or the session's as it stood when the transaction ran.
        "sequence": action.sequence if action is not None else sequence,
        "kind": "execution_failure",
        "actor": _failure_actor(row, action, parties),
        "quote_amount_minor": None,
        "valid_until_ts": None,
        "offer_hash": None,
        "references_offer_hash": None,
        "reason_code": None,
        "reason": row.last_error,
        "tx": {
            "tx_hash": str(row.tx_hash),
            "status": row.status.value,
            "block_number": row.block_number,
            "block_hash": None if row.block_hash is None else str(row.block_hash),
            "confirmations": 0,
            "explorer_url": _explorer_url(context, str(row.tx_hash)),
        },
        "sentence": row.sentence,
        "recorded_at": _iso(recorded_at),
    }


def build_timeline(
    events: Sequence[ChainEventRecord],
    outbox: Iterable[OutboxRecord],
    actions: Iterable[SignedActionRecord],
    context: TimelineContext,
) -> list[dict[str, Any]]:
    """Every canonical event but `SessionOpened`, and every recorded execution failure, in chain
    order: a failure after every event of its block, since it emitted none of them. Sentences are
    the stored ones; nothing is rendered here (ADR-024)."""
    opened = _opened(events)
    if opened is None:
        return []
    parties = Parties(opened)
    rows = list(outbox)
    by_hash = {str(row.tx_hash): row for row in rows}
    by_action = {action.id: action for action in actions}
    state: dict[str, Any] = {"sequence": 0, "offers": {}, "acceptor": None}

    items: list[tuple[tuple[int, int], ChainEventRecord | OutboxRecord]] = [
        ((event.block_number, event.log_index), event)
        for event in events
        if event.event_name != "SessionOpened"
    ]
    items += [
        ((row.block_number, 2**31), row)
        for row in rows
        if row.status == TxStatus.REVERTED and row.sentence and row.block_number is not None
    ]
    timeline: list[dict[str, Any]] = []
    for _, item in sorted(items, key=lambda pair: pair[0]):
        if isinstance(item, OutboxRecord):
            action = None if item.signed_action_id is None else by_action.get(item.signed_action_id)
            timeline.append(_failure_entry(item, action, parties, state["sequence"], context))
            continue
        entry = _entry_fields(item, parties, state)
        entry["tx"] = _event_tx(item, by_hash, context)
        entry["sentence"] = item.sentence
        entry["recorded_at"] = _iso(item.created_at)
        timeline.append(entry)
    return timeline


# ---------------------------------------------------------------------------------------------
# Balances and outcome
# ---------------------------------------------------------------------------------------------


def latest_balances(snapshots: Iterable[BalanceSnapshotRecord]) -> dict[str, dict[str, str]]:
    """Each party's most recent canonical base and quote balance, as the run resource shows them."""
    latest: dict[tuple[Party, TokenRole], BalanceSnapshotRecord] = {}
    for snapshot in snapshots:
        key = (snapshot.party, snapshot.token)
        if key not in latest or snapshot.block_number >= latest[key].block_number:
            latest[key] = snapshot
    view: dict[str, dict[str, str]] = {}
    for (party, token), snapshot in latest.items():
        if token in (TokenRole.BASE, TokenRole.QUOTE):
            amount = str(int(snapshot.amount_minor))
            view.setdefault(party.value, {})[f"{token.value}_minor"] = amount
    return view


def derive_outcome(events: Sequence[ChainEventRecord], threshold: int) -> Outcome | None:
    """The economic outcome, from a canonical terminal event at the threshold (data model 5)."""
    opened = _opened(events)
    terminal = next((event for event in events if event.event_name in TERMINAL_EVENTS), None)
    if opened is None or terminal is None or (terminal.confirmations_at_index or 0) < threshold:
        return None
    parties = Parties(opened)
    kind = TERMINAL_EVENTS[terminal.event_name]
    args = terminal.decoded
    tx_hash = Digest(str(terminal.tx_hash))
    if kind == OutcomeKind.SETTLED:
        acceptance = next(
            event
            for event in events
            if event.event_name == "AcceptanceRecorded" and event.tx_hash == terminal.tx_hash
        )
        actor = PartyOrOperator(parties.role(acceptance.decoded["actor"]).value)
        return Outcome(kind=kind, actor=actor, tx_hash=tx_hash)
    if kind == OutcomeKind.CLOSED:
        actor = PartyOrOperator(parties.role(args["actor"]).value)
        return Outcome(kind=kind, actor=actor, tx_hash=tx_hash, reason_code=int(args["reason"]))
    if kind == OutcomeKind.EXPIRED:
        return Outcome(kind=kind, actor=PartyOrOperator.ANYONE, tx_hash=tx_hash)
    return Outcome(
        kind=kind,
        actor=PartyOrOperator.OPERATOR,
        tx_hash=tx_hash,
        reason_code=int(args["reason"]),
    )
