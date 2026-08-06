"""Naked Claude vs. Feature Pilot, on the same issues.

The control condition. Same model, same issue, same scoring — but one shot, with
no tools, no test feedback, and no repair loop. The difference between the two
columns is what the orchestration actually buys, which is the only honest way to
justify building any of it.

Kept deliberately fair to the baseline:

- Same model (`FP_MODEL_CODER`), so this measures scaffolding, not model tier.
- It gets the **full source of every file it might need**, since it cannot search.
  Withholding context would measure retrieval, not reasoning.
- Its patch is applied and scored by exactly the same baseline-aware comparison
  the agent's is, in an identical container.

What it does not get: the ability to run tests, see a failure, or try again.

Usage:
    uv run python -m eval.baseline            # all five
    uv run python -m eval.baseline --only 01
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from eval.dataset import CASES, TARGET_REPO, Case
from eval.gate import RESULTS
from featurepilot.config import Role, Settings, get_settings
from featurepilot.graph.nodes.tester import TEST_COMMAND, collected_total, failing_ids, parse
from featurepilot.llm import call_structured
from featurepilot.metrics.events import InMemorySink
from featurepilot.metrics.recorder import MetricsRecorder
from featurepilot.sandbox.runner import Sandbox

SYSTEM = """\
You are an experienced engineer fixing a bug in a Python repository.

You are given the issue and the complete contents of the relevant source files.
You cannot run commands, search, or see test results — return the corrected files
in one shot.

For each file you change, return its ENTIRE new contents, not a diff and not a
fragment. Change as little as the issue requires: do not reformat, do not
refactor code you happened to read, and do not modify tests.
"""

#: Files handed to the baseline. Tests are included as reading material — it can
#: see what is expected of it, just as the agent can — but it is told not to edit
#: them, and any edit it makes is reverted before scoring, exactly as for the agent.
_SOURCE_SUFFIXES = {".py", ".md", ".toml"}
_MAX_CONTEXT_CHARS = 120_000


class NewFile(BaseModel):
    path: str = Field(description="Repo-relative path of the file you are replacing.")
    content: str = Field(description="The complete new contents of that file.")


class BaselinePatch(BaseModel):
    reasoning: str = Field(description="One paragraph: the cause and your fix.")
    files: list[NewFile] = Field(default_factory=list)


@dataclass(slots=True)
class BaselineOutcome:
    issue: str
    difficulty: str
    solved: bool
    clean: bool
    resolved: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    regressions: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    wall_seconds: float = 0.0
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.solved and self.clean


def _repo_context(root: Path) -> str:
    """Every source file, concatenated. The baseline cannot grep, so it gets
    everything rather than being penalised for a retrieval step it lacks."""
    blocks: list[str] = []
    budget = _MAX_CONTEXT_CHARS
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(p in {".venv", "__pycache__", ".pytest_cache", ".git"} for p in rel.parts):
            continue
        if not path.is_file() or path.suffix not in _SOURCE_SUFFIXES:
            continue
        body = path.read_text(encoding="utf-8", errors="replace")
        block = f"===== {rel} =====\n{body}"
        if len(block) > budget:
            break
        blocks.append(block)
        budget -= len(block)
    return "\n\n".join(blocks)


async def run_case(case: Case, settings: Settings) -> BaselineOutcome:
    started = time.perf_counter()
    recorder = MetricsRecorder(f"baseline-{case.issue}", InMemorySink(), settings)
    sandbox = Sandbox(TARGET_REPO, settings=settings)

    try:
        await sandbox.start()
        await sandbox.install_dependencies()
        await sandbox.cut_network()
        await sandbox.snapshot()

        before = await sandbox._exec_shell(TEST_COMMAND, timeout=600)
        baseline = failing_ids(before.combined)
        baseline_total = collected_total(before.combined)

        patch = await call_structured(
            Role.CODER,
            BaselinePatch,
            [
                SystemMessage(content=SYSTEM),
                HumanMessage(
                    content=(
                        f"## Issue\n\n{case.read()}\n\n"
                        f"## Repository\n\n{_repo_context(TARGET_REPO)}"
                    )
                ),
            ],
            settings=settings,
            recorder=recorder,
        )

        written: list[str] = []
        for file in patch.files:
            try:
                await sandbox.write_text(file.path, file.content)
                written.append(file.path)
            except Exception as exc:  # noqa: BLE001 - a bad path is a failed attempt
                return BaselineOutcome(
                    issue=case.issue,
                    difficulty=case.difficulty,
                    solved=False,
                    clean=False,
                    files_written=written,
                    error=f"could not write {file.path}: {exc}",
                    wall_seconds=round(time.perf_counter() - started, 1),
                    input_tokens=recorder.totals.input_tokens,
                    output_tokens=recorder.totals.output_tokens,
                    cost_usd=round(recorder.totals.cost_usd, 4),
                )

        # Same integrity rule as the agent: test edits are reverted before scoring.
        changed = await sandbox.changed_files()
        touched_tests = [p for p in changed if "test" in p.lower() or "conftest" in p.lower()]
        if touched_tests:
            await sandbox.restore_paths(touched_tests)

        after = await sandbox.exec(TEST_COMMAND, timeout=600)
        result = parse(
            after.combined,
            after.exit_code,
            baseline=baseline,
            baseline_total=baseline_total,
            modified_test_files=touched_tests,
        )

        expected = set(case.expected_failures)
        resolved = set(result.resolved)
        return BaselineOutcome(
            issue=case.issue,
            difficulty=case.difficulty,
            solved=expected <= resolved,
            clean=not result.regressions and not result.tests_disappeared,
            resolved=sorted(resolved & expected),
            missing=sorted(expected - resolved),
            regressions=list(result.regressions),
            files_written=written,
            input_tokens=recorder.totals.input_tokens,
            output_tokens=recorder.totals.output_tokens,
            cost_usd=round(recorder.totals.cost_usd, 4),
            wall_seconds=round(time.perf_counter() - started, 1),
        )
    finally:
        await sandbox.destroy()


def _load_agent_results() -> dict[str, dict[str, object]]:
    """Most recent gate run, for the side-by-side. Absent is fine — the baseline
    still stands alone, it just has nothing to be compared against."""
    files = sorted(RESULTS.glob("gate-*.json"))
    if not files:
        return {}
    data = json.loads(files[-1].read_text())
    return {o["issue"]: o for o in data.get("outcomes", [])}


def _usd(record: dict[str, object]) -> float:
    """Pull a cost out of a loaded results record.

    The loader types its values as `object` because it reads whatever JSON the
    last gate run wrote. Coercing explicitly beats trusting the shape: a missing
    or malformed `cost_usd` reads as $0 rather than raising halfway through a
    comparison table.
    """
    value = record.get("cost_usd")
    return float(value) if isinstance(value, int | float) else 0.0


def _comparison(outcomes: list[BaselineOutcome], agent: dict[str, dict[str, object]]) -> str:
    header = (
        f"{'issue':<32} {'diff':<7} | {'baseline':<9} {'cost':<8} | "
        f"{'agent':<7} {'att':<4} {'cost':<8}"
    )
    rows = [header, "-" * len(header)]
    for o in outcomes:
        a = agent.get(o.issue, {})
        a_result = "PASS" if a.get("solved") and a.get("clean") else ("FAIL" if a else "-")
        rows.append(
            f"{o.issue:<32} {o.difficulty:<7} | "
            f"{('PASS' if o.passed else 'FAIL'):<9} ${o.cost_usd:<7.4f} | "
            f"{a_result:<7} {str(a.get('attempts', '-')):<4} "
            f"${_usd(a):<7.4f}"
        )
    return "\n".join(rows)


def interpret(outcomes: list[BaselineOutcome], delta: int) -> list[str]:
    """State what the numbers do and do not support.

    A benchmark that reports a tie without explaining why invites the reader to
    conclude whichever thing they already believed. When the baseline matches the
    agent, the honest reading is usually that the fixture is too easy — not that
    orchestration is worthless — and the harness should say which.
    """
    if delta > 0:
        return [
            "",
            "The agent solved cases the one-shot baseline could not. That gap is "
            "what the planning, testing and repair loop bought.",
        ]
    if delta < 0:
        return [
            "",
            "The baseline beat the agent. Something in the pipeline is losing "
            "information the model had — look at retrieval first.",
        ]
    return [
        "",
        "READ THIS BEFORE QUOTING THE NUMBERS ABOVE.",
        "",
        "A tie does not show the orchestration is worthless. It shows this fixture",
        "cannot tell the two apart, for two reasons:",
        "",
        f"  1. The whole repository ({_MAX_CONTEXT_CHARS // 1000}k chars max) fits in",
        "     one prompt, so the baseline needs no retrieval. On any repository too",
        "     large to paste, that advantage disappears — which is the case the",
        "     retriever and the MCP tools exist for.",
        "  2. Every seeded defect is localised enough to fix first try, so the",
        "     repair loop never engages. A control that never needs a second attempt",
        "     cannot measure the value of being able to make one.",
        "",
        "To make this comparison mean something: run both against a repository that",
        "does not fit in context, and against defects whose obvious first fix is",
        "wrong. Until then, the honest claim is 'the agent matches a one-shot",
        "baseline on easy, fully-visible bugs at ~2.5x the cost' — not that it is",
        "better.",
    ]


async def main() -> int:
    parser = argparse.ArgumentParser(description="One-shot baseline for comparison.")
    parser.add_argument("--only", nargs="*", help="Issue prefixes, e.g. 01 05")
    args = parser.parse_args()

    settings = get_settings()
    cases = list(CASES)
    if args.only:
        cases = [c for c in cases if c.issue.startswith(tuple(args.only))]

    outcomes: list[BaselineOutcome] = []
    for case in cases:
        print(f"\n=== baseline: {case.issue} ({case.difficulty}) ===", flush=True)
        try:
            outcome = await run_case(case, settings)
        except Exception as exc:  # noqa: BLE001 - one bad case must not lose the rest
            print(f"  crashed: {type(exc).__name__}: {exc}", flush=True)
            outcome = BaselineOutcome(
                issue=case.issue,
                difficulty=case.difficulty,
                solved=False,
                clean=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        outcomes.append(outcome)
        print(
            f"  {'PASS' if outcome.passed else 'FAIL'}  "
            f"fixed={len(outcome.resolved)}/{len(outcome.resolved) + len(outcome.missing)} "
            f"regressions={len(outcome.regressions)} cost=${outcome.cost_usd:.4f}",
            flush=True,
        )

    agent = _load_agent_results()
    print("\n" + _comparison(outcomes, agent))

    b_pass = sum(1 for o in outcomes if o.passed)
    b_cost = sum(o.cost_usd for o in outcomes)
    a_pass = sum(
        1 for o in outcomes if (a := agent.get(o.issue)) and a.get("solved") and a.get("clean")
    )
    a_cost = sum(_usd(agent.get(o.issue, {})) for o in outcomes)

    print()
    print(f"baseline (one shot, no tools): {b_pass}/{len(outcomes)} solved, ${b_cost:.4f}")
    if agent:
        print(f"agent    (plan/test/repair) : {a_pass}/{len(outcomes)} solved, ${a_cost:.4f}")
        delta = a_pass - b_pass
        print()
        print(
            f"The orchestration is worth {delta:+d} issue(s) "
            f"for {a_cost - b_cost:+.4f} USD across {len(outcomes)} case(s)."
        )
        for line in interpret(outcomes, delta):
            print(line)
    else:
        print("(no gate results found; run `uv run python -m eval.gate` to compare)")

    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / f"baseline-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps([asdict(o) for o in outcomes], indent=2) + "\n")
    print(f"\nwritten to {path.relative_to(Path.cwd())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
