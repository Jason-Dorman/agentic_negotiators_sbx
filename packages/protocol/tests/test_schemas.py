"""The schemas are the machine-readable half of the protocol's boundaries, so they are tested.

A schema that accepts everything passes no test by accident: each block below pairs a valid
instance with the specific mutations the schema exists to refuse. The mutations are chosen from
the failures that would matter — an extra field on a decision, a float amount, a counterparty
mandate inside an observation, a private field in a default export.

`additionalProperties: false` is doing the load-bearing work in three of these. It is what turns
"the observation allowlist in docs/protocol.md section 12 is exhaustive" from a sentence into a
check, and what makes "the default export contains no private fields" a schema failure rather
than a code review finding (docs/test_strategy.md section 7).
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, ValidationError

from negotiation_protocol import SCHEMA_FILES, load_schema, schemas_dir, validate

REPO_ROOT = Path(__file__).resolve().parents[3]

VALID_MANDATE: dict[str, Any] = {
    "reservation_price_minor": "100000000",
    "min_remaining_inventory_minor": "0",
    "instructions": "Buy below your limit. Leave room to concede.",
}

VALID_OBSERVATION: dict[str, Any] = {
    "schema_version": "1",
    "run_id": "3f6d1c18-9b2e-4a7c-8f1d-2b4a6c8e0d12",
    "role": "buyer",
    "my_address": "0x0376AAc07Ad725E01357B1725B5ceC61aE10473c",
    "session": {
        "session_id": "0x" + "11" * 32,
        "config_hash": "0x" + "22" * 32,
        "chain_id": 31337,
        "exchange_address": "0x9fE46736679d2D9a65F0992F2272dE9f3c7fa6e0",
        "base_token": "0x5FbDB2315678afecb367f032d93F642f64180aa3",
        "quote_token": "0xe7f1725E7734CE288F8367e1Bb143E90bb3F0512",
        "base_amount_minor": "10000000",
        "expires_at": 1760001800,
        "max_offers": 8,
        "token_decimals": 6,
    },
    "chain_time": 1760000000,
    "expected_sequence": 3,
    "offers_remaining_for_me": 3,
    "active_offer": {
        "offer_hash": "0x" + "33" * 32,
        "proposer": "seller",
        "quote_amount_minor": "97000000",
        "valid_until": 1760000600,
        "sequence": 2,
    },
    "history": [
        {
            "sequence": 1,
            "actor": "buyer",
            "kind": "offer",
            "quote_amount_minor": "92000000",
            "valid_until": 1760000500,
            "offer_hash": "0x" + "44" * 32,
            "status": "replaced",
        }
    ],
    "my_balances": {"base_minor": "0", "quote_minor": "250000000"},
    "my_previous_decisions": [
        {
            "turn": 1,
            "decision": {"action": "offer", "quote_amount_minor": "92000000"},
            "result": "recorded",
        }
    ],
    "mandate": VALID_MANDATE,
}


class TestEverySchemaIsWellFormed:
    @pytest.mark.parametrize("name", SCHEMA_FILES)
    def test_the_schema_is_a_valid_2020_12_schema(self, name: str) -> None:
        Draft202012Validator.check_schema(load_schema(name))

    @pytest.mark.parametrize("name", SCHEMA_FILES)
    def test_the_schema_declares_its_dialect_and_identity(self, name: str) -> None:
        schema = load_schema(name)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
        assert schema["$id"].endswith(name)
        # A description is not decoration here: these files are read by implementers in three
        # languages, and the reason a field exists does not survive in the field name alone.
        assert schema["description"]

    def test_the_directory_holds_exactly_the_declared_schemas(self) -> None:
        # Guards the registry in `resources.py`: a schema added to the directory but not to
        # SCHEMA_FILES would be unresolvable as a `$ref` target, and `oneOf`/`$ref` resolution
        # failures are reported as validation *passes* by some tooling.
        on_disk = {path.name for path in schemas_dir().glob("*.json")}
        assert on_disk == set(SCHEMA_FILES)


def _objects_accepting_unlisted_fields(schema: Any, path: str = "") -> list[str]:
    """Every object in `schema` that does not set `additionalProperties: false`, by JSON path.

    Recursive, and it walks `$defs` as well as `properties`, because a subschema reached only by
    `$ref` is as much part of the allowlist as an inline one.
    """
    if not isinstance(schema, dict):
        return []

    found: list[str] = []
    if "properties" in schema and schema.get("additionalProperties") is not False:
        found.append(path or "<root>")

    for key, value in schema.items():
        if key in {"properties", "$defs"} and isinstance(value, dict):
            for name, child in value.items():
                found += _objects_accepting_unlisted_fields(child, f"{path}/{key}/{name}")
        elif isinstance(value, dict):
            found += _objects_accepting_unlisted_fields(value, f"{path}/{key}")
        elif isinstance(value, list):
            for index, child in enumerate(value):
                found += _objects_accepting_unlisted_fields(child, f"{path}/{key}/{index}")
    return found


#: Transcribed by hand from docs/protocol.md section 12, which is the allowlist. Deliberately not
#: derived from the schema, because the schema is what is under test.
SECTION_12_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "role",
        "my_address",
        "session",
        "chain_time",
        "expected_sequence",
        "offers_remaining_for_me",
        "active_offer",
        "history",
        "my_balances",
        "my_previous_decisions",
        "mandate",
    }
)

SECTION_12_SESSION_FIELDS = frozenset(
    {
        "session_id",
        "config_hash",
        "chain_id",
        "exchange_address",
        "base_token",
        "quote_token",
        "base_amount_minor",
        "expires_at",
        "max_offers",
        "token_decimals",
    }
)

SECTION_12_HISTORY_FIELDS = frozenset(
    {"sequence", "actor", "kind", "quote_amount_minor", "valid_until", "offer_hash", "status"}
)


class TestTheAllowlistIsTheDocumentsAllowlist:
    """The observation schema's field names, against docs/protocol.md section 12 itself.

    Every other test in this file asks whether the schema refuses a *value*. This asks whether the
    schema's field list is the document's, which is a different question and the one an adversarial
    review caught: `historyEntry` carried a `reason_code` that section 12 does not list, inside the
    schema that declares itself the exhaustive machine-readable form of that section. No
    negative-instance test could have found it, because nothing was checking the field set.
    """

    def test_the_top_level_fields_are_exactly_section_12s(self) -> None:
        schema = load_schema("observation.v1.json")
        assert set(schema["properties"]) == SECTION_12_FIELDS
        # And every one is required: an allowlisted field that may be absent is one a policy
        # cannot rely on being told.
        assert set(schema["required"]) == SECTION_12_FIELDS

    def test_the_session_block_fields_are_exactly_section_12s(self) -> None:
        session = load_schema("observation.v1.json")["$defs"]["session"]
        assert set(session["properties"]) == SECTION_12_SESSION_FIELDS
        assert set(session["required"]) == SECTION_12_SESSION_FIELDS

    def test_the_history_entry_fields_are_exactly_section_12s(self) -> None:
        entry = load_schema("observation.v1.json")["$defs"]["historyEntry"]
        assert set(entry["properties"]) == SECTION_12_HISTORY_FIELDS

    def test_a_terminal_reason_code_is_not_smuggled_into_history(self) -> None:
        # The specific regression. A reason code is a fact about a terminal action, and history
        # holds confirmed actions of a session that is still open; adding it here would be a
        # protocol change to section 12, made quietly in a schema file.
        document = deepcopy(VALID_OBSERVATION)
        document["history"][0]["reason_code"] = 1
        with pytest.raises(ValidationError):
            validate(document, "observation.v1.json")

    def test_every_object_in_the_observation_closes_its_properties(self) -> None:
        """`additionalProperties: false` at every level, recursively, including through `$defs`.

        This is the property the allowlist rests on. One object left open anywhere is a slot
        anything can travel in, and the leak would be invisible to every value-level test.
        """
        open_objects = _objects_accepting_unlisted_fields(load_schema("observation.v1.json"))
        assert open_objects == [], f"objects that accept unlisted fields: {open_objects}"


class TestAgentDecision:
    @pytest.mark.parametrize(
        "decision",
        [
            {"action": "offer", "quote_amount_minor": "94000000"},
            {"action": "accept", "offer_hash": "0x" + "ab" * 32},
            {"action": "walk_away", "reason": "terms_unacceptable"},
            {"action": "walk_away", "reason": "inventory_constraint"},
            {"action": "walk_away", "reason": "no_further_concession"},
        ],
    )
    def test_each_permitted_action_validates(self, decision: dict[str, Any]) -> None:
        validate({"decision": decision}, "agent_decision.v1.json")

    def test_an_explanation_is_optional_and_bounded(self) -> None:
        validate(
            {"decision": {"action": "offer", "quote_amount_minor": "1"}, "explanation": "x" * 280},
            "agent_decision.v1.json",
        )
        with pytest.raises(ValidationError):
            validate(
                {
                    "decision": {"action": "offer", "quote_amount_minor": "1"},
                    "explanation": "x" * 281,
                },
                "agent_decision.v1.json",
            )

    @pytest.mark.parametrize(
        ("case", "document"),
        [
            # An amount the model wrote as a JSON number. Every parser in the chain would turn
            # this into a float, and a price that has been through a float is not the price that
            # was signed.
            ("numeric amount", {"decision": {"action": "offer", "quote_amount_minor": 94000000}}),
            ("zero amount", {"decision": {"action": "offer", "quote_amount_minor": "0"}}),
            ("leading zero", {"decision": {"action": "offer", "quote_amount_minor": "094"}}),
            ("negative", {"decision": {"action": "offer", "quote_amount_minor": "-94"}}),
            ("decimal point", {"decision": {"action": "offer", "quote_amount_minor": "94.0"}}),
            ("exponent", {"decision": {"action": "offer", "quote_amount_minor": "9.4e7"}}),
            # An extra field is a reject, not something to ignore. A model that returned
            # `{"action": "offer", "quote_amount_minor": "94", "recipient": "0x…"}` would be
            # proposing something the schema has no opinion about, and silence would be consent.
            (
                "extra field on the action",
                {
                    "decision": {
                        "action": "offer",
                        "quote_amount_minor": "94",
                        "recipient": "0x" + "11" * 20,
                    }
                },
            ),
            (
                "extra field on the envelope",
                {"decision": {"action": "offer", "quote_amount_minor": "94"}, "sequence": 3},
            ),
            ("unknown action", {"decision": {"action": "settle", "quote_amount_minor": "94"}}),
            ("unknown walk-away reason", {"decision": {"action": "walk_away", "reason": "bored"}}),
            ("short offer hash", {"decision": {"action": "accept", "offer_hash": "0xabc"}}),
            (
                "offer hash without prefix",
                {"decision": {"action": "accept", "offer_hash": "ab" * 32}},
            ),
            ("missing decision", {"explanation": "no action"}),
            # Two actions in one object: `oneOf` refuses it. `anyOf` would have accepted the
            # first branch that matched and discarded the rest of the model's intent.
            (
                "two actions",
                {
                    "decision": {
                        "action": "offer",
                        "quote_amount_minor": "94",
                        "offer_hash": "0x" + "11" * 32,
                    }
                },
            ),
        ],
    )
    def test_a_malformed_decision_is_refused(self, case: str, document: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            validate(document, "agent_decision.v1.json")
        assert case  # named for the failure report


class TestObservation:
    def test_the_reference_observation_validates(self) -> None:
        validate(VALID_OBSERVATION, "observation.v1.json")

    def test_no_active_offer_is_null_rather_than_absent(self) -> None:
        # Absent and null are different states to a policy: "there is no offer to accept" is a
        # fact it must be told, not one it infers from a missing key.
        document = deepcopy(VALID_OBSERVATION)
        document["active_offer"] = None
        validate(document, "observation.v1.json")

        del document["active_offer"]
        with pytest.raises(ValidationError):
            validate(document, "observation.v1.json")

    @pytest.mark.parametrize(
        ("case", "mutate"),
        [
            # The failure the allowlist exists to prevent. Every one of these is a plausible
            # convenience a future observation builder might add.
            ("counterparty mandate", lambda d: d.update({"their_mandate": VALID_MANDATE})),
            (
                "counterparty reservation price",
                lambda d: d.update({"counterparty_reservation_price_minor": "90000000"}),
            ),
            ("feasibility", lambda d: d.update({"feasible": True})),
            (
                "feasible interval",
                lambda d: d.update({"feasible_interval_minor": ["90000000", "100000000"]}),
            ),
            (
                "counterparty rejected attempt",
                lambda d: d["history"].append(
                    {"sequence": 2, "actor": "seller", "kind": "offer", "status": "rejected"}
                ),
            ),
            (
                "counterparty balances",
                lambda d: d.update({"their_balances": {"base_minor": "1", "quote_minor": "1"}}),
            ),
            ("a private key", lambda d: d.update({"my_private_key": "0x" + "11" * 32})),
            ("the prompt", lambda d: d.update({"system_prompt": "You are a buyer…"})),
            # Structural failures that would let a policy act on unconfirmed or ill-typed state.
            ("float amount", lambda d: d["my_balances"].update({"quote_minor": 250000000})),
            ("wrong schema version", lambda d: d.update({"schema_version": "2"})),
            ("unknown role", lambda d: d.update({"role": "operator"})),
            ("wrong token decimals", lambda d: d["session"].update({"token_decimals": 18})),
            ("max offers above 32", lambda d: d["session"].update({"max_offers": 33})),
            ("sequence below one", lambda d: d.update({"expected_sequence": 0})),
            (
                "mandate with an extra field",
                lambda d: d["mandate"].update({"counterparty_hint": "they are desperate"}),
            ),
        ],
    )
    def test_anything_outside_the_allowlist_is_refused(self, case: str, mutate: Any) -> None:
        document = deepcopy(VALID_OBSERVATION)
        mutate(document)
        with pytest.raises(ValidationError):
            validate(document, "observation.v1.json")
        assert case

    def test_the_observation_reuses_the_decision_schema_for_previous_attempts(self) -> None:
        # The cross-file `$ref` is what keeps a policy's own history in the same shape as its
        # output. If it resolved to nothing, this malformed entry would pass.
        document = deepcopy(VALID_OBSERVATION)
        document["my_previous_decisions"][0]["decision"] = {
            "action": "offer",
            "quote_amount_minor": "92000000",
            "recipient": "0x" + "11" * 20,
        }
        with pytest.raises(ValidationError):
            validate(document, "observation.v1.json")


class TestMandate:
    def test_the_reference_mandate_validates(self) -> None:
        validate(VALID_MANDATE, "mandate.v1.json")

    def test_extra_is_reserved_and_empty_rather_than_a_free_slot(self) -> None:
        """`extra` claims to be "empty until there are some", so the schema enforces that.

        Left open it was an unconstrained object inside a schema that `observation.v1.json` embeds
        by `$ref`, so the observation allowlist accepted an arbitrary payload one level down --
        which is exactly what the allowlist exists to prevent.
        """
        validate({**VALID_MANDATE, "extra": {}}, "mandate.v1.json")

        with pytest.raises(ValidationError):
            validate(
                {**VALID_MANDATE, "extra": {"counterparty_hint": "they are desperate"}},
                "mandate.v1.json",
            )

    def test_a_payload_in_extra_is_refused_through_the_observation_reference(self) -> None:
        document = deepcopy(VALID_OBSERVATION)
        document["mandate"] = {**VALID_MANDATE, "extra": {"their_floor_minor": "90000000"}}
        with pytest.raises(ValidationError):
            validate(document, "observation.v1.json")

    @pytest.mark.parametrize(
        ("case", "document"),
        [
            (
                "missing reservation price",
                {"min_remaining_inventory_minor": "0", "instructions": ""},
            ),
            (
                "numeric reservation price",
                {
                    "reservation_price_minor": 100000000,
                    "min_remaining_inventory_minor": "0",
                    "instructions": "",
                },
            ),
            (
                "extra field",
                {
                    "reservation_price_minor": "1",
                    "min_remaining_inventory_minor": "0",
                    "instructions": "",
                    "walk_away_after_turns": 3,
                },
            ),
        ],
    )
    def test_a_malformed_mandate_is_refused(self, case: str, document: dict[str, Any]) -> None:
        with pytest.raises(ValidationError):
            validate(document, "mandate.v1.json")
        assert case


class TestScenarioFiles:
    def test_there_is_at_least_one_scenario_to_validate(self) -> None:
        # A schema with no instances is a gate that has never been shown working
        # (docs/build_plan.md stage 0, on the same mistake).
        assert list((REPO_ROOT / "scenarios").glob("*.json"))

    @pytest.mark.parametrize(
        "path",
        sorted((REPO_ROOT / "scenarios").glob("*.json")),
        ids=lambda path: path.name,
    )
    def test_every_committed_scenario_validates(self, path: Path) -> None:
        validate(json.loads(path.read_text(encoding="utf-8")), "scenario.v1.json")

    def test_scenario_ids_are_unique_and_match_their_file_names(self) -> None:
        # The loader upserts by `scenario_id`, so two files sharing one would silently make the
        # second overwrite the first (docs/data_model.md section 3.1).
        for path in sorted((REPO_ROOT / "scenarios").glob("*.json")):
            document = json.loads(path.read_text(encoding="utf-8"))
            assert document["scenario_id"] == path.stem, path.name

    @pytest.mark.parametrize(
        "path",
        sorted((REPO_ROOT / "scenarios").glob("*.json")),
        ids=lambda path: path.name,
    )
    def test_no_public_field_states_a_private_value(self, path: Path) -> None:
        """`name` and `description` are returned by `GET /v1/scenarios` with no reveal header.

        An adversarial review found both scenario descriptions stating each party's reservation
        price *and* the feasible interval — beside a `feasibility_hint` the API contract promises is
        always null on that route. The schema cannot catch this, because the leak was in the value
        of an allowed field, so it is asserted here against the mandates the same file carries.

        Matched on word boundaries, and the reservation prices are also checked in whole mUSD,
        which is how a description would naturally phrase one. A zero inventory floor is skipped:
        the absence of a floor is not a secret, and "0" as a substring is in half the file. The
        floor's whole-unit form is skipped too — 10 mASSET is both the seller's floor and the
        public base amount in these scenarios, so it cannot be distinguished here, and it is the
        prices that carry the information an agent must not have.
        """
        document = json.loads(path.read_text(encoding="utf-8"))
        public = f"{document['name']} {document['description']}".lower()

        for party in ("buyer", "seller"):
            mandate = document[party]["mandate"]

            price = mandate["reservation_price_minor"]
            assert not re.search(rf"\b{re.escape(price)}\b", public), (
                f"{path.name}: public text states {party}'s reservation price in minor units"
            )
            whole = int(price) // 1_000_000
            assert f"{whole} musd" not in public, (
                f"{path.name}: public text states {party}'s reservation price as {whole} mUSD"
            )

            floor = mandate["min_remaining_inventory_minor"]
            if int(floor) != 0:
                assert not re.search(rf"\b{re.escape(floor)}\b", public), (
                    f"{path.name}: public text states {party}'s inventory floor in minor units"
                )

        # The verdict, not only the numbers: an agent must not be told whether a deal is possible.
        for verdict in (
            "feasible interval",
            "infeasible interval",
            "no price satisfies",
            "no valid exchange",
            "surplus",
        ):
            assert verdict not in public, f"{path.name}: public text states feasibility ({verdict})"

    def test_a_scenario_carrying_a_feasibility_hint_is_refused(self) -> None:
        # Feasibility is the evaluator's alone. A scenario file that stated it would put the
        # answer one route away from an agent (docs/api_contract.md section 2.1).
        document = json.loads(
            (REPO_ROOT / "scenarios" / "default-overlap.json").read_text(encoding="utf-8")
        )
        document["feasible"] = True
        with pytest.raises(ValidationError):
            validate(document, "scenario.v1.json")
