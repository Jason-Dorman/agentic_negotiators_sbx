"""`POST /v1/runs`, parsed strictly (api_contract section 2.2): every refusal names a location and
a rule, and none repeats the value — the body carries both mandates."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from api.controller import RunRequest, RunRequestError

SECRET_INSTRUCTIONS = "never say 100 out loud"


def body() -> dict[str, Any]:
    party = {
        "policy": "deterministic",
        "model_id": None,
        "effort": None,
        "initial_balances": {"base_minor": "0", "quote_minor": "250000000"},
        "allowance_minor": "250000000",
        "mandate": {
            "reservation_price_minor": "100000000",
            "min_remaining_inventory_minor": "0",
            "instructions": SECRET_INSTRUCTIONS,
        },
    }
    return {
        "name": "run",
        "scenario_id": None,
        "deployment_id": "local",
        "public_config": {
            "base_amount_minor": "10000000",
            "max_offers": 8,
            "session_duration_s": 1800,
            "offer_lifetime_s": 600,
            "first_proposer": "buyer",
        },
        "buyer": party,
        "seller": copy.deepcopy(party),
        "limits": {
            "model_call_ceiling": 20,
            "model_spend_ceiling_usd": "2.00",
            "model_timeout_s": 45,
            "repair_attempts": 1,
        },
    }


def refused(document: dict[str, Any]) -> dict[str, str]:
    with pytest.raises(RunRequestError) as error:
        RunRequest.parse(document)
    assert error.value.code == "validation_error"
    rendered = str(error.value) + repr(error.value.details)
    assert SECRET_INSTRUCTIONS not in rendered and "100000000" not in rendered
    fields: dict[str, str] = error.value.details["fields"]
    return fields


def test_a_well_formed_request_parses_and_leaves_the_threshold_to_the_deployment() -> None:
    request = RunRequest.parse(body())
    assert request.public_config_document() == {
        "base_amount_minor": "10000000",
        "max_offers": 8,
        "session_duration_s": 1800,
        "offer_lifetime_s": 600,
        "first_proposer": "buyer",
        "token_decimals": 6,
    }


@pytest.mark.parametrize(
    ("path", "value", "location"),
    [
        (
            ("buyer", "mandate", "reservation_price_minor"),
            "100000000\n",
            "buyer.mandate.reservation_price_minor",
        ),
        (
            ("buyer", "mandate", "reservation_price_minor"),
            100000000,
            "buyer.mandate.reservation_price_minor",
        ),
        (("public_config", "base_amount_minor"), "0", "public_config.base_amount_minor"),
        (("public_config", "max_offers"), 33, "public_config.max_offers"),
        (("public_config", "first_proposer"), "seller", "public_config.first_proposer"),
        (("public_config", "confirmation_threshold"), 0, "public_config.confirmation_threshold"),
        (("seller", "allowance_minor"), "-1", "seller.allowance_minor"),
    ],
)
def test_a_bad_field_is_named_and_not_repeated(
    path: tuple[str, ...], value: Any, location: str
) -> None:
    document = body()
    target = document
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    assert location in refused(document)


def test_an_unknown_field_is_refused() -> None:
    document = body()
    document["buyer"]["mandate"]["valid_until"] = 1
    assert "buyer.mandate.valid_until" in refused(document)


def test_a_model_policy_names_its_model() -> None:
    document = body()
    document["seller"]["policy"] = "model"
    assert "seller" in refused(document)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("public_config", "confirmation_threshold"), "2"),
        (("public_config", "confirmation_threshold"), True),
        (("public_config", "max_offers"), 8.0),
        (("public_config", "session_duration_s"), "1800"),
        (("limits", "repair_attempts"), False),
    ],
)
def test_strings_and_floats_are_not_coerced_into_integers(
    path: tuple[str, ...], value: Any
) -> None:
    """ADR-059: the threshold is an integer of at least 1, and `"2"` or `true` is not one."""
    document = body()
    document[path[0]][path[1]] = value
    assert ".".join(path) in refused(document)


def test_a_parent_run_id_arrives_as_a_json_string() -> None:
    document = body()
    document["parent_run_id"] = "5b0c7c1e-5e5e-4c1e-9c1e-5e5e5e5e5e5e"
    assert str(RunRequest.parse(document).parent_run_id) == document["parent_run_id"]
