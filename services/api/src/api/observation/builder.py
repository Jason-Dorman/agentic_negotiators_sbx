"""The allowlisted observation of docs/protocol.md section 12, built for the acting party alone.

**Privacy-sensitive** (docs/contributing.md section 1.2). Everything an agent learns about the
negotiation passes through `ObservationBuilder.build`, so what it reads is what it can leak:

- **One mandate row, the acting party's.** The builder reads `mandate_versions` only through
  `get_for_party` with the party it is building for (docs/data_model.md section 3.4), and a test
  holds it to that by failing on any other read.
- **Public chain state, and only what is confirmed.** History, turn and sequence come from the run's
  canonical events at its confirmation threshold; an event below the threshold means the chain has
  not settled what happened, and no observation is built until it has. Balances are the party's own
  canonical `post_setup` snapshot — nothing a negotiation does before its terminal event moves them.
- **The party's own decisions**, including its refused attempts and their private feedback, and
  never the counterparty's (protocol 12).

The document is checked against `observation.v1.json` before it leaves, and a failure is reported
by location and rule, never by value: the document holds a mandate.

`body` is what is sent: the document less `mandate`, which the agent injects itself (api_contract
section 6). `document` — with the mandate — is what `turns.observation` stores and what
`observation_hash` covers, which is why the hash equals the one the agent reports exactly when the
two agree on the mandate.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from api.config import confirmation_threshold
from api.db.enums import ActionKind, ActionStatus, Party, SnapshotStage, TokenRole
from api.db.protocols import Transactions, UnitOfWork
from api.db.records import (
    ChainEventRecord,
    DecisionRecord,
    DeploymentRecord,
    RunRecord,
    SignedActionRecord,
    TurnRecord,
)
from negotiation_protocol import json_sha256, validator_for

SCHEMA: Final = "observation.v1.json"
TOKEN_DECIMALS: Final = 6
TERMINAL_EVENTS: Final = frozenset(
    {"SettlementCompleted", "SessionClosed", "SessionExpired", "SessionAborted"}
)

#: What an authorised attempt's action came to, from its signed action's status and kind.
_CONFIRMED: Final = frozenset({ActionStatus.CONFIRMED, ActionStatus.FINALIZED})
_SETTLED_ACTIONS: Final = _CONFIRMED | {ActionStatus.REVERTED}
_RESULT_FOR_KIND: Final = {
    ActionKind.OFFER: "recorded",
    ActionKind.ACCEPT: "settled",
    ActionKind.CLOSE: "closed",
}


class ObservationError(Exception):
    """No observation can be built for the run as it stands. `code` says why."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class BuiltObservation:
    party: Party
    expected_sequence: int
    #: The observation less its mandate: what is sent to the agent.
    body: dict[str, Any]
    #: The observation with the acting party's own mandate. **Private.**
    document: dict[str, Any]
    observation_hash: str


def counterparty(party: Party) -> Party:
    return Party.SELLER if party == Party.BUYER else Party.BUYER


def offer_share(party: Party, max_offers: int) -> int:
    """Protocol 13: the buyer has the odd opportunity."""
    return (max_offers + 1) // 2 if party == Party.BUYER else max_offers // 2


class ObservationBuilder:
    def __init__(self, transactions: Transactions, *, default_threshold: int) -> None:
        self._transactions = transactions
        self._default_threshold = default_threshold

    async def build(self, run_id: uuid.UUID, chain_time: int) -> BuiltObservation:
        """The observation for whichever party acts next (protocol 5, rule 3), at `chain_time`."""
        async with self._transactions.unit_of_work() as uow:
            run, deployment = await _run_and_deployment(uow, run_id)
            threshold = confirmation_threshold(run.public_config, self._default_threshold)
            events = await uow.chain_events.canonical_for_run(run_id)
            roles = {
                str(wallet.address): wallet.party
                for wallet in await uow.wallets.list_for_run(run_id)
            }
            opened, offers = _confirmed_session(events, threshold)
            _actions_settled(await uow.signed_actions.list_for_run(run_id))
            party = _next_party(offers, roles)
            mandate = await uow.mandates.get_for_party(run_id, party)
            if mandate is None:
                raise ObservationError("mandate_missing", f"run {run_id} has no {party} mandate")
            balances = await _own_balances(uow, run_id, party)
            previous = await _previous_decisions(uow, run_id, party)

        max_offers = int(opened.decoded["maxOffers"])
        own_offers = sum(1 for offer in offers if roles[str(offer.decoded["proposer"])] == party)
        address = next(address for address, role in roles.items() if role == party)
        body: dict[str, Any] = {
            "schema_version": "1",
            "run_id": str(run_id),
            "role": party.value,
            "my_address": address,
            "session": _session(opened, deployment),
            "chain_time": chain_time,
            "expected_sequence": _expected_sequence(offers),
            "offers_remaining_for_me": max(offer_share(party, max_offers) - own_offers, 0),
            "active_offer": _active_offer(offers, roles, chain_time),
            "history": _history(offers, roles, chain_time),
            "my_balances": balances,
            "my_previous_decisions": previous,
        }
        document = {**body, "mandate": mandate.as_document()}
        _check_schema(document)
        return BuiltObservation(
            party=party,
            expected_sequence=body["expected_sequence"],
            body=body,
            document=document,
            observation_hash=json_sha256(document),
        )


async def _run_and_deployment(
    uow: UnitOfWork, run_id: uuid.UUID
) -> tuple[RunRecord, DeploymentRecord]:
    run = await uow.runs.get(run_id)
    if run is None:
        raise ObservationError("not_found", f"no run {run_id}")
    deployment = await uow.deployments.get(run.deployment_id)
    if deployment is None:
        raise ObservationError("not_found", f"no deployment {run.deployment_id}")
    return run, deployment


def _confirmed_session(
    events: Sequence[ChainEventRecord], threshold: int
) -> tuple[ChainEventRecord, list[ChainEventRecord]]:
    """The session's opening and its offers, once every canonical event is at the threshold."""
    if any((event.confirmations_at_index or 0) < threshold for event in events):
        raise ObservationError("not_confirmed", "an event of this run is below its threshold")
    if any(event.event_name in TERMINAL_EVENTS for event in events):
        raise ObservationError("session_ended", "the session has a terminal event")
    opened = next((event for event in events if event.event_name == "SessionOpened"), None)
    if opened is None:
        raise ObservationError("session_not_open", "the run's session has not opened")
    offers = sorted(
        (event for event in events if event.event_name == "OfferRecorded"),
        key=lambda event: int(event.decoded["sequence"]),
    )
    return opened, offers


def _actions_settled(actions: Sequence[SignedActionRecord]) -> None:
    """Every action the run signed is confirmed or reverted. One a reorg set back to `submitted`
    is the chain still deciding what happened, and an observation now would ask for a second
    decision on its sequence (stage 2.4 review)."""
    if any(action.status not in _SETTLED_ACTIONS for action in actions):
        raise ObservationError("not_confirmed", "an action of this run is not yet settled")


def _next_party(offers: Sequence[ChainEventRecord], roles: Mapping[str, Party]) -> Party:
    """Protocol 5, rule 3 and ADR-016: the buyer first, then the counterparty of the last offer's
    proposer, whether or not that offer has expired."""
    if not offers:
        return Party.BUYER
    return counterparty(roles[str(offers[-1].decoded["proposer"])])


def _expected_sequence(offers: Sequence[ChainEventRecord]) -> int:
    return int(offers[-1].decoded["sequence"]) + 1 if offers else 1


def _session(opened: ChainEventRecord, deployment: DeploymentRecord) -> dict[str, Any]:
    """From the canonical `SessionOpened` event — what the agents approved — not from the run row,
    which was written before the session existed."""
    args = opened.decoded
    return {
        "session_id": str(args["sessionId"]),
        "config_hash": str(args["configHash"]),
        "chain_id": deployment.chain_id,
        "exchange_address": str(deployment.exchange_address),
        "base_token": str(args["baseToken"]),
        "quote_token": str(args["quoteToken"]),
        "base_amount_minor": str(args["baseAmount"]),
        "expires_at": int(args["expiresAt"]),
        "max_offers": int(args["maxOffers"]),
        "token_decimals": TOKEN_DECIMALS,
    }


def _active_offer(
    offers: Sequence[ChainEventRecord], roles: Mapping[str, Party], chain_time: int
) -> dict[str, Any] | None:
    """The last offer while it stands; null once chain time reaches its `validUntil` (ADR-046)."""
    if not offers or int(offers[-1].decoded["validUntil"]) <= chain_time:
        return None
    args = offers[-1].decoded
    return {
        "offer_hash": str(args["offerHash"]),
        "proposer": roles[str(args["proposer"])].value,
        "quote_amount_minor": str(args["quoteAmount"]),
        "valid_until": int(args["validUntil"]),
        "sequence": int(args["sequence"]),
    }


def _history(
    offers: Sequence[ChainEventRecord], roles: Mapping[str, Party], chain_time: int
) -> list[dict[str, Any]]:
    """Ascending by sequence from 1 (ADR-046): the last offer `active` or `expired`, every earlier
    one `replaced`."""
    entries = []
    for index, offer in enumerate(offers):
        args = offer.decoded
        last = index == len(offers) - 1
        if not last:
            status = "replaced"
        else:
            status = "active" if int(args["validUntil"]) > chain_time else "expired"
        entries.append(
            {
                "sequence": int(args["sequence"]),
                "actor": roles[str(args["proposer"])].value,
                "kind": "offer",
                "quote_amount_minor": str(args["quoteAmount"]),
                "valid_until": int(args["validUntil"]),
                "offer_hash": str(args["offerHash"]),
                "status": status,
            }
        )
    return entries


async def _own_balances(uow: UnitOfWork, run_id: uuid.UUID, party: Party) -> dict[str, str]:
    holdings = {
        snapshot.token: str(int(snapshot.amount_minor))
        for snapshot in await uow.balances.canonical_for_run(run_id)
        if snapshot.stage == SnapshotStage.POST_SETUP and snapshot.party == party
    }
    if TokenRole.BASE not in holdings or TokenRole.QUOTE not in holdings:
        raise ObservationError("balances_missing", f"no post-setup balances for the {party}")
    return {"base_minor": holdings[TokenRole.BASE], "quote_minor": holdings[TokenRole.QUOTE]}


async def _previous_decisions(
    uow: UnitOfWork, run_id: uuid.UUID, party: Party
) -> list[dict[str, Any]]:
    """The party's own attempts, oldest first. An attempt whose `decision` the decision schema
    cannot describe is left out (Q46); the deterministic policy never makes one."""
    decisions = await uow.decisions.list_for_party(run_id, party)
    turns = {turn.id: turn for turn in await uow.turns.list_for_run(run_id)}
    actions = {action.turn_id: action for action in await uow.signed_actions.list_for_run(run_id)}
    previous = []
    for decision in sorted(decisions, key=lambda d: (turns[d.turn_id].turn, d.attempt)):
        entry = _previous(decision, turns[decision.turn_id], actions.get(decision.turn_id))
        if entry is not None:
            previous.append(entry)
    return previous


def _previous(
    decision: DecisionRecord, turn: TurnRecord, action: SignedActionRecord | None
) -> dict[str, Any] | None:
    raw = decision.raw_response
    choice = raw.get("decision") if isinstance(raw, dict) else None
    if not isinstance(choice, dict) or _decision_errors(choice):
        return None
    if not decision.validation_ok:
        entry: dict[str, Any] = {"turn": turn.turn, "decision": choice, "result": "rejected"}
        if decision.validation_feedback is not None:
            entry["feedback"] = decision.validation_feedback
        return entry
    if not decision.authorized or action is None:
        return None
    if action.status == ActionStatus.REVERTED:
        result = "failed"
    elif action.status in _CONFIRMED:
        result = _RESULT_FOR_KIND[action.kind]
    else:
        return None  # not yet confirmed: no observation is built until it is
    return {"turn": turn.turn, "decision": choice, "result": result}


def _decision_errors(choice: Mapping[str, Any]) -> bool:
    validator = validator_for("agent_decision.v1.json").evolve(
        schema={"$ref": "agent_decision.v1.json#/properties/decision"}
    )
    return any(True for _ in validator.iter_errors(choice))


def _check_schema(document: Mapping[str, Any]) -> None:
    problems = sorted(
        {
            f"{'/'.join(str(part) for part in error.absolute_path) or '<root>'}: "
            f"fails {error.validator}"
            for error in validator_for(SCHEMA).iter_errors(document)
        }
    )
    if problems:
        raise ObservationError(
            "schema", "the observation does not match its schema: " + "; ".join(problems)
        )
