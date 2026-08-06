"""Baseline-aware test evaluation.

The signal the whole repair loop pivots on. Getting it wrong is expensive in both
directions: too strict and a correct patch is sent back to the debugger forever
(which is exactly what happened on the first real run, before a baseline
existed); too lax and a patch that broke the suite reaches a PR.
"""

from __future__ import annotations

import pytest

from featurepilot.contracts import TesterOutput
from featurepilot.graph.nodes.tester import TEST_COMMAND, failing_ids, parse

RED = """\
FAILED tests/test_pricing.py::test_tier - assert 0 == 6250
FAILED tests/test_cart.py::test_shipping
17 failed, 68 passed in 1.48s
"""

GREEN = "85 passed in 1.05s\n"

# The situation from the first live run: the patch fixed its own issue, and the
# other seeded defects were still failing.
PARTIAL = """\
FAILED tests/test_cart.py::test_shipping
10 failed, 75 passed in 1.40s
"""


class TestFailingIds:
    def test_extracts_node_ids(self) -> None:
        assert failing_ids(RED) == {
            "tests/test_pricing.py::test_tier",
            "tests/test_cart.py::test_shipping",
        }

    def test_green_output_has_none(self) -> None:
        assert failing_ids(GREEN) == set()

    def test_collection_errors_count(self) -> None:
        """A collection error is a failure, not an absence of one."""
        assert failing_ids("ERROR tests/test_x.py - ImportError\n1 error in 0.1s") == {
            "tests/test_x.py"
        }


class TestWithoutBaseline:
    def test_green_is_success(self) -> None:
        result = parse(GREEN, 0)
        assert result.all_green
        assert result.success

    def test_red_is_not(self) -> None:
        assert not parse(RED, 1).success

    def test_unparseable_nonzero_exit_is_a_failure(self) -> None:
        """ "We could not tell" must never read as success — that is how a broken
        patch reaches a PR."""
        result = parse("Segmentation fault", 139)
        assert not result.success
        assert result.failed == 1
        assert result.failing_tests[0].test_id == "<test run>"

    def test_unparseable_zero_exit_is_trusted(self) -> None:
        result = parse("no tests ran", 0)
        assert result.success


class TestAgainstABaseline:
    """The case the first live run exposed: fixing one issue in a repository that
    has other, unrelated failures."""

    def test_fixing_the_issue_succeeds_despite_other_failures(self) -> None:
        baseline = failing_ids(RED)
        result = parse(PARTIAL, 1, baseline=baseline)
        assert result.resolved == ["tests/test_pricing.py::test_tier"]
        assert result.regressions == []
        assert result.pre_existing == ["tests/test_cart.py::test_shipping"]
        assert result.success, "a correct patch must not be condemned by pre-existing failures"
        assert not result.all_green, "the suite is genuinely not green, and says so"

    def test_a_regression_fails_even_when_something_was_fixed(self) -> None:
        """Breaking a passing test outweighs fixing a failing one."""
        baseline = {"tests/test_pricing.py::test_tier"}
        output = "FAILED tests/test_api.py::test_health\n1 failed, 84 passed in 1s"
        result = parse(output, 1, baseline=baseline)
        assert result.regressions == ["tests/test_api.py::test_health"]
        assert result.resolved == ["tests/test_pricing.py::test_tier"]
        assert not result.success

    def test_fixing_nothing_is_not_success(self) -> None:
        """No regressions but no progress means the patch missed. That belongs
        with the debugger, not the reviewer."""
        baseline = failing_ids(RED)
        result = parse(RED, 1, baseline=baseline)
        assert result.regressions == []
        assert result.resolved == []
        assert not result.success

    def test_full_green_against_a_red_baseline_succeeds(self) -> None:
        result = parse(GREEN, 0, baseline=failing_ids(RED))
        assert len(result.resolved) == 2
        assert result.success

    def test_a_green_baseline_needs_only_no_regressions(self) -> None:
        """A healthy repository has nothing to resolve. Requiring a resolved test
        here would fail every run against a green repo — including a feature
        request where nothing was broken to begin with."""
        result = parse(GREEN, 0, baseline=set())
        assert result.resolved == []
        assert result.baseline_size == 0
        assert result.success

    def test_a_green_baseline_still_catches_regressions(self) -> None:
        output = "FAILED tests/test_api.py::test_health\n1 failed, 84 passed in 1s"
        result = parse(output, 1, baseline=set())
        assert result.regressions == ["tests/test_api.py::test_health"]
        assert not result.success

    def test_baseline_size_is_recorded(self) -> None:
        result = parse(PARTIAL, 1, baseline=failing_ids(RED))
        assert result.baseline_size == 2
        assert result.baseline_known


class TestContractDefaults:
    def test_a_bare_output_does_not_claim_a_baseline(self) -> None:
        """A TesterOutput built by hand (e.g. the no-sandbox path) must not look
        as though it was compared against anything."""
        result = TesterOutput(exit_code=1)
        assert not result.baseline_known
        assert not result.success

    @pytest.mark.parametrize("exit_code", [0, 1, 137])
    def test_all_green_tracks_the_exit_code(self, exit_code: int) -> None:
        result = TesterOutput(exit_code=exit_code, passed=85, failed=0)
        assert result.all_green is (exit_code == 0)


class TestSuiteIntegrity:
    """Removing a passing test is a way to make a suite green that no pass/fail
    comparison can detect — deleting a passing test produces no regression. The
    collected-count check is the only thing standing between that and success."""

    def test_a_shrinking_suite_is_not_success(self) -> None:
        # Baseline: 85 collected, 2 failing. Now: 83 collected, 0 failing.
        result = parse("83 passed in 1s", 0, baseline=failing_ids(RED), baseline_total=85)
        assert result.all_green, "the suite does look green"
        assert result.tests_disappeared == 2
        assert not result.success, "but two tests vanished, so it is not a fix"

    def test_a_stable_suite_is_fine(self) -> None:
        result = parse("85 passed in 1s", 0, baseline=failing_ids(RED), baseline_total=85)
        assert result.tests_disappeared == 0
        assert result.success

    def test_a_growing_suite_is_fine(self) -> None:
        """Adding tests for new code is good practice, not a violation."""
        result = parse("88 passed in 1s", 0, baseline=failing_ids(RED), baseline_total=85)
        assert result.tests_disappeared == 0
        assert result.success

    def test_no_baseline_total_disables_the_check(self) -> None:
        """Without a baseline count there is nothing to compare against, and
        guessing would produce false accusations."""
        result = parse("10 passed in 1s", 0, baseline=failing_ids(RED), baseline_total=0)
        assert result.tests_disappeared == 0

    def test_counts_include_skips_and_errors(self) -> None:
        """Skipping a test also removes it as a signal, so it must count."""
        from featurepilot.graph.nodes.tester import collected_total

        assert collected_total("2 failed, 80 passed, 3 skipped in 1s") == 85


class TestVerifiedAgainstOriginalTests:
    """ "Did the agent bend the tests to fit the code" is the central trust
    question for an autonomous coding agent. Reverting its test edits and
    re-running makes it a measurement rather than a reviewer's judgement call."""

    def test_records_which_test_files_were_modified(self) -> None:
        result = parse(
            GREEN,
            0,
            baseline=failing_ids(RED),
            baseline_total=85,
            modified_test_files=["tests/test_promotions.py"],
        )
        assert result.modified_test_files == ["tests/test_promotions.py"]
        assert result.verified_against_original_tests

    def test_an_untouched_suite_is_not_marked_verified(self) -> None:
        """The flag must mean something: claiming verification that never
        happened is worse than not claiming it."""
        result = parse(GREEN, 0, baseline=failing_ids(RED), baseline_total=85)
        assert result.modified_test_files == []
        assert not result.verified_against_original_tests

    def test_a_fix_that_only_passes_its_own_tests_fails(self) -> None:
        """After reverting the agent's test edits the original tests still fail,
        so nothing was resolved and the patch does not pass."""
        result = parse(
            RED,
            1,
            baseline=failing_ids(RED),
            baseline_total=85,
            modified_test_files=["tests/test_pricing.py"],
        )
        assert result.resolved == []
        assert not result.success

    def test_a_genuine_fix_survives_the_revert(self) -> None:
        result = parse(
            PARTIAL,
            1,
            baseline=failing_ids(RED),
            baseline_total=85,
            modified_test_files=["tests/test_promotions.py"],
        )
        assert result.resolved == ["tests/test_pricing.py::test_tier"]
        assert result.success


def test_test_path_detection() -> None:
    from featurepilot.graph.nodes.tester import _is_test_path

    for path in ("tests/test_cart.py", "src/foo_test.py", "tests/conftest.py"):
        assert _is_test_path(path), path
    for path in ("src/shopsvc/cart.py", "README.md", "src/latest.py"):
        assert not _is_test_path(path), path


class TestRealWorldNodeIds:
    """Regression guard from running against a real repository.

    pytest node ids are not whitespace-free — parametrised ids embed the repr of
    their arguments, so `test_echo_via_pager[test0- less ]` is a genuine id from
    click's suite. Capturing `\\S+` truncated every one of those at the first
    space, collapsing 24 distinct failures into 8 ids. Because `resolved` and
    `regressions` are set comparisons over these ids, the scoring went silently
    wrong rather than failing loudly.
    """

    CLICK_OUTPUT = """\
FAILED tests/test_utils/test_echo_via_pager.py::test_echo_via_pager[test0-less]
FAILED tests/test_utils/test_echo_via_pager.py::test_echo_via_pager[test0- less]
FAILED tests/test_utils/test_echo_via_pager.py::test_echo_via_pager[test0- less ]
FAILED tests/test_basic.py::test_other - AssertionError: expected 1 got 2
24 failed, 1916 passed, 24 skipped in 2.99s
"""

    def test_spaces_inside_parametrised_ids_are_preserved(self) -> None:
        ids = failing_ids(self.CLICK_OUTPUT)
        assert len(ids) == 4, f"ids collapsed: {sorted(ids)}"
        assert "tests/test_utils/test_echo_via_pager.py::test_echo_via_pager[test0- less ]" in ids

    def test_ids_that_differ_only_by_whitespace_stay_distinct(self) -> None:
        """These three are different tests. Treating them as one would make a
        fix for one look like a fix for all three."""
        ids = failing_ids(self.CLICK_OUTPUT)
        pager = {i for i in ids if "echo_via_pager" in i}
        assert len(pager) == 3

    def test_the_message_is_still_split_off(self) -> None:
        result = parse(self.CLICK_OUTPUT, 1)
        by_id = {t.test_id: t.message for t in result.failing_tests}
        assert by_id["tests/test_basic.py::test_other"] == "AssertionError: expected 1 got 2"

    def test_an_id_with_no_message_has_an_empty_one(self) -> None:
        result = parse(self.CLICK_OUTPUT, 1)
        by_id = {t.test_id: t.message for t in result.failing_tests}
        assert (
            by_id["tests/test_utils/test_echo_via_pager.py::test_echo_via_pager[test0-less]"] == ""
        )

    def test_scoring_is_correct_across_a_fix(self) -> None:
        """The bug's real cost: with truncated ids, fixing one parametrised case
        looked like fixing all of them."""
        baseline = failing_ids(self.CLICK_OUTPUT)
        after = """\
FAILED tests/test_utils/test_echo_via_pager.py::test_echo_via_pager[test0- less]
FAILED tests/test_utils/test_echo_via_pager.py::test_echo_via_pager[test0- less ]
FAILED tests/test_basic.py::test_other - AssertionError: expected 1 got 2
23 failed, 1917 passed, 24 skipped in 2.99s
"""
        result = parse(after, 1, baseline=baseline, baseline_total=1964)
        assert result.resolved == [
            "tests/test_utils/test_echo_via_pager.py::test_echo_via_pager[test0-less]"
        ]
        assert result.regressions == []


class TestTestCommand:
    """The command is harness policy, and two parts of it were learned the hard
    way against a real repository."""

    def test_neutralises_warnings_as_errors(self) -> None:
        """A repo with `filterwarnings = ["error"]` whose code predates the
        sandbox's pytest dies during collection — one new deprecation becomes an
        error and the whole suite aborts. Observed on click: 1 collected, not 1705."""
        assert "filterwarnings=" in TEST_COMMAND

    def test_does_not_clear_addopts(self) -> None:
        """click uses addopts to deselect 31000 stress tests. Clearing it turns a
        3-second suite into a timeout."""
        assert "addopts" not in TEST_COMMAND

    def test_disables_the_cache_plugin(self) -> None:
        """Writes to .pytest_cache would show up as agent-made changes in the diff."""
        assert "no:cacheprovider" in TEST_COMMAND
