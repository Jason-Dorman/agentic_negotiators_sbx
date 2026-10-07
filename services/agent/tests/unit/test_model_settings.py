"""The model's configuration: its key by reference (ADR-086), `max_tokens` (ADR-087), the price
table and unknown prices (FR-A9)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import SecretStr

from agent.model import ModelKeyError, resolve_model_key
from agent.settings import AgentSettings, SettingsError, load_settings

KEY = "sk-ant-api03-" + "z" * 40
ENVIRONMENT = {
    "AGENT_ROLE": "buyer",
    "AGENT_INSTANCE": "agent-a",
    "AGENT_ROOT_KEY_REF": "env:BUYER_ROOT_KEY",
    "AGENT_SHARED_SECRET": "x" * 32,
    "AGENT_PORT": "8101",
}


@pytest.fixture
def environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    for name, value in ENVIRONMENT.items():
        monkeypatch.setenv(name, value)
    for name in (
        "AGENT_MODEL_KEY_REF",
        "AGENT_MODEL_MAX_TOKENS",
        "AGENT_MODEL_PRICE_TABLE",
        "AGENT_ALLOW_UNKNOWN_PRICE",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


def test_an_instance_with_no_model_settings_has_the_defaults(environment: None) -> None:
    settings = AgentSettings()  # type: ignore[call-arg]  # reason: every field comes from the environment
    assert settings.model_key_ref is None
    assert settings.model_max_tokens == 16_000
    assert settings.model_price_table is None
    assert settings.allow_unknown_price is False


def test_the_model_settings_come_from_agent_prefixed_variables(
    environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_MODEL_KEY_REF", "env:BUYER_ANTHROPIC_API_KEY")
    monkeypatch.setenv("AGENT_MODEL_MAX_TOKENS", "8000")
    monkeypatch.setenv("AGENT_MODEL_PRICE_TABLE", "/etc/negotiation/model_prices.json")
    monkeypatch.setenv("AGENT_ALLOW_UNKNOWN_PRICE", "true")
    settings = AgentSettings()  # type: ignore[call-arg]  # reason: every field comes from the environment
    assert settings.model_key_ref == "env:BUYER_ANTHROPIC_API_KEY"
    assert settings.model_max_tokens == 8_000
    assert settings.model_price_table == Path("/etc/negotiation/model_prices.json")
    assert settings.allow_unknown_price is True


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AGENT_MODEL_KEY_REF", "keystore:/run/secrets/buyer-anthropic.json"),
        ("AGENT_MODEL_KEY_REF", "BUYER_ANTHROPIC_API_KEY"),
        ("AGENT_MODEL_KEY_REF", "env:buyer_anthropic_api_key"),
        ("AGENT_MODEL_MAX_TOKENS", "0"),
        ("AGENT_MODEL_MAX_TOKENS", "128001"),
    ],
    ids=["a keystore", "no scheme", "lower-case name", "zero max_tokens", "beyond 128K"],
)
def test_a_bad_model_setting_stops_the_instance_starting(
    environment: None, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)
    with pytest.raises(SettingsError, match=name):
        load_settings()


@pytest.mark.parametrize("pasted", [KEY, f"env:{KEY}"], ids=["bare", "behind env:"])
def test_a_key_pasted_where_its_reference_belongs_is_not_printed_back(
    environment: None, monkeypatch: pytest.MonkeyPatch, pasted: str
) -> None:
    monkeypatch.setenv("AGENT_MODEL_KEY_REF", pasted)
    with pytest.raises(SettingsError) as refused:
        load_settings()
    assert "zzzz" not in str(refused.value)
    assert "AGENT_MODEL_KEY_REF" in str(refused.value)


def test_the_key_is_read_from_the_variable_its_reference_names() -> None:
    key = resolve_model_key("env:BUYER_ANTHROPIC_API_KEY", {"BUYER_ANTHROPIC_API_KEY": KEY})
    assert isinstance(key, SecretStr)
    assert key.get_secret_value() == KEY
    assert KEY not in repr(key) and KEY not in str(key)


@pytest.mark.parametrize(
    ("key_ref", "environ", "message"),
    [
        ("env:BUYER_ANTHROPIC_API_KEY", {}, "BUYER_ANTHROPIC_API_KEY is unset or empty"),
        ("env:BUYER_ANTHROPIC_API_KEY", {"BUYER_ANTHROPIC_API_KEY": "  "}, "unset or empty"),
        ("keystore:/run/secrets/key.json", {}, "of the form env:NAME"),
        (KEY, {}, "of the form env:NAME"),
    ],
    ids=["unset", "blank", "a keystore", "a pasted key"],
)
def test_a_key_that_cannot_be_read_is_refused_without_quoting_a_value(
    key_ref: str, environ: dict[str, str], message: str
) -> None:
    with pytest.raises(ModelKeyError, match=message) as refused:
        resolve_model_key(key_ref, environ)
    assert "zzzz" not in str(refused.value)


@pytest.mark.parametrize(
    "name",
    [
        "AGENT_SHARED_SECRET",
        "AGENT_A_SHARED_SECRET",
        "BUYER_ROOT_KEY",
        "SELLER_ROOT_KEY",
        "OPERATOR_PRIVATE_KEY",
        "RELAY_PRIVATE_KEY",
        "KEYSTORE_PASSWORD",
    ],
)
def test_a_reference_to_another_secret_of_the_deployment_is_refused(
    environment: None, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    """A one-word slip would send a signing key or the HMAC secret to the provider (ADR-086)."""
    monkeypatch.setenv("AGENT_MODEL_KEY_REF", f"env:{name}")
    with pytest.raises(SettingsError, match="AGENT_MODEL_KEY_REF") as refused:
        load_settings()
    assert "other secrets" in str(refused.value)
    with pytest.raises(ModelKeyError, match="other secrets"):
        resolve_model_key(f"env:{name}", {name: KEY})


def test_the_variable_the_root_reference_names_is_refused_whatever_it_is_called(
    environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_ROOT_KEY_REF", "env:BUYER_SIGNING_SEED")
    monkeypatch.setenv("AGENT_MODEL_KEY_REF", "env:BUYER_SIGNING_SEED")
    with pytest.raises(SettingsError, match="AGENT_MODEL_KEY_REF") as refused:
        load_settings()
    assert "AGENT_ROOT_KEY_REF" in str(refused.value)


@pytest.mark.parametrize(
    "value", ["0x" + "4f" * 32, "not-a-key", "sk-other-" + "z" * 40], ids=["hex", "word", "prefix"]
)
def test_a_value_that_is_not_an_anthropic_key_is_refused_without_being_quoted(value: str) -> None:
    with pytest.raises(ModelKeyError, match="does not hold an Anthropic API key") as refused:
        resolve_model_key("env:BUYER_ANTHROPIC_API_KEY", {"BUYER_ANTHROPIC_API_KEY": value})
    assert value not in str(refused.value)
    assert "4f4f" not in str(refused.value) and "zzzz" not in str(refused.value)


def test_an_empty_price_table_variable_is_unset_not_the_working_directory(
    environment: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`infra/.env.example` leaves `AGENT_MODEL_PRICE_TABLE=` empty."""
    monkeypatch.setenv("AGENT_MODEL_PRICE_TABLE", "")
    settings = AgentSettings()  # type: ignore[call-arg]  # reason: every field comes from the environment
    assert settings.model_price_table is None
