"""The deterministic supervisor.

Every one of these runs with no model, no container, and no database — which is
the whole argument for making the router a pure function. Routing bugs are
otherwise the worst kind: the run doesn't crash, it just quietly does the wrong
thing (skips review, loops forever, gives up early).
"""

from __future__ import annotations

import pytest

from featurepilot.contracts import (
    DebuggerOutput,
    FailingTest,
    HumanDecision,
    PlannerOutput,
    PlanStep,
    RetrieverOutput,
    ReviewerOutput,
    TesterOutput,
)
from featurepilot.graph.router import (
    APPROVAL,
    CODE,
    DEBUG,
    FINISH,
    PLAN,
    RETRIEVE,
    REVIEW,
    SUMMARIZE,
    TEST,
    next_phase_after_tests,
    route,
)
from featurepilot.graph.state import AgentState, new_state
from featurepilot.lifecycle import RunPhase


def state_at(phase: RunPhase, **overrides: object) -> AgentState:
    base = new_state(run_id="r", repo_path="/tmp/repo", issue="i", issue_ref="ref")
    base["phase"] = phase
    base.update(overrides)  # type: ignore[typeddict-item]
    return base


PLAN_OK = PlannerOutput(
    summary="fix the boundary",
    steps=[PlanStep(description="use >=", files=["src/shopsvc/pricing.py"])],
    confidence="high",
)
CONTEXT = RetrieverOutput(files=["src/shopsvc/pricing.py"], strategy="filesystem")
GREEN = TesterOutput(exit_code=0, passed=85, failed=0)
RED = TesterOutput(
    exit_code=1,
    passed=68,
    failed=17,
    failing_tests=[FailingTest(test_id="tests/test_pricing.py::x", message="assert 0 == 6250")],
)


class TestForwardPath:
    def test_new_run_retrieves_first(self) -> None:
        """The planner should read real code, not guess at structure."""
        assert route(state_at(RunPhase.CREATED)) == RETRIEVE

    def test_planning_without_context_retrieves(self) -> None:
        assert route(state_at(RunPhase.PLANNING)) == RETRIEVE

    def test_planning_with_context_plans(self) -> None:
        assert route(state_at(RunPhase.PLANNING, context=CONTEXT)) == PLAN

    def test_planning_with_a_plan_goes_to_approval(self) -> None:
        assert route(state_at(RunPhase.PLANNING, context=CONTEXT, plan=PLAN_OK)) == APPROVAL

    def test_approved_plan_codes(self) -> None:
        decision = HumanDecision(verdict="approve")
        assert route(state_at(RunPhase.WAITING_APPROVAL, decision=decision)) == CODE

    def test_coding_phase_codes(self) -> None:
        assert route(state_at(RunPhase.CODING)) == CODE

    def test_testing_without_results_tests(self) -> None:
        assert route(state_at(RunPhase.TESTING)) == TEST

    def test_review_phase_reviews(self) -> None:
        assert route(state_at(RunPhase.REVIEW)) == REVIEW

    def test_approved_review_summarises(self) -> None:
        review = ReviewerOutput(verdict="approve")
        assert route(state_at(RunPhase.REVIEW, review=review)) == SUMMARIZE


class TestGreenTestsStillGetReviewed:
    """Passing tests are necessary, not sufficient. A patch that edited a test to
    agree with the code is green and wrong, so review is not skippable."""

    def test_green_goes_to_review_not_the_summary(self) -> None:
        assert route(state_at(RunPhase.TESTING, tests=GREEN)) == REVIEW

    def test_phase_helper_agrees_with_the_router(self) -> None:
        """The tester node and the router must not disagree about where green
        goes; sharing this helper makes them agree by construction."""
        assert next_phase_after_tests(green=True) is RunPhase.REVIEW
        assert next_phase_after_tests(green=False) is RunPhase.DEBUGGING


class TestRepairLoop:
    def test_red_tests_debug(self) -> None:
        assert route(state_at(RunPhase.TESTING, tests=RED)) == DEBUG

    def test_debugging_without_a_diagnosis_debugs(self) -> None:
        assert route(state_at(RunPhase.DEBUGGING)) == DEBUG

    def test_retryable_diagnosis_returns_to_the_coder(self) -> None:
        """The edge that makes this an agent rather than a one-shot patcher."""
        diagnosis = DebuggerOutput(
            failure_category="assertion", root_cause="wrong basis", retry=True
        )
        assert route(state_at(RunPhase.DEBUGGING, diagnosis=diagnosis, attempt=1)) == CODE

    def test_unretryable_diagnosis_stops(self) -> None:
        """An environmental or pre-existing failure will not be fixed by trying
        the same thing again."""
        diagnosis = DebuggerOutput(
            failure_category="env", root_cause="missing fixture", retry=False
        )
        assert route(state_at(RunPhase.DEBUGGING, diagnosis=diagnosis, attempt=1)) == FINISH

    def test_attempt_budget_stops_a_willing_debugger(self) -> None:
        """Bounded attempts are load-bearing: an agent that loops is an agent
        that bills."""
        diagnosis = DebuggerOutput(failure_category="assertion", root_cause="x", retry=True)
        assert (
            route(state_at(RunPhase.DEBUGGING, diagnosis=diagnosis, attempt=3), max_attempts=3)
            == FINISH
        )

    def test_budget_is_configurable(self) -> None:
        diagnosis = DebuggerOutput(failure_category="assertion", root_cause="x", retry=True)
        st = state_at(RunPhase.DEBUGGING, diagnosis=diagnosis, attempt=3)
        assert route(st, max_attempts=5) == CODE

    def test_rejected_review_returns_to_the_coder(self) -> None:
        review = ReviewerOutput(verdict="reject", blocking=["masks the symptom"])
        assert route(state_at(RunPhase.REVIEW, review=review, attempt=1)) == CODE

    def test_rejected_review_at_the_budget_summarises_anyway(self) -> None:
        """Better to hand back a documented imperfect patch than to spin."""
        review = ReviewerOutput(verdict="reject", blocking=["nit"])
        assert (
            route(state_at(RunPhase.REVIEW, review=review, attempt=3), max_attempts=3) == SUMMARIZE
        )


class TestHumanGate:
    def test_unanswered_interrupt_stays_parked(self) -> None:
        """Without a decision the run must not proceed to code on its own."""
        assert route(state_at(RunPhase.WAITING_APPROVAL)) == APPROVAL

    def test_rejected_plan_replans_rather_than_coding_it(self) -> None:
        decision = HumanDecision(verdict="reject", feedback="wrong module")
        assert route(state_at(RunPhase.WAITING_APPROVAL, decision=decision)) == PLAN


class TestTerminals:
    @pytest.mark.parametrize("phase", [RunPhase.DONE, RunPhase.FAILED])
    def test_terminal_phases_end(self, phase: RunPhase) -> None:
        assert route(state_at(phase)) == FINISH

    def test_terminal_wins_over_populated_outputs(self) -> None:
        """A failed run with results present must still stop."""
        assert route(state_at(RunPhase.FAILED, tests=RED, plan=PLAN_OK)) == FINISH


class TestTotality:
    @pytest.mark.parametrize("phase", list(RunPhase))
    def test_every_phase_has_a_rule(self, phase: RunPhase) -> None:
        """A phase with no rule would raise mid-run. Adding a phase to the enum
        without a route is exactly the mistake this catches."""
        assert route(state_at(phase))

    def test_routing_does_not_mutate_state(self) -> None:
        """The router is called on every transition; a hidden write here would be
        very hard to trace."""
        st = state_at(RunPhase.TESTING, tests=RED)
        before = dict(st)
        route(st)
        assert dict(st) == before

    def test_is_deterministic(self) -> None:
        st = state_at(RunPhase.TESTING, tests=RED)
        assert len({route(st) for _ in range(20)}) == 1


class TestPhaseSurvivesStorage:
    """A checkpoint round-trip returns the phase as a plain `str` unless the enum
    is registered with the serialiser. Every branch in `route` tests identity, so
    without coercion a resumed run dies on "router has no rule for phase" — a bug
    invisible to any single-process test."""

    @pytest.mark.parametrize("phase", list(RunPhase))
    def test_string_phases_route_identically_to_enums(self, phase: RunPhase) -> None:
        as_enum = state_at(phase)
        as_string = state_at(phase)
        as_string["phase"] = str(phase)  # type: ignore[typeddict-item]
        assert route(as_string) == route(as_enum)

    def test_a_resumed_approval_still_parks(self) -> None:
        """The exact shape that broke: parked on approval, reloaded from storage."""
        st = state_at(RunPhase.WAITING_APPROVAL)
        st["phase"] = "WAITING_APPROVAL"  # type: ignore[typeddict-item]
        assert route(st) == APPROVAL

    def test_an_unknown_phase_is_still_rejected(self) -> None:
        """Coercion must not turn a genuine bug into a silent default."""
        st = state_at(RunPhase.CREATED)
        st["phase"] = "NOT_A_PHASE"  # type: ignore[typeddict-item]
        with pytest.raises(AssertionError, match="unknown run phase"):
            route(st)
