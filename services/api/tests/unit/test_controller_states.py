"""The run state machine (architecture 6.1): the table matches the diagram, and every transition
the diagram does not draw is refused — every pair of states, not a sample."""

from __future__ import annotations

import re
from itertools import product
from pathlib import Path

import pytest

from api.controller import TRANSITIONS, InvalidTransitionError, allowed, require_transition
from api.controller.states import ACTIVE, FINISHED, sources
from api.db import RunState

ARCHITECTURE = Path(__file__).resolve().parents[4] / "docs" / "architecture.md"


def diagram_transitions() -> set[tuple[RunState, RunState]]:
    """The arrows of the stateDiagram in architecture section 6.1, less `[*]`."""
    text = ARCHITECTURE.read_text()
    section = text[text.index("### 6.1 Run operational state") : text.index("### 6.2")]
    diagram = section[
        section.index("```mermaid") : section.index("```", section.index("```mermaid") + 3)
    ]
    arrows = re.findall(r"^\s*(\[\*\]|[A-Z_]+)\s*-->\s*(\[\*\]|[A-Z_]+)", diagram, re.MULTILINE)
    return {
        (RunState(source.lower()), RunState(target.lower()))
        for source, target in arrows
        if "[*]" not in (source, target)
    }


def test_the_table_is_the_diagram() -> None:
    assert diagram_transitions() == set(TRANSITIONS)


@pytest.mark.parametrize(("current", "target"), sorted(product(RunState, RunState)))
def test_every_pair_is_allowed_exactly_when_drawn(current: RunState, target: RunState) -> None:
    if (current, target) in TRANSITIONS:
        assert allowed(current, target)
        require_transition(current, target)
    else:
        assert not allowed(current, target)
        with pytest.raises(InvalidTransitionError) as refused:
            require_transition(current, target)
        assert (refused.value.current, refused.value.target) == (current, target)


def test_finished_states_lead_nowhere_and_draft_is_only_left_by_validation() -> None:
    for state in FINISHED:
        assert not any(source == state for source, _ in TRANSITIONS)
    assert {target for source, target in TRANSITIONS if source == RunState.DRAFT} == {
        RunState.VALIDATED
    }
    assert ACTIVE.isdisjoint(FINISHED)
    assert list(sources(RunState.TERMINAL)) == [
        RunState.RUNNING,
        RunState.PAUSED,
        RunState.RECOVERY_REQUIRED,
    ]
