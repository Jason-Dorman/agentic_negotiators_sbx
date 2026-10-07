"""`ModelRuntime`: what an instance needs to give each model run its own client (ADR-088).

Built once, in the composition root, from the instance's own configuration: live, with the
provider's SDK client and the key its reference names, or fixture, with its role's canned script.
Each provisioned model run then gets a fresh client from it, with the run's model, effort and
timeout and a `BudgetGuard` over the run's own ceilings, so no two runs share a budget or a script
position — and behind the instance's `OutboundCheck`, with the run's own key added to it, so that no
request carrying anything private to the instance leaves (ADR-092).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from agent.budget import BudgetGuard, BudgetLimits, ModelPriceTable
from agent.model.checked import CheckedModelClient, RequestObserver
from agent.model.client import ModelCallConfig, ModelClient
from agent.outbound import KeyProbe, OutboundCheck

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
    #: The instance's denylist; each run's client adds its own key to it.
    check: OutboundCheck = field(default_factory=OutboundCheck, repr=False)
    #: Sees each request that passed the check. The isolation suite's, None in service.
    observer: RequestObserver | None = field(default=None, repr=False)

    def client_for(
        self, model_id: str, effort: str, limits: RunModelLimits, run_key: KeyProbe
    ) -> ModelClient:
        guard = BudgetGuard(
            BudgetLimits(limits.call_ceiling, limits.spend_ceiling_usd),
            self.prices.price(model_id),
            allow_unknown_price=self.allow_unknown_price,
        )
        config = ModelCallConfig(model_id, effort, self.max_tokens, float(limits.timeout_s))
        check = self.check.for_run(run_key)
        return CheckedModelClient(self.make(config, guard), config, check, self.observer)


__all__ = ["ClientMaker", "ModelMode", "ModelRuntime", "RunModelLimits"]
