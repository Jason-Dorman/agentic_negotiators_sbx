"""The body of `POST /v1/runs` (api_contract section 2.2), parsed strictly.

The route of stage 2.5 hands this to `RunController.create_run`; the controller and its tests
build it directly. Every amount is parsed through `MinorAmount`, which matches the whole string,
so an amount with a trailing newline — which a `jsonschema` pattern admits — is refused
(docs/contributing.md section 3). Every free-text field refuses a NUL character, which PostgreSQL
cannot store, so the refusal is the request's `422` and not the database's `500` (Q69). A refusal
names the field and the rule, never the value: a mandate is in this body.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal, Self

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

from api.controller.errors import RunRequestError
from negotiation_protocol import MinorAmount


def _amount(value: str) -> str:
    MinorAmount.parse(value)
    return value


def _positive(value: str) -> str:
    if MinorAmount.parse(value) == 0:
        raise ValueError("must be positive")
    return value


def _text(value: str) -> str:
    if "\x00" in value:
        raise ValueError("must not contain a NUL character")
    return value


Amount = Annotated[str, AfterValidator(_amount)]
PositiveAmount = Annotated[str, AfterValidator(_positive)]
Text = Annotated[str, AfterValidator(_text)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class Balances(_Strict):
    base_minor: Amount
    quote_minor: Amount


class Mandate(_Strict):
    """**Private.** Stored as `mandate_versions` and sent to its own agent once."""

    reservation_price_minor: Amount
    min_remaining_inventory_minor: Amount
    instructions: Annotated[Text, Field(max_length=4000)]


class PartyConfig(_Strict):
    policy: Literal["model", "deterministic"]
    model_id: Text | None = None
    effort: Text | None = None
    initial_balances: Balances
    allowance_minor: Amount
    mandate: Mandate

    @model_validator(mode="after")
    def _model_named(self) -> Self:
        if self.policy == "model" and not self.model_id:
            raise ValueError("model_id is required for a model policy")
        return self


class PublicConfig(_Strict):
    base_amount_minor: PositiveAmount
    max_offers: Annotated[int, Field(ge=1, le=32)]
    session_duration_s: Annotated[int, Field(gt=0)]
    offer_lifetime_s: Annotated[int, Field(gt=0)]
    first_proposer: Literal["buyer"]
    confirmation_threshold: Annotated[int, Field(ge=1)] | None = None
    token_decimals: Literal[6] = 6


class Limits(_Strict):
    model_call_ceiling: Annotated[int, Field(ge=0)]
    model_spend_ceiling_usd: Annotated[str, Field(pattern=r"^[0-9]+(\.[0-9]+)?$")]
    model_timeout_s: Annotated[int, Field(gt=0)]
    repair_attempts: Annotated[int, Field(ge=0)]


class RunRequest(_Strict):
    name: Annotated[Text, Field(min_length=1, max_length=200)]
    scenario_id: Text | None = None
    deployment_id: Text
    public_config: PublicConfig
    buyer: PartyConfig
    seller: PartyConfig
    limits: Limits
    #: A JSON string; the one field whose Python type JSON cannot carry as such.
    parent_run_id: Annotated[uuid.UUID, Field(strict=False)] | None = None

    @classmethod
    def parse(cls, document: Any) -> RunRequest:
        """From JSON-shaped data, as a route receives it, strictly: a threshold of `"2"` or `true`,
        a number where an amount string belongs, or a float where an integer belongs is refused
        (ADR-059). Refusals name locations, not values."""
        try:
            return cls.model_validate(document)
        except ValidationError as error:
            fields = {
                ".".join(str(part) for part in item["loc"]) or "<root>": item["type"]
                for item in error.errors(include_input=False, include_url=False)
            }
            raise RunRequestError("the run request is invalid", {"fields": fields}) from None

    def public_config_document(self) -> dict[str, Any]:
        """`runs.public_config`. A threshold left out is the deployment's (Q14, ADR-059)."""
        return self.public_config.model_dump(mode="json", exclude_none=True)
