"""Explicit run lifecycle.

Graph progress is a single enum on the state rather than something inferred
from which keys happen to be populated. That buys three things:

- **Resume** reads one field. A checkpoint restore knows exactly where it was.
- **Routing** is a pure function of (phase, last output) — see graph/router.py.
- **Metrics and UI** get a stable vocabulary for "where is this run".

Illegal transitions raise. A run that reaches an impossible state is a bug we
want to see immediately, not a run that quietly does the wrong thing.
"""

from __future__ import annotations

from enum import StrEnum


class RunPhase(StrEnum):
    CREATED = "CREATED"
    # Declared from day one but only *entered* in Phase 1B, when a retriever
    # needs an index. 1A goes CREATED -> PLANNING directly. Declaring it now
    # avoids an enum migration and a transition-table rewrite later.
    INDEXING = "INDEXING"
    PLANNING = "PLANNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    CODING = "CODING"
    TESTING = "TESTING"
    DEBUGGING = "DEBUGGING"
    REVIEW = "REVIEW"
    DONE = "DONE"
    FAILED = "FAILED"


#: Legal forward transitions. FAILED is reachable from anywhere and is handled
#: separately rather than being listed on every row.
_TRANSITIONS: dict[RunPhase, frozenset[RunPhase]] = {
    RunPhase.CREATED: frozenset({RunPhase.INDEXING, RunPhase.PLANNING}),
    RunPhase.INDEXING: frozenset({RunPhase.PLANNING}),
    # Planning may skip approval when the plan raises no questions and the
    # caller opted out of the gate (--yes).
    RunPhase.PLANNING: frozenset({RunPhase.WAITING_APPROVAL, RunPhase.CODING}),
    # Re-planning is legal: a human can reject a plan and send it back.
    RunPhase.WAITING_APPROVAL: frozenset({RunPhase.CODING, RunPhase.PLANNING}),
    RunPhase.CODING: frozenset({RunPhase.TESTING}),
    # Green -> REVIEW, red -> DEBUGGING.
    RunPhase.TESTING: frozenset({RunPhase.REVIEW, RunPhase.DEBUGGING}),
    # The repair loop. DEBUGGING -> CODING is the edge that makes this an
    # agent rather than a one-shot patcher.
    RunPhase.DEBUGGING: frozenset({RunPhase.CODING}),
    # Reviewer rejection sends work back to the coder.
    RunPhase.REVIEW: frozenset({RunPhase.DONE, RunPhase.CODING}),
    RunPhase.DONE: frozenset(),
    RunPhase.FAILED: frozenset(),
}

TERMINAL: frozenset[RunPhase] = frozenset({RunPhase.DONE, RunPhase.FAILED})

#: Phases where the graph is parked waiting on a human, not on itself.
BLOCKED_ON_HUMAN: frozenset[RunPhase] = frozenset({RunPhase.WAITING_APPROVAL})


class IllegalTransition(RuntimeError):
    def __init__(self, src: RunPhase, dst: RunPhase) -> None:
        allowed = sorted(p.value for p in _TRANSITIONS.get(src, frozenset()))
        super().__init__(
            f"illegal phase transition {src.value} -> {dst.value}; "
            f"allowed from {src.value}: {allowed or ['<terminal>']} (or FAILED)"
        )
        self.src = src
        self.dst = dst


def can_transition(src: RunPhase, dst: RunPhase) -> bool:
    """FAILED is reachable from any non-terminal phase — anything can break."""
    if dst is RunPhase.FAILED:
        return src not in TERMINAL
    return dst in _TRANSITIONS.get(src, frozenset())


def assert_transition(src: RunPhase, dst: RunPhase) -> RunPhase:
    """Return `dst`, or raise. Call this at every phase change instead of
    assigning the field directly."""
    if not can_transition(src, dst):
        raise IllegalTransition(src, dst)
    return dst


def is_terminal(phase: RunPhase) -> bool:
    return phase in TERMINAL
