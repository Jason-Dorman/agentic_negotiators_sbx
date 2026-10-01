"""What the chain adapter hands upward: frozen values, never web3 objects.

A web3 `AttributeDict` is a loosely typed mapping whose byte fields arrive as `HexBytes`, and
passing one upward would spread web3's types into the relay and the indexer — which `.importlinter`
lets depend on `api.chain` precisely so that they need not depend on web3. Every field here is a
value object from the protocol package or a plain `int` or `bytes`, so the layers above are written
and tested against this module alone.
"""

from __future__ import annotations

from dataclasses import dataclass

from negotiation_protocol import Address, Digest


@dataclass(frozen=True, slots=True)
class BlockRef:
    """A block as the reorg check needs it: where it is, what it is, and when."""

    number: int
    hash: Digest
    parent_hash: Digest
    timestamp: int


@dataclass(frozen=True, slots=True)
class RawLog:
    """One log, undecoded, with its position in the chain."""

    address: Address
    topics: tuple[bytes, ...]
    data: bytes
    block_number: int
    block_hash: Digest
    tx_hash: Digest
    log_index: int


@dataclass(frozen=True, slots=True)
class Receipt:
    """A transaction's receipt. `succeeded` is the receipt status: false means it reverted."""

    tx_hash: Digest
    succeeded: bool
    block_number: int
    block_hash: Digest
    gas_used: int
    effective_gas_price: int
    sender: Address
    to: Address | None
    logs: tuple[RawLog, ...]


@dataclass(frozen=True, slots=True)
class TransactionView:
    """A transaction the node knows, mined or still pending (`block_number` is then None)."""

    tx_hash: Digest
    sender: Address
    to: Address | None
    input: bytes
    nonce: int
    value: int
    gas: int
    block_number: int | None


@dataclass(frozen=True, slots=True)
class FeeQuote:
    """What the node suggests: the latest base fee and a priority fee."""

    base_fee_per_gas: int
    max_priority_fee_per_gas: int


@dataclass(frozen=True, slots=True)
class CallRequest:
    """An `eth_call` or `eth_estimateGas` request."""

    sender: Address
    to: Address
    data: bytes
    value: int = 0
    gas: int | None = None
