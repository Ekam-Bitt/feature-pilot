"""Tester node.

**No model call.** The suite is run and its output parsed mechanically, because
asking a model whether tests passed invites it to tell us what we want to hear —
and this is the one signal the whole repair loop pivots on. Exit code and parsed
counts are facts; a model's opinion about them is not.

Results are judged against a baseline captured before any edit. See
`TesterOutput` for why: without one, fixing the issue you were asked about still
reports failure on any repository that had other failures already.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

from featurepilot.contracts import FailingTest, TesterOutput
from featurepilot.graph.nodes.base import Node
from featurepilot.graph.router import next_phase_after_tests
from featurepilot.graph.state import AgentState
from featurepilot.lifecycle import RunPhase
from featurepilot.tracing import traced

#: `--override-ini=filterwarnings=` neutralises a target repo's
#: `filterwarnings = ["error"]`. Without it, a repo whose code predates the
#: sandbox's pytest dies during *collection* — one new deprecation warning
#: becomes an error and the entire suite aborts, so every patch looks equally
#: broken. Observed on click: 1 test collected instead of 1705.
#:
#: The trade-off is real but small: a bug that manifests *as* a warning
#: becomes invisible. Toolchain drift breaking every run is the worse failure.
#:
#: `addopts` is deliberately NOT overridden — click uses it to deselect 31000
#: stress tests, and clearing it turns a 3-second suite into a timeout.
TEST_COMMAND = "pytest -q --tb=short -p no:cacheprovider --override-ini='filterwarnings='"

#: Paths treated as tests for the revert-and-verify pass.
_TEST_MARKERS = ("test_", "_test.py", "tests/", "conftest.py")


def _is_test_path(path: str) -> bool:
    lowered = path.lower()
    return any(marker in lowered for marker in _TEST_MARKERS)


_COUNT = re.compile(r"(\d+) (passed|failed|error|errors|skipped)")
#: pytest's short summary is `FAILED <nodeid>` optionally followed by
#: ` - <message>`. The node id is NOT whitespace-free: parametrised ids embed
#: the repr of their arguments, so real ids like
#:   test_echo_via_pager[test0- less ]
#: contain spaces. Capturing `\S+` truncates those at the first space, and
#: since `resolved`/`regressions` are set comparisons over these ids, the
#: scoring silently goes wrong rather than failing loudly. Capture the whole
#: line and split on the first ' - ' instead.
_FAILED = re.compile(r"^FAILED (.+)$", re.MULTILINE)
#: pytest reports collection errors with a different prefix to failures.
_ERROR_LINE = re.compile(r"^ERROR (.+)$", re.MULTILINE)


def collected_total(output: str) -> int:
    """Tests pytest accounted for: passed + failed + errors + skipped.

    Used to notice a suite that got smaller. Deleting a passing test is a way
    to turn a suite green that no pass/fail diff can detect.
    """
    counts = {kind: int(n) for n, kind in _COUNT.findall(output)}
    return sum(counts.values())


def _split_summary_line(line: str) -> tuple[str, str]:
    """Split `<nodeid> - <message>` on the first separator.

    Partitioning rather than regexing the id: the id may contain spaces but the
    ` - ` separator is what pytest actually writes between id and message.
    """
    node, sep, message = line.partition(" - ")
    return (node.strip(), message.strip()) if sep else (line.strip(), "")


def failing_ids(output: str) -> set[str]:
    """Test IDs that failed, from pytest's terse output.

    Exported because the baseline run needs exactly this and nothing else.
    """
    lines = _FAILED.findall(output) + _ERROR_LINE.findall(output)
    return {_split_summary_line(line)[0] for line in lines}


def parse(
    output: str,
    exit_code: int,
    *,
    baseline: Iterable[str] | None = None,
    baseline_total: int = 0,
    modified_test_files: list[str] | None = None,
) -> TesterOutput:
    """Turn pytest output into a TesterOutput, compared against `baseline`.

    Pure and exported so it can be tested against captured output. Unparseable
    output with a non-zero exit is reported as one synthetic failure rather than
    as a green run — silently treating "we could not tell" as success is how a
    broken patch reaches a PR.
    """
    counts = {kind: int(n) for n, kind in _COUNT.findall(output)}
    lines = _FAILED.findall(output) + _ERROR_LINE.findall(output)
    failing = [
        FailingTest(test_id=node, message=message)
        for node, message in (_split_summary_line(line) for line in lines)
    ]

    passed = counts.get("passed", 0)
    failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)

    if not counts and not failing and exit_code != 0:
        failing = [
            FailingTest(
                test_id="<test run>",
                message="the test command failed without a parseable pytest summary",
            )
        ]
        failed = 1

    now = {f.test_id for f in failing}
    regressions: list[str] = []
    resolved: list[str] = []
    pre_existing: list[str] = []
    before: set[str] = set()
    if baseline is not None:
        before = set(baseline)
        regressions = sorted(now - before)
        resolved = sorted(before - now)
        pre_existing = sorted(now & before)

    return TesterOutput(
        exit_code=exit_code,
        passed=passed,
        failed=max(failed, len(failing)) if exit_code != 0 else failed,
        failing_tests=failing[:40],
        raw_output=output,
        regressions=regressions,
        resolved=resolved,
        pre_existing=pre_existing,
        baseline_known=baseline is not None,
        baseline_size=len(before),
        baseline_total=baseline_total,
        collected_total=collected_total(output),
        modified_test_files=list(modified_test_files or []),
        verified_against_original_tests=bool(modified_test_files),
    )


class TesterNode(Node):
    name = "test"
    phase = RunPhase.TESTING

    @traced("run_tests", run_type="tool")
    async def invoke(self, state: AgentState) -> TesterOutput:
        sandbox = self.ctx.sandbox
        if sandbox is None:
            # Nothing to run against. Reported as a failure rather than a pass,
            # for the same reason as unparseable output.
            return TesterOutput(
                exit_code=1,
                failing_tests=[
                    FailingTest(test_id="<sandbox>", message="no sandbox is attached to this run")
                ],
            )
        baseline = state.get("baseline_failures")
        baseline_total = int(state.get("baseline_total", 0) or 0)

        # If the agent edited tests, the honest question is whether the fix holds
        # against the ORIGINAL ones. Revert just the test files and measure that;
        # a patch that only passes its own rewritten tests has not fixed anything.
        # Source changes are left in place, so legitimate work is unaffected.
        changed = await sandbox.changed_files()
        touched_tests = [p for p in changed if _is_test_path(p)]
        if touched_tests:
            await sandbox.restore_paths(touched_tests)

        result = await sandbox.exec(TEST_COMMAND, timeout=self.ctx.settings.test_timeout_s)
        output = result.combined
        if result.timed_out:
            output += "\n[the test run exceeded its timeout]"

        return parse(
            output,
            result.exit_code,
            baseline=baseline,
            baseline_total=baseline_total,
            modified_test_files=touched_tests,
        )

    def apply(self, state: AgentState, output: TesterOutput) -> dict[str, object]:  # type: ignore[override]
        return {
            "tests": output,
            "phase": self._transition(state, next_phase_after_tests(output.success)),
        }
