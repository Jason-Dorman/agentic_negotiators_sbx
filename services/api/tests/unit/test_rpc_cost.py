"""ADR-061: RPC requests counted by method and by run, and priced from the operator's table.

The adapter is run against a real HTTP JSON-RPC server, so what is counted is what crossed the wire:
one adapter call that needs two requests counts two, a request that failed still counts, and a
request made in a worker thread is still the run's. The price table is checked against worked
figures, and unknown is never zero.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest

from api.chain import RpcCounter, RpcUnavailableError, Web3ChainAdapter
from api.config import DEFAULT_RPC_PRICE_TABLE, PriceTableError, RpcPriceTable
from negotiation_protocol import Address


@pytest.fixture
def rpc_url() -> Iterator[str]:
    """Answers `eth_chainId` and `eth_maxPriorityFeePerGas`; anything else is a JSON-RPC error."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            answers: dict[str, dict[str, Any]] = {
                "eth_chainId": {"result": "0x7a69"},
                "eth_maxPriorityFeePerGas": {"result": "0x1"},
                "eth_getBlockByNumber": {
                    "result": {
                        "number": "0x1",
                        "hash": "0x" + "11" * 32,
                        "parentHash": "0x" + "00" * 32,
                        "timestamp": "0x1",
                        "baseFeePerGas": "0x7",
                    }
                },
            }
            reply = answers.get(body["method"], {"error": {"code": -32601, "message": "no"}})
            payload = json.dumps({"jsonrpc": "2.0", "id": body["id"], **reply}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


async def test_requests_are_counted_by_method_and_attributed_to_the_run(rpc_url: str) -> None:
    counter = RpcCounter()
    adapter = Web3ChainAdapter(rpc_url, timeout_s=2, counter=counter)
    run_id, other = uuid.uuid4(), uuid.uuid4()

    with counter.attributed_to(run_id):
        assert await adapter.chain_id() == 31337
        quote = await adapter.fee_quote()  # two requests: the latest block, then the priority fee
        assert quote.max_priority_fee_per_gas == 1
        with pytest.raises(RpcUnavailableError):
            # Refused by the server, and still a request.
            await adapter.pending_nonce(Address("0x" + "11" * 20))
    await adapter.chain_id()  # outside any run: belongs to no run

    assert counter.drain(run_id) == {
        "eth_chainId": 1,
        "eth_getBlockByNumber": 1,
        "eth_maxPriorityFeePerGas": 1,
        "eth_getTransactionCount": 1,
    }
    assert counter.drain(run_id) == {}, "a drain forgets what it returned"
    assert counter.drain(other) == {}
    assert counter.unattributed() == {"eth_chainId": 1}


async def test_attribution_follows_each_task_and_its_worker_threads(rpc_url: str) -> None:
    counter = RpcCounter()
    adapter = Web3ChainAdapter(rpc_url, timeout_s=2, counter=counter)
    first, second = uuid.uuid4(), uuid.uuid4()

    async def drive(run_id: uuid.UUID, times: int) -> None:
        with counter.attributed_to(run_id):
            for _ in range(times):
                await adapter.chain_id()
                await asyncio.sleep(0)

    await asyncio.gather(drive(first, 3), drive(second, 2))
    assert counter.drain(first) == {"eth_chainId": 3}
    assert counter.drain(second) == {"eth_chainId": 2}
    assert RpcCounter.current() is None


async def test_an_adapter_without_a_counter_counts_nothing(rpc_url: str) -> None:
    counter = RpcCounter()
    adapter = Web3ChainAdapter(rpc_url, timeout_s=2)
    with counter.attributed_to(uuid.uuid4()):
        await adapter.chain_id()
    assert counter.unattributed() == {}


# ---------------------------------------------------------------------------------------------
# The price table
# ---------------------------------------------------------------------------------------------


def _table(**provider: Any) -> RpcPriceTable:
    entry = {
        "description": "test",
        "source": "test",
        "last_verified": "2026-10-02",
        "usd_per_million_units": "0.525",
        "units_by_method": {"eth_getLogs": 75, "eth_blockNumber": 10},
        "default_units": None,
        **provider,
    }
    return RpcPriceTable.from_document({"table_version": "1", "providers": {"hosted": entry}})


def test_a_cost_is_units_times_the_price_rounded_up_to_the_micro_dollar() -> None:
    table = _table()
    # 3 x 75 + 2 x 10 = 245 units at $0.525 per million = $0.000128625, rounded up.
    assert table.cost_usd("hosted", {"eth_getLogs": 3, "eth_blockNumber": 2}) == Decimal("0.000129")
    assert table.cost_usd("hosted", {}) == Decimal("0.000000")


def test_unknown_is_never_zero() -> None:
    table = _table()
    assert table.cost_usd("hosted", {"eth_getLogs": 1, "eth_call": 1}) is None
    assert table.cost_usd("elsewhere", {"eth_getLogs": 1}) is None
    with_default = _table(default_units=26)
    assert with_default.cost_usd("hosted", {"eth_call": 2}) == Decimal("0.000028")


def test_the_packaged_table_prices_anvil_at_zero_and_nothing_else() -> None:
    table = RpcPriceTable.load(DEFAULT_RPC_PRICE_TABLE)
    assert set(table.providers) == {"anvil"}
    assert table.cost_usd("anvil", {"eth_getLogs": 1_000, "eth_call": 7}) == Decimal("0.000000")
    anvil = table.provider("anvil")
    assert anvil is not None
    assert anvil.last_verified == date(2026, 10, 2)


def test_a_malformed_table_names_the_location_and_quotes_nothing() -> None:
    with pytest.raises(PriceTableError) as refused:
        _table(usd_per_million_units="0.5e3", units_by_method={"eth_call": -1})
    message = str(refused.value)
    assert "providers.hosted.usd_per_million_units" in message
    assert "providers.hosted.units_by_method.eth_call" in message
    assert "0.5e3" not in message
