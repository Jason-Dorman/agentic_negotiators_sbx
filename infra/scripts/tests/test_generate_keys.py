"""`generate_keys.py` names the agents' roots as ADR-039 does, and prints no secret on Sepolia."""

from __future__ import annotations

import json
import re
import stat
from pathlib import Path

# `infra/scripts` is on pytest's pythonpath (see [tool.pytest.ini_options] in pyproject.toml).
import generate_keys
import pytest
from eth_account import Account

HEX64 = re.compile(r"[0-9a-fA-F]{64}")


def test_the_local_profile_names_the_two_roots_and_their_references(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert generate_keys.main(["--profile", "local"]) == 0
    out = capsys.readouterr().out
    assert "BUYER_ROOT_KEY_REF=env:BUYER_ROOT_KEY\n" in out
    assert "SELLER_ROOT_KEY_REF=env:SELLER_ROOT_KEY\n" in out
    assert re.search(r"^BUYER_ROOT_KEY=0x[0-9a-f]{64}$", out, re.MULTILINE)
    assert re.search(r"^SELLER_ROOT_KEY=0x[0-9a-f]{64}$", out, re.MULTILINE)
    assert "RELAY_KEY_REF=env:RELAY_PRIVATE_KEY\n" in out
    assert "OPERATOR_KEY_REF=env:OPERATOR_PRIVATE_KEY\n" in out
    assert "BUYER_PRIVATE_KEY" not in out and "SELLER_PRIVATE_KEY" not in out


def test_no_address_is_printed_for_a_root(capsys: pytest.CaptureFixture[str]) -> None:
    """Nothing uses a root's own address, so printing one would only invite funding it."""
    generate_keys.main(["--profile", "local"])
    out = capsys.readouterr().out
    root_comments = [line for line in out.splitlines() if "agent root" in line]
    assert len(root_comments) == 2
    assert not any("0x" in line for line in root_comments)
    key_comments = [line for line in out.splitlines() if line.startswith(("# relay", "# operator"))]
    assert all(re.search(r"0x[0-9a-fA-F]{40}", line) for line in key_comments)


def test_the_sepolia_profile_writes_root_keystores_and_prints_no_secret(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    generate_keys.emit_sepolia("correct horse", tmp_path)
    out = capsys.readouterr().out
    assert "BUYER_ROOT_KEY_REF=keystore:/run/secrets/buyer-root.json\n" in out
    assert "SELLER_ROOT_KEY_REF=keystore:/run/secrets/seller-root.json\n" in out
    assert not HEX64.search(out)
    for name in ("relay.json", "operator.json", "buyer-root.json", "seller-root.json"):
        path = tmp_path / name
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        root = Account.decrypt(json.loads(path.read_text()), "correct horse")
        assert len(root) == 32


def test_the_sepolia_profile_refuses_without_a_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("KEYSTORE_PASSWORD", raising=False)
    assert generate_keys.main(["--profile", "sepolia"]) == 2
    assert "KEYSTORE_PASSWORD is not set" in capsys.readouterr().err


def test_an_existing_keystore_is_never_overwritten(tmp_path: Path) -> None:
    (tmp_path / "relay.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="already exists"):
        generate_keys.emit_sepolia("correct horse", tmp_path)
    assert (tmp_path / "relay.json").read_text(encoding="utf-8") == "{}"
