"""A throwaway Anvil and a real deployment, for every suite whose claim is about a real chain.

Moved here from this directory's conftest in stage 2.2, when the agent service's exit test became
the second suite to need a chain: fixtures cannot be imported across test directories, functions
can.
`packages/protocol/tests` is on pytest's `pythonpath`, so this imports by bare name anywhere.

**Skipping is a local convenience, not a CI behaviour** — the rule the PostgreSQL harness set in
stage 2.1, applied to the Foundry toolchain. Without `anvil` or `forge` a developer gets a clear
skip; with `REQUIRE_INTEGRATION=1`, set by CI and `make ci`, the same absence is a failure, because
a suite that skips reports green without having run.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Final

import pytest

REPO_ROOT: Final = Path(__file__).resolve().parents[3]
CONTRACTS_DIR: Final = REPO_ROOT / "contracts"

#: Where test deployments write their manifests. Under `contracts/out/`, not `docs/deployments/`:
#: that is where real manifests live, and a test must not leave a file there that looks like one;
#: pytest's temp directory is outside Foundry's `fs_permissions`, and widening those for a test
#: would weaken the one setting that says which paths a script may write.
TEST_MANIFEST_DIR: Final = CONTRACTS_DIR / "out" / "test-deployments"

# Anvil's published test accounts, in its own order. Worthless by construction and named in
# infra/compose.local.yaml on purpose (ADR-023).
ANVIL_KEYS: Final = (
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",  # 0 deployer, operator
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",  # 1 relay
    "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",  # 2 buyer
    "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",  # 3 seller
)


def integration_required() -> bool:
    return os.environ.get("REQUIRE_INTEGRATION", "").lower() in {"1", "true", "yes"}


def require_tool(name: str) -> None:
    """Skip without the Foundry tool `name`, or fail when integration is required."""
    if shutil.which(name) is not None:
        return
    message = f"{name} not on PATH; install Foundry (see README prerequisites)"
    if integration_required():
        pytest.fail(message + ". REQUIRE_INTEGRATION is set, so this is a failure.")
    pytest.skip(message)


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


@contextmanager
def running_anvil() -> Iterator[str]:
    """A fresh Anvil on a free port, chain 31337, torn down on exit. Yields its RPC URL."""
    require_tool("anvil")
    port = free_port()
    process = subprocess.Popen(
        ["anvil", "--port", str(port), "--chain-id", "31337", "--silent"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    url = f"http://127.0.0.1:{port}"
    try:
        _wait_for_rpc(url, process)
        yield url
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover  reason: only on a wedged anvil
            process.kill()


def _wait_for_rpc(url: str, process: subprocess.Popen[bytes], timeout: float = 20.0) -> None:
    from web3 import Web3

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail(f"anvil exited with {process.returncode} before accepting connections")
        if Web3(Web3.HTTPProvider(url)).is_connected():
            return
        time.sleep(0.2)
    process.terminate()
    pytest.fail(f"anvil did not accept connections on {url} within {timeout}s")


def run_deploy_script(
    rpc_url: str,
    deployment_id: str,
    manifest_dir: Path,
    *,
    broadcast: bool = True,
    overwrite: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Invoke the real deploy script. Returns the process, so a caller can assert on a refusal."""
    from eth_account import Account

    environment = {
        **os.environ,
        "DEPLOYMENT_ID": deployment_id,
        "OPERATOR_ADDRESS": Account.from_key(ANVIL_KEYS[0]).address,
        "RELAY_ADDRESS": Account.from_key(ANVIL_KEYS[1]).address,
        "MANIFEST_DIR": str(manifest_dir),
    }
    if overwrite:
        environment["MANIFEST_OVERWRITE"] = "true"

    command = ["forge", "script", "script/Deploy.s.sol:Deploy", "--rpc-url", rpc_url]
    if broadcast:
        command += ["--broadcast", "--private-key", ANVIL_KEYS[0]]

    return subprocess.run(
        command,
        cwd=CONTRACTS_DIR,
        capture_output=True,
        text=True,
        env=environment,
        check=False,
    )


def deploy(rpc_url: str, deployment_id: str) -> dict[str, Any]:
    """Run the real deploy script against `rpc_url` and return its manifest, with `_path` added.

    Any earlier manifest of the same id is removed first rather than overwritten: the script
    refuses to replace one unless `MANIFEST_OVERWRITE` is set, and passing that here would mean the
    suites never exercised the ordinary path.
    """
    require_tool("forge")
    TEST_MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = TEST_MANIFEST_DIR / f"{deployment_id}.json"
    manifest_path.unlink(missing_ok=True)

    completed = run_deploy_script(rpc_url, deployment_id, TEST_MANIFEST_DIR)
    if completed.returncode != 0:
        pytest.fail(f"deploy script failed:\n{completed.stdout}\n{completed.stderr}")
    assert manifest_path.is_file(), f"the deploy script wrote no manifest:\n{completed.stdout}"

    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["_path"] = str(manifest_path)
    return manifest
