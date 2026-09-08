"""What `deploy/scripts/self-deploy.sh` means by "already deployed".

A systemd timer runs the script every minute. The question it must answer is
whether the *running* API is on main's commit, not whether the checkout is.
The two part ways whenever the script itself dies after `git reset` and before
`systemctl restart`: HEAD then says "done" while the process still serves the
old code, and a gate that reads HEAD never tries again. That is what happened
on 2026-09-08, when removing a dependency extra made `uv sync` refuse a flag
the not-yet-updated script was still passing.

git is real here: a bare origin, a clone playing the host's checkout, and a
second clone that moves main. Everything else the script touches — uv, docker,
`sudo systemctl`, and the API behind curl — is a stub on PATH. The sudo stub's
`systemctl restart` writes the checkout's HEAD into the fake /health, which is
what a real restart does, so the loop closes the same way it does on the host.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from dataclasses import dataclass
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "scripts" / "self-deploy.sh"

pytestmark = pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bash", "git", "jq")),
    reason="the script is bash and shells out to git and jq",
)


@dataclass
class World:
    target: Path
    stubs: Path
    health: Path
    log: Path
    env: dict[str, str]
    old: str
    new: str

    def git(self, *args: str, cwd: Path) -> str:
        return _git(self.env, cwd, *args)

    def serve(self, sha: str) -> None:
        """Pretend the API process is serving `sha`."""
        self.health.write_text(json.dumps({"status": "ok", "commit": sha}))

    def serving(self) -> str | None:
        if not self.health.exists():
            return None
        return str(json.loads(self.health.read_text())["commit"])

    def calls(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def run(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["bash", str(SCRIPT)],
            env=self.env,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )


def _git(env: dict[str, str], cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _stub(path: Path, body: str) -> None:
    path.write_text("#!/usr/bin/env bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


@pytest.fixture
def world(tmp_path: Path) -> World:
    stubs = tmp_path / "bin"
    stubs.mkdir()
    health = tmp_path / "health.json"
    log = tmp_path / "calls.log"
    target = tmp_path / "target"

    env = {
        "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}",
        "HOME": str(tmp_path),
        # Whoever runs this suite must not leak signing, hooks, or identity in.
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "FP_TARGET": str(target),
        "FP_UV": str(stubs / "uv"),
        "FP_TEST_HEALTH": str(health),
        "FP_TEST_LOG": str(log),
    }

    origin = tmp_path / "origin.git"
    _git(env, tmp_path, "init", "--quiet", "--bare", "-b", "main", str(origin))

    author = tmp_path / "author"
    _git(env, tmp_path, "clone", "--quiet", str(origin), str(author))
    (author / "v").write_text("1\n")
    _git(env, author, "add", "v")
    _git(env, author, "commit", "--quiet", "-m", "one")
    _git(env, author, "push", "--quiet", "-u", "origin", "main")
    old = _git(env, author, "rev-parse", "HEAD")

    # The host cloned at the first commit ...
    _git(env, tmp_path, "clone", "--quiet", str(origin), str(target))

    # ... and then main moved on.
    (author / "v").write_text("2\n")
    _git(env, author, "commit", "--quiet", "-am", "two")
    _git(env, author, "push", "--quiet")
    new = _git(env, author, "rev-parse", "HEAD")

    _stub(stubs / "uv", 'printf "uv %s\\n" "$*" >> "$FP_TEST_LOG"\n')
    _stub(stubs / "docker", "exit 0\n")
    _stub(
        stubs / "sudo",
        """\
        printf "sudo %s\\n" "$*" >> "$FP_TEST_LOG"
        if [ "$*" = "systemctl restart featurepilot-api" ]; then
          sha=$(git -C "$FP_TARGET" rev-parse HEAD)
          printf '{"status":"ok","commit":"%s"}' "$sha" > "$FP_TEST_HEALTH"
        fi
        """,
    )
    _stub(
        stubs / "curl",
        """\
        # `curl -sf` on a connection refused: nothing on stdout, exit 7.
        [ -f "$FP_TEST_HEALTH" ] || exit 7
        cat "$FP_TEST_HEALTH"
        """,
    )

    return World(target, stubs, health, log, env, old, new)


RESTART = "sudo systemctl restart featurepilot-api"


def test_deploys_when_main_has_moved(world: World) -> None:
    world.serve(world.old)

    result = world.run()

    assert result.returncode == 0, result.stderr
    assert f"deploying {world.old[:9]} -> {world.new[:9]}" in result.stdout
    assert world.calls() == ["uv sync --frozen --quiet", RESTART]
    assert world.serving() == world.new


def test_leaves_a_host_that_is_serving_main_alone(world: World) -> None:
    world.git("fetch", "--quiet", "origin", cwd=world.target)
    world.git("reset", "--hard", "--quiet", "origin/main", cwd=world.target)
    world.serve(world.new)

    result = world.run()

    assert result.returncode == 0, result.stderr
    assert world.calls() == []


def test_redeploys_when_head_moved_but_the_process_did_not(world: World) -> None:
    """The 2026-09-08 shape: an earlier run reset the checkout and then died
    before the restart. HEAD equals origin/main; the process serves the commit
    before it. The next tick must finish the job, not declare it done."""
    world.git("fetch", "--quiet", "origin", cwd=world.target)
    world.git("reset", "--hard", "--quiet", "origin/main", cwd=world.target)
    world.serve(world.old)

    result = world.run()

    assert result.returncode == 0, result.stderr
    assert RESTART in world.calls()
    assert world.serving() == world.new


def test_treats_an_api_that_is_not_answering_as_not_deployed(world: World) -> None:
    """Nothing is listening, so nothing is on main. Bringing the host to main
    means starting it, whatever the checkout says."""
    world.git("fetch", "--quiet", "origin", cwd=world.target)
    world.git("reset", "--hard", "--quiet", "origin/main", cwd=world.target)
    assert world.serving() is None

    result = world.run()

    assert result.returncode == 0, result.stderr
    assert RESTART in world.calls()
    assert world.serving() == world.new
