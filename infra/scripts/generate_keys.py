#!/usr/bin/env python3
"""Generate the four test-network secrets a deployment needs, in the form each profile uses.

    local    `env:` references. Values are printed for pasting into infra/.env.
    sepolia  `keystore:` references. Encrypted web3 keystore JSON is written to
             infra/secrets/ and no secret is ever printed.

Both paths exist from stage 0 rather than the keystore being retrofitted at stage 5, so the
`keystore:` branch of the key holder is exercised from stage 2 and the Sepolia deployment
introduces no new code path (ADR-023).

Neither is production custody. These secrets hold test ETH and mock ERC-20s and nothing else.

    uv run --group tooling python infra/scripts/generate_keys.py --profile local
    KEYSTORE_PASSWORD='…' uv run --group tooling python infra/scripts/generate_keys.py \
        --profile sepolia

Two kinds of secret, following the authority model in docs/security_and_trust_boundaries.md
section 4 and ADR-039:

- **Keys** for the relay, which pays gas and cannot sign a trade, and the operator, which opens and
  aborts sessions. Each is used as it is, so its address is printed: it is the address to fund.
- **Roots** for the two agent instances, `BUYER_ROOT_KEY` and `SELLER_ROOT_KEY`. A root never
  signs anything and is never funded; each agent derives a fresh participant key from it for every
  run. No address is printed for a root, because the address a root would have is one nothing ever
  uses, and printing it invites someone to fund it.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from eth_account import Account

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
SECRETS_DIR: Final = REPO_ROOT / "infra" / "secrets"


@dataclass(frozen=True)
class Secret:
    role: str
    variable: str
    ref_variable: str
    keystore: str
    is_root: bool


SECRETS: Final = (
    Secret("relay", "RELAY_PRIVATE_KEY", "RELAY_KEY_REF", "relay.json", is_root=False),
    Secret("operator", "OPERATOR_PRIVATE_KEY", "OPERATOR_KEY_REF", "operator.json", is_root=False),
    Secret("buyer", "BUYER_ROOT_KEY", "BUYER_ROOT_KEY_REF", "buyer-root.json", is_root=True),
    Secret("seller", "SELLER_ROOT_KEY", "SELLER_ROOT_KEY_REF", "seller-root.json", is_root=True),
)


def describe(secret: Secret, address: str) -> str:
    if secret.is_root:
        return f"{secret.role} agent root: never signs; each run's key is derived from it (ADR-039)"
    return f"{secret.role}: {address}"


def write_private(path: Path, payload: str) -> None:
    """Write owner-readable only, and refuse to clobber an existing keystore."""
    if path.exists():
        raise SystemExit(
            f"{path} already exists. Move or delete it first; this script never overwrites a key."
        )
    path.write_text(payload, encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def emit_local() -> None:
    print("# Fresh secrets for the local profile. Paste into infra/.env, which is git-ignored.")
    print("# Throwaway values for chain 31337. Fund the relay and operator from an Anvil account.")
    for secret in SECRETS:
        account = Account.create()
        print(f"\n# {describe(secret, account.address)}")
        print(f"{secret.ref_variable}=env:{secret.variable}")
        print(f"{secret.variable}=0x{account.key.hex().removeprefix('0x')}")


def emit_sepolia(password: str, directory: Path = SECRETS_DIR) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    print("# Encrypted keystores written to infra/secrets/. Paste the references into infra/.env.")
    print("# No secret is printed by this branch, and none is stored outside these files.")
    for secret in SECRETS:
        account = Account.create()
        keystore = Account.encrypt(account.key, password)
        path = directory / secret.keystore
        write_private(path, json.dumps(keystore, indent=2) + "\n")
        # The path is the container's view: infra/secrets is mounted read-only at /run/secrets.
        print(f"\n# {describe(secret, account.address)}  ->  infra/secrets/{secret.keystore}")
        print(f"{secret.ref_variable}=keystore:/run/secrets/{secret.keystore}")


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
