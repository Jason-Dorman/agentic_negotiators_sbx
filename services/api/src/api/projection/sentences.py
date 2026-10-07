"""The timeline's sentences (docs/api_contract.md section 4), rendered once and stored (ADR-024).

`TimelineSentences` is handed to the indexer, which renders each event's sentence as it records the
event and stores it on the `chain_events` row — and each execution failure's on its `tx_outbox` row
(ADR-051). The live view, replay and export then read the stored text, so the three cannot tell
different stories, and a later change to these rules does not rewrite a past run's evidence.

It lives here, in the projection, because a sentence is a presentation of the timeline. The indexer
cannot import the projection — `.importlinter` makes them independent siblings — so it declares the
interface it needs and is given this implementation by the composition root.

Every amount is rendered from minor units with six decimals and trailing zeros trimmed, through
`format_minor` and never through an f-string of a value object, which printed `MinorAmount(…)` in
stage 2.2 (docs/contributing.md section 3).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final

from negotiation_protocol import abort_reason_name, close_reason_name

#: docs/protocol.md section 2. Both tokens have six decimals.
BASE_SYMBOL: Final = "mASSET"
QUOTE_SYMBOL: Final = "mUSD"
TOKEN_DECIMALS: Final = 6

EarlierEvents = Sequence[tuple[str, Mapping[str, Any]]]


class SentenceContextError(ValueError):
    """An event cannot be described without its session's `SessionOpened`, which is missing."""


def format_minor(amount: int | str, decimals: int = TOKEN_DECIMALS) -> str:
    """`87654321` → `"87.654321"`, `92000000` → `"92"`. Exact: integers, never a float."""
    number = int(amount)
    if number < 0:
        raise ValueError("a token amount is never negative")
    whole, fraction = divmod(number, 10**decimals)
    digits = str(fraction).rjust(decimals, "0").rstrip("0")
    return f"{whole}.{digits}" if digits else str(whole)


def _words(name: str) -> str:
    return name.replace("_", " ")


class _Session:
    """What the sentences need from a session's earlier events: who is who, and what was offered."""

    def __init__(self, earlier: EarlierEvents) -> None:
        opened = next((args for name, args in earlier if name == "SessionOpened"), None)
        if opened is None:
            raise SentenceContextError("the session's SessionOpened event has not been recorded")
        self.buyer = str(opened["buyer"])
        self.seller = str(opened["seller"])
        self.base_amount = str(opened["baseAmount"])
        self.offers = {
            str(args["offerHash"]): str(args["quoteAmount"])
            for name, args in earlier
            if name == "OfferRecorded"
        }

    def role(self, address: object) -> str:
        if address == self.buyer:
            return "Buyer"
        if address == self.seller:
            return "Seller"
        raise SentenceContextError("an event names an address that is neither party")


class TimelineSentences:
    """The renderer the indexer is given. Stateless; one instance serves every run."""

    def event_sentence(
        self, name: str, args: Mapping[str, Any], earlier: EarlierEvents
    ) -> str | None:
        """The sentence for one exchange event, or None for `SessionOpened`, which has none.

        `earlier` is the session's canonical events before this one, in chain order.
        """
        if name == "SessionOpened":
            return None
        if name == "SessionExpired":
            moment = datetime.fromtimestamp(int(args["expiresAt"]), tz=UTC)
            return f"Session expired at {moment:%H:%M:%S} UTC."
        if name == "SessionAborted":
            reason = _words(abort_reason_name(int(args["reason"])))
            return f"Operator aborted the session ({reason})."
        return self._party_sentence(name, args, _Session(earlier))

    @staticmethod
    def _party_sentence(name: str, args: Mapping[str, Any], session: _Session) -> str:
        base = f"{format_minor(session.base_amount)} {BASE_SYMBOL}"
        if name == "OfferRecorded":
            quote = f"{format_minor(args['quoteAmount'])} {QUOTE_SYMBOL}"
            verb = "offers" if int(args["sequence"]) == 1 else "counters at"
            return f"{session.role(args['proposer'])} {verb} {quote} for {base}."
        if name == "AcceptanceRecorded":
            amount = session.offers.get(str(args["offerHash"]))
            if amount is None:
                raise SentenceContextError("the accepted offer has not been recorded")
            quote = f"{format_minor(amount)} {QUOTE_SYMBOL}"
            return f"{session.role(args['actor'])} accepts {quote} for {base}."
        if name == "SettlementCompleted":
            settled_base = f"{format_minor(args['baseAmount'])} {BASE_SYMBOL}"
            quote = f"{format_minor(args['quoteAmount'])} {QUOTE_SYMBOL}"
            return f"Settled: {settled_base} to buyer, {quote} to seller."
        if name == "SessionClosed":
            reason = _words(close_reason_name(int(args["reason"])))
            return f"{session.role(args['actor'])} walks away ({reason})."
        raise SentenceContextError(f"{name} is not an exchange event")

    def execution_failure_sentence(self, error_name: str) -> str:
        """A reverted transaction (ADR-051): its decoded error, and that nothing traded."""
        return f"Transaction reverted: {error_name}. No trade occurred."
