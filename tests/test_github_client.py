"""The subprocess seam: git and gh invocations, token injection, failures."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from featurepilot.github import client
from featurepilot.github.client import GhError


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> Any:
    return subprocess.CompletedProcess(
        args=["fake"], returncode=returncode, stdout=stdout, stderr=stderr
    )


class TestRunGit:
    def test_returns_stdout_on_success(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[list[str]] = []

        def fake_run(argv: list[str], **kwargs: Any) -> Any:
            calls.append(argv)
            return _completed(stdout="abc123\n")

        monkeypatch.setattr(client.subprocess, "run", fake_run)
        out = client.run_git(Path("/repo"), "rev-parse", "HEAD")
        assert out == "abc123\n"
        assert calls == [["git", "-C", "/repo", "rev-parse", "HEAD"]]

    def test_raises_gherror_with_stderr_on_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            client.subprocess,
            "run",
            lambda *a, **k: _completed(returncode=128, stderr="fatal: not a git repository"),
        )
        with pytest.raises(GhError, match="not a git repository"):
            client.run_git(Path("/repo"), "status")


class TestRunGh:
    def test_injects_token_as_gh_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        seen_env: dict[str, str] = {}

        def fake_run(argv: list[str], **kwargs: Any) -> Any:
            seen_env.update(kwargs["env"])
            return _completed(stdout="ok")

        monkeypatch.setattr(client.subprocess, "run", fake_run)
        assert client.run_gh("auth", "status", token="tok-123") == "ok"
        assert seen_env["GH_TOKEN"] == "tok-123"

    def test_without_token_leaves_env_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        captured: dict[str, Any] = {}

        def fake_run(argv: list[str], **kwargs: Any) -> Any:
            captured["env"] = kwargs["env"]
            return _completed(stdout="ok")

        monkeypatch.setattr(client.subprocess, "run", fake_run)
        monkeypatch.delenv("GH_TOKEN", raising=False)
        client.run_gh("auth", "status")
        assert "GH_TOKEN" not in captured["env"]

    def test_raises_on_nonzero(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            client.subprocess,
            "run",
            lambda *a, **k: _completed(returncode=1, stderr="HTTP 404: Not Found"),
        )
        with pytest.raises(GhError, match="404"):
            client.run_gh("repo", "view", "nope/nope")


class TestGhJson:
    def test_parses_stdout(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            client.subprocess, "run", lambda *a, **k: _completed(stdout='{"title": "T"}')
        )
        assert client.gh_json("issue", "view", "1") == {"title": "T"}

    def test_invalid_json_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(client.subprocess, "run", lambda *a, **k: _completed(stdout="not json"))
        with pytest.raises(GhError, match="JSON"):
            client.gh_json("issue", "view", "1")


class TestEnsureGhAvailable:
    def test_missing_binary(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(client.shutil, "which", lambda name: None)
        with pytest.raises(GhError, match="install"):
            client.ensure_gh_available()

    def test_unauthenticated(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(client.shutil, "which", lambda name: "/usr/bin/gh")
        monkeypatch.setattr(
            client.subprocess,
            "run",
            lambda *a, **k: _completed(returncode=1, stderr="You are not logged in"),
        )
        with pytest.raises(GhError, match="not logged in"):
            client.ensure_gh_available()

    def test_ok(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(client.shutil, "which", lambda name: "/usr/bin/gh")
        monkeypatch.setattr(client.subprocess, "run", lambda *a, **k: _completed(stdout="ok"))
        client.ensure_gh_available()  # does not raise
