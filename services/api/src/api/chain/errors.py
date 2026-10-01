"""The chain adapter's failures, in terms the relay and the indexer act on.

Three kinds, because each calls for a different response and spec section 9.4 keeps them apart:

- `RpcUnavailableError`: the node could not be reached or did not answer. Nothing is known about
  the request; a broadcast may or may not have landed, so the relay looks the transaction up rather
  than assuming either (A13).
- `TransactionRejectedError`: the node answered and refused a raw transaction — a nonce already
  used, a transaction it already holds, a replacement priced too low. It carries the node's own
  message, which is about the transaction and holds no secret.
- `ExecutionRevertedError`: an `eth_call` or gas estimate ran and reverted. It carries the revert
  data, which `ExchangeCodec.decode_revert` turns into a protocol error name (ADR-017).
"""

from __future__ import annotations


class ChainError(Exception):
    """Base of every failure the chain adapter raises."""


class RpcUnavailableError(ChainError):
    """The RPC endpoint could not be reached or timed out."""


class TransactionRejectedError(ChainError):
    """The node refused a raw transaction. `reason` is the node's message."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    @property
    def nonce_too_low(self) -> bool:
        """The nonce is already used — by this very transaction, or by another one."""
        return "nonce too low" in self.reason.lower()

    @property
    def already_known(self) -> bool:
        """The node already holds this exact transaction in its pool."""
        reason = self.reason.lower()
        return "already known" in reason or "already imported" in reason


class ExecutionRevertedError(ChainError):
    """A call or a gas estimate reverted. `data` is the raw revert data, possibly empty."""

    def __init__(self, data: bytes) -> None:
        super().__init__(f"execution reverted ({len(data)} bytes of revert data)")
        self.data = data
