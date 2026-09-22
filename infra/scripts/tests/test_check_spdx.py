"""ADR-032's identifier is compiled into contract metadata, so it has to be right before the
first deployment, not corrected after one. These tests show the check a correct header, the two
ways of getting it wrong that look right, and a file with no header at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from check_spdx import REQUIRED, main

BODY = "pragma solidity ^0.8.28;\n\ncontract Example {}\n"


def write(tmp_path: Path, header: str) -> Path:
    path = tmp_path / "Example.sol"
    path.write_text(header + BODY, encoding="utf-8")
    return path


def test_accepts_the_required_header(tmp_path: Path) -> None:
    assert main([str(write(tmp_path, REQUIRED + "\n"))]) == 0


@pytest.mark.parametrize(
    "header",
    [
        "",
        "// SPDX-License-Identifier: MIT\n",
        "// SPDX-License-Identifier: UNLICENSED\n",
        # Present, but not first: solc reads the first line, and so does the metadata.
        "pragma solidity ^0.8.28;\n" + REQUIRED + "\n",
    ],
)
def test_rejects_anything_else(tmp_path: Path, header: str) -> None:
    assert main([str(write(tmp_path, header))]) == 1


def test_ignores_files_that_are_not_solidity(tmp_path: Path) -> None:
    other = tmp_path / "notes.md"
    other.write_text("no identifier here\n", encoding="utf-8")
    assert main([str(other)]) == 0
