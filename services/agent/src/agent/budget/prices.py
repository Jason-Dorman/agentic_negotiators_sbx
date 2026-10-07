"""The operator-maintained model price table (FR-A9, docs/architecture.md section 7).

The provider reports what a call used, in tokens, and never what it cost. A model call's cost is
therefore always the usage priced from this table: before the call, a conservative bound on the
request — the *estimate* — and after it, the usage the provider reported — the *reported* cost
(ADR-085). The two are kept apart on every decision record and never substituted for each other.

A model is priced per million tokens of each kind it bills: uncached input, output, cache reads, and
cache writes at the two cache durations, because a 1-hour write costs more than a 5-minute one and
the provider's usage says which was written. A model the table does not list has no price, which is
unknown and never zero (FR-U5); `BudgetGuard` refuses to call it unless the operator allows unknown
prices.

The packaged table, `model_prices.json` beside this module, lists the models a run may name today,
priced from Anthropic's pricing page on the date each entry gives. `AGENT_MODEL_PRICE_TABLE` points
at an operator's own copy. Its shape is the RPC price table's (ADR-075): a `table_version`, and per
entry a `description`, a `source` and a `last_verified` date, so a stale price is visible as stale.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

#: The packaged table. An operator's own is named by `AGENT_MODEL_PRICE_TABLE`.
DEFAULT_MODEL_PRICE_TABLE: Final = Path(__file__).resolve().parent / "model_prices.json"
#: `decisions.cost_estimated_usd` and `cost_reported_usd` are NUMERIC(12,6).
_USD_PLACES: Final = Decimal("0.000001")
_MILLION: Final = Decimal(1_000_000)

_Decimal = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")]


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """What one call used, as the provider reported it.

    `input_tokens` is the uncached remainder only; the prompt's size is the sum of the three input
    counts, which is how the metrics count it (ADR-079, Q68). `cache_creation_1h_input_tokens` is
    the part of `cache_creation_input_tokens` written to the 1-hour cache: it prices the call and
    is not part of the decision record, whose usage has the four counts `export.v1.json` admits.
    """

    input_tokens: int
    output_tokens: int
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_creation_1h_input_tokens: int = 0

    def as_record(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
        }


class _Rates(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    input: _Decimal
    output: _Decimal
    cache_write_5m: _Decimal
    cache_write_1h: _Decimal
    cache_read: _Decimal


class _ModelDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    description: str
    source: str
    last_verified: date = Field(strict=False)
    usd_per_million_tokens: _Rates


class _TableDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    table_version: str = Field(pattern=r"^1$")
    models: dict[str, _ModelDocument]


class PriceTableError(ValueError):
    """The price table file cannot be read as a price table."""


def _usd(tokens_times_rate: Decimal) -> Decimal:
    """Per-million rates applied, rounded up to the micro-dollar: an estimate never undercounts."""
    return (tokens_times_rate / _MILLION).quantize(_USD_PLACES, rounding=ROUND_CEILING)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """One model's rates, in USD per million tokens."""

    model_id: str
    last_verified: date
    input: Decimal
    output: Decimal
    cache_write_5m: Decimal
    cache_write_1h: Decimal
    cache_read: Decimal

    def bound_usd(self, input_tokens: int, max_tokens: int) -> Decimal:
        """The most a request of `input_tokens` capped at `max_tokens` can cost.

        Every input token is priced at the dearest rate an input token can be billed at — a
        1-hour cache write — because the count cannot say which tokens the cache will write or
        read; and the output at `max_tokens`, thinking included, which is the most the provider
        will generate.
        """
        dearest_input = max(self.input, self.cache_write_5m, self.cache_write_1h, self.cache_read)
        return _usd(input_tokens * dearest_input + max_tokens * self.output)

    def cost_usd(self, usage: TokenUsage) -> Decimal:
        """The cost of what the provider reported, each kind of token at its own rate."""
        written_5m = usage.cache_creation_input_tokens - usage.cache_creation_1h_input_tokens
        return _usd(
            usage.input_tokens * self.input
            + usage.output_tokens * self.output
            + usage.cache_read_input_tokens * self.cache_read
            + written_5m * self.cache_write_5m
            + usage.cache_creation_1h_input_tokens * self.cache_write_1h
        )


@dataclass(frozen=True, slots=True)
class ModelPriceTable:
    models: Mapping[str, ModelPrice]

    def price(self, model_id: str) -> ModelPrice | None:
        """None — unknown, never zero — when the table does not list the model."""
        return self.models.get(model_id)

    @classmethod
    def from_document(cls, document: Any) -> ModelPriceTable:
        try:
            parsed = _TableDocument.model_validate(document)
        except ValidationError as error:
            locations = sorted(
                ".".join(str(part) for part in item["loc"]) or "<root>"
                for item in error.errors(include_input=False, include_url=False)
            )
            raise PriceTableError(f"invalid model price table at {', '.join(locations)}") from None
        return cls(
            {
                model_id: ModelPrice(
                    model_id=model_id,
                    last_verified=entry.last_verified,
                    input=Decimal(entry.usd_per_million_tokens.input),
                    output=Decimal(entry.usd_per_million_tokens.output),
                    cache_write_5m=Decimal(entry.usd_per_million_tokens.cache_write_5m),
                    cache_write_1h=Decimal(entry.usd_per_million_tokens.cache_write_1h),
                    cache_read=Decimal(entry.usd_per_million_tokens.cache_read),
                )
                for model_id, entry in parsed.models.items()
            }
        )

    @classmethod
    def load(cls, path: Path = DEFAULT_MODEL_PRICE_TABLE) -> ModelPriceTable:
        try:
            document = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_unique)
        except (OSError, ValueError) as error:
            raise PriceTableError(f"cannot read the model price table {path}: {error}") from None
        return cls.from_document(document)


def _unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """A JSON object, refusing a key given twice: `json` would keep the last price silently."""
    document: dict[str, Any] = {}
    for key, value in pairs:
        if key in document:
            raise ValueError(f"the key {key!r} appears twice")
        document[key] = value
    return document
