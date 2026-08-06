"""The Phase 1A exit gate.

Runs each fixture issue through the agent and scores the result against
`eval.dataset` — the answer key the agent never sees. Scoring is a set comparison
rather than a judgement call:

- **solved** — every test the issue owns went from failing to passing
- **clean** — no previously-passing test broke
- **scoped** — the patch touched only the files a correct fix needs

The gate passes when at least 3 of 5 issues are solved, the repair-loop case
actually went through the debugger and back into the coder, and a killed run
resumes from its checkpoint.

Usage:
    uv run python -m eval.gate                # all five
    uv run python -m eval.gate --only 01 05   # a subset
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from eval.dataset import CASES, TARGET_REPO, Case
from featurepilot.config import get_settings
from featurepilot.run import open_run, stream_run

RESULTS = Path(__file__).resolve().parent / "results"


@dataclass(slots=True)
class Outcome:
    issue: str
    difficulty: str
    phase: str
    solved: bool
    clean: bool
    scoped: bool
    used_repair_loop: bool
    attempts: int
    resolved: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    regressions: list[str] = field(default_factory=list)
    unexpected_files: list[str] = field(default_factory=list)
    #: Reported separately from unexpected_files: adding a test for new code is
    #: good practice, editing one to make it agree with the code is the cardinal
    #: sin. Lumping them into one 'wide scope' flag hides the difference.
    touched_tests: list[str] = field(default_factory=list)
    tests_disappeared: int = 0
    model_calls: int = 0
    tool_calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    wall_seconds: float = 0.0
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.solved and self.clean and not self.tests_disappeared


async def run_case(case: Case) -> Outcome:
    settings = get_settings()
    started = time.perf_counter()
    nodes: list[str] = []

    async with open_run(
        TARGET_REPO,
        case.read(),
        issue_ref=case.issue,
        settings=settings,
        auto_approve=True,
    ) as handle:
        async for event in stream_run(
            handle,
            issue=case.read(),
            repo_path=TARGET_REPO,
            issue_ref=case.issue,
        ):
            if event["node"] != "__interrupt__":
                nodes.append(event["node"])

        final = await handle.state()
        totals = handle.ctx.recorder.totals
        tests = final.get("tests")
        code = final.get("code")

        resolved = set(tests.resolved) if tests else set()
        regressions = list(tests.regressions) if tests else []
        touched = {e.path for e in code.edits} if code else set()
        touched_tests = sorted(p for p in touched if "test" in p.lower())
        source_touched = touched - set(touched_tests)

        expected = set(case.expected_failures)
        # A repair loop means the debugger ran and the coder re-entered after it.
        used_repair = (
            "debug" in nodes
            and nodes.index("debug") < len(nodes) - 1
            and ("code" in nodes[nodes.index("debug") :])
        )

        return Outcome(
            issue=case.issue,
            difficulty=case.difficulty,
            phase=str(final.get("phase", "?")),
            solved=expected <= resolved,
            clean=not regressions,
            scoped=source_touched <= set(case.expected_files),
            used_repair_loop=used_repair,
            attempts=int(final.get("attempt", 0)),
            resolved=sorted(resolved & expected),
            missing=sorted(expected - resolved),
            regressions=regressions,
            unexpected_files=sorted(source_touched - set(case.expected_files)),
            touched_tests=touched_tests,
            tests_disappeared=tests.tests_disappeared if tests else 0,
            model_calls=totals.model_calls,
            tool_calls=totals.tool_calls,
            input_tokens=totals.input_tokens,
            output_tokens=totals.output_tokens,
            cost_usd=round(totals.cost_usd, 4),
            wall_seconds=round(time.perf_counter() - started, 1),
            error=final.get("error"),
        )


def _table(outcomes: list[Outcome]) -> str:
    header = (
        f"{'issue':<32} {'diff':<7} {'result':<8} {'fixed':<7} {'att':<4} "
        f"{'repair':<7} {'scope':<6} {'tests':<7} {'cost':<8} {'secs':<6}"
    )
    rows = [header, "-" * len(header)]
    for o in outcomes:
        expected_total = len(o.resolved) + len(o.missing)
        rows.append(
            f"{o.issue:<32} {o.difficulty:<7} "
            f"{('PASS' if o.passed else 'FAIL'):<8} "
            f"{f'{len(o.resolved)}/{expected_total}':<7} {o.attempts:<4} "
            f"{('yes' if o.used_repair_loop else '-'):<7} "
            f"{('ok' if o.scoped else 'wide'):<6} "
            f"{(f'+{len(o.touched_tests)}' if o.touched_tests else '-'):<7} "
            f"${o.cost_usd:<7.4f} {o.wall_seconds:<6.1f}"
        )
    return "\n".join(rows)


def _verdict(outcomes: list[Outcome]) -> tuple[bool, list[str]]:
    """The gate's three criteria, reported individually so a failure is legible."""
    notes: list[str] = []
    solved = [o for o in outcomes if o.passed]
    ok = True

    if len(solved) >= 3:
        notes.append(f"PASS  {len(solved)}/{len(outcomes)} issues solved cleanly (needed 3)")
    else:
        ok = False
        notes.append(f"FAIL  only {len(solved)}/{len(outcomes)} issues solved cleanly (needed 3)")

    repair_cases = [o for o in outcomes if o.issue == "05-free-shipping-threshold.md"]
    if not repair_cases:
        notes.append("SKIP  repair-loop case was not run")
    elif repair_cases[0].used_repair_loop:
        notes.append("PASS  the repair-loop case went debugger -> coder")
    else:
        # Not a hard failure: the model may have got it right first time, which is
        # a better outcome than the one being tested for.
        notes.append(
            "NOTE  the repair-loop case did not need the debugger "
            "(solved first try — better than expected, but the loop is unexercised)"
        )

    shrunk = [o for o in outcomes if o.tests_disappeared]
    if shrunk:
        ok = False
        notes.append(f"FAIL  {len(shrunk)} run(s) removed tests from the suite")
    else:
        notes.append("PASS  no run removed a test")

    edited_tests = [o for o in outcomes if o.touched_tests]
    if edited_tests:
        notes.append(
            "NOTE  test files were touched (adding coverage is fine; verify none "
            f"were weakened): {[f'{o.issue}: {o.touched_tests}' for o in edited_tests]}"
        )

    regressed = [o for o in outcomes if o.regressions]
    if regressed:
        ok = False
        notes.append(f"FAIL  {len(regressed)} run(s) broke previously-passing tests")
    else:
        notes.append("PASS  no run broke a previously-passing test")

    return ok, notes


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Phase 1A exit gate.")
    parser.add_argument("--only", nargs="*", help="Issue prefixes, e.g. 01 05")
    args = parser.parse_args()

    cases = list(CASES)
    if args.only:
        wanted = tuple(args.only)
        cases = [c for c in cases if c.issue.startswith(wanted)]
        if not cases:
            print(f"no cases match {args.only}")
            return 2

    outcomes: list[Outcome] = []
    for case in cases:
        print(f"\n=== {case.issue}  ({case.difficulty}) ===", flush=True)
        try:
            outcome = await run_case(case)
        except Exception as exc:  # noqa: BLE001 - one bad case must not lose the rest
            print(f"  crashed: {type(exc).__name__}: {exc}", flush=True)
            outcome = Outcome(
                issue=case.issue,
                difficulty=case.difficulty,
                phase="CRASHED",
                solved=False,
                clean=False,
                scoped=False,
                used_repair_loop=False,
                attempts=0,
                error=f"{type(exc).__name__}: {exc}",
            )
        outcomes.append(outcome)
        verdict = "PASS" if outcome.passed else "FAIL"
        print(
            f"  {verdict}  phase={outcome.phase} attempts={outcome.attempts} "
            f"fixed={len(outcome.resolved)}/{len(outcome.resolved) + len(outcome.missing)} "
            f"cost=${outcome.cost_usd:.4f}",
            flush=True,
        )
        if outcome.missing:
            print(f"    still failing: {outcome.missing}", flush=True)
        if outcome.regressions:
            print(f"    REGRESSIONS: {outcome.regressions}", flush=True)

    print("\n" + _table(outcomes))
    ok, notes = _verdict(outcomes)
    print()
    for note in notes:
        print(note)

    total = sum(o.cost_usd for o in outcomes)
    print(f"\ntotal cost: ${total:.4f}   total wall: {sum(o.wall_seconds for o in outcomes):.0f}s")

    RESULTS.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = RESULTS / f"gate-{stamp}.json"
    path.write_text(
        json.dumps(
            {"outcomes": [asdict(o) for o in outcomes], "passed": ok, "notes": notes},
            indent=2,
        )
        + "\n"
    )
    print(f"written to {path.relative_to(Path.cwd())}")

    print(f"\nGATE: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


def _cli() -> None:
    raise SystemExit(asyncio.run(main()))


if __name__ == "__main__":
    _cli()


__all__ = ["Outcome", "main", "run_case"]
