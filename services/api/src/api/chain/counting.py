"""Counting the JSON-RPC requests the backend makes, by method and by run (ADR-061).

The count is taken where the request leaves the process — the HTTP provider underneath
`Web3ChainAdapter` — so one adapter call that needs two requests, `fee_quote` say, is counted as
two, and a request that fails is counted too: the provider bills it either way.

Attribution is a context variable. The controller attributes everything it does while driving a run
to that run, which is the run holding the lease; `asyncio.to_thread` copies the context into the
worker thread the provider runs in, so the attribution follows the request there. A request made
outside any run — a health check, a validation of a draft — belongs to no run's metrics, and is
counted in the log instead.

Counts are held in memory only until the controller drains them into `run_metrics`, which it does
as each tick ends; a crash loses at most one tick's requests, which is the precision the figure
claims.
"""

from __future__ import annotations

import contextlib
import threading
import uuid
from collections import Counter
from collections.abc import Iterator
from contextvars import ContextVar
from typing import Final

import structlog

_log = structlog.get_logger(component="chain")

_RUN: Final[ContextVar[uuid.UUID | None]] = ContextVar("rpc_attribution", default=None)


class RpcCounter:
    """Requests by run and JSON-RPC method, safe to record from the adapter's worker threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_run: dict[uuid.UUID, Counter[str]] = {}
        self._unattributed: Counter[str] = Counter()

    @staticmethod
    @contextlib.contextmanager
    def attributed_to(run_id: uuid.UUID) -> Iterator[None]:
        """Every request made inside the block, in this task and the threads it starts, is the
        run's."""
        token = _RUN.set(run_id)
        try:
            yield
        finally:
            _RUN.reset(token)

    @staticmethod
    def current() -> uuid.UUID | None:
        """The run requests are attributed to here, if any."""
        return _RUN.get()

    def record(self, method: str) -> None:
        run_id = _RUN.get()
        with self._lock:
            if run_id is None:
                self._unattributed[method] += 1
                total = self._unattributed.total()
            else:
                self._by_run.setdefault(run_id, Counter())[method] += 1
                return
        _log.debug("rpc.request_unattributed", method=method, unattributed_total=total)

    def drain(self, run_id: uuid.UUID) -> dict[str, int]:
        """The run's counts since the last drain, and forget them."""
        with self._lock:
            counts = self._by_run.pop(run_id, Counter())
        return dict(counts)

    def unattributed(self) -> dict[str, int]:
        with self._lock:
            return dict(self._unattributed)
