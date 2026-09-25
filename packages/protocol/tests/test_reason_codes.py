"""The Python reason table against `fixtures/reason_codes.v1.json`.

The codes go on-chain: a participant signs `Close.reason`, the operator passes
`abortSession(reason)`, and the contract reverts on anything undefined. The strings are what the
UI shows and what a policy returns. A code present in one language and not the other would mean a
timeline sentence describing a different walk-away than the chain recorded, so the fixture is the
single source and both tables are checked against it. The TypeScript half lives in
`reason_codes.test.ts`.
"""

from __future__ import annotations

import pytest

from negotiation_protocol import (
    ABORT_REASON_CODES,
    ABORT_REASONS,
    CLOSE_REASON_CODES,
    CLOSE_REASONS,
    UnknownReasonCodeError,
    abort_reason_code,
    abort_reason_name,
    close_reason_code,
    close_reason_name,
    load_fixture,
)

FIXTURE = load_fixture("reason_codes.v1.json")


class TestTablesMatchTheFixture:
    def test_close_reasons_match(self) -> None:
        expected = {entry["code"]: entry["name"] for entry in FIXTURE["close_reasons"]}
        assert dict(CLOSE_REASONS) == expected

    def test_abort_reasons_match(self) -> None:
        expected = {entry["code"]: entry["name"] for entry in FIXTURE["abort_reasons"]}
        assert dict(ABORT_REASONS) == expected

    def test_the_codes_are_the_contiguous_ranges_the_contract_enforces(self) -> None:
        # `closeSession` accepts 1..3 and `abortSession` 1..4, as literal comparisons against
        # MAX_CLOSE_REASON and MAX_ABORT_REASON. A gap or a zero here would be a code the
        # contract refuses and the application believes in (docs/protocol.md section 8.2).
        assert sorted(CLOSE_REASONS) == [1, 2, 3]
        assert sorted(ABORT_REASONS) == [1, 2, 3, 4]

    def test_the_reverse_maps_are_exact_inverses(self) -> None:
        assert {name: code for code, name in CLOSE_REASONS.items()} == CLOSE_REASON_CODES
        assert {name: code for code, name in ABORT_REASONS.items()} == ABORT_REASON_CODES

    def test_no_name_is_shared_between_the_two_tables(self) -> None:
        # They are separate namespaces with overlapping integers, so a shared name would make a
        # rendered reason ambiguous about who ended the session.
        assert not set(CLOSE_REASON_CODES) & set(ABORT_REASON_CODES)

    def test_the_tables_are_not_mutable_by_a_caller(self) -> None:
        # Module-level mutable state is forbidden (docs/contributing.md section 2.1), and a table
        # a caller could extend would let an unknown code become known at run time.
        with pytest.raises(TypeError):
            CLOSE_REASONS[9] = "invented"  # type: ignore[index]  # reason: the refusal is the test


class TestRendering:
    @pytest.mark.parametrize(
        ("code", "name"),
        [(1, "terms_unacceptable"), (2, "inventory_constraint"), (3, "no_further_concession")],
    )
    def test_a_close_code_renders_and_round_trips(self, code: int, name: str) -> None:
        assert close_reason_name(code) == name
        assert close_reason_code(name) == code

    @pytest.mark.parametrize(
        ("code", "name"),
        [
            (1, "operator_request"),
            (2, "model_failure"),
            (3, "budget_exhausted"),
            (4, "execution_failure"),
        ],
    )
    def test_an_abort_code_renders_and_round_trips(self, code: int, name: str) -> None:
        assert abort_reason_name(code) == name
        assert abort_reason_code(name) == code

    @pytest.mark.parametrize("code", [0, 4, 5, -1, 255])
    def test_an_undefined_close_code_raises(self, code: int) -> None:
        # Raises rather than returning "unknown". A code outside the table means this package and
        # the deployed contract disagree, and rendering that as an ordinary outcome would hide a
        # protocol mismatch behind a plausible-looking timeline (docs/architecture.md goal 4).
        with pytest.raises(UnknownReasonCodeError):
            close_reason_name(code)

    @pytest.mark.parametrize("code", [0, 5, -1, 255])
    def test_an_undefined_abort_code_raises(self, code: int) -> None:
        with pytest.raises(UnknownReasonCodeError):
            abort_reason_name(code)

    @pytest.mark.parametrize("name", ["", "bored", "operator_request", "TERMS_UNACCEPTABLE"])
    def test_an_unknown_close_name_raises(self, name: str) -> None:
        # `operator_request` is included deliberately: it is a valid *abort* reason, and a policy
        # returning it as a walk-away reason must be refused rather than mapped to code 1.
        with pytest.raises(UnknownReasonCodeError):
            close_reason_code(name)

    @pytest.mark.parametrize("name", ["", "bored", "terms_unacceptable"])
    def test_an_unknown_abort_name_raises(self, name: str) -> None:
        with pytest.raises(UnknownReasonCodeError):
            abort_reason_code(name)


class TestAgainstTheDecisionSchema:
    def test_the_walk_away_reasons_are_exactly_the_close_reasons(self) -> None:
        """The schema's enum and the close table are the same set, in both directions.

        A policy returns `walk_away.reason` as a string and the signer converts it to a code.
        If the schema permitted a name the table could not convert, the signer would raise on a
        decision the validator had already accepted — a failure in the wrong component, several
        steps from its cause.
        """
        from negotiation_protocol import load_schema

        schema = load_schema("agent_decision.v1.json")
        enum = schema["$defs"]["walkAway"]["properties"]["reason"]["enum"]
        assert set(enum) == set(CLOSE_REASON_CODES)
