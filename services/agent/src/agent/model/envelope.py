"""The decision envelope as a Pydantic model, for structured outputs (ADR-014).

`messages.parse` turns this model into the JSON schema the provider constrains the response to, so
it restates `agent_decision.v1.json`: one of three decision shapes, told apart by `action`, and an
optional `explanation`. It is the *requested* shape and has no authority. What the model wrote is
kept as written and checked by the `MandateValidator`, structurally and then economically, exactly
as a deterministic policy's response is (ADR-006); `test_model_client.py` checks that this model
and the schema agree on every response the validator's table holds.

The provider cannot enforce every constraint here: string patterns and lengths are among those its
structured outputs do not support, and the SDK moves them into descriptions and checks them on the
client. A response that breaks one is therefore possible, and is the client's `unparseable`, not a
decision. Two things are written out by hand so that the provider *can* enforce them: each
`action` is a one-value `enum`, because the SDK demotes a `const` to a description; and
`explanation` may be left out but is never `null`, which the decision schema refuses.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, WithJsonSchema

from agent.validation.structure import EXPLANATION_MAX_CHARS


class _Shape(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


def _action(name: str) -> WithJsonSchema:
    return WithJsonSchema({"type": "string", "enum": [name]})


def _no_default(schema: dict[str, Any]) -> None:
    """The Python default is how a field is left out; the provider does not need to see it."""
    schema.pop("default", None)


class OfferDecision(_Shape):
    action: Annotated[Literal["offer"], _action("offer")]
    quote_amount_minor: Annotated[
        str,
        Field(
            pattern=r"^[1-9][0-9]{0,77}$",
            description="Positive integer in minor units, as a base-10 string.",
        ),
    ]


class AcceptDecision(_Shape):
    action: Annotated[Literal["accept"], _action("accept")]
    offer_hash: Annotated[
        str,
        Field(
            pattern=r"^0x[0-9a-fA-F]{64}$",
            description="The active offer's digest, exactly as the observation gives it.",
        ),
    ]


class WalkAwayDecision(_Shape):
    action: Annotated[Literal["walk_away"], _action("walk_away")]
    reason: Literal["terms_unacceptable", "inventory_constraint", "no_further_concession"]


class DecisionEnvelope(_Shape):
    decision: OfferDecision | AcceptDecision | WalkAwayDecision
    explanation: Annotated[
        str,
        Field(max_length=EXPLANATION_MAX_CHARS, json_schema_extra=_no_default),
        WithJsonSchema(
            {
                "type": "string",
                "description": "Optional. Your own short account of this decision, for the "
                f"operator only, at most {EXPLANATION_MAX_CHARS} characters.",
            }
        ),
    ] = ""
