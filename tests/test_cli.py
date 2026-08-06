"""`doctor` — the command whose whole job is to not lie about readiness.

Every probe is stubbed, so this runs offline and in milliseconds rather than
waiting on three connection timeouts.

The redis rows are the reason this file exists. Redis carries the API's SSE
events and `manager.subscribe` returns `None` when it is down instead of raising,
so a stopped Redis produces an empty event stream and no error anywhere. `doctor`
omitted the check entirely and therefore reported a clean bill of health for a
setup whose streaming was silently dead.
"""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from featurepilot.cli.main import app

runner = CliRunner()


class _Unreachable:
    def __init__(self, *_: Any, **__: Any) -> None:
        raise OSError("connection refused")


class _Reachable:
    def __init__(self, *_: Any, **__: Any) -> None:
        pass

    def __enter__(self) -> _Reachable:
        return self

    def __exit__(self, *_: object) -> None:
        return None

    def ping(self) -> bool:
        return True

    def version(self) -> dict[str, str]:
        return {"Version": "test"}


@pytest.fixture
def stub_probes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Everything reachable by default; a test overrides the one it cares about."""
    import docker
    import psycopg
    import redis

    monkeypatch.setattr(docker, "from_env", lambda *a, **k: _Reachable())
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _Reachable())
    monkeypatch.setattr(redis, "from_url", lambda *a, **k: _Reachable())


def _run() -> str:
    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    # Rich wraps the table to the terminal width; joining lets assertions match
    # a row's text without depending on where it broke.
    return " ".join(result.output.split())


class TestDoctor:
    def test_reports_redis_when_it_answers(self, stub_probes: None) -> None:
        assert "redis" in _run()

    def test_reports_redis_absent_when_it_does_not(
        self, stub_probes: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import redis

        monkeypatch.setattr(redis, "from_url", _Unreachable)
        output = _run()
        assert "redis" in output
        # The consequence, not just the status: an operator who sees "absent"
        # without "SSE" has no idea what stopped working.
        assert "SSE" in output, f"redis failure must name what it breaks: {output}"

    def test_a_missing_datastore_is_not_fatal(
        self, stub_probes: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Doctor must survive every probe failing — it is the command you run
        precisely when things are broken."""
        import docker
        import psycopg
        import redis

        monkeypatch.setattr(docker, "from_env", _Unreachable)
        monkeypatch.setattr(psycopg, "connect", _Unreachable)
        monkeypatch.setattr(redis, "from_url", _Unreachable)

        output = _run()
        for check in ("docker", "postgres", "redis", "retriever"):
            assert check in output, f"missing {check} row: {output}"

    def test_checks_every_datastore_a_run_touches(self, stub_probes: None) -> None:
        """A regression guard on omission rather than on wording: the failure this
        file exists for was a check that was never written, which no assertion
        about existing rows would have caught."""
        output = _run()
        for check in ("anthropic key", "docker", "postgres", "redis", "tracing"):
            assert check in output, f"doctor no longer checks {check}: {output}"
