"""Graph state.

A TypedDict rather than a Pydantic model, because LangGraph merges partial
updates per node — returning `{"tests": ...}` and having it folded in is the
whole ergonomic win, and a Pydantic model would require reconstructing the
object on every node return.

The *values* are still typed contracts, so nothing here is a loose dict. The
phase field is what makes routing a pure function and resume deterministic.
"""

from __future__ import annotations

from typing import Annotated, TypedDict

from langchain_core.messages import AnyMessage
from langgraph.graph.message import add_messages

from featurepilot.contracts import (
    CoderOutput,
    CriticOutput,
    DebuggerOutput,
    HumanDecision,
    PlannerOutput,
    PRSummary,
    RetrieverOutput,
    ReviewerOutput,
    TesterOutput,
)
from featurepilot.lifecycle import RunPhase


class AgentState(TypedDict, total=False):
    # --- identity, set once at run creation -------------------------------
    run_id: str
    repo_path: str
    issue: str
    issue_ref: str

    # --- control ----------------------------------------------------------
    phase: RunPhase
    #: Coding attempts consumed. Bounded by Settings.max_attempts; the router
    #: reads it to decide repair-vs-give-up, and nodes read it to decide whether
    #: to escalate the model tier.
    attempt: int
    #: Test IDs failing before any edit. Captured once at run start so the
    #: tester can tell "the patch broke this" from "this was already broken".
    baseline_failures: list[str]
    #: Tests collected at baseline, so a shrinking suite is detectable.
    baseline_total: int

    #: Set when the run fails. Populated on the transition to FAILED so the
    #: reason survives into the checkpoint rather than only reaching a log.
    error: str | None

    # --- node outputs -----------------------------------------------------
    plan: PlannerOutput | None
    context: RetrieverOutput | None
    code: CoderOutput | None
    critique: CriticOutput | None  # Phase 1B
    tests: TesterOutput | None
    diagnosis: DebuggerOutput | None
    review: ReviewerOutput | None
    pr: PRSummary | None

    # --- human input ------------------------------------------------------
    #: Written by the API/CLI when resuming from an interrupt.
    decision: HumanDecision | None

    # --- conversation -----------------------------------------------------
    #: Kept for traceability and for nodes that benefit from prior turns.
    #: add_messages appends rather than replaces.
    messages: Annotated[list[AnyMessage], add_messages]


def new_state(*, run_id: str, repo_path: str, issue: str, issue_ref: str) -> AgentState:
    """Initial state. Every optional key is set explicitly so a node never has
    to distinguish "absent" from "not yet produced"."""
    return AgentState(
        run_id=run_id,
        repo_path=repo_path,
        issue=issue,
        issue_ref=issue_ref,
        phase=RunPhase.CREATED,
        attempt=0,
        baseline_failures=[],
        baseline_total=0,
        error=None,
        plan=None,
        context=None,
        code=None,
        critique=None,
        tests=None,
        diagnosis=None,
        review=None,
        pr=None,
        decision=None,
        messages=[],
    )
