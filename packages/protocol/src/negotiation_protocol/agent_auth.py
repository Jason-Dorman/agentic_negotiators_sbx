"""Authentication of the agent internal API (docs/api_contract.md section 6, ADR-041).

Every request the backend makes to an agent service carries `X-Agent-Auth`: the HMAC-SHA256, under
that instance's shared secret, of the request's **method, path and body**, as lowercase hex. The
backend's `agent_client` computes it and the agent verifies it, so the byte layout lives here, once,
where both import it.

Binding the method and path as well as the body is the point of ADR-041. A MAC over the body alone
is the same for every empty body, so a captured `GET /internal/health` would have authorised
`POST /internal/runs/{any run}/release`, and a captured provisioning body, which carries no run id,
could have been replayed against a different run's path. Under this layout a MAC is valid for one
route, one run and one body.

It is not replay protection for an identical request. Every agent endpoint is idempotent for an
identical request, so replaying one returns what the backend already received; the property that
matters is that a MAC cannot be moved to a request it was not made for.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Final

AUTH_HEADER: Final = "X-Agent-Auth"
REQUEST_ID_HEADER: Final = "X-Request-Id"


def agent_request_message(method: str, path: str, body: bytes) -> bytes:
    """The authenticated bytes: `METHOD`, a newline, the path, a newline, the raw body.

    The method is upper-cased and the path is the URL path without a query string. Neither can
    contain a newline, so the three parts cannot run into one another.
    """
    if "\n" in method or "\n" in path:
        raise ValueError("an HTTP method or path cannot contain a newline")
    return method.upper().encode("ascii") + b"\n" + path.encode("utf-8") + b"\n" + body


def agent_request_mac(secret: bytes, method: str, path: str, body: bytes) -> str:
    """The `X-Agent-Auth` value for one request, as lowercase hex."""
    if not secret:
        raise ValueError("an empty shared secret authenticates nothing")
    message = agent_request_message(method, path, body)
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def verify_agent_request(
    secret: bytes, method: str, path: str, body: bytes, presented: str | None
) -> bool:
    """True only when `presented` is exactly the MAC of this request. Constant-time."""
    if not presented:
        return False
    expected = agent_request_mac(secret, method, path, body)
    return hmac.compare_digest(expected.encode("ascii"), presented.encode("utf-8"))
