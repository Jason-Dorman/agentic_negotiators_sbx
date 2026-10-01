"""The signer: session approval, typed messages from validated state, and the setup approval.

Three claims, each of which a stage-2 mistake would break silently:

- The session this instance signs for is one it checked itself: `configHash` recomputed from its
  own provisioned expectation (docs/protocol.md section 3), the parties, the amounts and the expiry
  window (ADR-044), with every differing field reported.
- Every typed message is built from the approved session, the validated decision, the supplied
  sequence and chain time, and nothing else — and it reproduces the EIP-712 fixture's digest and
  signature byte for byte, so the three other implementations checked against that fixture vouch
  for this one too.
- The setup `approve` is built from provisioned state; the caller's nonce and fee caps are the only
  inputs it supplies (ADR-040), and the gas bound is enforced (ADR-042).
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any
from uuid import UUID

import pytest
from agent_fakes import FIXTURE_KEYS, KeyedRunSigner
from agent_observations import FIXTURE, session, typed
from agent_observations import observation as observation_document
from eth_abi.abi import decode as abi_decode
from eth_account import Account
from eth_account.typed_transactions.typed_transaction import TypedTransaction
from eth_utils.crypto import keccak
from hexbytes import HexBytes

from agent.errors import InvalidStateError, RequestValidationError, SessionMismatchError
from agent.keys import KeyDerivation
from agent.observation import Role, SessionView
from agent.signing import (
    APPROVE_SELECTOR,
    ApprovedSession,
    ExpectedSession,
    FeeTerms,
    SessionOpened,
    SetupBounds,
    approve_session,
    build_setup_approval,
    sign_decision,
)
from agent.validation import ProposedOffer, ValidAccept, ValidOffer, ValidWalkAway
from negotiation_protocol import (
    Address,
    Close,
    Digest,
    MinorAmount,
    Sequence,
    SessionId,
    recover_signer,
)

RUN = UUID("6f1c2a4e-9d7b-4c3a-8e2f-1a2b3c4d5e6f")
CONFIG = FIXTURE["session_config"]
MESSAGES = FIXTURE["messages"]
CHAIN_TIME = int(FIXTURE["chain_time"])
OPENED_AT = int(CONFIG["expires_at"]) - 1800

EXPECTED = ExpectedSession(
    chain_id=FIXTURE["domain"]["chain_id"],
    exchange_address=Address(FIXTURE["domain"]["verifying_contract"]),
    base_token=Address(FIXTURE["deployment"]["base_token"]),
    quote_token=Address(FIXTURE["deployment"]["quote_token"]),
    base_amount=MinorAmount(int(CONFIG["base_amount"])),
    max_offers=int(CONFIG["max_offers"]),
    session_duration_s=1800,
    offer_lifetime_s=600,
)

OPENED = SessionOpened(
    session_id=SessionId(CONFIG["session_id"]),
    buyer=Address(CONFIG["buyer"]),
    seller=Address(CONFIG["seller"]),
    base_token=EXPECTED.base_token,
    quote_token=EXPECTED.quote_token,
    base_amount=EXPECTED.base_amount,
    expires_at=int(CONFIG["expires_at"]),
    max_offers=EXPECTED.max_offers,
    config_hash=Digest(FIXTURE["config_hash"]),
    opened_at=OPENED_AT,
)


def signer(role: Role) -> KeyedRunSigner:
    return KeyedRunSigner(FIXTURE_KEYS[role], KeyDerivation(chain_id=31337, role=role, run_id=RUN))


def approved(role: Role = "buyer") -> ApprovedSession:
    return approve_session(EXPECTED, OPENED, role, signer(role).address)


def mismatched_fields(opened: SessionOpened, role: Role = "buyer") -> dict[str, Any]:
    with pytest.raises(SessionMismatchError) as refused:
        approve_session(EXPECTED, opened, role, signer(role).address)
    fields: dict[str, Any] = refused.value.details["fields"]
    return fields


# --------------------------------------------------------------------------------------
# Session approval
# --------------------------------------------------------------------------------------


def test_the_fixture_session_is_approved_with_its_recomputed_config_hash() -> None:
    session_approval = approved()
    assert session_approval.config_hash == FIXTURE["config_hash"]
    assert session_approval.session_id == CONFIG["session_id"]
    assert session_approval.expires_at == CONFIG["expires_at"]
    assert session_approval.offer_lifetime_s == 600


def test_the_seller_approves_the_same_session_from_its_side() -> None:
    assert approved("seller").config_hash == FIXTURE["config_hash"]


@pytest.mark.parametrize(
    ("change", "field"),
    [
        ({"base_token": Address("0x" + "11" * 20)}, "base_token"),
        ({"quote_token": Address("0x" + "22" * 20)}, "quote_token"),
        ({"base_amount": MinorAmount(20_000_000)}, "base_amount_minor"),
        ({"max_offers": 9}, "max_offers"),
        ({"buyer": Address("0x" + "33" * 20)}, "buyer"),
        ({"config_hash": Digest("0x" + "44" * 32)}, "config_hash"),
    ],
)
def test_each_differing_field_is_named(change: dict[str, Any], field: str) -> None:
    fields = mismatched_fields(replace(OPENED, **change))
    assert field in fields
    assert set(fields[field]) == {"expected", "received"}


def test_a_changed_party_also_fails_the_recomputed_config_hash() -> None:
    fields = mismatched_fields(replace(OPENED, buyer=Address("0x" + "33" * 20)))
    assert set(fields) == {"buyer", "config_hash"}


def test_the_config_hash_is_recomputed_from_the_provisioned_tokens_not_the_events() -> None:
    """A session opened on other tokens fails the token check and the hash check together."""
    fields = mismatched_fields(replace(OPENED, base_token=Address("0x" + "11" * 20)))
    assert set(fields) == {"base_token"}, "the event's own hash still binds the provisioned token"
    other = replace(
        OPENED,
        base_token=Address("0x" + "11" * 20),
        config_hash=Digest("0x" + "55" * 32),
    )
    assert set(mismatched_fields(other)) == {"base_token", "config_hash"}


def test_the_seller_refuses_a_session_that_names_someone_else_as_seller() -> None:
    fields = mismatched_fields(replace(OPENED, seller=Address("0x" + "66" * 20)), role="seller")
    assert "seller" in fields


def test_a_session_between_a_party_and_itself_is_refused() -> None:
    fields = mismatched_fields(replace(OPENED, seller=OPENED.buyer))
    assert "seller" in fields


def test_a_zero_counterparty_is_refused() -> None:
    fields = mismatched_fields(replace(OPENED, seller=Address("0x" + "00" * 20)))
    assert "seller" in fields


@pytest.mark.parametrize(
    ("expires_at", "accepted"),
    [
        (OPENED_AT + 1800, True),  # exactly the provisioned duration
        (OPENED_AT + 1788, True),  # the block landed 12 s after the operator read the head
        (OPENED_AT + 1, True),
        (OPENED_AT + 1801, False),  # longer than provisioned
        (OPENED_AT, False),  # already expired when opened
    ],
)
def test_the_expiry_window(expires_at: int, accepted: bool) -> None:
    """ADR-044: opened_at < expires_at <= opened_at + session_duration_s."""
    opened = replace(OPENED, expires_at=expires_at)
    if accepted:
        # The fixture's hash binds its own expiry; recompute it for this one.
        opened = replace(opened, config_hash=_recomputed_hash(opened))
        assert approve_session(EXPECTED, opened, "buyer", signer("buyer").address)
    else:
        fields = mismatched_fields(replace(opened, config_hash=_recomputed_hash(opened)))
        assert set(fields) == {"expires_at_ts"}


def _recomputed_hash(opened: SessionOpened) -> Digest:
    from negotiation_protocol import SessionConfig

    config = SessionConfig(
        session_id=opened.session_id.to_bytes(),
        buyer=opened.buyer,
        seller=opened.seller,
        base_amount=opened.base_amount,
        expires_at=opened.expires_at,
        max_offers=opened.max_offers,
    )
    return Digest(config.config_hash(EXPECTED.base_token, EXPECTED.quote_token))


def test_an_observation_of_the_approved_session_matches_it() -> None:
    view = typed(observation_document("buyer")).session
    assert approved().mismatches_with(view) == {}


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("session_id", "0x" + "77" * 32),
        ("config_hash", "0x" + "88" * 32),
        ("chain_id", 11155111),
        ("exchange_address", "0x" + "99" * 20),
        ("base_token", "0x" + "aa" * 20),
        ("quote_token", "0x" + "bb" * 20),
        ("base_amount_minor", "20000000"),
        ("expires_at", 1),
        ("max_offers", 9),
    ],
)
def test_an_observation_of_any_other_session_is_named_field_by_field(key: str, value: Any) -> None:
    view = SessionView.from_json({**session(), key: value})
    assert set(approved().mismatches_with(view)) == {key}


# --------------------------------------------------------------------------------------
# Typed messages, from validated state
# --------------------------------------------------------------------------------------


def test_an_offer_reproduces_the_fixture_digest_and_signature() -> None:
    buyer = signer("buyer")
    action = sign_decision(
        ValidOffer(MinorAmount(92_000_000)),
        approval=approved("buyer"),
        sequence=Sequence(1),
        chain_time=CHAIN_TIME,
        signer=buyer,
    )
    fixture = MESSAGES["offer"]
    assert action.kind == "offer"
    assert action.digest == fixture["digest"]
    assert action.signature == fixture["signature"]
    assert action.signer == buyer.address
    assert action.typed_message == {
        "sessionId": fixture["message"]["session_id"],
        "configHash": fixture["message"]["config_hash"],
        "sequence": 1,
        "proposer": fixture["message"]["proposer"],
        "quoteAmount": "92000000",
        "validUntil": fixture["message"]["valid_until"],
    }


def test_an_accept_reproduces_the_fixture_digest_and_signature() -> None:
    action = sign_decision(
        ValidAccept(Digest(MESSAGES["offer"]["digest"])),
        approval=approved("seller"),
        sequence=Sequence(2),
        chain_time=CHAIN_TIME,
        signer=signer("seller"),
    )
    fixture = MESSAGES["accept"]
    assert (action.kind, action.digest, action.signature) == (
        "accept",
        fixture["digest"],
        fixture["signature"],
    )
    assert action.typed_message == {
        "sessionId": fixture["message"]["session_id"],
        "configHash": fixture["message"]["config_hash"],
        "sequence": 2,
        "actor": fixture["message"]["actor"],
        "offerHash": fixture["message"]["offer_hash"],
    }


def test_a_close_reproduces_the_fixture_digest_and_signature() -> None:
    action = sign_decision(
        ValidWalkAway("terms_unacceptable", 1),
        approval=approved("seller"),
        sequence=Sequence(2),
        chain_time=CHAIN_TIME,
        signer=signer("seller"),
    )
    fixture = MESSAGES["close"]
    assert (action.kind, action.digest, action.signature) == (
        "close",
        fixture["digest"],
        fixture["signature"],
    )
    assert action.typed_message["reason"] == 1


def test_every_signature_recovers_to_the_signer() -> None:
    buyer = signer("buyer")
    action = sign_decision(
        ValidOffer(MinorAmount(80_000_000)),
        approval=approved(),
        sequence=Sequence(1),
        chain_time=CHAIN_TIME,
        signer=buyer,
    )
    assert recover_signer(Digest(action.digest).to_bytes(), action.signature) == buyer.address


def test_validuntil_is_capped_at_the_session_expiry() -> None:
    """docs/protocol.md section 6: validUntil = min(chain time + offer lifetime, expiresAt)."""
    late = int(CONFIG["expires_at"]) - 100
    action = sign_decision(
        ValidOffer(MinorAmount(80_000_000)),
        approval=approved(),
        sequence=Sequence(1),
        chain_time=late,
        signer=signer("buyer"),
    )
    assert action.typed_message["validUntil"] == CONFIG["expires_at"]


def test_the_signed_action_serialises_for_the_turn_response() -> None:
    action = sign_decision(
        ValidWalkAway("no_further_concession", 3),
        approval=approved(),
        sequence=Sequence(4),
        chain_time=CHAIN_TIME,
        signer=signer("buyer"),
    )
    assert action.to_json() == {
        "kind": "close",
        "typed_message": action.typed_message,
        "digest": action.digest,
        "signature": action.signature,
        "signer": action.signer,
    }


def test_nothing_is_signed_without_an_approved_session() -> None:
    buyer = signer("buyer")
    with pytest.raises(InvalidStateError) as refused:
        sign_decision(
            ValidOffer(MinorAmount(80_000_000)),
            approval=None,
            sequence=Sequence(1),
            chain_time=CHAIN_TIME,
            signer=buyer,
        )
    assert refused.value.details == {"state": "provisioned", "allowed_from": ["approved"]}
    assert buyer.signatures_made == 0


def test_nothing_is_signed_at_or_after_the_session_deadline() -> None:
    buyer = signer("buyer")
    with pytest.raises(InvalidStateError, match="deadline"):
        sign_decision(
            ValidWalkAway("terms_unacceptable", 1),
            approval=approved(),
            sequence=Sequence(1),
            chain_time=int(CONFIG["expires_at"]),
            signer=buyer,
        )
    assert buyer.signatures_made == 0


def test_nothing_is_signed_by_a_key_that_is_not_the_sessions_party() -> None:
    seller_key_as_buyer = KeyedRunSigner(
        FIXTURE_KEYS["seller"], KeyDerivation(chain_id=31337, role="buyer", run_id=RUN)
    )
    with pytest.raises(SessionMismatchError, match="not the session's buyer"):
        sign_decision(
            ValidOffer(MinorAmount(80_000_000)),
            approval=approved(),
            sequence=Sequence(1),
            chain_time=CHAIN_TIME,
            signer=seller_key_as_buyer,
        )
    assert seller_key_as_buyer.signatures_made == 0


def test_an_unvalidated_proposal_cannot_be_signed() -> None:
    """The type system refuses this; the runtime refuses it too, for anything that got past it."""
    buyer = signer("buyer")
    with pytest.raises(TypeError, match="only a validated decision"):
        sign_decision(
            ProposedOffer(MinorAmount(80_000_000)),  # type: ignore[arg-type]  # reason: the point of the test
            approval=approved(),
            sequence=Sequence(1),
            chain_time=CHAIN_TIME,
            signer=buyer,
        )
    assert buyer.signatures_made == 0


# --------------------------------------------------------------------------------------
# The setup approval (ADR-040, ADR-042)
# --------------------------------------------------------------------------------------

TERMS = FeeTerms(
    nonce=0,
    gas_limit=70_000,
    max_fee_per_gas=2_000_000_000,
    max_priority_fee_per_gas=1_000_000_000,
)


BOUNDS = SetupBounds(gas_limit_max=100_000, max_cost_wei=10**16)


def setup(
    role: Role,
    terms: FeeTerms = TERMS,
    bounds: SetupBounds = BOUNDS,
    expected: ExpectedSession = EXPECTED,
) -> Any:
    return build_setup_approval(
        role=role,
        expected=expected,
        allowance=MinorAmount(250_000_000 if role == "buyer" else 25_000_000),
        terms=terms,
        bounds=bounds,
        signer=signer(role),
    )


def decoded(raw_tx: str) -> dict[str, Any]:
    return dict(TypedTransaction.from_bytes(HexBytes(raw_tx)).as_dict())


@pytest.mark.parametrize(
    ("role", "token", "amount"),
    [("buyer", EXPECTED.quote_token, 250_000_000), ("seller", EXPECTED.base_token, 25_000_000)],
)
def test_the_approval_is_the_roles_token_to_the_provisioned_exchange(
    role: Role, token: str, amount: int
) -> None:
    approval = setup(role)
    transaction = decoded(approval.raw_tx)
    assert Address("0x" + bytes(transaction["to"]).hex()) == token
    data = bytes(transaction["data"])
    assert data[:4] == APPROVE_SELECTOR == bytes.fromhex("095ea7b3")
    spender, value = abi_decode(["address", "uint256"], data[4:])
    assert Address(spender) == EXPECTED.exchange_address
    assert value == amount
    assert transaction["value"] == 0
    assert transaction["chainId"] == EXPECTED.chain_id
    assert transaction["type"] == 2
    assert approval.to_json() == {
        "raw_tx": approval.raw_tx,
        "tx_hash": approval.tx_hash,
        "from": signer(role).address,
        "token": token,
        "spender": EXPECTED.exchange_address,
        "amount_minor": str(amount),
        "nonce": 0,
    }


def test_the_callers_nonce_and_fee_caps_are_the_only_inputs_it_takes() -> None:
    terms = FeeTerms(nonce=3, gas_limit=65_000, max_fee_per_gas=7, max_priority_fee_per_gas=5)
    transaction = decoded(setup("buyer", terms).raw_tx)
    assert (transaction["nonce"], transaction["gas"]) == (3, 65_000)
    assert (transaction["maxFeePerGas"], transaction["maxPriorityFeePerGas"]) == (7, 5)


def test_the_approval_is_signed_by_the_run_key() -> None:
    approval = setup("buyer")
    raw = bytes.fromhex(approval.raw_tx[2:])
    assert Account.recover_transaction(raw) == signer("buyer").address
    # The transaction hash is keccak of the signed envelope, not TypedTransaction.hash(), which is
    # the pre-signature signing hash.
    assert approval.tx_hash == "0x" + keccak(raw).hex()


def test_the_same_request_returns_the_same_transaction() -> None:
    assert setup("seller") == setup("seller")


def test_a_gas_limit_at_the_bound_is_accepted() -> None:
    assert setup("buyer", replace(TERMS, gas_limit=100_000)).raw_tx


def test_a_gas_limit_above_the_bound_is_refused_and_nothing_is_signed() -> None:
    buyer = signer("buyer")
    with pytest.raises(RequestValidationError) as refused:
        build_setup_approval(
            role="buyer",
            expected=EXPECTED,
            allowance=MinorAmount(250_000_000),
            terms=replace(TERMS, gas_limit=100_001),
            bounds=BOUNDS,
            signer=buyer,
        )
    assert set(refused.value.details["fields"]) == {"gas_limit"}
    assert buyer.signatures_made == 0


def test_a_priority_fee_above_the_fee_cap_is_refused() -> None:
    with pytest.raises(RequestValidationError) as refused:
        setup("buyer", replace(TERMS, max_priority_fee_per_gas=TERMS.max_fee_per_gas + 1))
    assert set(refused.value.details["fields"]) == {"max_priority_fee_per_gas_wei"}


def test_a_fee_cap_that_could_cost_more_than_the_bound_is_refused_and_nothing_is_signed() -> None:
    """ADR-047: gas limit x fee cap, the most the transaction can burn, at most 0.01 ETH."""
    buyer = signer("buyer")
    at_bound = replace(TERMS, gas_limit=100_000, max_fee_per_gas=10**11, max_priority_fee_per_gas=1)
    assert setup("buyer", at_bound).raw_tx
    over = replace(at_bound, max_fee_per_gas=10**11 + 1)
    with pytest.raises(RequestValidationError) as refused:
        build_setup_approval(
            role="buyer",
            expected=EXPECTED,
            allowance=MinorAmount(250_000_000),
            terms=over,
            bounds=BOUNDS,
            signer=buyer,
        )
    assert set(refused.value.details["fields"]) == {"max_fee_per_gas_wei"}
    assert buyer.signatures_made == 0


def test_a_priority_fee_equal_to_the_fee_cap_is_accepted() -> None:
    assert setup("buyer", replace(TERMS, max_priority_fee_per_gas=TERMS.max_fee_per_gas)).raw_tx


def test_the_approval_is_signed_for_the_provisioned_chain() -> None:
    """Not 31337 by habit: an approval is valid only on the chain it was provisioned for."""
    sepolia = replace(EXPECTED, chain_id=11155111)
    assert decoded(setup("seller", expected=sepolia).raw_tx)["chainId"] == 11155111
    assert decoded(setup("seller").raw_tx)["chainId"] == 31337


def test_the_callers_nonce_is_echoed() -> None:
    assert setup("buyer", replace(TERMS, nonce=3)).to_json()["nonce"] == 3


@pytest.mark.parametrize(
    ("reason", "code"), [("inventory_constraint", 2), ("no_further_concession", 3)]
)
def test_every_close_reason_is_signed_as_the_code_the_decision_carries(
    reason: str, code: int
) -> None:
    """The fixture holds only reason 1, so the other two are recomputed here from the struct."""
    seller = signer("seller")
    session_approval = approved("seller")
    action = sign_decision(
        ValidWalkAway(reason, code),  # type: ignore[arg-type]  # reason: parametrised Literal
        approval=session_approval,
        sequence=Sequence(2),
        chain_time=CHAIN_TIME,
        signer=seller,
    )
    expected = Close(
        session_approval.session_id.to_bytes(),
        session_approval.config_hash.to_bytes(),
        2,
        seller.address,
        code,
    ).digest(session_approval.domain())
    assert action.typed_message["reason"] == code
    assert action.digest == Digest(expected)
    assert recover_signer(expected, action.signature) == seller.address
