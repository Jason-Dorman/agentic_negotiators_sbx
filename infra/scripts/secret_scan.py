#!/usr/bin/env python3
"""Reject the three things docs/security_and_trust_boundaries.md section 7 names.

    hex private keys, `sk-ant-` prefixes, and keystore JSON

Run by the pre-commit hook over staged files and by CI over every tracked file. With no
arguments it scans `git ls-files`.

The hard part is not catching secrets, it is not crying wolf: an EIP-712 digest, a transaction
hash and a block hash are all 32 bytes of hex, and stage 1 commits fixtures full of them. So a
bare 64-hex string is not evidence on its own. It is evidence when it sits on a line that names
a key, or in a file whose whole purpose is to hold configuration values.

Exit status is 1 if anything is found, which fails the commit and the build.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# Anvil's published test mnemonic and the accounts it derives. Worthless by construction, and
# named in infra/compose.local.yaml on purpose (ADR-023).
ANVIL_MNEMONIC = "test test test test test test test test test test test junk"

# Paths whose contents are documentation about secrets rather than secrets.
SKIP_DIRS = frozenset(
    {".git", "node_modules", ".venv", "venv", "out", "cache", "dist", "coverage", "lib"}
)
SKIP_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".ico", ".pdf", ".woff", ".woff2"})

HEX64 = re.compile(r"(?<![0-9a-fA-F])(?:0x)?[0-9a-fA-F]{64}(?![0-9a-fA-F])")
SECRET_CONTEXT = re.compile(
    r"(?i)(private[_-]?key|privkey|secret[_-]?key|signing[_-]?key|mnemonic|seed[_-]?phrase)"
)
ANTHROPIC_KEY = re.compile(r"sk-ant-[A-Za-z0-9_\-]{16,}")
# A value that is obviously a stand-in is not a leak.
PLACEHOLDER = re.compile(
    r"(?i)"
    r"(0x)?(0{16,}|f{16,}|deadbeef|abcdef01|1234567890abcdef|<[^>]+>|\.\.\.|…)"
    r"|(change[_-]?me|placeholder|example|your[_-]|redacted|xxx+)"
)
KEYSTORE_MARKERS = ("ciphertext", "kdfparams", "cipherparams")

# Files that discuss the patterns above in prose, including this scanner itself, and the test
# that has to hold one real-looking sample of each pattern in order to prove the scanner works.
# Adding to this list is a reviewed change; the list is deliberately short.
ALLOWED_PATHS = frozenset(
    {
        "infra/scripts/secret_scan.py",
        "infra/scripts/tests/test_secret_scan.py",
        "infra/.env.example",
        "infra/secrets/README.md",
        "docs/security_and_trust_boundaries.md",
        "docs/decision_log.md",
    }
)


def tracked_files() -> list[Path]:
    out = subprocess.run(
        # Fixed argv, no shell. git is a documented prerequisite of every toolchain here.
        ["git", "ls-files", "-z"],  # noqa: S607  reason: git resolved from PATH by design
        cwd=REPO_ROOT,
        capture_output=True,
        check=True,
        text=True,
    )
    return [REPO_ROOT / name for name in out.stdout.split("\0") if name]


def is_scannable(path: Path) -> bool:
    if not path.is_file():
        return False
    if path.suffix.lower() in SKIP_SUFFIXES:
        return False
    return not SKIP_DIRS.intersection(path.parts)


def repo_relative(path: Path) -> str:
    """Path as written in the repository, or the absolute path if it lies outside."""
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def findings_for(path: Path, text: str) -> list[tuple[int, str]]:
    """Return (line number, reason) for every hit in one file."""
    if repo_relative(path) in ALLOWED_PATHS:
        return []
    found: list[tuple[int, str]] = []

    lowered = text.lower()
    if all(marker in lowered for marker in KEYSTORE_MARKERS):
        found.append((1, "looks like an encrypted keystore file; it belongs in infra/secrets/"))

    is_env_file = path.name.startswith(".env") and path.name != ".env.example"

    for number, line in enumerate(text.splitlines(), start=1):
        if ANTHROPIC_KEY.search(line):
            found.append((number, "Anthropic API key (`sk-ant-` prefix)"))

        if ANVIL_MNEMONIC in line:
            continue

        hex_hit = HEX64.search(line)
        if hex_hit and not PLACEHOLDER.search(hex_hit.group(0)):
            if SECRET_CONTEXT.search(line):
                found.append((number, "32 bytes of hex on a line that names a key"))
            elif is_env_file:
                found.append((number, "32 bytes of hex in an environment file"))

    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="files to scan; default: git ls-files")
    args = parser.parse_args(argv)

    candidates = [p.resolve() for p in args.paths] if args.paths else tracked_files()

    failures = 0
    for path in candidates:
        if not is_scannable(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, reason in findings_for(path, text):
            print(f"{repo_relative(path)}:{number}: {reason}")
            failures += 1

    if failures:
        print(
            f"\n{failures} possible secret(s). Keys are references (`env:`, `keystore:`), "
            "never values: docs/security_and_trust_boundaries.md section 7.",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
