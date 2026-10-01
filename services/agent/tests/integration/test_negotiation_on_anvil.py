"""Stage 2.2's exit condition (docs/build_plan.md).

Two agent instances, driven over their internal API by a stand-in for the backend and the relay,
negotiate `default-overlap` and `infeasible-clone` on Anvil through the real contract. The
deterministic pair settles at 93.333333 mUSD; the infeasible pair ends in the buyer's signed Close
with `terms_unacceptable`. Every signature recovers to that run's derived address and none to a
root.

Both runs happen once, in a module fixture, through the same two agent processes; the tests below
each assert one thing about what they did. Running two runs through one pair of processes is itself
part of the claim: each run derives fresh wallets from the same roots (ADR-039).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from agent_harness import CLOSED, ROLES, SETTLED, AgentProcess, Backend, Chain, RunRecord
from anvil_chain import REPO_ROOT
from eth_account import Account
from reconstruct import Reconstructor
from web3.logs import DISCARD

from negotiation_protocol import (
    Accept,
    Close,
    Digest,
    Domain,
    Offer,
    recover_signer,
    to_bytes32,
    validate,
)

SCENARIOS = ("default-overlap", "infeasible-clone")


def scenario(name: str) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(
        (REPO_ROOT / "scenarios" / f"{name}.json").read_text(encoding="utf-8")
    )
    validate(document, "scenario.v1.json")
    return document


EXPIRED = "default-overlap, the buyer's third offer left to expire"


def expire_before_turn_6(turn: int, chain: Chain) -> None:
    """After the buyer's 93.333333, push chain time past its validUntil before the seller acts."""
    if turn == 6:
        chain.advance_time(601)


@pytest.fixture(scope="module")
def runs(chain: Chain, agents: dict[str, AgentProcess]) -> dict[str, RunRecord]:
    backend = Backend(chain, agents)
    recorded = {name: backend.run(scenario(name)) for name in SCENARIOS}
    recorded[EXPIRED] = backend.run(scenario("default-overlap"), before_turn=expire_before_turn_6)
    return recorded


def moves(record: RunRecord) -> list[tuple[str, str, Any]]:
    """Each action as (role, kind, amount or reason), in the order the chain recorded them."""
    summary: list[tuple[str, str, Any]] = []
    for role, action in record.actions:
        message = action["typed_message"]
        detail = {"offer": message.get("quoteAmount"), "close": message.get("reason")}
        summary.append((role, action["kind"], detail.get(action["kind"])))
    return summary


# --------------------------------------------------------------------------------------
# The two outcomes
# --------------------------------------------------------------------------------------


def test_the_deterministic_pair_settles_at_93_333333_musd(
    runs: dict[str, RunRecord], chain: Chain
) -> None:
    record = runs["default-overlap"]
    assert record.final_status == SETTLED
    assert moves(record) == [
        ("buyer", "offer", "80000000"),
        ("seller", "offer", "108000000"),
        ("buyer", "offer", "86666666"),
        ("seller", "offer", "102000000"),
        ("buyer", "offer", "93333333"),
        ("seller", "accept", None),
    ]
    # The receipt also holds AcceptanceRecorded and two ERC-20 Transfer logs; only this is wanted.
    receipt = record.receipts[-1]
    (settlement,) = chain.exchange.events.SettlementCompleted().process_receipt(receipt, DISCARD)
    assert settlement["args"]["quoteAmount"] == 93_333_333
    assert settlement["args"]["baseAmount"] == 10_000_000


def test_the_settlement_moved_exactly_the_signed_amounts(runs: dict[str, RunRecord]) -> None:
    record = runs["default-overlap"]
    before, after = record.balances_before, record.balances_after
    assert after["buyer"]["quote"] - before["buyer"]["quote"] == -93_333_333
    assert after["seller"]["quote"] - before["seller"]["quote"] == 93_333_333
    assert after["buyer"]["base"] - before["buyer"]["base"] == 10_000_000
    assert after["seller"]["base"] - before["seller"]["base"] == -10_000_000


def test_the_infeasible_pair_ends_in_the_buyers_close_with_terms_unacceptable(
    runs: dict[str, RunRecord], chain: Chain
) -> None:
    record = runs["infeasible-clone"]
    assert record.final_status == CLOSED
    assert moves(record) == [
        ("buyer", "offer", "80000000"),
        ("seller", "offer", "126000000"),
        ("buyer", "offer", "86666666"),
        ("seller", "offer", "119000000"),
        ("buyer", "offer", "93333333"),
        ("seller", "offer", "112000000"),
        ("buyer", "offer", "100000000"),
        ("seller", "offer", "105000000"),
        ("buyer", "close", 1),
    ]
    (closed,) = chain.exchange.events.SessionClosed().process_receipt(record.receipts[-1], DISCARD)
    assert closed["args"]["actor"] == record.addresses["buyer"]
    assert closed["args"]["reason"] == 1


def test_the_infeasible_run_moved_no_tokens(runs: dict[str, RunRecord]) -> None:
    record = runs["infeasible-clone"]
    assert record.balances_after == record.balances_before


# --------------------------------------------------------------------------------------
# Authority
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", SCENARIOS)
def test_every_signature_recovers_to_the_runs_derived_address_and_none_to_a_root(
    runs: dict[str, RunRecord],
    agents: dict[str, AgentProcess],
    chain: Chain,
    name: str,
) -> None:
    record = runs[name]
    roots = {agents[role].root_address for role in ROLES}
    for role, action in record.actions:
        signer = recover_signer(Digest(action["digest"]).to_bytes(), action["signature"])
        assert signer == action["signer"] == record.addresses[role]
        assert signer not in roots
        assert signer != chain.relay.address


@pytest.mark.parametrize("name", SCENARIOS)
def test_each_digest_is_the_one_the_contract_computes_for_that_message(
    runs: dict[str, RunRecord], chain: Chain, name: str
) -> None:
    """The typed message is the whole authority: recompute it three ways and compare."""
    domain = Domain(int(chain.manifest["chain_id"]), chain.manifest["exchange_address"])
    functions = chain.exchange.functions
    for _, action in runs[name].actions:
        message = action["typed_message"]
        head = (
            to_bytes32(message["sessionId"]),
            to_bytes32(message["configHash"]),
            message["sequence"],
        )
        if action["kind"] == "offer":
            fields = (message["proposer"], int(message["quoteAmount"]), message["validUntil"])
            ours = Offer(*head, *fields).digest(domain)
            theirs = functions.hashOffer((*head, *fields)).call()
        elif action["kind"] == "accept":
            offer_hash = to_bytes32(message["offerHash"])
            ours = Accept(*head, message["actor"], offer_hash).digest(domain)
            theirs = functions.hashAccept((*head, message["actor"], offer_hash)).call()
        else:
            ours = Close(*head, message["actor"], message["reason"]).digest(domain)
            theirs = functions.hashClose((*head, message["actor"], message["reason"])).call()
        assert Digest(ours) == Digest(bytes(theirs)) == action["digest"]


@pytest.mark.parametrize("name", SCENARIOS)
def test_the_setup_approvals_came_from_the_derived_wallets_and_set_finite_allowances(
    runs: dict[str, RunRecord], agents: dict[str, AgentProcess], chain: Chain, name: str
) -> None:
    record = runs[name]
    tokens = {"buyer": chain.quote_token, "seller": chain.base_token}
    allowances = {role: int(scenario(name)[role]["allowance_minor"]) for role in ROLES}
    for role in ROLES:
        approval = record.setup_approvals[role]
        sender = Account.recover_transaction(approval["raw_tx"])
        assert sender == approval["from"] == record.addresses[role]
        assert sender != agents[role].root_address
        assert approval["spender"] == chain.exchange.address
        assert approval["receipt"]["to"] == tokens[role].address
    # After settlement the buyer's allowance is spent down by exactly the price it paid.
    spent = {"default-overlap": {"buyer": 93_333_333, "seller": 10_000_000}}.get(name, {})
    for role in ROLES:
        remaining = (
            tokens[role].functions.allowance(record.addresses[role], chain.exchange.address).call()
        )
        assert remaining == allowances[role] - spent.get(role, 0)


def test_each_run_had_fresh_wallets(runs: dict[str, RunRecord]) -> None:
    """ADR-039: the same roots, a different run, different keys."""
    first, second = (runs[name] for name in SCENARIOS)
    for role in ROLES:
        assert first.addresses[role] != second.addresses[role]
        for record in (first, second):
            derivation = record.provisioned[role]["key_derivation"]
            assert derivation["run_id"] == str(record.run_id)
            assert derivation["role"] == role
            assert derivation["chain_id"] == 31337


@pytest.mark.parametrize("name", SCENARIOS)
def test_both_agents_approved_the_session_with_the_on_chain_config_hash(
    runs: dict[str, RunRecord], name: str
) -> None:
    record = runs[name]
    for role in ROLES:
        assert record.approvals[role] == {
            "approved": True,
            "config_hash": record.opened["config_hash"],
        }


@pytest.mark.parametrize("name", SCENARIOS)
def test_the_chain_record_reconstructs_without_the_harness(
    runs: dict[str, RunRecord], chain: Chain, name: str
) -> None:
    """A15's tool, reading only the chain and the manifest, agrees with what the agents signed."""
    result = Reconstructor(chain.w3, dict(chain.manifest)).run(runs[name].opened["session_id"])
    failures = [check for check in result.checks if not check.ok]
    assert not failures, "\n".join(f"{check.check}: {check.detail}" for check in failures)
    assert result.ok


# --------------------------------------------------------------------------------------
# After the run
# --------------------------------------------------------------------------------------


def test_a_released_run_signs_nothing_more(
    runs: dict[str, RunRecord], agents: dict[str, AgentProcess]
) -> None:
    record = runs["default-overlap"]
    response = agents["buyer"].client.call(
        "POST",
        f"/internal/runs/{record.run_id}/setup-approval",
        {
            "nonce": 1,
            "gas_limit": 70_000,
            "max_fee_per_gas_wei": "1",
            "max_priority_fee_per_gas_wei": "1",
        },
    )
    assert response.status_code == 409
    assert response.json()["error"]["details"]["state"] == "released"


def test_an_expired_offer_is_countered_not_accepted(runs: dict[str, RunRecord]) -> None:
    """The path the two scenarios never take: an offer that expires before its counterparty acts.

    The seller's observation carries the expired offer in history, marked `expired`, and a null
    `active_offer` (protocol 12, ADR-046); the agent accepts that as consistent, the baseline cannot
    accept, so it counters at its next price, and the buyer accepts the counter.
    """
    record = runs[EXPIRED]
    seen_by_seller = record.observations[5]
    assert seen_by_seller["role"] == "seller"
    assert seen_by_seller["active_offer"] is None
    assert seen_by_seller["history"][-1]["status"] == "expired"
    assert record.final_status == SETTLED
    assert moves(record) == [
        ("buyer", "offer", "80000000"),
        ("seller", "offer", "108000000"),
        ("buyer", "offer", "86666666"),
        ("seller", "offer", "102000000"),
        ("buyer", "offer", "93333333"),
        ("seller", "offer", "96000000"),
        ("buyer", "accept", None),
    ]


def test_neither_agent_logged_a_secret_or_a_mandate(
    runs: dict[str, RunRecord], agents: dict[str, AgentProcess]
) -> None:
    """Every line both processes wrote, start-up included, against every secret either was given.

    The review of stage 2.2 found the first version of this scan looked only for mandate values, so
    an agent that logged its root at start-up passed it.
    """
    logs = {role: Path(agents[role].log_path).read_text(encoding="utf-8") for role in ROLES}
    assert all('"event": "started"' in text for text in logs.values())
    assert all('"event": "request"' in text for text in logs.values())
    secrets = [value for role in ROLES for value in agents[role].private_values]
    for text in logs.values():
        for value in secrets:
            assert value not in text
    for name in SCENARIOS:
        for role in ROLES:
            mandate = scenario(name)[role]["mandate"]
            for text in logs.values():
                assert mandate["instructions"] not in text
                assert f'"{mandate["reservation_price_minor"]}"' not in text
                assert f'"{mandate["min_remaining_inventory_minor"]}"' not in text
