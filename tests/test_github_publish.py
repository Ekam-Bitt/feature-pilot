"""Branching, patch application, fork/PR orchestration.

git runs for real against tmp_path repos; gh is faked at the client seam.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from featurepilot.github.publish import PublishError, apply_patch, branch_name


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _make_repo(root: Path, name: str = "repo") -> Path:
    repo = root / name
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main", str(repo)], check=True, capture_output=True)
    (repo / "mod.py").write_text("def f():\n    return 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


class TestBranchName:
    def test_kebab_cases_the_title(self) -> None:
        assert branch_name(7, "Fix the Widget!  Now") == "feature-pilot/issue-7-fix-the-widget-now"

    def test_truncates_long_titles(self) -> None:
        name = branch_name(12, "A " + "very " * 30 + "long title")
        slug = name.removeprefix("feature-pilot/issue-12-")
        assert 0 < len(slug) <= 40
        assert not slug.endswith("-")

    def test_survives_a_symbol_only_title(self) -> None:
        assert branch_name(3, "!!!") == "feature-pilot/issue-3"


class TestApplyPatch:
    DIFF = (
        "diff --git a/mod.py b/mod.py\n"
        "--- a/mod.py\n"
        "+++ b/mod.py\n"
        "@@ -1,2 +1,2 @@\n"
        " def f():\n"
        "-    return 1\n"
        "+    return 2\n"
    )

    def test_applies_a_worktree_relative_diff(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        apply_patch(repo, self.DIFF)
        assert (repo / "mod.py").read_text() == "def f():\n    return 2\n"

    def test_failure_carries_git_stderr(self, tmp_path: Path) -> None:
        repo = _make_repo(tmp_path)
        bad = self.DIFF.replace("return 1", "return 999")  # context mismatch
        with pytest.raises(PublishError, match="patch"):
            apply_patch(repo, bad)
        # and the tree is untouched
        assert (repo / "mod.py").read_text() == "def f():\n    return 1\n"


class _ScriptedGh:
    """Fake for the client seam: records calls, plays scripted responses."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.responses: dict[str, list[object]] = {}

    def script(self, prefix: str, *responses: object) -> None:
        self.responses[prefix] = list(responses)

    def _play(self, args: tuple[str, ...]) -> object:
        self.calls.append(list(args))
        key = " ".join(args[:2])
        queue = self.responses.get(key)
        if not queue:
            raise AssertionError(f"unscripted gh call: {args}")
        response = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(response, Exception):
            raise response
        return response

    def run_gh(self, *args: str, token: str | None = None) -> str:
        result = self._play(args)
        assert isinstance(result, str)
        return result

    def gh_json(self, *args: str, token: str | None = None) -> object:
        return self._play(args)


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> _ScriptedGh:
    from featurepilot.github import publish

    fake = _ScriptedGh()
    monkeypatch.setattr(publish.client, "run_gh", fake.run_gh)
    monkeypatch.setattr(publish.client, "gh_json", fake.gh_json)
    monkeypatch.setattr(publish.time, "sleep", lambda s: None)
    return fake


class TestEnsureFork:
    def test_forks_and_waits_for_propagation(self, gh: _ScriptedGh) -> None:
        from featurepilot.github.client import GhError
        from featurepilot.github.issues import IssueRef
        from featurepilot.github.publish import ensure_fork

        gh.script("repo fork", "created fork user1/widget")
        gh.script("api user", {"login": "user1"})
        gh.script(
            "repo view",
            GhError(["gh"], 1, "Could not resolve"),  # not propagated yet
            {"name": "widget"},
        )
        fork = ensure_fork(IssueRef("acme", "widget", 7))
        assert fork == "user1/widget"
        assert ["repo", "fork", "acme/widget", "--clone=false"] in gh.calls

    def test_existing_fork_is_fine(self, gh: _ScriptedGh) -> None:
        from featurepilot.github.client import GhError
        from featurepilot.github.issues import IssueRef
        from featurepilot.github.publish import ensure_fork

        gh.script("repo fork", GhError(["gh"], 1, "user1/widget already exists"))
        gh.script("api user", {"login": "user1"})
        gh.script("repo view", {"name": "widget"})
        assert ensure_fork(IssueRef("acme", "widget", 7)) == "user1/widget"

    def test_gives_up_after_polling(self, gh: _ScriptedGh) -> None:
        from featurepilot.github.client import GhError
        from featurepilot.github.issues import IssueRef
        from featurepilot.github.publish import PublishError, ensure_fork

        gh.script("repo fork", "ok")
        gh.script("api user", {"login": "user1"})
        gh.script("repo view", GhError(["gh"], 1, "Could not resolve"))
        with pytest.raises(PublishError, match="fork"):
            ensure_fork(IssueRef("acme", "widget", 7))


class TestPublishRun:
    DIFF = TestApplyPatch.DIFF

    def _setup(self, tmp_path: Path, gh: _ScriptedGh):
        from featurepilot.contracts import PRSummary
        from featurepilot.github.clone import shallow_clone
        from featurepilot.github.issues import IssueRef

        upstream = _make_repo(tmp_path, "widget-upstream")
        ref = IssueRef("acme", "widget", 7)
        clone = shallow_clone(ref, tmp_path / "work", url_override=f"file://{upstream}")
        # A real fork starts with all of upstream's objects — an empty bare
        # repo would reject a push from a shallow clone (absent parents).
        fork_bare = tmp_path / "fork.git"
        subprocess.run(
            ["git", "clone", "--bare", f"file://{upstream}", str(fork_bare)],
            check=True,
            capture_output=True,
        )
        pr = PRSummary(title="Fix widget", body="It was broken.", test_plan="pytest")
        gh.script("repo fork", "ok")
        gh.script("api user", {"login": "user1"})
        gh.script("repo view", {"name": "widget"})
        return clone, fork_bare, pr

    def test_publishes_branch_and_opens_pr(self, tmp_path: Path, gh: _ScriptedGh) -> None:
        from featurepilot.github.publish import publish_run

        clone, fork_bare, pr = self._setup(tmp_path, gh)
        gh.script("pr list", [])
        gh.script("pr create", "https://github.com/acme/widget/pull/99\n")

        result = publish_run(clone, self.DIFF, pr, fork_url_override=f"file://{fork_bare}")

        assert result.pr_url == "https://github.com/acme/widget/pull/99"
        assert result.branch == "feature-pilot/issue-7-fix-widget"
        assert result.fork == "user1/widget"
        assert result.reused_pr is False
        # the branch on the "fork" carries the patched content
        show = subprocess.run(
            ["git", "-C", str(fork_bare), "show", f"{result.branch}:mod.py"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert "return 2" in show
        # commit message: title subject, body + test plan + issue link
        msg = subprocess.run(
            ["git", "-C", str(fork_bare), "log", "-1", "--format=%B", result.branch],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        assert msg.startswith("Fix widget")
        assert "## Test plan" in msg and "Closes #7" in msg
        # cross-repo PR against upstream, from the fork's branch
        create = next(c for c in gh.calls if c[:2] == ["pr", "create"])
        assert "acme/widget" in create
        assert f"user1:{result.branch}" in create
        assert "--draft" not in create

    def test_republish_reuses_branch_and_open_pr(self, tmp_path: Path, gh: _ScriptedGh) -> None:
        from featurepilot.github.publish import publish_run

        clone, fork_bare, pr = self._setup(tmp_path, gh)
        gh.script("pr list", [])
        gh.script("pr create", "https://github.com/acme/widget/pull/99\n")
        first = publish_run(clone, self.DIFF, pr, fork_url_override=f"file://{fork_bare}")

        gh.script("pr list", [{"url": first.pr_url}])
        second = publish_run(clone, self.DIFF, pr, fork_url_override=f"file://{fork_bare}")

        assert second.pr_url == first.pr_url
        assert second.branch == first.branch
        assert second.reused_pr is True
        # only the first publish created a PR; the second just force-pushed
        assert sum(1 for c in gh.calls if c[:2] == ["pr", "create"]) == 1

    def test_draft_flag_reaches_gh(self, tmp_path: Path, gh: _ScriptedGh) -> None:
        from featurepilot.github.publish import publish_run

        clone, fork_bare, pr = self._setup(tmp_path, gh)
        gh.script("pr list", [])
        gh.script("pr create", "https://github.com/acme/widget/pull/100\n")
        publish_run(clone, self.DIFF, pr, draft=True, fork_url_override=f"file://{fork_bare}")
        create = next(c for c in gh.calls if c[:2] == ["pr", "create"])
        assert "--draft" in create

    def test_refuses_a_dirty_clone(self, tmp_path: Path, gh: _ScriptedGh) -> None:
        from featurepilot.github.publish import PublishError, publish_run

        clone, fork_bare, pr = self._setup(tmp_path, gh)
        (clone.path / "mod.py").write_text("tampered\n")
        with pytest.raises(PublishError, match="uncommitted|dirty"):
            publish_run(clone, self.DIFF, pr, fork_url_override=f"file://{fork_bare}")

    def test_refuses_an_empty_diff(self, tmp_path: Path, gh: _ScriptedGh) -> None:
        from featurepilot.github.publish import PublishError, publish_run

        clone, fork_bare, pr = self._setup(tmp_path, gh)
        with pytest.raises(PublishError, match="empty"):
            publish_run(clone, "", pr, fork_url_override=f"file://{fork_bare}")


class TestEnsureForkOwnRepo:
    def test_owning_upstream_skips_the_fork(self, gh: _ScriptedGh) -> None:
        """GitHub refuses self-forks; when you own upstream, the branch goes
        straight there and the 'fork' is upstream itself."""
        from featurepilot.github.issues import IssueRef
        from featurepilot.github.publish import ensure_fork

        gh.script("api user", {"login": "acme"})
        fork = ensure_fork(IssueRef("acme", "widget", 7))
        assert fork == "acme/widget"
        assert not any(c[:2] == ["repo", "fork"] for c in gh.calls)
