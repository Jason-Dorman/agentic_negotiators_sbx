"""The agent internal API's request MAC (ADR-041).

The layout is spelled out byte for byte in the first test rather than recomputed through the module,
so a change to it cannot pass by changing both sides at once. The rest pin the properties ADR-041
exists for: a MAC made for one method, path or body is refused for any other, which is what stops a
captured health check from authorising a release.
"""

from __future__ import annotations

import hashlib
import hmac

import pytest

from negotiation_protocol.agent_auth import (
    agent_request_mac,
    agent_request_message,
    verify_agent_request,
)

SECRET = b"an-instance-shared-secret-of-32-characters"
TURN_PATH = "/internal/runs/6f1c2a4e-9d7b-4c3a-8e2f-1a2b3c4d5e6f/turn"


def test_the_mac_is_hmac_sha256_over_method_path_and_body_as_lowercase_hex() -> None:
    body = b'{"turn":1}'
    expected = hmac.new(SECRET, b"POST\n" + TURN_PATH.encode() + b"\n" + body, hashlib.sha256)
    assert agent_request_mac(SECRET, "POST", TURN_PATH, body) == expected.hexdigest()
    assert agent_request_mac(SECRET, "post", TURN_PATH, body) == expected.hexdigest()


def test_the_message_layout_is_method_newline_path_newline_body() -> None:
    assert agent_request_message("get", "/internal/health", b"") == b"GET\n/internal/health\n"


def test_a_health_check_mac_does_not_authorise_a_release() -> None:
    """Both bodies are empty. Under a body-only MAC these two were the same value."""
    health = agent_request_mac(SECRET, "GET", "/internal/health", b"")
    release_path = "/internal/runs/6f1c2a4e-9d7b-4c3a-8e2f-1a2b3c4d5e6f/release"
    assert not verify_agent_request(SECRET, "POST", release_path, b"", health)


def test_a_provisioning_mac_does_not_move_to_another_run() -> None:
    body = b'{"role":"buyer"}'
    run_a = "/internal/runs/00000000-0000-4000-8000-00000000000a/provision"
    run_b = "/internal/runs/00000000-0000-4000-8000-00000000000b/provision"
    mac = agent_request_mac(SECRET, "POST", run_a, body)
    assert verify_agent_request(SECRET, "POST", run_a, body, mac)
    assert not verify_agent_request(SECRET, "POST", run_b, body, mac)


@pytest.mark.parametrize(
    ("method", "path", "body", "secret"),
    [
        ("PUT", TURN_PATH, b"{}", SECRET),
        ("POST", TURN_PATH + "x", b"{}", SECRET),
        ("POST", TURN_PATH, b"{} ", SECRET),
        ("POST", TURN_PATH, b"{}", SECRET + b"!"),
    ],
)
def test_any_change_to_the_request_or_the_secret_is_refused(
    method: str, path: str, body: bytes, secret: bytes
) -> None:
    mac = agent_request_mac(SECRET, "POST", TURN_PATH, b"{}")
    assert not verify_agent_request(secret, method, path, body, mac)


@pytest.mark.parametrize("presented", [None, "", "zz", "é" * 64])
def test_a_missing_or_malformed_header_is_refused_without_raising(presented: str | None) -> None:
    assert not verify_agent_request(SECRET, "GET", "/internal/health", b"", presented)


def test_upper_case_hex_is_not_the_canonical_form_and_is_refused() -> None:
    mac = agent_request_mac(SECRET, "GET", "/internal/health", b"")
    assert not verify_agent_request(SECRET, "GET", "/internal/health", b"", mac.upper())


def test_an_empty_secret_is_refused_rather_than_authenticating_everything() -> None:
    with pytest.raises(ValueError, match="empty shared secret"):
        agent_request_mac(b"", "GET", "/internal/health", b"")


@pytest.mark.parametrize(("method", "path"), [("GET\n", "/x"), ("GET", "/x\nPOST")])
def test_a_newline_cannot_be_smuggled_into_the_method_or_path(method: str, path: str) -> None:
    with pytest.raises(ValueError, match="newline"):
        agent_request_message(method, path, b"")
