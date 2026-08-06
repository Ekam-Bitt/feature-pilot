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
        self.settings = type("S", (), {"redis_url": "redis://127.0.0.1:1/0"})()
        self.runs: dict[str, RunRecord] = {}
        self.started: list[tuple[str, str, bool]] = []
        self.approvals: list[HumanDecision] = []

    def get(self, run_id: str) -> RunRecord | None:
        return self.runs.get(run_id)

    def list(self) -> list[dict[str, Any]]:
        return [r.public() for r in self.runs.values()]

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
    # Bypass lifespan so no real RunManager (and no Docker) is constructed.
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
