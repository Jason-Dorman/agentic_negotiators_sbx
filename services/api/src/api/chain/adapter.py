"""`ChainAdapter`: the one interface through which the backend reads and writes the chain.

docs/architecture.md section 9 names it as a protocol, so the relay and the indexer depend on this
shape and not on web3, and a test can wrap the real adapter to inject a fault at an exact point —
the crash between `send_raw_transaction` and receipt persistence that A13 is about.

`Web3ChainAdapter` is the real one. web3.py's HTTP provider blocks, so every call runs in a worker
thread (`asyncio.to_thread`) and the event loop never waits on the network
(docs/contributing.md section 2.1). Every failure is translated here into one of the three of
`errors.py`, and the stage 2.3 review's lesson is why the translation is exhaustive rather than
hopeful: a JSON-RPC error answered over HTTP 200, a rate limit, an unparseable body and a null
result from a lagging node all used to escape as web3 types — or, for a block, read as "no such
block", which the indexer took for a reorg.

- **Unreachable** (`RpcUnavailableError`): no answer, a transport error, an unparseable answer, a
  JSON-RPC error on a read, a rate limit anywhere, or a block at or below the head that the node did
  not return. Nothing is known; the caller tries again later and never infers a fact from it.
- **Refused** (`TransactionRejectedError`): a JSON-RPC error answering a raw transaction, other
  than a rate limit — the node read the bytes and said no.
- **Reverted** (`ExecutionRevertedError`): a call or estimate that executed and reverted.
- **None** means only what each method says: a block above the head, a transaction or receipt the
  node does not know.

web3's own retries are switched off (ADR-060): the relay and the indexer already recover by looking
up before they act, and a retry layer beneath them made `RPC_TIMEOUT_S` about five times longer than
its name says.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from typing import Any, Final, Protocol, cast

from eth_typing import HexStr
from web3 import Web3
from web3.exceptions import (
    BadResponseFormat,
    BlockNotFound,
    ContractLogicError,
    TransactionNotFound,
    Web3RPCError,
)
from web3.types import TxParams

from api.chain.errors import ExecutionRevertedError, RpcUnavailableError, TransactionRejectedError
from api.chain.types import BlockRef, CallRequest, FeeQuote, RawLog, Receipt, TransactionView
from negotiation_protocol import Address, Digest

#: How hosted RPCs say "slow down": HTTP 429 as a JSON-RPC code, EIP-1474's limit-exceeded code, and
#: the wording Alchemy and Infura use. A rate limit is never a refusal of the transaction itself.
_RATE_LIMIT_CODES: Final = frozenset({429, -32005})
_RATE_LIMIT_WORDS: Final = re.compile(r"rate limit|exceeded|capacity|too many requests", re.I)


class ChainAdapter(Protocol):
    async def chain_id(self) -> int: ...

    async def head(self) -> BlockRef:
        """The latest block."""
        ...

    async def block(self, number: int) -> BlockRef | None:
        """The canonical block at a height, or None above the head."""
        ...

    async def finalized_number(self) -> int:
        """The RPC's finalized head. Spec 9.3: only this may be labelled final."""
        ...

    async def logs(self, address: Address, from_block: int, to_block: int) -> list[RawLog]: ...

    async def receipt(self, tx_hash: Digest) -> Receipt | None: ...

    async def transaction(self, tx_hash: Digest) -> TransactionView | None:
        """A mined or pending transaction the node knows, else None."""
        ...

    async def pending_nonce(self, address: Address) -> int: ...

    async def mined_nonce(self, address: Address) -> int:
        """The nonce after every *mined* transaction: below it, every nonce is consumed."""
        ...

    async def send_raw(self, raw_tx: bytes) -> Digest: ...

    async def fee_quote(self) -> FeeQuote: ...

    async def estimate_gas(self, request: CallRequest) -> int: ...

    async def call(self, request: CallRequest, block_number: int) -> bytes: ...

    async def eth_balance(self, address: Address, block_number: int) -> int: ...


def _digest(value: Any) -> Digest:
    return Digest(bytes(value))


def _optional_address(value: Any) -> Address | None:
    return None if value is None else Address(str(value))


class Web3ChainAdapter(ChainAdapter):
    def __init__(self, rpc_url: str, *, timeout_s: float = 10.0) -> None:
        provider = Web3.HTTPProvider(
            rpc_url, request_kwargs={"timeout": timeout_s}, exception_retry_configuration=None
        )
        self._w3 = Web3(provider)

    async def _run[T](self, function: Callable[..., T], *args: Any) -> T:
        """Run a blocking call in a worker thread; every failure leaves as a `ChainError`.

        The callable handles what its method gives a meaning to — a missing transaction, a revert,
        a refused raw transaction — before anything reaches the handlers below.
        """
        try:
            return await asyncio.to_thread(function, *args)
        except Web3RPCError as error:
            # Includes BlockNotFound and TransactionNotFound not handled by the method itself.
            raise RpcUnavailableError(
                f"the RPC answered with an error: {_rpc_message(error)}"
            ) from None
        except (OSError, json.JSONDecodeError, BadResponseFormat) as error:
            # `requests` raises its connection and timeout errors as `OSError` subclasses; an
            # HTML error page or a truncated body is a decode error. Neither is an answer.
            raise RpcUnavailableError(f"{type(error).__name__}: the RPC did not answer") from None

    async def chain_id(self) -> int:
        return int(await self._run(lambda: self._w3.eth.chain_id))

    @staticmethod
    def _block_ref(block: Any) -> BlockRef:
        return BlockRef(
            number=int(block["number"]),
            hash=_digest(block["hash"]),
            parent_hash=_digest(block["parentHash"]),
            timestamp=int(block["timestamp"]),
        )

    async def head(self) -> BlockRef:
        return self._block_ref(await self._run(self._w3.eth.get_block, "latest"))

    async def block(self, number: int) -> BlockRef | None:
        """None only when the height is above the head. A block at or below the head that the node
        does not return is an unanswered question, never evidence of a reorg."""

        def fetch() -> Any:
            try:
                return self._w3.eth.get_block(number)
            except BlockNotFound:
                return None

        block = await self._run(fetch)
        if block is not None:
            return self._block_ref(block)
        head = await self.head()
        if number > head.number:
            return None
        raise RpcUnavailableError(
            f"block {number} is at or below the head {head.number} and the RPC did not return it"
        )

    async def finalized_number(self) -> int:
        block = await self._run(self._w3.eth.get_block, "finalized")
        return int(block["number"])

    async def logs(self, address: Address, from_block: int, to_block: int) -> list[RawLog]:
        entries = await self._run(
            self._w3.eth.get_logs,
            {"address": address, "fromBlock": from_block, "toBlock": to_block},
        )
        return [self._raw_log(entry) for entry in entries]

    @staticmethod
    def _raw_log(entry: Any) -> RawLog:
        return RawLog(
            address=Address(str(entry["address"])),
            topics=tuple(bytes(topic) for topic in entry["topics"]),
            data=bytes(entry["data"]),
            block_number=int(entry["blockNumber"]),
            block_hash=_digest(entry["blockHash"]),
            tx_hash=_digest(entry["transactionHash"]),
            log_index=int(entry["logIndex"]),
        )

    async def receipt(self, tx_hash: Digest) -> Receipt | None:
        def fetch() -> Any:
            try:
                return self._w3.eth.get_transaction_receipt(HexStr(tx_hash))
            except TransactionNotFound:
                return None

        receipt = await self._run(fetch)
        if receipt is None:
            return None
        return Receipt(
            tx_hash=_digest(receipt["transactionHash"]),
            succeeded=int(receipt["status"]) == 1,
            block_number=int(receipt["blockNumber"]),
            block_hash=_digest(receipt["blockHash"]),
            gas_used=int(receipt["gasUsed"]),
            effective_gas_price=int(receipt["effectiveGasPrice"]),
            sender=Address(str(receipt["from"])),
            to=_optional_address(receipt["to"]),
            logs=tuple(self._raw_log(entry) for entry in receipt["logs"]),
        )

    async def transaction(self, tx_hash: Digest) -> TransactionView | None:
        def fetch() -> Any:
            try:
                return self._w3.eth.get_transaction(HexStr(tx_hash))
            except TransactionNotFound:
                return None

        tx = await self._run(fetch)
        if tx is None:
            return None
        block_number = tx.get("blockNumber")
        return TransactionView(
            tx_hash=_digest(tx["hash"]),
            sender=Address(str(tx["from"])),
            to=_optional_address(tx.get("to")),
            input=bytes(tx["input"]),
            nonce=int(tx["nonce"]),
            value=int(tx["value"]),
            gas=int(tx["gas"]),
            block_number=None if block_number is None else int(block_number),
        )

    async def pending_nonce(self, address: Address) -> int:
        return int(await self._run(self._w3.eth.get_transaction_count, address, "pending"))

    async def mined_nonce(self, address: Address) -> int:
        return int(await self._run(self._w3.eth.get_transaction_count, address, "latest"))

    async def send_raw(self, raw_tx: bytes) -> Digest:
        def send() -> Any:
            try:
                return self._w3.eth.send_raw_transaction(raw_tx)
            except Web3RPCError as error:
                if _rate_limited(error):
                    raise
                raise TransactionRejectedError(_rpc_message(error)) from None

        return _digest(await self._run(send))

    async def fee_quote(self) -> FeeQuote:
        block = await self._run(self._w3.eth.get_block, "latest")
        priority = await self._run(lambda: self._w3.eth.max_priority_fee)
        return FeeQuote(
            base_fee_per_gas=int(block.get("baseFeePerGas", 0)),
            max_priority_fee_per_gas=int(priority),
        )

    @staticmethod
    def _params(request: CallRequest) -> TxParams:
        params: dict[str, Any] = {
            "from": request.sender,
            "to": request.to,
            "data": request.data,
            "value": request.value,
        }
        if request.gas is not None:
            params["gas"] = request.gas
        return cast("TxParams", params)

    @staticmethod
    def _reverting[T](function: Callable[[], T]) -> Callable[[], T]:
        def run() -> T:
            try:
                return function()
            except ContractLogicError as error:
                raise ExecutionRevertedError(_revert_data(error)) from None

        return run

    async def estimate_gas(self, request: CallRequest) -> int:
        estimate = self._reverting(lambda: self._w3.eth.estimate_gas(self._params(request)))
        return int(await self._run(estimate))

    async def call(self, request: CallRequest, block_number: int) -> bytes:
        call = self._reverting(lambda: self._w3.eth.call(self._params(request), block_number))
        return bytes(await self._run(call))

    async def eth_balance(self, address: Address, block_number: int) -> int:
        return int(await self._run(self._w3.eth.get_balance, address, block_number))


def _revert_data(error: ContractLogicError) -> bytes:
    # web3 declares `data` as a string; Anvil and geth send the revert data as `0x`-hex in it.
    data: object = error.data
    if isinstance(data, str) and data.startswith("0x"):
        try:
            return bytes.fromhex(data[2:])
        except ValueError:
            return b""
    return b""


def _rpc_error(error: Web3RPCError) -> dict[str, Any]:
    response = cast("dict[str, Any] | None", getattr(error, "rpc_response", None))
    if response and isinstance(response.get("error"), dict):
        return cast("dict[str, Any]", response["error"])
    return {}


def _rpc_message(error: Web3RPCError) -> str:
    return str(_rpc_error(error).get("message") or error)


def _rate_limited(error: Web3RPCError) -> bool:
    return (
        _rpc_error(error).get("code") in _RATE_LIMIT_CODES
        or _RATE_LIMIT_WORDS.search(_rpc_message(error)) is not None
    )
