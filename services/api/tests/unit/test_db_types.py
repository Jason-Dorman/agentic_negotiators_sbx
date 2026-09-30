"""The column types refuse what the value objects refuse, on the way in and on the way out.

No database needed: a `TypeDecorator`'s two conversion methods are plain functions of a value. What
they guard is the one path the value objects cannot see — a value that is already in a column,
written there by hand or by a future migration — so a fractional or malformed value read back fails
loudly instead of travelling onward as a bare `Decimal` or `str`.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from sqlalchemy.dialects import postgresql

from api.db.types import (
    AddressType,
    DigestType,
    MinorAmountType,
    SessionIdType,
    SignedAmountType,
)
from negotiation_protocol import Address, Digest, MinorAmount, ProtocolValueError, SessionId

DIALECT = postgresql.dialect()  # type: ignore[no-untyped-call]  # reason: SQLAlchemy's dialect factory is untyped


class TestMinorAmountType:
    def test_round_trip(self) -> None:
        column = MinorAmountType()
        stored = column.process_bind_param(MinorAmount(94_000_000), DIALECT)
        assert stored == Decimal(94_000_000)
        read = column.process_result_value(stored, DIALECT)
        assert read == 94_000_000 and type(read) is MinorAmount

    def test_null_passes_through(self) -> None:
        column = MinorAmountType()
        assert column.process_bind_param(None, DIALECT) is None
        assert column.process_result_value(None, DIALECT) is None

    def test_a_negative_int_that_slipped_past_the_type_checker_is_refused(self) -> None:
        with pytest.raises(ProtocolValueError):
            MinorAmountType().process_bind_param(-1, DIALECT)  # type: ignore[arg-type]  # reason: the refusal is the test

    @pytest.mark.parametrize("value", [Decimal("1.5"), 7, "7"])
    def test_a_non_integral_or_non_decimal_value_read_back_is_refused(self, value: object) -> None:
        with pytest.raises(ValueError, match="non-integral"):
            MinorAmountType().process_result_value(value, DIALECT)


class TestSignedAmountType:
    def test_negative_values_round_trip(self) -> None:
        column = SignedAmountType()
        stored = column.process_bind_param(-5, DIALECT)
        assert stored == Decimal(-5)
        assert column.process_result_value(stored, DIALECT) == -5
        assert column.process_bind_param(None, DIALECT) is None
        assert column.process_result_value(None, DIALECT) is None

    @pytest.mark.parametrize("value", [True, 1.0])
    def test_bools_and_floats_are_refused(self, value: object) -> None:
        with pytest.raises(TypeError, match="integer"):
            SignedAmountType().process_bind_param(value, DIALECT)  # type: ignore[arg-type]  # reason: the refusal is the test

    def test_a_fractional_value_read_back_is_refused(self) -> None:
        with pytest.raises(ValueError, match="non-integral"):
            SignedAmountType().process_result_value(Decimal("-0.5"), DIALECT)


class TestTextValueTypes:
    def test_addresses_are_stored_checksummed(self) -> None:
        lower = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"
        stored = AddressType().process_bind_param(Address(lower), DIALECT)
        assert stored == "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
        assert type(AddressType().process_result_value(stored, DIALECT)) is Address
        assert AddressType().process_bind_param(None, DIALECT) is None
        assert AddressType().process_result_value(None, DIALECT) is None

    def test_a_malformed_address_read_back_is_refused(self) -> None:
        with pytest.raises(ProtocolValueError):
            AddressType().process_result_value("not-an-address", DIALECT)

    def test_digests_and_session_ids_keep_their_types(self) -> None:
        value = "0x" + "ab" * 32
        assert type(DigestType().process_result_value(value, DIALECT)) is Digest
        assert type(SessionIdType().process_result_value(value, DIALECT)) is SessionId
        assert DigestType().process_bind_param(Digest(value), DIALECT) == value
        assert SessionIdType().process_bind_param(SessionId(value), DIALECT) == value
        for column in (DigestType(), SessionIdType()):
            assert column.process_bind_param(None, DIALECT) is None
            assert column.process_result_value(None, DIALECT) is None
