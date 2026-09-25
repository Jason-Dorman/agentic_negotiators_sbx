"""`export.v1.json` and `deployment_manifest.v1.json`, checked against instances.

A large schema full of `$ref`s can be well-formed, load without error, and accept anything — an
unresolved reference is not always a validation failure. So both schemas get a complete valid
instance and a list of mutations they must refuse.

The mutations that matter are the privacy ones. The export is the document this project asks to be
believed: it is published, attached to evidence, and read by someone who was not in the room. The
claim is that the default export carries no mandate, no model response text and no validation
feedback, and each of those is a case below.
"""

from __future__ import annotations

import json
import subprocess
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import ValidationError

from negotiation_protocol import validate

REPO_ROOT = Path(__file__).resolve().parents[3]

DIGEST = "0x" + "ab" * 32
BLOCK_HASH = "0x" + "cd" * 32
TX_HASH = "0x" + "ef" * 32
EXCHANGE = "0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0"
BASE_TOKEN = "0x5FbDB2315678afecb367f032d93F642f64180aa3"
QUOTE_TOKEN = "0xe7f1725E7734CE288F8367e1Bb143E90bb3F0512"
BUYER = "0x0376AAc07Ad725E01357B1725B5ceC61aE10473c"
SELLER = "0xC3A4c5eF1696195f390D03e9fE370E6BAbD28251"
OPERATOR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"
RELAY = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"

MANIFEST: dict[str, Any] = {
    "manifest_version": "1",
    "deployment_id": "local-2026-09-24-01",
    "protocol_version": "1",
    "chain_id": 31337,
    "exchange_address": EXCHANGE,
    "base_token_address": BASE_TOKEN,
    "quote_token_address": QUOTE_TOKEN,
    "operator_address": OPERATOR,
    "relay_address": RELAY,
    "token_decimals": 6,
    "code_hashes": {"exchange": DIGEST, "base_token": DIGEST, "quote_token": DIGEST},
    "compiler": {
        "solc": "0.8.28+commit.7893614a",
        "optimizer": True,
        "runs": 200,
        "evm_version": "cancun",
        "bytecode_hash": "ipfs",
    },
    "start_block": 1,
    "deployed_at_ts": 1760000000,
    "explorer_base_url": None,
    "ens": None,
}


def _tx() -> dict[str, Any]:
    return {
        "tx_hash": TX_HASH,
        "status": "confirmed",
        "block_number": 12,
        "block_hash": BLOCK_HASH,
        "confirmations": 1,
        "explorer_url": None,
    }


def _party(address: str) -> dict[str, Any]:
    return {
        "address": address,
        "policy": "deterministic",
        "model_id": None,
        "effort": None,
        "balances": {"base_minor": "0", "quote_minor": "250000000"},
        "current_action": "idle",
        "decision_status": None,
    }


def valid_export() -> dict[str, Any]:
    """A settled run's default export: public everywhere, `private` null."""
    return {
        "export_version": "1",
        "exported_at": "2026-09-24T12:00:00Z",
        "includes_private": False,
        "disclaimer": (
            "Test assets and simulated economics. Public testnets may be reset; this export is "
            "the archive of record."
        ),
        "run": {
            "run_id": "3f6d1c18-9b2e-4a7c-8f1d-2b4a6c8e0d12",
            "name": "Default overlap, deterministic",
            "parent_run_id": None,
            "created_at": "2026-09-24T11:55:00Z",
            "state": "terminal",
            "state_cause": None,
            "mode": "live",
            "outcome": {"kind": "settled", "reason_code": None, "reason": None, "actor": "seller"},
            "deployment": {
                "deployment_id": "local-2026-09-24-01",
                "chain_id": 31337,
                "exchange_address": EXCHANGE,
                "explorer_base_url": None,
            },
            "session": {
                "session_id": DIGEST,
                "config_hash": DIGEST,
                "buyer_address": BUYER,
                "seller_address": SELLER,
                "base_amount_minor": "10000000",
                "expires_at_ts": 1760001800,
                "max_offers": 8,
                "status": "settled",
                "sequence": 2,
                "offer_count": 1,
                "active_offer": None,
                "opened_tx_hash": TX_HASH,
            },
            "parties": {"buyer": _party(BUYER), "seller": _party(SELLER)},
            "timeline": [
                {
                    "sequence": 1,
                    "kind": "offer",
                    "actor": "buyer",
                    "quote_amount_minor": "92000000",
                    "valid_until_ts": 1760000600,
                    "offer_hash": DIGEST,
                    "references_offer_hash": None,
                    "reason_code": None,
                    "reason": None,
                    "tx": _tx(),
                    "sentence": "Buyer offers 92 mUSD for 10 mASSET.",
                    "recorded_at": "2026-09-24T11:56:00Z",
                },
                {
                    "sequence": 2,
                    "kind": "settle",
                    "actor": "seller",
                    "quote_amount_minor": "92000000",
                    "valid_until_ts": None,
                    "offer_hash": DIGEST,
                    "references_offer_hash": DIGEST,
                    "reason_code": None,
                    "reason": None,
                    "tx": _tx(),
                    "sentence": "Settled: 10 mASSET to buyer, 92 mUSD to seller.",
                    "recorded_at": "2026-09-24T11:57:00Z",
                },
            ],
            "metrics": {
                "recorded_offers": 1,
                "decision_time_ms": 120,
                "chain_wait_ms": 2400,
                "model_calls": 0,
                "model_cost_estimated_usd": None,
                "model_cost_reported_usd": None,
                "gas_used": 412000,
                "fee_wei": "412000000000000",
            },
            "labels": {
                "test_assets": True,
                "simulated_economics": True,
                "fixture": False,
                "operator_abort_power": True,
            },
        },
        "deployment_manifest": MANIFEST,
        "reproducibility": {
            "scenario_id": "default-overlap",
            "policy_versions": {"buyer": "deterministic-1", "seller": "deterministic-1"},
            "prompt_template_versions": {},
            "model_ids": {},
            "effort": {},
            "seed": None,
            "seed_supported": False,
            "protocol_version": "1",
            "software_version": "0.1.0+abc1234",
        },
        "signed_actions": [
            {
                "sequence": 1,
                "kind": "offer",
                "typed_message": {
                    "sessionId": DIGEST,
                    "configHash": DIGEST,
                    "sequence": 1,
                    "proposer": BUYER,
                    "quoteAmount": "92000000",
                    "validUntil": 1760000600,
                },
                "digest": DIGEST,
                "signer": BUYER,
                "signature": "0x" + "11" * 65,
                "tx_hash": TX_HASH,
            }
        ],
        "chain_events": [
            {
                "event": "SettlementCompleted",
                "block_number": 12,
                "block_hash": BLOCK_HASH,
                "tx_hash": TX_HASH,
                "log_index": 2,
                "canonical": True,
                "decoded": {"quoteAmount": "92000000"},
            }
        ],
        "calldata": [
            {
                "tx_hash": TX_HASH,
                "to": EXCHANGE,
                "input": "0xdeadbeef",
                "decoded_function": "acceptAndSettle",
                "decoded_args": {"acceptance": {}, "signature": "0x" + "11" * 65},
            }
        ],
        "balance_snapshots": [
            {
                "stage": "post_settlement",
                "party": "buyer",
                "token": "quote",
                "amount_minor": "158000000",
                "block_number": 12,
                "block_hash": BLOCK_HASH,
                "canonical": True,
            }
        ],
        "decisions_public": [
            {
                "turn": 1,
                "party": "buyer",
                "attempt": 1,
                "status": "valid",
                "action": "offer",
                "usage": None,
                "latency_ms": 4,
                "cost_estimated_usd": None,
                "cost_reported_usd": None,
                "label": "private_operational_record_not_an_authorized_offer",
            }
        ],
        "metrics": {
            "recorded_offers": 1,
            "decision_time_ms": 120,
            "chain_wait_ms": 2400,
            "setup_chain_wait_ms": 800,
            "model_calls": 0,
            "input_tokens": 0,
            "output_tokens": 0,
            "model_cost_estimated_usd": None,
            "model_cost_reported_usd": None,
            "gas_used_negotiation": "412000",
            "gas_used_setup": "180000",
            "fee_wei_negotiation": "412000000000000",
            "fee_wei_setup": "180000000000000",
            "settled_quote_minor": "92000000",
            "mandate_violations": 0,
            "failure_class": "none",
            "audit_complete": True,
        },
        "private": None,
    }


class TestDeploymentManifest:
    def test_the_reference_manifest_validates(self) -> None:
        validate(MANIFEST, "deployment_manifest.v1.json")

    def test_a_sepolia_manifest_with_names_validates(self) -> None:
        document = deepcopy(MANIFEST)
        document["chain_id"] = 11155111
        document["deployment_id"] = "sepolia-2026-09-24-01"
        document["explorer_base_url"] = "https://sepolia.etherscan.io"
        document["ens"] = {
            "root": "agentnegotiation.eth",
            "exchange": "exchange.agentnegotiation.eth",
            "base_token": "masset.agentnegotiation.eth",
            "quote_token": "musd.agentnegotiation.eth",
            "resolved_at": "2026-09-24T12:00:00Z",
        }
        validate(document, "deployment_manifest.v1.json")

    @pytest.mark.parametrize(
        ("case", "mutate"),
        [
            # A missing nullable field is not the same as a null one: it could mean an older
            # script wrote the file, and the consumer cannot tell.
            ("omitted explorer", lambda d: d.pop("explorer_base_url")),
            ("omitted ens", lambda d: d.pop("ens")),
            ("omitted scan start block", lambda d: d.pop("start_block")),
            # A manifest whose code hash is not 32 bytes cannot be compared against `eth_getCode`,
            # so the identity check that A17 rests on would silently pass.
            ("short code hash", lambda d: d["code_hashes"].update({"exchange": "0xabcd"})),
            ("extra top-level field", lambda d: d.update({"deployer_private_key": "0x00"})),
            ("wrong token decimals", lambda d: d.update({"token_decimals": 18})),
            ("unknown protocol version", lambda d: d.update({"protocol_version": "2"})),
            (
                "uppercase deployment id",
                lambda d: d.update({"deployment_id": "Local-2026-09-24-01"}),
            ),
        ],
    )
    def test_a_malformed_manifest_is_refused(self, case: str, mutate: Any) -> None:
        document = deepcopy(MANIFEST)
        mutate(document)
        with pytest.raises(ValidationError):
            validate(document, "deployment_manifest.v1.json")
        assert case

    def test_every_committed_manifest_validates(self) -> None:
        """Sepolia manifests are committed; local ones are git-ignored (ADR-038).

        Scoped to *tracked* files rather than to everything in the directory. A developer's local
        Anvil manifest sitting there, possibly written by an older deploy script, is not something
        this project promises anything about — and failing the suite over one would train people to
        ignore the failure. Empty today; the loop is what makes the stage 5 manifest fail the build
        rather than the reviewer's eye.
        """
        listed = subprocess.run(
            ["git", "ls-files", "-z", "docs/deployments"],
            cwd=REPO_ROOT,
            capture_output=True,
            check=True,
            text=True,
        )
        for name in (entry for entry in listed.stdout.split("\0") if entry.endswith(".json")):
            path = REPO_ROOT / name
            validate(json.loads(path.read_text(encoding="utf-8")), "deployment_manifest.v1.json")


class TestExport:
    def test_the_reference_export_validates(self) -> None:
        validate(valid_export(), "export.v1.json")

    def test_a_private_export_validates(self) -> None:
        document = valid_export()
        document["includes_private"] = True
        document["private"] = {
            "mandates": {
                "buyer": {
                    "reservation_price_minor": "100000000",
                    "min_remaining_inventory_minor": "0",
                    "instructions": "Pay as little as you can.",
                },
                "seller": {
                    "reservation_price_minor": "90000000",
                    "min_remaining_inventory_minor": "10000000",
                    "instructions": "Obtain as much as you can.",
                },
            },
            "decisions": [{"raw_response": {"decision": {"action": "offer"}}}],
            "observations": [{"schema_version": "1"}],
            "evaluator": {
                "feasible": True,
                "feasible_interval_minor": ["90000000", "100000000"],
                "feasible_surplus_minor": "10000000",
            },
        }
        validate(document, "export.v1.json")

    @pytest.mark.parametrize(
        ("case", "mutate"),
        [
            # The four privacy failures the schema exists to catch. Each is a field that a
            # careless export builder could plausibly include.
            (
                "mandate at the top level",
                lambda d: d.update({"mandates": {"buyer": {}, "seller": {}}}),
            ),
            (
                "reservation price on a party",
                lambda d: d["run"]["parties"]["buyer"].update(
                    {"reservation_price_minor": "100000000"}
                ),
            ),
            (
                "raw model response in the public decisions",
                lambda d: d["decisions_public"][0].update(
                    {"raw_response": {"decision": {"action": "offer"}}}
                ),
            ),
            (
                "validation feedback in the public decisions",
                lambda d: d["decisions_public"][0].update(
                    {"validation_feedback": "Your quote is below your reservation price."}
                ),
            ),
            (
                "utility in the public metrics",
                lambda d: d["metrics"].update({"buyer_utility_minor": "8000000"}),
            ),
            (
                "feasibility in the public metrics",
                lambda d: d["metrics"].update({"feasible": True}),
            ),
            (
                "a key reference value",
                lambda d: d["run"]["parties"]["buyer"].update({"private_key": "0x00"}),
            ),
            # The label on a decision record is constant, because it is the sentence that stops a
            # reader mistaking an attempt for an offer.
            (
                "relabelled decision record",
                lambda d: d["decisions_public"][0].update({"label": "offer"}),
            ),
            # Structural failures that would make the export unverifiable.
            ("missing signed actions", lambda d: d.pop("signed_actions")),
            ("missing calldata", lambda d: d.pop("calldata")),
            ("short signature", lambda d: d["signed_actions"][0].update({"signature": "0xabcd"})),
            ("float amount", lambda d: d["metrics"].update({"settled_quote_minor": 92000000})),
            ("missing disclaimer", lambda d: d.pop("disclaimer")),
            ("empty disclaimer", lambda d: d.update({"disclaimer": ""})),
            ("test assets denied", lambda d: d["run"]["labels"].update({"test_assets": False})),
            (
                "simulated economics denied",
                lambda d: d["run"]["labels"].update({"simulated_economics": False}),
            ),
            ("unknown event name", lambda d: d["chain_events"][0].update({"event": "Transfer"})),
            ("wrong export version", lambda d: d.update({"export_version": "2"})),
        ],
    )
    def test_a_leaking_or_malformed_export_is_refused(self, case: str, mutate: Any) -> None:
        document = valid_export()
        mutate(document)
        with pytest.raises(ValidationError):
            validate(document, "export.v1.json")
        assert case

    @pytest.mark.parametrize(
        ("case", "mutate"),
        [
            # The seven slots that used to be bare `{"type": "object"}`. Each one was a place a
            # private value could ride out of the server inside the one document this project asks
            # to be believed, and no value-level test would have noticed.
            (
                "mandate hidden in policy_versions",
                lambda d: d["reproducibility"]["policy_versions"].update(
                    {"buyer_reservation_price_minor": "100000000"}
                ),
            ),
            (
                "mandate hidden in model_ids",
                lambda d: d["reproducibility"]["model_ids"].update({"seller_floor": "90000000"}),
            ),
            (
                "nested object in prompt_template_versions",
                lambda d: d["reproducibility"]["prompt_template_versions"].update(
                    {"buyer": {"instructions": "Pay as little as you can."}}
                ),
            ),
            (
                "api key in effort",
                lambda d: d["reproducibility"]["effort"].update({"api_key": "sk-ant-xxx"}),
            ),
            (
                "mandate field in a typed message",
                lambda d: d["signed_actions"][0]["typed_message"].update(
                    {"reservationPrice": "100000000"}
                ),
            ),
            (
                "mandate field in a decoded event",
                lambda d: d["chain_events"][0]["decoded"].update({"buyerFloor": "100000000"}),
            ),
            (
                "mandate field in decoded calldata",
                lambda d: d["calldata"][0]["decoded_args"].update({"mandate": {"floor": "1"}}),
            ),
        ],
    )
    def test_a_private_value_cannot_hide_in_a_loosely_typed_slot(
        self, case: str, mutate: Any
    ) -> None:
        document = valid_export()
        mutate(document)
        with pytest.raises(ValidationError):
            validate(document, "export.v1.json")
        assert case

    def test_the_legitimate_contents_of_those_slots_still_validate(self) -> None:
        """The constraints have to admit what actually goes there, or they would just be removed.

        `typed_message` carries the Offer/Accept/Close fields, `decoded` the seven events' fields,
        and `decoded_args` the ABI parameter names — including a nested struct, whose own shape is
        left alone because it is the *names* that are pinned.
        """
        document = valid_export()
        document["signed_actions"][0]["typed_message"] = {
            "sessionId": DIGEST,
            "configHash": DIGEST,
            "sequence": 2,
            "actor": SELLER,
            "offerHash": DIGEST,
        }
        document["chain_events"][0]["decoded"] = {
            "sessionId": DIGEST,
            "buyer": BUYER,
            "seller": SELLER,
            "baseToken": BASE_TOKEN,
            "baseAmount": "10000000",
            "quoteToken": QUOTE_TOKEN,
            "quoteAmount": "92000000",
            "offerHash": DIGEST,
        }
        document["calldata"][0]["decoded_args"] = {
            "acceptance": {
                "sessionId": DIGEST,
                "configHash": DIGEST,
                "sequence": 2,
                "actor": SELLER,
                "offerHash": DIGEST,
            },
            "signature": "0x" + "11" * 65,
        }
        document["reproducibility"]["policy_versions"] = {"buyer": "det-1", "seller": None}

        validate(document, "export.v1.json")

    def test_the_embedded_manifest_is_validated_through_the_reference(self) -> None:
        # Proves the cross-file `$ref` resolves. If it did not, a manifest with a bogus code hash
        # would pass inside an export while failing on its own.
        document = valid_export()
        document["deployment_manifest"]["code_hashes"]["exchange"] = "0xabcd"
        with pytest.raises(ValidationError):
            validate(document, "export.v1.json")

    def test_the_embedded_mandates_are_validated_through_the_reference(self) -> None:
        document = valid_export()
        document["includes_private"] = True
        document["private"] = {
            "mandates": {
                "buyer": {"reservation_price_minor": "100000000"},  # missing required fields
                "seller": {
                    "reservation_price_minor": "90000000",
                    "min_remaining_inventory_minor": "10000000",
                    "instructions": "",
                },
            },
            "decisions": [],
            "observations": [],
            "evaluator": {
                "feasible": None,
                "feasible_interval_minor": None,
                "feasible_surplus_minor": None,
            },
        }
        with pytest.raises(ValidationError):
            validate(document, "export.v1.json")
