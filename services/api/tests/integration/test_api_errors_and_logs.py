"""What a failure writes, and what a refused body gets back (the stage 2.5 review's first finding).

An exception's own text is whatever its raiser put in it — a database error quoted the row it
refused, a schema error its instance — and the first version of `api.logs` formatted tracebacks
after redacting, so a mandate reached the logs through them. Here an unhandled exception carrying
private values is raised inside a real request, served by uvicorn with the process's own logging,
and nothing private may appear in any line either structlog or the standard library writes.
"""

from __future__ import annotations

import io
import json
import logging
import uuid
from collections.abc import AsyncIterator, Iterator

import httpx
import pytest
import structlog
from api_chain import AnvilChain
from api_controller import ControllerHarness
from api_http import ApiHarness, run_body, serve

from api.db import Database
from api.logs import configure_logging

SECRET = "MANDATE-SECRET-" + uuid.uuid4().hex[:8]


@pytest.fixture
def captured() -> Iterator[io.StringIO]:
    """The process's own logging, set up before the app is served (contributing 3)."""
    config = structlog.get_config()
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    stream = io.StringIO()
    configure_logging(level="DEBUG", stream=stream)
    yield stream
    structlog.configure(**config)
    structlog.contextvars.clear_contextvars()
    root.handlers, root.level = handlers, level


@pytest.fixture
async def api(
    database: Database, chain: AnvilChain, captured: io.StringIO
) -> AsyncIterator[ApiHarness]:
    harness = ApiHarness(ControllerHarness(database, chain))
    yield harness
    await harness.aclose()


async def test_an_unhandled_exception_is_a_500_with_its_request_id_and_no_message_logged(
    api: ApiHarness, captured: io.StringIO, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def explode(*args: object, **kwargs: object) -> None:
        cause = ValueError(f"inner {SECRET} reservation 123456789")
        raise RuntimeError(f"the row ({SECRET}, 'Pay as little as you can')") from cause

    monkeypatch.setattr(api.services.controller, "create_run", explode)
    await api.prepare()
    async with serve(api) as url, httpx.AsyncClient(base_url=url) as client:
        response = await client.post(
            "/v1/runs",
            json=run_body("default-overlap", api.deployment_id),
            headers={"X-Request-Id": "req-boom"},
        )
    assert response.status_code == 500
    assert response.json()["error"] == {
        "code": "internal_error",
        "message": "internal error",
        "details": {},
        "request_id": "req-boom",
    }
    assert response.headers["X-Request-Id"] == "req-boom"

    text = captured.getvalue()
    for private in (SECRET, "123456789", "Pay as little"):
        assert private not in text, private
    lines = [json.loads(line) for line in text.splitlines() if line.startswith("{")]
    (failure,) = [line for line in lines if line.get("event") == "request.unhandled"]
    assert failure["exception"]["type"] == "RuntimeError"
    assert failure["exception"]["causes"] == ["ValueError"]
    assert any("routes/app.py" in frame for frame in failure["exception"]["frames"])
    (request,) = [line for line in lines if line.get("event") == "request"]
    assert (request["status"], request["request_id"]) == (500, "req-boom")


async def test_a_nul_in_mandate_text_is_refused_at_the_boundary(api: ApiHarness) -> None:
    """Q69: PostgreSQL cannot store a NUL; the request is refused 422, not failed 500."""
    await api.prepare()
    body = run_body("default-overlap", api.deployment_id)
    body["buyer"]["mandate"]["instructions"] = f"Pay as little {SECRET}\u0000"
    response = await api.client.post("/v1/runs", json=body)
    assert response.status_code == 422
    assert response.json()["error"]["details"]["fields"] == {
        "buyer.mandate.instructions": "value_error"
    }
    assert SECRET not in response.text
    run = await api.create()
    clone = await api.client.post(f"/v1/runs/{run['run_id']}/clone", json={"name": "a\u0000b"})
    assert clone.status_code == 422


async def test_a_float_in_a_keyed_body_is_refused_not_failed(api: ApiHarness) -> None:
    """Canonical JSON refuses a float, and a clone's merge patch is free-form, so the patch reaches
    the idempotency hash: it falls back to the bytes, and the clone's own parsing answers 422."""
    run = await api.create()
    response = await api.client.post(
        f"/v1/runs/{run['run_id']}/clone",
        json={"public_config": {"max_offers": 8.5}},
        headers={"Idempotency-Key": str(uuid.uuid4())},
    )
    assert response.status_code == 422, response.text
    assert response.json()["error"]["details"]["fields"] == {"public_config.max_offers": "int_type"}
