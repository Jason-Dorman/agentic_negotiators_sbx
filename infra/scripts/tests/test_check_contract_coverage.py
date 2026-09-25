"""The coverage gate's parsing, which is the gate's only real assertion.

`check_contract_coverage.py` turns a `forge coverage` summary into a pass or a fail. If it read a
missing row, a renamed contract or a changed table format as a pass, the gate would be green for a
reason other than the one it names — which is exactly the failure stage 0 wrote its own bootstrap
tests to avoid.
"""

from __future__ import annotations

import pytest
from check_contract_coverage import MEASURES, TARGET, failures, main

# A `forge coverage --report summary` table, trimmed to the rows that matter and to the columns
# this check reads. The real table carries `(115/115)` counts beside each percentage; the parser
# takes the percentages, and `test_an_integer_percentage_is_read` covers the other formatting it
# has to tolerate.
FULL_REPORT = """
| File                        | % Lines  | % Statements | % Branches | % Funcs  |
| src/MockERC20.sol           | 100.00%  | 100.00%      | 100.00%    | 100.00%  |
| src/NegotiationExchange.sol | 100.00%  | 100.00%      | 100.00%    | 100.00%  |
| Total                       | 100.00%  | 100.00%      | 100.00%    | 100.00%  |
"""


def _row(lines: str, statements: str, branches: str, functions: str) -> str:
    return (
        f"| {TARGET} | {lines}% (1/1) | {statements}% (1/1) "
        f"| {branches}% (1/1) | {functions}% (1/1) |"
    )


class TestThreshold:
    def test_a_full_report_passes(self) -> None:
        assert failures(FULL_REPORT) == []

    @pytest.mark.parametrize(
        ("index", "measure"),
        list(enumerate(MEASURES)),
        ids=MEASURES,
    )
    def test_any_single_measure_below_one_hundred_fails(self, index: int, measure: str) -> None:
        # One test per column, so a parser that read only the first percentage — the mistake this
        # gate exists to avoid — fails three of these four.
        values = ["100.00"] * len(MEASURES)
        values[index] = "99.13"
        problems = failures(_row(*values))

        assert len(problems) == 1
        assert measure in problems[0]
        assert "99.13" in problems[0]

    def test_all_shortfalls_are_reported_not_just_the_first(self) -> None:
        problems = failures(_row("99.13", "100.00", "96.77", "100.00"))
        assert len(problems) == 2
        assert "lines" in problems[0]
        assert "branches" in problems[1]

    def test_ninety_nine_point_nine_nine_is_not_one_hundred(self) -> None:
        assert failures(_row("99.99", "100.00", "100.00", "100.00"))

    def test_an_integer_percentage_is_read(self) -> None:
        # `forge` prints `100.00%`, but the gate must not depend on the decimals being there.
        assert failures(_row("100", "100", "100", "100")) == []


class TestReportsThatAreNotAPass:
    def test_a_missing_contract_row_fails(self) -> None:
        # The important one. An empty or unrelated report means `forge coverage` did not measure
        # the contract, and reading that as a pass would make the gate decorative.
        report = FULL_REPORT.replace(TARGET, "src/SomethingElse.sol")
        problems = failures(report)

        assert len(problems) == 1
        assert "missing" in problems[0]

    def test_an_empty_report_fails(self) -> None:
        assert failures("")

    def test_a_row_with_too_few_columns_fails(self) -> None:
        # A future `forge` that drops or adds a column must fail rather than have its columns
        # silently misread as different measures.
        problems = failures(f"| {TARGET} | 100.00% (1/1) | 100.00% (1/1) |")

        assert len(problems) == 1
        assert "format changed" in problems[0]

    def test_a_row_with_too_many_columns_fails(self) -> None:
        problems = failures(f"| {TARGET} | " + "100.00% (1/1) | " * 5)

        assert len(problems) == 1
        assert "format changed" in problems[0]


class TestEntryPoint:
    def test_a_passing_report_exits_zero(self, tmp_path) -> None:  # type: ignore[no-untyped-def]  # reason: pytest's tmp_path fixture
        report = tmp_path / "coverage.txt"
        report.write_text(FULL_REPORT, encoding="utf-8")
        assert main([str(report)]) == 0

    def test_a_failing_report_exits_one(self, tmp_path) -> None:  # type: ignore[no-untyped-def]  # reason: pytest's tmp_path fixture
        report = tmp_path / "coverage.txt"
        report.write_text(_row("99.13", "100.00", "100.00", "100.00"), encoding="utf-8")
        assert main([str(report)]) == 1

    def test_an_unreadable_report_exits_one(self, tmp_path) -> None:  # type: ignore[no-untyped-def]  # reason: pytest's tmp_path fixture
        # Not zero. "The report is not there" and "the report is fine" must not share a status.
        assert main([str(tmp_path / "does-not-exist.txt")]) == 1
