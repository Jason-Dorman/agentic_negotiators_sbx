"""The secret scan is a merge gate (docs/test_strategy.md section 10).

A scanner that has never been shown a secret is a gate that passes forever. These tests show it
one of each of the three things docs/security_and_trust_boundaries.md section 7 names, and then
show it the two things that look like secrets and are not: an EIP-712 digest and a placeholder.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# `infra/scripts` is on pytest's pythonpath (see [tool.pytest.ini_options] in pyproject.toml).
from secret_scan import ANVIL_MNEMONIC, findings_for

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
