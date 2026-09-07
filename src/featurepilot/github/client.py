"""The one subprocess seam for git and gh.

Everything in this package (and eval's harnesses) shells out through these
functions, so a test fakes exactly one module — `client.subprocess.run` — and
the rest of the package is exercised for real.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any


class GhError(RuntimeError):
    """A git or gh invocation failed; carries the command and its stderr."""

    def __init__(self, argv: list[str], returncode: int, stderr: str) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{' '.join(argv)} exited {returncode}: {stderr.strip()}")


#: Written once per process, not per call: git invokes it as a subprocess, so
#: it has to be a real file on disk that outlives the call.
_ASKPASS_CACHE: dict[str, str] = {}


def _askpass_for(token: str) -> str:
    """Path to a helper that prints `token`, for git to authenticate with.

    Out of band on purpose. A token in the remote URL ends up in `git remote
    -v`, and a token in a command-line flag ends up in GhError's message —
    which is returned to API clients. Neither is a place for a credential.
    """
    cached = _ASKPASS_CACHE.get(token)
    if cached and Path(cached).exists():
        return cached
    with tempfile.NamedTemporaryFile(
        "w", prefix="fp-askpass-", suffix=".sh", delete=False
    ) as handle:
        # Any argument: git asks for a username first, then a password, and the
        # token answers both (GitHub ignores the username for token auth).
        handle.write(f"#!/bin/sh\nprintf %s {shlex.quote(token)}\n")
    path = Path(handle.name)
    path.chmod(0o700)
    _ASKPASS_CACHE[token] = str(path)
    return str(path)


def run_git(cwd: Path, *args: str, token: str | None = None) -> str:
    """Run git in `cwd`; return stdout, raise GhError on nonzero exit.

    With a token, git authenticates through GIT_ASKPASS rather than whatever
    credential helper the machine happens to have configured. A developer's
    laptop has one; a freshly provisioned server does not, and a push there
    fails with "could not read Username" instead.
    """
    argv = ["git", "-C", str(cwd), *args]
    env: dict[str, str] | None = None
    if token:
        env = dict(os.environ)
        env["GIT_ASKPASS"] = _askpass_for(token)
        # Never block waiting for a human that isn't there.
        env["GIT_TERMINAL_PROMPT"] = "0"
    result = subprocess.run(  # noqa: S603
        argv, capture_output=True, text=True, check=False, env=env
    )
    if result.returncode != 0:
        raise GhError(argv, result.returncode, result.stderr)
    return result.stdout


def run_gh(*args: str, token: str | None = None) -> str:
    """Run gh; return stdout, raise GhError on nonzero exit.

    A token is injected as GH_TOKEN for this invocation only — gh prefers it
    over ambient `gh auth` state, so the configured credential always wins
    without touching the caller's environment.
    """
    argv = ["gh", *args]
    env = dict(os.environ)
    if token:
        env["GH_TOKEN"] = token
    result = subprocess.run(  # noqa: S603
        argv, capture_output=True, text=True, check=False, env=env
    )
    if result.returncode != 0:
        raise GhError(argv, result.returncode, result.stderr)
    return result.stdout


def gh_json(*args: str, token: str | None = None) -> Any:
    """run_gh + json.loads; a malformed payload is an error, never {}."""
    out = run_gh(*args, token=token)
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        raise GhError(["gh", *args], 0, f"gh returned invalid JSON: {exc}") from exc


def ensure_gh_available(token: str | None = None) -> None:
    """Fail fast — before any tokens are spent — if gh can't publish.

    Raises GhError with an install hint when the binary is missing, or with
    gh's own message when authentication is absent.
    """
    if shutil.which("gh") is None:
        raise GhError(
            ["gh"], 127, "gh not found — install it (https://cli.github.com) to publish PRs"
        )
    run_gh("auth", "status", token=token)
