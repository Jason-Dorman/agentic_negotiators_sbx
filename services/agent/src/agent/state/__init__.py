"""Run-scoped, in-memory state (docs/architecture.md section 3.3).

What one instance knows about one run: what it was provisioned with, the session it approved, and
the turns it has answered. Nothing here is persisted. A restarted agent knows no runs, and the
controller re-provisions it from `mandate_versions` when it reports one unknown — the derivation of
ADR-039 is what makes that safe, because re-provisioning reproduces the same key and address.

A released run leaves a tombstone, so nothing can be signed for it again in this process's life:
after `release`, the run is terminal on-chain and any further signing request for it is a bug or an
attack. The tombstone is a run id and nothing else.

The registry is created by the composition root and injected, never module-level
(docs/contributing.md section 2.1).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from agent.errors import InvalidStateError
from agent.keys import RunSigner
from agent.observation import Role
from agent.policy import Policy
from agent.signing import ApprovedSession, ExpectedSession, SessionOpened
from negotiation_protocol import MinorAmount

UNPROVISIONED: Final = "unprovisioned"
PROVISIONED: Final = "provisioned"
APPROVED: Final = "approved"
RELEASED: Final = "released"


@dataclass(frozen=True, slots=True)
class Provisioning:
    """Everything provisioning delivers for one run (docs/api_contract.md section 6)."""

    role: Role
    policy: str
    model_id: str | None
    effort: str | None
    repair_attempts: int
    model_call_ceiling: int
    model_spend_ceiling_usd: Decimal
    model_timeout_s: int
    expected: ExpectedSession
    key_ref: str
    mandate_version_id: UUID
    mandate_document: Mapping[str, Any]
    initial_base: MinorAmount
    initial_quote: MinorAmount
    allowance: MinorAmount
    fingerprint: str

    def __repr__(self) -> str:
        return f"Provisioning(role={self.role}, policy={self.policy}, <private>)"


@dataclass(frozen=True, slots=True)
class AnsweredTurn:
    observation_hash: str
    response: Mapping[str, Any]


@dataclass(slots=True)
class RunRecord:
    run_id: UUID
    provisioning: Provisioning
    policy: Policy
    signer: RunSigner
    provisioned_response: Mapping[str, Any]
    opened: SessionOpened | None = None
    approval: ApprovedSession | None = None
    turns: dict[int, AnsweredTurn] = field(default_factory=dict)
    turn_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    #: Set by `release`. A turn already deciding holds this record, not the registry, so it reads
    #: this flag just before signing and signs nothing once it is set (ADR-048).
    released: bool = False

    @property
    def state(self) -> str:
        return APPROVED if self.approval is not None else PROVISIONED


class RunRegistry:
    def __init__(self) -> None:
        self._runs: dict[UUID, RunRecord] = {}
        self._released: set[UUID] = set()

    def get(self, run_id: UUID) -> RunRecord | None:
        return self._runs.get(run_id)

    def is_released(self, run_id: UUID) -> bool:
        return run_id in self._released

    def add(self, record: RunRecord) -> None:
        if record.run_id in self._runs or record.run_id in self._released:
            raise ValueError(f"run {record.run_id} is already known to this instance")
        self._runs[record.run_id] = record

    def require(self, run_id: UUID, allowed: tuple[str, ...]) -> RunRecord:
        """The run's record, if its state is one of `allowed`; `invalid_state` otherwise."""
        record = self._runs.get(run_id)
        if run_id in self._released:
            state = RELEASED
        elif record is None:
            state = UNPROVISIONED
        else:
            state = record.state
        if record is None or state not in allowed:
            raise InvalidStateError(
                f"run {run_id} is {state} on this instance",
                state=state,
                allowed_from=allowed,
            )
        return record

    def release(self, run_id: UUID) -> None:
        """Discard the run's mandate and signer, and refuse it from now on. Idempotent.

        A turn in flight keeps its own reference to the record, so the record is also marked: the
        turn sees it before signing and returns `invalid_state` with nothing signed (ADR-048).
        """
        record = self._runs.pop(run_id, None)
        if record is not None:
            record.released = True
        self._released.add(run_id)

    def __len__(self) -> int:
        return len(self._runs)


__all__ = [
    "APPROVED",
    "PROVISIONED",
    "RELEASED",
    "UNPROVISIONED",
    "AnsweredTurn",
    "Provisioning",
    "RunRecord",
    "RunRegistry",
]
