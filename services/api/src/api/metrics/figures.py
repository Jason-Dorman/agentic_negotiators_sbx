"""The figures of spec section 11.2 for one run, as pure functions over records.

Each function takes what the repositories return and computes one figure, so each is tested by hand
against a worked example (docs/test_strategy.md section 7) without a database.

**Private figures.** Feasibility, the utilities and the captured surplus need both mandates, and are
private experimental input (data model section 7): they are computed here, stored on `run_metrics`,
and leave the server only through the observer reveal header or a private export.

**Feasibility follows ADR-045.** Each party's inventory floor applies to the token it gives up: the
seller must keep `min_remaining_inventory_minor` of the base token after delivering
`base_amount_minor`, and the buyer must keep its floor of the quote token after paying, so its
quote balance less its floor caps what it can pay, beside its reservation price. A trade is feasible
when the highest price the buyer can pay is at least the lowest the seller can take, which is its
reservation price and never below one minor unit (spec section 11.1 keeps prices strictly positive).

**The utilities use the same two bounds** (Q66): the buyer's is the most it could pay less the
price, the seller's the price less the least it could take. So the captured surplus of a settlement
within both mandates is exactly the feasible surplus, and the efficiency ratio stays within 0 and 1.
Where the buyer's capital does not bind — the default scenarios — the buyer's bound is its
reservation price and these are spec 11.2's reservation utilities.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

from api.db.enums import (
    ActionKind,
    OutcomeKind,
    Party,
    RunState,
    SnapshotStage,
    TokenRole,
    TxKind,
)
from api.db.records import (
    BalanceSnapshotRecord,
    ChainEventRecord,
    DecisionRecord,
    MandateVersionRecord,
    OutboxRecord,
    RunRecord,
    SignedActionRecord,
    WalletRecord,
)

NEGOTIATION_TXS: Final = frozenset(
    {
        TxKind.RECORD_OFFER,
        TxKind.ACCEPT_AND_SETTLE,
        TxKind.CLOSE_SESSION,
        TxKind.ABORT_SESSION,
        TxKind.EXPIRE_SESSION,
    }
)
SETUP_TXS: Final = frozenset({TxKind.CREATE_SESSION, TxKind.MINT, TxKind.APPROVE, TxKind.FUND_ETH})

_TERMINAL_EVENTS: Final = {
    "SettlementCompleted": OutcomeKind.SETTLED,
    "SessionClosed": OutcomeKind.CLOSED,
    "SessionExpired": OutcomeKind.EXPIRED,
    "SessionAborted": OutcomeKind.ABORTED,
}
_ACTION_EVENT: Final = {
    ActionKind.OFFER: "OfferRecorded",
    ActionKind.ACCEPT: "AcceptanceRecorded",
    ActionKind.CLOSE: "SessionClosed",
}
_MINED: Final = frozenset({"included", "confirmed", "finalized"})
_FINISHED: Final = frozenset({RunState.TERMINAL, RunState.FAILED_SETUP})

# docs/data_model.md section 3.13: `failure_class` by the cause that ended or stopped the run.
_RPC_CAUSES: Final = frozenset(
    {"rpc_timeout", "unreachable", "refused", "nonce_conflict", "chain_unavailable"}
)
_MODEL_CAUSES: Final = frozenset({"model_failure", "budget_exhausted"})
_EXECUTION_CAUSES: Final = frozenset(
    {
        "execution_failure",
        "session_refused",
        "settlement_check_failed",
        "termination_reverted",
        # Q67: the indexer's problems with a run that cannot proceed on chain as configured.
        "session_opening_missing",
        "invalid_confirmation_threshold",
    }
)
_SIGNING_PREFIXES: Final = ("agent_", "observation_", "signed_action_")
_INPUT_TOKEN_KEYS: Final = (
    "input_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


# ---------------------------------------------------------------------------------------------
# Feasibility, utilities and mandate violations (private)
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Feasibility:
    feasible: bool
    #: The lowest and highest prices both mandates allow, when there are any.
    interval: tuple[int, int] | None
    surplus: int | None


def _limits(
    mandates: Mapping[Party, MandateVersionRecord],
    wallets: Mapping[Party, WalletRecord],
    base_amount: int,
) -> tuple[int, int, bool]:
    """(highest price the buyer may pay, lowest the seller may take, the seller can deliver)."""
    buyer, seller = mandates[Party.BUYER], mandates[Party.SELLER]
    buyer_cap = min(
        int(buyer.reservation_price_minor),
        int(wallets[Party.BUYER].initial_quote_minor) - int(buyer.min_remaining_inventory_minor),
    )
    seller_floor = max(int(seller.reservation_price_minor), 1)
    deliverable = int(wallets[Party.SELLER].initial_base_minor) - base_amount >= int(
        seller.min_remaining_inventory_minor
    )
    return buyer_cap, seller_floor, deliverable


def feasibility(
    mandates: Mapping[Party, MandateVersionRecord],
    wallets: Mapping[Party, WalletRecord],
    base_amount: int,
) -> Feasibility:
    high, low, deliverable = _limits(mandates, wallets, base_amount)
    if not deliverable or high < low:
        return Feasibility(feasible=False, interval=None, surplus=None)
    return Feasibility(feasible=True, interval=(low, high), surplus=high - low)


def utilities(
    mandates: Mapping[Party, MandateVersionRecord],
    wallets: Mapping[Party, WalletRecord],
    base_amount: int,
    settled_quote: int | None,
) -> tuple[int | None, int | None]:
    """Buyer and seller utility of a settlement against the bounds feasibility uses (Q66); None
    for both without one."""
    if settled_quote is None:
        return None, None
    high, low, _ = _limits(mandates, wallets, base_amount)
    return high - settled_quote, settled_quote - low


def _outside(party: Party, quote: int, limits: tuple[int, int, bool]) -> bool:
    high, low, deliverable = limits
    if party == Party.BUYER:
        return quote > high
    return quote < low or not deliverable


def mandate_violations(
    actions: Sequence[SignedActionRecord],
    mandates: Mapping[Party, MandateVersionRecord],
    wallets: Mapping[Party, WalletRecord],
    base_amount: int,
) -> int:
    """Authorised actions outside a mandate: a signed offer, or a signed acceptance of an offer, at
    a price the signer's own mandate does not allow (reservation price or inventory floor). A close
    is never one. The target is zero."""
    limits = _limits(mandates, wallets, base_amount)
    party_of = {str(wallet.address): party for party, wallet in wallets.items()}
    offers = {
        str(action.digest): int(action.typed_message["quoteAmount"])
        for action in actions
        if action.kind == ActionKind.OFFER
    }
    count = 0
    for action in actions:
        party = party_of.get(str(action.signer))
        if party is None or action.kind == ActionKind.CLOSE:
            continue
        if action.kind == ActionKind.OFFER:
            quote: int | None = int(action.typed_message["quoteAmount"])
        else:
            quote = offers.get(str(action.typed_message["offerHash"]))
        if quote is not None and _outside(party, quote, limits):
            count += 1
    return count


# ---------------------------------------------------------------------------------------------
# Costs and time
# ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChainCost:
    gas_used: int = 0
    fee_wei: int = 0
    wait_ms: int = 0


def chain_cost(rows: Iterable[OutboxRecord], kinds: frozenset[TxKind]) -> ChainCost:
    """Gas and fee of every transaction of these kinds that was mined — a reverted one costs gas
    too — and the time each waited from its first broadcast at that sender and nonce, replaced
    predecessors included, until its receipt was seen."""
    rows = list(rows)
    first_sent: dict[tuple[str, int], datetime] = {}
    for row in rows:
        if row.submitted_at is not None:
            key = (str(row.sender), row.nonce)
            first_sent[key] = min(first_sent.get(key, row.submitted_at), row.submitted_at)
    gas = fee = wait = 0
    for row in rows:
        if row.kind not in kinds or row.gas_used is None:
            continue
        gas += int(row.gas_used)
        fee += int(row.gas_used) * int(row.effective_gas_price_wei or 0)
        sent = first_sent.get((str(row.sender), row.nonce))
        if sent is not None and row.included_at is not None:
            wait += max(0, int((row.included_at - sent).total_seconds() * 1000))
    return ChainCost(gas_used=gas, fee_wei=fee, wait_ms=wait)


@dataclass(frozen=True, slots=True)
class ModelCost:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    #: None when any call's price is unknown: unknown is never shown as zero (FR-A9).
    estimated_usd: Decimal | None = Decimal(0)
    reported_usd: Decimal | None = Decimal(0)
    decision_time_ms: int = 0


def model_cost(decisions: Iterable[DecisionRecord]) -> ModelCost:
    """Decision time is every attempt's latency, either policy's; calls, tokens and cost are the
    model's attempts only — a deterministic decision calls no model."""
    calls = input_tokens = output_tokens = elapsed = 0
    estimated: Decimal | None = Decimal(0)
    reported: Decimal | None = Decimal(0)
    for decision in decisions:
        elapsed += decision.latency_ms or 0
        if decision.policy.value != "model":
            continue
        calls += 1
        usage = decision.usage or {}
        # Q68: every input token the model was sent, the cached ones included.
        input_tokens += sum(int(usage.get(key, 0)) for key in _INPUT_TOKEN_KEYS)
        output_tokens += int(usage.get("output_tokens", 0))
        estimated = _add(estimated, decision.cost_estimated_usd)
        reported = _add(reported, decision.cost_reported_usd)
    return ModelCost(calls, input_tokens, output_tokens, estimated, reported, elapsed)


def _add(total: Decimal | None, value: Decimal | None) -> Decimal | None:
    return None if total is None or value is None else total + value


# ---------------------------------------------------------------------------------------------
# Outcome, failure class and audit
# ---------------------------------------------------------------------------------------------


def settled_quote(run: RunRecord, events: Iterable[ChainEventRecord]) -> int | None:
    if run.outcome_kind != OutcomeKind.SETTLED:
        return None
    settlement = next((e for e in events if e.event_name == "SettlementCompleted"), None)
    return None if settlement is None else int(settlement.decoded["quoteAmount"])


def failure_class(run: RunRecord) -> str | None:
    """Spec 11.2's operational failure classes, from the cause that ended or stopped the run:
    `model`, `signing`, `rpc`, `execution`, or `none` for a run that ended without a fault. None
    while the run is still going, and for a stop no class describes."""
    if run.state not in _FINISHED and run.state != RunState.RECOVERY_REQUIRED:
        return None
    cause = run.termination_cause or run.state_cause or ""
    if run.state == RunState.RECOVERY_REQUIRED:
        cause = run.state_cause or ""
    if cause in _MODEL_CAUSES:
        return "model"
    if cause in _EXECUTION_CAUSES or cause.endswith("_reverted"):
        return "execution"
    if cause in _RPC_CAUSES:
        return "rpc"
    if cause == "agent_unavailable" or cause.startswith(_SIGNING_PREFIXES):
        return "signing"
    if run.state in _FINISHED:
        return "none"
    return None


def _actions_recorded(
    actions: Iterable[SignedActionRecord], events: Sequence[ChainEventRecord]
) -> bool:
    """Every mined signed action has its canonical event, and every action event a signed action."""
    by_sequence = {
        (event.event_name, int(event.decoded["sequence"])): event
        for event in events
        if event.event_name in _ACTION_EVENT.values()
    }
    mined = [action for action in actions if action.status.value in _MINED]
    for action in mined:
        event = by_sequence.get((_ACTION_EVENT[action.kind], action.sequence))
        if event is None:
            return False
        if action.kind == ActionKind.OFFER and str(event.decoded["offerHash"]) != str(
            action.digest
        ):
            return False
    sequences = sorted(action.sequence for action in mined)
    return len(by_sequence) == len(mined) and sequences == list(range(1, len(sequences) + 1))


def _deltas_match(snapshots: Iterable[BalanceSnapshotRecord], base_amount: int, quote: int) -> bool:
    """Data model invariant 1: post-settlement less pre-settlement is exactly the two legs."""
    amounts = {
        (snapshot.stage, snapshot.party, snapshot.token): int(snapshot.amount_minor)
        for snapshot in snapshots
    }
    expected = {
        (Party.BUYER, TokenRole.BASE): base_amount,
        (Party.BUYER, TokenRole.QUOTE): -quote,
        (Party.SELLER, TokenRole.BASE): -base_amount,
        (Party.SELLER, TokenRole.QUOTE): quote,
    }
    for (party, token), delta in expected.items():
        before = amounts.get((SnapshotStage.PRE_SETTLEMENT, party, token))
        after = amounts.get((SnapshotStage.POST_SETTLEMENT, party, token))
        if before is None or after is None or after - before != delta:
            return False
    return True


def audit_complete(
    run: RunRecord,
    actions: Sequence[SignedActionRecord],
    events: Sequence[ChainEventRecord],
    snapshots: Sequence[BalanceSnapshotRecord],
    threshold: int,
) -> bool:
    """Spec 11.2's audit completeness, checked against the canonical chain events the indexer read
    and verified (Q58): the outcome is the canonical terminal event's, at the threshold; every
    authorised public action that was mined has its event, in a gapless sequence; both parties'
    terminal balances are recorded; and a settlement's balances moved by exactly its two legs. The
    database-free reconstruction from the chain is the A15 tool's, run apart from the metrics."""
    if run.outcome_kind == OutcomeKind.PENDING:
        return False
    terminal = next((e for e in events if e.event_name in _TERMINAL_EVENTS), None)
    if (
        terminal is None
        or _TERMINAL_EVENTS[terminal.event_name] != run.outcome_kind
        or str(terminal.tx_hash) != str(run.outcome_tx_hash)
        or (terminal.confirmations_at_index or 0) < threshold
        or not _actions_recorded(actions, events)
    ):
        return False
    final = {(s.party, s.token) for s in snapshots if s.stage == SnapshotStage.TERMINAL}
    if not {(p, t) for p in Party for t in (TokenRole.BASE, TokenRole.QUOTE)} <= final:
        return False
    if run.outcome_kind != OutcomeKind.SETTLED:
        return True
    opened = next((e for e in events if e.event_name == "SessionOpened"), None)
    if opened is None:
        return False
    base_amount = int(opened.decoded["baseAmount"])
    return _deltas_match(snapshots, base_amount, int(terminal.decoded["quoteAmount"]))
