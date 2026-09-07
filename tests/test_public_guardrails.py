"""What keeps a publicly-reachable deployment from falling over or overspending.

Each run holds a 2 GB sandbox, so concurrency is a memory ceiling; runs using
the server's own key spend the operator's money, so they need a daily ceiling;
and a run publishing under the operator's identity should not be able to open a
non-draft PR on a stranger's repository.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from featurepilot.api.manager import RunManager
from featurepilot.config import Settings
from featurepilot.credentials import RunCredentials

SERVER_CREDS = RunCredentials()
VISITOR_CREDS = RunCredentials(anthropic_api_key=SecretStr("sk-visitor"))


def _manager(**overrides: object) -> RunManager:
    settings = Settings(
        anthropic_api_key=SecretStr("sk-server"),
        github_token=SecretStr("ghp-server"),
        _env_file=None,
        **overrides,
    )  # type: ignore[call-arg]
    return RunManager(settings)


class TestConcurrency:
    def test_admission_is_capped_by_settings(self) -> None:
        manager = _manager(max_concurrent_runs=2)
        assert manager.slots_available == 2
        manager.reserve_slot()
        manager.reserve_slot()
        assert manager.slots_available == 0

    def test_releasing_returns_the_slot(self) -> None:
        manager = _manager(max_concurrent_runs=1)
        manager.reserve_slot()
        manager.release_slot()
        assert manager.slots_available == 1

    def test_a_full_queue_is_refused_rather_than_grown(self) -> None:
        manager = _manager(max_concurrent_runs=1, max_queued_runs=2)
        for _ in range(3):  # 1 running + 2 queued
            manager.reserve_slot()
        assert manager.accepting_runs is False


class TestDailySpendCap:
    def test_server_credential_spend_counts_against_the_cap(self) -> None:
        manager = _manager(max_usd_per_day=1.00)
        manager.record_spend(0.60, credentials=SERVER_CREDS)
        assert manager.within_daily_budget(SERVER_CREDS) is True
        manager.record_spend(0.50, credentials=SERVER_CREDS)
        assert manager.within_daily_budget(SERVER_CREDS) is False

    def test_a_visitors_own_key_is_not_counted(self) -> None:
        manager = _manager(max_usd_per_day=1.00)
        manager.record_spend(5.00, credentials=VISITOR_CREDS)
        assert manager.within_daily_budget(VISITOR_CREDS) is True
        # ...and it did not consume the operator's allowance either
        assert manager.within_daily_budget(SERVER_CREDS) is True

    def test_the_cap_resets_on_a_new_day(self) -> None:
        manager = _manager(max_usd_per_day=1.00)
        manager.record_spend(2.00, credentials=SERVER_CREDS)
        assert manager.within_daily_budget(SERVER_CREDS) is False
        manager._spend_day = manager._spend_day.replace(year=manager._spend_day.year - 1)
        assert manager.within_daily_budget(SERVER_CREDS) is True


class TestServerCredentialPublishing:
    """With the operator's PAT, a stranger's run authors a PR as the operator.
    Draft is then not a preference but the safe floor."""

    def test_server_credentials_force_a_draft(self) -> None:
        manager = _manager()
        assert manager.effective_draft(SERVER_CREDS, requested_draft=False) is True

    def test_a_visitor_with_their_own_token_may_choose(self) -> None:
        manager = _manager()
        creds = RunCredentials(github_token=SecretStr("ghp-visitor"))
        assert manager.effective_draft(creds, requested_draft=False) is False
        assert manager.effective_draft(creds, requested_draft=True) is True


class TestSlotLifecycle:
    """A slot must be held for exactly as long as the run lives, and returned
    on every exit path — a slot leaked by a crashed run permanently lowers the
    host's capacity."""

    @pytest.fixture
    def stubs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from featurepilot.api import manager as manager_module
        from featurepilot.github.clone import ClonedRepo
        from featurepilot.github.issues import FetchedIssue

        monkeypatch.setattr(
            manager_module,
            "fetch_issue",
            lambda r, **k: FetchedIssue(title="t", body="b", state="OPEN", url="u"),
        )
        monkeypatch.setattr(
            manager_module,
            "shallow_clone",
            lambda r, dest, **k: ClonedRepo(dest, r, "sha", "main"),
        )

    async def test_the_slot_is_held_while_running_and_returned_after(
        self, stubs: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        from featurepilot.github.issues import IssueRef
        from featurepilot.lifecycle import RunPhase

        manager = _manager(max_concurrent_runs=1)
        holding = asyncio.Event()
        release = asyncio.Event()

        async def fake_drive(record: object, *a: object, **k: object) -> None:
            holding.set()
            await release.wait()
            record.phase = RunPhase.DONE  # type: ignore[attr-defined]

        monkeypatch.setattr(manager, "_drive", fake_drive)

        record = await manager.start_from_url(IssueRef("acme", "widget", 7))
        await asyncio.wait_for(holding.wait(), timeout=2)

        assert manager.slots_available == 0, "a running run must occupy its slot"

        release.set()
        assert record.task is not None
        await record.task
        assert manager.slots_available == 1

    async def test_a_crashed_ingestion_returns_its_slot(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from featurepilot.api import manager as manager_module
        from featurepilot.github.client import GhError
        from featurepilot.github.issues import IssueRef

        manager = _manager(max_concurrent_runs=1)

        def boom(*a: object, **k: object) -> None:
            raise GhError(["gh"], 1, "no such issue")

        monkeypatch.setattr(manager_module, "fetch_issue", boom)

        record = await manager.start_from_url(IssueRef("a", "b", 1))
        assert record.task is not None
        await record.task
        assert manager.slots_available == 1
        assert record.error is not None

    async def test_a_failing_run_returns_its_slot(
        self, stubs: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from featurepilot.github.issues import IssueRef

        manager = _manager(max_concurrent_runs=1)

        async def explode(*a: object, **k: object) -> None:
            raise RuntimeError("the graph fell over")

        monkeypatch.setattr(manager, "_drive", explode)

        record = await manager.start_from_url(IssueRef("acme", "widget", 7))
        assert record.task is not None
        await record.task
        assert manager.slots_available == 1


class TestQueueVisibility:
    """A queued run looks identical to a stalled one from the browser. The UI
    needs to distinguish "waiting for a slot" from "working"."""

    async def test_a_waiting_run_reports_itself_queued(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        from featurepilot.github.issues import IssueRef

        manager = _manager(max_concurrent_runs=1)
        holding, release = asyncio.Event(), asyncio.Event()

        async def fake_drive(record: object, *a: object, **k: object) -> None:
            holding.set()
            await release.wait()

        monkeypatch.setattr(manager, "_drive", fake_drive)
        monkeypatch.setattr(manager, "_run_one", fake_drive)

        first = await manager.start_from_url(IssueRef("acme", "widget", 1))
        await asyncio.wait_for(holding.wait(), timeout=2)
        second = await manager.start_from_url(IssueRef("acme", "widget", 2))
        await asyncio.sleep(0)  # let the second task reach the semaphore

        assert first.public()["queued"] is False
        assert second.public()["queued"] is True

        release.set()
        for record in (first, second):
            assert record.task is not None
            await record.task
