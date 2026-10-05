"""The OpenAPI contract test of api_contract section 8.

Two checks. The OpenAPI document the application generates must equal the committed snapshot,
`openapi.snapshot.json`, so any change to a route, a parameter or a response shape is a deliberate,
reviewed diff — `make openapi` rewrites the snapshot after an intended change. And the routes must
be exactly the ones api_contract section 2 heads, read from the document itself, less the two it
defers: the batch routes to stage 6, replay to stage 4 (Q55). A route added to one and not the other
fails here.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from api_http import api_settings

from api.routes import create_app

REPOSITORY = Path(__file__).resolve().parents[4]
SNAPSHOT = Path(__file__).resolve().parent / "openapi.snapshot.json"
#: Headed in api_contract section 2, and deliberately not served yet.
DEFERRED = {
    ("POST", "/v1/batches"),
    ("GET", "/v1/batches/{batch_id}"),
    ("GET", "/v1/batches/{batch_id}/report"),
    ("GET", "/v1/runs/{run_id}/replay"),
}


def openapi() -> dict[str, Any]:
    document: dict[str, Any] = create_app(api_settings()).openapi()
    return document


def test_the_openapi_document_matches_the_committed_snapshot() -> None:
    generated = json.dumps(openapi(), indent=2, sort_keys=True) + "\n"
    if os.environ.get("UPDATE_OPENAPI_SNAPSHOT") == "1":
        SNAPSHOT.write_text(generated, encoding="utf-8")
    assert SNAPSHOT.read_text(encoding="utf-8") == generated, (
        "The API changed. If that was intended, run `make openapi` and review the diff, and "
        "update docs/api_contract.md in the same change (section 8)."
    )


def _contract_routes() -> set[tuple[str, str]]:
    text = (REPOSITORY / "docs" / "api_contract.md").read_text(encoding="utf-8")
    section = text[text.index("## 2. Operator API") : text.index("## 3. Server-sent events")]
    return {
        (method, path)
        for method, path in re.findall(
            r"^#### `(GET|POST|PUT|PATCH|DELETE) (/v1/\S+)`", section, re.M
        )
    }


def test_the_routes_are_exactly_the_contracts_less_what_it_defers() -> None:
    served = {
        (method.upper(), path)
        for path, operations in openapi()["paths"].items()
        for method in operations
    }
    contract = _contract_routes()
    assert contract >= DEFERRED, "a deferred route is no longer in the contract"
    assert served == contract - DEFERRED


def test_every_response_names_its_error_envelope() -> None:
    """A client generated from the document knows the shape of every refusal (section 1.1)."""
    for path, operations in openapi()["paths"].items():
        for method, operation in operations.items():
            if path == "/v1/health":
                continue
            refusals = [code for code in operation["responses"] if code.startswith(("4", "5"))]
            assert refusals, (method, path)
