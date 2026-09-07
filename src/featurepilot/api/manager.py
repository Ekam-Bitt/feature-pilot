"""Run lifecycle for the API.

A run is long-lived — it owns a container and two MCP subprocesses — so it cannot
live inside a request. Each one runs as a background task that drives the graph
and parks on an `asyncio.Queue` when it needs a human. `POST /approve` puts a
decision on that queue; the task picks it up and carries on.

Events reach clients through Redis pub/sub rather than from this object, so the
SSE endpoint works for several viewers at once and would keep working if the API
were run as more than one process.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from featurepilot.config import Settings, get_settings
from featurepilot.contracts import HumanDecision, PRSummary
from featurepilot.credentials import RunCredentials
from featurepilot.github.client import GhError
from featurepilot.github.clone import ClonedRepo, clone_root_for, shallow_clone
from featurepilot.github.issues import IssueRef, fetch_issue
from featurepilot.github.publish import PublishError, publish_run
from featurepilot.graph.nodes.describe import describe_tests
from featurepilot.lifecycle import RunPhase
from featurepilot.metrics.events import EventKind, InMemorySink, MetricEvent
from featurepilot.run import open_run, stream_run

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RunRecord:
    """What the API knows about one run without touching the graph."""

    run_id: str
    repo: str
    issue_ref: str
    phase: RunPhase = RunPhase.CREATED
    #: The interrupt payload the run is parked on, if any.
    pending: dict[str, Any] | None = None
    error: str | None = None
    task: asyncio.Task[None] | None = None
    decisions: asyncio.Queue[HumanDecision] = field(default_factory=asyncio.Queue)
    events: InMemorySink = field(default_factory=InMemorySink)
    #: Set whenever the run stops waiting on a human. Lets the SSE stream await
    #: a wake-up instead of polling `pending` on a timer.
    resumed: asyncio.Event = field(default_factory=asyncio.Event)
    #: URL-started runs only. The publish artifacts are cached on the record
    #: because the checkpointer dies with open_run's exit stack — by the time
    #: POST /publish arrives there is no graph left to query.
    issue_url: str | None = None
    clone: ClonedRepo | None = None
    pr_summary: PRSummary | None = None
    diff: str | None = None
    #: How the suite ended, in the same words the reviewer was shown.
    test_summary: str | None = None
    pr_url: str | None = None
    publishing: bool = False
    draft: bool = False
    #: This run's credentials. In memory only: never written to Postgres, never
    #: in `public()`, never in an event payload.
    credentials: RunCredentials = field(default_factory=RunCredentials)
    #: True from admission until a concurrency slot frees up. A queued run and
    #: a stalled one look identical from a browser without this.
    queued: bool = True

    @property
    def finished(self) -> bool:
        return self.phase in (RunPhase.DONE, RunPhase.FAILED)

    @property
    def publishable(self) -> bool:
        return (
            self.phase is RunPhase.DONE
            and self.clone is not None
            and bool(self.diff)
            and self.pr_summary is not None
        )

    def public(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "repo": self.repo,
            "issue_ref": self.issue_ref,
            "phase": str(self.phase),
            "finished": self.finished,
            "awaiting_human": self.pending is not None,
            "pending": self.pending,
            "error": self.error,
            "pr_url": self.pr_url,
            "publishable": self.publishable,
            "queued": self.queued,
        }


class RunManager:
    """Owns every in-flight run for this process."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._runs: dict[str, RunRecord] = {}
        #: Admitted runs, running or waiting for a slot. Counted rather than
        #: derived from `_runs` because finished runs stay in `_runs` for
        #: status queries long after they release their sandbox.
        self._admitted = 0
        self._slots = asyncio.Semaphore(self.settings.max_concurrent_runs)
        #: Spend on the *server's* credentials only, for the current day.
        self._spend_today = 0.0
        self._spend_day = date.today()
        #: Where to recover a previous process's spend from. Read once, lazily,
        #: because the ceiling has to survive a restart: held only in memory it
        #: reset to zero on every deploy and the guard failed open, which is
        #: the one direction a money guard must not fail.
        self._spend_source: Callable[[], float] | None = _durable_spend_today
        self._seeded = False

    # --- admission ---------------------------------------------------------
    #
    # Each concurrent run holds a sandbox container sized `sandbox_memory`, so
    # concurrency is a memory ceiling. Past it runs queue; past the queue they
    # are refused, because a queue that grows without limit just converts an
    # overload into a room full of visitors watching a placeholder.

    @property
    def slots_available(self) -> int:
        return max(0, self.settings.max_concurrent_runs - self._admitted)

    @property
    def accepting_runs(self) -> bool:
        ceiling = self.settings.max_concurrent_runs + self.settings.max_queued_runs
        return self._admitted < ceiling

    def reserve_slot(self) -> None:
        self._admitted += 1

    def release_slot(self) -> None:
        self._admitted = max(0, self._admitted - 1)

    # --- spend -------------------------------------------------------------

    def seed_spend_from(self, source: Callable[[], float] | None) -> None:
        """Override where recovered spend comes from (tests, or a deployment
        with no durable store)."""
        self._spend_source = source
        self._seeded = False

    def _seed_once(self) -> None:
        if self._seeded:
            return
        self._seeded = True
        if self._spend_source is None:
            return
        try:
            self._spend_today += self._spend_source()
        except Exception as exc:  # noqa: BLE001
            # A datastore that cannot be read must not stop the API serving.
            # The cap degrades to this process's own accounting, and says so.
            log.warning("could not recover today's spend (%s); cap is per-process", exc)

    def _roll_day(self) -> None:
        today = date.today()
        if today != self._spend_day:
            self._spend_day = today
            self._spend_today = 0.0
            self._seeded = False
        self._seed_once()

    def record_spend(self, usd: float, *, credentials: RunCredentials) -> None:
        """Only the operator's own credentials accrue: a visitor supplying a
        key is spending their money, and metering it here would cap a budget
        this process does not own."""
        if not credentials.uses_server_credentials:
            return
        self._roll_day()
        self._spend_today += usd

    def within_daily_budget(self, credentials: RunCredentials) -> bool:
        if not credentials.uses_server_credentials:
            return True
        self._roll_day()
        return self._spend_today < self.settings.max_usd_per_day

    # --- publishing --------------------------------------------------------

    def effective_draft(self, credentials: RunCredentials, *, requested_draft: bool) -> bool:
        """Draft is the floor for runs publishing under the operator's identity.

        A visitor's run would otherwise open a non-draft pull request, on a
        repository of their choosing, authored by the operator.
        """
        if credentials.uses_server_credentials:
            return True
        return requested_draft

    def get(self, run_id: str) -> RunRecord | None:
        return self._runs.get(run_id)

    def list(self) -> list[dict[str, Any]]:
        return [r.public() for r in self._runs.values()]

    async def start(
        self,
        repo: Path,
        issue: str,
        *,
        issue_ref: str = "",
        auto_approve: bool = False,
    ) -> RunRecord:
        run_id = uuid.uuid4().hex[:12]
        record = RunRecord(run_id=run_id, repo=str(repo), issue_ref=issue_ref or "(inline)")
        self._runs[run_id] = record
        record.task = asyncio.create_task(
            self._drive(record, repo, issue, issue_ref, auto_approve),
            name=f"fp-run-{run_id}",
        )
        return record

    async def start_from_url(
        self,
        ref: IssueRef,
        *,
        auto_approve: bool = False,
        auto_publish: bool = False,
        draft: bool = False,
        credentials: RunCredentials | None = None,
    ) -> RunRecord:
        """Start a run from a public issue URL. Returns before the clone lands:
        fetch and clone happen inside the driving task, so POST /runs answers
        immediately and ingestion progress is observable on the record."""
        run_id = uuid.uuid4().hex[:12]
        record = RunRecord(run_id=run_id, repo="(cloning)", issue_ref=f"{ref.slug}#{ref.number}")
        record.issue_url = f"https://github.com/{ref.slug}/issues/{ref.number}"
        record.draft = draft
        record.credentials = credentials or RunCredentials()
        self._runs[run_id] = record
        # Reserved here, not inside the task: admission has to be decided
        # before the caller is told the run was accepted, and the task may not
        # be scheduled for a while.
        self.reserve_slot()
        record.task = asyncio.create_task(
            self._drive_from_url(record, ref, auto_approve, auto_publish),
            name=f"fp-run-{run_id}",
        )
        return record

    def _settings_for(self, record: RunRecord) -> Settings:
        """This run's configuration: the server's, with the session's own
        credentials layered on where it supplied them."""
        return record.credentials.resolve(self.settings)

    def _github_token(self, record: RunRecord) -> str | None:
        token = self._settings_for(record).github_token
        return token.get_secret_value() if token else None

    async def publish(self, run_id: str, *, draft: bool = False) -> str:
        """Fork, push, and open the PR for a finished URL-started run."""
        record = self._runs[run_id]
        if record.clone is None or not record.diff or record.pr_summary is None:
            raise PublishError("run has no publishable artifacts")
        record.publishing = True
        try:
            result = await asyncio.to_thread(
                publish_run,
                record.clone,
                record.diff,
                record.pr_summary,
                token=self._github_token(record),
                draft=draft,
            )
        finally:
            record.publishing = False
        record.pr_url = result.pr_url
        return result.pr_url

    async def approve(self, run_id: str, decision: HumanDecision) -> bool:
        """Answer a pending interrupt. False if the run is not waiting."""
        record = self._runs.get(run_id)
        if record is None or record.pending is None:
            return False
        record.pending = None
        await record.decisions.put(decision)
        record.resumed.set()
        return True

    async def cancel(self, run_id: str) -> bool:
        record = self._runs.get(run_id)
        if record is None or record.task is None or record.task.done():
            return False
        record.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await record.task
        return True

    async def aclose(self) -> None:
        """Cancel everything still running. Called on API shutdown so containers
        are torn down rather than left for the reaper."""
        for run_id in list(self._runs):
            await self.cancel(run_id)

    async def _drive_from_url(
        self,
        record: RunRecord,
        ref: IssueRef,
        auto_approve: bool,
        auto_publish: bool,
    ) -> None:
        # `release_slot` is in the `finally` because every exit path — a failed
        # clone, a crashed graph, a cancelled task — has to return capacity.
        # A leaked slot lowers the host's ceiling until the process restarts.
        try:
            async with self._slots:
                record.queued = False
                await self._run_one(record, ref, auto_approve, auto_publish)
        except asyncio.CancelledError:
            record.phase = RunPhase.FAILED
            record.error = "cancelled"
            record.pending = None
            record.resumed.set()
            raise
        except Exception as exc:  # noqa: BLE001 - a failed run must not kill the API
            # `_drive` handles its own failures; this catches the ingestion and
            # publish steps around it, so a task never dies with nobody
            # holding its exception and a run never sits in a phase it left.
            log.exception("run %s failed outside the graph", record.run_id)
            record.phase = RunPhase.FAILED
            record.error = f"{type(exc).__name__}: {exc}"
            record.pending = None
            record.resumed.set()
        finally:
            self.release_slot()

    async def _run_one(
        self,
        record: RunRecord,
        ref: IssueRef,
        auto_approve: bool,
        auto_publish: bool,
    ) -> None:
        token = self._github_token(record)
        try:
            fetched = await asyncio.to_thread(fetch_issue, ref, token=token)
            clone = await asyncio.to_thread(
                shallow_clone, ref, clone_root_for(record.run_id), token=token
            )
        except GhError as exc:
            log.warning("ingestion for %s failed: %s", record.run_id, exc)
            record.phase = RunPhase.FAILED
            record.error = f"GitHub ingestion failed: {exc}"
            record.resumed.set()
            return
        record.repo = str(clone.path)
        record.clone = clone
        await self._drive(record, clone.path, fetched.text, record.issue_ref, auto_approve)
        if auto_publish and record.publishable:
            draft = self.effective_draft(record.credentials, requested_draft=record.draft)
            try:
                await self.publish(record.run_id, draft=draft)
            except (PublishError, GhError) as exc:
                log.warning("auto-publish for %s failed: %s", record.run_id, exc)
                record.error = f"run succeeded; publish failed: {exc}"

    async def _drive(
        self,
        record: RunRecord,
        repo: Path,
        issue: str,
        issue_ref: str,
        auto_approve: bool,
    ) -> None:
        try:
            async with open_run(
                repo,
                issue,
                issue_ref=issue_ref,
                settings=self._settings_for(record),
                run_id=record.run_id,
                auto_approve=auto_approve,
                extra_sinks=(record.events,),
            ) as handle:
                resume: HumanDecision | None = None
                while True:
                    async for event in stream_run(
                        handle,
                        issue=issue,
                        repo_path=repo,
                        issue_ref=issue_ref,
                        resume=resume,
                    ):
                        update = event.get("update")
                        if isinstance(update, dict) and (phase := update.get("phase")):
                            record.phase = RunPhase(str(phase))

                    pending = await handle.pending_interrupt()
                    if pending is None:
                        break

                    record.pending = pending
                    record.resumed.clear()
                    # Blocks until POST /approve supplies an answer. The container
                    # stays up meanwhile, which is the point of running this as a
                    # task rather than inside a request.
                    resume = await record.decisions.get()

                # Metered before the exit stack unwinds: the recorder's
                # totals are what a run actually cost, and the operator's
                # daily ceiling is enforced from them.
                self.record_spend(
                    handle.ctx.recorder.totals.cost_usd, credentials=record.credentials
                )
                final = await handle.state()
                record.phase = RunPhase(str(final.get("phase", RunPhase.FAILED)))
                record.error = final.get("error")
                if record.clone is not None:
                    # Stash while the checkpointer is still alive (see RunRecord).
                    record.pr_summary = final.get("pr")
                    code = final.get("code")
                    record.diff = code.diff if code is not None else None
                    record.test_summary = describe_tests(final.get("tests"))
                record.resumed.set()
        except asyncio.CancelledError:
            record.phase = RunPhase.FAILED
            record.error = "cancelled"
            record.pending = None
            record.resumed.set()  # release anyone waiting on this run
            raise
        except Exception as exc:  # noqa: BLE001 - a failed run must not kill the API
            log.exception("run %s failed", record.run_id)
            record.phase = RunPhase.FAILED
            record.error = f"{type(exc).__name__}: {exc}"
            record.pending = None
            record.resumed.set()


def _durable_spend_today() -> float:
    """Today's model spend, from the raw event log.

    The `run_metrics` and `node_metrics` projections carry zeros for cost —
    their inserts never populate those columns — so the raw log is the only
    place the real number lives, which is also what its own docstring says it
    is for.

    This deliberately counts *all* of today's spend, not just server-funded
    runs: the log does not record which credential paid, and over-counting
    makes the ceiling conservative. Erring the other way would let the guard
    fail open, and a visitor's own run is exempt from the ceiling anyway.
    """
    import psycopg

    settings = get_settings()
    with psycopg.connect(settings.postgres_dsn, connect_timeout=3) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM((payload->>'cost_usd')::numeric), 0)"
            " FROM metric_events"
            " WHERE kind = 'model_called' AND emitted_at >= date_trunc('day', now())"
        ).fetchone()
    return float(row[0]) if row else 0.0


async def subscribe(settings: Settings, run_id: str) -> Any:
    """Redis pub/sub subscription for a run's events, or None if Redis is down."""
    try:
        import redis.asyncio as redis

        client = redis.from_url(settings.redis_url)
        pubsub = client.pubsub()
        await pubsub.subscribe(f"fp:run:{run_id}")
    except Exception as exc:  # noqa: BLE001
        log.warning("cannot subscribe to run %s (%s); falling back to polling", run_id, exc)
        return None
    return pubsub


def replay(record: RunRecord) -> list[MetricEvent]:
    """Events already emitted, so a client connecting late still sees the run
    from the beginning rather than joining mid-story."""
    return [e.redacted() for e in record.events.events]


__all__ = ["EventKind", "RunManager", "RunRecord", "replay", "subscribe"]
