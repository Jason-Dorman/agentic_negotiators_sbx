"""The secret scan is a merge gate (docs/test_strategy.md section 10).

A scanner that has never been shown a secret is a gate that passes forever. These tests show it
one of each of the three things docs/security_and_trust_boundaries.md section 7 names, and then
show it the two things that look like secrets and are not: an EIP-712 digest and a placeholder.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# `infra/scripts` is on pytest's pythonpath (see [tool.pytest.ini_options] in pyproject.toml).
from secret_scan import ANVIL_MNEMONIC, REPO_ROOT, findings_for

# A real-looking 32-byte value, written once and reused, so no test line reads like a key.
HEX32 = "ac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"


def scan(tmp_path: Path, name: str, content: str) -> list[tuple[int, str]]:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return findings_for(path, content)


def test_catches_a_private_key_on_a_key_named_line(tmp_path: Path) -> None:
    assert scan(tmp_path, "settings.py", f'BUYER_PRIVATE_KEY = "0x{HEX32}"\n')


def test_catches_an_anthropic_credential(tmp_path: Path) -> None:
    found = scan(tmp_path, "client.py", 'Anthropic(api_key="sk-ant-api03-AbCdEf0123456789xyz")\n')
    assert any("sk-ant-" in reason for _, reason in found)


def test_catches_an_encrypted_keystore_file(tmp_path: Path) -> None:
    keystore = (
        '{"address":"aa","crypto":{"cipher":"aes-128-ctr","ciphertext":"beef",'
        '"cipherparams":{"iv":"00"},"kdf":"scrypt","kdfparams":{"n":8192}},"version":3}\n'
    )
    found = scan(tmp_path, "relay.json", keystore)
    assert any("keystore" in reason for _, reason in found)


def test_catches_a_bare_value_in_a_dotenv_file(tmp_path: Path) -> None:
    """An .env file is all values, so context is not needed to judge one."""
    assert scan(tmp_path, ".env", f"SOME_SETTING=0x{HEX32}\n")


@pytest.mark.parametrize(
    ("name", "content"),
    [
        # Stage 1 commits fixtures full of 32-byte digests. None of them is a key.
        ("fixtures.py", f'EXPECTED_DIGEST = "0x{HEX32}"\n'),
        ("tx.json", f'{{"transactionHash": "0x{HEX32}"}}\n'),
        # The template carries placeholders only.
        (".env.example", "RELAY_PRIVATE_KEY=\nPOSTGRES_PASSWORD=change-me-locally\n"),
        ("keys.md", "PRIVATE_KEY=<your key here>\n"),
        # Anvil's published test mnemonic is named in infra/compose.local.yaml on purpose.
        ("compose.yaml", f'      - "--mnemonic"\n      - "{ANVIL_MNEMONIC}"\n'),
    ],
)
def test_leaves_the_look_alikes_alone(tmp_path: Path, name: str, content: str) -> None:
    assert scan(tmp_path, name, content) == []


class TestEveryHexValueOnALineIsChecked:
    """The scanner looked at the first 64-hex match per line and stopped.

    Found by an adversarial review of the stage 1 fixtures. `HEX64.search` returns one match, and
    the placeholder test was applied to that one, so a placeholder-looking value in front of a real
    key made the whole line pass. The stage 1 EIP-712 fixture is exactly this shape: worthless keys
    written as 32 bytes of leading zeros, on lines that name a key.
    """

    def test_a_real_key_after_a_placeholder_on_the_same_line_is_found(self, tmp_path: Path) -> None:
        placeholder = "0x" + "0" * 64
        real = "0x" + "9f" * 32
        target = tmp_path / "fixture.json"
        target.write_text(f'{{"example_key": "{placeholder}", "private_key": "{real}"}}\n')

        findings = findings_for(target, target.read_text())

        assert len(findings) == 1
        assert findings[0][0] == 1
        assert "names a key" in findings[0][1]

    def test_a_real_key_before_a_placeholder_is_still_found(self, tmp_path: Path) -> None:
        target = tmp_path / "fixture.json"
        target.write_text(f'{{"private_key": "0x{"ab" * 32}", "example": "0x{"0" * 64}"}}\n')

        assert len(findings_for(target, target.read_text())) == 1

    def test_several_placeholders_on_one_line_still_pass(self, tmp_path: Path) -> None:
        # The committed fixture's own shape: two worthless keys as padded small integers. This must
        # stay quiet, or the scanner becomes a gate people learn to override.
        target = tmp_path / "eip712.v1.json"
        buyer = "0x" + "0" * 61 + "b0b"
        seller = "0x" + "0" * 58 + "5e11e4"
        target.write_text(f'{{"buyer_private_key": "{buyer}", "seller_private_key": "{seller}"}}\n')

        assert findings_for(target, target.read_text()) == []

    def test_one_line_yields_at_most_one_finding(self, tmp_path: Path) -> None:
        # Two real keys on a line is one problem to fix, not two lines of output.
        target = tmp_path / "keys.json"
        target.write_text(f'{{"private_key": "0x{"ab" * 32}", "signing_key": "0x{"cd" * 32}"}}\n')

        assert len(findings_for(target, target.read_text())) == 1

    def test_the_committed_eip712_fixture_is_still_clean(self) -> None:
        # The real file, not a reconstruction of it. If the scanner starts flagging it, that is a
        # decision to make deliberately rather than a surprise in someone's pre-commit hook.
        fixture = REPO_ROOT / "packages" / "protocol" / "fixtures" / "eip712.v1.json"
        assert fixture.is_file()
        assert findings_for(fixture, fixture.read_text(encoding="utf-8")) == []
