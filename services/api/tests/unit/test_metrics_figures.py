"""Spec 11.2's figures for one run, against examples worked by hand (test_strategy section 7).

The default scenario's numbers: buyer limit 100 mUSD with 250 mUSD to spend and no floor; seller
floor 90 mUSD holding 25 mASSET and keeping 10; 10 mASSET traded. So the feasible interval is 90 to
100 mUSD and the surplus 10 mUSD whatever the price (spec 11.2). The infeasible clone moves the
seller's floor to 105, above the buyer's limit.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from api.db import (
    ActionKind,
    ActionStatus,
    BalanceSnapshotRecord,
    ChainEventRecord,
    DecisionRecord,
    MandateVersionRecord,
    OutboxRecord,
    OutcomeKind,
    Party,
    PartyOrOperator,
    PolicyKind,
    RunMode,
    RunRecord,
    RunState,
    SignedActionRecord,
    SnapshotStage,
    TokenRole,
    TxKind,
    TxStatus,
    WalletRecord,
)
from api.metrics import MetricsCalculator, figures
from negotiation_protocol import Address, Digest, MinorAmount, SessionId

BASE = 10_000_000
RUN = uuid.uuid4()
BUYER = Address("0x" + "b0" * 20)
SELLER = Address("0x" + "5e" * 20)
T0 = datetime(2026, 10, 2, tzinfo=UTC)


def mandate(party: Party, reservation: int, floor: int) -> MandateVersionRecord:
    return MandateVersionRecord(
        id=uuid.uuid4(),
        run_id=RUN,
        party=party,
        version=1,
        reservation_price_minor=MinorAmount(reservation),
        min_remaining_inventory_minor=MinorAmount(floor),
        instructions="private",
        extra={},
        mandate_hash="0x",
    )


def wallet(party: Party, base: int, quote: int) -> WalletRecord:
    return WalletRecord(
        id=uuid.uuid4(),
        run_id=RUN,
        party=party,
        address=BUYER if party == Party.BUYER else SELLER,
        key_ref="env:X",
        key_derivation={},
        initial_base_minor=MinorAmount(base),
        initial_quote_minor=MinorAmount(quote),
        allowance_minor=MinorAmount(0),
        setup_nonce_next=0,
        funded_tx_hashes={},
    )


def default(seller_floor_price: int = 90_000_000) -> tuple[dict[Party, Any], dict[Party, Any]]:
    mandates = {
        Party.BUYER: mandate(Party.BUYER, 100_000_000, 0),
        Party.SELLER: mandate(Party.SELLER, seller_floor_price, 10_000_000),
    }
    wallets = {
        Party.BUYER: wallet(Party.BUYER, 0, 250_000_000),
        Party.SELLER: wallet(Party.SELLER, 25_000_000, 0),
    }
    return mandates, wallets


def digest(n: int) -> Digest:
    return Digest("0x" + f"{n:064x}")


def offer(sequence: int, signer: Address, quote: int) -> SignedActionRecord:
    return SignedActionRecord(
        id=uuid.uuid4(),
        run_id=RUN,
        turn_id=uuid.uuid4(),
        decision_id=uuid.uuid4(),
        sequence=sequence,
        kind=ActionKind.OFFER,
        typed_message={"quoteAmount": str(quote), "sequence": sequence, "proposer": str(signer)},
        digest=digest(sequence),
        signer=signer,
        signature="0x",
        status=ActionStatus.CONFIRMED,
        revert_error=None,
    )


def accept(sequence: int, signer: Address, offer_sequence: int) -> SignedActionRecord:
    return SignedActionRecord(
        id=uuid.uuid4(),
        run_id=RUN,
        turn_id=uuid.uuid4(),
        decision_id=uuid.uuid4(),
        sequence=sequence,
        kind=ActionKind.ACCEPT,
        typed_message={"offerHash": str(digest(offer_sequence)), "sequence": sequence},
        digest=digest(100 + sequence),
        signer=signer,
        signature="0x",
        status=ActionStatus.CONFIRMED,
        revert_error=None,
    )


# ---------------------------------------------------------------------------------------------
# Feasibility, utilities, violations
# ---------------------------------------------------------------------------------------------


def test_the_default_scenario_is_feasible_between_90_and_100_with_10_of_surplus() -> None:
    result = figures.feasibility(*default(), BASE)
    assert (result.feasible, result.interval, result.surplus) == (
        True,
        (90_000_000, 100_000_000),
        10_000_000,
    )


def test_the_infeasible_clone_has_no_interval() -> None:
    result = figures.feasibility(*default(105_000_000), BASE)
    assert (result.feasible, result.interval, result.surplus) == (False, None, None)


def test_a_buyers_capital_and_a_sellers_inventory_bound_feasibility_too() -> None:
    """ADR-045: the buyer's quote balance less its floor caps what it can pay; the seller must
    keep its floor of the base token after delivering."""
    mandates, wallets = default()
    mandates[Party.BUYER] = mandate(Party.BUYER, 100_000_000, 155_000_000)
    capped = figures.feasibility(mandates, wallets, BASE)
    assert (capped.interval, capped.surplus) == ((90_000_000, 95_000_000), 5_000_000)

    mandates, wallets = default()
    wallets[Party.SELLER] = wallet(Party.SELLER, 19_999_999, 0)
    assert figures.feasibility(mandates, wallets, BASE).feasible is False


def test_utilities_of_the_deterministic_settlement() -> None:
    mandates, wallets = default()
    buyer, seller = figures.utilities(mandates, wallets, BASE, 93_333_333)
    assert (buyer, seller) == (6_666_667, 3_333_333)
    assert buyer is not None and seller is not None
    assert buyer + seller == 10_000_000  # every valid price yields the whole surplus (spec 11.2)
    assert figures.utilities(mandates, wallets, BASE, None) == (None, None)


def test_utilities_use_the_bounds_feasibility_uses_so_the_capture_is_never_above_the_surplus() -> (
    None
):
    """Q66: with the buyer's capital binding at 95, a settlement at 92 captures 3 + 2 = 5, the
    feasible surplus, not 8 + 2 = 10."""
    mandates, wallets = default()
    mandates[Party.BUYER] = mandate(Party.BUYER, 100_000_000, 155_000_000)
    buyer, seller = figures.utilities(mandates, wallets, BASE, 92_000_000)
    assert (buyer, seller) == (3_000_000, 2_000_000)
    assert figures.feasibility(mandates, wallets, BASE).surplus == 5_000_000


def test_mandate_violations_count_offers_and_acceptances_outside_their_signers_mandate() -> None:
    mandates, wallets = default()
    within = [offer(1, BUYER, 80_000_000), offer(2, SELLER, 108_000_000), accept(3, BUYER, 2)]
    assert figures.mandate_violations(within, mandates, wallets, BASE) == 1  # 108 > the buyer's 100

    clean = [offer(1, BUYER, 80_000_000), offer(2, SELLER, 95_000_000), accept(3, BUYER, 2)]
    assert figures.mandate_violations(clean, mandates, wallets, BASE) == 0

    outside = [offer(1, BUYER, 100_000_001), offer(2, SELLER, 89_999_999), accept(3, SELLER, 1)]
    # The buyer offered above its limit, the seller offered below its floor, and the seller then
    # accepted the buyer's 100.000001 — which is within the seller's mandate, so not a third.
    assert figures.mandate_violations(outside, mandates, wallets, BASE) == 2


# ---------------------------------------------------------------------------------------------
# Costs and time
# ---------------------------------------------------------------------------------------------


def tx(
    kind: TxKind,
    nonce: int,
    *,
    gas: int | None,
    price: int = 0,
    sent: int = 0,
    seen: int | None = None,
    status: TxStatus = TxStatus.CONFIRMED,
) -> OutboxRecord:
    return OutboxRecord(
        id=uuid.uuid4(),
        run_id=RUN,
        signed_action_id=None,
        kind=kind,
        sender=BUYER,
        nonce=nonce,
        raw_tx=b"",
        tx_hash=digest(nonce * 10 + int(sent)),
        replaces_id=None,
        status=status,
        attempts=1,
        last_error=None,
        block_number=None if gas is None else 1,
        block_hash=None,
        gas_used=None if gas is None else MinorAmount(gas),
        effective_gas_price_wei=MinorAmount(price),
        submitted_at=T0 + timedelta(milliseconds=sent),
        included_at=None if seen is None else T0 + timedelta(milliseconds=seen),
        submitted_block=0,
        sentence=None,
    )


def test_chain_cost_splits_setup_and_times_a_replacement_from_its_first_send() -> None:
    rows = [
        tx(TxKind.MINT, 0, gas=50_000, price=2, sent=0, seen=1_000),
        # An offer replaced once: the original never mined, the successor mined 2.5 s after the
        # original was first sent.
        tx(TxKind.RECORD_OFFER, 1, gas=None, sent=10_000, status=TxStatus.REPLACED),
        tx(TxKind.RECORD_OFFER, 1, gas=90_000, price=3, sent=11_000, seen=12_500),
        # A reverted settlement still paid for its gas.
        tx(
            TxKind.ACCEPT_AND_SETTLE,
            2,
            gas=40_000,
            price=3,
            sent=20_000,
            seen=20_400,
            status=TxStatus.REVERTED,
        ),
    ]
    setup = figures.chain_cost(rows, figures.SETUP_TXS)
    negotiation = figures.chain_cost(rows, figures.NEGOTIATION_TXS)
    assert (setup.gas_used, setup.fee_wei, setup.wait_ms) == (50_000, 100_000, 1_000)
    assert (negotiation.gas_used, negotiation.fee_wei, negotiation.wait_ms) == (
        130_000,
        390_000,
        2_500 + 400,
    )


def decision(policy: PolicyKind, latency: int, estimated: str | None, usage: Any = None) -> Any:
    return DecisionRecord(
        id=uuid.uuid4(),
        turn_id=uuid.uuid4(),
        run_id=RUN,
        party=Party.BUYER,
        attempt=1,
        policy=policy,
        model_id=None,
        effort=None,
        prompt_template_version=None,
        request_hash=None,
        raw_response={},
        stop_reason=None,
        validation_ok=True,
        validation_code=None,
        validation_feedback=None,
        usage=usage,
        cost_estimated_usd=None if estimated is None else Decimal(estimated),
        cost_reported_usd=None,
        latency_ms=latency,
        requested_at=T0,
        authorized=True,
    )


def test_model_cost_counts_model_calls_only_and_unknown_stays_unknown() -> None:
    deterministic = figures.model_cost([decision(PolicyKind.DETERMINISTIC, 3, None)] * 4)
    assert (deterministic.calls, deterministic.decision_time_ms) == (0, 12)
    assert (deterministic.estimated_usd, deterministic.reported_usd) == (Decimal(0), Decimal(0))

    usage = {"input_tokens": 1_450, "output_tokens": 60}
    model = figures.model_cost(
        [
            decision(PolicyKind.MODEL, 3_000, "0.02", usage),
            decision(PolicyKind.MODEL, 2_000, "0.01", usage),
        ]
    )
    assert (model.calls, model.input_tokens, model.output_tokens) == (2, 2_900, 120)
    assert model.estimated_usd == Decimal("0.03")
    assert model.reported_usd is None, "the provider reported nothing: unknown, not zero"
    unpriced = figures.model_cost([decision(PolicyKind.MODEL, 1, None)])
    assert unpriced.estimated_usd is None


# ---------------------------------------------------------------------------------------------
# Failure class, audit, and the private shape
# ---------------------------------------------------------------------------------------------


def run(
    state: RunState,
    cause: str | None = None,
    termination: str | None = None,
    outcome: OutcomeKind = OutcomeKind.PENDING,
    outcome_tx: Digest | None = None,
) -> RunRecord:
    return RunRecord(
        id=RUN,
        name="r",
        parent_run_id=None,
        batch_id=None,
        scenario_id=None,
        deployment_id="d",
        public_config={"base_amount_minor": str(BASE)},
        limits={},
        buyer_policy=PolicyKind.DETERMINISTIC,
        seller_policy=PolicyKind.DETERMINISTIC,
        buyer_model_id=None,
        seller_model_id=None,
        buyer_effort=None,
        seller_effort=None,
        policy_versions={},
        prompt_template_versions={},
        software_version="t",
        state=state,
        state_cause=cause,
        mode=RunMode.LIVE,
        outcome_kind=outcome,
        outcome_reason_code=None,
        outcome_actor=None if outcome == OutcomeKind.PENDING else PartyOrOperator.SELLER,
        outcome_tx_hash=outcome_tx,
        session_id=None,
        config_hash=None,
        session_expires_at_ts=None,
        started_at=None,
        terminal_at=None,
        termination_cause=termination,
        termination_code=None,
        created_at=T0,
        updated_at=T0,
    )


@pytest.mark.parametrize(
    ("state", "cause", "termination", "expected"),
    [
        (RunState.RUNNING, None, None, None),
        (RunState.TERMINAL, None, None, "none"),
        (RunState.TERMINAL, "abort_requested", "abort_requested", "none"),
        (RunState.TERMINAL, "model_failure", "model_failure", "model"),
        (RunState.TERMINAL, "budget_exhausted", "budget_exhausted", "model"),
        (RunState.TERMINAL, "execution_failure", "execution_failure", "execution"),
        (RunState.FAILED_SETUP, "approve_reverted", None, "execution"),
        (RunState.FAILED_SETUP, "session_refused", "session_refused", "execution"),
        (RunState.RECOVERY_REQUIRED, "rpc_timeout", None, "rpc"),
        (RunState.RECOVERY_REQUIRED, "agent_unavailable", None, "signing"),
        (RunState.RECOVERY_REQUIRED, "agent_session_mismatch", None, "signing"),
        (RunState.RECOVERY_REQUIRED, "settlement_check_failed", None, "execution"),
        (RunState.RECOVERY_REQUIRED, "internal_error", None, None),
        # A fault that crossed a termination says nothing about why the session ends.
        (RunState.RECOVERY_REQUIRED, "rpc_timeout", "model_failure", "rpc"),
        (RunState.RECOVERY_REQUIRED, "unreachable", None, "rpc"),
        (RunState.RECOVERY_REQUIRED, "refused", None, "rpc"),
        (RunState.RECOVERY_REQUIRED, "nonce_conflict", None, "rpc"),
        (RunState.RECOVERY_REQUIRED, "chain_unavailable", None, "rpc"),
        (RunState.RECOVERY_REQUIRED, "observation_inconsistent", None, "signing"),
        (RunState.RECOVERY_REQUIRED, "signed_action_mismatch", None, "signing"),
        (RunState.RECOVERY_REQUIRED, "termination_reverted", "abort_requested", "execution"),
        # Q67: the indexer's two problems with a run that cannot proceed as configured.
        (RunState.RECOVERY_REQUIRED, "session_opening_missing", None, "execution"),
        (RunState.RECOVERY_REQUIRED, "invalid_confirmation_threshold", None, "execution"),
        (RunState.FAILED_SETUP, "abort_requested", "abort_requested", "none"),
        (RunState.TERMINAL, "session_deadline", "session_deadline", "none"),
    ],
)
def test_the_failure_class_follows_the_cause(
    state: RunState, cause: str | None, termination: str | None, expected: str | None
) -> None:
    assert figures.failure_class(run(state, cause, termination)) == expected


def event(name: str, block: int, log: int, tx_hash: Digest, **decoded: Any) -> ChainEventRecord:
    return ChainEventRecord(
        id=uuid.uuid4(),
        run_id=RUN,
        chain_id=31337,
        contract_address=Address("0x" + "ec" * 20),
        session_id=SessionId("0x" + "5a" * 32),
        block_number=block,
        block_hash=digest(1_000 + block),
        tx_hash=tx_hash,
        log_index=log,
        event_name=name,
        decoded=decoded,
        calldata=None,
        canonical=True,
        invalidated_at=None,
        confirmations_at_index=2,
        sentence=None,
        created_at=T0,
    )


def snapshot(stage: SnapshotStage, party: Party, token: TokenRole, amount: int) -> Any:
    return BalanceSnapshotRecord(
        id=uuid.uuid4(),
        run_id=RUN,
        stage=stage,
        party=party,
        token=token,
        amount_minor=MinorAmount(amount),
        block_number=9,
        block_hash=digest(9),
        canonical=True,
    )


def settled_run() -> tuple[RunRecord, list[Any], list[Any], list[Any]]:
    settle_tx = digest(500)
    actions = [offer(1, BUYER, 95_000_000), accept(2, SELLER, 1)]
    events = [
        event("SessionOpened", 1, 0, digest(400), baseAmount=str(BASE)),
        event("OfferRecorded", 2, 0, digest(401), sequence=1, offerHash=str(digest(1))),
        event("AcceptanceRecorded", 3, 0, settle_tx, sequence=2, offerHash=str(digest(1))),
        event("SettlementCompleted", 3, 1, settle_tx, quoteAmount="95000000"),
    ]
    snapshots = []
    for stage, buyer_base, buyer_quote, seller_base, seller_quote in (
        (SnapshotStage.PRE_SETTLEMENT, 0, 250_000_000, 25_000_000, 0),
        (SnapshotStage.POST_SETTLEMENT, BASE, 155_000_000, 15_000_000, 95_000_000),
        (SnapshotStage.TERMINAL, BASE, 155_000_000, 15_000_000, 95_000_000),
    ):
        snapshots += [
            snapshot(stage, Party.BUYER, TokenRole.BASE, buyer_base),
            snapshot(stage, Party.BUYER, TokenRole.QUOTE, buyer_quote),
            snapshot(stage, Party.SELLER, TokenRole.BASE, seller_base),
            snapshot(stage, Party.SELLER, TokenRole.QUOTE, seller_quote),
        ]
    record = run(RunState.TERMINAL, outcome=OutcomeKind.SETTLED, outcome_tx=settle_tx)
    return record, actions, events, snapshots


def test_a_settlement_that_reconciles_is_audit_complete() -> None:
    record, actions, events, snapshots = settled_run()
    assert figures.audit_complete(record, actions, events, snapshots, threshold=2)
    assert figures.settled_quote(record, events) == 95_000_000


def _swap_snapshot(
    snapshots: list[Any], stage: SnapshotStage, party: Party, token: TokenRole, amount: int
) -> list[Any]:
    return [
        snapshot(stage, party, token, amount)
        if (s.stage, s.party, s.token) == (stage, party, token)
        else s
        for s in snapshots
    ]


def _break(name: str, parts: tuple[Any, ...]) -> tuple[Any, ...]:  # noqa: C901  reason: one case per check
    record, actions, events, snapshots, threshold = parts
    post = SnapshotStage.POST_SETTLEMENT
    if name == "threshold":
        threshold = 3
    elif name == "missing_event":
        events = [e for e in events if e.event_name != "OfferRecorded"]
    elif name == "wrong_digest":
        actions = [replace(actions[0], digest=digest(77)), actions[1]]
    elif name == "a_sequence_gap":
        actions = [replace(actions[0], sequence=1), replace(actions[1], sequence=3)]
        events = [
            replace(e, decoded={**e.decoded, "sequence": 3})
            if e.event_name == "AcceptanceRecorded"
            else e
            for e in events
        ]
    elif name == "an_event_with_no_action":
        events = [*events, event("OfferRecorded", 2, 1, digest(402), sequence=9, offerHash="0x1")]
    elif name == "terminal_kind_not_the_outcome":
        record = replace(record, outcome_kind=OutcomeKind.CLOSED)
    elif name == "no_session_opened":
        events = [e for e in events if e.event_name != "SessionOpened"]
    elif name.startswith("leg_"):
        party, token = {
            "leg_buyer_base": (Party.BUYER, TokenRole.BASE),
            "leg_buyer_quote": (Party.BUYER, TokenRole.QUOTE),
            "leg_seller_base": (Party.SELLER, TokenRole.BASE),
            "leg_seller_quote": (Party.SELLER, TokenRole.QUOTE),
        }[name]
        moved = next(s for s in snapshots if (s.stage, s.party, s.token) == (post, party, token))
        snapshots = _swap_snapshot(snapshots, post, party, token, int(moved.amount_minor) + 1)
    elif name == "no_terminal_snapshot":
        snapshots = [s for s in snapshots if s.stage != SnapshotStage.TERMINAL]
    elif name == "pending":
        record = run(RunState.RUNNING)
    elif name == "outcome_tx":
        record = replace(record, outcome_tx_hash=digest(9_999))
    else:
        raise AssertionError(name)
    return record, actions, events, snapshots, threshold


@pytest.mark.parametrize(
    "break_it",
    [
        "threshold",
        "missing_event",
        "wrong_digest",
        "a_sequence_gap",
        "an_event_with_no_action",
        "terminal_kind_not_the_outcome",
        "no_session_opened",
        "leg_buyer_base",
        "leg_buyer_quote",
        "leg_seller_base",
        "leg_seller_quote",
        "no_terminal_snapshot",
        "pending",
        "outcome_tx",
    ],
)
def test_an_audit_fails_on_each_thing_it_checks(break_it: str) -> None:
    record, actions, events, snapshots = settled_run()
    assert figures.audit_complete(record, actions, events, snapshots, 2), "the control holds"
    broken = _break(break_it, (record, actions, events, snapshots, 2))
    assert not figures.audit_complete(*broken)


def test_a_close_is_audited_without_balance_legs() -> None:
    record, actions, events, snapshots = settled_run()
    closed_tx = digest(600)
    events = [
        e for e in events if e.event_name not in ("AcceptanceRecorded", "SettlementCompleted")
    ]
    events.append(event("SessionClosed", 3, 0, closed_tx, sequence=2, reason=1))
    close = replace(actions[1], kind=ActionKind.CLOSE, typed_message={"sequence": 2, "reason": 1})
    record = replace(record, outcome_kind=OutcomeKind.CLOSED, outcome_tx_hash=closed_tx)
    terminal = [s for s in snapshots if s.stage == SnapshotStage.TERMINAL]
    assert figures.audit_complete(record, [actions[0], close], events, terminal, 2)


def test_input_tokens_count_the_cached_ones_too() -> None:
    """Q68: every input token the model was sent."""
    usage = {
        "input_tokens": 100,
        "cache_read_input_tokens": 1_200,
        "cache_creation_input_tokens": 50,
        "output_tokens": 7,
    }
    cost = figures.model_cost([decision(PolicyKind.MODEL, 1, "0.01", usage)])
    assert (cost.input_tokens, cost.output_tokens) == (1_350, 7)


def test_the_efficiency_ratio_is_undefined_when_the_feasible_surplus_is_zero() -> None:
    from api.db import RunMetricsRecord

    def ratio(captured: int | None, surplus: int | None) -> str | None:
        record = RunMetricsRecord(
            run_id=RUN,
            computed_at=T0,
            captured_surplus_minor=captured,
            feasible_surplus_minor=surplus,
        )
        value: str | None = MetricsCalculator.private(record)["efficiency_ratio"]
        return value

    assert ratio(10_000_000, 10_000_000) == "1.000000"
    assert ratio(0, 10_000_000) == "0.000000"
    assert ratio(0, 0) is None
    assert ratio(None, None) is None
