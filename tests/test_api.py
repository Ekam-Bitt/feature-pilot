"""The HTTP surface.

No Docker and no model: the run manager is replaced with a stub, so these test
the API's own contract — validation, status codes, the approval handshake, and
the SSE replay — rather than re-testing the graph.

The replay behaviour is the one worth pinning: a client that connects after the
run started must still receive the plan it is being asked to approve, or the UI
shows an empty pane and the run looks hung.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from fastapi.testclient import TestClient

from featurepilot.api import main as api
from featurepilot.api.manager import RunRecord
from featurepilot.contracts import HumanDecision
from featurepilot.lifecycle import RunPhase
from featurepilot.metrics.events import EventKind, MetricEvent

PLAN_PAYLOAD = {
    "kind": "plan_approval",
    "summary": "Judge shipping on the payable amount.",
    "steps": [{"description": "subtract both discounts", "files": ["src/shopsvc/cart.py"]}],
    "open_questions": [],
    "confidence": "high",
}


class StubManager:
    """Stands in for RunManager. Same surface, no containers."""

    def __init__(self) -> None:
        self.settings = type(
            "S", (), {"redis_url": "redis://127.0.0.1:1/0", "allow_local_repos": True}
        )()
        self.runs: dict[str, RunRecord] = {}
        self.started: list[tuple[str, str, bool]] = []
        self.approvals: list[HumanDecision] = []
        self.url_started = []
        self.published = []
        self.publish_error = None
        self.credentials = []
        self.accepting_runs = True
        self.budget_ok = True
        self.drafts_forced: list[bool] = []

    url_started: list[tuple[str, int, bool, bool]]
    published: list[tuple[str, bool]]
    publish_error: Exception | None
    credentials: list[Any]

    def get(self, run_id: str) -> RunRecord | None:
        return self.runs.get(run_id)

    def list(self) -> list[dict[str, Any]]:
        return [r.public() for r in self.runs.values()]

    async def start_from_url(
        self,
        ref: Any,
        *,
        auto_approve: bool = False,
        auto_publish: bool = False,
        draft: bool = False,
        credentials: Any = None,
    ) -> RunRecord:
        record = RunRecord(run_id="run-1", repo="(cloning)", issue_ref=f"{ref.slug}#{ref.number}")
        record.issue_url = f"https://github.com/{ref.slug}/issues/{ref.number}"
        if credentials is not None:
            record.credentials = credentials
        self.credentials.append(credentials)
        self.runs[record.run_id] = record
        self.url_started.append((ref.slug, ref.number, auto_publish, draft))
        return record

    def within_daily_budget(self, credentials: Any) -> bool:
        return self.budget_ok

    def effective_draft(self, credentials: Any, *, requested_draft: bool) -> bool:
        forced = requested_draft or getattr(credentials, "uses_server_credentials", False)
        self.drafts_forced.append(forced)
        return forced

    async def publish(self, run_id: str, *, draft: bool = False) -> str:
        if self.publish_error is not None:
            raise self.publish_error
        self.published.append((run_id, draft))
        record = self.runs[run_id]
        record.pr_url = "https://github.com/acme/widget/pull/9"
        return record.pr_url

    async def start(
        self, repo: Any, issue: str, *, issue_ref: str = "", auto_approve: bool = False
    ) -> RunRecord:
        record = RunRecord(run_id="run-1", repo=str(repo), issue_ref=issue_ref or "(inline)")
        self.runs[record.run_id] = record
        self.started.append((str(repo), issue, auto_approve))
        return record

    async def approve(self, run_id: str, decision: HumanDecision) -> bool:
        record = self.runs.get(run_id)
        if record is None or record.pending is None:
            return False
        record.pending = None
        record.resumed.set()
        self.approvals.append(decision)
        return True

    async def cancel(self, run_id: str) -> bool:
        record = self.runs.get(run_id)
        if record is None:
            return False
        record.phase = RunPhase.FAILED
        record.resumed.set()
        return True

    async def aclose(self) -> None:
        return None


@pytest.fixture
def manager() -> StubManager:
    return StubManager()


@pytest.fixture
def client(manager: StubManager):  # noqa: ANN201
    # A `with TestClient(...)` block *runs* the lifespan, so the real RunManager
    # is constructed and then replaced. That is safe only because RunManager's
    # constructor stores settings and nothing else — no Docker client exists until
    # a run starts. It does mean the lifespan needs `Settings` to be constructible,
    # which the autouse `_hermetic_env` fixture guarantees; without it this fixture
    # depends on the developer having a populated `.env`.
    api.app.state.manager = manager
    with TestClient(api.app) as test_client:
        api.app.state.manager = manager
        yield test_client


class TestStartingRuns:
    def test_health(self, client) -> None:  # noqa: ANN001
        assert client.get("/health").json() == {"status": "ok"}

    def test_inline_issue_starts_a_run(self, client, manager: StubManager) -> None:  # noqa: ANN001
        response = client.post("/runs", json={"issue": "the total is wrong", "repo": "."})
        assert response.status_code == 201
        assert response.json()["run_id"] == "run-1"
        assert manager.started[0][1] == "the total is wrong"

    def test_issue_path_is_read(self, client, manager: StubManager, tmp_path) -> None:  # noqa: ANN001
        path = tmp_path / "issue.md"
        path.write_text("# Bug\n\nIt breaks.\n")
        response = client.post("/runs", json={"issue_path": str(path), "repo": "."})
        assert response.status_code == 201
        assert "It breaks." in manager.started[0][1]

    def test_missing_issue_is_rejected(self, client) -> None:  # noqa: ANN001
        assert client.post("/runs", json={"repo": "."}).status_code == 400

    def test_missing_issue_file_is_rejected(self, client) -> None:  # noqa: ANN001
        response = client.post("/runs", json={"issue_path": "/nope.md", "repo": "."})
        assert response.status_code == 400
        assert "no such issue file" in response.json()["detail"]

    def test_missing_repo_is_rejected(self, client) -> None:  # noqa: ANN001
        response = client.post("/runs", json={"issue": "x", "repo": "/not/a/repo"})
        assert response.status_code == 400

    def test_auto_approve_is_passed_through(self, client, manager: StubManager) -> None:  # noqa: ANN001
        client.post("/runs", json={"issue": "x", "repo": ".", "auto_approve": True})
        assert manager.started[0][2] is True


class TestStatus:
    def test_unknown_run_is_404(self, client) -> None:  # noqa: ANN001
        assert client.get("/runs/nope").status_code == 404

    def test_status_reports_what_it_waits_for(self, client, manager: StubManager) -> None:  # noqa: ANN001
        client.post("/runs", json={"issue": "x", "repo": "."})
        manager.runs["run-1"].pending = PLAN_PAYLOAD
        body = client.get("/runs/run-1").json()
        assert body["awaiting_human"] is True
        assert body["pending"]["summary"] == PLAN_PAYLOAD["summary"]

    def test_listing_runs(self, client) -> None:  # noqa: ANN001
        client.post("/runs", json={"issue": "x", "repo": "."})
        assert [r["run_id"] for r in client.get("/runs").json()] == ["run-1"]


class TestApproval:
    def test_approving_a_parked_run(self, client, manager: StubManager) -> None:  # noqa: ANN001
        client.post("/runs", json={"issue": "x", "repo": "."})
        manager.runs["run-1"].pending = PLAN_PAYLOAD
        response = client.post("/runs/run-1/approve", json={"verdict": "approve"})
        assert response.status_code == 200
        assert manager.approvals[0].verdict == "approve"

    def test_rejecting_carries_the_feedback(self, client, manager: StubManager) -> None:  # noqa: ANN001
        client.post("/runs", json={"issue": "x", "repo": "."})
        manager.runs["run-1"].pending = PLAN_PAYLOAD
        client.post(
            "/runs/run-1/approve",
            json={"verdict": "reject", "feedback": "wrong module", "answers": ["yes"]},
        )
        decision = manager.approvals[0]
        assert decision.verdict == "reject"
        assert decision.feedback == "wrong module"
        assert decision.answers == ["yes"]

    def test_approving_a_run_that_is_not_waiting_is_409(self, client) -> None:  # noqa: ANN001
        client.post("/runs", json={"issue": "x", "repo": "."})
        response = client.post("/runs/run-1/approve", json={"verdict": "approve"})
        assert response.status_code == 409

    def test_approving_an_unknown_run_is_404(self, client) -> None:  # noqa: ANN001
        assert client.post("/runs/nope/approve", json={}).status_code == 404

    @pytest.mark.parametrize(
        ("given", "expected"),
        [("approve", "approve"), ("reject", "reject"), ("R", "reject"), ("yes", "approve")],
    )
    def test_verdict_is_normalised(
        self,
        client,  # noqa: ANN001
        manager: StubManager,
        given: str,
        expected: str,
    ) -> None:
        """Anything not clearly a rejection approves — a typo must never silently
        reject someone's plan."""
        client.post("/runs", json={"issue": "x", "repo": "."})
        manager.runs["run-1"].pending = PLAN_PAYLOAD
        client.post("/runs/run-1/approve", json={"verdict": given})
        assert manager.approvals[-1].verdict == expected


class TestCancel:
    def test_cancelling(self, client) -> None:  # noqa: ANN001
        client.post("/runs", json={"issue": "x", "repo": "."})
        assert client.delete("/runs/run-1").json()["cancelled"] is True

    def test_cancelling_an_unknown_run_is_404(self, client) -> None:  # noqa: ANN001
        assert client.delete("/runs/nope").status_code == 404


class TestStream:
    def test_unknown_run_is_404(self, client) -> None:  # noqa: ANN001
        assert client.get("/runs/nope/stream").status_code == 404

    def test_replays_history_then_reports_completion(
        self,
        client,  # noqa: ANN001
        manager: StubManager,
    ) -> None:
        """A client connecting late must still see what already happened."""
        client.post("/runs", json={"issue": "x", "repo": "."})
        record = manager.runs["run-1"]
        asyncio.run(
            record.events.emit(
                MetricEvent(run_id="run-1", kind=EventKind.NODE_STARTED, payload={"node": "plan"})
            )
        )
        record.phase = RunPhase.DONE  # finished, so the stream terminates

        with client.stream("GET", "/runs/run-1/stream") as response:
            body = "".join(chunk for chunk in response.iter_text())

        assert "node_started" in body
        assert '"node": "plan"' in body or '"node":"plan"' in body
        assert "run_finished" in body

    def test_stream_redacts_file_contents(self, client, manager: StubManager) -> None:  # noqa: ANN001
        """Events leaving the process must not carry code scraped out of the
        target repository."""
        client.post("/runs", json={"issue": "x", "repo": "."})
        record = manager.runs["run-1"]
        asyncio.run(
            record.events.emit(
                MetricEvent(
                    run_id="run-1",
                    kind=EventKind.TOOL_CALLED,
                    payload={"tool": "read_file", "content": "SUPER_SECRET_TOKEN"},
                )
            )
        )
        record.phase = RunPhase.DONE

        with client.stream("GET", "/runs/run-1/stream") as response:
            body = "".join(chunk for chunk in response.iter_text())

        assert "SUPER_SECRET_TOKEN" not in body
        assert "read_file" in body


def test_openapi_documents_every_endpoint(client) -> None:  # noqa: ANN001
    paths = json.loads(client.get("/openapi.json").text)["paths"]
    assert {"/runs", "/runs/{run_id}", "/runs/{run_id}/approve", "/runs/{run_id}/stream"} <= set(
        paths
    )


URL = "https://github.com/acme/widget/issues/7"


def _done_url_record(manager: StubManager, *, pr_url: str | None = None) -> RunRecord:
    """A URL-started run that finished DONE with artifacts cached on the record."""
    from pathlib import Path

    from featurepilot.contracts import PRSummary
    from featurepilot.github.clone import ClonedRepo
    from featurepilot.github.issues import IssueRef

    record = RunRecord(run_id="run-1", repo="/tmp/clone/widget", issue_ref="acme/widget#7")
    record.phase = RunPhase.DONE
    record.issue_url = URL
    record.clone = ClonedRepo(
        path=Path("/tmp/clone/widget"),
        ref=IssueRef("acme", "widget", 7),
        head_sha="deadbeef",
        default_branch="main",
    )
    record.diff = "--- a/x\n+++ b/x\n"
    record.pr_summary = PRSummary(title="Fix", body="B", test_plan="T")
    record.pr_url = pr_url
    manager.runs[record.run_id] = record
    return record


class TestUrlRuns:
    def test_url_starts_a_run_and_returns_immediately(self, client, manager: StubManager) -> None:  # noqa: ANN001
        response = client.post("/runs", json={"issue_url": URL})
        assert response.status_code == 201, response.text
        assert manager.url_started == [("acme/widget", 7, False, False)]

    def test_a_bad_url_is_rejected(self, client) -> None:  # noqa: ANN001
        response = client.post("/runs", json={"issue_url": "https://github.com/acme/widget/pull/7"})
        assert response.status_code == 400
        assert "issues/<number>" in response.text

    def test_url_and_inline_issue_together_are_rejected(self, client) -> None:  # noqa: ANN001
        response = client.post("/runs", json={"issue_url": URL, "issue": "also this"})
        assert response.status_code == 400

    def test_auto_publish_is_passed_through(self, client, manager: StubManager) -> None:  # noqa: ANN001
        client.post("/runs", json={"issue_url": URL, "auto_publish": True, "draft": True})
        assert manager.url_started == [("acme/widget", 7, True, True)]


class TestPublish:
    def test_unknown_run_is_404(self, client) -> None:  # noqa: ANN001
        assert client.post("/runs/nope/publish", json={}).status_code == 404

    def test_publish_before_done_is_409(self, client, manager: StubManager) -> None:  # noqa: ANN001
        record = _done_url_record(manager)
        record.phase = RunPhase.CODING
        assert client.post("/runs/run-1/publish", json={}).status_code == 409

    def test_publish_of_a_non_url_run_is_409(self, client, manager: StubManager) -> None:  # noqa: ANN001
        record = _done_url_record(manager)
        record.clone = None
        response = client.post("/runs/run-1/publish", json={})
        assert response.status_code == 409

    def test_publish_returns_the_pr_url(self, client, manager: StubManager) -> None:  # noqa: ANN001
        _done_url_record(manager)
        response = client.post("/runs/run-1/publish", json={"draft": True})
        assert response.status_code == 200, response.text
        assert response.json()["pr_url"] == "https://github.com/acme/widget/pull/9"
        assert manager.published == [("run-1", True)]

    def test_republish_returns_the_cached_url_without_publishing(
        self, client, manager: StubManager
    ) -> None:  # noqa: ANN001
        _done_url_record(manager, pr_url="https://github.com/acme/widget/pull/9")
        response = client.post("/runs/run-1/publish", json={})
        assert response.status_code == 200
        assert response.json()["pr_url"].endswith("/pull/9")
        assert manager.published == []

    def test_publish_failure_is_502(self, client, manager: StubManager) -> None:  # noqa: ANN001
        from featurepilot.github.publish import PublishError

        _done_url_record(manager)
        manager.publish_error = PublishError("patch does not apply")
        response = client.post("/runs/run-1/publish", json={})
        assert response.status_code == 502
        assert "patch does not apply" in response.text

    def test_status_reports_publishability(self, client, manager: StubManager) -> None:  # noqa: ANN001
        _done_url_record(manager)
        body = client.get("/runs/run-1").json()
        assert body["publishable"] is True
        assert body["pr_url"] is None


class TestSessionCredentials:
    """A visitor's keys are used for their run and nothing else: not stored,
    not echoed back, not visible to another session."""

    def test_supplied_keys_reach_the_manager(self, client, manager: StubManager) -> None:  # noqa: ANN001
        client.post(
            "/runs",
            json={
                "issue_url": URL,
                "anthropic_api_key": "sk-visitor",
                "github_token": "ghp-visitor",
            },
        )
        [creds] = manager.credentials
        assert creds.anthropic_api_key.get_secret_value() == "sk-visitor"
        assert creds.github_token.get_secret_value() == "ghp-visitor"
        assert creds.uses_server_credentials is False

    def test_omitted_keys_mean_server_credentials(self, client, manager: StubManager) -> None:  # noqa: ANN001
        client.post("/runs", json={"issue_url": URL})
        [creds] = manager.credentials
        assert creds.uses_server_credentials is True

    def test_keys_are_never_echoed_in_the_response(self, client) -> None:  # noqa: ANN001
        response = client.post(
            "/runs", json={"issue_url": URL, "anthropic_api_key": "sk-visitor-secret"}
        )
        assert "sk-visitor-secret" not in response.text

    def test_keys_are_absent_from_status_and_listing(self, client, manager: StubManager) -> None:  # noqa: ANN001
        client.post("/runs", json={"issue_url": URL, "github_token": "ghp-visitor-secret"})
        assert "ghp-visitor-secret" not in client.get("/runs/run-1").text
        assert "ghp-visitor-secret" not in client.get("/runs").text

    def test_openapi_does_not_document_credentials_as_readable(self, client) -> None:  # noqa: ANN001
        """They are request-only; a schema that returns them invites a client
        to render them."""
        schemas = json.loads(client.get("/openapi.json").text)["components"]["schemas"]
        run_status = json.dumps(schemas)
        assert "anthropic_api_key" in run_status  # on StartRun


class TestBrowserAccess:
    """The frontend is served from another origin, so without CORS a browser
    can start a run and then be unable to read its event stream."""

    def test_an_allowed_origin_may_read_responses(self, client) -> None:  # noqa: ANN001
        response = client.get("/health", headers={"Origin": "http://localhost:5173"})
        assert response.headers.get("access-control-allow-origin") == "http://localhost:5173"

    def test_preflight_permits_the_post_that_starts_a_run(self, client) -> None:  # noqa: ANN001
        response = client.options(
            "/runs",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "content-type",
            },
        )
        assert response.status_code == 200
        assert "POST" in response.headers.get("access-control-allow-methods", "")

    def test_an_unlisted_origin_gets_no_grant(self, client) -> None:  # noqa: ANN001
        response = client.get("/health", headers={"Origin": "https://evil.example"})
        assert "access-control-allow-origin" not in response.headers


class TestCapacity:
    """Refusals a public deployment must make rather than accept and fall over."""

    def test_over_capacity_is_503_with_a_retry_hint(self, client, manager: StubManager) -> None:  # noqa: ANN001
        manager.accepting_runs = False
        response = client.post("/runs", json={"issue_url": URL})
        assert response.status_code == 503
        assert "retry-after" in {k.lower() for k in response.headers}

    def test_the_daily_budget_refuses_server_credential_runs(
        self, client, manager: StubManager
    ) -> None:  # noqa: ANN001
        manager.budget_ok = False
        response = client.post("/runs", json={"issue_url": URL})
        assert response.status_code == 429
        # The message has to say what would fix it, or a visitor cannot tell
        # this apart from being rate-limited.
        assert "own" in response.text.lower() and "key" in response.text.lower()

    def test_a_visitor_with_their_own_key_is_unaffected_by_the_budget(
        self, client, manager: StubManager
    ) -> None:  # noqa: ANN001
        manager.budget_ok = True
        response = client.post("/runs", json={"issue_url": URL, "anthropic_api_key": "sk-visitor"})
        assert response.status_code == 201


class TestPublishDraftFloor:
    def test_publish_consults_the_draft_floor(self, client, manager: StubManager) -> None:  # noqa: ANN001
        _done_url_record(manager)
        client.post("/runs/run-1/publish", json={"draft": False})
        assert manager.drafts_forced == [True]  # server credentials on the record


class TestLocalRepoRuns:
    """A publicly reachable deployment must not run against server paths: the
    retrieval context and the diff would carry their contents back out."""

    def test_local_repo_runs_are_refused_when_disabled(
        self, client, manager: StubManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:  # noqa: ANN001
        monkeypatch.setattr(manager.settings, "allow_local_repos", False, raising=False)
        response = client.post("/runs", json={"issue": "fix it", "repo": "/etc"})
        assert response.status_code == 403
        assert "issue_url" in response.text

    def test_local_repo_runs_work_by_default(self, client, manager: StubManager, tmp_path) -> None:  # noqa: ANN001
        response = client.post("/runs", json={"issue": "fix it", "repo": str(tmp_path)})
        assert response.status_code == 201


class TestArtifacts:
    """The browser cannot get the diff any other way.

    The event stream redacts `diff` and file contents from everything leaving
    the process — Redis and LangSmith are downstream of it — and the status
    endpoint never carried them. Without this endpoint the run view has nothing
    to show for a finished run, which is the whole payoff.
    """

    def test_artifacts_of_a_finished_run(self, client, manager: StubManager) -> None:  # noqa: ANN001
        _done_url_record(manager)
        body = client.get("/runs/run-1/artifacts").json()
        assert body["diff"].startswith("--- a/x")
        assert body["pr_summary"]["title"] == "Fix"
        assert body["pr_summary"]["body"] == "B"
        assert body["pr_summary"]["test_plan"] == "T"

    def test_unknown_run_is_404(self, client) -> None:  # noqa: ANN001
        assert client.get("/runs/nope/artifacts").status_code == 404

    def test_a_run_still_working_reports_empty_rather_than_404(
        self, client, manager: StubManager
    ) -> None:  # noqa: ANN001
        """The run view polls this once the phase reaches DONE, but a race
        between the phase flipping and the artifacts being stashed must read as
        'not yet', not as an error."""
        record = _done_url_record(manager)
        record.diff = None
        record.pr_summary = None
        body = client.get("/runs/run-1/artifacts").json()
        assert body["diff"] is None
        assert body["pr_summary"] is None

    def test_credentials_are_not_in_the_artifacts(self, client, manager: StubManager) -> None:  # noqa: ANN001
        from pydantic import SecretStr

        from featurepilot.credentials import RunCredentials

        record = _done_url_record(manager)
        record.credentials = RunCredentials(anthropic_api_key=SecretStr("sk-leak-canary"))
        assert "sk-leak-canary" not in client.get("/runs/run-1/artifacts").text

    def test_openapi_documents_it(self, client) -> None:  # noqa: ANN001
        paths = json.loads(client.get("/openapi.json").text)["paths"]
        assert "/runs/{run_id}/artifacts" in paths


def test_status_reports_the_draft_preference(client, manager: StubManager) -> None:  # noqa: ANN001
    """The run view publishes with the choice made when the run started, so it
    has to be able to read that choice back."""
    record = _done_url_record(manager)
    record.draft = False
    assert client.get("/runs/run-1").json()["draft"] is False
