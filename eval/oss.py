"""Ground-truth cases built from a real repository's history.

The fixture repo proved the pipeline works but could not prove it is *useful*:
20 files fit in one prompt, so a one-shot baseline tied the agent. A real
repository removes that — `click` is 7.7x the baseline's context budget, so no
control can simply read everything.

Construction follows SWE-bench: take a commit that fixed a bug, revert **only its
source changes**, and keep its tests. The suite then fails exactly where the bug
was, which makes FAIL_TO_PASS *known* rather than guessed. Using real open issues
instead would mean scoring against a success condition nobody can verify — most
open issues are feature requests or discussions.

Two fairness rules, both easy to get wrong:

1. **The issue text must not contain the fix.** A pull request body often
   explains the change ("switch to LEFT JOIN"), which turns diagnosis into
   transcription. Linked bug reports describe symptoms, so they are preferred and
   PR bodies are used only as a last resort, flagged when they are.
2. **Only source is reverted.** Reverting the tests too would leave nothing
   failing and no way to tell whether a patch worked.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

#: Where reconstructed repositories are staged. Gitignored.
#:
#: Absolute, and anchored to the project rather than the CWD. `git worktree
#: add` runs with `-C <clone>`, so a relative target resolves against the
#: clone — the worktrees land inside /tmp/oss-eval/click and every case then
#: reports 'target repo not found'.
WORKTREES = Path(__file__).resolve().parents[1] / ".fp" / "oss"

#: The upstream we build cases from. `click` was chosen on evidence: 7.7x the
#: context cap, 1964 tests that run green offline in ~3s, no native build deps.
DEFAULT_REPO = "pallets/click"
DEFAULT_CLONE = Path("/tmp/oss-eval/click")

#: Extra pytest arguments per target repository.
#:
#: click's pager tests spawn a subprocess that kills the run: the suite dies at
#: ~92% with no summary line at all, so every case looked like "no failing tests"
#: when the truth was "the suite never finished". Deselecting them yields 1474
#: collected instead of 0.
#:
#: Per-target rather than global on purpose — this is one repository's quirk, and
#: baking `-k 'not pager'` into the shared TEST_COMMAND would silently skip tests
#: in every other repo that happens to use the word.
EXTRA_PYTEST_ARGS: dict[str, str] = {
    "pallets/click": "-k 'not pager'",
}

#: Below this, the suite did not really run. click collects ~1700; a case
#: reporting 1 or 2 is a broken reconstruction that measures nothing, and
#: accepting it would put a case in the set that no patch can affect.
MIN_COLLECTED = 200

#: A commit is a candidate when it changes both source and tests and stays small.
#: Large commits are usually refactors or releases, where "the bug" is not a
#: single identifiable thing an agent could be asked to fix.
MAX_FILES_IN_COMMIT = 6

_PR_NUMBER = re.compile(r"\(#(\d+)\)\s*$")
_ISSUE_REF = re.compile(r"(?:closes|fixes|resolves)\s+#(\d+)", re.IGNORECASE)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )
    return result.stdout


def _gh_json(*args: str) -> dict[str, object]:
    result = subprocess.run(  # noqa: S603
        ["gh", *args], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return {}
    try:
        parsed = json.loads(result.stdout)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


@dataclass(slots=True)
class OSSCase:
    """One reconstructed bug, with its ground truth."""

    repo: str
    sha: str
    title: str
    #: The prompt the agent sees. Never contains the fix.
    issue: str
    #: Where the issue text came from, so a leaky source is visible in results
    #: rather than quietly inflating the score.
    issue_source: str
    #: Source files the real fix touched. Used to score scope discipline.
    source_files: frozenset[str] = frozenset()
    #: Test files the fixing commit added or changed. Never reverted.
    test_files: frozenset[str] = frozenset()
    #: Filled by `validate()`: tests that fail with the bug and pass with the fix.
    fail_to_pass: frozenset[str] = frozenset()
    collected_total: int = 0
    baseline_failures: frozenset[str] = frozenset()
    notes: str = ""

    @property
    def case_dir(self) -> Path:
        return WORKTREES / f"{self.sha[:9]}"

    @property
    def test_command(self) -> str:
        """The suite command for this target, including any per-repo args."""
        from featurepilot.graph.nodes.tester import TEST_COMMAND

        extra = EXTRA_PYTEST_ARGS.get(self.repo, "")
        return f"{TEST_COMMAND} {extra}".strip()

    @property
    def names_source_path(self) -> bool:
        """Whether the issue text points at a file the fix has to change.

        Real bug reports link to code constantly — issue #2877 embeds a GitHub
        permalink to `src/click/types.py#L929`. That is not leaking the *fix*
        (what to change is still unknown), but it does remove the *search*, which
        is the whole reason for moving off a fixture that fits in one prompt.

        Recorded rather than rejected. Excluding such issues would bias the set
        toward unrealistically vague reports; segmenting results by this flag
        answers "does retrieval matter" honestly instead of assuming it away.
        """
        return any(path in self.issue for path in self.source_files)

    @property
    def usable(self) -> bool:
        """A case with no FAIL_TO_PASS teaches nothing: there is no signal that
        distinguishes a correct patch from no patch at all."""
        return bool(self.fail_to_pass)


#: An issue shorter than this cannot be planned against — "Strip all ANSI
#: sequences" is 24 characters and states a change, not a symptom. Cases below
#: the bar are still usable but are tried last, because a thin prompt measures
#: guessing rather than diagnosis.
MIN_USEFUL_ISSUE_CHARS = 300


#: Directory names that never contain the code a fix changes.
_NON_SOURCE_DIRS = frozenset({"tests", "test", "examples", "docs", "doc", "benchmarks"})


def is_test_file(path: str) -> bool:
    """Whether a path is a test, decided from its name rather than its location.

    Layout-agnostic on purpose. An earlier version required `src/**` and `tests/**`,
    which silently found zero candidates in `rich` — a package with no `src/`
    directory. The assumption was in the tooling, not the ranker, and only a second
    repository with a different layout could surface it.
    """
    parts = path.lower().split("/")
    name = parts[-1]
    if name in {"conftest.py"} or name.startswith("test_") or name.endswith("_test.py"):
        return True
    return any(p in {"tests", "test"} for p in parts)


def is_source_file(path: str) -> bool:
    """A Python file that is implementation rather than test, example, or docs."""
    if not path.endswith(".py") or is_test_file(path):
        return False
    return not any(p in _NON_SOURCE_DIRS for p in path.lower().split("/"))


def candidate_commits(clone: Path, limit: int = 200) -> list[tuple[str, str]]:
    """Commits that changed both source and tests without being sprawling."""
    log = _git(clone, "log", f"-{limit}", "--format=%H\t%s")
    candidates: list[tuple[str, str]] = []
    for line in log.splitlines():
        sha, _, subject = line.partition("\t")
        if not sha:
            continue
        files = [f for f in _git(clone, "show", "--name-only", "--format=", sha).splitlines() if f]
        if len(files) > MAX_FILES_IN_COMMIT:
            continue
        if any(is_source_file(f) for f in files) and any(is_test_file(f) for f in files):
            candidates.append((sha, subject))
    return candidates


def rank_candidates(
    candidates: list[tuple[str, str]], repo: str, clone: Path
) -> list[tuple[str, str, str, str]]:
    """Order candidates best-prompt-first: (sha, subject, issue, source).

    A real linked bug report beats a fix description, which beats a bare
    subject line. Sorting rather than filtering keeps thin cases available if
    nothing better validates.
    """
    scored: list[tuple[int, str, str, str, str]] = []
    for sha, subject in candidates:
        text, source = issue_text(repo, sha, subject, clone)
        if source.startswith("issue #"):
            rank = 0 if len(text) >= MIN_USEFUL_ISSUE_CHARS else 1
        elif "leak" in source:
            rank = 3
        else:
            rank = 2 if len(text) >= MIN_USEFUL_ISSUE_CHARS else 4
        scored.append((rank, sha, subject, text, source))
    scored.sort(key=lambda row: (row[0], -len(row[3])))
    return [(sha, subject, text, source) for _, sha, subject, text, source in scored]


def issue_text(repo: str, sha: str, subject: str, clone: Path) -> tuple[str, str]:
    """Build the prompt, preferring a bug report over a fix description.

    Order of preference, most to least trustworthy:

    1. A linked issue ("fixes #123") — a symptom report, written before anyone
       knew the answer.
    2. The pull request body — often describes the change itself, so it is used
       only when nothing better exists and is labelled `pr_body (may leak fix)`
       so results built on it can be discounted.
    3. The commit subject alone — thin, but honest.
    """
    body = _git(clone, "show", "-s", "--format=%B", sha)

    # A "fixes #123" trailer points straight at the bug report, and is present on
    # plenty of commits whose subject carries no "(#456)" PR suffix.
    if direct := _ISSUE_REF.search(body):
        issue = _gh_json("issue", "view", direct.group(1), "--repo", repo, "--json", "title,body")
        if issue.get("body"):
            return (
                f"# {issue.get('title', subject)}\n\n{issue['body']}",
                f"issue #{direct.group(1)}",
            )

    pr_match = _PR_NUMBER.search(subject)
    if pr_match:
        pr = _gh_json(
            "pr",
            "view",
            pr_match.group(1),
            "--repo",
            repo,
            "--json",
            "title,body,closingIssuesReferences",
        )
        linked = pr.get("closingIssuesReferences") or []
        if isinstance(linked, list) and linked:
            number = linked[0].get("number") if isinstance(linked[0], dict) else None
            if number:
                issue = _gh_json(
                    "issue", "view", str(number), "--repo", repo, "--json", "title,body"
                )
                if issue.get("body"):
                    return (
                        f"# {issue.get('title', subject)}\n\n{issue['body']}",
                        f"issue #{number}",
                    )
        if pr.get("body"):
            return (f"# {pr.get('title', subject)}\n\n{pr['body']}", "pr_body (may leak fix)")

    # No PR reference. The commit body sometimes reads like a report; the subject
    # alone always does.
    trailer_free = "\n".join(
        line
        for line in body.splitlines()
        if not line.startswith(("Co-authored-by", "Signed-off-by"))
    ).strip()
    if len(trailer_free) > len(subject) + 40:
        return (f"# {subject}\n\n{trailer_free}", "commit_message (may leak fix)")
    return (f"# {subject}", "commit_subject")


def build_case(
    clone: Path,
    sha: str,
    subject: str,
    repo: str = DEFAULT_REPO,
    prompt: tuple[str, str] | None = None,
) -> OSSCase:
    """Reconstruct the repository as it was *with the bug but with the new tests*.

    Achieved by checking out the fixing commit, then restoring only its source
    files from the parent. The result is a tree where the new tests exist and
    fail.
    """
    files = [f for f in _git(clone, "show", "--name-only", "--format=", sha).splitlines() if f]
    source_files = frozenset(f for f in files if is_source_file(f))
    test_files = frozenset(f for f in files if is_test_file(f))

    case = OSSCase(
        repo=repo,
        sha=sha,
        title=subject,
        issue="",
        issue_source="",
        source_files=source_files,
        test_files=test_files,
    )
    case.issue, case.issue_source = prompt or issue_text(repo, sha, subject, clone)

    target = case.case_dir
    if target.exists():
        shutil.rmtree(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    # A plain copy rather than a git worktree: the sandbox excludes `.git`, and a
    # worktree's `.git` is a file pointing outside the copy, which would break.
    subprocess.run(  # noqa: S603
        ["git", "-C", str(clone), "worktree", "add", "--detach", "--force", str(target), sha],
        capture_output=True,
        text=True,
        check=False,
    )
    # Undo only the source half of the fix.
    subprocess.run(  # noqa: S603
        ["git", "-C", str(target), "checkout", f"{sha}~1", "--", *source_files],
        capture_output=True,
        text=True,
        check=False,
    )
    return case


def cleanup(clone: Path) -> None:
    """Remove staged worktrees. They are cheap to rebuild and confusing to keep."""
    subprocess.run(  # noqa: S603
        ["git", "-C", str(clone), "worktree", "prune"], capture_output=True, check=False
    )
    if WORKTREES.exists():
        shutil.rmtree(WORKTREES, ignore_errors=True)


@dataclass(slots=True)
class Reconstruction:
    """What a validation run learned about one candidate."""

    case: OSSCase
    ok: bool
    reason: str = ""
    extras: list[str] = field(default_factory=list)
