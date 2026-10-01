"""Request bodies of the agent internal API, as Pydantic models (docs/api_contract.md section 6).

`extra="forbid"` and strict parsing at every level (docs/contributing.md section 2.1): an unknown
field, a float where an integer belongs, or a number where an amount string belongs is refused
rather than coerced. Strictness also keeps the canonical-JSON fingerprint total — it refuses floats,
and after strict validation none can remain.

The turn body is not modelled here. It is the observation of docs/protocol.md section 12 less its
`mandate`, and `observation.v1.json` is its normative form; the service validates it against that
schema directly, so there is one statement of the allowlist rather than two that could drift.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from agent.keys.references import check_key_reference
from negotiation_protocol import UINT64_MAX, Address, Digest, MinorAmount, SessionId

#: Every chain integer the agent passes into a struct or a transaction is a `uint64` there; bounding
#: it here makes an oversized one a 422 rather than an exception at signing time.
Uint64 = Annotated[int, Field(ge=0, le=UINT64_MAX)]
#: EIP-2681: a transaction nonce is below 2**64 - 1.
Nonce = Annotated[int, Field(ge=0, le=UINT64_MAX - 1)]


class _Body(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Limits(_Body):
    model_call_ceiling: Annotated[int, Field(ge=0)]
    model_spend_ceiling_usd: Annotated[str, Field(pattern=r"^[0-9]+(\.[0-9]+)?$")]
    model_timeout_s: Annotated[int, Field(gt=0)]
    repair_attempts: Annotated[int, Field(ge=0)]


class ExpectedSessionBody(_Body):
    chain_id: Annotated[int, Field(ge=1, le=UINT64_MAX)]
    exchange_address: Address
    base_token: Address
    quote_token: Address
    base_amount_minor: MinorAmount
    max_offers: Annotated[int, Field(ge=1, le=32)]
    session_duration_s: Annotated[int, Field(gt=0, le=UINT64_MAX)]
    offer_lifetime_s: Annotated[int, Field(gt=0, le=UINT64_MAX)]

    @model_validator(mode="after")
    def _a_session_this_protocol_can_open(self) -> Self:
        if self.base_amount_minor == 0:
            raise ValueError("base_amount_minor must be positive (docs/protocol.md section 3)")
        if self.base_token == self.quote_token:
            raise ValueError("base_token and quote_token must differ (ADR-037)")
        return self


class Balances(_Body):
    base_minor: MinorAmount
    quote_minor: MinorAmount


class ProvisionBody(_Body):
    role: Literal["buyer", "seller"]
    policy: Annotated[str, Field(min_length=1)]
    model_id: str | None
    effort: str | None
    limits: Limits
    expected_session: ExpectedSessionBody
    key_ref: Annotated[str, AfterValidator(check_key_reference)]
    expected_address: Address | None
    mandate_version_id: UUID
    mandate: dict[str, Any]
    initial_balances: Balances
    allowance_minor: MinorAmount


class ApproveSessionBody(_Body):
    """The decoded `SessionOpened` fields and its block's timestamp (ADR-044)."""

    session_id: SessionId
    buyer: Address
    seller: Address
    base_token: Address
    quote_token: Address
    base_amount_minor: MinorAmount
    expires_at_ts: Uint64
    max_offers: Annotated[int, Field(ge=1, le=32)]
    config_hash: Digest
    opened_at_ts: Uint64


class SetupApprovalBody(_Body):
    nonce: Nonce
    gas_limit: Annotated[int, Field(gt=0)]
    max_fee_per_gas_wei: MinorAmount
    max_priority_fee_per_gas_wei: MinorAmount
