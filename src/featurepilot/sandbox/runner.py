"""Per-run container sandbox.

Everything the agent executes happens in here, never on the host. A hostile
target repo or a badly-chosen shell command can cost us a throwaway container and
nothing else.

Lifecycle:

    create -> install deps (network on) -> cut network -> snapshot
           -> agent edits -> test -> diff        \\
           -> restore -> agent edits -> test ... /  (the repair loop)
           -> destroy

**Snapshots use a git directory outside the worktree** (`/snapshot.git` with
`--work-tree=/work`). Three reasons over `docker commit`: it is near-instant, it
gives `diff()` for free, and the agent never sees a `.git` in its working tree —
so it can't accidentally commit, reset, or be confused by repository state that
isn't part of the task.
"""

from __future__ import annotations

import asyncio
import io
import logging
import shlex
import tarfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from featurepilot.config import Settings, get_settings
from featurepilot.sandbox.image import ensure_image

if TYPE_CHECKING:
    from docker.models.containers import Container

log = logging.getLogger(__name__)

#: Must match the user created in image.py's Dockerfile. Copied files are owned
#: by this uid so the agent can actually edit them.
SANDBOX_UID = 10001
SANDBOX_GID = 10001
SANDBOX_USER = "sandbox"

#: Never copied into the sandbox: host virtualenvs are the wrong platform, and
#: caches and VCS history are noise the agent would have to read past.
EXCLUDED = {
    ".venv",
    "venv",
    ".git",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".tox",
    "node_modules",
    ".DS_Store",
    ".fp",
}

#: Executables the agent may invoke. An allowlist rather than a blocklist: the
#: set of dangerous commands is unbounded, the set of useful ones is small.
#: Enforced here rather than in the MCP server so there is one checkpoint that
#: every execution path crosses.
DEFAULT_ALLOWED = frozenset(
    {
        "python",
        "python3",
        "pytest",
        "pip",
        "ls",
        "cat",
        "head",
        "tail",
        "wc",
        "find",
        "grep",
        "rg",
        "echo",
        "true",
        "false",
        "test",
        "diff",
        "sort",
        "uniq",
        "cut",
        "sed",
        "awk",
        "pwd",
        "which",
        "env",
    }
)


class SandboxError(RuntimeError):
    """Sandbox could not be created or operated."""


class PathOutsideWorktree(SandboxError):
    """A model-supplied path pointed outside the sandbox worktree."""

    def __init__(self, path: str, reason: str) -> None:
        super().__init__(f"path {path!r} rejected: {reason}. Use repo-relative paths.")
        self.path = path


class CommandNotAllowed(SandboxError):
    def __init__(self, executable: str, allowed: frozenset[str]) -> None:
        super().__init__(
            f"{executable!r} is not in the sandbox allowlist. Allowed: {', '.join(sorted(allowed))}"
        )
        self.executable = executable


@dataclass(slots=True)
class ExecResult:
    command: str
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0

    @property
    def combined(self) -> str:
        parts = [self.stdout]
        if self.stderr.strip():
            parts.append(f"--- stderr ---\n{self.stderr}")
        if self.timed_out:
            parts.append("--- command timed out ---")
        return "\n".join(p for p in parts if p.strip())


def _tar_of(source: Path) -> bytes:
    """Tar `source`'s contents for put_archive, skipping EXCLUDED paths.

    Ownership is rewritten to the sandbox user. `put_archive` preserves whatever
    uid the archive carries, which on macOS is the host developer's (501) — and
    the container runs as uid 10001, so without this every copied file and
    directory is read-only to the agent and no edit can land. Setting ownership
    in the archive avoids needing a privileged `chown` step inside the container.
    """
    buf = io.BytesIO()

    def own(info: tarfile.TarInfo) -> tarfile.TarInfo:
        info.uid = SANDBOX_UID
        info.gid = SANDBOX_GID
        info.uname = SANDBOX_USER
        info.gname = SANDBOX_USER
        # Guarantee the owner can write, whatever the host mode was.
        info.mode |= 0o200
        if info.isdir():
            info.mode |= 0o300  # write + traverse, so new files can be created
        return info

    with tarfile.open(fileobj=buf, mode="w") as tar:
        for path in sorted(source.rglob("*")):
            rel = path.relative_to(source)
            if any(part in EXCLUDED for part in rel.parts):
                continue
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                continue
            tar.add(path, arcname=str(rel), recursive=False, filter=own)
    return buf.getvalue()


class Sandbox:
    """A container scoped to one run.

    Async on the outside, threads on the inside: the docker SDK is synchronous
    and its calls are slow enough (seconds, for a test run) that blocking the
    loop would stall the event stream the CLI is rendering.
    """

    GIT_DIR = "/snapshot.git"

    def __init__(
        self,
        repo_path: Path,
        *,
        settings: Settings | None = None,
        allowed: frozenset[str] | None = None,
        run_id: str | None = None,
    ) -> None:
        self.repo_path = Path(repo_path).resolve()
        self.settings = settings or get_settings()
        self.allowed = allowed if allowed is not None else DEFAULT_ALLOWED
        self.run_id = run_id or uuid.uuid4().hex[:12]
        self.workdir = self.settings.sandbox_workdir
        self._container: Container | None = None
        self._network_cut = False

    # --- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        if not self.repo_path.is_dir():
            raise SandboxError(f"target repo not found: {self.repo_path}")

        await ensure_image(self.settings.sandbox_image)
        # Cheap insurance against orphans from a previously crashed run.
        await self.reap_stale(self.settings.sandbox_reap_after_s)
        await asyncio.to_thread(self._create_container)
        await asyncio.to_thread(self._copy_repo)
        log.info("sandbox %s started from %s", self.run_id, self.repo_path)

    def _create_container(self) -> None:
        import docker

        client = docker.from_env()
        try:
            self._container = client.containers.run(
                self.settings.sandbox_image,
                command="sleep infinity",
                name=f"featurepilot-{self.run_id}",
                detach=True,
                working_dir=self.workdir,
                # Resource caps: a runaway test or fork bomb hits a wall rather
                # than the host's memory.
                mem_limit=self.settings.sandbox_memory,
                nano_cpus=int(self.settings.sandbox_cpus * 1_000_000_000),
                pids_limit=self.settings.sandbox_pids_limit,
                # No new privileges: even if something in here is setuid, it
                # cannot escalate.
                security_opt=["no-new-privileges:true"],
                cap_drop=["ALL"],
                labels={"featurepilot.run": self.run_id},
                auto_remove=False,
            )
        except docker.errors.DockerException as exc:
            raise SandboxError(f"could not start sandbox container: {exc}") from exc

    def _copy_repo(self) -> None:
        assert self._container is not None
        archive = _tar_of(self.repo_path)
        if not self._container.put_archive(self.workdir, archive):
            raise SandboxError("failed to copy the target repo into the sandbox")

    @staticmethod
    async def reap_stale(max_age_seconds: int = 3600) -> list[str]:
        """Remove sandbox containers older than `max_age_seconds`.

        Teardown normally happens in a `finally`, but a hard crash — or a test
        harness that aborts during cleanup — skips it, and every orphan holds its
        memory reservation until the daemon restarts. Reaping by label at startup
        makes a leak self-correcting instead of cumulative.

        Only containers carrying the `featurepilot.run` label are touched, so
        nothing else on the developer's machine is at risk.
        """
        return await asyncio.to_thread(Sandbox._reap_sync, max_age_seconds)

    @staticmethod
    def _reap_sync(max_age_seconds: int) -> list[str]:
        import datetime as _dt

        import docker

        removed: list[str] = []
        try:
            client = docker.from_env()
            containers = client.containers.list(all=True, filters={"label": "featurepilot.run"})
        except docker.errors.DockerException as exc:
            log.warning("could not list sandbox containers to reap: %s", exc)
            return removed

        now = _dt.datetime.now(_dt.UTC)
        for container in containers:
            created_raw = container.attrs.get("Created", "")
            try:
                # Docker returns RFC3339 with nanoseconds; trim to microseconds.
                stamp = created_raw[:26].rstrip("Z")
                created = _dt.datetime.fromisoformat(stamp).replace(tzinfo=_dt.UTC)
            except ValueError:
                continue
            if (now - created).total_seconds() < max_age_seconds:
                continue
            try:
                container.remove(force=True)
                removed.append(str(container.name))
            except Exception as exc:  # noqa: BLE001 — best effort
                log.warning("could not reap %s: %s", container.name, exc)
        if removed:
            log.info("reaped %d stale sandbox containers", len(removed))
        return removed

    @classmethod
    async def attach(
        cls, run_id: str, *, settings: Settings | None = None, repo_path: Path | None = None
    ) -> Sandbox:
        """Reconnect to a running sandbox by run id.

        The MCP servers are separate stdio processes, so they cannot share this
        object with the graph. Attaching lets them reuse the same allowlist,
        timeout, and path-validation logic instead of reimplementing container
        access — one security checkpoint rather than three.
        """
        import docker

        box = cls(repo_path or Path.cwd(), settings=settings, run_id=run_id)
        client = docker.from_env()
        try:
            box._container = client.containers.get(f"featurepilot-{run_id}")
        except docker.errors.NotFound as exc:
            raise SandboxError(f"no running sandbox for run {run_id!r}") from exc
        # An attached sandbox never installs dependencies, so treat the network
        # as already cut rather than re-opening that door.
        box._network_cut = True
        return box

    # --- paths ------------------------------------------------------------

    def resolve(self, path: str) -> str:
        """Validate a repo-relative path and return its absolute container path.

        Model-supplied paths are untrusted. Everything is normalised and checked
        to be inside the worktree, so `../../etc/passwd`, an absolute path, or a
        `..` buried mid-path is rejected rather than escaping the sandbox. This is
        pure string work on the host: no container round trip, and it runs before
        any file operation.
        """
        if "\x00" in path:
            raise PathOutsideWorktree(path, "contains a null byte")
        candidate = PurePosixPath(path)
        root = PurePosixPath(self.workdir)
        if candidate.is_absolute():
            if not candidate.is_relative_to(root):
                raise PathOutsideWorktree(path, "absolute path outside the worktree")
            candidate = candidate.relative_to(root)
        # Resolve `.` and `..` textually; the container's own filesystem is not
        # consulted, so a symlink cannot be used to widen the check afterwards.
        parts: list[str] = []
        for part in candidate.parts:
            if part in (".", ""):
                continue
            if part == "..":
                if not parts:
                    raise PathOutsideWorktree(path, "escapes the worktree via '..'")
                parts.pop()
                continue
            parts.append(part)
        if not parts:
            raise PathOutsideWorktree(path, "resolves to the worktree root")
        return str(root.joinpath(*parts))

    # --- file access ------------------------------------------------------
    # Reads and writes go through the docker archive API rather than
    # `cat`/`tee`, so file content never passes through a shell. That matters:
    # the agent writes source code full of quotes, backslashes and newlines,
    # and any quoting scheme would eventually corrupt one of them.

    async def read_text(self, path: str) -> str:
        """Entire file contents.

        The common case, so it returns a plain string. Ranged reads have their own
        method rather than making every caller unpack a tuple it does not need.
        """
        return await asyncio.to_thread(self._read_sync, self.resolve(path))

    async def read_range(
        self, path: str, *, offset: int = 0, limit: int = 0
    ) -> tuple[str, int, int]:
        """Read a 1-indexed line range, returning (text, first_line, total_lines).

        Exists because whole-file reads do not survive a real repository: click's
        core.py is ~35k tokens, and a coder loop accumulating a few of those blew
        a 400k-token budget. The caller reports which slice it got, so the model
        knows what it has and can ask for more.
        """
        body = await self.read_text(path)
        lines = body.splitlines()
        total = len(lines)
        if not offset and not limit:
            return body, 1, total
        start = max(1, offset or 1)
        end = min(total, start + limit - 1) if limit else total
        return "\n".join(lines[start - 1 : end]), start, total

    def _read_sync(self, target: str) -> str:
        import docker

        assert self._container is not None
        try:
            stream, _stat = self._container.get_archive(target)
        except docker.errors.NotFound as exc:
            raise FileNotFoundError(f"no such file in sandbox: {target}") from exc
        raw = b"".join(stream)
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            member = next((m for m in tar.getmembers() if m.isfile()), None)
            if member is None:
                raise IsADirectoryError(f"not a file: {target}")
            extracted = tar.extractfile(member)
            if extracted is None:
                raise FileNotFoundError(f"could not read: {target}")
            return extracted.read().decode("utf-8", "replace")

    async def write_text(self, path: str, content: str) -> None:
        target = self.resolve(path)
        await asyncio.to_thread(self._write_sync, target, content)

    def _write_sync(self, target: str, content: str) -> None:
        assert self._container is not None
        posix = PurePosixPath(target)
        payload = content.encode("utf-8")
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name=posix.name)
            info.size = len(payload)
            info.mode = 0o644
            info.uid = SANDBOX_UID
            info.gid = SANDBOX_GID
            info.uname = SANDBOX_USER
            info.gname = SANDBOX_USER
            tar.addfile(info, io.BytesIO(payload))
        if not self._container.put_archive(str(posix.parent), buf.getvalue()):
            raise SandboxError(f"failed to write {target}")

    async def edit_text(self, path: str, old: str, new: str) -> str:
        """Replace exactly one occurrence of `old` with `new`.

        Requiring a unique match is the point: a model that passes an ambiguous
        snippet gets told so, instead of silently editing the wrong one of three
        identical lines. Zero matches usually means it edited from memory rather
        than from a read, which is worth surfacing as an error too.
        """
        body = await self.read_text(path)
        occurrences = body.count(old)
        if occurrences == 0:
            raise ValueError(
                f"the old string was not found in {path}. Read the file and copy "
                "the exact text, including indentation."
            )
        if occurrences > 1:
            raise ValueError(
                f"the old string appears {occurrences} times in {path}. Include "
                "enough surrounding context to identify one occurrence uniquely."
            )
        await self.write_text(path, body.replace(old, new, 1))
        return f"edited {path}"

    # --- search -----------------------------------------------------------
    # Patterns come from the model, so they are passed as single argv elements.
    # No shell means a pattern like `$(id)` or `*; rm -rf /` is inert text.

    async def glob(self, pattern: str) -> list[str]:
        result = await self._exec_argv(
            ["find", ".", "-type", "f", "-name", pattern, "-not", "-path", "./.git/*"],
            display=f"glob {pattern}",
            timeout=60,
        )
        return sorted(
            line.removeprefix("./") for line in result.stdout.splitlines() if line.strip()
        )

    async def grep(self, pattern: str, path: str = ".") -> ExecResult:
        target = "." if path == "." else self.resolve(path)
        return await self._exec_argv(
            # -E: extended regex. Without it `(def|class)` is a literal string,
            # so any alternation silently matches nothing — and the offline
            # benchmark (Python `re`, which is ERE-like) would disagree with
            # production about what a pattern means.
            ["grep", "-rnE", "--binary-files=without-match", pattern, target],
            display=f"grep {pattern} {path}",
            timeout=120,
        )

    async def install_dependencies(self, command: str | None = None) -> ExecResult:
        """Install the target repo's dependencies, with network access.

        Must be called before `cut_network()`. Uses the raw exec path rather than
        `exec()` because pip is a setup concern and shouldn't need to be on the
        agent's allowlist.
        """
        if self._network_cut:
            raise SandboxError("dependencies must be installed before the network is cut")
        cmd = command or "pip install -e '.[dev]' || pip install -e . || true"
        return await self._exec_shell(cmd, timeout=600)

    async def cut_network(self) -> None:
        """Disconnect the container from all networks.

        Everything after this point — every command the agent chooses — runs with
        no egress. Disconnecting beats recreating the container with
        `network_mode=none`, which would discard the installed dependencies.
        """
        await asyncio.to_thread(self._cut_network_sync)
        self._network_cut = True
        log.info("sandbox %s network cut", self.run_id)

    def _cut_network_sync(self) -> None:
        import docker

        assert self._container is not None
        client = docker.from_env()
        self._container.reload()
        networks = self._container.attrs["NetworkSettings"]["Networks"]
        for name in list(networks):
            try:
                client.networks.get(name).disconnect(self._container, force=True)
            except docker.errors.DockerException as exc:  # pragma: no cover
                log.warning("could not disconnect network %s: %s", name, exc)

    async def destroy(self) -> None:
        if self._container is None:
            return
        await asyncio.to_thread(self._destroy_sync)
        self._container = None
        log.info("sandbox %s destroyed", self.run_id)

    def _destroy_sync(self) -> None:
        assert self._container is not None
        try:
            self._container.remove(force=True)
        except Exception as exc:  # noqa: BLE001 — cleanup must not mask a real error
            log.warning("could not remove sandbox container: %s", exc)

    async def __aenter__(self) -> Sandbox:
        await self.start()
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.destroy()

    # --- execution --------------------------------------------------------

    def _check_allowed(self, command: str) -> list[str]:
        """Validate an agent-chosen command and return its argv.

        **The security boundary is that `exec()` never invokes a shell** — the
        returned argv is passed to `execve` directly, so `;`, `&&`, backticks and
        redirects arrive as literal arguments to the program and cannot chain
        into a second command. The token checks below exist to give the model a
        clear error instead of a baffling one (`pytest: unrecognized argument
        ';'`), not to carry the safety guarantee.

        Parsing with shlex first means quoting is respected, so a legitimate
        `python -c "import os; print(os.getuid())"` is accepted — the `;` is
        inside a single argument, not a shell operator.
        """
        try:
            argv = shlex.split(command)
        except ValueError as exc:
            raise CommandNotAllowed(f"<unparseable: {exc}>", self.allowed) from exc
        if not argv:
            raise CommandNotAllowed("<empty>", self.allowed)

        operators = {";", "&&", "||", "|", "&", ">", ">>", "<"}
        if any(token in operators for token in argv[1:]) or argv[0].endswith(";"):
            raise CommandNotAllowed("<shell operator: run one command per call>", self.allowed)

        executable = Path(argv[0]).name
        if executable not in self.allowed:
            raise CommandNotAllowed(executable, self.allowed)
        return argv

    async def exec(self, command: str, *, timeout: int | None = None) -> ExecResult:
        """Run an agent-chosen command. Allowlist-checked, and shell-free."""
        argv = self._check_allowed(command)
        return await self._exec_argv(argv, display=command, timeout=timeout)

    async def _exec_argv(
        self, argv: list[str], *, display: str, timeout: int | None = None
    ) -> ExecResult:
        """Execute argv with no shell involved."""
        limit = timeout or self.settings.sandbox_command_timeout_s
        # `timeout` is the exec'd program, so no shell is needed to apply it.
        wrapped = ["timeout", "--signal=KILL", str(limit), *argv]
        return await self._run(wrapped, display=display)

    async def _exec_shell(self, command: str, *, timeout: int | None = None) -> ExecResult:
        """Run a command through `sh -c`, with no allowlist check.

        **Trusted callers only** — dependency install, snapshots, diffs, and
        tests. These are commands Feature Pilot composes itself and genuinely
        need shell features (`&&` chaining, redirects). Agent-chosen commands go
        through `exec()`, which never reaches a shell.
        """
        limit = timeout or self.settings.sandbox_command_timeout_s
        wrapped = ["timeout", "--signal=KILL", str(limit), "sh", "-c", command]
        return await self._run(wrapped, display=command)

    async def _run(self, argv: list[str], *, display: str) -> ExecResult:
        if self._container is None:
            raise SandboxError("sandbox is not started")
        started = time.perf_counter()
        exit_code, stdout, stderr = await asyncio.to_thread(self._exec_sync, argv)
        elapsed = int((time.perf_counter() - started) * 1000)
        return ExecResult(
            command=display,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=elapsed,
            # 137 = 128 + SIGKILL, which is how `timeout --signal=KILL` reports.
            timed_out=exit_code == 137,
        )

    def _exec_sync(self, argv: list[str]) -> tuple[int, str, str]:
        assert self._container is not None
        result: Any = self._container.exec_run(
            argv,
            workdir=self.workdir,
            demux=True,
            user=SANDBOX_USER,
        )
        out, err = result.output if isinstance(result.output, tuple) else (result.output, b"")
        return (
            int(result.exit_code or 0),
            (out or b"").decode("utf-8", "replace"),
            (err or b"").decode("utf-8", "replace"),
        )

    # --- snapshots --------------------------------------------------------

    def _git(self, args: str) -> str:
        return f"git --git-dir={self.GIT_DIR} --work-tree={self.workdir} {args}"

    async def snapshot(self) -> None:
        """Record the current worktree as the clean baseline."""
        setup = " && ".join(
            [
                f"git --git-dir={self.GIT_DIR} init --quiet",
                self._git("config user.email fp@localhost"),
                self._git("config user.name 'Feature Pilot'"),
                self._git("add -A"),
                self._git("commit --quiet --allow-empty -m baseline"),
            ]
        )
        result = await self._exec_shell(setup, timeout=120)
        if not result.ok:
            raise SandboxError(f"snapshot failed: {result.combined}")
        log.debug("sandbox %s snapshot taken", self.run_id)

    async def restore(self) -> None:
        """Discard all edits and return to the snapshot.

        Called before each repair attempt, so the coder starts from clean code
        rather than compounding a broken patch across attempts.
        """
        cmd = " && ".join([self._git("checkout -- ."), self._git("clean -fdq")])
        result = await self._exec_shell(cmd, timeout=120)
        if not result.ok:
            raise SandboxError(f"restore failed: {result.combined}")
        log.debug("sandbox %s restored to snapshot", self.run_id)

    async def restore_paths(self, paths: list[str]) -> None:
        """Restore specific paths to their snapshot state, leaving the rest alone.

        Used to verify a patch against the *original* tests: if the agent edited
        a test, reverting just the tests and re-running says whether the fix was
        real or whether the test was bent to fit it. That turns "did it cheat"
        from a judgement call into a measurement.
        """
        if not paths:
            return
        safe = [shlex.quote(self.resolve(p)) for p in paths]
        result = await self._exec_shell(f"{self._git('checkout -- ')}{' '.join(safe)}", timeout=120)
        if not result.ok:
            raise SandboxError(f"could not restore {paths}: {result.combined}")

    async def diff(self) -> str:
        """Unified diff of the worktree against the snapshot."""
        result = await self._exec_shell(
            f"{self._git('add -A --intent-to-add')} && {self._git('diff')}",
            timeout=120,
        )
        return result.stdout

    async def changed_files(self) -> list[str]:
        result = await self._exec_shell(self._git("status --porcelain"), timeout=60)
        files: list[str] = []
        for line in result.stdout.splitlines():
            if len(line) > 3:
                files.append(line[3:].strip().strip('"'))
        return sorted(set(files))
