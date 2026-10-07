#!/usr/bin/env python3
"""No application code hardcodes a scenario's private bound or names the settlement price.

docs/test_strategy.md section 11 and CLAUDE.md: a model that cannot settle is a result to report,
never something to make pass by writing the answer into the code or the prompt. Two rules (stage
3.3, Q91, amended by its adversarial review, Q94 and Q96):

- **No reservation bound of a committed scenario in a comparison**, in any non-test Python,
  Solidity or TypeScript source. The bounds are read from `scenarios/*.json` — whatever is committed
  there is what is forbidden. A comparison is a statement with `<`, `>`, `<=`, `>=`, `==`, `!=`, a
  `case`, or a `min(` or `max(`, which is how a clamp is written; a statement runs across lines
  while a bracket is open, so a clamp a formatter wrapped is still one statement.
- **No deterministic settlement price anywhere** in those sources: 93.333333 mUSD, what the
  deterministic pair settles at on `default-overlap` (docs/protocol.md section 13). A module that
  names it is a module that knows the answer.
- **Neither, anywhere, in the prompt templates** (`services/agent/prompts/`): the text a model is
  given is the most direct place to plant an answer, and no bound belongs in it at all.

A value is recognised as a literal of any base and grouping: decimal with or without `_` or `,`
grouping, scientific (`1e8`, `100e6`), hexadecimal, octal or binary (`0x5F5E100`), a BigInt (`n`),
quoted or not, and whole tokens at the scenario's decimals with at least that many places
(`100.000000`, `93.3333330`), followed by anything but a letter, a digit or another fraction. An
arithmetic expression — `100 * 10**6`, `10**8` — is not evaluated, and a bound behind a named
constant compared in another statement is not caught; code review is the control for both
(docs/contributing.md section 5), and the second rule holds for the one price that would make a
demonstration pass. Tests are exempt: they are where expected values belong. So is this file, the
one module that has to name what it forbids.

With no arguments it checks the repository; `--root` checks another tree, which is how its tests
plant a literal.
"""

from __future__ import annotations

import argparse
import json
import re
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SOURCE_ROOTS: Final = (
    "services",
    "packages",
    "apps",
    "contracts/src",
    "contracts/script",
    "infra/scripts",
)
SUFFIXES: Final = frozenset({".py", ".sol", ".ts", ".tsx"})
#: The prompt templates, where both rules hold on every line.
PROMPT_ROOTS: Final = ("services/agent/prompts",)
PROMPT_SUFFIXES: Final = frozenset({".md", ".txt"})
#: Test and tool directories, excluded wherever they are.
EXCLUDED_ANYWHERE: Final = frozenset(
    {"tests", "test", "e2e", "node_modules", ".venv", "__pycache__"}
)
#: Build outputs, excluded only beside the manifest that produces them: `apps/web/dist`, never a
#: source directory that happens to share the name, such as `apps/web/src/lib`.
BUILD_OUTPUTS: Final = frozenset({"lib", "dist", "build", "out", "cache", "broadcast", "coverage"})
MANIFESTS: Final = ("package.json", "pyproject.toml", "foundry.toml")
TEST_NAMES: Final = re.compile(r"^(test_.*\.py|conftest\.py|.*\.(test|spec)\.tsx?|.*\.t\.sol)$")
#: The deterministic pair's settlement on `default-overlap`, in minor units (protocol 13).
SETTLEMENT_PRICES: Final = {"default-overlap": 93_333_333}

_DECIMAL: Final = re.compile(
    r"(?<![\w.])(?P<whole>\d[\d_]*)(?:\.(?P<fraction>\d+))?(?:[eE](?P<exponent>[+-]?\d+))?n?"
    r"(?!\w|\.\d)"
)
_GROUPED: Final = re.compile(
    r"(?<![\w.,])(?P<whole>\d{1,3}(?:,\d{3})+)(?:\.(?P<fraction>\d+))?(?!\w|,\d|\.\d)"
)
_RADIX: Final = re.compile(r"(?<![\w.])(?P<radix>0[xXoObB][0-9a-fA-F_]+)n?(?!\w)")
_COMPARISON: Final = re.compile(
    r"<=|>=|==|!=|(?<![-=<])<(?![<=])|(?<![-=>])>(?![>=])|\bcase\b|\b(?:min|max)\("
)
_OPENERS: Final = str.maketrans("", "", "".join(set(map(chr, range(128))) - set("([{")))
_CLOSERS: Final = str.maketrans("", "", "".join(set(map(chr, range(128))) - set(")]}")))


class Forbidden:
    """The values the rules name, each with what it is, for the message."""

    def __init__(self, root: Path) -> None:
        self.bounds: dict[int, str] = {}
        self.prices: dict[int, str] = {}
        self.decimals: set[int] = set()
        for path in sorted((root / "scenarios").glob("*.json")):
            scenario = json.loads(path.read_text(encoding="utf-8"))
            self.decimals.add(int(scenario["public_config"]["token_decimals"]))
            for role in ("buyer", "seller"):
                bound = int(scenario[role]["mandate"]["reservation_price_minor"])
                self.bounds[bound] = f"the {role}'s reservation bound in {path.name}"
            price = SETTLEMENT_PRICES.get(scenario["scenario_id"])
            if price is not None:
                self.prices[price] = f"the deterministic settlement price of {path.name}"
        if not self.bounds:
            raise SystemExit(f"no scenario found under {root / 'scenarios'}: nothing to check")

    def values(self, line: str) -> Iterator[int]:
        """Every minor-unit value a literal on the line can stand for."""
        for literal in _RADIX.finditer(line):
            try:
                yield int(literal["radix"].replace("_", ""), 0)
            except ValueError:
                continue
        for pattern in (_DECIMAL, _GROUPED):
            for literal in pattern.finditer(line):
                yield from self._decimal(literal)

    def _decimal(self, literal: re.Match[str]) -> Iterator[int]:
        """The literal itself, and whole tokens at a scenario's decimals when written with at
        least that many places."""
        whole = literal["whole"].replace(",", "").replace("_", "")
        fraction = literal["fraction"]
        digits = whole + (f".{fraction}" if fraction else "")
        exponent = literal.groupdict().get("exponent")
        value = Decimal(digits).scaleb(int(exponent or 0))
        candidates = [value]
        if fraction is not None:
            candidates += [value.scaleb(d) for d in self.decimals if len(fraction) >= d]
        for candidate in candidates:
            if candidate == candidate.to_integral_value():
                yield int(candidate)


def _excluded(path: Path, root: Path) -> bool:
    directories = path.relative_to(root).parts[:-1]
    if EXCLUDED_ANYWHERE.intersection(directories):
        return True
    for depth, name in enumerate(directories):
        if name in BUILD_OUTPUTS:
            parent = root.joinpath(*directories[:depth])
            if any((parent / manifest).is_file() for manifest in MANIFESTS):
                return True
    return False


def _files(root: Path, roots: tuple[str, ...], suffixes: frozenset[str]) -> Iterator[Path]:
    for name in roots:
        base = root / name
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if (
                path.suffix in suffixes
                and path.is_file()
                and path.resolve() != Path(__file__).resolve()
                and not _excluded(path, root)
                and not TEST_NAMES.match(path.name)
            ):
                yield path


def sources(root: Path) -> Iterator[Path]:
    """The code the two code rules read."""
    return _files(root, SOURCE_ROOTS, SUFFIXES)


def prompts(root: Path) -> Iterator[Path]:
    return _files(root, PROMPT_ROOTS, PROMPT_SUFFIXES)


def statements(lines: list[str]) -> Iterator[tuple[int, list[str]]]:
    """Physical lines grouped into statements: a line continues the one before while a bracket the
    statement opened is still open, or after a trailing backslash. A blank line always ends one, so
    an unbalanced bracket in a comment cannot swallow the rest of a file."""
    start, depth, continued = 1, 0, False
    group: list[str] = []
    for number, line in enumerate(lines, start=1):
        if not group:
            start = number
        group.append(line)
        opened = len(line.translate(_OPENERS)) - len(line.translate(_CLOSERS))
        depth = max(0, depth + opened)
        continued = line.rstrip().endswith("\\")
        if (depth == 0 and not continued) or not line.strip():
            yield start, group
            group, depth = [], 0
    if group:
        yield start, group


def findings(root: Path) -> Iterator[str]:
    forbidden = Forbidden(root)
    yield from _code_findings(root, forbidden)
    yield from _prompt_findings(root, forbidden)


def _code_findings(root: Path, forbidden: Forbidden) -> Iterator[str]:
    for path in sources(root):
        lines = path.read_text(encoding="utf-8").splitlines()
        for start, group in statements(lines):
            compares = _COMPARISON.search("\n".join(group)) is not None
            for offset, line in enumerate(group):
                where = f"{path.relative_to(root)}:{start + offset}"
                for value in forbidden.values(line):
                    if value in forbidden.prices:
                        yield f"{where}: names {forbidden.prices[value]}"
                    elif compares and value in forbidden.bounds:
                        yield f"{where}: compares against {forbidden.bounds[value]}"


def _prompt_findings(root: Path, forbidden: Forbidden) -> Iterator[str]:
    for path in prompts(root):
        lines = path.read_text(encoding="utf-8").splitlines()
        for number, line in enumerate(lines, start=1):
            where = f"{path.relative_to(root)}:{number}"
            for value in forbidden.values(line):
                if value in forbidden.prices:
                    yield f"{where}: names {forbidden.prices[value]}"
                elif value in forbidden.bounds:
                    yield f"{where}: names {forbidden.bounds[value]} in a prompt"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="the tree to check")
    args = parser.parse_args(argv)
    found = list(findings(args.root))
    for finding in found:
        print(finding)
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
