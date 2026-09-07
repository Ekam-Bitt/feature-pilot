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

    from featurepilot.cli import main as cli_main

    monkeypatch.setattr(docker, "from_env", lambda *a, **k: _Reachable())
    monkeypatch.setattr(psycopg, "connect", lambda *a, **k: _Reachable())
    monkeypatch.setattr(redis, "from_url", lambda *a, **k: _Reachable())
    monkeypatch.setattr(cli_main, "ensure_gh_available", lambda **k: None)


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

    def test_reports_gh_for_publishing(self, stub_probes: None) -> None:
        assert "gh" in _run()

    def test_gh_absent_names_the_consequence(
        self, stub_probes: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from featurepilot.cli import main as cli_main
        from featurepilot.github.client import GhError

        def boom(**k: Any) -> None:
            raise GhError(["gh"], 127, "gh not found")

        monkeypatch.setattr(cli_main, "ensure_gh_available", boom)
        output = _run()
        assert "publish" in output.lower()

    def test_bedrock_row_appears_when_a_model_uses_it(
        self, stub_probes: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(
            "FP_MODEL_SUMMARIZER", "bedrock/us.anthropic.claude-haiku-4-5-20251001-v1:0"
        )
        assert "bedrock" in _run()

    def test_no_bedrock_row_without_bedrock_models(self, stub_probes: None) -> None:
        assert "bedrock" not in _run()

    def test_checks_every_datastore_a_run_touches(self, stub_probes: None) -> None:
        """A regression guard on omission rather than on wording: the failure this
        file exists for was a check that was never written, which no assertion
        about existing rows would have caught."""
        output = _run()
        for check in ("anthropic key", "docker", "postgres", "redis", "tracing"):
            assert check in output, f"doctor no longer checks {check}: {output}"


# --- solve with an issue URL -------------------------------------------------


class _StubTotals:
    model_calls = 0
    tool_calls = 0
    input_tokens = 0
    output_tokens = 0
    cost_usd = 0.0
    total_refs = 0
    nonexistent_ref_rate = 0.0


class _StubRegistry:
    def __len__(self) -> int:
        return 0

    def names(self) -> list[str]:
        return []


class _StubCtx:
    registry = _StubRegistry()

    class recorder:  # noqa: N801 - attribute namespace, not a class in spirit
        totals = _StubTotals()


class _StubHandle:
    def __init__(
        self, final: dict[str, Any], interrupts: list[dict[str, Any]] | None = None
    ) -> None:
        self.run_id = "run-stub"
        self.ctx = _StubCtx()
        self._final = final
        self._interrupts = list(interrupts or [])

    async def state(self) -> dict[str, Any]:
        return self._final

    async def pending_interrupt(self) -> dict[str, Any] | None:
        return self._interrupts.pop(0) if self._interrupts else None


@pytest.fixture
def url_flow(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> dict[str, Any]:
    """A solved run behind fakes: gh present, clone local, graph already DONE."""
    import contextlib
    from pathlib import Path

    from featurepilot.cli import main as cli_main
    from featurepilot.contracts import CoderOutput, PRSummary
    from featurepilot.github.clone import ClonedRepo
    from featurepilot.github.issues import FetchedIssue, IssueRef
    from featurepilot.github.publish import PublishResult
    from featurepilot.lifecycle import RunPhase

    ref = IssueRef("acme", "widget", 7)
    clone = ClonedRepo(
        path=Path(tmp_path) / "widget", ref=ref, head_sha="deadbeef", default_branch="main"
    )
    final = {
        "phase": RunPhase.DONE,
        "attempt": 1,
        "pr": PRSummary(title="Fix widget", body="Fixed.", test_plan="pytest"),
        "code": CoderOutput(edits=[], diff="--- a/x\n+++ b/x\n"),
    }
    calls: dict[str, Any] = {"publish": [], "clone": [], "fetch": []}

    monkeypatch.setattr(cli_main, "ensure_gh_available", lambda **k: None)

    def fake_fetch(r: IssueRef, **k: Any) -> FetchedIssue:
        calls["fetch"].append(r)
        return FetchedIssue(title="Fix widget", body="It broke", state="OPEN", url="u")

    monkeypatch.setattr(cli_main, "fetch_issue", fake_fetch)

    def fake_clone(r: IssueRef, dest: Path, **k: Any) -> ClonedRepo:
        calls["clone"].append((r, dest))
        return clone

    monkeypatch.setattr(cli_main, "shallow_clone", fake_clone)

    def fake_publish(c: ClonedRepo, diff: str, pr: Any, **k: Any) -> PublishResult:
        calls["publish"].append({"clone": c, "diff": diff, "pr": pr, **k})
        return PublishResult(
            pr_url="https://github.com/acme/widget/pull/9",
            branch="feature-pilot/issue-7-fix-widget",
            fork="user1/widget",
            reused_pr=False,
        )

    monkeypatch.setattr(cli_main, "publish_run", fake_publish)

    @contextlib.asynccontextmanager
    async def fake_open_run(*a: Any, **k: Any):
        yield _StubHandle(final)

    async def fake_stream_run(*a: Any, **k: Any):
        return
        yield  # pragma: no cover - makes this an async generator

    monkeypatch.setattr(cli_main, "open_run", fake_open_run)
    monkeypatch.setattr(cli_main, "stream_run", fake_stream_run)
    return calls


class TestSolveUrl:
    URL = "https://github.com/acme/widget/issues/7"

    def test_url_clones_solves_and_publishes_with_yes(self, url_flow: dict[str, Any]) -> None:
        result = runner.invoke(app, ["solve", self.URL, "--push", "--yes"])
        assert result.exit_code == 0, result.output
        assert len(url_flow["fetch"]) == 1
        assert len(url_flow["clone"]) == 1
        [publish] = url_flow["publish"]
        assert publish["diff"].startswith("---")
        assert "pull/9" in result.output

    def test_push_prompts_before_publishing(
        self, url_flow: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from rich.prompt import Prompt

        answers = iter(["n"])
        monkeypatch.setattr(Prompt, "ask", staticmethod(lambda *a, **k: next(answers)))
        result = runner.invoke(app, ["solve", self.URL, "--push"])
        assert result.exit_code == 0, result.output
        assert url_flow["publish"] == []  # declined at the gate

    def test_without_push_nothing_reaches_github(self, url_flow: dict[str, Any]) -> None:
        result = runner.invoke(app, ["solve", self.URL, "--yes"])
        assert result.exit_code == 0, result.output
        assert url_flow["publish"] == []

    def test_draft_flag_is_forwarded(self, url_flow: dict[str, Any]) -> None:
        runner.invoke(app, ["solve", self.URL, "--push", "--yes", "--draft"])
        [publish] = url_flow["publish"]
        assert publish["draft"] is True

    def test_push_with_a_local_repo_is_an_error(self, url_flow: dict[str, Any]) -> None:
        result = runner.invoke(app, ["solve", "--issue", "nope.md", "--push"])
        assert result.exit_code != 0


class TestUnattendedInterrupt:
    """A run that parks on open questions must not read a stdin that isn't there.

    `--yes` skips the gate only when the planner asked nothing (planner.py:91);
    with questions outstanding the graph still interrupts, and in CI stdin is
    /dev/null — rich's Prompt.ask raises EOFError on EOF, which turned a
    legitimate "needs a human" outcome into an unhandled traceback.
    """

    URL = "https://github.com/acme/widget/issues/7"
    QUESTIONS = {
        "kind": "plan_approval",
        "summary": "Two ways to read the spec.",
        "steps": [],
        "files": [],
        "open_questions": ["Should discounts stack?"],
        "confidence": "low",
    }

    @pytest.fixture
    def parked(self, url_flow: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
        import contextlib

        from featurepilot.cli import main as cli_main
        from featurepilot.lifecycle import RunPhase

        handle = _StubHandle(
            {"phase": RunPhase.WAITING_APPROVAL, "attempt": 1}, interrupts=[self.QUESTIONS]
        )

        @contextlib.asynccontextmanager
        async def fake_open_run(*a: Any, **k: Any):
            yield handle

        monkeypatch.setattr(cli_main, "open_run", fake_open_run)
        return url_flow

    def test_no_tty_never_prompts(
        self, parked: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The fix is to not reach for stdin at all. CliRunner supplies a fake
        stdin, so asserting on EOFError would pass for the wrong reason —
        assert instead that the prompt is never attempted."""
        from rich.prompt import Prompt

        def forbidden(*a: Any, **k: Any) -> str:
            raise AssertionError("prompted for input with no TTY attached")

        from featurepilot.cli import main as cli_main

        monkeypatch.setattr(cli_main, "_interactive", lambda: False)
        monkeypatch.setattr(Prompt, "ask", staticmethod(forbidden))

        result = runner.invoke(app, ["solve", self.URL, "--push", "--yes"])
        assert result.exit_code != 0
        assert not isinstance(result.exception, AssertionError), result.exception
        # The questions and the way forward both have to be on screen: a CI log
        # saying only "parked" tells the reader nothing actionable.
        assert "Should discounts stack?" in result.output
        assert "fpilot resume" in result.output
        assert parked["publish"] == []  # nothing published from an unfinished run

    def test_a_tty_still_prompts(
        self, parked: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from rich.prompt import Prompt

        from featurepilot.cli import main as cli_main

        asked: list[str] = []
        monkeypatch.setattr(cli_main, "_interactive", lambda: True)
        monkeypatch.setattr(
            Prompt, "ask", staticmethod(lambda *a, **k: asked.append(str(a[0])) or "n")
        )
        runner.invoke(app, ["solve", self.URL, "--yes"])
        assert asked  # the interactive path is untouched


class TestUnattendedPublishGate:
    """The publish gate has the same no-stdin hazard as the plan gate, and the
    safe default differs: never publish to GitHub unasked."""

    URL = "https://github.com/acme/widget/issues/7"

    def test_push_without_yes_refuses_rather_than_prompting(
        self, url_flow: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from rich.prompt import Prompt

        from featurepilot.cli import main as cli_main

        def forbidden(*a: Any, **k: Any) -> str:
            raise AssertionError("prompted for input with no TTY attached")

        monkeypatch.setattr(cli_main, "_interactive", lambda: False)
        monkeypatch.setattr(Prompt, "ask", staticmethod(forbidden))

        result = runner.invoke(app, ["solve", self.URL, "--push"])
        assert not isinstance(result.exception, AssertionError), result.exception
        assert url_flow["publish"] == []
        assert "--yes" in result.output  # names the flag that would allow it

    def test_push_with_yes_publishes_unattended(
        self, url_flow: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from featurepilot.cli import main as cli_main

        monkeypatch.setattr(cli_main, "_interactive", lambda: False)
        result = runner.invoke(app, ["solve", self.URL, "--push", "--yes"])
        assert result.exit_code == 0, result.output
        assert len(url_flow["publish"]) == 1
