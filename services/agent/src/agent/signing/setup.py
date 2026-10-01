"""The participant's one setup transaction: an ERC-20 `approve` of the exchange (ADR-040).

Built entirely from provisioned state. The token is this role's — the quote token for the buyer,
who pays, and the base token for the seller, who delivers — the spender is the provisioned
exchange, the amount is the provisioned allowance, the chain is the provisioned chain. The caller
supplies only what it must know about the chain: the nonce, the gas limit and the fee caps. So the
rule that governs typed messages governs this transaction too: the agent never signs anything whose
target, calldata or value came from its caller.

Two bounds, so the caller cannot make the agent sign a transaction that burns its wallet's ETH: the
gas limit (ADR-042, `AGENT_SETUP_GAS_LIMIT_MAX`, default 100,000; an OpenZeppelin `approve` costs
about 46,000 the first time) and the worst-case cost, gas limit times fee cap (ADR-047,
`AGENT_SETUP_MAX_COST_WEI`, default 10^16 wei: 0.01 ETH, or 100 gwei at the full gas bound). The
priority fee may not exceed the fee cap, which no EIP-1559 transaction can carry anyway, and the
nonce must fit the `uint64` a transaction carries.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

from eth_abi.abi import encode as abi_encode
from eth_utils.crypto import keccak

from agent.errors import RequestValidationError
from agent.keys import RunSigner
from agent.observation import Role
from agent.signing.session import ExpectedSession
from negotiation_protocol import Address, Digest, MinorAmount

APPROVE_SELECTOR: Final = keccak(text="approve(address,uint256)")[:4]


#: ADR-042 and ADR-047.
DEFAULT_SETUP_GAS_LIMIT_MAX: Final = 100_000
DEFAULT_SETUP_MAX_COST_WEI: Final = 10**16


@dataclass(frozen=True, slots=True)
class SetupBounds:
    gas_limit_max: int
    max_cost_wei: int


@dataclass(frozen=True, slots=True)
class FeeTerms:
    nonce: int
    gas_limit: int
    max_fee_per_gas: int
    max_priority_fee_per_gas: int


@dataclass(frozen=True, slots=True)
class SetupApproval:
    raw_tx: str
    tx_hash: Digest
    sender: Address
    token: Address
    spender: Address
    amount: MinorAmount
    nonce: int

    def to_json(self) -> dict[str, Any]:
        return {
            "raw_tx": self.raw_tx,
            "tx_hash": str(self.tx_hash),
            "from": str(self.sender),
            "token": str(self.token),
            "spender": str(self.spender),
            "amount_minor": self.amount.to_json(),
            "nonce": self.nonce,
        }


def build_setup_approval(
    *,
    role: Role,
    expected: ExpectedSession,
    allowance: MinorAmount,
    terms: FeeTerms,
    bounds: SetupBounds,
    signer: RunSigner,
) -> SetupApproval:
    _check_terms(terms, bounds)
    token = expected.quote_token if role == "buyer" else expected.base_token
    calldata = APPROVE_SELECTOR + abi_encode(
        ["address", "uint256"], [expected.exchange_address, int(allowance)]
    )
    signed = signer.sign_transaction(
        {
            "type": 2,
            "chainId": expected.chain_id,
            "nonce": terms.nonce,
            "to": str(token),
            "value": 0,
            "data": "0x" + calldata.hex(),
            "gas": terms.gas_limit,
            "maxFeePerGas": terms.max_fee_per_gas,
            "maxPriorityFeePerGas": terms.max_priority_fee_per_gas,
            "accessList": [],
        }
    )
    return SetupApproval(
        raw_tx="0x" + signed.raw_transaction.hex(),
        tx_hash=signed.tx_hash,
        sender=signer.address,
        token=token,
        spender=expected.exchange_address,
        amount=allowance,
        nonce=terms.nonce,
    )


def _check_terms(terms: FeeTerms, bounds: SetupBounds) -> None:
    if terms.gas_limit > bounds.gas_limit_max:
        raise RequestValidationError(
            "the gas limit is above this agent's bound",
            {"gas_limit": f"at most {bounds.gas_limit_max}"},
        )
    if terms.gas_limit * terms.max_fee_per_gas > bounds.max_cost_wei:
        raise RequestValidationError(
            "the transaction could cost more than this agent's bound",
            {"max_fee_per_gas_wei": f"times gas_limit, at most {bounds.max_cost_wei} wei"},
        )
    if terms.max_priority_fee_per_gas > terms.max_fee_per_gas:
        raise RequestValidationError(
            "the priority fee is above the fee cap",
            {"max_priority_fee_per_gas_wei": "must not exceed max_fee_per_gas_wei"},
        )
