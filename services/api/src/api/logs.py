"""The backend process's structured JSON logs, with a redaction pass (docs/architecture.md 8).

Every line is JSON and carries `component`; the request line carries `request_id`, and anything
about a run its `run_id`. No request or response body is logged anywhere: the routes log what
happened, never what was said. The redaction processor is the second line of defence behind that
rule. It replaces the value of any field whose name is private — a mandate's fields, an observation,
a feedback, a raw response, an authorization header, anything naming a key, a secret, a password or
a token — and any string that looks like an Anthropic credential, however deeply nested; an object
that is not plain JSON is rendered as its type name, never its `repr`.

**An exception is logged by its type and its frames, never its message** — the structlog lines'
and the standard library's, uvicorn's own included. An exception's text is whatever its raiser put
in it: a database error quotes the row it refused, a schema error the instance, and no name-based
rule can find a mandate inside free text. So the message never reaches the processor chain at all:
`render_exception` replaces `exc_info` with `{type, frames, causes}` before anything renders it.
This is the rule the agent service's own logs keep, which render an exception as its type name; the
stage 2.5 review found the first version of this module formatting tracebacks after redacting.

The agent service has its own copy of these rules (`agent.logs`): the two are separate deployables
and neither imports the other.
"""

from __future__ import annotations

import logging
import re
import sys
import traceback
from collections.abc import MutableMapping
from types import TracebackType
from typing import Any, Final, TextIO

import structlog

REDACTED: Final = "[redacted]"

PRIVATE_FIELDS: Final = frozenset(
    {
        "mandate",
        "mandates",
        "reservation_price_minor",
        "min_remaining_inventory_minor",
        "instructions",
        "observation",
        "feedback",
        "validation_feedback",
        "raw_response",
        "explanation",
        "authorization",
        "x-agent-auth",
    }
)
_PRIVATE_NAME: Final = re.compile(r"(key|secret|password|token|root)", re.IGNORECASE)
_KEPT: Final = frozenset({"key_ref", "root_key_ref", "relay_key_ref", "operator_key_ref"})
_CREDENTIAL: Final = re.compile(r"sk-ant-[A-Za-z0-9_-]+")


def _is_private(name: str) -> bool:
    lowered = name.lower()
    return lowered in PRIVATE_FIELDS or (
        _PRIVATE_NAME.search(lowered) is not None and lowered not in _KEPT
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


def _exception(value: Any) -> BaseException | None:
    if isinstance(value, BaseException):
        return value
    if isinstance(value, tuple) and len(value) == 3 and isinstance(value[1], BaseException):
        return value[1]
    if value is True:
        return sys.exc_info()[1]
    return None


def _frames(trace: TracebackType | None) -> list[str]:
    return [
        f"{frame.filename}:{frame.lineno} in {frame.name}" for frame in traceback.extract_tb(trace)
    ]


def render_exception(
    logger: Any, method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """A structlog processor: `exc_info` becomes the exception's type, its frames and the types
    of its causes — never a message, which may quote a private value."""
    error = _exception(event_dict.pop("exc_info", None))
    if error is None:
        return event_dict
    causes: list[str] = []
    cause = error.__cause__ or error.__context__
    while cause is not None and len(causes) < 8:
        causes.append(type(cause).__qualname__)
        cause = cause.__cause__ or cause.__context__
    event_dict["exception"] = {
        "type": type(error).__qualname__,
        "frames": _frames(error.__traceback__),
        "causes": causes,
    }
    return event_dict


def redact(
    logger: Any, method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """A structlog processor. A key *reference* is kept: it is a reference, not a key. The event
    text is a message, not a field, so only its credential pattern is scrubbed."""
    for name in list(event_dict):
        if name.startswith("_"):
            continue
        if name == "event":
            event_dict[name] = _scrub(event_dict[name])
            continue
        event_dict[name] = REDACTED if _is_private(name) else _scrub(event_dict[name])
    return event_dict


def configure_logging(*, level: str, stream: TextIO | None = None) -> None:
    numeric = logging.getLevelName(level.upper())
    if not isinstance(numeric, int):
        numeric = logging.INFO
    output = stream or sys.stdout
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        render_exception,
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
    structlog.configure(
        processors=[*shared, structlog.processors.JSONRenderer(sort_keys=True)],
        wrapper_class=structlog.make_filtering_bound_logger(numeric),
        logger_factory=structlog.PrintLoggerFactory(file=output),
        cache_logger_on_first_use=False,
    )
    structlog.contextvars.bind_contextvars(service="api")
