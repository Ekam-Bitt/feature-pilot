"""Path validation.

The second security boundary after the command allowlist. Model-supplied paths
reach `read_text`/`write_text`/`edit_text` directly, so an escape here means the
agent reads or writes host-visible container paths outside its worktree.

All pure: `resolve()` is string work on the host and never consults the
container's filesystem, which is deliberate — a symlink planted inside the
worktree cannot widen the check after the fact.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from featurepilot.config import Settings
from featurepilot.sandbox.runner import PathOutsideWorktree, Sandbox


@pytest.fixture
def box(settings: Settings) -> Sandbox:
    return Sandbox(Path("fixtures/target-repo"), settings=settings, run_id="paths")


class TestAccepted:
    @pytest.mark.parametrize(
        ("given", "expected"),
        [
            ("src/shopsvc/cart.py", "/work/src/shopsvc/cart.py"),
            ("./src/shopsvc/cart.py", "/work/src/shopsvc/cart.py"),
            ("pyproject.toml", "/work/pyproject.toml"),
            ("src//shopsvc///cart.py", "/work/src/shopsvc/cart.py"),
            ("src/./shopsvc/cart.py", "/work/src/shopsvc/cart.py"),
            # A `..` that stays inside the worktree is legitimate.
            ("src/shopsvc/../shopsvc/cart.py", "/work/src/shopsvc/cart.py"),
            # Absolute paths are fine when already inside the worktree, since
            # that is how the agent sees paths echoed back by tools.
            ("/work/src/shopsvc/cart.py", "/work/src/shopsvc/cart.py"),
        ],
    )
    def test_normalises(self, box: Sandbox, given: str, expected: str) -> None:
        assert box.resolve(given) == expected


class TestRejected:
    @pytest.mark.parametrize(
        "given",
        [
            "../etc/passwd",
            "../../etc/passwd",
            "src/../../etc/passwd",
            "src/shopsvc/../../../etc/passwd",
            "./../outside.py",
        ],
    )
    def test_traversal_above_the_worktree(self, box: Sandbox, given: str) -> None:
        with pytest.raises(PathOutsideWorktree):
            box.resolve(given)

    @pytest.mark.parametrize(
        "given",
        ["/etc/passwd", "/snapshot.git/config", "/venv/bin/python", "/", "/workshop/x.py"],
    )
    def test_absolute_paths_outside_the_worktree(self, box: Sandbox, given: str) -> None:
        """`/workshop` is the interesting one: a naive prefix check on the string
        '/work' would accept it."""
        with pytest.raises(PathOutsideWorktree):
            box.resolve(given)

    def test_the_snapshot_git_dir_is_unreachable(self, box: Sandbox) -> None:
        """Reaching /snapshot.git would let the agent rewrite its own baseline
        and defeat restore()."""
        with pytest.raises(PathOutsideWorktree):
            box.resolve("/snapshot.git/HEAD")

    @pytest.mark.parametrize("given", ["", ".", "./", "/work", "/work/", "src/.."])
    def test_paths_resolving_to_the_root_itself(self, box: Sandbox, given: str) -> None:
        with pytest.raises(PathOutsideWorktree):
            box.resolve(given)

    def test_null_byte(self, box: Sandbox) -> None:
        with pytest.raises(PathOutsideWorktree, match="null byte"):
            box.resolve("src/cart.py\x00.txt")

    def test_error_message_is_actionable(self, box: Sandbox) -> None:
        """The message goes back to the model, which needs to know what to do."""
        with pytest.raises(PathOutsideWorktree) as exc:
            box.resolve("../../etc/passwd")
        assert "repo-relative" in str(exc.value)


class TestEncodedTraversal:
    @pytest.mark.parametrize("given", ["%2e%2e/etc/passwd", "..%2fetc%2fpasswd"])
    def test_url_encoding_is_not_decoded(self, box: Sandbox, given: str) -> None:
        """These must not be *decoded* into a traversal. They are treated as
        ordinary (odd) filenames inside the worktree, which is safe — the danger
        would be a layer that decodes them after this check."""
        resolved = box.resolve(given)
        assert resolved.startswith("/work/")
        assert resolved != "/etc/passwd"
