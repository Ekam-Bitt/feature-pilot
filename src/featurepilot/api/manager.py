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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from featurepilot.config import Settings, get_settings
from featurepilot.contracts import HumanDecision
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

    @property
    def finished(self) -> bool:
        return self.phase in (RunPhase.DONE, RunPhase.FAILED)

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
        }


class RunManager:
    """Owns every in-flight run for this process."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._runs: dict[str, RunRecord] = {}

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
                settings=self.settings,
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

                final = await handle.state()
                record.phase = RunPhase(str(final.get("phase", RunPhase.FAILED)))
                record.error = final.get("error")
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
