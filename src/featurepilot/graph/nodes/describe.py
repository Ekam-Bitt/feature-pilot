"""Shared prompt fragments.

`describe_tests` exists because it drifted. The debugger was taught to read
results against the baseline while the reviewer and summariser were still shown
raw counts, so on a repository with pre-existing failures the reviewer read
"75 passed, 10 failed" and concluded a correct patch was broken — and the PR
summary said so.

One function, three callers. A change to how results are framed now reaches all
of them or none.
"""

from __future__ import annotations

from featurepilot.contracts import TesterOutput


def describe_tests(tests: TesterOutput | None, *, detail: bool = False) -> str:
    """Explain a test run in terms of what this patch changed.

    Leads with the delta rather than the totals. On a repository that started with
    failures, the totals are actively misleading: "10 failed" invites the reader
    to condemn a patch that fixed everything it was asked to.
    """
    if tests is None:
        return "No test results are available."

    lines = [f"{tests.passed} passed, {tests.failed} failed (exit code {tests.exit_code})"]

    if not tests.baseline_known:
        lines.append("")
        lines.append("No baseline was captured, so these counts are the only signal.")
        if detail and tests.failing_tests:
            lines.append("")
            lines.append("Failing tests:")
            lines.extend(f"- {t.test_id}: {t.message}" for t in tests.failing_tests[:20])
        return "\n".join(lines)

    lines.append("")
    lines.append(
        "Read this against the baseline taken before any edit — "
        f"{tests.baseline_size} test(s) were already failing then."
    )

    if tests.modified_test_files:
        lines.append("")
        lines.append(
            "NOTE: this patch modified test files "
            f"({', '.join(tests.modified_test_files)}). Those edits were REVERTED "
            "before the run below, so these results reflect the original tests. "
            "The fix stands on its own, but check whether the test edits were "
            "appropriate."
        )

    if tests.tests_disappeared:
        lines.append("")
        lines.append(
            f"WARNING: {tests.tests_disappeared} test(s) are no longer collected. "
            "Tests were deleted or skipped. Removing a test is never a fix."
        )

    if tests.regressions:
        lines.append("")
        lines.append(f"BROKEN BY THIS PATCH ({len(tests.regressions)}) — these are the problem:")
        detail_by_id = {t.test_id: t.message for t in tests.failing_tests}
        lines.extend(f"- {tid}: {detail_by_id.get(tid, '')}" for tid in tests.regressions[:20])
    else:
        lines.append("")
        lines.append("This patch broke nothing.")

    if tests.resolved:
        lines.append("")
        lines.append(f"FIXED BY THIS PATCH ({len(tests.resolved)}):")
        lines.extend(f"- {tid}" for tid in tests.resolved[:20])
    else:
        lines.append("")
        lines.append("This patch fixed nothing.")

    if tests.pre_existing:
        lines.append("")
        lines.append(
            f"STILL FAILING, AND ALREADY FAILING BEFORE THIS PATCH "
            f"({len(tests.pre_existing)}) — out of scope for this issue, caused by "
            "unrelated defects elsewhere in the repository. Do not treat these as "
            "evidence that the patch is wrong, and do not try to fix them:"
        )
        lines.extend(f"- {tid}" for tid in tests.pre_existing[:20])

    return "\n".join(lines)


def verdict_hint(tests: TesterOutput | None) -> str:
    """A one-line statement of whether the patch met its bar.

    Given to the reviewer so it is judging the right question. Without it, a
    reviewer looking at a non-green suite reliably rejects a correct patch.
    """
    if tests is None:
        return "No test results are available."
    if not tests.baseline_known:
        return "The suite is green." if tests.all_green else "The suite is not green."
    if tests.tests_disappeared:
        return (
            f"This patch removed {tests.tests_disappeared} test(s) from the suite, "
            "which is disqualifying regardless of what else it did."
        )
    if tests.regressions:
        return "This patch broke previously-passing tests, which is disqualifying."
    if tests.resolved:
        return (
            f"This patch fixed {len(tests.resolved)} previously-failing test(s) and broke "
            "none. It has met the test bar; judge it on correctness and scope."
        )
    if tests.baseline_size == 0:
        return "The repository started green and remains green."
    return "This patch neither fixed nor broke anything."
