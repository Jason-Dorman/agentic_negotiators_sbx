"""The operator-maintained RPC price table (ADR-061, docs/architecture.md section 8).

No RPC provider reports what a request cost, so a run's RPC cost is always an estimate: the request
counts the chain adapter recorded, priced from this table. The table prices a provider in units —
Alchemy's compute units, say — with a price per million units and a unit cost per JSON-RPC method,
because that is how the providers bill; a provider that bills per request is one unit a request.

Unknown is never zero (FR-U5). A provider the table does not list, or a method it lists no units
for when the provider has no default, prices the whole run at None, which the interface shows as
unknown. Anvil, which really is free, is listed at zero.

The packaged table, `rpc_prices.json` beside this module, lists Anvil alone until stage 5 prices
Sepolia's provider against its current unit table (Q56). `RPC_PRICE_TABLE` points at an operator's
own copy. Each provider carries its own `last_verified` date, which the interface shows beside the
estimate, so a stale price is visible as stale.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from decimal import ROUND_CEILING, Decimal
from pathlib import Path
from typing import Annotated, Any, Final

from pydantic import BaseModel, ConfigDict, Field, ValidationError

#: The packaged table. An operator's own is named by `RPC_PRICE_TABLE`.
DEFAULT_RPC_PRICE_TABLE: Final = Path(__file__).resolve().parent / "rpc_prices.json"
#: `run_metrics.rpc_cost_estimated_usd` is NUMERIC(12,6).
_USD_PLACES: Final = Decimal("0.000001")
_MILLION: Final = Decimal(1_000_000)

_Decimal = Annotated[str, Field(pattern=r"^(0|[1-9][0-9]*)(\.[0-9]+)?$")]


class _ProviderDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    description: str
    source: str
    last_verified: date = Field(strict=False)
    usd_per_million_units: _Decimal
    units_by_method: dict[str, Annotated[int, Field(ge=0)]]
    #: Units for a method not listed; null makes an unlisted method's price unknown.
    default_units: Annotated[int, Field(ge=0)] | None


class _TableDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    table_version: str = Field(pattern=r"^1$")
    providers: dict[str, _ProviderDocument]


class PriceTableError(ValueError):
    """The price table file cannot be read as a price table."""


@dataclass(frozen=True, slots=True)
class ProviderPrice:
    name: str
    last_verified: date
    usd_per_million_units: Decimal
    units_by_method: Mapping[str, int] = field(default_factory=dict)
    default_units: int | None = None

    def units(self, method: str) -> int | None:
        return self.units_by_method.get(method, self.default_units)

    def cost_usd(self, by_method: Mapping[str, int]) -> Decimal | None:
        """The estimate for these counts, rounded up to the micro-dollar; None if any method's
        price is unknown."""
        total_units = 0
        for method, count in by_method.items():
            units = self.units(method)
            if units is None:
                return None
            total_units += units * count
        usd = Decimal(total_units) * self.usd_per_million_units / _MILLION
        return usd.quantize(_USD_PLACES, rounding=ROUND_CEILING)


@dataclass(frozen=True, slots=True)
class RpcPriceTable:
    providers: Mapping[str, ProviderPrice]

    def provider(self, name: str) -> ProviderPrice | None:
        return self.providers.get(name)

    def cost_usd(self, provider: str, by_method: Mapping[str, int]) -> Decimal | None:
        """None — unknown, never zero — when the provider is not in the table."""
        price = self.provider(provider)
        return None if price is None else price.cost_usd(by_method)

    @classmethod
    def from_document(cls, document: Any) -> RpcPriceTable:
        try:
            parsed = _TableDocument.model_validate(document)
        except ValidationError as error:
            locations = sorted(
                ".".join(str(part) for part in item["loc"]) or "<root>"
                for item in error.errors(include_input=False, include_url=False)
            )
            raise PriceTableError(f"invalid RPC price table at {', '.join(locations)}") from None
        return cls(
            {
                name: ProviderPrice(
                    name=name,
                    last_verified=entry.last_verified,
                    usd_per_million_units=Decimal(entry.usd_per_million_units),
                    units_by_method=dict(entry.units_by_method),
                    default_units=entry.default_units,
                )
                for name, entry in parsed.providers.items()
            }
        )

    @classmethod
    def load(cls, path: Path = DEFAULT_RPC_PRICE_TABLE) -> RpcPriceTable:
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise PriceTableError(f"cannot read the RPC price table {path}: {error}") from None
        return cls.from_document(document)
