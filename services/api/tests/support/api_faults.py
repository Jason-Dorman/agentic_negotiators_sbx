"""Chain adapters that fail at an exact point, for the recovery tests (A13).

Each subclasses the real adapter and overrides one method, so everything else is the real RPC and a
test can say precisely where the process "died". The death is a `BaseException`, like the
`KeyboardInterrupt` or task cancellation a real crash would look like from inside, so no handler in
the code under test can catch it by accident and carry on.
"""

from __future__ import annotations

import asyncio

from api.chain import (
    BlockRef,
    CallRequest,
    Receipt,
    RpcUnavailableError,
    TransactionRejectedError,
    Web3ChainAdapter,
)
from negotiation_protocol import Digest


class ProcessKilledError(BaseException):
    """The process stopped here. Nothing after this point ran."""


class CountingAdapter(Web3ChainAdapter):
    """The real adapter, counting raw transactions handed to the node."""

    def __init__(self, rpc_url: str) -> None:
        super().__init__(rpc_url)
        self.sends = 0

    async def send_raw(self, raw_tx: bytes) -> Digest:
        self.sends += 1
        return await super().send_raw(raw_tx)


class DiesAfterSend(CountingAdapter):
    """The node accepts the transaction, and the process dies before it records that (A13)."""

    async def send_raw(self, raw_tx: bytes) -> Digest:
        await super().send_raw(raw_tx)
        raise ProcessKilledError("killed between send_raw_transaction and receipt persistence")


class DiesBeforeSend(CountingAdapter):
    """The transaction is persisted, and the process dies before it reaches the node."""

    async def send_raw(self, raw_tx: bytes) -> Digest:
        raise ProcessKilledError("killed after persisting and before broadcasting")


class MeetsAtEstimate(Web3ChainAdapter):
    """Holds every caller at the gas estimate until `parties` callers have arrived.

    Two relays sharing one of these both get past "is there a live transaction?" before either
    persists one, so their inserts genuinely race and the database decides (A06).
    """

    def __init__(self, rpc_url: str, parties: int) -> None:
        super().__init__(rpc_url)
        self._barrier = asyncio.Barrier(parties)
        #: How many submitters reached the estimate: two is the evidence the race happened, since
        #: a submitter that found the other's row would resume it without estimating.
        self.arrivals = 0

    async def estimate_gas(self, request: CallRequest) -> int:
        self.arrivals += 1
        await self._barrier.wait()
        return await super().estimate_gas(request)


class UnreachableOnSend(CountingAdapter):
    """The RPC times out on the broadcast itself: nothing is known about whether it landed."""

    async def send_raw(self, raw_tx: bytes) -> Digest:
        self.sends += 1
        raise RpcUnavailableError("ReadTimeout: the RPC did not answer")


class Unreachable(Web3ChainAdapter):
    """Every receipt lookup times out: recovery can learn nothing."""

    async def receipt(self, tx_hash: Digest) -> Receipt | None:
        raise RpcUnavailableError("ConnectionError: the RPC did not answer")


class RefusesSend(CountingAdapter):
    """The node reads the bytes and refuses them, as it does a relay out of gas money."""

    async def send_raw(self, raw_tx: bytes) -> Digest:
        self.sends += 1
        raise TransactionRejectedError("insufficient funds for gas * price + value")


class FailsBlockOnce(Web3ChainAdapter):
    """One `eth_getBlockByNumber` for a stored height goes unanswered, as from a lagging node."""

    def __init__(self, rpc_url: str, height: int) -> None:
        super().__init__(rpc_url)
        self.height = height
        self.failed = False

    async def block(self, number: int) -> BlockRef | None:
        if number == self.height and not self.failed:
            self.failed = True
            raise RpcUnavailableError("header not found")
        return await super().block(number)


class HeadOneBehind(Web3ChainAdapter):
    """The head the poll reads is one block old: a transaction is mined just after it is read,
    as Anvil's automine and a busy Sepolia both allow. What the poll then finds in that newer block
    is beyond the head it is describing."""

    def __init__(self, rpc_url: str) -> None:
        super().__init__(rpc_url)
        self.armed = True

    async def head(self) -> BlockRef:
        head = await super().head()
        if not self.armed:
            return head
        self.armed = False
        older = await super().block(head.number - 1)
        assert older is not None
        return older
