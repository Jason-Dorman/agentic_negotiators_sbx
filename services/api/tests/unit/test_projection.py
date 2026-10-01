"""The projection over synthetic canonical rows: sentences, view, timeline, outcome, balances.

Test strategy section 7: one sentence case per timeline kind, and a source-level check that the
projection never reads non-canonical rows. The integration suite then compares the same views with
what a real chain recorded and with the contract's own `getSession`.
"""

from __future__ import annotations

import ast
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from api.db import (
    ActionKind,
    ActionStatus,
    BalanceSnapshotRecord,
    ChainEventRecord,
    OutboxRecord,
    OutcomeKind,
    Party,
    PartyOrOperator,
    SignedActionRecord,
    SnapshotStage,
    TokenRole,
    TxKind,
    TxStatus,
)
from api.projection import (
    SentenceContextError,
    TimelineContext,
    TimelineSentences,
    build_timeline,
    derive_outcome,
    format_minor,
    latest_balances,
    session_view,
)
from negotiation_protocol import Address, Digest, MinorAmount, SessionId

BUYER = "0x" + "44" * 20
SELLER = "0x" + "55" * 20
SESSION = "0x" + "aa" * 32
OFFER_1 = "0x" + "b1" * 32
OFFER_2 = "0x" + "b2" * 32
RUN = uuid.uuid4()
WHEN = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)

OPENED = {
    "sessionId": SESSION,
    "buyer": Address(BUYER),
    "seller": Address(SELLER),
    "baseToken": "0x" + "22" * 20,
    "quoteToken": "0x" + "33" * 20,
    "baseAmount": "10000000",
    "expiresAt": 1758300000,
    "maxOffers": 8,
    "configHash": "0x" + "cc" * 32,
}


def _offer(sequence: int, proposer: str, amount: str, digest: str) -> dict[str, Any]:
    return {
        "sessionId": SESSION,
        "sequence": sequence,
        "proposer": Address(proposer),
        "quoteAmount": amount,
        "validUntil": 1758299400,
        "offerHash": digest,
    }


def _event(
    name: str, args: dict[str, Any], block: int, log: int = 0, tx: int | None = None, depth: int = 1
) -> ChainEventRecord:
    return ChainEventRecord(
        id=uuid.uuid4(),
        run_id=RUN,
        chain_id=31337,
        contract_address=Address("0x" + "11" * 20),
        session_id=SessionId(SESSION),
        block_number=block,
        block_hash=Digest(bytes([block]) * 32),
        tx_hash=Digest(bytes([tx if tx is not None else block + 100]) * 32),
        log_index=log,
        event_name=name,
        decoded=args,
        calldata=None,
        canonical=True,
        invalidated_at=None,
        confirmations_at_index=depth,
        sentence=f"sentence {block}.{log}",
        created_at=WHEN,
    )


def _settled_session() -> list[ChainEventRecord]:
    return [
        _event("SessionOpened", OPENED, 1),
        _event("OfferRecorded", _offer(1, BUYER, "80000000", OFFER_1), 2),
        _event("OfferRecorded", _offer(2, SELLER, "96000000", OFFER_2), 3),
        _event(
            "AcceptanceRecorded",
            {"sessionId": SESSION, "sequence": 3, "actor": Address(BUYER), "offerHash": OFFER_2},
            4,
            0,
            tx=40,
        ),
        _event(
            "SettlementCompleted",
            {
                "sessionId": SESSION,
                "buyer": BUYER,
                "seller": SELLER,
                "baseToken": "0x" + "22" * 20,
                "baseAmount": "10000000",
                "quoteToken": "0x" + "33" * 20,
                "quoteAmount": "96000000",
                "offerHash": OFFER_2,
            },
            4,
            3,
            tx=40,
        ),
    ]


# ---------------------------------------------------------------------------------------------
# Sentences (api_contract section 4): one case per kind
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "text"),
    [
        (92_000_000, "92"),
        (93_333_333, "93.333333"),
        (10_000_000, "10"),
        (500_000, "0.5"),
        (0, "0"),
        (1, "0.000001"),
        (MinorAmount(97_000_000), "97"),
        ("86666666", "86.666666"),
    ],
)
def test_amounts_render_with_six_decimals_and_no_trailing_zeros(amount: Any, text: str) -> None:
    assert format_minor(amount) == text


def test_a_negative_amount_is_not_rendered() -> None:
    with pytest.raises(ValueError, match="never negative"):
        format_minor(-1)


def _earlier(*events: ChainEventRecord) -> list[tuple[str, dict[str, Any]]]:
    return [(event.event_name, event.decoded) for event in events]


@pytest.mark.parametrize(
    ("name", "args", "sentence"),
    [
        (
            "OfferRecorded",
            _offer(1, BUYER, "92000000", OFFER_1),
            "Buyer offers 92 mUSD for 10 mASSET.",
        ),
        (
            "OfferRecorded",
            _offer(2, SELLER, "97000000", OFFER_2),
            "Seller counters at 97 mUSD for 10 mASSET.",
        ),
        (
            "OfferRecorded",
            _offer(3, BUYER, "93333333", OFFER_2),
            "Buyer counters at 93.333333 mUSD for 10 mASSET.",
        ),
        (
            "AcceptanceRecorded",
            {"sessionId": SESSION, "sequence": 2, "actor": Address(SELLER), "offerHash": OFFER_1},
            "Seller accepts 95 mUSD for 10 mASSET.",
        ),
        (
            "SettlementCompleted",
            {"baseAmount": "10000000", "quoteAmount": "95000000"},
            "Settled: 10 mASSET to buyer, 95 mUSD to seller.",
        ),
        (
            "SessionClosed",
            {"sessionId": SESSION, "sequence": 3, "actor": Address(SELLER), "reason": 3},
            "Seller walks away (no further concession).",
        ),
        (
            "SessionClosed",
            {"sessionId": SESSION, "sequence": 3, "actor": Address(BUYER), "reason": 1},
            "Buyer walks away (terms unacceptable).",
        ),
        (
            "SessionExpired",
            {"sessionId": SESSION, "expiresAt": 1758300000},
            "Session expired at 16:40:00 UTC.",
        ),
        (
            "SessionAborted",
            {"sessionId": SESSION, "operator": BUYER, "reason": 2},
            "Operator aborted the session (model failure).",
        ),
    ],
)
def test_one_sentence_per_kind(name: str, args: dict[str, Any], sentence: str) -> None:
    earlier = _earlier(
        _event("SessionOpened", OPENED, 1),
        _event("OfferRecorded", _offer(1, BUYER, "95000000", OFFER_1), 2),
    )
    assert TimelineSentences().event_sentence(name, args, earlier) == sentence


def test_the_opening_has_no_sentence_and_a_failure_has_its_own() -> None:
    renderer = TimelineSentences()
    assert renderer.event_sentence("SessionOpened", OPENED, []) is None
    assert (
        renderer.execution_failure_sentence("SequenceMismatch")
        == "Transaction reverted: SequenceMismatch. No trade occurred."
    )


def test_a_sentence_without_its_context_is_refused_not_guessed() -> None:
    renderer = TimelineSentences()
    offer = _offer(1, BUYER, "92000000", OFFER_1)
    with pytest.raises(SentenceContextError, match="SessionOpened"):
        renderer.event_sentence("OfferRecorded", offer, [])
    stranger = {**offer, "proposer": "0x" + "66" * 20}
    with pytest.raises(SentenceContextError, match="neither party"):
        renderer.event_sentence(
            "OfferRecorded", stranger, _earlier(_event("SessionOpened", OPENED, 1))
        )
    unknown = {"sessionId": SESSION, "sequence": 2, "actor": Address(SELLER), "offerHash": OFFER_2}
    with pytest.raises(SentenceContextError, match="accepted offer"):
        renderer.event_sentence(
            "AcceptanceRecorded", unknown, _earlier(_event("SessionOpened", OPENED, 1))
        )


# ---------------------------------------------------------------------------------------------
# Session view, timeline, outcome, balances
# ---------------------------------------------------------------------------------------------


def test_the_session_view_of_a_settlement() -> None:
    view = session_view(_settled_session())
    assert view is not None
    assert (view["status"], view["sequence"], view["offer_count"], view["active_offer"]) == (
        "settled",
        3,
        2,
        None,
    )
    assert view["buyer_address"] == BUYER
    assert view["opened_tx_hash"] == str(Digest(bytes([101]) * 32))
    assert session_view([]) is None


def test_the_active_offer_survives_a_close_as_the_contract_keeps_it() -> None:
    events = [
        *_settled_session()[:3],
        _event(
            "SessionClosed",
            {"sessionId": SESSION, "sequence": 3, "actor": Address(BUYER), "reason": 1},
            5,
        ),
    ]
    view = session_view(events)
    assert view is not None
    assert view["status"] == "closed"
    assert view["active_offer"] == {
        "offer_hash": OFFER_2,
        "proposer": "seller",
        "quote_amount_minor": "96000000",
        "valid_until_ts": 1758299400,
        "sequence": 2,
    }


def _row(status: TxStatus, tx: int, **fields: Any) -> OutboxRecord:
    values: dict[str, Any] = {
        "id": uuid.uuid4(),
        "run_id": RUN,
        "signed_action_id": None,
        "kind": TxKind.RECORD_OFFER,
        "sender": Address("0x" + "77" * 20),
        "nonce": tx,
        "raw_tx": b"",
        "tx_hash": Digest(bytes([tx]) * 32),
        "replaces_id": None,
        "status": status,
        "attempts": 1,
        "last_error": None,
        "block_number": None,
        "block_hash": None,
        "gas_used": None,
        "effective_gas_price_wei": None,
        "submitted_at": WHEN,
        "included_at": WHEN,
        "submitted_block": 1,
        "sentence": None,
    }
    values.update(fields)
    return OutboxRecord(**values)


def test_the_timeline_of_a_settlement() -> None:
    events = _settled_session()
    outbox = [_row(TxStatus.FINALIZED, 102), _row(TxStatus.CONFIRMED, 40)]
    timeline = build_timeline(
        events, outbox, [], TimelineContext(1, "https://sepolia.etherscan.io/")
    )
    assert [(entry["kind"], entry["actor"], entry["sequence"]) for entry in timeline] == [
        ("offer", "buyer", 1),
        ("offer", "seller", 2),
        ("accept", "buyer", 3),
        ("settle", "buyer", 3),
    ]
    assert timeline[2]["quote_amount_minor"] == "96000000"
    assert timeline[2]["references_offer_hash"] == OFFER_2
    assert timeline[0]["tx"]["status"] == "finalized"  # from the outbox row
    assert timeline[1]["tx"]["status"] == "confirmed"  # no row: from its depth
    assert (
        timeline[3]["tx"]["explorer_url"]
        == f"https://sepolia.etherscan.io/tx/{Digest(bytes([40]) * 32)}"
    )
    assert [entry["sentence"] for entry in timeline] == [
        "sentence 2.0",
        "sentence 3.0",
        "sentence 4.0",
        "sentence 4.3",
    ]
    assert timeline[0]["recorded_at"] == "2026-09-30T12:00:00Z"


def test_an_event_below_the_threshold_is_included_not_confirmed() -> None:
    events = _settled_session()[:2]
    timeline = build_timeline(events, [], [], TimelineContext(2, None))
    assert timeline[0]["tx"]["status"] == "included"
    assert timeline[0]["tx"]["explorer_url"] is None


def test_an_execution_failure_is_its_own_entry_after_its_block() -> None:
    events = _settled_session()[:2]
    action = SignedActionRecord(
        id=uuid.uuid4(),
        run_id=RUN,
        turn_id=uuid.uuid4(),
        decision_id=uuid.uuid4(),
        sequence=2,
        kind=ActionKind.ACCEPT,
        typed_message={},
        digest=Digest(b"\x09" * 32),
        signer=Address(BUYER),
        signature="0x",
        status=ActionStatus.REVERTED,
        revert_error="SelfAcceptance",
    )
    failed = _row(
        TxStatus.REVERTED,
        9,
        signed_action_id=action.id,
        block_number=2,
        block_hash=Digest(b"\x02" * 32),
        last_error="SelfAcceptance",
        sentence="Transaction reverted: SelfAcceptance. No trade occurred.",
    )
    expired = _row(
        TxStatus.REVERTED,
        10,
        kind=TxKind.EXPIRE_SESSION,
        block_number=3,
        last_error="SessionNotExpired",
        sentence="Transaction reverted: SessionNotExpired. No trade occurred.",
    )
    timeline = build_timeline(events, [failed, expired], [action], TimelineContext(1, None))
    assert [(entry["kind"], entry["actor"], entry["sequence"]) for entry in timeline] == [
        ("offer", "buyer", 1),
        ("execution_failure", "buyer", 2),
        ("execution_failure", "anyone", 1),
    ]
    assert timeline[1]["reason"] == "SelfAcceptance"


@pytest.mark.parametrize(
    ("terminal", "expected"),
    [
        (None, (OutcomeKind.SETTLED, PartyOrOperator.BUYER, None)),
        (
            (
                "SessionClosed",
                {"sessionId": SESSION, "sequence": 3, "actor": Address(SELLER), "reason": 2},
            ),
            (OutcomeKind.CLOSED, PartyOrOperator.SELLER, 2),
        ),
        (
            ("SessionExpired", {"sessionId": SESSION, "expiresAt": 1}),
            (OutcomeKind.EXPIRED, PartyOrOperator.ANYONE, None),
        ),
        (
            ("SessionAborted", {"sessionId": SESSION, "operator": BUYER, "reason": 4}),
            (OutcomeKind.ABORTED, PartyOrOperator.OPERATOR, 4),
        ),
    ],
)
def test_the_outcome_of_each_terminal_event(terminal: Any, expected: tuple[Any, ...]) -> None:
    events = _settled_session()
    if terminal is not None:
        events = [*events[:3], _event(terminal[0], terminal[1], 5)]
    outcome = derive_outcome(events, threshold=1)
    assert outcome is not None
    assert (outcome.kind, outcome.actor, outcome.reason_code) == expected


def test_no_outcome_below_the_threshold_or_without_a_terminal_event() -> None:
    assert derive_outcome(_settled_session(), threshold=2) is None
    assert derive_outcome(_settled_session()[:3], threshold=1) is None
    assert derive_outcome([], threshold=1) is None


def test_balances_are_the_latest_canonical_snapshot_per_party_and_token() -> None:
    def snapshot(
        stage: SnapshotStage, party: Party, token: TokenRole, amount: int, block: int
    ) -> BalanceSnapshotRecord:
        return BalanceSnapshotRecord(
            id=uuid.uuid4(),
            run_id=RUN,
            stage=stage,
            party=party,
            token=token,
            amount_minor=MinorAmount(amount),
            block_number=block,
            block_hash=Digest(bytes([block]) * 32),
            canonical=True,
        )

    view = latest_balances(
        [
            snapshot(SnapshotStage.PRE_SETTLEMENT, Party.BUYER, TokenRole.QUOTE, 250, 3),
            snapshot(SnapshotStage.POST_SETTLEMENT, Party.BUYER, TokenRole.QUOTE, 154, 4),
            snapshot(SnapshotStage.POST_SETTLEMENT, Party.BUYER, TokenRole.BASE, 10, 4),
            snapshot(SnapshotStage.POST_SETTLEMENT, Party.BUYER, TokenRole.ETH, 99, 4),
        ]
    )
    assert view == {"buyer": {"quote_minor": "154", "base_minor": "10"}}


def test_the_projection_never_reads_invalidated_rows() -> None:
    """Test strategy 7: every chain read goes through a canonical repository method."""
    package = Path(__file__).resolve().parents[2] / "src" / "api" / "projection"
    for source in sorted(package.glob("*.py")):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        names = {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
        assert "history_for_export" not in names, source.name


def test_a_seller_who_accepts_is_the_settlements_actor() -> None:
    """data_model 5: the outcome's actor is the accepting party, whichever it is."""
    events = [
        _event("SessionOpened", OPENED, 1),
        _event("OfferRecorded", _offer(1, BUYER, "95000000", OFFER_1), 2),
        _event(
            "AcceptanceRecorded",
            {"sessionId": SESSION, "sequence": 2, "actor": Address(SELLER), "offerHash": OFFER_1},
            3,
            0,
            tx=30,
        ),
        _event(
            "SettlementCompleted",
            {"baseAmount": "10000000", "quoteAmount": "95000000", "offerHash": OFFER_1},
            3,
            3,
            tx=30,
        ),
    ]
    outcome = derive_outcome(events, threshold=1)
    assert outcome is not None
    assert outcome.actor == PartyOrOperator.SELLER
    timeline = build_timeline(events, [], [], TimelineContext(1, None))
    assert [(entry["kind"], entry["actor"]) for entry in timeline] == [
        ("offer", "buyer"),
        ("accept", "seller"),
        ("settle", "seller"),
    ]


@pytest.mark.parametrize(
    ("name", "args", "status"),
    [
        ("SessionExpired", {"sessionId": SESSION, "expiresAt": 1}, "expired"),
        ("SessionAborted", {"sessionId": SESSION, "operator": BUYER, "reason": 1}, "aborted"),
    ],
)
def test_expiry_and_abort_end_the_session_and_keep_the_active_offer(
    name: str, args: dict[str, Any], status: str
) -> None:
    view = session_view([*_settled_session()[:3], _event(name, args, 5)])
    assert view is not None
    assert (view["status"], view["sequence"], view["offer_count"]) == (status, 2, 2)
    assert view["active_offer"] is not None  # only settlement clears it, as in the contract


def test_failed_approvals_are_their_signers_and_lifecycle_failures_carry_their_time() -> None:
    events = _settled_session()[:3]  # offers at sequence 1 (block 2) and 2 (block 3)
    approval = _row(
        TxStatus.REVERTED,
        11,
        kind=TxKind.APPROVE,
        sender=Address(SELLER),
        block_number=1,
        last_error="ERC20InvalidSpender",
        sentence="Transaction reverted: ERC20InvalidSpender. No trade occurred.",
    )
    abort = _row(
        TxStatus.REVERTED,
        12,
        kind=TxKind.ABORT_SESSION,
        block_number=2,
        last_error="SessionNotOpen",
        sentence="Transaction reverted: SessionNotOpen. No trade occurred.",
    )
    timeline = build_timeline(events, [approval, abort], [], TimelineContext(1, None))
    assert [(entry["kind"], entry["actor"], entry["sequence"]) for entry in timeline] == [
        ("execution_failure", "seller", 0),
        ("offer", "buyer", 1),
        ("execution_failure", "operator", 1),
        ("offer", "seller", 2),
    ]


def test_a_parties_view_refuses_an_address_that_is_neither() -> None:
    with pytest.raises(ValueError, match="neither party"):
        session_view(
            [
                _event("SessionOpened", OPENED, 1),
                _event("OfferRecorded", _offer(1, "0x" + "66" * 20, "1", OFFER_1), 2),
            ]
        )
