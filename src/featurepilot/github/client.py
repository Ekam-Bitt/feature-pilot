"""The one subprocess seam for git and gh.

Everything in this package (and eval's harnesses) shells out through these
functions, so a test fakes exactly one module — `client.subprocess.run` — and
the rest of the package is exercised for real.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any


class GhError(RuntimeError):
    """A git or gh invocation failed; carries the command and its stderr."""

    def __init__(self, argv: list[str], returncode: int, stderr: str) -> None:
        self.argv = argv
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(f"{' '.join(argv)} exited {returncode}: {stderr.strip()}")


def run_git(cwd: Path, *args: str) -> str:
    """Run git in `cwd`; return stdout, raise GhError on nonzero exit."""
    argv = ["git", "-C", str(cwd), *args]
    result = subprocess.run(argv, capture_output=True, text=True, check=False)  # noqa: S603
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
