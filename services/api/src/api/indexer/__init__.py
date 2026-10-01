"""Receipt polling, log decoding, block-hash tracking, the canonical flag, reorg detection and
rebuild, confirmation depth and finality, terminal-event balances and settlement verification.

Built in stage 2.3 of docs/build_plan.md; see docs/architecture.md sections 3.2, 5.3 and 5.5, and
`indexer.py` for the order a poll does things in and why.
"""

from api.indexer.indexer import (
    TERMINAL_EVENT_NAMES,
    UNDETERMINED_REVERT,
    Indexer,
    SentenceRenderer,
)
from api.indexer.reports import (
    PollReport,
    Reorg,
    RunProblem,
    SettlementCheck,
    TerminalConfirmed,
)
from api.indexer.settlement import check_settlement

__all__ = [
    "TERMINAL_EVENT_NAMES",
    "UNDETERMINED_REVERT",
    "Indexer",
    "PollReport",
    "Reorg",
    "RunProblem",
    "SentenceRenderer",
    "SettlementCheck",
    "TerminalConfirmed",
    "check_settlement",
]
