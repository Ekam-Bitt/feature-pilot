"""Which part of the pipeline actually earns its cost?

The fixture comparison said the full agent ties a one-shot baseline at 2.5x the
cost. That result is unactionable: "the agent" was one indivisible thing, so a tie
says nothing about whether planning, retrieval, testing or repair is the part
pulling its weight.

This runs the same cases through progressively larger configurations:

    code-only            coder alone, issue text only
    +retrieve            search the repository first
    +plan                a plan before coding
    +test                run the suite (verdict only — no repair)
    +debug               diagnose and re-code on failure
    +review              the full pipeline

Each rung adds exactly one stage, so the delta between two rows is that stage's
contribution — in solve rate, cost, and latency.

Two rungs deserve their expectations stated in advance, because a benchmark you
can't be wrong about isn't measuring anything:

- `+test` should score the *same* as `+plan`. Running tests without repair tells
  you the patch failed but cannot act on it. If it scores higher, something is
  leaking test feedback into the patch and the ladder is not isolating stages.
- `+debug` is where the repair loop can first pay off. If it does not beat
  `+test`, the loop is not earning its cost on these cases.

Usage:
    uv run python -m eval.ablation                      # click cases (the real test)
    uv run python -m eval.ablation --suite fixture      # the toy fixture
    uv run python -m eval.ablation --rungs code-only full
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from eval.dataset import CASES as FIXTURE_CASES
from eval.dataset import TARGET_REPO
from eval.oss import OSSCase
from featurepilot.config import Settings, get_settings
from featurepilot.graph.router import Stages
from featurepilot.run import open_run, stream_run

RESULTS = Path(__file__).resolve().parent / "results"
OSS_CASES_FILE = Path(__file__).resolve().parent / "oss_cases.json"

#: The ladder. Ordered, each rung adding exactly one stage to the one before, so
#: a difference between adjacent rows is attributable to a single change.
RUNGS: tuple[tuple[str, Stages], ...] = (
    ("code-only", Stages(retrieve=False, plan=False, test=False, debug=False, review=False)),
    ("+retrieve", Stages(plan=False, test=False, debug=False, review=False)),
    ("+plan", Stages(test=False, debug=False, review=False)),
    ("+test", Stages(debug=False, review=False)),
    ("+debug", Stages(review=False)),
    ("full", Stages()),
)


@dataclass(slots=True)
class Trial:
    """One (rung, case) result."""

    rung: str
    case: str
    solved: bool
    clean: bool
    attempts: int
    resolved: int
    expected: int
    regressions: int
    tests_removed: int
    cost_usd: float
    wall_seconds: float
    model_calls: int
    tool_calls: int
    #: Tokens, not dollars, are the scaling metric: a run that exceeds the
    #: context budget fails outright regardless of what it would have cost.
    input_tokens: int = 0
    output_tokens: int = 0
    hit_budget: bool = False
    #: Spend per role, so a rung's extra cost is attributable to the stage it added.
    per_role_cost: dict[str, float] = field(default_factory=dict)
    tool_calls_by_node: dict[str, int] = field(default_factory=dict)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.solved and self.clean and not self.tests_removed


@dataclass(slots=True)
class Target:
    """A case set, normalised so the fixture and the OSS set run identically."""

    name: str
    case_id: str
    repo: Path
    issue: str
    expected: frozenset[str]
    baseline_failures: frozenset[str] = frozenset()
    baseline_total: int = 0
    notes: str = ""


def _fixture_targets() -> list[Target]:
    return [
        Target(
            name="fixture",
            case_id=c.issue,
            repo=TARGET_REPO,
            issue=c.read(),
            expected=frozenset(c.expected_failures),
        )
        for c in FIXTURE_CASES
    ]


def _oss_targets() -> list[Target]:
    if not OSS_CASES_FILE.is_file():
        raise SystemExit(
            f"no {OSS_CASES_FILE.name}. Build it first:\n"
            "  uv run python -m eval.oss_build --limit 200 --want 6"
        )
    payload = json.loads(OSS_CASES_FILE.read_text())
    targets: list[Target] = []
    for raw in payload["cases"]:
        case = OSSCase(
            repo=payload["repo"],
            sha=raw["sha"],
            title=raw["title"],
            issue=raw["issue"],
            issue_source=raw["issue_source"],
            source_files=frozenset(raw["source_files"]),
        )
        if not case.case_dir.is_dir():
            raise SystemExit(
                f"case {case.sha[:9]} is not staged at {case.case_dir}.\n"
                "Rebuild with: uv run python -m eval.oss_build --limit 200 --want 6"
            )
        targets.append(
            Target(
                name="click",
                case_id=case.sha[:9],
                repo=case.case_dir,
                issue=case.issue,
                expected=frozenset(raw["fail_to_pass"]),
                baseline_failures=frozenset(raw["baseline_failures"]),
                baseline_total=raw["collected_total"],
                notes=("names-file " if raw.get("names_source_path") else "") + raw["issue_source"],
            )
        )
    return targets


async def run_trial(rung: str, stages: Stages, target: Target, settings: Settings) -> Trial:
    started = time.perf_counter()
    blank = Trial(
        rung=rung,
        case=target.case_id,
        solved=False,
        clean=False,
        attempts=0,
        resolved=0,
        expected=len(target.expected),
        regressions=0,
        tests_removed=0,
        cost_usd=0.0,
        wall_seconds=0.0,
        model_calls=0,
        tool_calls=0,
    )
    try:
        async with open_run(
            target.repo,
            target.issue,
            issue_ref=target.case_id,
            settings=settings,
            auto_approve=True,
            stages=stages,
        ) as handle:
            async for _ in stream_run(
                handle, issue=target.issue, repo_path=target.repo, issue_ref=target.case_id
            ):
                pass

            final = await handle.state()
            totals = handle.ctx.recorder.totals
            tests = final.get("tests")
            resolved = set(tests.resolved) if tests else set()

            return Trial(
                rung=rung,
                case=target.case_id,
                solved=target.expected <= resolved,
                clean=not (tests.regressions if tests else []),
                attempts=int(final.get("attempt", 0)),
                resolved=len(resolved & target.expected),
                expected=len(target.expected),
                regressions=len(tests.regressions) if tests else 0,
                tests_removed=tests.tests_disappeared if tests else 0,
                cost_usd=round(totals.cost_usd, 4),
                wall_seconds=round(time.perf_counter() - started, 1),
                model_calls=totals.model_calls,
                tool_calls=totals.tool_calls,
                input_tokens=totals.input_tokens,
                output_tokens=totals.output_tokens,
                hit_budget="ceiling" in (final.get("error") or ""),
                per_role_cost={r: round(s.cost_usd, 4) for r, s in totals.per_role.items()},
                tool_calls_by_node=dict(totals.tool_calls_by_node),
                error=final.get("error"),
            )
    except Exception as exc:  # noqa: BLE001 - one bad trial must not lose the sweep
        blank.error = f"{type(exc).__name__}: {exc}"
        blank.wall_seconds = round(time.perf_counter() - started, 1)
        return blank


def _table(trials: list[Trial], rungs: list[str], n_cases: int) -> str:
    header = (
        f"{'configuration':<14}{'solved':>8}{'rate':>7}{'tokens':>10}"
        f"{'cost':>9}{'$/solve':>9}{'secs':>7}{'calls':>7}{'budget':>8}"
    )
    rows = [header, "-" * len(header)]
    for rung in rungs:
        group = [t for t in trials if t.rung == rung]
        if not group:
            continue
        solved = sum(1 for t in group if t.passed)
        cost = sum(t.cost_usd for t in group)
        per_solve = f"${cost / solved:.4f}" if solved else "—"
        tokens = sum(t.input_tokens + t.output_tokens for t in group)
        blown = sum(1 for t in group if t.hit_budget)
        rows.append(
            f"{rung:<14}{f'{solved}/{len(group)}':>8}{solved / len(group):>7.0%}"
            f"{tokens:>10,}{f'${cost:.4f}':>9}{per_solve:>9}"
            f"{sum(t.wall_seconds for t in group):>7.0f}"
            f"{sum(t.model_calls for t in group):>7}"
            f"{(f'{blown} blown' if blown else 'ok'):>8}"
        )
    return "\n".join(rows)


def _deltas(trials: list[Trial], rungs: list[str]) -> list[str]:
    """What each added stage bought, rung over rung."""
    lines = ["", "What each stage contributed (vs the rung above):"]
    previous: tuple[str, int, float] | None = None
    for rung in rungs:
        group = [t for t in trials if t.rung == rung]
        if not group:
            continue
        solved = sum(1 for t in group if t.passed)
        cost = sum(t.cost_usd for t in group)
        if previous is None:
            lines.append(f"  {rung:<14} baseline: {solved}/{len(group)} at ${cost:.4f}")
        else:
            _, prev_solved, prev_cost = previous
            d_solved = solved - prev_solved
            d_cost = cost - prev_cost
            verdict = (
                f"{d_solved:+d} solved for {d_cost:+.4f} USD"
                if d_solved
                else f"no change in solve rate, {d_cost:+.4f} USD"
            )
            lines.append(f"  {rung:<14} {verdict}")
        previous = (rung, solved, cost)
    return lines


def _sanity(trials: list[Trial]) -> list[str]:
    """Check the predictions made before running. A ladder that cannot be wrong
    is not measuring anything."""
    notes: list[str] = []

    def solved(rung: str) -> int | None:
        group = [t for t in trials if t.rung == rung]
        return sum(1 for t in group if t.passed) if group else None

    plan, test, debug = solved("+plan"), solved("+test"), solved("+debug")

    if plan is not None and test is not None:
        if test == plan:
            notes.append(
                "  OK    +test matched +plan, as predicted — a verdict with no "
                "repair cannot change the outcome."
            )
        else:
            notes.append(
                f"  CHECK +test scored {test} vs +plan {plan}. Running tests without "
                "repair should not change the patch; test feedback may be leaking "
                "into the coder, which would mean the rungs are not isolated."
            )
    if test is not None and debug is not None:
        if debug > test:
            notes.append(f"  OK    the repair loop earned {debug - test} extra solve(s).")
        else:
            notes.append(
                "  NOTE  the repair loop earned nothing on these cases — either the "
                "first patch is usually right, or the debugger is not helping."
            )
    return notes


async def main() -> int:
    parser = argparse.ArgumentParser(description="Ablate the pipeline stage by stage.")
    parser.add_argument("--suite", choices=("click", "fixture"), default="click")
    parser.add_argument("--rungs", nargs="*", help="Subset of rung names.")
    parser.add_argument("--cases", nargs="*", help="Subset of case ids.")
    args = parser.parse_args()

    settings = get_settings()
    targets = _oss_targets() if args.suite == "click" else _fixture_targets()
    if args.cases:
        targets = [t for t in targets if any(t.case_id.startswith(c) for c in args.cases)]

    ladder = [(n, s) for n, s in RUNGS if not args.rungs or n in args.rungs]
    if not ladder or not targets:
        print("nothing to run")
        return 2

    print(
        f"suite={args.suite}  cases={len(targets)}  rungs={len(ladder)}  "
        f"=> {len(targets) * len(ladder)} runs\n"
    )

    trials: list[Trial] = []
    for rung, stages in ladder:
        print(f"=== {rung} ({stages.label}) ===", flush=True)
        for target in targets:
            trial = await run_trial(rung, stages, target, settings)
            trials.append(trial)
            mark = "PASS" if trial.passed else "fail"
            extra = f"  {trial.error[:60]}" if trial.error else ""
            print(
                f"  {mark}  {target.case_id:<12} "
                f"{trial.resolved}/{trial.expected} att={trial.attempts} "
                f"${trial.cost_usd:.4f} {trial.wall_seconds:.0f}s{extra}",
                flush=True,
            )

    rung_names = [n for n, _ in ladder]
    print("\n" + _table(trials, rung_names, len(targets)))
    for line in _deltas(trials, rung_names):
        print(line)
    if checks := _sanity(trials):
        print("\nPredictions made before running:")
        for line in checks:
            print(line)

    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / f"ablation-{args.suite}-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(
        json.dumps(
            {
                "suite": args.suite,
                "cases": [t.case_id for t in targets],
                "trials": [asdict(t) for t in trials],
            },
            indent=2,
        )
        + "\n"
    )
    print(f"\nwritten to {path.relative_to(Path.cwd())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
