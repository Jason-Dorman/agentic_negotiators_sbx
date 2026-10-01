"""What one indexer poll found, for the controller to act on (stage 2.4).

The indexer records facts; it does not move a run between operational states. A reorg pauses the
run, a revert ends a turn as an execution failure, and a terminal event at the threshold makes the
run terminal — but those transitions are the controller's (docs/architecture.md section 6.1,
ADR-052), so the indexer reports them and the controller decides.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from api.db.records import ChainEventRecord, OutboxRecord
from negotiation_protocol import Digest


@dataclass(frozen=True, slots=True)
class Reorg:
    """Stored block hashes no longer on the chain (docs/architecture.md section 5.5).

    `fork_block` is the lowest height whose stored hash the chain no longer has, and the scan
    resumes from it; every row in a block the chain no longer has was invalidated. The same facts
    are written, per run and in the same transaction, as a `chain.reorg` run event (ADR-058), so a
    report lost with its process does not lose the reorg. `invalidated_digests` are the offer
    digests and signed-action digests whose recorded effect the reorg removed.
    """

    fork_block: int
    head_block: int
    invalidated_events: tuple[ChainEventRecord, ...]
    reset_transactions: tuple[OutboxRecord, ...]
    invalidated_digests: tuple[Digest, ...]
    run_ids: tuple[uuid.UUID, ...]


@dataclass(frozen=True, slots=True)
class SettlementCheck:
    """docs/architecture.md section 5.3: the acceptance, the settlement and exactly the two
    transfers the signed offer authorised, in one receipt — or the problems found instead."""

    tx_hash: Digest
    problems: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.problems


@dataclass(frozen=True, slots=True)
class TerminalConfirmed:
    """A run's terminal event reached its confirmation threshold, and its balances were taken."""

    run_id: uuid.UUID
    event: ChainEventRecord
    settlement: SettlementCheck | None


@dataclass(frozen=True, slots=True)
class RunProblem:
    """A fault in one run's records the indexer could not resolve, reported rather than raised so
    that it never stops the poll for the others. The controller sends the run back to a person.

    `code` is `invalid_confirmation_threshold` (ADR-059) or `session_opening_missing`.
    """

    run_id: uuid.UUID
    code: str
    detail: str


@dataclass(frozen=True, slots=True)
class PollReport:
    head_block: int
    finalized_block: int
    reorg: Reorg | None
    indexed: tuple[ChainEventRecord, ...]
    included: tuple[OutboxRecord, ...]
    reverted: tuple[OutboxRecord, ...]
    confirmed: tuple[OutboxRecord, ...]
    finalized: tuple[OutboxRecord, ...]
    #: Level-triggered: every watched run whose terminal event is at its threshold, on every poll
    #: until the controller makes the run terminal (ADR-058).
    terminal: tuple[TerminalConfirmed, ...]
    problems: tuple[RunProblem, ...] = ()
