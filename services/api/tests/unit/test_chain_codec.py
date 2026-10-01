"""`ExchangeCodec`: decoding the protocol's events, calldata and errors, and encoding its calls.

Logs are built here from the committed ABI with `eth_abi`, independently of the codec's own
encoding, so a decoder that agreed only with itself would fail. The integration suite then decodes
what a real chain emitted.
"""

from __future__ import annotations

import secrets
from typing import Any

import pytest
from eth_abi.abi import encode as abi_encode
from eth_utils.crypto import keccak

from api.chain import EXCHANGE_EVENTS, CodecError, ExchangeCodec, RawLog
from negotiation_protocol import Address, Digest, load_abi

EXCHANGE = Address("0x" + "11" * 20)
BASE = Address("0x" + "22" * 20)
QUOTE = Address("0x" + "33" * 20)
BUYER = Address("0x" + "44" * 20)
SELLER = Address("0x" + "55" * 20)
SESSION = "0x" + "aa" * 32
OFFER_HASH = "0x" + "bb" * 32

#: docs/protocol.md section 8.3, all twenty-three.
PROTOCOL_ERRORS = {
    "NotOperator",
    "SessionExists",
    "InvalidParties",
    "InvalidTokenPair",
    "InvalidBaseAmount",
    "InvalidExpiry",
    "InvalidMaxOffers",
    "SessionNotOpen",
    "SessionDeadlinePassed",
    "SessionNotExpired",
    "ConfigHashMismatch",
    "SequenceMismatch",
    "BadSignature",
    "NotParticipant",
    "NotProposerTurn",
    "OfferLimitReached",
    "InvalidQuoteAmount",
    "InvalidValidUntil",
    "NoActiveOffer",
    "StaleOfferDigest",
    "OfferExpired",
    "SelfAcceptance",
    "InvalidReason",
}

#: One instance of every event, by its protocol field names, in the JSON form the codec promises.
EVENTS: dict[str, dict[str, Any]] = {
    "SessionOpened": {
        "sessionId": SESSION,
        "buyer": BUYER,
        "seller": SELLER,
        "baseToken": BASE,
        "quoteToken": QUOTE,
        "baseAmount": "10000000",
        "expiresAt": 1758300000,
        "maxOffers": 8,
        "configHash": "0x" + "cc" * 32,
    },
    "OfferRecorded": {
        "sessionId": SESSION,
        "sequence": 2,
        "proposer": SELLER,
        "quoteAmount": "97000000",
        "validUntil": 1758299400,
        "offerHash": OFFER_HASH,
    },
    "AcceptanceRecorded": {
        "sessionId": SESSION,
        "sequence": 3,
        "actor": BUYER,
        "offerHash": OFFER_HASH,
    },
    "SettlementCompleted": {
        "sessionId": SESSION,
        "buyer": BUYER,
        "seller": SELLER,
        "baseToken": BASE,
        "baseAmount": "10000000",
        "quoteToken": QUOTE,
        "quoteAmount": "97000000",
        "offerHash": OFFER_HASH,
    },
    "SessionClosed": {"sessionId": SESSION, "sequence": 4, "actor": SELLER, "reason": 3},
    "SessionExpired": {"sessionId": SESSION, "expiresAt": 1758300000},
    "SessionAborted": {"sessionId": SESSION, "operator": BUYER, "reason": 2},
}


def _entry(abi_name: str, kind: str, name: str) -> dict[str, Any]:
    return next(
        item for item in load_abi(abi_name) if item["type"] == kind and item["name"] == name
    )


def _native(kind: str, value: Any) -> Any:
    if kind == "bytes32":
        return bytes.fromhex(value[2:])
    if kind.startswith("uint"):
        return int(value)
    return value


def _log(
    name: str, args: dict[str, Any], address: Address = EXCHANGE, abi: str = "NegotiationExchange"
) -> RawLog:
    entry = _entry(abi, "event", name)
    signature = f"{name}({','.join(item['type'] for item in entry['inputs'])})"
    indexed = [item for item in entry["inputs"] if item["indexed"]]
    plain = [item for item in entry["inputs"] if not item["indexed"]]
    topics = [keccak(text=signature)] + [
        abi_encode([item["type"]], [_native(item["type"], args[item["name"]])]) for item in indexed
    ]
    data = abi_encode(
        [item["type"] for item in plain],
        [_native(item["type"], args[item["name"]]) for item in plain],
    )
    return RawLog(
        address=address,
        topics=tuple(topics),
        data=data,
        block_number=7,
        block_hash=Digest(secrets.token_bytes(32)),
        tx_hash=Digest(secrets.token_bytes(32)),
        log_index=0,
    )


@pytest.fixture
def codec() -> ExchangeCodec:
    return ExchangeCodec(EXCHANGE, BASE, QUOTE)


@pytest.mark.parametrize("name", EXCHANGE_EVENTS)
def test_every_event_decodes_to_its_protocol_fields(codec: ExchangeCodec, name: str) -> None:
    event = codec.decode_event(_log(name, EVENTS[name]))
    assert event is not None
    assert event.name == name
    assert event.args == EVENTS[name]
    # Amounts are strings, the narrower integers stay integers (api_contract section 1).
    for key, value in event.args.items():
        assert isinstance(value, str if key in {"baseAmount", "quoteAmount"} else type(value))


def test_the_seven_events_are_the_only_ones_decoded(codec: ExchangeCodec) -> None:
    declared = {item["name"] for item in load_abi("NegotiationExchange") if item["type"] == "event"}
    # OpenZeppelin's EIP712 adds EIP-5267's event to the ABI. It is not a protocol event, has no
    # session id, and must not reach `chain_events` (the export's event enum lists seven).
    assert declared == {*EXCHANGE_EVENTS, "EIP712DomainChanged"}
    assert codec.decode_event(_log("EIP712DomainChanged", {})) is None


def test_a_log_is_decoded_only_from_the_address_it_belongs_to(codec: ExchangeCodec) -> None:
    # The stage 1 trap: decoding by signature regardless of emitter. Same bytes, other address.
    elsewhere = _log("OfferRecorded", EVENTS["OfferRecorded"], address=BASE)
    assert codec.decode_event(elsewhere) is None

    transfer = {"from": BUYER, "to": SELLER, "value": "5"}
    assert codec.decode_transfer(_log("Transfer", transfer, QUOTE, "MockERC20")) is not None
    assert codec.decode_transfer(_log("Transfer", transfer, EXCHANGE, "MockERC20")) is None
    decoded = codec.decode_transfer(_log("Transfer", transfer, BASE, "MockERC20"))
    assert decoded is not None
    assert (decoded.token, decoded.sender, decoded.recipient, decoded.amount) == (
        BASE,
        BUYER,
        SELLER,
        5,
    )


def test_an_unknown_topic_or_a_malformed_log_is_not_an_event(codec: ExchangeCodec) -> None:
    log = _log("SessionExpired", EVENTS["SessionExpired"])
    assert codec.decode_event(RawLog(**{**_fields(log), "topics": (b"\x00" * 32,)})) is None
    assert codec.decode_event(RawLog(**{**_fields(log), "topics": ()})) is None
    assert codec.decode_event(RawLog(**{**_fields(log), "topics": log.topics[:1]})) is None


def _fields(log: RawLog) -> dict[str, Any]:
    return {name: getattr(log, name) for name in log.__slots__}


@pytest.mark.parametrize("name", sorted(PROTOCOL_ERRORS))
def test_every_protocol_error_decodes_by_name(codec: ExchangeCodec, name: str) -> None:
    entry = _entry("NegotiationExchange", "error", name)
    types = [item["type"] for item in entry["inputs"]]
    samples = {"uint8": 2, "uint16": 8, "uint64": 3, "address": BUYER, "bytes32": b"\x01" * 32}
    selector = keccak(text=f"{name}({','.join(types)})")[:4]
    revert = codec.decode_revert(selector + abi_encode(types, [samples[kind] for kind in types]))
    assert revert.name == name


def test_the_protocol_errors_are_all_in_the_abi() -> None:
    declared = {item["name"] for item in load_abi("NegotiationExchange") if item["type"] == "error"}
    assert declared >= PROTOCOL_ERRORS


def test_revert_arguments_take_their_json_form(codec: ExchangeCodec) -> None:
    selector = keccak(text="SequenceMismatch(uint64,uint64)")[:4]
    revert = codec.decode_revert(selector + abi_encode(["uint64", "uint64"], [3, 5]))
    assert (revert.name, revert.args) == ("SequenceMismatch", {"expected": 3, "actual": 5})


def test_reverts_that_are_not_protocol_errors(codec: ExchangeCodec) -> None:
    assert codec.decode_revert(b"").name == "NoRevertData"
    message = keccak(text="Error(string)")[:4] + abi_encode(["string"], ["no"])
    assert (codec.decode_revert(message).name, codec.decode_revert(message).args) == (
        "Error",
        {"message": "no"},
    )
    panic = keccak(text="Panic(uint256)")[:4] + abi_encode(["uint256"], [17])
    assert codec.decode_revert(panic).args == {"code": 17}
    assert codec.decode_revert(b"\xde\xad\xbe\xef").name == "UnknownError(0xdeadbeef)"
    allowance = keccak(text="ERC20InsufficientAllowance(address,uint256,uint256)")[:4]
    token_error = allowance + abi_encode(["address", "uint256", "uint256"], [EXCHANGE, 1, 2])
    assert codec.decode_revert(token_error).name == "ERC20InsufficientAllowance"


OFFER = {
    "sessionId": SESSION,
    "configHash": "0x" + "cc" * 32,
    "sequence": 1,
    "proposer": BUYER,
    "quoteAmount": "80000000",
    "validUntil": 1758299400,
}
SIGNATURE = "0x" + "ab" * 64 + "1b"


@pytest.mark.parametrize(
    ("kind", "function", "message"),
    [
        ("offer", "recordOffer", OFFER),
        (
            "accept",
            "acceptAndSettle",
            {
                "sessionId": SESSION,
                "configHash": "0x" + "cc" * 32,
                "sequence": 2,
                "actor": SELLER,
                "offerHash": OFFER_HASH,
            },
        ),
        (
            "close",
            "closeSession",
            {
                "sessionId": SESSION,
                "configHash": "0x" + "cc" * 32,
                "sequence": 2,
                "actor": SELLER,
                "reason": 1,
            },
        ),
    ],
)
def test_a_signed_action_encodes_by_field_name_and_decodes_back(
    codec: ExchangeCodec, kind: str, function: str, message: dict[str, Any]
) -> None:
    data = codec.encode_signed_action(kind, message, SIGNATURE)
    call = codec.decode_call(EXCHANGE, data)
    assert call is not None
    assert call.function == function
    (struct_name,) = [name for name in call.args if name != "signature"]
    assert call.args[struct_name] == message
    assert call.args["signature"] == SIGNATURE
    document = call.as_document(EXCHANGE, data)
    assert document["input"] == "0x" + data.hex()
    assert document["decoded_function"] == function


@pytest.mark.parametrize(
    "message",
    [
        {**OFFER, "valid_until": 1},  # a field the struct does not have
        {key: value for key, value in OFFER.items() if key != "validUntil"},
        {**OFFER, "quoteAmount": "80000000\n"},  # $ before a trailing newline (contributing 3)
        {**OFFER, "quoteAmount": "-1"},
        {**OFFER, "sequence": True},
        {**OFFER, "proposer": "0x1234"},
    ],
)
def test_a_malformed_message_is_refused_rather_than_encoded(
    codec: ExchangeCodec, message: dict[str, Any]
) -> None:
    with pytest.raises((CodecError, ValueError)):
        codec.encode_signed_action("offer", message, SIGNATURE)


def test_refusals_for_kind_and_signature(codec: ExchangeCodec) -> None:
    with pytest.raises(CodecError, match="no exchange function"):
        codec.encode_signed_action("walk_away", OFFER, SIGNATURE)
    with pytest.raises(CodecError, match="65 bytes"):
        codec.encode_signed_action("offer", OFFER, "0x" + "ab" * 64)


def test_calldata_for_anything_but_the_exchange_is_not_decoded(codec: ExchangeCodec) -> None:
    data = codec.encode_expire_session(SESSION)
    assert codec.decode_call(BASE, data) is None
    assert codec.decode_call(None, data) is None
    assert codec.decode_call(EXCHANGE, b"\x00\x01") is None
    assert codec.decode_call(EXCHANGE, b"\xde\xad\xbe\xef") is None
    call = codec.decode_call(EXCHANGE, data)
    assert call is not None
    assert (call.function, call.args) == ("expireSession", {"sessionId": SESSION})
    abort = codec.decode_call(EXCHANGE, codec.encode_abort_session(SESSION, 4))
    assert abort is not None
    assert abort.args == {"sessionId": SESSION, "reason": 4}


def test_token_calls_and_balance_results(codec: ExchangeCodec) -> None:
    assert codec.encode_balance_of(BUYER)[:4] == keccak(text="balanceOf(address)")[:4]
    assert codec.encode_mint(BUYER, 5)[:4] == keccak(text="mint(address,uint256)")[:4]
    assert codec.encode_approve(EXCHANGE, 5)[:4] == keccak(text="approve(address,uint256)")[:4]
    assert codec.decode_uint(abi_encode(["uint256"], [2**200])) == 2**200


def test_a_token_event_that_is_not_a_transfer_is_not_a_transfer(codec: ExchangeCodec) -> None:
    approval = {"owner": BUYER, "spender": EXCHANGE, "value": "5"}
    assert codec.decode_transfer(_log("Approval", approval, QUOTE, "MockERC20")) is None
    log = _log("Transfer", {"from": BUYER, "to": SELLER, "value": "5"}, QUOTE, "MockERC20")
    assert codec.decode_transfer(RawLog(**{**_fields(log), "topics": (b"\x01" * 32,)})) is None


def test_a_struct_that_is_not_an_object_is_refused(codec: ExchangeCodec) -> None:
    from api.chain.codec import _from_json

    struct = {"name": "offer", "type": "tuple", "components": [{"name": "x", "type": "uint8"}]}
    with pytest.raises(CodecError, match="must be an object"):
        _from_json(struct, ["not", "a", "mapping"])
    with pytest.raises(CodecError, match="no encoding"):
        _from_json({"name": "flag", "type": "bool"}, True)
    assert _from_json({"name": "data", "type": "bytes"}, "0x0102") == b"\x01\x02"


@pytest.mark.parametrize(
    ("field", "value"),
    [("sequence", 2**64), ("validUntil", 2**64), ("quoteAmount", str(2**256))],
)
def test_a_value_too_wide_for_its_field_is_a_codec_error(
    codec: ExchangeCodec, field: str, value: Any
) -> None:
    with pytest.raises((CodecError, ValueError)) as refused:
        codec.encode_signed_action("offer", {**OFFER, field: value}, SIGNATURE)
    assert refused.type is CodecError or "uint256" in str(refused.value)
    close = {
        "sessionId": SESSION,
        "configHash": "0x" + "cc" * 32,
        "sequence": 2,
        "actor": SELLER,
        "reason": 256,
    }
    with pytest.raises(CodecError, match="does not fit uint8"):
        codec.encode_signed_action("close", close, SIGNATURE)
