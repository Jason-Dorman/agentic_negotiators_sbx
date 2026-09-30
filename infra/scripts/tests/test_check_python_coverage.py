"""The Python coverage gate passes what meets its rules and refuses the rest, including nothing.

The failure this gate is built around is the quiet one: a rule whose glob matches no file. Over no
files every threshold is vacuously met, so a renamed directory would turn the gate green for good.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from check_python_coverage import Rule, evaluate, main


def _file(statements: int, covered: int, branches: int = 0, covered_branches: int = 0) -> Any:
    return {
        "summary": {
            "num_statements": statements,
            "covered_lines": covered,
            "num_branches": branches,
            "covered_branches": covered_branches,
        }
    }


REPORT: dict[str, Any] = {
    "files": {
        "services/api/src/api/db/models.py": _file(100, 90),
        "services/api/src/api/db/types.py": _file(100, 80),
        "services/agent/src/agent/validation/validator.py": _file(50, 50, 20, 19),
        "services/agent/src/agent/signing/signer.py": _file(40, 40, 0, 0),
    }
}


class TestRules:
    def test_parse(self) -> None:
        assert Rule.parse("services/api/src/api/**:lines:85") == Rule(
            "services/api/src/api/**", "lines", 85.0
        )

    @pytest.mark.parametrize("text", ["x:lines", "x:words:10", ":lines:10", "x:lines:101"])
    def test_malformed_rules_are_refused(self, text: str) -> None:
        with pytest.raises(ValueError):
            Rule.parse(text)


class TestEvaluate:
    def test_lines_are_pooled_across_every_matching_file(self) -> None:
        result = evaluate(REPORT, Rule("services/api/src/api/*", "lines", 85))
        assert (result.files, result.covered, result.total) == (2, 170, 200)
        assert result.percent == 85.0 and result.ok

    def test_a_point_below_is_a_failure(self) -> None:
        assert not evaluate(REPORT, Rule("services/api/src/api/*", "lines", 85.5)).ok

    def test_branches(self) -> None:
        result = evaluate(REPORT, Rule("*/validator.py", "branches", 100))
        assert (result.covered, result.total) == (19, 20)
        assert not result.ok

    def test_a_matched_file_with_no_branches_is_fully_covered(self) -> None:
        assert evaluate(REPORT, Rule("*/signer.py", "branches", 100)).ok

    def test_matching_nothing_is_a_failure_not_a_vacuous_pass(self) -> None:
        result = evaluate(REPORT, Rule("services/renamed/**", "lines", 0))
        assert result.files == 0
        assert not result.ok
        assert "matched no file" in result.describe()


class TestMain:
    def test_exit_statuses(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        report = tmp_path / "coverage.json"
        report.write_text(json.dumps(REPORT), encoding="utf-8")
        assert main([str(report), "--rule", "services/api/src/api/*:lines:85"]) == 0
        assert main([str(report), "--rule", "services/api/src/api/*:lines:86"]) == 1
        assert main([str(report), "--rule", "nothing/**:lines:0"]) == 1
        assert main([str(report), "--rule", "bad-rule"]) == 2
        assert main([str(tmp_path / "missing.json"), "--rule", "x:lines:1"]) == 2
        output = capsys.readouterr().out
        assert "ok   services/api/src/api/* lines: 85.00%" in output
        assert "FAIL nothing/** matched no file" in output
