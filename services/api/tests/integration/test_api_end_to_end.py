"""The stage 2 exit condition through the HTTP API (stage 2.5): A02 and A03 end to end.

Everything a client does goes through the operator API — create, validate, start, read — served by
the real FastAPI application over the real controller, relay, indexer and projection, PostgreSQL,
Anvil and two real agent applications. The run is driven by the controller's background driver,
exactly as in a process, and the `start_run` operation records how it ended.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from api_chain import AnvilChain
from api_controller import ControllerHarness, table_counts
from api_http import ApiHarness

from api.db import Database


@pytest.fixture
async def api(database: Database, chain: AnvilChain) -> AsyncIterator[ApiHarness]:
    harness = ApiHarness(ControllerHarness(database, chain))
    yield harness
    await harness.aclose()


async def _evidence_in_every_table(api: ApiHarness, run_id: str) -> None:
    import uuid

    counts = dict(await table_counts(api.database, uuid.UUID(run_id)))
    async with api.database.unit_of_work() as uow:
        counts["run_metrics"] = int(await uow.metrics.get(uuid.UUID(run_id)) is not None)
    assert all(count > 0 for count in counts.values()), counts


async def test_a02_the_deterministic_pair_settles_through_the_api(api: ApiHarness) -> None:
    run_id = await api.validated()
    response = await api.client.post(f"/v1/runs/{run_id}/start")
    assert response.status_code == 202, response.text
    operation = response.json()
    assert response.headers["Location"] == f"/v1/operations/{operation['operation_id']}"
    assert (operation["kind"], operation["status"]) == ("start_run", "running")

    await api.settle()

    run = await api.run(run_id)
    assert (run["state"], run["outcome"]["kind"], run["outcome"]["actor"]) == (
        "terminal",
        "settled",
        "seller",
    )
    offers = [entry["quote_amount_minor"] for entry in run["timeline"] if entry["kind"] == "offer"]
    assert offers == ["80000000", "108000000", "86666666", "102000000", "93333333"]
    assert [entry["kind"] for entry in run["timeline"]][-2:] == ["accept", "settle"]
    assert run["timeline"][-1]["sentence"] == (
        "Settled: 10 mASSET to buyer, 93.333333 mUSD to seller."
    )
    assert run["session"]["status"] == "settled"
    assert run["parties"]["buyer"]["balances"] == {
        "base_minor": "10000000",
        "quote_minor": "156666667",
    }
    assert run["labels"] == {
        "test_assets": True,
        "simulated_economics": True,
        "fixture": False,
        "operator_abort_power": True,
    }
    # The metric strip, recomputed when the run ended: five offers, gas paid, no model.
    assert run["metrics"]["recorded_offers"] == 5
    assert run["metrics"]["model_calls"] == 0
    assert run["metrics"]["gas_used"] > 0
    assert run["metrics"]["rpc_requests"] > 0
    assert run["metrics"]["rpc_cost_estimated_usd"] == "0.000000"

    # The start operation ended when the run was running; the run going on to settle is the
    # driver's, not the operation's.
    finished = await api.operation(operation["operation_id"])
    assert finished["status"] == "succeeded"
    assert finished["result"]["state"] == "running"

    await _evidence_in_every_table(api, run_id)
    await _metrics_are_recorded_and_wired(api, run_id, turns=6, offers=5)


async def _metrics_are_recorded_and_wired(
    api: ApiHarness, run_id: str, *, turns: int, offers: int
) -> None:
    """Recomputed after each turn and when the run ended (data_model 3.13), and each figure from
    its own source: the outbox's two kinds of transaction, the decisions' latencies."""
    import uuid

    from api.metrics.figures import NEGOTIATION_TXS, SETUP_TXS

    rid = uuid.UUID(run_id)
    async with api.database.unit_of_work() as uow:
        events = await uow.run_events.after(rid, 0, limit=10_000)
        stored = await uow.metrics.get(rid)
        outbox = await uow.outbox.list_for_run(rid)
        decisions = await uow.decisions.list_for_run(rid)
    strips = [event.data for event in events if event.event_type == "metrics"]
    # One after each turn and one when the run ended — the same one for the last turn, whose
    # action ended the session: the run ends before that turn is seen confirmed.
    assert len(strips) == turns, "one after each turn but the last, and one when the run ended"
    assert stored is not None
    assert strips[-1]["rpc_requests"] == stored.rpc_requests, "streamed as stored"
    assert strips[0]["recorded_offers"] == 1 and strips[-1]["recorded_offers"] == offers

    def gas(kinds: frozenset[object]) -> int:
        return sum(int(r.gas_used or 0) for r in outbox if r.kind in kinds)

    def fee(kinds: frozenset[object]) -> int:
        return sum(
            int(r.gas_used or 0) * int(r.effective_gas_price_wei or 0)
            for r in outbox
            if r.kind in kinds
        )

    assert int(stored.gas_used_setup) == gas(SETUP_TXS) > 0
    assert int(stored.gas_used_negotiation) == gas(NEGOTIATION_TXS) > 0
    assert int(stored.fee_wei_setup) == fee(SETUP_TXS)
    assert int(stored.fee_wei_negotiation) == fee(NEGOTIATION_TXS)
    assert stored.decision_time_ms == sum(d.latency_ms or 0 for d in decisions)
    assert stored.chain_wait_ms > 0 and stored.setup_chain_wait_ms > 0
    assert (stored.input_tokens, stored.output_tokens, stored.model_calls) == (0, 0, 0)
    strip = (await api.run(run_id))["metrics"]
    assert strip["gas_used"] == gas(SETUP_TXS) + gas(NEGOTIATION_TXS)
    assert strip["fee_wei"] == str(fee(SETUP_TXS) + fee(NEGOTIATION_TXS))


async def test_a03_the_infeasible_pair_closes_through_the_api(api: ApiHarness) -> None:
    run_id = await api.validated("infeasible-clone")
    response = await api.client.post(f"/v1/runs/{run_id}/start")
    assert response.status_code == 202, response.text
    await api.settle()

    run = await api.run(run_id)
    assert run["state"] == "terminal"
    assert run["outcome"] == {
        "kind": "closed",
        "reason_code": 1,
        "reason": "terms_unacceptable",
        "actor": "buyer",
    }
    assert run["session"]["offer_count"] == 8
    assert all(entry["kind"] != "settle" for entry in run["timeline"])
    # No token moved: each party ends with what setup gave it.
    assert run["parties"]["buyer"]["balances"] == {"base_minor": "0", "quote_minor": "250000000"}
    assert run["parties"]["seller"]["balances"] == {"base_minor": "25000000", "quote_minor": "0"}
    await _evidence_in_every_table(api, run_id)
    await _metrics_are_recorded_and_wired(api, run_id, turns=9, offers=8)

    # Worked by hand: eight offers and the buyer's close, no trade, infeasible, nothing violated.
    metrics = (
        await api.client.get(f"/v1/runs/{run_id}/metrics", headers={"X-Observer-Reveal": "true"})
    ).json()
    assert metrics["recorded_offers"] == 8
    assert metrics["settled_quote_minor"] is None
    assert (metrics["mandate_violations"], metrics["failure_class"]) == (0, "none")
    assert metrics["audit_complete"] is True
    assert (metrics["buyer_utility_minor"], metrics["seller_utility_minor"]) == (None, None)
    assert (metrics["captured_surplus_minor"], metrics["feasible"]) == ("0", False)
    assert (metrics["feasible_surplus_minor"], metrics["efficiency_ratio"]) == (None, None)


async def test_a_run_whose_setup_failed_captured_nothing(api: ApiHarness) -> None:
    """Spec 11.2: the captured surplus of a run that ended without a trade is zero — a failed
    setup's too, which never reached a terminal event."""
    import uuid

    from api.db import RunState

    created = await api.create()
    rid = uuid.UUID(created["run_id"])
    async with api.database.unit_of_work() as uow:
        await uow.runs.update_state(rid, RunState.FAILED_SETUP, "abort_requested")
    record = await api.services.metrics.compute(rid)
    assert (record.captured_surplus_minor, record.failure_class) == (0, "none")
