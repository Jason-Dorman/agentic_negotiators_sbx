"""Structural validation of a policy's response: `agent_decision.v1.json`, one rule at a time.

The schema is the normative statement of the shape, and `test_validator.py` checks that this module
agrees with it on every valid response and every refusal in its table. It is written out by hand
rather than delegated to `jsonschema` for one reason: a refusal has to say *which* rule failed, in a
sentence a model can act on, and a `oneOf` failure from a schema validator does not.

In two respects it is deliberately stricter than the schema as Python evaluates it. An amount or a
digest must match its pattern over the whole string, where `jsonschema`'s `re.search` lets `$`
accept a trailing newline; and an amount must fit `uint256`, which the schema's 78-digit bound
admits values above. Both are cases where the schema's intent is clear and its enforcement is loose.

The checks run in a fixed order, so a response with several faults is refused for the first:
the envelope, then `decision`, then its `action`, then unexpected fields, then missing ones, then
the value.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any, Final

from agent.validation.codes import Code
from agent.validation.decisions import Proposal, ProposedAccept, ProposedOffer, ProposedWalkAway
from negotiation_protocol import (
    CLOSE_REASON_CODES,
    Digest,
    MinorAmount,
    ProtocolValueError,
    close_reason_name,
)

EXPLANATION_MAX_CHARS: Final = 280
_ENVELOPE_FIELDS: Final = frozenset({"decision", "explanation"})

_ENVELOPE_FEEDBACK: Final = (
    'Your response must be a JSON object: {"decision": {...}} with an optional "explanation".'
)
_ACTION_FEEDBACK: Final = '"decision.action" must be "offer", "accept" or "walk_away".'
_AMOUNT_FEEDBACK: Final = (
    '"quote_amount_minor" must be a whole number of minor units written as a base-10 string, '
    'with no sign, decimal point, exponent or leading zeros, for example "94000000".'
)
_HASH_FEEDBACK: Final = '"offer_hash" must be 0x followed by 64 hexadecimal characters.'
_REASON_FEEDBACK: Final = (
    '"reason" must be "terms_unacceptable", "inventory_constraint" or "no_further_concession".'
)


@dataclass(frozen=True, slots=True)
class Refusal:
    code: Code
    feedback: str


@dataclass(frozen=True, slots=True)
class ParseResult:
    """Exactly one of a proposal and a refusal, as one field so a type checker can narrow it."""

    outcome: Proposal | Refusal

    @property
    def ok(self) -> bool:
        return not isinstance(self.outcome, Refusal)


def _refuse(code: Code, feedback: str) -> ParseResult:
    return ParseResult(Refusal(code, feedback))


def _quoted(names: Iterable[object]) -> str:
    return ", ".join(f'"{name}"' for name in sorted(str(name) for name in names))


def parse_decision(response: object) -> ParseResult:
    """Parse a decision envelope into a `Proposal`, or say which structural rule it breaks."""
    if not isinstance(response, dict):
        return _refuse(Code.SCHEMA_ERROR, _ENVELOPE_FEEDBACK)
    envelope_fault = _check_envelope(response)
    if envelope_fault is not None:
        return envelope_fault
    decision = response["decision"]
    if not isinstance(decision, dict):
        return _refuse(Code.SCHEMA_ERROR, '"decision" must be a JSON object.')
    action = decision.get("action")
    shape = _SHAPES.get(action) if isinstance(action, str) else None
    if shape is None:
        return _refuse(Code.SCHEMA_ERROR, _ACTION_FEEDBACK)
    return shape.parse(decision)


def _check_envelope(response: dict[Any, Any]) -> ParseResult | None:
    extra = [key for key in response if key not in _ENVELOPE_FIELDS]
    if extra:
        return _refuse(
            Code.EXTRA_FIELDS,
            'Your response may contain only "decision" and an optional "explanation"; '
            f"remove {_quoted(extra)}.",
        )
    if "decision" not in response:
        return _refuse(
            Code.SCHEMA_ERROR,
            'Your response has no "decision". It must contain exactly one decision.',
        )
    if "explanation" in response and not _is_explanation(response["explanation"]):
        return _refuse(
            Code.SCHEMA_ERROR,
            f'"explanation" must be a string of at most {EXPLANATION_MAX_CHARS} characters.',
        )
    return None


def _is_explanation(value: object) -> bool:
    return isinstance(value, str) and len(value) <= EXPLANATION_MAX_CHARS


@dataclass(frozen=True, slots=True)
class _Shape:
    """One of the three decision shapes: its action, its one other field and how to read it."""

    article: str
    field: str
    read: Callable[[object], Proposal | Refusal]

    def parse(self, decision: dict[Any, Any]) -> ParseResult:
        extra = [key for key in decision if key not in ("action", self.field)]
        if extra:
            return _refuse(
                Code.EXTRA_FIELDS,
                f'{self.article} decision may contain only "action" and "{self.field}"; '
                f"remove {_quoted(extra)}.",
            )
        if self.field not in decision:
            return _refuse(
                Code.SCHEMA_ERROR, f'{self.article} decision must include "{self.field}".'
            )
        return ParseResult(self.read(decision[self.field]))


def _read_amount(value: object) -> Proposal | Refusal:
    if not isinstance(value, str):
        return Refusal(Code.INVALID_AMOUNT, _AMOUNT_FEEDBACK)
    try:
        amount = MinorAmount.parse(value)
    except ProtocolValueError:
        return Refusal(Code.INVALID_AMOUNT, _AMOUNT_FEEDBACK)
    if amount == 0:
        return Refusal(Code.ZERO_AMOUNT, '"quote_amount_minor" must be greater than zero.')
    return ProposedOffer(amount)


def _read_offer_hash(value: object) -> Proposal | Refusal:
    if not isinstance(value, str):
        return Refusal(Code.INVALID_OFFER_HASH, _HASH_FEEDBACK)
    try:
        return ProposedAccept(Digest(value))
    except ProtocolValueError:
        return Refusal(Code.INVALID_OFFER_HASH, _HASH_FEEDBACK)


def _read_reason(value: object) -> Proposal | Refusal:
    if not isinstance(value, str) or value not in CLOSE_REASON_CODES:
        return Refusal(Code.INVALID_REASON, _REASON_FEEDBACK)
    code = CLOSE_REASON_CODES[value]
    return ProposedWalkAway(close_reason_name(code), code)


_SHAPES: Final[dict[str, _Shape]] = {
    "offer": _Shape("An offer", "quote_amount_minor", _read_amount),
    "accept": _Shape("An accept", "offer_hash", _read_offer_hash),
    "walk_away": _Shape("A walk_away", "reason", _read_reason),
}
