"""A throwaway chain and a real deployment, for the reconstruction tests.

The reconstruction tool's whole claim is that it needs nothing but an RPC endpoint and a manifest.
Testing it against a fake would test the fake. So these fixtures start a real Anvil, run the real
`script/Deploy.s.sol`, and let the tool read what the chain actually recorded — which is how the
`vm.serializeJson` mistake in the deploy script and the `process_receipt` address bug in the tool
were both found.

The machinery is in `anvil_chain.py`, shared with the agent service's exit test since stage 2.2, and
the tests here import from it by name. Never `from conftest import …`: with more than one
`conftest.py` in the session, a bare `conftest` is whichever was imported last, so the import
depends on collection order (it broke `pytest services/agent/tests packages/protocol/tests`).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from anvil_chain import deploy, running_anvil


@pytest.fixture(scope="session")
def anvil_rpc() -> Iterator[str]:
    """A fresh Anvil on a free port, torn down with the session."""
    with running_anvil() as url:
        yield url


@pytest.fixture(scope="session")
def deployment(anvil_rpc: str) -> dict[str, Any]:
    """The real deploy script, run against the throwaway chain."""
    return deploy(anvil_rpc, "local-test-run")
