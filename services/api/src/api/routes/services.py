"""What the routes are handed by the composition root, and the dependencies they share.

The routes import the controller, the evidence and metrics modules and the repository interfaces
(docs/contributing.md section 1.1); everything concrete arrives here, built once at start-up.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import structlog
from fastapi import Request

from api.config import ApiSettings
from api.controller import RunController
from api.db.protocols import Transactions
from api.evidence import Exporter, ObserverViews, RunResources
from api.metrics import MetricsCalculator
from api.routes.errors import RevealRequiredError, request_id
from api.routes.idempotency import Idempotency
from api.routes.operations import OperationTracker

_log = structlog.get_logger(component="routes")

REVEAL_HEADER = "X-Observer-Reveal"


class Shutdown:
    """Set once the process begins to shut down: every event stream ends at its next look, so the
    server's graceful phase is not held open by a browser (ADR-082)."""

    def __init__(self) -> None:
        self.requested = False

    def request(self) -> None:
        self.requested = True


@dataclass(frozen=True, slots=True)
class Services:
    transactions: Transactions
    controller: RunController
    resources: RunResources
    exporter: Exporter
    observer: ObserverViews
    metrics: MetricsCalculator
    operations: OperationTracker
    idempotency: Idempotency
    settings: ApiSettings
    software_version: str
    clock: Callable[[], datetime]
    shutdown: Shutdown = field(default_factory=Shutdown)


def services(request: Request) -> Services:
    found: Services = request.app.state.services
    return found


def revealed(request: Request) -> bool:
    return request.headers.get(REVEAL_HEADER, "").lower() == "true"


def require_reveal(request: Request, what: str) -> None:
    """ADR-018: private routes need `X-Observer-Reveal: true`, and each access is logged — the
    route and the run, never what was revealed. Friction and audit, not a security boundary."""
    if not revealed(request):
        raise RevealRequiredError(
            f"{what} is private: send {REVEAL_HEADER}: true", {"header": REVEAL_HEADER}
        )
    _log.info(
        "observer.reveal",
        route=request.url.path,
        run_id=request.path_params.get("run_id"),
        request_id=request_id(request),
    )
