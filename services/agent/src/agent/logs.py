"""Structured JSON logs with a redaction pass (docs/architecture.md section 8).

Every line carries `component`, `instance` and `role`, and the request line carries `run_id` and
`request_id`. No request or response body is ever logged: the routes log what happened, never what
was said. The redaction processor is the second line of defence behind that rule, not the first. It
replaces the value of any field whose name is private (`mandate`, `feedback`, `raw_response`, the
mandate's own fields, anything naming a key, a secret or a password), any string that looks like an
Anthropic credential, however deeply nested, and any value that is not plain JSON: an object is
rendered as its type name and never as its `repr`, because a `Validation`, a `Repair` or a
`DecisionRecord` carries private feedback in its fields.

The standard library's records — uvicorn's own lines — pass through the same processor and come out
as JSON too, so there is one redaction rule for everything the process writes. The model SDK's and
its HTTP library's loggers are held at `WARNING` whatever the level or `ANTHROPIC_LOG` says: at
`DEBUG` they print each request's options, the system prompt and its mandate among them, inside a
message string no field-name rule can see into.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any, Final, TextIO

import structlog

REDACTED: Final = "[redacted]"

PRIVATE_FIELDS: Final = frozenset(
    {
        "mandate",
        "reservation_price_minor",
        "min_remaining_inventory_minor",
        "instructions",
        "observation",
        "feedback",
        "validation_feedback",
        "raw_response",
        "my_previous_decisions",
        "explanation",
        "authorization",
        "x-agent-auth",
    }
)
_PRIVATE_NAME = re.compile(r"(key|secret|password|token|root)", re.IGNORECASE)
_CREDENTIAL = re.compile(r"sk-ant-[A-Za-z0-9_-]+")
#: References, not secrets; the database stores the first two.
_REFERENCE_FIELDS: Final = frozenset({"key_ref", "root_key_ref", "model_key_ref"})
#: Held at WARNING (stage 3.1): `anthropic` writes request bodies at DEBUG; `httpx2` and
#: `httpcore2`, under the SDK, and `httpx` and `httpcore`, under other clients, write request lines.
QUIET_LOGGERS: Final = ("anthropic", "httpx2", "httpcore2", "httpx", "httpcore")


def _is_private(name: str) -> bool:
    lowered = name.lower()
    return lowered in PRIVATE_FIELDS or (
        _PRIVATE_NAME.search(lowered) is not None and lowered not in _REFERENCE_FIELDS
    )


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: REDACTED if _is_private(str(key)) else _scrub(item) for key, item in value.items()
        }
    if isinstance(value, list | tuple):
        return [_scrub(item) for item in value]
    if isinstance(value, str):
        return _CREDENTIAL.sub(REDACTED, value)
    if value is None or isinstance(value, bool | int | float):
        return value
    return f"<{type(value).__name__}>"


def redact(
    logger: Any, method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """A structlog processor. `key_ref` is kept: it is a reference, and the database stores it."""
    for name in list(event_dict):
        if name.startswith("_"):
            continue  # structlog's own bookkeeping (`_record`), removed before rendering
        event_dict[name] = REDACTED if _is_private(name) else _scrub(event_dict[name])
    return event_dict


def configure_logging(
    *, level: str, instance: str, role: str, stream: TextIO | None = None
) -> None:
    numeric = logging.getLevelName(level.upper())
    if not isinstance(numeric, int):
        numeric = logging.INFO
    output = stream or sys.stdout
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        redact,
    ]
    handler = logging.StreamHandler(output)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.JSONRenderer(sort_keys=True),
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(numeric)
    for name in QUIET_LOGGERS:
        logging.getLogger(name).setLevel(max(numeric, logging.WARNING))
    structlog.configure(
        processors=[*shared, structlog.processors.JSONRenderer(sort_keys=True)],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.PrintLoggerFactory(file=output),
        cache_logger_on_first_use=False,
    )
    structlog.contextvars.bind_contextvars(instance=instance, role=role)
