"""The supervisor.

**A pure function, not an LLM call.** The next node is determined by
`(phase, last typed output)`, which is possible only because every node returns a
Pydantic contract rather than prose. Three consequences:

- Routing is unit-testable with no model and no container — see test_router.py.
- It costs zero tokens and adds zero latency, on every transition.
- It cannot be the bottleneck the centralised-supervisor critique warns about,
  because there is no per-decision inference to serialise on.

An LLM router earns its place when the decision is genuinely ambiguous. Here it
never is: a red suite goes to the debugger, a green one to the reviewer. Spending
a model call to rediscover that each turn would be waste dressed as architecture.
"""

from __future__ import annotations

from typing import Final

from featurepilot.graph.state import AgentState
from featurepilot.lifecycle import RunPhase

#: Graph vertex names. Kept as constants because the router returns them and
#: build.py wires them; a typo in either place would be a silent dead end.
RETRIEVE: Final = "retrieve"
PLAN: Final = "plan"
APPROVAL: Final = "approval"
CODE: Final = "code"
TEST: Final = "test"
DEBUG: Final = "debug"
REVIEW: Final = "review"
SUMMARIZE: Final = "summarize"
FINISH: Final = "__end__"


def _as_phase(value: object) -> RunPhase:
    """Normalise a phase that may have come back from storage as a string."""
    if isinstance(value, RunPhase):
        return value
    try:
        return RunPhase(str(value))
    except ValueError:
        raise AssertionError(f"unknown run phase {value!r}") from None


def route(state: AgentState, *, max_attempts: int = 3) -> str:
    """Return the next vertex for `state`.

    Reads `phase` plus the relevant typed output and nothing else. No mutation,
    no I/O — feed it a dict and assert on the answer.

    The phase is coerced rather than trusted. A checkpoint round-trip returns it
    as a plain `str` unless the enum is registered with the serialiser, and every
    branch below tests identity — so a resumed run would fall through to "no rule
    for phase" and die. Registering the enum fixes the storage; coercing here
    means correctness does not depend on having remembered to.
    """
    phase = _as_phase(state.get("phase", RunPhase.CREATED))

    if phase in (RunPhase.DONE, RunPhase.FAILED):
        return FINISH

    if phase is RunPhase.CREATED:
        return RETRIEVE

    if phase is RunPhase.INDEXING:  # Phase 1B only
        return RETRIEVE

    if phase is RunPhase.PLANNING:
        # Retrieval runs before planning so the planner reads real code rather
        # than guessing at structure.
        if state.get("context") is None:
            return RETRIEVE
        # A rejection must re-plan. Checking this before the `plan is None` test
        # matters: the rejected plan is still in state, so without it the run
        # routes back to APPROVAL and loops between approve and reject forever.
        decision = state.get("decision")
        if decision is not None and decision.verdict == "reject":
            return PLAN
        if state.get("plan") is None:
            return PLAN
        return APPROVAL

    if phase is RunPhase.WAITING_APPROVAL:
        decision = state.get("decision")
        if decision is None:
            # Still parked on the human; the interrupt has not been answered.
            return APPROVAL
        if decision.verdict == "reject":
            # Re-plan with the feedback rather than coding a rejected plan.
            return PLAN
        return CODE

    if phase is RunPhase.CODING:
        return CODE

    if phase is RunPhase.TESTING:
        tests = state.get("tests")
        if tests is None:
            return TEST
        # `success` rather than `all_green`: the suite is judged against the
        # baseline taken before any edit, so pre-existing failures elsewhere in
        # the repository do not condemn a correct patch.
        #
        # Success goes to REVIEW, not straight to the summary: passing tests are
        # necessary but not sufficient, and a patch that edited a test to make it
        # agree with the code is green and wrong.
        return REVIEW if tests.success else DEBUG

    if phase is RunPhase.DEBUGGING:
        diagnosis = state.get("diagnosis")
        if diagnosis is None:
            return DEBUG
        # Two independent reasons to stop trying: the debugger judged a retry
        # pointless, or the attempt budget is spent. Either way, finishing with a
        # clear failure beats burning another attempt to arrive back here.
        if not diagnosis.retry or state.get("attempt", 0) >= max_attempts:
            return FINISH
        return CODE

    if phase is RunPhase.REVIEW:
        review = state.get("review")
        if review is None:
            return REVIEW
        if review.verdict == "reject" and state.get("attempt", 0) < max_attempts:
            return CODE
        return SUMMARIZE

    raise AssertionError(f"router has no rule for phase {phase!r}")


def next_phase_after_tests(green: bool) -> RunPhase:
    """Phase a passing/failing test run moves to. Split out so the tester node
    and the router agree by construction rather than by coincidence."""
    return RunPhase.REVIEW if green else RunPhase.DEBUGGING
