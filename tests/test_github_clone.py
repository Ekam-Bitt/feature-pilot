"""Shallow clones under .fp/clones/<run_id>.

Uses real git against tmp_path repositories — offline-safe, and the same
subprocess the production path runs, so nothing is faked here.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from featurepilot.github.clone import clone_root_for, existing_clone, shallow_clone
from featurepilot.github.issues import IssueRef

REF = IssueRef(owner="acme", repo="widget", number=7)


def _make_source_repo(root: Path) -> Path:
    """A local repo with two commits, so shallowness is observable."""
    src = root / "widget-src"
    src.mkdir()
    env = {"GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}

    def git(*args: str) -> None:
        subprocess.run(
            ["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t", *args],
            check=True,
            capture_output=True,
            env={**env, "PATH": "/usr/bin:/bin:/usr/local/bin"},
        )

    subprocess.run(["git", "init", "-b", "main", str(src)], check=True, capture_output=True)
    (src / "a.txt").write_text("one\n")
    git("add", "-A")
    git("commit", "-m", "first")
    (src / "a.txt").write_text("two\n")
    git("add", "-A")
    git("commit", "-m", "second")
    return src


class TestCloneRootFor:
    def test_lives_under_fp_clones_keyed_by_run_id(self) -> None:
        assert clone_root_for("abc123") == Path(".fp") / "clones" / "abc123"


class TestShallowClone:
    def test_clones_depth_one_and_reports_head(self, tmp_path: Path) -> None:
        src = _make_source_repo(tmp_path)
        src_head = subprocess.run(
            ["git", "-C", str(src), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

        cloned = shallow_clone(REF, tmp_path / "dest", url_override=f"file://{src}")

        assert cloned.path == tmp_path / "dest" / "widget"
        assert cloned.ref == REF
        assert cloned.head_sha == src_head
        assert cloned.default_branch == "main"
        depth = subprocess.run(
            ["git", "-C", str(cloned.path), "rev-list", "--count", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert depth == "1"


class TestExistingClone:
    def test_finds_the_sole_clone_for_a_run(self, tmp_path: Path) -> None:
        src = _make_source_repo(tmp_path)
        root = tmp_path / "clones" / "run42"
        shallow_clone(REF, root, url_override=f"file://{src}")
        assert existing_clone("run42", clones_root=tmp_path / "clones") == root / "widget"

    def test_none_when_absent(self, tmp_path: Path) -> None:
        assert existing_clone("nope", clones_root=tmp_path / "clones") is None


class TestReattachClone:
    def test_rebuilds_the_clone_facts_for_resume(self, tmp_path: Path) -> None:
        from featurepilot.github.clone import reattach_clone

        src = _make_source_repo(tmp_path)
        root = tmp_path / "clones" / "run7"
        original = shallow_clone(REF, root, url_override=f"file://{src}")

        # even after publishing moved HEAD to a feature branch...
        subprocess.run(
            ["git", "-C", str(original.path), "checkout", "-b", "feature-pilot/issue-7-x"],
            check=True,
            capture_output=True,
        )
        reattached = reattach_clone("run7", REF, clones_root=tmp_path / "clones")

        assert reattached == original  # ...the base facts are recovered

    def test_none_when_no_clone_exists(self, tmp_path: Path) -> None:
        from featurepilot.github.clone import reattach_clone

        assert reattach_clone("gone", REF, clones_root=tmp_path / "clones") is None
