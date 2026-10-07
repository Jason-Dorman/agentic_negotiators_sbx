"""The outbound-context assertion (stage 3.3, ADR-092): what it refuses, and what it lets through.

The denylist is this instance's own: its secrets matched exactly, its key material in hex of any
case with or without `0x`, and credential-shaped text (Q88, Q89). Public data — an observation full
of 64-hex digests, a rendered system prompt — passes. A hit raises before the wrapped client is
called, so nothing is counted, priced or sent, and the log names the kind and never the text.
"""

from __future__ import annotations

import io
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import structlog
from agent_fakes import FakeModelClient, KeyedKeyHolder
from agent_observations import MANDATES, observation
from pydantic import SecretStr

from agent.errors import OutboundContextRefusedError
from agent.keys import KeyDerivation, RootKeyHolder
from agent.keys.derivation import derive_run_key
from agent.logs import configure_logging
from agent.main import instance_check, load_model_runtime
from agent.model import (
    CheckedModelClient,
    DecisionEnvelope,
    ModelCallConfig,
    ModelOutcome,
    ModelResult,
    request_body,
)
from agent.outbound import (
    CREDENTIAL_MASK,
    CREDENTIAL_PATTERNS,
    OutboundCheck,
    credential_kind,
    mask_credentials,
)
from agent.prompting import PromptTemplate
from agent.settings import AgentSettings

ROOT = bytes.fromhex("3c" * 16 + "a5" * 16)
RUN = UUID("6f1c2a4e-9d7b-4c3a-8e2f-1a2b3c4d5e6f")
OTHER_RUN = UUID("00000000-0000-4000-8000-000000000002")
SECRET = "the-shared-secret-of-agent-a-is-long-enough"
MODEL_KEY = "sk-ant-api03-" + "k" * 40
CONFIG = ModelCallConfig("claude-sonnet-5-5", "high", 16_000, 45.0)
TEMPLATE = PromptTemplate.load(DecisionEnvelope)


def run_key(run_id: UUID = RUN) -> bytes:
    return derive_run_key(ROOT, KeyDerivation(31337, "buyer", run_id))


def holder() -> RootKeyHolder:
    return RootKeyHolder("env:BUYER_ROOT_KEY", ROOT)


def public_request() -> dict[str, Any]:
    """A real request: the rendered system prompt and an observation full of public digests."""
    document = observation("buyer", history=[])
    return request_body(
        CONFIG,
        TEMPLATE.system_prompt("buyer", str(MANDATES["buyer"]["instructions"])),
        json.dumps(document),
        DecisionEnvelope,
        TEMPLATE.repair_message("above_reservation", "The offer is above your bound."),
    )


# --------------------------------------------------------------------------------------
# The patterns
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        ("my key is sk-ant-" + "api03-abcDEF_123-x", "anthropic_key_shaped"),
        ("-----BEGIN PRIVATE" + " KEY-----\nMIIE", "pem_private_key"),
        ("-----BEGIN EC PRIVATE" + " KEY-----", "pem_private_key"),
        ("-----BEGIN OPENSSH PRIVATE" + " KEY-----", "pem_private_key"),
        ('{"crypto": {"ciphertext": "ab"}}', "keystore_document"),
        ('{"kdfparams" : {}}', "keystore_document"),
        ('{\\"crypto\\": {\\"ciphertext\\": \\"ab\\"}}', "keystore_document"),
    ],
)
def test_each_credential_shape_is_named(text: str, kind: str) -> None:
    assert credential_kind(text) == kind
    assert OutboundCheck().violation({"messages": [{"content": text}]}) == kind


@pytest.mark.parametrize(
    "text",
    [
        "sk-ant-",  # the prefix alone, with nothing after it
        "-----BEGIN PUBLIC KEY-----",
        "the ciphertext of a keystore",
        "0x" + "ab" * 32,  # a digest: public, and exactly a private key's shape (Q89)
        "AB" * 32,
    ],
)
def test_what_is_not_a_credential_passes(text: str) -> None:
    assert credential_kind(text) is None
    assert OutboundCheck().violation({"text": text}) is None


def test_every_pattern_has_a_case_above() -> None:
    assert set(CREDENTIAL_PATTERNS) == {
        "anthropic_key_shaped",
        "pem_private_key",
        "keystore_document",
    }


def test_masking_replaces_only_the_credential_shaped_run() -> None:
    assert mask_credentials("a sk-ant-api03-xyz b") == f"a {CREDENTIAL_MASK} b"
    assert mask_credentials("plain") == "plain"
    assert (
        credential_kind(mask_credentials('x"ciphertext":-----BEGIN RSA PRIVATE' + " KEY-----"))
        is None
    )


# --------------------------------------------------------------------------------------
# The denylist
# --------------------------------------------------------------------------------------


def test_a_real_request_with_public_digests_passes_the_whole_check() -> None:
    keys = holder()
    signer = keys.signer_for(KeyDerivation(31337, "buyer", RUN))
    check = (
        OutboundCheck({"shared_secret": SecretStr(SECRET), "model_key": SecretStr(MODEL_KEY)})
        .with_probe("instance_key", keys)
        .for_run(signer)
    )
    request = public_request()
    assert "0x" in json.dumps(request)  # the session id, the config hash: 64-hex, and public
    assert check.violation(request) is None


@pytest.mark.parametrize("kind", ["shared_secret", "model_key", "keystore_password"])
def test_each_exact_secret_is_refused_by_its_kind(kind: str) -> None:
    value = {"shared_secret": SECRET, "model_key": MODEL_KEY, "keystore_password": "pw-1234"}[kind]
    check = OutboundCheck({kind: SecretStr(value)})
    assert check.violation({"system": [{"text": f"before {value} after"}]}) == kind
    # Exactly, and whole: a truncated model key is still credential-shaped, but not the key.
    assert check.violation({"system": [{"text": value[:-1]}]}) != kind


def test_an_empty_secret_is_left_out_rather_than_matching_everything() -> None:
    check = OutboundCheck({"keystore_password": SecretStr("")})
    assert check.violation({"text": "anything"}) is None
    assert "keystore_password" not in check.kinds


@pytest.mark.parametrize(
    "form",
    [
        lambda key: key.hex(),
        lambda key: key.hex().upper(),
        lambda key: "0x" + key.hex(),
        lambda key: "0X" + key.hex().upper(),
        lambda key: f"feedback quoting {key.hex()} mid-sentence",
    ],
    ids=["lower", "upper", "0x", "0X", "inside"],
)
def test_key_material_is_found_in_any_hex_form(form: Any) -> None:
    keys = holder()
    signer = keys.signer_for(KeyDerivation(31337, "buyer", RUN))
    check = OutboundCheck().with_probe("instance_key", keys).for_run(signer)
    assert check.violation({"text": form(ROOT)}) == "instance_key"
    assert check.violation({"text": form(run_key())}) == "run_key"


def test_part_of_a_key_is_not_a_match() -> None:
    keys = holder()
    assert not keys.appears_in(ROOT.hex()[:-1])
    assert not keys.appears_in(ROOT.hex()[1:])
    assert keys.appears_in(ROOT.hex())


def test_another_runs_key_on_this_instance_is_found_while_that_run_holds_it() -> None:
    keys = holder()
    mine = keys.signer_for(KeyDerivation(31337, "buyer", RUN))
    other = keys.signer_for(KeyDerivation(31337, "buyer", OTHER_RUN))
    check = OutboundCheck().with_probe("instance_key", keys).for_run(mine)
    assert check.violation({"text": run_key(OTHER_RUN).hex()}) == "instance_key"
    del other  # the run is released and its signer goes with it
    assert check.violation({"text": run_key(OTHER_RUN).hex()}) is None


def test_object_keys_are_checked_as_well_as_values() -> None:
    check = OutboundCheck({"shared_secret": SecretStr(SECRET)})
    assert check.violation({"messages": [{SECRET: 1}]}) == "shared_secret"
    assert check.violation(("a", ["b", {"c": SECRET}])) == "shared_secret"


def test_the_check_shows_its_kinds_and_never_a_secret() -> None:
    keys = holder()
    check = OutboundCheck({"shared_secret": SecretStr(SECRET)}).with_probe("instance_key", keys)
    text = repr(check) + str(check)
    assert SECRET not in text and ROOT.hex() not in text.lower()
    assert "shared_secret" in text and "instance_key" in text


# --------------------------------------------------------------------------------------
# The composition root's denylist
# --------------------------------------------------------------------------------------


def settings(**overrides: Any) -> AgentSettings:
    values: dict[str, Any] = {
        "role": "buyer",
        "instance": "agent-a",
        "root_key_ref": "env:BUYER_ROOT_KEY",
        "shared_secret": SECRET,
        "port": 8101,
        **overrides,
    }
    return AgentSettings(**values)


def test_the_instance_check_holds_the_shared_secret_and_the_root() -> None:
    check = instance_check(settings(), {}, holder())
    assert check.kinds[:2] == ("shared_secret", "instance_key")
    assert check.violation({"t": SECRET}) == "shared_secret"
    assert check.violation({"t": ROOT.hex()}) == "instance_key"


def test_a_keystore_roots_password_is_denied_and_an_env_roots_variable_is_not_read() -> None:
    environ = {"KEYSTORE_PASSWORD": "a-keystore-password"}
    keystore = instance_check(settings(root_key_ref="keystore:/keys/buyer.json"), environ, None)
    assert keystore.violation({"t": "a-keystore-password"}) == "keystore_password"
    env_root = instance_check(settings(), environ, None)
    assert "keystore_password" not in env_root.kinds


def test_a_live_instance_adds_its_model_key_and_a_fixture_instance_has_none() -> None:
    live = settings(model_key_ref="env:BUYER_ANTHROPIC_API_KEY")
    runtime, problem = load_model_runtime(
        live, {"BUYER_ANTHROPIC_API_KEY": MODEL_KEY}, instance_check(live, {}, None)
    )
    assert problem is None and runtime is not None
    assert runtime.check.violation({"t": MODEL_KEY}) == "model_key"

    fixtures = Path(__file__).resolve().parents[4] / "tests/fixtures/model_responses"
    canned = settings(model_fixtures=fixtures / "default-overlap-settles")
    runtime, problem = load_model_runtime(canned, {}, instance_check(canned, {}, None))
    assert problem is None and runtime is not None
    assert "model_key" not in runtime.check.kinds


# --------------------------------------------------------------------------------------
# The send path
# --------------------------------------------------------------------------------------


@pytest.fixture
def log_stream() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    configure_logging(level="DEBUG", instance="agent-a", role="buyer", stream=stream)
    yield stream
    structlog.reset_defaults()
    structlog.contextvars.clear_contextvars()


def decided() -> ModelResult:
    return ModelResult(ModelOutcome.DECIDED, CONFIG.model_id, decision={"decision": {}}, sent=True)


async def test_a_refused_request_reaches_neither_the_client_nor_the_observer(
    log_stream: io.StringIO,
) -> None:
    inner = FakeModelClient(decided())
    seen: list[Any] = []
    client = CheckedModelClient(
        inner, CONFIG, OutboundCheck({"shared_secret": SecretStr(SECRET)}), seen.append
    )
    with pytest.raises(OutboundContextRefusedError) as raised:
        await client.decide("system", json.dumps({"leak": SECRET}), DecisionEnvelope)
    assert raised.value.code == "outbound_context_refused"
    assert raised.value.status == 422
    assert raised.value.details == {"kind": "shared_secret"}
    assert SECRET not in str(raised.value)
    assert inner.calls == [] and seen == []

    lines = [json.loads(line) for line in log_stream.getvalue().splitlines() if line.strip()]
    [line] = [line for line in lines if line["event"] == "outbound_context_refused"]
    assert line["kind"] == "shared_secret"
    assert SECRET not in log_stream.getvalue()


async def test_a_refusal_in_a_repair_block_is_found_too() -> None:
    inner = FakeModelClient(decided())
    client = CheckedModelClient(inner, CONFIG, OutboundCheck())
    with pytest.raises(OutboundContextRefusedError):
        await client.decide("system", "{}", DecisionEnvelope, repair="sk-ant-api03-zzz")
    assert inner.calls == []


async def test_a_passing_request_is_observed_as_sent_and_handed_on_unchanged() -> None:
    inner = FakeModelClient(decided())
    seen: list[Any] = []
    client = CheckedModelClient(inner, CONFIG, OutboundCheck(), seen.append)
    result = await client.decide(
        "system", '{"o":1}', DecisionEnvelope, repair="fix it", within_s=3.5
    )
    assert result.outcome is ModelOutcome.DECIDED
    [call] = inner.calls
    assert (call.system_prompt, call.observation, call.repair, call.within_s) == (
        "system",
        '{"o":1}',
        "fix it",
        3.5,
    )
    assert seen == [
        {
            **request_body(CONFIG, "system", '{"o":1}', DecisionEnvelope, "fix it"),
            "max_tokens": 16_000,
        }
    ]


def test_the_fake_key_holder_has_no_root_to_find() -> None:
    assert not KeyedKeyHolder().appears_in("anything")


# --------------------------------------------------------------------------------------
# What the stage 3.3 adversarial review found
# --------------------------------------------------------------------------------------

#: Fragments a model could put in a field name, alone or together, to make the validator's own
#: quoting, the repair template or JSON escaping complete a credential shape.
_FRAGMENTS = (
    "ciphertext",
    "kdfparams",
    '"',
    '":',
    '" :',
    '\\"',
    ":",
    "sk-ant",
    "sk-ant-",
    "-----BEGIN RSA PRIVATE" + " KEY-----",
    "-----BEGIN ",
    "PRIVATE KEY-----",
    "x",
    ",",
    " ",
)


def _names() -> Iterator[tuple[str, ...]]:
    for first in _FRAGMENTS:
        for second in _FRAGMENTS:
            yield (first + second,)
            yield (first, second)
    yield ('crypto": {"ciphertext', '": 1')


def test_no_field_name_a_model_writes_can_trip_the_check_through_its_feedback() -> None:
    """Q92 as amended: the feedback, the repair message built from it, and the next observation
    carrying it as JSON are all clean, whatever names the model wrote."""
    from agent.validation.structure import parse_decision

    for names in _names():
        response: dict[str, Any] = {"decision": {"action": "offer", "quote_amount_minor": "1"}}
        response.update({name: 1 for name in names if name not in ("decision", "explanation")})
        result = parse_decision(response)
        if result.ok:
            continue
        feedback = result.outcome.feedback  # type: ignore[union-attr]  # reason: refused above
        repair = TEMPLATE.repair_message("extra_fields", feedback)
        previous = json.dumps({"my_previous_decisions": [{"feedback": feedback}]})
        for text in (feedback, repair, previous, json.dumps(previous)):
            assert OutboundCheck().violation({"text": text}) is None, (names, text)


@pytest.mark.parametrize(
    "secret",
    [
        'a-shared-secret-with-a-"quote"-in-it-0123',
        "a-shared-secret-with-a-\\-in-it-012345",
        "é" * 32,
    ],
    ids=["quote", "backslash", "non_ascii"],
)
def test_a_secret_with_characters_json_escapes_is_found_inside_an_observation(secret: str) -> None:
    """Q95: the observation reaches the request as JSON text, where such a secret is escaped."""
    check = OutboundCheck({"shared_secret": SecretStr(secret)})
    for ascii_only in (False, True):
        observation_text = json.dumps({"feedback": f"leaked {secret}"}, ensure_ascii=ascii_only)
        assert check.violation({"messages": [{"content": observation_text}]}) == "shared_secret"
        twice = json.dumps(observation_text, ensure_ascii=ascii_only)
        assert check.violation({"text": twice}) == "shared_secret"


@pytest.mark.parametrize(
    ("password", "offered"),
    [("short-pass1", False), ("a-12-char-pw", True), ("", True)],
    ids=["eleven", "twelve", "unset"],
)
def test_a_keystore_password_under_twelve_characters_leaves_the_model_unoffered(
    password: str, offered: bool
) -> None:
    """Q95: a short password would be found in ordinary text and refuse every model request."""
    fixtures = Path(__file__).resolve().parents[4] / "tests/fixtures/model_responses"
    keystore = settings(
        root_key_ref="keystore:/keys/buyer.json",
        model_fixtures=fixtures / "default-overlap-settles",
    )
    environ = {"KEYSTORE_PASSWORD": password} if password else {}
    runtime, problem = load_model_runtime(
        keystore, environ, instance_check(keystore, environ, None)
    )
    assert (runtime is not None) is offered
    if not offered:
        assert problem is not None and "shorter than 12 characters" in problem
        assert password not in problem
    # An env: root never reads the password at all.
    env_root = settings(model_fixtures=fixtures / "default-overlap-settles")
    assert load_model_runtime(env_root, environ)[0] is not None


def test_a_pem_header_with_digits_in_its_key_type_is_found() -> None:
    assert credential_kind("-----BEGIN ED25519 PRIVATE" + " KEY-----") == "pem_private_key"


def test_a_secret_is_named_before_key_material_found_in_the_same_text() -> None:
    keys = holder()
    check = OutboundCheck({"shared_secret": SecretStr(SECRET)}).with_probe("instance_key", keys)
    assert check.violation({"text": f"{ROOT.hex()} {SECRET}"}) == "shared_secret"


def test_the_check_lists_its_patterns_among_its_kinds() -> None:
    assert set(CREDENTIAL_PATTERNS) <= set(OutboundCheck().kinds)


async def test_the_observer_sees_a_passing_request_even_when_the_client_then_fails() -> None:
    """The capture is of what left the check, so a client that raises cannot hide a request."""

    class Exploding(FakeModelClient):
        async def decide(self, *args: Any, **kwargs: Any) -> ModelResult:
            raise RuntimeError("the client failed after the request left")

    seen: list[Any] = []
    client = CheckedModelClient(Exploding(), CONFIG, OutboundCheck(), seen.append)
    with pytest.raises(RuntimeError):
        await client.decide("system", "{}", DecisionEnvelope)
    assert len(seen) == 1
