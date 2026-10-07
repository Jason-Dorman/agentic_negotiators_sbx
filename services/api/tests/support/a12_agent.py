"""`python a12_agent.py`: one agent instance as `python -m agent` runs it, for the isolation suite.

It is `agent.__main__` with two additions, both test-only and both off unless their variable is set:

- `A12_CAPTURE` names a file that receives, as one JSON line each, every model request that passed
  the outbound-context assertion — through the composition root's `model_request_observer`, so
  what is captured is exactly what a live instance would have sent (ADR-092).
- `A12_PLANT_REPAIR` names a file whose text, when there is any, is appended to every repair
  message this instance writes: a deliberately planted leak, standing in for a bug that put the
  counterparty's feedback into this agent's repair, so the suite can show its scan catches one.

Everything else — settings, logging with its redaction, the real application under uvicorn — is the
real entry point's, so the log lines scanned are the lines a deployed instance would write.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import uvicorn

from agent.logs import configure_logging
from agent.main import create_app
from agent.prompting import PromptTemplate
from agent.settings import SettingsError, load_settings


def _capture(path: Path) -> Any:
    def write(body: Mapping[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as capture:
            capture.write(json.dumps(body, ensure_ascii=False) + "\n")

    return write


def _plant_repairs(path: Path) -> None:
    honest = PromptTemplate.repair_message

    def planted(self: PromptTemplate, code: str, feedback: str) -> str:
        message = honest(self, code, feedback)
        extra = path.read_text(encoding="utf-8") if path.exists() else ""
        return message + extra

    PromptTemplate.repair_message = planted  # type: ignore[method-assign]  # reason: the planted bug


def main() -> None:
    try:
        settings = load_settings()
    except SettingsError as error:
        print(error, file=sys.stderr)
        raise SystemExit(2) from None
    configure_logging(level=settings.log_level, instance=settings.instance, role=settings.role)
    capture = os.environ.get("A12_CAPTURE")
    plant = os.environ.get("A12_PLANT_REPAIR")
    if plant:
        _plant_repairs(Path(plant))
    app = create_app(
        settings,
        environ=os.environ,
        model_request_observer=_capture(Path(capture)) if capture else None,
    )
    uvicorn.run(app, host=settings.host, port=settings.port, access_log=False, log_config=None)


if __name__ == "__main__":
    main()
