"""Ground-truth case construction from a real repository.

The pure parts are tested here; building a case needs a clone and validating one
needs Docker, so those stay out of the default suite.

Every rule below was learned by running against click and getting a wrong answer
first — that history is why each has a test rather than a comment.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eval.oss import (
    EXTRA_PYTEST_ARGS,
    MIN_COLLECTED,
    MIN_USEFUL_ISSUE_CHARS,
    OSSCase,
)

CASES_FILE = Path("eval/oss_cases.json")


def _case(**kw: object) -> OSSCase:
    base: dict[str, object] = {
        "repo": "pallets/click",
        "sha": "a" * 40,
        "title": "t",
        "issue": "i",
        "issue_source": "issue #1",
    }
    return OSSCase(**{**base, **kw})  # type: ignore[arg-type]


class TestTestCommand:
    def test_click_deselects_pager_tests(self) -> None:
        """click's pager tests spawn a subprocess that kills the run: the suite
        dies at ~92% with no summary, so every case reads as 'nothing failed'."""
        assert "not pager" in _case().test_command

    def test_other_repos_get_no_extra_args(self) -> None:
        """One repository's quirk must not become policy for all of them —
        `-k 'not pager'` would silently skip tests anywhere the word appears."""
        assert "not pager" not in _case(repo="some/other").test_command

    def test_the_shared_command_is_still_the_base(self) -> None:
        from featurepilot.graph.nodes.tester import TEST_COMMAND

        assert _case(repo="some/other").test_command == TEST_COMMAND

    def test_extras_are_declared_per_repo(self) -> None:
        assert set(EXTRA_PYTEST_ARGS) == {"pallets/click"}


class TestUsability:
    def test_a_case_with_no_fail_to_pass_is_unusable(self) -> None:
        """Nothing distinguishes a correct patch from no patch at all."""
        assert not _case().usable

    def test_a_case_with_fail_to_pass_is_usable(self) -> None:
        assert _case(fail_to_pass=frozenset({"tests/t.py::x"})).usable

    def test_the_collected_floor_is_meaningful(self) -> None:
        """click collects ~1600. A case reporting 1 or 2 is a broken
        reconstruction that no patch could affect."""
        assert 50 < MIN_COLLECTED < 1000

    def test_the_issue_length_floor_is_meaningful(self) -> None:
        """'Strip all ANSI sequences' is 24 characters and states a change, not a
        symptom — too thin to diagnose from."""
        assert MIN_USEFUL_ISSUE_CHARS >= 100


@pytest.mark.skipif(not CASES_FILE.exists(), reason="run `python -m eval.oss_build` first")
class TestBuiltCaseSet:
    """Assertions about the committed case set, so a bad rebuild is caught."""

    @staticmethod
    def _cases() -> list[dict]:
        return json.loads(CASES_FILE.read_text())["cases"]

    def test_every_case_has_ground_truth(self) -> None:
        for case in self._cases():
            assert case["fail_to_pass"], f"{case['sha'][:9]} has no FAIL_TO_PASS"

    def test_every_case_really_ran_its_suite(self) -> None:
        """Guards the failure that slipped through once: a case recorded with
        `1 of 1 collected`, which measures nothing."""
        for case in self._cases():
            assert case["collected_total"] >= MIN_COLLECTED, (
                f"{case['sha'][:9]} collected only {case['collected_total']}"
            )

    def test_most_issues_come_from_real_bug_reports(self) -> None:
        """A commit message often describes the fix, which turns diagnosis into
        transcription. Real linked issues describe symptoms."""
        cases = self._cases()
        from_issues = [c for c in cases if c["issue_source"].startswith("issue #")]
        assert len(from_issues) > len(cases) / 2, (
            f"only {len(from_issues)}/{len(cases)} come from real issues"
        )

    def test_issues_are_substantial_enough_to_plan_against(self) -> None:
        for case in self._cases():
            if case["issue_source"].startswith("issue #"):
                assert len(case["issue"]) >= 300, f"{case['sha'][:9]} issue is too thin"

    def test_cases_naming_the_file_are_flagged_not_hidden(self) -> None:
        """Real reports link to code — issue #2877 embeds a permalink to the very
        file that needs changing. That does not leak the fix, but it does remove
        the search. Recording it lets results be segmented; silently dropping such
        cases would bias the set toward unrealistically vague reports."""
        for case in self._cases():
            names_it = any(p in case["issue"] for p in case["source_files"])
            assert case["names_source_path"] == names_it, (
                f"{case['sha'][:9]} flag disagrees with its text"
            )

    def test_retrieval_is_still_exercised_by_most_cases(self) -> None:
        """If every issue named its file, click would prove nothing the fixture
        did not — the point of a 7.7x-context repo is that search is required."""
        cases = self._cases()
        needs_search = [c for c in cases if not c["names_source_path"]]
        assert len(needs_search) > len(cases) / 2, (
            f"only {len(needs_search)}/{len(cases)} cases require retrieval"
        )
