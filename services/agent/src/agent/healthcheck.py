"""`python -m agent.healthcheck`: the container's health probe (infra/compose.local.yaml).

`GET /internal/health` requires the instance's HMAC like every route (ADR-041), so a bare HTTP probe
cannot call it. This signs the request with the instance's own shared secret, from its own
environment, and exits 0 only when the instance answers `ok` with its signer loaded — so Compose
holds the backend back until both agents can sign, and "the agent is up but its key is not" shows as
unhealthy rather than as a run that fails at provisioning.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from negotiation_protocol.agent_auth import agent_request_mac

PATH = "/internal/health"


def probe(port: int, secret: bytes, *, timeout_s: float = 3.0) -> bool:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{PATH}",
        headers={
            "X-Agent-Auth": agent_request_mac(secret, "GET", PATH, b""),
            "X-Request-Id": "healthcheck",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310  reason: a fixed http URL on the loopback
            body = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return bool(body.get("status") == "ok" and body.get("signer_ok") is True)


def main() -> int:
    port = int(os.environ.get("AGENT_PORT", "0"))
    secret = os.environ.get("AGENT_SHARED_SECRET", "").encode("utf-8")
    return 0 if port and secret and probe(port, secret) else 1


if __name__ == "__main__":
    sys.exit(main())
