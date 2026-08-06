"""Sandbox isolation.

The allowlist tests are pure and run in the default suite, because they are the
security boundary — a gap there means an agent-chosen command reaches the host
network or filesystem. The lifecycle tests need a real daemon and are
`docker`-marked.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from featurepilot.config import Settings
from featurepilot.sandbox.runner import (
    DEFAULT_ALLOWED,
    EXCLUDED,
    CommandNotAllowed,
    ExecResult,
    Sandbox,
    _tar_of,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_REPO = REPO_ROOT / "fixtures" / "target-repo"


@pytest.fixture
def sandbox(settings: Settings) -> Sandbox:
    """Unstarted sandbox: enough to exercise the pure logic."""
    return Sandbox(FIXTURE_REPO, settings=settings, run_id="test")


class TestAllowlist:
    @pytest.mark.parametrize(
        "command",
        [
            "pytest -q",
            "python -m pytest tests/",
            "ls -la",
            "cat src/shopsvc/cart.py",
            "grep -rn threshold src/",
            "/venv/bin/python -V",  # absolute path, basename is allowed
        ],
    )
    def test_permitted_commands(self, sandbox: Sandbox, command: str) -> None:
        sandbox._check_allowed(command)  # must not raise

    @pytest.mark.parametrize(
        "command",
        ["curl https://example.com", "wget x", "nc -l 1234", "ssh host", "sudo ls", "bash -c x"],
    )
    def test_denied_executables(self, sandbox: Sandbox, command: str) -> None:
        with pytest.raises(CommandNotAllowed):
            sandbox._check_allowed(command)

    @pytest.mark.parametrize(
        "command",
        [
            "pytest; curl https://evil.example",
            "pytest && curl https://evil.example",
            "pytest || curl https://evil.example",
            "cat f | sh",
            "cat /etc/passwd > /tmp/x",
            "sh < script",
        ],
    )
    def test_chaining_attempts_are_rejected_with_a_clear_message(
        self, sandbox: Sandbox, command: str
    ) -> None:
        """These would be inert anyway (no shell runs them), but a crisp error
        beats the model puzzling over `pytest: unrecognized argument ';'`."""
        with pytest.raises(CommandNotAllowed):
            sandbox._check_allowed(command)

    @pytest.mark.parametrize(
        "command",
        [
            'python -c "import os; print(os.getuid())"',
            "grep 'a|b' src/shopsvc/cart.py",
            'python -c "print(1 > 0)"',
            "echo 'a && b'",
        ],
    )
    def test_metacharacters_inside_quotes_are_accepted(
        self, sandbox: Sandbox, command: str
    ) -> None:
        """Parsing with shlex before checking means quoting is respected. A naive
        substring scan rejects all of these, which would block legitimate work —
        `python -c` is how you inspect anything non-trivial."""
        argv = sandbox._check_allowed(command)
        assert len(argv) >= 2

    def test_check_returns_argv_for_shell_free_execution(self, sandbox: Sandbox) -> None:
        """The returned argv is what gets exec'd — no shell in between. That,
        not the token blocklist, is what makes chaining impossible."""
        assert sandbox._check_allowed("pytest -q tests/") == ["pytest", "-q", "tests/"]

    def test_empty_command_rejected(self, sandbox: Sandbox) -> None:
        with pytest.raises(CommandNotAllowed):
            sandbox._check_allowed("   ")

    def test_unparseable_quoting_rejected(self, sandbox: Sandbox) -> None:
        with pytest.raises(CommandNotAllowed):
            sandbox._check_allowed("pytest 'unterminated")

    def test_error_message_lists_the_allowlist(self, sandbox: Sandbox) -> None:
        """The message is fed back to the model, so it has to be actionable."""
        with pytest.raises(CommandNotAllowed) as exc:
            sandbox._check_allowed("curl x")
        assert "pytest" in str(exc.value)

    def test_allowlist_has_no_shell_or_network_tools(self) -> None:
        """A regression guard: adding `bash` or `curl` here would quietly undo
        the network cut, since the agent could then re-enable egress itself."""
        assert not {"bash", "sh", "zsh", "curl", "wget", "nc", "ssh", "sudo"} & DEFAULT_ALLOWED

    def test_custom_allowlist_is_respected(self, settings: Settings) -> None:
        box = Sandbox(FIXTURE_REPO, settings=settings, allowed=frozenset({"ls"}))
        box._check_allowed("ls")
        with pytest.raises(CommandNotAllowed):
            box._check_allowed("pytest")


class TestRepoArchive:
    def test_includes_source_and_tests(self) -> None:
        import io
        import tarfile

        names = set()
        with tarfile.open(fileobj=io.BytesIO(_tar_of(FIXTURE_REPO))) as tar:
            names = set(tar.getnames())
        assert "src/shopsvc/cart.py" in names
        assert "tests/test_cart.py" in names
        assert "pyproject.toml" in names

    def test_excludes_host_virtualenv_and_caches(self) -> None:
        """A host .venv is the wrong platform and would shadow the container's."""
        import io
        import tarfile

        with tarfile.open(fileobj=io.BytesIO(_tar_of(FIXTURE_REPO))) as tar:
            names = tar.getnames()
        for name in names:
            assert not any(part in EXCLUDED for part in Path(name).parts), name

    def test_excluded_set_covers_the_usual_suspects(self) -> None:
        assert {".venv", ".git", "__pycache__", ".pytest_cache"} <= EXCLUDED


class TestExecResult:
    def test_ok_only_on_zero(self) -> None:
        assert ExecResult("c", 0, "", "", 1).ok
        assert not ExecResult("c", 1, "", "", 1).ok

    def test_combined_includes_stderr(self) -> None:
        result = ExecResult("c", 1, "out", "boom", 1)
        assert "out" in result.combined
        assert "boom" in result.combined

    def test_combined_notes_a_timeout(self) -> None:
        result = ExecResult("c", 137, "", "", 1, timed_out=True)
        assert "timed out" in result.combined

    def test_clean_run_has_no_stderr_section(self) -> None:
        assert ExecResult("c", 0, "all good", "", 1).combined == "all good"


class TestGuards:
    async def test_exec_before_start_raises(self, sandbox: Sandbox) -> None:
        from featurepilot.sandbox.runner import SandboxError

        with pytest.raises(SandboxError, match="not started"):
            await sandbox._exec_shell("ls")

    async def test_missing_repo_raises(self, settings: Settings) -> None:
        from featurepilot.sandbox.runner import SandboxError

        box = Sandbox(Path("/nope/does/not/exist"), settings=settings)
        with pytest.raises(SandboxError, match="not found"):
            await box.start()

    async def test_destroy_is_safe_before_start(self, sandbox: Sandbox) -> None:
        await sandbox.destroy()  # must not raise


# --------------------------------------------------------------------------
# Integration: needs a running Docker daemon.
# --------------------------------------------------------------------------


@pytest.mark.docker
class TestContainerLifecycle:
    @pytest.fixture
    async def live(self, settings: Settings):  # noqa: ANN201
        box = Sandbox(FIXTURE_REPO, settings=settings)
        await box.start()
        try:
            yield box
        finally:
            await box.destroy()

    async def test_repo_is_present_in_the_container(self, live: Sandbox) -> None:
        result = await live.exec("ls src/shopsvc")
        assert result.ok
        assert "cart.py" in result.stdout

    async def test_runs_as_non_root(self, live: Sandbox) -> None:
        result = await live.exec('python -c "import os; print(os.getuid())"')
        assert result.stdout.strip() == "10001"

    async def test_host_filesystem_is_not_visible(self, live: Sandbox) -> None:
        """The container must not see the Feature Pilot source tree."""
        result = await live.exec("ls /work")
        assert "featurepilot" not in result.stdout

    async def test_suite_is_red_before_any_fix(self, live: Sandbox) -> None:
        install = await live.install_dependencies()
        assert install.ok or "Successfully installed" in install.combined
        result = await live.exec("pytest -q --tb=no", timeout=300)
        assert not result.ok, "fixture should start red"
        assert "17 failed" in result.combined

    async def test_network_is_cut(self, live: Sandbox) -> None:
        await live.install_dependencies()
        await live.cut_network()
        # pip is the handiest network probe already present in the image.
        result = await live._exec_shell("pip download --no-deps --dest /tmp/x requests", timeout=60)
        assert not result.ok, "network should be unreachable after cut_network()"

    async def test_dependency_install_after_cut_is_refused(self, live: Sandbox) -> None:
        from featurepilot.sandbox.runner import SandboxError

        await live.cut_network()
        with pytest.raises(SandboxError, match="before the network is cut"):
            await live.install_dependencies()

    async def test_timeout_is_enforced(self, live: Sandbox) -> None:
        result = await live.exec('python -c "import time; time.sleep(30)"', timeout=2)
        assert result.timed_out
        assert result.duration_ms < 15_000

    async def test_agent_commands_reach_no_shell(self, live: Sandbox) -> None:
        """The actual security property, proven rather than asserted.

        If a shell were interpreting agent commands, `$(id -u)` would expand to
        10001. Getting the literal text back proves argv went straight to execve,
        which is what makes command chaining impossible regardless of what the
        token blocklist happens to catch.
        """
        result = await live.exec("echo $(id -u)")
        assert result.ok
        assert result.stdout.strip() == "$(id -u)"
        assert "10001" not in result.stdout

    async def test_copied_files_are_writable_by_the_agent(self, live: Sandbox) -> None:
        """Regression guard for a bug that made the whole system inert.

        put_archive preserves the archive's uid, which on macOS is the host
        developer's (501) while the container runs as 10001 — so every copied
        file was read-only and no edit could ever land.
        """
        result = await live._exec_shell("echo '# touched' >> src/shopsvc/cart.py")
        assert result.ok, f"agent cannot write to a copied file: {result.combined}"
        listing = await live.exec("ls -ln src/shopsvc/cart.py")
        assert " 10001 " in listing.stdout, listing.stdout

    async def test_agent_can_create_files_in_copied_directories(self, live: Sandbox) -> None:
        result = await live._exec_shell("echo x > src/shopsvc/scratch.py")
        assert result.ok, f"agent cannot create files in a copied dir: {result.combined}"


@pytest.mark.docker
class TestSnapshotRestore:
    @pytest.fixture
    async def live(self, settings: Settings):  # noqa: ANN201
        box = Sandbox(FIXTURE_REPO, settings=settings)
        await box.start()
        await box.snapshot()
        try:
            yield box
        finally:
            await box.destroy()

    async def test_snapshot_hides_git_from_the_worktree(self, live: Sandbox) -> None:
        """The agent must not see repository state that isn't part of the task."""
        result = await live.exec("ls -a")
        assert ".git" not in result.stdout.split()

    async def test_diff_is_empty_at_baseline(self, live: Sandbox) -> None:
        assert (await live.diff()).strip() == ""

    async def test_diff_reports_an_edit(self, live: Sandbox) -> None:
        assert (await live._exec_shell("echo '# touched' >> src/shopsvc/cart.py")).ok
        diff = await live.diff()
        assert "cart.py" in diff
        assert "# touched" in diff

    async def test_changed_files_lists_the_edit(self, live: Sandbox) -> None:
        assert (await live._exec_shell("echo '# touched' >> src/shopsvc/cart.py")).ok
        assert "src/shopsvc/cart.py" in await live.changed_files()

    async def test_restore_discards_edits(self, live: Sandbox) -> None:
        """The property the repair loop depends on: attempt N+1 starts clean."""
        assert (await live._exec_shell("echo '# broken' >> src/shopsvc/cart.py")).ok
        assert (await live.diff()).strip() != ""
        await live.restore()
        assert (await live.diff()).strip() == ""
        assert await live.changed_files() == []

    async def test_restore_removes_new_files(self, live: Sandbox) -> None:
        assert (await live._exec_shell("echo x > src/shopsvc/oops.py")).ok
        await live.restore()
        result = await live.exec("ls src/shopsvc")
        assert "oops.py" not in result.stdout


@pytest.mark.docker
class TestReaper:
    """Teardown lives in a `finally`, but a hard crash skips it and every orphan
    holds its memory reservation until the daemon restarts. The reaper makes a
    leak self-correcting rather than cumulative."""

    async def test_reaps_an_orphan(self, settings: Settings) -> None:
        box = Sandbox(FIXTURE_REPO, settings=settings)
        await box.start()
        name = f"featurepilot-{box.run_id}"
        # Simulate a crash: drop the handle without destroying the container.
        box._container = None

        removed = await Sandbox.reap_stale(0)
        assert name in removed

    async def test_leaves_a_fresh_container_alone(self, settings: Settings) -> None:
        """A concurrent run must survive another run's startup reap."""
        async with Sandbox(FIXTURE_REPO, settings=settings) as box:
            removed = await Sandbox.reap_stale(3600)
            assert f"featurepilot-{box.run_id}" not in removed
            assert (await box.exec("pwd")).ok, "container should still be usable"

    async def test_only_touches_labelled_containers(self, settings: Settings) -> None:
        """Nothing else on the developer's machine is at risk."""
        import docker

        client = docker.from_env()
        bystander = client.containers.run(
            "python:3.13-slim", command="sleep 60", detach=True, name="fp-bystander-test"
        )
        try:
            await Sandbox.reap_stale(0)
            bystander.reload()
            assert bystander.status == "running"
        finally:
            bystander.remove(force=True)
