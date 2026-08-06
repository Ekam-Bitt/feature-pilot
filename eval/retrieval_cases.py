"""Retrieval-only cases, for a second repository.

The end-to-end case set (`eval/oss_build.py`) validates every candidate in a
container — install, run the suite with the bug, restore the fix, run again — which
costs two sandbox runs per candidate. That guarantee is what makes a *solve rate*
trustworthy.

The retrieval benchmark never runs tests. It asks only "does the retriever rank
the file the real fix touched", and git states which files those were. So a
retrieval case needs no container, which makes a second repository cheap enough to
be worth having.

Why a second repository at all: every click case has its answer under `src/click/`,
so a ranker that simply preferred `src/**` would score perfectly while having
learned nothing. `rich` has a flat layout with no `src/` directory, so a
layout-shaped shortcut scores zero there. If the content-based ranker holds on
both, the improvement is a property of the ranker rather than of click.

Usage:
    uv run python -m eval.retrieval_cases --repo Textualize/rich \\
        --clone /tmp/oss-eval/rich --want 6
"""

from __future__ import annotations

import argparse
import json
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

from eval.oss import WORKTREES, candidate_commits, is_source_file, rank_candidates

CASES_FILE = Path(__file__).resolve().parent / "retrieval_cases.json"


@dataclass(slots=True)
class RetrievalCase:
    """An issue and the files its real fix changed. No solvability guarantee."""

    repo: str
    sha: str
    title: str
    issue: str
    issue_source: str
    source_files: list[str]
    #: Recorded, not filtered: a report that names the file removes the search
    #: rather than the diagnosis, so those cases must be visible in results.
    names_source_path: bool = False

    @property
    def case_dir(self) -> Path:
        return WORKTREES / f"{self.repo.split('/')[-1]}-{self.sha[:9]}"


def _git(clone: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603
        ["git", "-C", str(clone), *args], capture_output=True, text=True, check=False
    ).stdout


def _source_files(clone: Path, sha: str, repo: str) -> list[str]:
    """Python files the commit changed that are neither tests nor examples.

    Layout-agnostic on purpose: `click` keeps its package under `src/`, `rich`
    does not, and the point of a second repository is to stop layout assumptions
    creeping in. A file is excluded because of what it *is* — a test, an example,
    documentation — not where it sits.
    """
    changed = [f for f in _git(clone, "show", "--name-only", "--format=", sha).splitlines() if f]
    return sorted(f for f in changed if is_source_file(f))


def build(clone: Path, repo: str, sha: str, subject: str, prompt: tuple[str, str]) -> RetrievalCase:
    """Stage the repository with the bug present, and record the ground truth."""
    sources = _source_files(clone, sha, repo)
    case = RetrievalCase(
        repo=repo,
        sha=sha,
        title=subject,
        issue=prompt[0],
        issue_source=prompt[1],
        source_files=sources,
    )
    case.names_source_path = any(path in case.issue for path in sources)

    target = case.case_dir
    if target.exists():
        subprocess.run(  # noqa: S603
            ["git", "-C", str(clone), "worktree", "remove", "--force", str(target)],
            capture_output=True,
            check=False,
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(  # noqa: S603
        ["git", "-C", str(clone), "worktree", "add", "--detach", "--force", str(target), sha],
        capture_output=True,
        check=False,
    )
    # Revert only the source half, so the tests that describe the bug remain and
    # the code under discussion is in its broken state — the state a retriever
    # would actually be searching.
    if sources:
        subprocess.run(  # noqa: S603
            ["git", "-C", str(target), "checkout", f"{sha}~1", "--", *sources],
            capture_output=True,
            check=False,
        )
    return case


def main() -> int:
    parser = argparse.ArgumentParser(description="Build retrieval-only cases.")
    parser.add_argument("--repo", required=True, help="e.g. Textualize/rich")
    parser.add_argument("--clone", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=300)
    parser.add_argument("--want", type=int, default=6)
    args = parser.parse_args()

    if not args.clone.is_dir():
        print(f"no clone at {args.clone}")
        return 2

    ranked = rank_candidates(candidate_commits(args.clone, args.limit), args.repo, args.clone)
    print(f"{len(ranked)} candidate commit(s), best prompt first\n")

    cases: list[RetrievalCase] = []
    for sha, subject, prompt, source in ranked:
        if len(cases) >= args.want:
            break
        case = build(args.clone, args.repo, sha, subject, (prompt, source))
        if not case.source_files:
            print(f"  drop {sha[:9]}  no non-test source files changed")
            continue
        if not case.case_dir.is_dir():
            print(f"  drop {sha[:9]}  could not stage a worktree")
            continue
        cases.append(case)
        flag = "  [names the file]" if case.names_source_path else ""
        print(f"  keep {sha[:9]}  [{source}]{flag}  {case.source_files}")

    if not cases:
        print("\nno usable cases")
        return 1

    existing: dict[str, list[dict[str, object]]] = {}
    if CASES_FILE.is_file():
        existing = json.loads(CASES_FILE.read_text())
    existing[args.repo] = [asdict(c) for c in cases]
    CASES_FILE.write_text(json.dumps(existing, indent=2) + "\n")
    print(f"\n{len(cases)} case(s) written to {CASES_FILE.name} under {args.repo!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
