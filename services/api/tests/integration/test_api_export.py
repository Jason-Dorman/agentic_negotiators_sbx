"""The evidence export through the API (api_contract section 5), and acceptance A15 at run level.

The export of a real settled run, taken over HTTP, validates against `export.v1.json` with
`private: null`; the reconstruction tool, reading nothing but the chain, agrees with it action by
action, on the outcome and on the balances. A private export validates too, carries what it should,
and is refused without the reveal header. The metrics route reports the same figures.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from api_chain import AnvilChain
from api_controller import ControllerHarness
from api_http import ApiHarness
from reconstruct import Reconstructor

from api.db import Database
from negotiation_protocol import validate


@pytest.fixture
async def api(database: Database, chain: AnvilChain) -> AsyncIterator[ApiHarness]:
    harness = ApiHarness(ControllerHarness(database, chain))
    yield harness
    await harness.aclose()


async def _settled(api: ApiHarness) -> str:
    run_id = await api.validated()
    await api.client.post(f"/v1/runs/{run_id}/start")
    await api.settle()
    return run_id


def _hex(value: str) -> str:
    value = value.lower()
    return value if value.startswith("0x") else "0x" + value


async def test_a15_the_export_validates_and_the_chain_alone_agrees_with_it(
    api: ApiHarness, chain: AnvilChain
) -> None:
    run_id = await _settled(api)
    response = await api.client.get(f"/v1/runs/{run_id}/export")
    assert response.status_code == 200
    assert response.headers["Content-Disposition"] == (
        f'attachment; filename="run-{run_id}.export.json"'
    )
    export: dict[str, Any] = response.json()
    validate(export, "export.v1.json")
    assert export["private"] is None and export["includes_private"] is False

    session_id = export["run"]["session"]["session_id"]
    reconstruction = Reconstructor(chain.w3, chain.manifest).run(session_id)
    assert reconstruction.ok, reconstruction.as_dict()
    document = reconstruction.document

    # Every authorised action: the same sequence, kind, digest, signer and transaction.
    exported = [
        (a["sequence"], a["kind"], a["digest"].lower(), a["signer"], _hex(a["tx_hash"]))
        for a in export["signed_actions"]
    ]
    rebuilt = [
        (a["sequence"], a["kind"], a["digest"].lower(), a["recovered_signer"], _hex(a["tx_hash"]))
        for a in document["actions"]
    ]
    assert exported == rebuilt
    for action, chain_action in zip(export["signed_actions"], document["actions"], strict=True):
        assert {k: str(v) for k, v in action["typed_message"].items()} == {
            k: str(v) for k, v in chain_action["typed_message"].items()
        }

    # The same outcome, and the same terminal transaction.
    assert document["outcome"]["kind"] == export["run"]["outcome"]["kind"] == "settled"
    terminal = [e for e in export["chain_events"] if e["event"] == "SettlementCompleted"]
    assert _hex(document["outcome"]["tx_hash"]) == terminal[0]["tx_hash"].lower()

    # The same balance movement: the export's settlement snapshots against the chain's deltas.
    def held(stage: str, party: str, token: str) -> int:
        (row,) = [
            s
            for s in export["balance_snapshots"]
            if (s["stage"], s["party"], s["token"]) == (stage, party, token)
        ]
        return int(row["amount_minor"])

    for party in ("buyer", "seller"):
        for token in ("base", "quote"):
            delta = held("post_settlement", party, token) - held("pre_settlement", party, token)
            assert str(delta) == document["balances"]["deltas"][party][token]

    # Every event the chain has for the session is in the export, canonical. The tool reports the
    # opening apart from the rest, under `config`.
    chain_events = {(_hex(e["tx_hash"]), e["log_index"]) for e in document["events"]}
    canonical = {
        (e["tx_hash"].lower(), e["log_index"]): e["event"]
        for e in export["chain_events"]
        if e["canonical"]
    }
    opened = [key for key, name in canonical.items() if name == "SessionOpened"]
    assert [tx for tx, _ in opened] == [_hex(document["config"]["opened_tx_hash"])]
    assert chain_events == set(canonical) - set(opened)

    # The calldata of each action, as the chain has it.
    by_tx = {c["tx_hash"].lower(): c for c in export["calldata"]}
    for action in document["actions"]:
        assert by_tx[_hex(action["tx_hash"])]["decoded_function"] == action["function"]

    # The metrics reconcile, and the run's RPC requests were counted and priced (Anvil: free).
    metrics = export["metrics"]
    assert metrics["audit_complete"] is True
    assert metrics["mandate_violations"] == 0
    assert metrics["failure_class"] == "none"
    assert metrics["settled_quote_minor"] == "93333333"
    assert metrics["rpc_requests"] == sum(metrics["rpc_requests_by_method"].values()) > 0
    assert "eth_getLogs" in metrics["rpc_requests_by_method"]
    assert metrics["rpc_cost_estimated_usd"] == "0.000000"
    assert export["reproducibility"]["policy_versions"] == {
        "buyer": "det-1.0.0",
        "seller": "det-1.0.0",
    }


async def test_the_default_export_carries_no_private_value(api: ApiHarness) -> None:
    run_id = await _settled(api)
    text = (await api.client.get(f"/v1/runs/{run_id}/export")).text
    for secret in ("Pay as little", "Obtain as much", "below your", "mandate_version_id"):
        assert secret not in text
    document = json.loads(text)
    numbers = _numbers(document)
    assert "100000000" not in numbers and "90000000" not in numbers


def _numbers(value: object) -> set[str]:
    if isinstance(value, dict):
        return set().union(*(_numbers(item) for item in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(_numbers(item) for item in value)) if value else set()
    return {str(value)}


async def test_a_private_export_validates_and_carries_the_private_inputs(api: ApiHarness) -> None:
    run_id = await _settled(api)
    refused = await api.client.get(f"/v1/runs/{run_id}/export", params={"include_private": True})
    assert refused.status_code == 403
    response = await api.client.get(
        f"/v1/runs/{run_id}/export",
        params={"include_private": True},
        headers={"X-Observer-Reveal": "true"},
    )
    export = response.json()
    validate(export, "export.v1.json")
    private = export["private"]
    assert export["includes_private"] is True
    assert private["mandates"]["seller"]["reservation_price_minor"] == "90000000"
    assert private["evaluator"]["feasible_interval_minor"] == ["90000000", "100000000"]
    assert len(private["observations"]) == len({d["turn"] for d in private["decisions"]}) == 6
    assert all(
        d["label"] == "private_operational_record_not_an_authorized_offer"
        for d in private["decisions"]
    )
    # Each decision names the observation it was decided on, as the route does.
    decided = (
        await api.client.get(f"/v1/runs/{run_id}/decisions", headers={"X-Observer-Reveal": "true"})
    ).json()["decisions"]
    hashes = {(o["turn"], o["party"]): o["observation_hash"] for o in private["observations"]}
    assert [d["observation_hash"] for d in decided] == [
        hashes[(d["turn"], d["party"])] for d in decided
    ]
    assert all(h.startswith("0x") for h in hashes.values())
    # The observation each agent was sent holds its own mandate and only its own.
    for observation in private["observations"]:
        own = private["mandates"][observation["party"]]["reservation_price_minor"]
        mandate = observation["observation"]["mandate"]
        assert mandate["reservation_price_minor"] == own


async def test_the_metrics_route_hides_the_utilities_without_the_reveal_header(
    api: ApiHarness,
) -> None:
    run_id = await _settled(api)
    public = (await api.client.get(f"/v1/runs/{run_id}/metrics")).json()
    assert public["settled_quote_minor"] == "93333333"
    assert public["rpc_price_last_verified"] == "2026-10-02"
    for field in (
        "buyer_utility_minor",
        "seller_utility_minor",
        "captured_surplus_minor",
        "feasible",
        "feasible_surplus_minor",
        "efficiency_ratio",
    ):
        assert public[field] is None, field
    revealed = (
        await api.client.get(f"/v1/runs/{run_id}/metrics", headers={"X-Observer-Reveal": "true"})
    ).json()
    assert (revealed["buyer_utility_minor"], revealed["seller_utility_minor"]) == (
        "6666667",
        "3333333",
    )
    assert revealed["captured_surplus_minor"] == "10000000"
    assert (revealed["feasible"], revealed["feasible_surplus_minor"]) == (True, "10000000")
    assert revealed["efficiency_ratio"] == "1.000000"
    unknown = await api.client.get(f"/v1/runs/{uuid.uuid4()}/metrics")
    assert unknown.status_code == 404
