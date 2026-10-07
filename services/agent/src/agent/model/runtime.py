"""`ModelRuntime`: what an instance needs to give each model run its own client (ADR-088).

Built once, in the composition root, from the instance's own configuration: live, with the
provider's SDK client and the key its reference names, or fixture, with its role's canned script.
Each provisioned model run then gets a fresh client from it, with the run's model, effort and
timeout and a `BudgetGuard` over the run's own ceilings, so no two runs share a budget or a script
position.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from agent.budget import BudgetGuard, BudgetLimits, ModelPriceTable
from agent.model.client import ModelCallConfig, ModelClient

ModelMode = Literal["live", "fixture"]
ClientMaker = Callable[[ModelCallConfig, BudgetGuard], ModelClient]


@dataclass(frozen=True, slots=True)
class RunModelLimits:
    """The run's `limits`, as provisioning delivers them."""

    call_ceiling: int
    spend_ceiling_usd: Decimal
    timeout_s: int


@dataclass(frozen=True, slots=True)
class ModelRuntime:
    mode: ModelMode
    prices: ModelPriceTable
    allow_unknown_price: bool
    max_tokens: int
    make: ClientMaker = field(repr=False)

    def client_for(self, model_id: str, effort: str, limits: RunModelLimits) -> ModelClient:
        guard = BudgetGuard(
            BudgetLimits(limits.call_ceiling, limits.spend_ceiling_usd),
            self.prices.price(model_id),
            allow_unknown_price=self.allow_unknown_price,
        )
        config = ModelCallConfig(model_id, effort, self.max_tokens, float(limits.timeout_s))
        return self.make(config, guard)


__all__ = ["ClientMaker", "ModelMode", "ModelRuntime", "RunModelLimits"]
