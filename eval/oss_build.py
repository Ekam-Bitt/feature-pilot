"""Validate reconstructed cases, and write the ones that hold up.

A reconstruction is only a case if it is *verified solvable*: the suite must fail
with the bug present and pass once the real fix is restored. Skipping that check
would let an unfixable case into the set, and every future failure on it would be
blamed on the agent.

Each candidate costs two sandbox runs (bug present, fix restored), so validation
is the expensive step — which is exactly why it belongs here and not in the gate.

Usage:
    uv run python -m eval.oss_build --limit 12 --want 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from dataclasses import asdict
from pathlib import Path

from eval.oss import (
    DEFAULT_CLONE,
    DEFAULT_REPO,
    MIN_COLLECTED,
    OSSCase,
    build_case,
    candidate_commits,
    cleanup,
    rank_candidates,
)
from featurepilot.config import get_settings
from featurepilot.graph.nodes.tester import collected_total, failing_ids
from featurepilot.sandbox.runner import Sandbox

CASES_FILE = Path("eval/oss_cases.json")

#: click installs from its own extras; the fallback covers a repo without them.
INSTALL = "pip install -e '.[dev]' || pip install -e . && pip install pytest"


async def _run_suite(box: Sandbox, command: str) -> tuple[set[str], int, str]:
    """Run the suite and report failures, collected count, and any problem.

    The third element distinguishes "the suite ran and nothing failed" from "the
    suite never produced a summary". Conflating them is how a truncated run gets
    recorded as a clean one — click's pager tests killed the process at ~92% with
    no summary, and every affected case was silently dropped as unfixable.
    """
    result = await box._exec_shell(command, timeout=900)
    output = result.combined
    total = collected_total(output)

    if "passed" not in output and "failed" not in output and "error" not in output.lower():
        return set(), total, "the suite produced no parseable summary (crashed or truncated)"
    if total < MIN_COLLECTED:
        return set(), total, f"only {total} test(s) collected; the suite did not really run"
    return failing_ids(output), total, ""


async def validate(case: OSSCase, clone: Path) -> tuple[bool, str]:
    """Measure FAIL_TO_PASS, and confirm the real fix actually resolves it."""
    settings = get_settings()
    box = Sandbox(case.case_dir, settings=settings)
    try:
        await box.start()
        install = await box.install_dependencies(INSTALL)
        if not install.ok and "Successfully installed" not in install.combined:
            return False, f"install failed: {install.combined[-160:]}"
        await box.cut_network()
        await box.snapshot()

        with_bug, total, problem = await _run_suite(box, case.test_command)
        if problem:
            return False, problem
        if not with_bug:
            # Nothing fails, so there is no signal distinguishing a fix from a
            # no-op. Usually means the test did not actually cover the change.
            return False, "no failing tests with the bug present"

        # Restore the real fix and re-run. Whatever flips is genuinely caused by
        # this commit; anything still failing is unrelated background noise.
        for path in sorted(case.source_files):
            written = await _restore_from_git(box, clone, case.sha, path)
            if not written:
                return False, f"could not restore {path} from the fixing commit"
        with_fix, _, problem = await _run_suite(box, case.test_command)
        if problem:
            return False, f"after restoring the fix: {problem}"

        flipped = with_bug - with_fix
        if not flipped:
            return False, "the real fix did not change any test outcome"
        broke = with_fix - with_bug
        if broke:
            return False, f"restoring the fix broke {len(broke)} other test(s)"

        case.fail_to_pass = frozenset(flipped)
        case.collected_total = total
        # Failures unrelated to this commit: present both before and after.
        case.baseline_failures = frozenset(with_bug & with_fix)
        return True, ""
    finally:
        await box.destroy()


async def _restore_from_git(box: Sandbox, clone: Path, sha: str, path: str) -> bool:
    """Copy one file's post-fix contents into the sandbox.

    Read from the host clone rather than running git inside the container: the
    sandbox deliberately has no `.git` and no network, and giving it either to
    support validation would weaken the isolation the whole design rests on.
    """
    import subprocess

    def _show() -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603
            ["git", "-C", str(clone), "show", f"{sha}:{path}"],
            capture_output=True,
            text=True,
            check=False,
        )

    blob = await asyncio.to_thread(_show)
    if blob.returncode != 0:
        return False
    await box.write_text(path, blob.stdout)
    return True


async def main() -> int:
    parser = argparse.ArgumentParser(description="Build ground-truth OSS cases.")
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--clone", type=Path, default=DEFAULT_CLONE)
    parser.add_argument("--limit", type=int, default=40, help="Commits to consider.")
    parser.add_argument("--want", type=int, default=5, help="Stop after this many good cases.")
    args = parser.parse_args()

    if not args.clone.is_dir():
        print(f"no clone at {args.clone}. git clone https://github.com/{args.repo} there first.")
        return 2

    raw = candidate_commits(args.clone, args.limit)
    candidates = rank_candidates(raw, args.repo, args.clone)
    print(f"{len(candidates)} candidate commit(s) from the last {args.limit}, best prompt first\n")

    good: list[OSSCase] = []
    rejected: list[tuple[str, str]] = []

    for sha, subject, prompt, source in candidates:
        if len(good) >= args.want:
            break
        print(f"--- {sha[:9]}  [{source}]  {subject[:56]}", flush=True)
        case = build_case(args.clone, sha, subject, repo=args.repo, prompt=(prompt, source))
        started = time.perf_counter()
        try:
            ok, reason = await validate(case, args.clone)
        except Exception as exc:  # noqa: BLE001 - one bad candidate must not stop the sweep
            ok, reason = False, f"{type(exc).__name__}: {exc}"
        elapsed = time.perf_counter() - started

        if ok:
            good.append(case)
            located = "  [issue names the file]" if case.names_source_path else ""
            print(
                f"    KEEP  fail_to_pass={len(case.fail_to_pass)} "
                f"of {case.collected_total} collected  "
                f"issue={case.issue_source}{located}  ({elapsed:.0f}s)",
                flush=True,
            )
        else:
            rejected.append((sha[:9], reason))
            print(f"    drop  {reason[:90]}  ({elapsed:.0f}s)", flush=True)

    print(f"\n{len(good)} usable case(s), {len(rejected)} rejected")
    if good:
        located = [c for c in good if c.names_source_path]
        if located:
            print(
                f"\nNOTE: {len(located)} case(s) name a file the fix must change "
                f"({', '.join(c.sha[:9] for c in located)}). Real reports do link to "
                "code, but those cases test diagnosis without testing retrieval."
            )
        leaky = [c for c in good if "may leak" in c.issue_source]
        if leaky:
            print(
                f"\nNOTE: {len(leaky)} case(s) use text that may describe the fix "
                f"({', '.join(c.sha[:9] for c in leaky)}). Their scores are worth less."
            )
        await asyncio.to_thread(CASES_FILE.parent.mkdir, exist_ok=True)
        await asyncio.to_thread(
            CASES_FILE.write_text,
            json.dumps(
                {
                    "repo": args.repo,
                    "built_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                    "cases": [
                        {
                            **{
                                k: (sorted(v) if isinstance(v, frozenset) else v)
                                for k, v in asdict(c).items()
                            },
                            "names_source_path": c.names_source_path,
                        }
                        for c in good
                    ],
                },
                indent=2,
            )
            + "\n",
        )
        print(f"written to {CASES_FILE}")
    else:
        cleanup(args.clone)
    return 0 if good else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
