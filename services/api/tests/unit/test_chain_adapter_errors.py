"""`Web3ChainAdapter` against a JSON-RPC server that misbehaves the way hosted RPCs do.

A real HTTP server on a free port, answering each method as the test configures it: a JSON-RPC
error over HTTP 200, a null result, an HTML page, a 503. The stage 2.3 review found that every one
of these used to escape the adapter as a web3 type — or, for a block, came back as "no such block",
which the indexer took for a reorg. Here each must come out as exactly one of the adapter's three
failures, and a block may be None only above the head.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from api.chain import RpcUnavailableError, TransactionRejectedError, Web3ChainAdapter
from negotiation_protocol import Address, Digest

HEAD = 100

#: One method's behaviour: given the request's params, the HTTP status and the body to send.
Answer = Callable[[list[Any]], tuple[int, bytes]]


def _block(number: int) -> dict[str, Any]:
    return {
        "number": hex(number),
        "hash": "0x" + f"{number:064x}",
        "parentHash": "0x" + f"{max(number - 1, 0):064x}",
        "timestamp": hex(1_700_000_000 + number),
        "baseFeePerGas": hex(1),
        "transactions": [],
        "gasLimit": hex(30_000_000),
        "gasUsed": "0x0",
        "miner": "0x" + "00" * 20,
        "difficulty": "0x0",
        "extraData": "0x",
        "logsBloom": "0x" + "00" * 256,
        "nonce": "0x" + "00" * 8,
        "receiptsRoot": "0x" + "00" * 32,
        "sha3Uncles": "0x" + "00" * 32,
        "size": "0x0",
        "stateRoot": "0x" + "00" * 32,
        "totalDifficulty": "0x0",
        "transactionsRoot": "0x" + "00" * 32,
        "uncles": [],
    }


class FakeRpc:
    """Answers per method: a callable taking the params and returning (status, body)."""

    def __init__(self) -> None:
        self.answers: dict[str, Answer] = {}
        self.requests: list[str] = []

    def answer(self, method: str, params: list[Any]) -> tuple[int, bytes]:
        handler = self.answers.get(method)
        if handler is None:
            return 200, json.dumps(
                {"jsonrpc": "2.0", "id": 0, "error": {"code": -32601, "message": "no"}}
            ).encode()
        return handler(params)


def result(value: Any) -> Answer:
    return lambda params: (200, json.dumps({"jsonrpc": "2.0", "id": 0, "result": value}).encode())


def rpc_error(code: int, message: str) -> Answer:
    body = {"jsonrpc": "2.0", "id": 0, "error": {"code": code, "message": message}}
    return lambda params: (200, json.dumps(body).encode())


def blocks(missing: int | None = None, error_at: int | None = None) -> Answer:
    def answer(params: list[Any]) -> tuple[int, bytes]:
        tag = params[0]
        number = HEAD if tag in ("latest", "finalized") else int(tag, 16)
        if number == error_at:
            return rpc_error(-32000, "header not found")(params)
        if number > HEAD or number == missing:
            return result(None)(params)
        return result(_block(number))(params)

    return answer


@pytest.fixture
def rpc() -> Iterator[tuple[FakeRpc, str]]:
    fake = FakeRpc()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            fake.requests.append(body["method"])
            status, payload = fake.answer(body["method"], body.get("params", []))
            payload = payload.replace(b'"id": 0', f'"id": {json.dumps(body["id"])}'.encode())
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield fake, f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


async def test_a_block_is_none_only_above_the_head(rpc: tuple[FakeRpc, str]) -> None:
    fake, url = rpc
    fake.answers["eth_getBlockByNumber"] = blocks(missing=5)
    adapter = Web3ChainAdapter(url, timeout_s=2)
    block = await adapter.block(7)
    assert block is not None
    assert block.number == 7
    assert await adapter.block(HEAD + 1) is None
    # A null answer for a height the chain has is a lagging node, not a missing block.
    with pytest.raises(RpcUnavailableError, match="did not return it"):
        await adapter.block(5)


async def test_a_json_rpc_error_on_a_read_is_unreachable_never_none(
    rpc: tuple[FakeRpc, str],
) -> None:
    fake, url = rpc
    fake.answers["eth_getBlockByNumber"] = blocks(error_at=6)
    fake.answers["eth_getLogs"] = rpc_error(-32600, "query exceeds max block range 10")
    adapter = Web3ChainAdapter(url, timeout_s=2)
    with pytest.raises(RpcUnavailableError, match="header not found"):
        await adapter.block(6)
    with pytest.raises(RpcUnavailableError, match="max block range"):
        await adapter.logs(Address("0x" + "11" * 20), 0, 5)


async def test_an_unparseable_or_failing_answer_is_unreachable(rpc: tuple[FakeRpc, str]) -> None:
    fake, url = rpc
    fake.answers["eth_getBlockByNumber"] = lambda params: (200, b"<html>bad gateway</html>")
    fake.answers["eth_getTransactionCount"] = lambda params: (503, b"unavailable")
    adapter = Web3ChainAdapter(url, timeout_s=2)
    with pytest.raises(RpcUnavailableError):
        await adapter.head()
    with pytest.raises(RpcUnavailableError):
        await adapter.pending_nonce(Address("0x" + "22" * 20))


async def test_web3_does_not_retry_underneath_the_relay(rpc: tuple[FakeRpc, str]) -> None:
    """ADR-060: one call, one request, so RPC_TIMEOUT_S means what it says."""
    fake, url = rpc
    fake.answers["eth_blockNumber"] = lambda params: (503, b"unavailable")
    adapter = Web3ChainAdapter(url, timeout_s=2)
    with pytest.raises(RpcUnavailableError):
        await adapter._run(lambda: adapter._w3.eth.block_number)
    assert fake.requests.count("eth_blockNumber") == 1


async def test_a_refused_transaction_is_refused_and_a_rate_limit_is_not(
    rpc: tuple[FakeRpc, str],
) -> None:
    fake, url = rpc
    adapter = Web3ChainAdapter(url, timeout_s=2)
    fake.answers["eth_sendRawTransaction"] = rpc_error(-32000, "insufficient funds for gas")
    with pytest.raises(TransactionRejectedError, match="insufficient funds"):
        await adapter.send_raw(b"\x02\xc0")
    fake.answers["eth_sendRawTransaction"] = rpc_error(
        429, "Your app has exceeded its compute units per second capacity"
    )
    with pytest.raises(RpcUnavailableError, match="exceeded"):
        await adapter.send_raw(b"\x02\xc0")


async def test_an_unknown_transaction_or_receipt_is_none(rpc: tuple[FakeRpc, str]) -> None:
    fake, url = rpc
    fake.answers["eth_getTransactionReceipt"] = result(None)
    fake.answers["eth_getTransactionByHash"] = result(None)
    adapter = Web3ChainAdapter(url, timeout_s=2)
    assert await adapter.receipt(Digest("0x" + "ab" * 32)) is None
    assert await adapter.transaction(Digest("0x" + "ab" * 32)) is None
