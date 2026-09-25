#!/usr/bin/env python3
"""Assert the coverage gate on `NegotiationExchange`: 100 percent, all four measures.

    forge coverage --root contracts --no-match-coverage "(test|script)/" --report summary > cov.txt
    uv run python infra/scripts/check_contract_coverage.py cov.txt

docs/test_strategy.md section 10. The contract is one of the three places where a missed branch is
an incorrect transfer rather than an untested error path, so the threshold is all four figures
`forge coverage` reports — lines, statements, branches and functions — and not lines alone.

This lives in a script with tests rather than in a heredoc inside `.github/workflows/ci.yml`,
because the gate's only real assertion is this parsing. A gate whose logic cannot be tested is one
that is believed rather than known, which is the thing stage 0 set out to avoid — and an absent or
malformed row must fail loudly rather than read as a pass.

Exit status: 0 when the threshold is met, 1 when it is not or the report cannot be read.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

#: The contract the threshold applies to, as `forge coverage` names it in the summary table.
TARGET = "src/NegotiationExchange.sol"

#: The four measures, in the column order `forge coverage --report summary` prints them.
MEASURES = ("lines", "statements", "branches", "functions")

THRESHOLD = 100.0

_PERCENTAGE = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def failures(report: str) -> list[str]:
    """Every reason the report does not meet the gate. Empty means the gate passes."""
    row = next((line for line in report.splitlines() if TARGET in line), None)
    if row is None:
        return [
            f"{TARGET} is missing from the coverage report. Either the contract was renamed or "
            "`forge coverage` did not run; an absent row is not a pass."
        ]

    found = _PERCENTAGE.findall(row)
    if len(found) != len(MEASURES):
        return [
            f"expected {len(MEASURES)} percentages in the {TARGET} row, found {found}. "
            "The summary format changed, so this check can no longer read it.\n"
            f"  row: {row.strip()}"
        ]

    return [
        f"{measure} at {value}%, below {THRESHOLD:g}%"
        for measure, value in zip(MEASURES, found, strict=True)
        if float(value) < THRESHOLD
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "report",
        type=Path,
        help="a file holding the output of `forge coverage --report summary`",
    )
    args = parser.parse_args(argv)

    try:
        report = args.report.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"cannot read {args.report}: {exc}", file=sys.stderr)
        return 1

    problems = failures(report)
    if problems:
        print(
            f"{TARGET} does not meet the coverage gate (docs/test_strategy.md section 10):",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 1

    print(f"{TARGET}: {THRESHOLD:g} percent of {', '.join(MEASURES)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
