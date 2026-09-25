"""A throwaway chain and a real deployment, for the reconstruction tests.

The reconstruction tool's whole claim is that it needs nothing but an RPC endpoint and a manifest.
Testing it against a fake would test the fake. So these fixtures start a real Anvil, run the real
`script/Deploy.s.sol`, and let the tool read what the chain actually recorded — which is how the
`vm.serializeJson` mistake in the deploy script and the `process_receipt` address bug in the tool
were both found.

Everything here skips rather than fails when `anvil` or `forge` is absent: the Foundry toolchain is
a documented prerequisite, but a contributor running only the Python suite should get a clear skip,
not a stack trace. The CI contract gate installs Foundry, so the tests do run there.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
CONTRACTS_DIR = REPO_ROOT / "contracts"

# Anvil's published test accounts, in its own order. Worthless by construction and named in
# infra/compose.local.yaml on purpose (ADR-023).
ANVIL_KEYS = (
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80",  # 0 deployer, operator
    "0x59c6995e998f97a5a0044966f0945389dc9e86dae88c7a8412f4603b6b78690d",  # 1 relay
    "0x5de4111afa1a4b94908f83103eb1f1706367c2e68ca870fc3fb9a804cdab365a",  # 2 buyer
    "0x7c852118294e51e653712a81e05800f419141751be58f605c371e15141b007a6",  # 3 seller
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


@pytest.fixture(scope="session")
def anvil_rpc() -> Iterator[str]:
    """A fresh Anvil on a free port, torn down with the session."""
    if shutil.which("anvil") is None:
        pytest.skip("anvil not on PATH; install Foundry (see README prerequisites)")

    port = _free_port()
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


TEST_MANIFEST_DIR = CONTRACTS_DIR / "out" / "test-deployments"


@pytest.fixture(scope="session")
def deployment(anvil_rpc: str) -> dict[str, Any]:
    """The real deploy script, run against the throwaway chain.

    The manifest lands under `contracts/out/`, not in `docs/deployments/` and not in pytest's temp
    directory. `docs/deployments/` is where real manifests live and a test must not leave a file
    there that looks like one; pytest's temp directory is outside Foundry's `fs_permissions`, and
    widening those to `/tmp` for a test's convenience would weaken the one setting that says which
    paths a script may write. `contracts/out/` is already writable, already git-ignored, and is
    build output by definition.
    """
    if shutil.which("forge") is None:
        pytest.skip("forge not on PATH; install Foundry (see README prerequisites)")

    TEST_MANIFEST_DIR.mkdir(parents=True, exist_ok=True)

    # Removed rather than overwritten. The script refuses to replace an existing manifest unless
    # `MANIFEST_OVERWRITE` is set, because a manifest is the only record of the deployment it
    # describes. Passing that flag here would mean the suite never exercised the ordinary path;
    # `TestDeployScriptPreconditions` covers the refusal separately.
    manifest_path = TEST_MANIFEST_DIR / "local-test-run.json"
    manifest_path.unlink(missing_ok=True)

    completed = run_deploy_script(
        anvil_rpc, deployment_id="local-test-run", manifest_dir=TEST_MANIFEST_DIR
    )
    if completed.returncode != 0:
        pytest.fail(f"deploy script failed:\n{completed.stdout}\n{completed.stderr}")

    assert manifest_path.is_file(), f"the deploy script wrote no manifest:\n{completed.stdout}"

    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["_path"] = str(manifest_path)
    return manifest
