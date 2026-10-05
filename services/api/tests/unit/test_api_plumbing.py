"""The HTTP layer's pure pieces: when an operation has its verdict (Q54), what the backend's logs
redact, and what start-up refuses to load."""

from __future__ import annotations

import io
import json
import logging
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog

from api.db import OperationStatus
from api.logs import REDACTED, configure_logging
from api.main import StartupError, load_deployment, load_scenarios
from api.routes.operations import ABORT, RESUME, START, STEP, verdict

REPOSITORY = Path(__file__).resolve().parents[4]
OK, FAILED = OperationStatus.SUCCEEDED, OperationStatus.FAILED


def state(name: str, cause: str | None = None) -> dict[str, Any]:
    return {"state": name, "state_cause": cause, "outcome": {"kind": "pending"}}


@pytest.mark.parametrize(
    ("kind", "data", "expected"),
    [
        (START, state("preparing", "start"), None),
        (START, state("running"), OK),
        (START, state("terminal"), OK),
        (START, state("recovery_required", "rpc_timeout"), FAILED),
        (START, state("failed_setup", "approve_reverted"), FAILED),
        (STEP, state("paused", "step"), None),
        (STEP, state("paused", "step_complete"), OK),
        (STEP, state("running"), None),
        (STEP, state("failed_setup", "session_refused"), FAILED),
        (RESUME, state("running"), OK),
        (RESUME, state("paused", "recovered"), OK),
        (RESUME, state("preparing", "recovered"), OK),
        (RESUME, state("paused", "reorg"), None),
        (RESUME, state("recovery_required", "unreachable"), FAILED),
        (ABORT, state("running"), None),
        (ABORT, state("terminal"), OK),
        (ABORT, state("failed_setup", "abort_requested"), OK),
        (ABORT, state("recovery_required", "termination_reverted"), FAILED),
        # Q64: a start or a step the chain interrupted, paused short of what it asked for.
        (START, state("paused", "reorg"), FAILED),
        (START, state("paused", "session_open"), FAILED),
        (STEP, state("paused", "reorg"), FAILED),
        (STEP, state("paused", "session_open"), FAILED),
        (START, state("preparing", "reorg"), None),
        (RESUME, state("paused", "session_open"), None),
    ],
)
def test_an_operation_is_decided_by_the_run_state_it_was_for(
    kind: str, data: dict[str, Any], expected: OperationStatus | None
) -> None:
    assert verdict(kind, data) == expected


def test_an_abort_sent_from_recovery_is_not_failed_by_the_state_it_started_in() -> None:
    assert verdict(ABORT, state("recovery_required", "rpc_timeout"), initial=True) is None
    assert verdict(RESUME, state("recovery_required", "rpc_timeout"), initial=True) == FAILED


@pytest.fixture
def restored_logging() -> Iterator[None]:
    """`configure_logging` is process-wide; put structlog and the root logger back."""
    config = structlog.get_config()
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    structlog.configure(**config)
    structlog.contextvars.clear_contextvars()
    root.handlers, root.level = handlers, level


@pytest.mark.usefixtures("restored_logging")
def test_the_logs_redact_private_fields_and_credentials_at_any_depth() -> None:
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream)
    log = structlog.get_logger(component="test")
    log.info(
        "probe",
        run_id="r",
        mandate={"reservation_price_minor": "100000000"},
        nested={"deeper": {"instructions": "never say it", "ok": 1}},
        authorization="Bearer abc",
        operator_token="abc",
        note="a credential sk-ant-api03-SECRET in text",
        relay_key_ref="env:RELAY_PRIVATE_KEY",
    )
    line = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert line["mandate"] == REDACTED
    assert line["nested"]["deeper"] == {"instructions": REDACTED, "ok": 1}
    assert line["authorization"] == REDACTED
    assert line["operator_token"] == REDACTED
    assert "SECRET" not in line["note"]
    assert line["relay_key_ref"] == "env:RELAY_PRIVATE_KEY", "a reference is not a key"
    assert line["run_id"] == "r"
    for secret in ("100000000", "never say it", "abc"):
        assert secret not in stream.getvalue()


@pytest.mark.usefixtures("restored_logging")
def test_an_exception_is_logged_by_type_and_frames_never_by_message() -> None:
    """The stage 2.5 review's first finding: tracebacks were formatted after the redaction."""
    stream = io.StringIO()
    configure_logging(level="INFO", stream=stream)
    try:
        try:
            raise KeyError("cause 90000000")
        except KeyError as cause:
            raise RuntimeError("[parameters: ('Pay as little', 100000000)]") from cause
    except RuntimeError:
        structlog.get_logger(component="test").exception("boom")
        logging.getLogger("uvicorn.error").error("Exception in ASGI application", exc_info=True)
    logging.getLogger("x").warning("a credential sk-ant-api03-INTEXT in a message")
    text = stream.getvalue()
    for private in ("90000000", "100000000", "Pay as little", "INTEXT"):
        assert private not in text, private
    lines = [json.loads(line) for line in text.strip().splitlines()]
    assert [line["exception"]["type"] for line in lines[:2]] == ["RuntimeError", "RuntimeError"]
    assert lines[0]["exception"]["causes"] == ["KeyError"]
    assert all("test_api_plumbing.py" in line["exception"]["frames"][0] for line in lines[:2])


def test_a_scenario_that_fails_its_schema_is_named_without_its_contents(tmp_path: Path) -> None:
    document = json.loads((REPOSITORY / "scenarios" / "default-overlap.json").read_text())
    document["seller"]["mandate"]["note"] = "SECRET-NOTE"
    (tmp_path / "default-overlap.json").write_text(json.dumps(document))
    with pytest.raises(StartupError) as refused:
        load_scenarios(tmp_path)
    message = str(refused.value)
    assert "default-overlap.json does not match scenario.v1.json at seller/mandate" in message
    for private in ("SECRET-NOTE", "90000000", "Obtain as much"):
        assert private not in message
    with pytest.raises(StartupError, match="is not a directory"):
        load_scenarios(tmp_path / "nowhere")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(StartupError, match="holds no scenario"):
        load_scenarios(empty)


def test_python_m_api_names_a_missing_variable_and_exits_2() -> None:
    import os
    import subprocess
    import sys

    environment = {k: v for k, v in os.environ.items() if k not in ("DATABASE_URL",)}
    environment["DEPLOYMENT_MANIFEST"] = "m.json"
    environment["OPERATOR_TOKEN"] = "TOKEN-SECRET-VALUE"
    done = subprocess.run(
        [sys.executable, "-m", "api"], env=environment, capture_output=True, text=True, timeout=60
    )
    assert done.returncode == 2
    assert "DATABASE_URL" in done.stderr
    assert "TOKEN-SECRET-VALUE" not in done.stderr + done.stdout


async def test_an_operation_watcher_outlives_a_database_error() -> None:
    """A watcher that meets an error waits and reads again from where it was (stage 2.5 review)."""
    from tracker_fakes import FlakyEvents

    from api.routes.operations import OperationTracker

    fake = FlakyEvents(failures=2)
    tracker = OperationTracker(fake, poll_interval_s=0.0, sleep=fake.sleep)
    await tracker.accepted(fake.operation, fake.run, {"kind": "pending"}, cursor=0)
    await tracker.wait()
    assert fake.recorded == [OperationStatus.RUNNING, OperationStatus.SUCCEEDED]
    assert fake.failures_seen == 2


def test_the_committed_scenarios_load_and_a_misnamed_one_is_refused(tmp_path: Path) -> None:
    scenarios = load_scenarios(REPOSITORY / "scenarios")
    assert [s.scenario_id for s in scenarios] == ["default-overlap", "infeasible-clone"]
    assert all(s.source_hash.startswith("0x") and len(s.source_hash) == 66 for s in scenarios)

    shutil.copy(REPOSITORY / "scenarios" / "default-overlap.json", tmp_path / "renamed.json")
    with pytest.raises(StartupError, match="must match the file name"):
        load_scenarios(tmp_path)


def test_a_manifest_that_is_missing_or_invalid_is_refused(tmp_path: Path) -> None:
    with pytest.raises(StartupError, match="cannot read"):
        load_deployment(tmp_path / "absent.json")
    invalid = tmp_path / "invalid.json"
    invalid.write_text(json.dumps({"deployment_id": "x"}))
    with pytest.raises(
        StartupError, match=r"does not match deployment_manifest.v1.json at <root> \(required\)"
    ):
        load_deployment(invalid)


def test_an_empty_optional_variable_is_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """`infra/.env.example` leaves the optional variables empty; empty must not be `Path(".")` or a
    token of nothing."""
    from api.config import load_api_settings

    for name, value in {
        "DATABASE_URL": "postgresql+asyncpg://x",
        "DEPLOYMENT_MANIFEST": "m.json",
        "RPC_PRICE_TABLE": "",
        "OPERATOR_TOKEN": "",
    }.items():
        monkeypatch.setenv(name, value)
    settings = load_api_settings()
    assert settings.rpc_price_table is None
    assert settings.token() is None
    monkeypatch.setenv("OPERATOR_TOKEN", "t0ken")
    assert load_api_settings().token() == "t0ken"
