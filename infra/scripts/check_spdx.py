#!/usr/bin/env python3
"""Every Solidity source starts with `// SPDX-License-Identifier: Apache-2.0` (ADR-032).

This is not a style rule. The identifier is compiled into contract metadata, so it reaches the
deployed artefact and the verified source on the explorer; getting it wrong is not something a
later commit can correct for an already-deployed contract. Hence a check that runs before the
file is ever committed, from stage 0, rather than a convention that stage 1 has to remember.

With no arguments it checks every tracked `.sol` file.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
REQUIRED: Final = "// SPDX-License-Identifier: Apache-2.0"


def tracked_solidity() -> list[Path]:
    out = subprocess.run(
        # Fixed argv, no shell. git is a documented prerequisite of every toolchain here.
        ["git", "ls-files", "-z", "*.sol"],  # noqa: S607  reason: git resolved from PATH
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    return [REPO_ROOT / name for name in out.stdout.split("\0") if name]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="files to check; default: tracked .sol")
    args = parser.parse_args(argv)

    candidates = [p for p in args.paths if p.suffix == ".sol"] if args.paths else tracked_solidity()

    failures = 0
    for path in candidates:
        if not path.is_file() or "lib/" in path.as_posix():
            continue
        first = path.read_text(encoding="utf-8").splitlines()[:1]
        if first and first[0].strip() == REQUIRED:
            continue
        print(f"{path}:1: first line must be exactly `{REQUIRED}` (ADR-032)")
        failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
