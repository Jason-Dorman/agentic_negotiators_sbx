"""Reason codes, as the two tables of docs/protocol.md section 10.

The codes are on-chain: a participant signs `Close.reason` and the operator passes
`abortSession(reason)`, and the contract reverts on anything undefined. The strings are the
agent-facing and operator-facing names for the same values. Both directions are needed, in both
languages, and getting them out of step would mean a timeline sentence that described a
different walk-away than the one the chain recorded — so the mapping lives here once and the
fixture test in `tests/` checks the TypeScript copy against it.

A close reason is the agent's *statement*, not a verified fact: `inventory_constraint` means the
agent said it was at its inventory floor, and the validator checked that claim separately
(docs/protocol.md section 10).
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Final, Literal

CloseReason = Literal["terms_unacceptable", "inventory_constraint", "no_further_concession"]
AbortReason = Literal["operator_request", "model_failure", "budget_exhausted", "execution_failure"]

#: Participant walk-away codes, signed in `Close.reason`. Also the `walk_away.reason` string a
#: policy returns (docs/protocol.md section 11).
CLOSE_REASONS: Final[MappingProxyType[int, CloseReason]] = MappingProxyType(
    {
        1: "terms_unacceptable",
        2: "inventory_constraint",
        3: "no_further_concession",
    }
)

#: Operator abort codes, passed to `abortSession`. Never signed by a participant.
ABORT_REASONS: Final[MappingProxyType[int, AbortReason]] = MappingProxyType(
    {
        1: "operator_request",
        2: "model_failure",
        3: "budget_exhausted",
        4: "execution_failure",
    }
)

#: The reverse direction, keyed by plain `str` rather than by the `Literal` alias. A policy hands
#: back whatever string the model produced, so the lookup *is* the validation: typing the key as
#: `CloseReason` would push every caller into a cast before it could ask the question.
CLOSE_REASON_CODES: Final[MappingProxyType[str, int]] = MappingProxyType(
    {name: code for code, name in CLOSE_REASONS.items()}
)

ABORT_REASON_CODES: Final[MappingProxyType[str, int]] = MappingProxyType(
    {name: code for code, name in ABORT_REASONS.items()}
)


class UnknownReasonCodeError(ValueError):
    """A code or name outside the protocol's tables.

    Raised rather than defaulted. An unrecognised code is a disagreement between this package
    and the deployed contract, and rendering it as "unknown" in a timeline would present a
    protocol mismatch as an ordinary outcome (docs/architecture.md goal 4).
    """


def close_reason_name(code: int) -> CloseReason:
    """Render a signed `Close.reason`. Raises `UnknownReasonCodeError` outside 1..3."""
    try:
        return CLOSE_REASONS[code]
    except KeyError as exc:
        raise UnknownReasonCodeError(
            f"close reason {code} is not one of {sorted(CLOSE_REASONS)}"
        ) from exc


def abort_reason_name(code: int) -> AbortReason:
    """Render an operator abort code. Raises `UnknownReasonCodeError` outside 1..4."""
    try:
        return ABORT_REASONS[code]
    except KeyError as exc:
        raise UnknownReasonCodeError(
            f"abort reason {code} is not one of {sorted(ABORT_REASONS)}"
        ) from exc


def close_reason_code(name: str) -> int:
    """Map a policy's `walk_away.reason` string to the code the signer will put on-chain."""
    try:
        return CLOSE_REASON_CODES[name]
    except KeyError as exc:
        raise UnknownReasonCodeError(
            f"close reason {name!r} is not one of {sorted(CLOSE_REASON_CODES)}"
        ) from exc


def abort_reason_code(name: str) -> int:
    """Map an operator abort reason string to its on-chain code."""
    try:
        return ABORT_REASON_CODES[name]
    except KeyError as exc:
        raise UnknownReasonCodeError(
            f"abort reason {name!r} is not one of {sorted(ABORT_REASON_CODES)}"
        ) from exc
