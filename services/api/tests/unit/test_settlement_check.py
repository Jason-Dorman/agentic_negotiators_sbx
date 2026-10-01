"""Settlement verification (docs/architecture.md section 5.3) over constructed receipts.

The quote leg is held to the amount the accepted offer was signed for, never to the amount the
event announces (docs/protocol.md section 14), and a transfer is counted only if it came from one of
the deployment's two tokens — the stage 1 four-transfers trap.
"""

from __future__ import annotations

import secrets

from eth_abi.abi import encode as abi_encode
from eth_utils.crypto import keccak

from api.chain import ExchangeCodec, RawLog, Receipt
from api.indexer import check_settlement
from negotiation_protocol import Address, Digest

EXCHANGE = Address("0x" + "11" * 20)
BASE = Address("0x" + "22" * 20)
QUOTE = Address("0x" + "33" * 20)
BUYER = Address("0x" + "44" * 20)
SELLER = Address("0x" + "55" * 20)
SESSION = b"\xaa" * 32
OFFER = b"\xbb" * 32
TX = Digest(secrets.token_bytes(32))

CODEC = ExchangeCodec(EXCHANGE, BASE, QUOTE)


def _log(address: Address, topics: list[bytes], data: bytes, index: int) -> RawLog:
    return RawLog(address, tuple(topics), data, 9, Digest(b"\x09" * 32), TX, index)


def _topic(value: bytes | str) -> bytes:
    if isinstance(value, str):
        return abi_encode(["address"], [value])
    return value


def _transfer(
    token: Address, sender: Address, recipient: Address, amount: int, index: int
) -> RawLog:
    topics = [keccak(text="Transfer(address,address,uint256)"), _topic(sender), _topic(recipient)]
    return _log(token, topics, abi_encode(["uint256"], [amount]), index)


def _acceptance(index: int) -> RawLog:
    topics = [
        keccak(text="AcceptanceRecorded(bytes32,uint64,address,bytes32)"),
        SESSION,
        _topic(BUYER),
    ]
    return _log(EXCHANGE, topics, abi_encode(["uint64", "bytes32"], [3, OFFER]), index)


def _settlement(quote_amount: int, index: int) -> RawLog:
    signature = (
        "SettlementCompleted(bytes32,address,address,address,uint256,address,uint256,bytes32)"
    )
    data = abi_encode(
        ["address", "address", "address", "uint256", "address", "uint256", "bytes32"],
        [BUYER, SELLER, BASE, 10_000_000, QUOTE, quote_amount, OFFER],
    )
    return _log(EXCHANGE, [keccak(text=signature), SESSION], data, index)


def _receipt(*logs: RawLog, succeeded: bool = True) -> Receipt:
    return Receipt(TX, succeeded, 9, Digest(b"\x09" * 32), 120_000, 1, BUYER, EXCHANGE, logs)


def _check(receipt: Receipt, signed: int | None = 96_000_000) -> tuple[str, ...]:
    result = check_settlement(
        receipt,
        CODEC,
        buyer=BUYER,
        seller=SELLER,
        base_amount=10_000_000,
        signed_quote_amount=signed,
    )
    assert result.ok == (not result.problems)
    return result.problems


def _good_logs(quote: int = 96_000_000) -> list[RawLog]:
    return [
        _transfer(QUOTE, BUYER, SELLER, quote, 0),
        _transfer(BASE, SELLER, BUYER, 10_000_000, 1),
        _acceptance(2),
        _settlement(quote, 3),
    ]


def test_the_two_signed_legs_with_acceptance_and_settlement_verify() -> None:
    assert _check(_receipt(*_good_logs())) == ()


def test_a_transfer_from_an_address_that_is_not_a_token_is_not_counted() -> None:
    impostor = _transfer(Address("0x" + "99" * 20), BUYER, SELLER, 1, 4)
    assert _check(_receipt(*_good_logs(), impostor)) == ()


def test_an_extra_transfer_is_a_problem() -> None:
    extra = _transfer(QUOTE, BUYER, SELLER, 1, 4)
    assert _check(_receipt(*_good_logs(), extra)) == (
        "3 token transfers that are not exactly the two signed legs",
    )


def test_a_settlement_at_an_amount_nobody_signed_is_two_problems() -> None:
    problems = _check(_receipt(*_good_logs(quote=97_000_000)))
    assert problems == (
        "SettlementCompleted announces a quote amount the offer was not signed for",
        "2 token transfers that are not exactly the two signed legs",
    )


def test_missing_events_an_unknown_offer_and_a_revert_are_each_reported() -> None:
    logs = _good_logs()[:2]
    problems = _check(_receipt(*logs, succeeded=False), signed=None)
    assert problems[0] == "the settlement transaction reverted"
    assert "0 AcceptanceRecorded events in the receipt, not 1" in problems
    assert "0 SettlementCompleted events in the receipt, not 1" in problems
    assert "the settled digest matches no recorded offer, so no amount was signed" in problems
