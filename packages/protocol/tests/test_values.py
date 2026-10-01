"""The value objects refuse what they exist to refuse.

Each class's claim is about construction: an instance is always in range and in canonical form. So
most of these tests are refusals — a value object that accepted `True` as a quote amount, or quietly
re-checksummed a mistyped address, would pass every test that only fed it good input.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from negotiation_protocol import (
    UINT64_MAX,
    UINT256_MAX,
    Address,
    Digest,
    MinorAmount,
    ProtocolValueError,
    Sequence,
    SessionId,
)

# Anvil's account 0, as its checksummed, lowercase and uppercase-hex forms.
CHECKSUMMED = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
LOWER = CHECKSUMMED.lower()


class TestMinorAmount:
    def test_accepts_the_uint256_range_inclusive(self) -> None:
        assert MinorAmount(0) == 0
        assert MinorAmount(UINT256_MAX) == UINT256_MAX

    @pytest.mark.parametrize("value", [-1, UINT256_MAX + 1])
    def test_refuses_values_outside_uint256(self, value: int) -> None:
        with pytest.raises(ProtocolValueError, match="uint256"):
            MinorAmount(value)

    def test_refuses_a_bool_although_bool_is_an_int(self) -> None:
        with pytest.raises(ProtocolValueError, match="integer"):
            MinorAmount(True)

    def test_refuses_a_float_even_an_integral_one(self) -> None:
        with pytest.raises(ProtocolValueError, match="integer"):
            MinorAmount(5.0)  # type: ignore[arg-type]  # reason: the refusal is the point

    @pytest.mark.parametrize(
        "text",
        ["", "01", "-1", "+1", "1.0", "1e6", " 1", "0x10", "9" * 79, str(UINT256_MAX + 1)],
    )
    def test_parse_refuses_anything_but_the_canonical_string(self, text: str) -> None:
        with pytest.raises(ProtocolValueError):
            MinorAmount.parse(text)

    def test_parse_and_to_json_round_trip(self) -> None:
        assert MinorAmount.parse("94000000") == 94_000_000
        assert MinorAmount.parse(str(UINT256_MAX)).to_json() == str(UINT256_MAX)
        assert MinorAmount(0).to_json() == "0"

    def test_arithmetic_leaves_the_type(self) -> None:
        # A difference of two amounts can be negative; it is not an amount.
        difference = MinorAmount(5) - MinorAmount(7)
        assert difference == -2
        assert type(difference) is int


class TestSequence:
    def test_accepts_zero_and_the_uint64_maximum(self) -> None:
        assert Sequence(0) == 0
        assert Sequence(UINT64_MAX) == UINT64_MAX

    @pytest.mark.parametrize("value", [-1, UINT64_MAX + 1])
    def test_refuses_values_outside_uint64(self, value: int) -> None:
        with pytest.raises(ProtocolValueError, match="uint64"):
            Sequence(value)

    def test_refuses_a_bool(self) -> None:
        with pytest.raises(ProtocolValueError):
            Sequence(False)


class TestAddress:
    def test_normalises_to_the_eip55_checksum(self) -> None:
        assert Address(LOWER) == CHECKSUMMED
        assert Address("0x" + LOWER[2:].upper()) == CHECKSUMMED
        assert Address(CHECKSUMMED) == CHECKSUMMED

    def test_refuses_a_mixed_case_address_with_a_broken_checksum(self) -> None:
        # Flip the case of one letter: under EIP-55 that is a typo, and re-checksumming it would
        # turn the typo into a different valid-looking address.
        letter = next(i for i, char in enumerate(CHECKSUMMED) if i >= 2 and char.isalpha())
        broken = CHECKSUMMED[:letter] + CHECKSUMMED[letter].swapcase() + CHECKSUMMED[letter + 1 :]
        assert broken != CHECKSUMMED
        assert broken.lower() == LOWER, "still the same hex, only the case differs"
        with pytest.raises(ProtocolValueError, match="checksum"):
            Address(broken)

    @pytest.mark.parametrize(
        "text", ["", "0x", LOWER[:-1], LOWER + "0", LOWER[2:], "0x" + "g" * 40]
    )
    def test_refuses_malformed_addresses(self, text: str) -> None:
        with pytest.raises(ProtocolValueError):
            Address(text)


class TestDigest:
    def test_normalises_to_lowercase(self) -> None:
        assert Digest("0x" + "AB" * 32) == "0x" + "ab" * 32

    def test_round_trips_bytes(self) -> None:
        raw = bytes(range(32))
        assert Digest(raw).to_bytes() == raw
        assert Digest(raw) == "0x" + raw.hex()

    @pytest.mark.parametrize("value", [b"\x00" * 31, b"\x00" * 33])
    def test_refuses_bytes_of_the_wrong_length(self, value: bytes) -> None:
        with pytest.raises(ProtocolValueError, match="32 bytes"):
            Digest(value)

    @pytest.mark.parametrize("text", ["", "0x" + "a" * 63, "0x" + "a" * 65, "a" * 64])
    def test_refuses_malformed_strings(self, text: str) -> None:
        with pytest.raises(ProtocolValueError):
            Digest(text)

    def test_a_session_id_is_a_distinct_type_with_the_same_form(self) -> None:
        session = SessionId("0x" + "11" * 32)
        assert isinstance(session, Digest)
        assert type(session) is SessionId
        assert repr(session).startswith("SessionId(")


class _Wire(BaseModel):
    amount: MinorAmount
    sequence: Sequence
    address: Address
    digest: Digest


class TestPydanticBoundary:
    """What a request body may carry: amounts as strings, never JSON numbers."""

    def test_json_amounts_must_be_strings(self) -> None:
        good = (
            '{"amount": "94000000", "sequence": 3, '
            f'"address": "{LOWER}", "digest": "0x{"AB" * 32}"}}'
        )
        model = _Wire.model_validate_json(good)
        assert model.amount == 94_000_000 and type(model.amount) is MinorAmount
        assert model.address == CHECKSUMMED
        assert model.digest == "0x" + "ab" * 32

        as_number = good.replace('"94000000"', "94000000")
        with pytest.raises(ValidationError, match="never numbers"):
            _Wire.model_validate_json(as_number)

    def test_a_sequence_is_never_a_string_or_a_bool(self) -> None:
        for bad in ('"3"', "true"):
            body = (
                f'{{"amount": "1", "sequence": {bad}, '
                f'"address": "{LOWER}", "digest": "0x{"ab" * 32}"}}'
            )
            with pytest.raises(ValidationError):
                _Wire.model_validate_json(body)

    def test_json_dump_writes_amounts_as_strings(self) -> None:
        model = _Wire(
            amount=MinorAmount(UINT256_MAX),
            sequence=Sequence(1),
            address=Address(LOWER),
            digest=Digest("0x" + "ab" * 32),
        )
        dumped = model.model_dump(mode="json")
        assert dumped["amount"] == str(UINT256_MAX)
        assert dumped["address"] == CHECKSUMMED

    def test_the_json_schema_names_the_patterns(self) -> None:
        properties = _Wire.model_json_schema()["properties"]
        assert properties["amount"]["type"] == "string"
        assert properties["amount"]["pattern"].startswith("^(0|[1-9]")
        assert properties["address"]["pattern"] == "^0x[0-9a-fA-F]{40}$"


class TestCanonicalJson:
    """One definition of the hash both services record, so it cannot quietly differ between them."""

    def test_key_order_and_whitespace_do_not_change_the_hash(self) -> None:
        from negotiation_protocol import canonical_json, json_sha256

        first = {"b": [1, {"y": "2", "x": None}], "a": True}
        second = {"a": True, "b": [1, {"x": None, "y": "2"}]}
        assert canonical_json(first) == b'{"a":true,"b":[1,{"x":null,"y":"2"}]}'
        assert json_sha256(first) == json_sha256(second)
        assert json_sha256(first).startswith("0x") and len(json_sha256(first)) == 66

    def test_non_ascii_is_utf8_not_escaped(self) -> None:
        from negotiation_protocol import canonical_json

        assert canonical_json({"note": "café"}) == '{"note":"café"}'.encode()

    def test_floats_are_refused_wherever_they_hide(self) -> None:
        from negotiation_protocol import canonical_json

        for value in (1.0, {"a": [1, 2.5]}, [{"deep": {"er": 0.1}}]):
            with pytest.raises(TypeError, match="floats"):
                canonical_json(value)

    def test_non_string_keys_are_refused(self) -> None:
        from negotiation_protocol import canonical_json

        with pytest.raises(TypeError, match="keys"):
            canonical_json({1: "one"})


class TestFormatting:
    """An amount interpolated into a sentence is its decimal, not its `repr`.

    `int` has no `__str__` of its own, so an `int` subclass that overrides `__repr__` is printed by
    `str()` and by every f-string through that override. Before this was fixed, a feedback text
    built as f"your offer of {amount}" read "your offer of MinorAmount(94000000)".
    """

    @pytest.mark.parametrize("value", [MinorAmount(94_000_000), Sequence(3)])
    def test_str_and_f_strings_give_the_decimal(self, value: int) -> None:
        assert str(value) == str(int(value))
        assert f"{value}" == str(int(value))
        assert f"{value:>12}" == f"{int(value):>12}"

    def test_repr_still_names_the_type(self) -> None:
        assert repr(MinorAmount(5)) == "MinorAmount(5)"
        assert repr(Sequence(5)) == "Sequence(5)"
