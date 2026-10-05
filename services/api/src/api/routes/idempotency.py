"""Idempotency keys on every `POST` (api_contract section 1), kept in `operations`.

A request that carries `Idempotency-Key` claims the key first — an `operations` row with the
route, the key and the canonical hash of the body — and only then does its work, so two requests
racing with one key cannot both do it: the database's `(route, idempotency_key)` uniqueness decides
which one claimed it (Q57).

- The same key and the same body again: the stored response, with `Idempotent-Replayed: true`. For
  a long-running route that is the operation record as it stands now.
- The same key with a different body: `409 idempotency_conflict`. So is the same key while the
  first request is still being answered.
- A request that was refused — any error response — keeps no key: the key is released, and a retry
  does the work again. Only success is replayed. A long-running request's replay while it is still
  in flight is its live operation, `202` (ADR-078 as amended, Q63).
- A key whose request never answered — the process died, or the client left and its task was
  cancelled before the refusal could be recorded — is released when it is next seen
  `IDEMPOTENCY_CLAIM_TIMEOUT_S` after the claim, and at start-up (Q63): its operation is recorded
  `failed` with code `interrupted`, and a retry does the work.
- A key older than its retention, 24 hours, is released when it is next seen and may be reused.

The route is the concrete path, `POST /v1/runs/<run id>/start`, so a key is scoped to the run it
was sent for.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Request

from api.db.enums import OperationStatus
from api.db.errors import DuplicateError
from api.db.protocols import Transactions
from api.db.records import NewOperation, OperationRecord
from api.routes.errors import BadRequestError, IdempotencyConflictError
from api.routes.operations import interrupted
from negotiation_protocol import json_sha256


@dataclass(frozen=True, slots=True)
class Claim:
    """What a request may do: replay `replay`, or do its work under `operation` (None when the
    request carried no key and is not a long-running operation)."""

    route: str
    request_hash: str
    operation: OperationRecord | None = None
    replay: OperationRecord | None = None


async def request_hash(request: Request) -> str:
    """The canonical hash of the body: `json_sha256` of its JSON, so formatting and key order do
    not make the same request look different. No body hashes as nothing."""
    raw = await request.body()
    if not raw.strip():
        return "0x" + hashlib.sha256(b"").hexdigest()
    try:
        document = json.loads(raw)
    except json.JSONDecodeError:
        raise BadRequestError("the body is not valid JSON") from None
    try:
        return json_sha256(document)
    except (TypeError, ValueError):
        # Canonical JSON refuses a float, which no request of this API carries: the bytes then
        # stand for themselves, and the route's own validation refuses the body.
        return "0x" + hashlib.sha256(raw).hexdigest()


def route_of(request: Request) -> str:
    return f"{request.method} {request.url.path}"


def idempotency_key(request: Request) -> str | None:
    value = request.headers.get("Idempotency-Key")
    if value is None:
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        raise BadRequestError(
            "Idempotency-Key must be a UUID", {"header": "Idempotency-Key"}
        ) from None


class Idempotency:
    def __init__(
        self,
        transactions: Transactions,
        *,
        retention: timedelta,
        claim_timeout: timedelta = timedelta(seconds=60),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._transactions = transactions
        self._retention = retention
        self._claim_timeout = claim_timeout
        self._clock = clock

    async def claim(
        self,
        request: Request,
        kind: str,
        run_id: uuid.UUID | None,
        *,
        long_running: bool,
    ) -> Claim:
        """Claim the request's key, or find what to replay. A long-running request without a key
        still gets an operation record; any other request without one gets none."""
        key = idempotency_key(request)
        route, digest = route_of(request), await request_hash(request)
        if key is None:
            if not long_running:
                return Claim(route, digest)
            return Claim(route, digest, operation=await self._create(kind, route, digest, run_id))
        existing = await self._existing(route, key)
        if existing is not None:
            return self._decide(existing, route, digest, long_running)
        try:
            operation = await self._create(kind, route, digest, run_id, key)
        except DuplicateError:
            raced = await self._existing(route, key)
            if raced is None:
                raise
            return self._decide(raced, route, digest, long_running)
        return Claim(route, digest, operation=operation)

    async def _existing(self, route: str, key: str) -> OperationRecord | None:
        now = self._clock()
        async with self._transactions.unit_of_work() as uow:
            found = await uow.operations.get_by_key(route, key)
            if found is None:
                return None
            if found.created_at + self._retention <= now:
                await uow.operations.release_key(found.id)
                return None
            if found.status == OperationStatus.PENDING and (
                found.created_at + self._claim_timeout <= now
            ):
                # A claim whose request never answered: a request answered at once is pending
                # only while it works, and a long-running one is `running` once accepted.
                await uow.operations.update(found.id, OperationStatus.FAILED, error=interrupted())
                await uow.operations.release_key(found.id)
                return None
        return found

    @staticmethod
    def _decide(existing: OperationRecord, route: str, digest: str, long_running: bool) -> Claim:
        if existing.request_hash != digest:
            raise IdempotencyConflictError(
                "this Idempotency-Key was used with a different request",
                {"operation_id": str(existing.id)},
            )
        if not long_running and existing.status != OperationStatus.SUCCEEDED:
            raise IdempotencyConflictError(
                "a request with this Idempotency-Key is still being answered",
                {"operation_id": str(existing.id)},
            )
        return Claim(route, digest, replay=existing)

    async def _create(
        self,
        kind: str,
        route: str,
        digest: str,
        run_id: uuid.UUID | None,
        key: str | None = None,
    ) -> OperationRecord:
        async with self._transactions.unit_of_work() as uow:
            return await uow.operations.create(
                NewOperation(
                    kind=kind,
                    route=route,
                    request_hash=digest,
                    run_id=run_id,
                    idempotency_key=key,
                )
            )

    async def succeeded(self, claim: Claim, body: dict[str, Any]) -> None:
        """A request that is not long-running answered: keep its response for a replay."""
        if claim.operation is None:
            return
        async with self._transactions.unit_of_work() as uow:
            await uow.operations.update(claim.operation.id, OperationStatus.SUCCEEDED, result=body)

    async def refused(self, claim: Claim, error: dict[str, Any]) -> OperationRecord | None:
        """The request was refused: the operation failed, and its key is free for a retry."""
        if claim.operation is None:
            return None
        async with self._transactions.unit_of_work() as uow:
            await uow.operations.update(claim.operation.id, OperationStatus.FAILED, error=error)
            return await uow.operations.release_key(claim.operation.id)
