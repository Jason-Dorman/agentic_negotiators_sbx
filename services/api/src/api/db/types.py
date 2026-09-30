"""Column types that carry the protocol's value objects into and out of PostgreSQL.

A row read through these comes back as a `MinorAmount`, `Address`, `Digest` or `SessionId`, never
as the `Decimal` or `str` the driver produced, so the value-object rule (docs/contributing.md
section 2.1) holds on the way out of the database as well as on the way in. A malformed value in a
column — something written by hand in `psql` — fails loudly on read instead of travelling onward as
a plain string.

`NUMERIC(78,0)` is the amount type (data model principle 5, ADR-020): 78 digits hold `2**256 - 1`,
and a scale of 0 means PostgreSQL stores integers only. It would *round* a fractional input rather
than refuse it, which is why nothing reaches this column except through `MinorAmount`, which is an
`int` and cannot be fractional.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import Numeric, Text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from negotiation_protocol import Address, Digest, MinorAmount, SessionId

#: The PostgreSQL type of every token amount (ADR-020).
AMOUNT_NUMERIC = Numeric(78, 0, asdecimal=True)

#: Model-cost figures in US dollars, to the micro-dollar (data model section 3.8).
USD_NUMERIC = Numeric(12, 6, asdecimal=True)


class MinorAmountType(TypeDecorator[MinorAmount]):
    """`NUMERIC(78,0)` <-> `MinorAmount`."""

    impl = AMOUNT_NUMERIC
    cache_ok = True

    def process_bind_param(self, value: MinorAmount | None, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        # Re-validated here as well as at construction: an `int` that slipped past the type checker
        # would otherwise reach the column unchecked, and a negative one would be stored.
        return Decimal(MinorAmount(value))

    def process_result_value(self, value: object, dialect: Dialect) -> MinorAmount | None:
        if value is None:
            return None
        if not isinstance(value, Decimal) or value != value.to_integral_value():
            raise ValueError(f"amount column held a non-integral value {value!r}")
        return MinorAmount(int(value))


class SignedAmountType(TypeDecorator[int]):
    """`NUMERIC(78,0)` <-> `int`, for the few figures that may be negative.

    A reservation utility is a difference of two amounts, and a mandate violation makes it negative
    (spec section 11.2), so it is not a `MinorAmount`. It is still an integer in minor units.
    """

    impl = AMOUNT_NUMERIC
    cache_ok = True

    def process_bind_param(self, value: int | None, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"expected an integer amount, got {type(value).__name__}")
        return Decimal(value)

    def process_result_value(self, value: object, dialect: Dialect) -> int | None:
        if value is None:
            return None
        if not isinstance(value, Decimal) or value != value.to_integral_value():
            raise ValueError(f"amount column held a non-integral value {value!r}")
        return int(value)


class AddressType(TypeDecorator[Address]):
    """`TEXT` <-> `Address`, always stored EIP-55 checksummed."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Address | None, dialect: Dialect) -> str | None:
        return None if value is None else str(Address(value))

    def process_result_value(self, value: object, dialect: Dialect) -> Address | None:
        return None if value is None else Address(str(value))


class DigestType(TypeDecorator[Digest]):
    """`TEXT` <-> `Digest`, always stored as `0x` + 64 lowercase hex."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: Digest | None, dialect: Dialect) -> str | None:
        return None if value is None else str(Digest(value))

    def process_result_value(self, value: object, dialect: Dialect) -> Digest | None:
        return None if value is None else Digest(str(value))


class SessionIdType(TypeDecorator[SessionId]):
    """`TEXT` <-> `SessionId`."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value: SessionId | None, dialect: Dialect) -> str | None:
        return None if value is None else str(SessionId(value))

    def process_result_value(self, value: object, dialect: Dialect) -> SessionId | None:
        return None if value is None else SessionId(str(value))
