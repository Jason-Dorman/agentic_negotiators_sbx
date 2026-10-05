"""The web3 adapter: blocking calls behind a thread executor, ABI loading, event and custom-error
decoding. Depends only on the protocol package (`.importlinter`, `chain-is-pure`).

Built in stage 2.3 of docs/build_plan.md; see docs/architecture.md section 3.2. The relay and the
indexer reach the chain through `ChainAdapter` and decode through `ExchangeCodec`, and never import
web3 themselves.
"""

from api.chain.adapter import ChainAdapter, Web3ChainAdapter
from api.chain.codec import (
    EXCHANGE_EVENTS,
    SIGNED_ACTION_FUNCTIONS,
    CodecError,
    DecodedCall,
    DecodedEvent,
    DecodedRevert,
    ExchangeCodec,
    Transfer,
)
from api.chain.counting import RpcCounter
from api.chain.errors import (
    ChainError,
    ExecutionRevertedError,
    RpcUnavailableError,
    TransactionRejectedError,
)
from api.chain.types import BlockRef, CallRequest, FeeQuote, RawLog, Receipt, TransactionView

__all__ = [
    "EXCHANGE_EVENTS",
    "SIGNED_ACTION_FUNCTIONS",
    "BlockRef",
    "CallRequest",
    "ChainAdapter",
    "ChainError",
    "CodecError",
    "DecodedCall",
    "DecodedEvent",
    "DecodedRevert",
    "ExchangeCodec",
    "ExecutionRevertedError",
    "FeeQuote",
    "RawLog",
    "Receipt",
    "RpcCounter",
    "RpcUnavailableError",
    "TransactionRejectedError",
    "TransactionView",
    "Transfer",
    "Web3ChainAdapter",
]
