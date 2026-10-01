"""A real chain, a real deployment and two real agent processes, for stage 2.2's exit test.

The two instances are configured the two ways ADR-023 names, so both reference forms are exercised
end to end by the same test: the buyer's root arrives as `env:BUYER_ROOT_KEY`, the seller's as a
`keystore:` file encrypted with the default scrypt parameters `generate_keys.py` uses and opened
with `KEYSTORE_PASSWORD`. Each has its own shared secret. The roots are fresh random values for
the run of the suite and never leave this fixture except into their own process's environment.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from agent_harness import AgentProcess, Chain, running_agent
from anvil_chain import deploy, running_anvil
from eth_account import Account

INTEGRATION_DIR = Path(__file__).resolve().parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    for item in items:
        if INTEGRATION_DIR in Path(str(item.fspath)).resolve().parents:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(scope="module")
def anvil_rpc() -> Iterator[str]:
    with running_anvil() as url:
        yield url


@pytest.fixture(scope="module")
def deployment(anvil_rpc: str) -> dict[str, Any]:
    return deploy(anvil_rpc, "local-agent-test")


@pytest.fixture(scope="module")
def chain(anvil_rpc: str, deployment: dict[str, Any]) -> Chain:
    return Chain(anvil_rpc, deployment)


@pytest.fixture(scope="module")
def agents(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, AgentProcess]]:
    directory = tmp_path_factory.mktemp("agents")
    buyer_root = secrets.token_bytes(32)
    seller_root = secrets.token_bytes(32)
    password = secrets.token_urlsafe(16)
    buyer_secret, seller_secret = secrets.token_hex(32), secrets.token_hex(32)
    keystore = directory / "seller-root.json"
    keystore.write_text(json.dumps(Account.encrypt(seller_root, password)), encoding="utf-8")

    with (
        running_agent(
            role="buyer",
            instance="agent-a",
            key_ref="env:BUYER_ROOT_KEY",
            root_address=Account.from_key(buyer_root).address,
            secret=buyer_secret,
            environment={"BUYER_ROOT_KEY": "0x" + buyer_root.hex()},
            log_path=directory / "agent-a.log",
            private_values=(buyer_root.hex(), buyer_root.hex().upper(), buyer_secret),
        ) as buyer,
        running_agent(
            role="seller",
            instance="agent-b",
            key_ref=f"keystore:{keystore}",
            root_address=Account.from_key(seller_root).address,
            secret=seller_secret,
            environment={"KEYSTORE_PASSWORD": password},
            log_path=directory / "agent-b.log",
            private_values=(seller_root.hex(), seller_root.hex().upper(), seller_secret, password),
        ) as seller,
    ):
        yield {"buyer": buyer, "seller": seller}
