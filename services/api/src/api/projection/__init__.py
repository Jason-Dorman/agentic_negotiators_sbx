"""Session view, timeline and balances derived from canonical events only (data model invariant 6),
the economic outcome they support, and the timeline sentences the indexer stores (ADR-024).

Built in stage 2.3 of docs/build_plan.md; see docs/architecture.md section 3.2.
"""

from api.config import confirmation_threshold
from api.projection.projector import Projector, RunProjection
from api.projection.sentences import (
    BASE_SYMBOL,
    QUOTE_SYMBOL,
    SentenceContextError,
    TimelineSentences,
    format_minor,
)
from api.projection.views import (
    TERMINAL_EVENTS,
    Parties,
    TimelineContext,
    build_timeline,
    derive_outcome,
    latest_balances,
    session_view,
)

__all__ = [
    "BASE_SYMBOL",
    "QUOTE_SYMBOL",
    "TERMINAL_EVENTS",
    "Parties",
    "Projector",
    "RunProjection",
    "SentenceContextError",
    "TimelineContext",
    "TimelineSentences",
    "build_timeline",
    "confirmation_threshold",
    "derive_outcome",
    "format_minor",
    "latest_balances",
    "session_view",
]
