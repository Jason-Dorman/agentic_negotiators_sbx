"""Instance configuration from the environment, and the log redaction pass."""

from __future__ import annotations

import io
import json
from collections.abc import Iterator

import pytest
import structlog
from pydantic import ValidationError

from agent.logs import REDACTED, configure_logging, redact
from agent.main import load_key_holder
from agent.settings import AgentSettings, SettingsError, load_settings

SECRET = "x" * 32
ENVIRONMENT = {
    "AGENT_ROLE": "seller",
    "AGENT_INSTANCE": "agent-b",
    "AGENT_ROOT_KEY_REF": "keystore:/run/secrets/seller-root.json",
    "AGENT_SHARED_SECRET": SECRET,
    "AGENT_PORT": "8102",
}


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    yield


def test_settings_come_from_agent_prefixed_variables(environment: None) -> None:
    settings = AgentSettings()  # type: ignore[call-arg]  # reason: every field comes from the environment
    assert settings.role == "seller"
    assert settings.instance == "agent-b"
    assert settings.root_key_ref == "keystore:/run/secrets/seller-root.json"
    assert settings.port == 8102
    assert settings.host == "127.0.0.1"
    assert settings.setup_gas_limit_max == 100_000
    assert settings.shared_secret.get_secret_value() == SECRET
    assert SECRET not in repr(settings)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AGENT_SHARED_SECRET", "x" * 31),
        ("AGENT_ROOT_KEY_REF", "0x" + "11" * 32),
        ("AGENT_ROLE", "operator"),
        ("AGENT_PORT", "0"),
        ("AGENT_INSTANCE", "Agent A"),
        ("AGENT_SETUP_GAS_LIMIT_MAX", "0"),
    ],
    ids=[
        "short secret",
        "a key where a reference belongs",
        "no such role",
        "port 0",
        "instance name",
        "zero gas bound",
    ],
)
def test_a_bad_value_stops_the_instance_starting(
    environment: None, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(SettingsError, match=name):
        load_settings()


def test_a_key_pasted_where_its_reference_belongs_is_not_printed_back(
    environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pydantic's own message would quote it; the start-up error must not."""
    key = "0x" + "4f" * 32
    monkeypatch.setenv("AGENT_ROOT_KEY_REF", key)
    with pytest.raises(ValidationError) as raw:
        AgentSettings()  # type: ignore[call-arg]  # reason: every field comes from the environment
    assert "4f4f4f4f" in str(raw.value), "the reason load_settings exists"
    with pytest.raises(SettingsError) as refused:
        load_settings()
    assert "4f4f" not in str(refused.value)
    assert "AGENT_ROOT_KEY_REF" in str(refused.value)


def test_a_short_secret_is_not_printed_back(
    environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_SHARED_SECRET", "q" * 31)
    with pytest.raises(SettingsError) as refused:
        load_settings()
    assert "qqqq" not in str(refused.value)


def test_an_unset_secret_stops_the_instance_starting(
    environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("AGENT_SHARED_SECRET")
    with pytest.raises(SettingsError, match="AGENT_SHARED_SECRET"):
        load_settings()


def test_a_bad_root_reference_leaves_no_key_holder_and_a_reason() -> None:
    keys, problem = load_key_holder("env:NOWHERE", {})
    assert keys is None
    assert problem == "the environment variable NOWHERE is not set"


# --------------------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------------------


def test_private_fields_are_redacted_wherever_they_are() -> None:
    event = redact(
        None,
        "info",
        {
            "event": "turn",
            "mandate": {"reservation_price_minor": "100000000"},
            "reservation_price_minor": "100000000",
            "details": {"instructions": "never pay more", "raw_response": {"decision": {}}},
            "items": [{"validation_feedback": "too high"}, {"ok": True}],
            "shared_secret": SECRET,
            "keystore_password": "hunter2",
            "private_key": "0x" + "ab" * 32,
        },
    )
    assert event["mandate"] == REDACTED
    assert event["reservation_price_minor"] == REDACTED
    assert event["details"] == {"instructions": REDACTED, "raw_response": REDACTED}
    assert event["items"] == [{"validation_feedback": REDACTED}, {"ok": True}]
    assert event["shared_secret"] == REDACTED
    assert event["keystore_password"] == REDACTED
    assert event["private_key"] == REDACTED


def test_a_credential_inside_a_string_is_redacted() -> None:
    fake_credential = "sk-" + "ant-api03-AbC_123-x"  # assembled so the secret scan sees no shape
    event = redact(None, "error", {"event": f"failed with {fake_credential} in the header"})
    assert event["event"] == f"failed with {REDACTED} in the header"


def test_the_key_reference_is_kept_because_it_is_not_a_secret() -> None:
    event = redact(None, "info", {"key_ref": "env:BUYER_ROOT_KEY", "run_id": "r", "status": 200})
    assert event == {"key_ref": "env:BUYER_ROOT_KEY", "run_id": "r", "status": 200}


def test_log_lines_are_json_with_the_instance_bound() -> None:
    stream = io.StringIO()
    try:
        configure_logging(level="info", instance="agent-a", role="buyer", stream=stream)
        structlog.get_logger(component="test").info("hello", instructions="be secret")
    finally:
        structlog.reset_defaults()
        structlog.contextvars.clear_contextvars()
    line = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert line["event"] == "hello"
    assert line["instance"] == "agent-a" and line["role"] == "buyer"
    assert line["component"] == "test"
    assert line["instructions"] == REDACTED
    assert line["level"] == "info"


def test_an_unknown_log_level_falls_back_to_info() -> None:
    stream = io.StringIO()
    try:
        configure_logging(level="chatty", instance="agent-a", role="buyer", stream=stream)
        structlog.get_logger().debug("hidden")
        structlog.get_logger().info("shown")
    finally:
        structlog.reset_defaults()
        structlog.contextvars.clear_contextvars()
    assert "hidden" not in stream.getvalue()
    assert "shown" in stream.getvalue()


@pytest.mark.parametrize(
    "ref",
    ["env:BUYER_ROOT_KEY=0x" + "4f" * 32, "env:0x" + "4f" * 32, "keystore:" + "4f" * 32],
)
def test_a_key_in_any_part_of_the_reference_stops_start_up_unrepeated(
    environment: None, monkeypatch: pytest.MonkeyPatch, ref: str
) -> None:
    """ADR-049: the grammar, not only the prefix. The first version accepted all three."""
    monkeypatch.setenv("AGENT_ROOT_KEY_REF", ref)
    with pytest.raises(SettingsError) as refused:
        load_settings()
    assert "4f4f" not in str(refused.value)


def test_the_setup_cost_bound_defaults_to_one_hundredth_of_an_ether(environment: None) -> None:
    settings = AgentSettings()  # type: ignore[call-arg]  # reason: every field comes from the environment
    assert settings.setup_max_cost_wei == 10**16


def test_an_object_is_logged_as_its_type_never_its_repr() -> None:
    """A Validation or a Repair carries private feedback in its fields; its repr would print it."""
    from agent.policy import Repair

    event = redact(None, "info", {"event": "x", "attempt": Repair("above_reservation", "R=1")})
    assert event["attempt"] == "<Repair>"
    assert redact(None, "info", {"n": 3, "ok": True, "none": None}) == {
        "n": 3,
        "ok": True,
        "none": None,
    }


def test_standard_library_records_go_through_the_same_redaction() -> None:
    import logging

    stream = io.StringIO()
    try:
        configure_logging(level="info", instance="agent-a", role="buyer", stream=stream)
        # Assembled at run time so no committed line carries a credential's shape for the scan.
        fake_credential = "sk-" + "ant-api03-" + "AbCdEf0123456789xyz"
        logging.getLogger("uvicorn.error").info(f"started with {fake_credential}")
    finally:
        structlog.reset_defaults()
        structlog.contextvars.clear_contextvars()
        logging.getLogger().handlers = []
    line = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert line["event"] == f"started with {REDACTED}"
    assert line["instance"] == "agent-a"


def test_private_dataclasses_hide_their_values_in_a_repr() -> None:
    from agent_observations import observation, typed

    from agent.observation import Mandate

    mandate = Mandate.from_json(
        {
            "reservation_price_minor": "123456789",
            "min_remaining_inventory_minor": "0",
            "instructions": "never above 123456789",
        }
    )
    view = typed(observation("buyer"))
    for text in (repr(mandate), repr(view), str(view)):
        assert "123456789" not in text and "100000000" not in text
        assert "instructions" not in text
