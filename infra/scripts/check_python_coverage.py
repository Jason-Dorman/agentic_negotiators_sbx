#!/usr/bin/env python3
"""Assert the Python coverage thresholds of docs/test_strategy.md section 10 on a JSON report.

    uv run pytest --cov --cov-report=json:coverage.json
    uv run python infra/scripts/check_python_coverage.py coverage.json \\
        --rule 'services/api/src/api/**:lines:85'

A rule is `<glob>:<metric>:<percent>`. The metric is `lines` (covered statements over statements) or
`branches` (covered branches over branches), and the percentage is taken over **all files the glob
matches together**, which is how "backend 85 percent lines" reads.

This is the gate's whole assertion, so it lives in a script with tests rather than in a workflow
heredoc (docs/contributing.md section 3), and the failure it is built around is the quiet one:
**a rule that matches no file is a failure, not a pass.** A threshold over nothing is vacuously met,
and a renamed directory would otherwise turn the gate green for good — the same trap as `all([])`
in the reconstruction tool.

Exit status: 0 every rule met, 1 a rule missed or matched nothing, 2 the report could not be read.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

METRICS = ("lines", "branches")


@dataclass(frozen=True)
class Rule:
    pattern: str
    metric: str
    threshold: float

    @classmethod
    def parse(cls, text: str) -> Rule:
        pattern, _, rest = text.rpartition(":")
        pattern, _, metric = pattern.rpartition(":")
        if not pattern or metric not in METRICS:
            raise ValueError(f"rule {text!r} is not <glob>:<lines|branches>:<percent>")
        threshold = float(rest)
        if not 0 <= threshold <= 100:
            raise ValueError(f"rule {text!r}: the percentage must be between 0 and 100")
        return cls(pattern, metric, threshold)


@dataclass(frozen=True)
class Result:
    rule: Rule
    files: int
    covered: int
    total: int

    @property
    def percent(self) -> float:
        # Nothing to cover in matched files (every one of them branch-free, say) is fully covered.
        # Matching no file at all is a different case, and is refused by `ok`.
        return 100.0 if self.total == 0 else 100.0 * self.covered / self.total

    @property
    def ok(self) -> bool:
        return self.files > 0 and self.percent >= self.rule.threshold

    def describe(self) -> str:
        if self.files == 0:
            return f"FAIL {self.rule.pattern} matched no file in the report"
        verdict = "ok  " if self.ok else "FAIL"
        return (
            f"{verdict} {self.rule.pattern} {self.rule.metric}: {self.percent:.2f}% "
            f"({self.covered}/{self.total} over {self.files} files), "
            f"threshold {self.rule.threshold:g}%"
        )


def evaluate(report: dict[str, Any], rule: Rule) -> Result:
    files = [
        summary
        for path, entry in report.get("files", {}).items()
        if fnmatch(path, rule.pattern) and (summary := entry.get("summary")) is not None
    ]
    if rule.metric == "lines":
        covered = sum(int(s["covered_lines"]) for s in files)
        total = sum(int(s["num_statements"]) for s in files)
    else:
        covered = sum(int(s.get("covered_branches", 0)) for s in files)
        total = sum(int(s.get("num_branches", 0)) for s in files)
    return Result(rule, len(files), covered, total)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("report", type=Path, help="coverage JSON (`--cov-report=json:<path>`)")
    parser.add_argument("--rule", action="append", required=True, help="<glob>:<metric>:<percent>")
    args = parser.parse_args(argv)

    try:
        rules = [Rule.parse(text) for text in args.rule]
    except ValueError as error:
        print(f"check_python_coverage: {error}", file=sys.stderr)
        return 2

    try:
        report = json.loads(args.report.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        print(f"check_python_coverage: cannot read {args.report}: {error}", file=sys.stderr)
        return 2

    results = [evaluate(report, rule) for rule in rules]
    for result in results:
        print(result.describe())
    return 0 if all(result.ok for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
