"""Settlement verification (docs/architecture.md section 5.3), from one receipt.

`AcceptanceRecorded`, `SettlementCompleted` and exactly two ERC-20 transfers — the quote amount the
accepted offer was **signed for**, buyer to seller, and the session's base amount, seller to buyer —
in the same receipt. The quote leg is held to the signed amount, not to the amount the event
announces, for the reason the reconstruction tool gives (docs/protocol.md section 14): comparing the
transfers with the event would only show the contract agreeing with itself. A settlement that moved
anything else, or announced a different amount from the one signed, is reported, problem by problem.
"""

from __future__ import annotations

from collections import Counter

from api.chain import ExchangeCodec, Receipt
from api.indexer.reports import SettlementCheck
from negotiation_protocol import Address


def check_settlement(
    receipt: Receipt,
    codec: ExchangeCodec,
    *,
    buyer: Address,
    seller: Address,
    base_amount: int,
    signed_quote_amount: int | None,
) -> SettlementCheck:
    problems: list[str] = []
    if not receipt.succeeded:
        problems.append("the settlement transaction reverted")
    events = [event for log in receipt.logs if (event := codec.decode_event(log)) is not None]
    names = Counter(event.name for event in events)
    for name in ("AcceptanceRecorded", "SettlementCompleted"):
        if names[name] != 1:
            problems.append(f"{names[name]} {name} events in the receipt, not 1")
    if signed_quote_amount is None:
        problems.append("the settled digest matches no recorded offer, so no amount was signed")
        signed_quote_amount = -1

    settled = next((event for event in events if event.name == "SettlementCompleted"), None)
    if settled is not None and int(settled.args["quoteAmount"]) != signed_quote_amount:
        problems.append("SettlementCompleted announces a quote amount the offer was not signed for")

    transfers = Counter(
        (transfer.token, transfer.sender, transfer.recipient, transfer.amount)
        for log in receipt.logs
        if (transfer := codec.decode_transfer(log)) is not None
    )
    expected = Counter(
        [
            (codec.quote_token, buyer, seller, signed_quote_amount),
            (codec.base_token, seller, buyer, base_amount),
        ]
    )
    if transfers != expected:
        problems.append(
            f"{sum(transfers.values())} token transfers that are not exactly the two signed legs"
        )
    return SettlementCheck(tx_hash=receipt.tx_hash, problems=tuple(problems))
