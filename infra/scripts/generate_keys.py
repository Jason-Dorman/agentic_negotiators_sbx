#!/usr/bin/env python3
"""Generate the four test-network keys a run needs, in the form each profile uses.

    local    `env:` references. Keys are printed for pasting into infra/.env.
    sepolia  `keystore:` references. Encrypted web3 keystore JSON is written to
             infra/secrets/ and no private key is ever printed.

Both paths exist from stage 0 rather than the keystore being retrofitted at stage 5, so the
`keystore:` branch of the key holder is exercised from stage 2 and the Sepolia deployment
introduces no new code path (ADR-023).

Neither is production custody. These keys hold test ETH and mock ERC-20s and nothing else.

    uv run --group tooling python infra/scripts/generate_keys.py --profile local
    KEYSTORE_PASSWORD='…' uv run --group tooling python infra/scripts/generate_keys.py \
        --profile sepolia

The four roles are the authority model in docs/security_and_trust_boundaries.md section 4:
the relay pays gas and cannot sign a trade; the operator opens and aborts sessions; the two
participant keys are real trading authority and each reaches exactly one agent instance.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path
from typing import Final

from eth_account import Account

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SECRETS_DIR: Final = REPO_ROOT / "infra" / "secrets"

# role -> the environment variable name the key reference points at
ROLES: Final[dict[str, str]] = {
    "relay": "RELAY_PRIVATE_KEY",
    "operator": "OPERATOR_PRIVATE_KEY",
    "buyer": "BUYER_PRIVATE_KEY",
    "seller": "SELLER_PRIVATE_KEY",
}


def write_private(path: Path, payload: str) -> None:
    """Write owner-readable only, and refuse to clobber an existing keystore."""
    if path.exists():
        raise SystemExit(
            f"{path} already exists. Move or delete it first; this script never overwrites a key."
        )
    path.write_text(payload, encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def emit_local() -> None:
    print("# Fresh keys for the local profile. Paste into infra/.env, which is git-ignored.")
    print("# These are throwaway keys for chain 31337. Fund them from an Anvil account.")
    for role, var in ROLES.items():
        account = Account.create()
        ref_var = f"{role.upper()}_KEY_REF"
        print(f"\n# {role}: {account.address}")
        print(f"{ref_var}=env:{var}")
        print(f"{var}=0x{account.key.hex().removeprefix('0x')}")


def emit_sepolia(password: str) -> None:
    SECRETS_DIR.mkdir(parents=True, exist_ok=True)
    print("# Encrypted keystores written to infra/secrets/. Paste the references into infra/.env.")
    print("# No private key is printed by this branch, and none is stored outside these files.")
    for role, _ in ROLES.items():
        account = Account.create()
        keystore = Account.encrypt(account.key, password)
        path = SECRETS_DIR / f"{role}.json"
        write_private(path, json.dumps(keystore, indent=2) + "\n")
        ref_var = f"{role.upper()}_KEY_REF"
        # The path is the container's view: infra/secrets is mounted read-only at /run/secrets.
        print(f"\n# {role}: {account.address}  ->  {path.relative_to(REPO_ROOT)}")
        print(f"{ref_var}=keystore:/run/secrets/{role}.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("local", "sepolia"), required=True)
    args = parser.parse_args(argv)

    if args.profile == "local":
        emit_local()
        return 0

    password = os.environ.get("KEYSTORE_PASSWORD", "")
    if not password:
        print(
            "KEYSTORE_PASSWORD is not set. The Sepolia profile stores keys encrypted "
            "(ADR-023); set it in your shell rather than passing it as an argument, so it "
            "does not land in shell history.",
            file=sys.stderr,
        )
        return 2

    emit_sepolia(password)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
