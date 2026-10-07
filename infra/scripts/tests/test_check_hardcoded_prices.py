"""The no-hardcoding gate (stage 3.3, Q91): the repository passes, and a planted literal fails it.

Each test builds a small tree with the committed scenarios in it, plants one line in a non-test
source, and runs the gate over that tree. The repository itself is checked last, so the gate is in
CI as a test as well as in `make lint`.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from check_hardcoded_prices import REPO_ROOT, main


def tree(tmp_path: Path, relative: str, line: str) -> Path:
    shutil.copytree(REPO_ROOT / "scenarios", tmp_path / "scenarios")
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{line}\n", encoding="utf-8")
    return tmp_path


def gate(root: Path) -> int:
    return main(["--root", str(root)])


def test_the_repository_passes() -> None:
    assert gate(REPO_ROOT) == 0


@pytest.mark.parametrize(
    ("relative", "line"),
    [
        ("services/api/src/api/m.py", "if price <= 100_000_000:"),
        ("services/api/src/api/m.py", "if price <= 100000000:"),
        ("services/agent/src/agent/m.py", 'accept = amount == "90000000"'),
        ("services/agent/src/agent/m.py", "quote = min(quote, 90_000_000)"),
        ("services/agent/src/agent/m.py", "match amount:\n    case 105000000: pass"),
        ("packages/protocol/src/negotiation_protocol/m.py", "ok = 1e8 >= x"),
        ("contracts/src/M.sol", "require(price <= 100e6);"),
        ("apps/web/src/m.ts", "if (amount > 90000000n) {}"),
        ("apps/web/src/m.tsx", 'const ok = shown !== "105.000000";'),
        ("infra/scripts/m.py", "assert x != 100.000000"),
        # The stage 3.3 review: other bases, a following full stop, a bare `<`, `max(`.
        ("services/api/src/api/m.py", "if y > 0x5f5e100:"),
        ("contracts/src/M.sol", "require(price <= 0x5F5E100);"),
        ("services/api/src/api/m.py", "if y >= 0o575360400:"),
        ("services/api/src/api/m.py", "if y >= 0b101_1111_0101_1110_0001_0000_0000:"),
        ("services/api/src/api/m.py", "if offer > 100000000.:"),
        ("services/api/src/api/m.py", "if offer < 90_000_000:"),
        ("services/api/src/api/m.py", "quote = max(quote, 90_000_000)"),
        ("services/api/src/api/m.py", "quote = min(\n    quote,\n    100_000_000,\n)"),
        ("apps/web/src/m.ts", "const q = Math.min(\n  quote,\n  90000000n,\n);"),
        ("services/api/src/api/m.py", "ok = price <= \\\n    105_000_000"),
        # A source directory named like a build output is still source.
        ("apps/web/src/lib/m.ts", "if (amount > 90000000n) {}"),
    ],
)
def test_a_scenario_bound_in_a_comparison_fails(tmp_path: Path, relative: str, line: str) -> None:
    assert gate(tree(tmp_path, relative, line)) == 1


@pytest.mark.parametrize(
    ("relative", "line"),
    [
        ("services/api/src/api/m.py", "PRICE = 93_333_333"),
        ("services/agent/src/agent/m.py", '"""Settles at 93.333333 mUSD."""'),
        ("apps/web/src/m.ts", "const expected = 93333333n;"),
        ("contracts/script/M.sol", "uint256 constant P = 93333333;"),
        # The stage 3.3 review: a following full stop, a display grouping, trailing zeros, a base.
        ("services/api/src/api/m.py", "# settles at 93333333."),
        ("services/agent/src/agent/m.py", '"""Settles at 93.333333."""'),
        ("apps/web/src/m.ts", "const p = 93333333.;"),
        ("apps/web/src/m.tsx", 'const shown = "93,333,333";'),
        ("services/api/src/api/m.py", "P = 93.3333330"),
        ("services/api/src/api/m.py", "P = 0x5902755"),
        # Q94: the prompt templates, where any bound is forbidden too.
        ("services/agent/prompts/v9/system.md", "Settle at about 93.333333 mUSD."),
        ("services/agent/prompts/v9/system.md", "Your counterparty's floor is 90000000."),
    ],
)
def test_the_settlement_price_anywhere_fails(tmp_path: Path, relative: str, line: str) -> None:
    assert gate(tree(tmp_path, relative, line)) == 1


@pytest.mark.parametrize(
    ("relative", "line"),
    [
        # A bound outside a comparison: a default, not a decision.
        ("services/api/src/api/m.py", "DEFAULT = 100_000_000"),
        # Numbers that merely contain a bound's digits, or a hex literal of another value.
        ("services/api/src/api/m.py", "if x <= 1000000000 or y > 0x5f5e101:"),
        ("services/api/src/api/m.py", "if version == '100000000.1':"),
        # A run of digits ending in an underscore is read, not refused with an error.
        ("services/api/src/api/m.py", 'if note == "build 100_ of 7":'),
        # Whole tokens with the wrong number of places, and a percentage.
        ("infra/scripts/m.py", "if lines < 100.0:"),
        # A fraction is not a whole number: the lookbehind keeps `1.93333333` one literal.
        ("services/api/src/api/m.py", "ratio = 1.93333333"),
        ("services/api/src/api/m.py", "if lines < 1.90000000:"),
        # An arrow is not a comparison.
        ("apps/web/src/m.ts", "const f = (x: number) => 100_000_000;"),
        ("services/api/src/api/m.py", "def f() -> int: return 100_000_000"),
        # A blank line ends a statement, so a bracket left open in a comment does not join the
        # next one's literal to an earlier comparison.
        ("services/api/src/api/m.py", "# note (if x < 1\n\nDEFAULT = 100_000_000"),
        # The bound in a prompt's arithmetic is not forbidden when it is not the bound.
        ("services/agent/prompts/v9/system.md", 'so "94000000" is 94 whole tokens'),
        # Tests are where expected values belong.
        ("services/api/tests/unit/test_m.py", "assert price == 93_333_333"),
        ("services/agent/tests/support/m.py", "if price <= 100_000_000:"),
        ("apps/web/src/m.test.ts", "expect(price).toBe(93333333n);"),
        ("contracts/test/M.t.sol", "assertEq(price, 93333333);"),
        ("contracts/src/M.t.sol", "assertEq(price, 93333333);"),
        ("services/api/src/api/conftest.py", "PRICE = 93_333_333"),
        ("apps/web/src/m.spec.ts", "expect(price).toBe(93333333n);"),
        ("services/api/src/api/test_m.py", "PRICE = 93_333_333"),
        ("apps/web/src/test/m.ts", "const p = 93333333n;"),
        ("apps/web/node_modules/x/m.ts", "const p = 93333333n;"),
        # Outside the source roots.
        ("docs/m.py", "if price <= 100_000_000:"),
    ],
)
def test_what_is_not_hardcoding_passes(tmp_path: Path, relative: str, line: str) -> None:
    assert gate(tree(tmp_path, relative, line)) == 0


def test_a_build_output_beside_its_manifest_is_not_source(tmp_path: Path) -> None:
    root = tree(tmp_path, "apps/web/dist/m.ts", "const p = 93333333n;")
    assert gate(root) == 1  # no manifest: `dist` is just a directory
    (root / "apps/web/package.json").write_text("{}", encoding="utf-8")
    assert gate(root) == 0
    (root / "apps/web/src").mkdir(parents=True)
    (root / "apps/web/src/lib").mkdir()
    (root / "apps/web/src/lib/m.ts").write_text("const p = 93333333n;\n", encoding="utf-8")
    assert gate(root) == 1  # `src/lib` is beside no manifest


def test_a_finding_names_the_physical_line_of_a_wrapped_statement(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tree(
        tmp_path,
        "services/api/src/api/m.py",
        "x = 1\nquote = min(\n    quote,\n    100_000_000,\n)",
    )
    assert gate(root) == 1
    assert "services/api/src/api/m.py:4: compares against" in capsys.readouterr().out


def test_the_forbidden_bounds_follow_the_committed_scenarios(tmp_path: Path) -> None:
    root = tree(tmp_path, "services/api/src/api/m.py", "if price <= 77_000_000:")
    assert gate(root) == 0
    path = root / "scenarios" / "default-overlap.json"
    path.write_text(path.read_text().replace('"100000000"', '"77000000"', 1), encoding="utf-8")
    assert gate(root) == 1


def test_a_tree_with_no_scenarios_is_refused_rather_than_passed(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match="no scenario"):
        gate(tmp_path)
