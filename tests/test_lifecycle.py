"""The transition table is load-bearing: routing and resume both read it, so a
wrong edge is a silently misrouted run rather than a crash."""

from __future__ import annotations

import pytest

from featurepilot.lifecycle import (
    BLOCKED_ON_HUMAN,
    TERMINAL,
    IllegalTransition,
    RunPhase,
    assert_transition,
    can_transition,
    is_terminal,
)


class TestHappyPath:
    def test_full_run_is_legal(self) -> None:
        path = [
            RunPhase.CREATED,
            RunPhase.PLANNING,
            RunPhase.WAITING_APPROVAL,
            RunPhase.CODING,
            RunPhase.TESTING,
            RunPhase.REVIEW,
            RunPhase.DONE,
        ]
        # Not strict=True: the offset slice is deliberately one shorter.
        for src, dst in zip(path, path[1:]):  # noqa: B905
            assert can_transition(src, dst), f"{src} -> {dst} should be legal"

    def test_repair_loop_is_legal(self) -> None:
        """The edge that makes this an agent rather than a one-shot patcher."""
        assert can_transition(RunPhase.TESTING, RunPhase.DEBUGGING)
        assert can_transition(RunPhase.DEBUGGING, RunPhase.CODING)
        assert can_transition(RunPhase.CODING, RunPhase.TESTING)

    def test_rejected_plan_can_be_replanned(self) -> None:
        assert can_transition(RunPhase.WAITING_APPROVAL, RunPhase.PLANNING)

    def test_rejected_review_returns_to_coder(self) -> None:
        assert can_transition(RunPhase.REVIEW, RunPhase.CODING)

    def test_approval_gate_may_be_skipped(self) -> None:
        """--yes with no open questions goes straight to coding."""
        assert can_transition(RunPhase.PLANNING, RunPhase.CODING)

    def test_indexing_is_reachable_for_phase_1b(self) -> None:
        assert can_transition(RunPhase.CREATED, RunPhase.INDEXING)
        assert can_transition(RunPhase.INDEXING, RunPhase.PLANNING)


class TestIllegalTransitions:
    @pytest.mark.parametrize(
        ("src", "dst"),
        [
            # Skipping the approval gate from CREATED bypasses planning entirely.
            (RunPhase.CREATED, RunPhase.CODING),
            # Cannot review a patch that was never tested.
            (RunPhase.CODING, RunPhase.REVIEW),
            # Cannot claim done straight out of testing without review.
            (RunPhase.TESTING, RunPhase.DONE),
            # Debugging must go back through the coder, not around it.
            (RunPhase.DEBUGGING, RunPhase.TESTING),
            (RunPhase.DEBUGGING, RunPhase.REVIEW),
            # Backwards into planning from code is not a supported flow.
            (RunPhase.CODING, RunPhase.PLANNING),
        ],
    )
    def test_rejected(self, src: RunPhase, dst: RunPhase) -> None:
        assert not can_transition(src, dst)
        with pytest.raises(IllegalTransition):
            assert_transition(src, dst)

    def test_error_names_the_legal_moves(self) -> None:
        """The message has to be actionable — this fires during development."""
        with pytest.raises(IllegalTransition, match="TESTING"):
            assert_transition(RunPhase.CODING, RunPhase.REVIEW)
        try:
            assert_transition(RunPhase.CODING, RunPhase.REVIEW)
        except IllegalTransition as exc:
            assert "allowed from CODING" in str(exc)
            assert "TESTING" in str(exc)


class TestFailureAndTerminals:
    @pytest.mark.parametrize("src", [p for p in RunPhase if p not in TERMINAL])
    def test_failed_reachable_from_any_live_phase(self, src: RunPhase) -> None:
        assert can_transition(src, RunPhase.FAILED)

    @pytest.mark.parametrize("src", sorted(TERMINAL))
    def test_terminals_are_dead_ends(self, src: RunPhase) -> None:
        assert is_terminal(src)
        for dst in RunPhase:
            assert not can_transition(src, dst), f"{src} should not reach {dst}"

    def test_assert_transition_returns_destination(self) -> None:
        """Used as `state.phase = assert_transition(state.phase, next_phase)`."""
        assert assert_transition(RunPhase.CODING, RunPhase.TESTING) is RunPhase.TESTING


def test_only_approval_blocks_on_a_human() -> None:
    """If this grows, the API needs another resume endpoint — so pin it."""
    assert frozenset({RunPhase.WAITING_APPROVAL}) == BLOCKED_ON_HUMAN
