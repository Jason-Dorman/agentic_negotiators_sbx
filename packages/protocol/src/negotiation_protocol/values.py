"""Value objects for the protocol's primitives (docs/contributing.md section 2.1).

`MinorAmount`, `Address`, `Digest`, `SessionId` and `Sequence` exist so that a function taking a
price cannot be handed a sequence number and a function taking an address cannot be handed a
digest. Across a module boundary the rule is that none of these travels as a bare `int` or `str`.

Each subclasses the primitive it wraps rather than holding it. That is deliberate: web3, eth-abi,
SQLAlchemy and JSON all accept an `int` or a `str` without an unwrapping step at every call site,
while mypy still treats `MinorAmount` and `int` as different types for a parameter annotated with
one of them. What the subclass adds is the check at construction, so an instance is always in range
and always in canonical form — an address is always EIP-55 checksummed, a digest is always
lowercase — and two equal values compare equal as strings.

Validation raises rather than coercing. A quote of `1.5` minor units, a sequence above `uint64` or
an address with a broken checksum is a bug upstream, and quietly rounding or re-checksumming it
would put a value on the wire that nobody chose (CLAUDE.md: never fix a model-proposed price).

**At the JSON boundary amounts are strings** (docs/api_contract.md section 1). The Pydantic hooks
below enforce that in JSON mode: `"94000000"` is an amount and `94000000` is a validation error,
because a JSON number is a float in most parsers and a price is exact.
"""

from __future__ import annotations

import re
from typing import Any, Final, Self

from eth_utils.address import is_checksum_address, to_checksum_address
from pydantic import GetCoreSchemaHandler, GetJsonSchemaHandler
from pydantic.json_schema import JsonSchemaValue
from pydantic_core import core_schema

from negotiation_protocol.eip712 import ProtocolValueError

UINT256_MAX: Final = 2**256 - 1
UINT64_MAX: Final = 2**64 - 1

#: The schemas' `minorAmount` pattern: base-10, no sign, no leading zeros, at most 78 digits.
MINOR_AMOUNT_PATTERN: Final = r"^(0|[1-9][0-9]{0,77})$"
ADDRESS_PATTERN: Final = r"^0x[0-9a-fA-F]{40}$"
DIGEST_PATTERN: Final = r"^0x[0-9a-fA-F]{64}$"

_MINOR_AMOUNT = re.compile(MINOR_AMOUNT_PATTERN)
_ADDRESS = re.compile(ADDRESS_PATTERN)
_DIGEST = re.compile(DIGEST_PATTERN)


def _require_int(value: object, name: str) -> int:
    # `bool` is an `int` subclass, and `True` as a quote amount is a bug, not a one.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProtocolValueError(f"{name} must be an integer, got {type(value).__name__}")
    return value


class MinorAmount(int):
    """A token amount in minor units: an integer in `0 .. 2**256 - 1`.

    Six decimals means 94 mUSD is `MinorAmount(94_000_000)`. Arithmetic returns a plain `int`,
    which is intended: a difference of two amounts can be negative, and a utility is not an amount.
    """

    __slots__ = ()

    def __new__(cls, value: int) -> Self:
        number = _require_int(value, "MinorAmount")
        if not 0 <= number <= UINT256_MAX:
            raise ProtocolValueError(f"MinorAmount {number} is outside uint256")
        return super().__new__(cls, number)

    @classmethod
    def parse(cls, text: str) -> Self:
        """The JSON form: a base-10 string with no sign, no leading zeros and no exponent."""
        if not isinstance(text, str) or _MINOR_AMOUNT.fullmatch(text) is None:
            raise ProtocolValueError(f"{text!r} is not a minor amount string")
        return cls(int(text))

    def to_json(self) -> str:
        return str(int(self))

    def __repr__(self) -> str:
        return f"MinorAmount({int(self)})"

    def __str__(self) -> str:
        # Without this, `str()` and every f-string fall through to `__repr__`, because `int` has no
        # `__str__` of its own: f"{amount}" rendered as "MinorAmount(94000000)" in any sentence that
        # interpolated one. The decimal is what a reader of a sentence or a feedback text expects.
        return int.__repr__(self)

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.with_info_plain_validator_function(
            _validate_minor_amount,
            serialization=core_schema.plain_serializer_function_ser_schema(
                _minor_amount_to_json, when_used="json"
            ),
        )

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: core_schema.CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return {"type": "string", "pattern": MINOR_AMOUNT_PATTERN}


def _validate_minor_amount(value: object, info: core_schema.ValidationInfo) -> MinorAmount:
    if isinstance(value, MinorAmount):
        return value
    if isinstance(value, str):
        return MinorAmount.parse(value)
    if info.mode == "json":
        raise ProtocolValueError("amounts are base-10 strings in JSON, never numbers")
    return MinorAmount(_require_int(value, "MinorAmount"))


def _minor_amount_to_json(value: MinorAmount) -> str:
    return value.to_json()


class Sequence(int):
    """A per-session action counter: an integer in `0 .. 2**64 - 1` (`uint64` on-chain).

    Zero is valid — it is the stored sequence of a session with no action yet — and every
    participant-signed action carries at least 1 (docs/protocol.md section 5).
    """

    __slots__ = ()

    def __new__(cls, value: int) -> Self:
        number = _require_int(value, "Sequence")
        if not 0 <= number <= UINT64_MAX:
            raise ProtocolValueError(f"Sequence {number} is outside uint64")
        return super().__new__(cls, number)

    def __repr__(self) -> str:
        return f"Sequence({int(self)})"

    def __str__(self) -> str:
        return int.__repr__(self)  # see MinorAmount.__str__

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(
            cls, core_schema.int_schema(strict=True)
        )


class Address(str):
    """An account or contract address, always in its EIP-55 checksummed form.

    An all-lowercase or all-uppercase address is accepted and checksummed. A mixed-case address
    whose checksum is wrong is refused: under EIP-55 that is what a mistyped address looks like,
    and re-checksumming it would silently turn a typo into a different, valid address.
    """

    __slots__ = ()

    def __new__(cls, value: str) -> Self:
        if not isinstance(value, str) or _ADDRESS.fullmatch(value) is None:
            raise ProtocolValueError(f"{value!r} is not 0x followed by 40 hex digits")
        body = value[2:]
        mixed_case = body != body.lower() and body != body.upper()
        if mixed_case and not is_checksum_address(value):
            raise ProtocolValueError(f"{value} has a broken EIP-55 checksum")
        return super().__new__(cls, to_checksum_address(value))

    def __repr__(self) -> str:
        return f"Address({str.__str__(self)!r})"

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: core_schema.CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return {"type": "string", "pattern": ADDRESS_PATTERN}


class Digest(str):
    """A 32-byte value as `0x` + 64 lowercase hex: an EIP-712 digest, a tx or block hash.

    Lowercase is the canonical form (docs/api_contract.md section 1), so equal digests are equal
    strings and a database uniqueness constraint on one cannot be defeated by changing case.
    """

    __slots__ = ()

    def __new__(cls, value: str | bytes) -> Self:
        if isinstance(value, bytes | bytearray):
            if len(value) != 32:
                raise ProtocolValueError(f"expected 32 bytes, got {len(value)}")
            return super().__new__(cls, "0x" + bytes(value).hex())
        if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
            raise ProtocolValueError(f"{value!r} is not 0x followed by 64 hex digits")
        return super().__new__(cls, value.lower())

    def to_bytes(self) -> bytes:
        return bytes.fromhex(self[2:])

    def __repr__(self) -> str:
        return f"{type(self).__name__}({str.__str__(self)!r})"

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.no_info_after_validator_function(cls, core_schema.str_schema())

    @classmethod
    def __get_pydantic_json_schema__(
        cls, schema: core_schema.CoreSchema, handler: GetJsonSchemaHandler
    ) -> JsonSchemaValue:
        return {"type": "string", "pattern": DIGEST_PATTERN}


class SessionId(Digest):
    """An on-chain `sessionId`: a fresh random `bytes32`, never reused (docs/protocol.md section 3).

    A distinct type from `Digest` so that a session id and an offer digest — both 32 bytes, both
    `0x`-hex — cannot be passed for one another.
    """

    __slots__ = ()
