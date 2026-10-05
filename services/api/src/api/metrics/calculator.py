"""`MetricsCalculator`: one run's `run_metrics` row, recomputed from the database on demand.

The row is recomputed after every turn and when the run ends — the controller is handed `record`
for that, through its `RunMetricsSink` protocol — and whenever the metrics route is asked. Nothing
is incremental except the RPC request counts, which only the controller adds to as it drives the
run (`RunMetricsRepository.add_rpc_requests`); a recomputation carries them through and prices them
(ADR-061).

`summary`, `public` and `private` are the three shapes the figures leave the server in: the run
resource's metric strip, the export's `metrics` object, and the utilities and feasibility that need
both mandates and so only travel with the observer reveal header (data model section 7).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from api.config import RpcPriceTable, confirmation_threshold
from api.db.enums import RunState
from api.db.errors import NotFoundError
from api.db.protocols import Transactions
from api.db.records import RunMetricsRecord
from api.metrics import figures
from negotiation_protocol import MinorAmount

_USD_PLACES: Final = Decimal("0.000001")


def usd(value: Decimal | None) -> str | None:
    """A USD figure as the API writes one: six places, or null for unknown — never zero."""
    return None if value is None else f"{value.quantize(_USD_PLACES):f}"


class MetricsCalculator:
    def __init__(
        self,
        transactions: Transactions,
        prices: RpcPriceTable,
        *,
        rpc_provider: str,
        default_threshold: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._transactions = transactions
        self._prices = prices
        self._provider = rpc_provider
        self._default_threshold = default_threshold
        self._clock = clock

    @property
    def rpc_price_verified(self) -> str | None:
        """The `last_verified` date of this deployment's RPC price, shown beside the estimate."""
        price = self._prices.provider(self._provider)
        return None if price is None else price.last_verified.isoformat()

    async def compute(self, run_id: uuid.UUID) -> RunMetricsRecord:
        async with self._transactions.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
            if run is None:
                raise NotFoundError(f"no run {run_id}")
            mandates = await uow.mandates.get_both(run_id)
            wallets = {wallet.party: wallet for wallet in await uow.wallets.list_for_run(run_id)}
            actions = await uow.signed_actions.list_for_run(run_id)
            events = await uow.chain_events.canonical_for_run(run_id)
            snapshots = await uow.balances.canonical_for_run(run_id)
            outbox = await uow.outbox.list_for_run(run_id)
            decisions = await uow.decisions.list_for_run(run_id)
            existing = await uow.metrics.get(run_id)

        base_amount = int(run.public_config["base_amount_minor"])
        complete = len(mandates) == 2 and len(wallets) == 2
        settled = figures.settled_quote(run, events)
        negotiation = figures.chain_cost(outbox, figures.NEGOTIATION_TXS)
        setup = figures.chain_cost(outbox, figures.SETUP_TXS)
        model = figures.model_cost(decisions)
        counts = {} if existing is None else dict(existing.rpc_requests_by_method)
        feasibility = figures.feasibility(mandates, wallets, base_amount) if complete else None
        buyer_utility, seller_utility = (
            figures.utilities(mandates, wallets, base_amount, settled) if complete else (None, None)
        )
        # A run that ended — on chain, or in a setup that failed — without a trade captured 0.
        finished = run.state in (RunState.TERMINAL, RunState.FAILED_SETUP)
        return RunMetricsRecord(
            run_id=run_id,
            computed_at=self._clock(),
            recorded_offers=sum(1 for e in events if e.event_name == "OfferRecorded"),
            decision_time_ms=model.decision_time_ms,
            chain_wait_ms=negotiation.wait_ms,
            setup_chain_wait_ms=setup.wait_ms,
            model_calls=model.calls,
            input_tokens=model.input_tokens,
            output_tokens=model.output_tokens,
            model_cost_estimated_usd=model.estimated_usd,
            model_cost_reported_usd=model.reported_usd,
            gas_used_negotiation=_amount(negotiation.gas_used),
            gas_used_setup=_amount(setup.gas_used),
            fee_wei_negotiation=_amount(negotiation.fee_wei),
            fee_wei_setup=_amount(setup.fee_wei),
            settled_quote_minor=None if settled is None else _amount(settled),
            buyer_utility_minor=buyer_utility,
            seller_utility_minor=seller_utility,
            # Spec 11.2: the sum of the two utilities, and zero for a run that ended with no trade.
            captured_surplus_minor=(
                buyer_utility + seller_utility
                if buyer_utility is not None and seller_utility is not None
                else (0 if finished else None)
            ),
            feasible=None if feasibility is None else feasibility.feasible,
            feasible_surplus_minor=None if feasibility is None else feasibility.surplus,
            mandate_violations=(
                figures.mandate_violations(actions, mandates, wallets, base_amount)
                if complete
                else 0
            ),
            failure_class=figures.failure_class(run),
            audit_complete=figures.audit_complete(
                run,
                actions,
                events,
                snapshots,
                confirmation_threshold(run.public_config, self._default_threshold),
            ),
            rpc_requests=sum(counts.values()),
            rpc_requests_by_method=counts,
            rpc_cost_estimated_usd=self._prices.cost_usd(self._provider, counts),
        )

    async def refresh(self, run_id: uuid.UUID) -> RunMetricsRecord:
        """Recompute and store the row; the stored row is returned, with the RPC counts as they
        stand now."""
        computed = await self.compute(run_id)
        async with self._transactions.unit_of_work() as uow:
            await uow.metrics.upsert(computed)
            stored = await uow.metrics.get(run_id)
        return stored or computed

    async def record(self, run_id: uuid.UUID) -> None:
        """After a turn and when the run ends: refresh the row and stream the run's metric strip as
        a `metrics` run event (api_contract section 3)."""
        stored = await self.refresh(run_id)
        async with self._transactions.unit_of_work() as uow:
            await uow.run_events.append(run_id, "metrics", self.summary(stored))

    async def evaluator(self, run_id: uuid.UUID) -> dict[str, Any]:
        """**Private.** The offline evaluator's view of the run's two mandates (api_contract 2.2):
        never shown on a setup route, never to an agent."""
        async with self._transactions.unit_of_work() as uow:
            run = await uow.runs.get(run_id)
            if run is None:
                raise NotFoundError(f"no run {run_id}")
            mandates = await uow.mandates.get_both(run_id)
            wallets = {wallet.party: wallet for wallet in await uow.wallets.list_for_run(run_id)}
        if len(mandates) != 2 or len(wallets) != 2:
            return {
                "feasible": None,
                "feasible_interval_minor": None,
                "feasible_surplus_minor": None,
            }
        result = figures.feasibility(mandates, wallets, int(run.public_config["base_amount_minor"]))
        return {
            "feasible": result.feasible,
            "feasible_interval_minor": (
                None if result.interval is None else [str(value) for value in result.interval]
            ),
            "feasible_surplus_minor": None if result.surplus is None else str(result.surplus),
        }

    # -----------------------------------------------------------------------------------------
    # Shapes
    # -----------------------------------------------------------------------------------------

    @staticmethod
    def summary(record: RunMetricsRecord | None) -> dict[str, Any]:
        """The run resource's metric strip (api_contract section 2.2, FR-U5). Gas and fee include
        setup's; the metrics route and the export keep the two apart."""
        record = record or _empty()
        return {
            "recorded_offers": record.recorded_offers,
            "decision_time_ms": record.decision_time_ms,
            "chain_wait_ms": record.chain_wait_ms,
            "model_calls": record.model_calls,
            "model_cost_estimated_usd": usd(record.model_cost_estimated_usd),
            "model_cost_reported_usd": usd(record.model_cost_reported_usd),
            "gas_used": int(record.gas_used_negotiation) + int(record.gas_used_setup),
            "fee_wei": str(int(record.fee_wei_negotiation) + int(record.fee_wei_setup)),
            "rpc_requests": record.rpc_requests,
            "rpc_cost_estimated_usd": usd(record.rpc_cost_estimated_usd),
        }

    @staticmethod
    def public(record: RunMetricsRecord | None) -> dict[str, Any]:
        """The export's `metrics` object: every public column of `run_metrics`."""
        record = record or _empty()
        return {
            "recorded_offers": record.recorded_offers,
            "decision_time_ms": record.decision_time_ms,
            "chain_wait_ms": record.chain_wait_ms,
            "setup_chain_wait_ms": record.setup_chain_wait_ms,
            "model_calls": record.model_calls,
            "input_tokens": record.input_tokens,
            "output_tokens": record.output_tokens,
            "model_cost_estimated_usd": usd(record.model_cost_estimated_usd),
            "model_cost_reported_usd": usd(record.model_cost_reported_usd),
            "gas_used_negotiation": str(int(record.gas_used_negotiation)),
            "gas_used_setup": str(int(record.gas_used_setup)),
            "fee_wei_negotiation": str(int(record.fee_wei_negotiation)),
            "fee_wei_setup": str(int(record.fee_wei_setup)),
            "settled_quote_minor": _optional(record.settled_quote_minor),
            "mandate_violations": record.mandate_violations,
            "failure_class": record.failure_class,
            "audit_complete": record.audit_complete,
            "rpc_requests": record.rpc_requests,
            "rpc_requests_by_method": dict(sorted(record.rpc_requests_by_method.items())),
            "rpc_cost_estimated_usd": usd(record.rpc_cost_estimated_usd),
        }

    @staticmethod
    def private(record: RunMetricsRecord | None) -> dict[str, Any]:
        """**Private.** The figures that need both mandates; null where there is none yet."""
        record = record or _empty()
        surplus = record.feasible_surplus_minor
        captured = record.captured_surplus_minor
        ratio = None
        if surplus and captured is not None:
            # Spec 11.2: undefined when the feasible surplus is zero, and so null.
            ratio = f"{(Decimal(captured) / Decimal(surplus)).quantize(_USD_PLACES):f}"
        return {
            "buyer_utility_minor": _optional(record.buyer_utility_minor),
            "seller_utility_minor": _optional(record.seller_utility_minor),
            "captured_surplus_minor": _optional(captured),
            "feasible": record.feasible,
            "feasible_surplus_minor": _optional(surplus),
            "efficiency_ratio": ratio,
        }


def _amount(value: int) -> MinorAmount:
    return MinorAmount(value)


def _optional(value: int | None) -> str | None:
    return None if value is None else str(int(value))


def _empty() -> RunMetricsRecord:
    return RunMetricsRecord(run_id=uuid.UUID(int=0), computed_at=datetime.now(UTC))
