"""Durable and live event sinks.

Three consumers of one event stream, which is the whole reason
`metrics/events.py` emits events rather than writing rows:

- `PostgresSink` — the append-only log plus the aggregates a dashboard reads
- `RedisSink`    — ephemeral pub/sub behind the SSE endpoint
- `ArtifactStore`— the big outputs (diff, retrieved context, raw test output)

Artifacts matter more than they look. Without them, answering "what did that run
actually change?" means paying for the run again — which is exactly what happened
before this module existed.

Every sink swallows its own failures. Telemetry breaking is never a reason for a
run to fail, and `CompositeSink` isolates them from each other besides.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from featurepilot.metrics.events import EventKind, MetricEvent

log = logging.getLogger(__name__)

#: Payload keys holding content too large for the event row. Written to
#: run_artifacts instead, so the event log stays scannable.
_BULK_KEYS = ("content", "raw_output", "diff")


class PostgresSink:
    """Appends every event, and projects the two aggregate tables from it.

    The raw log is the source of truth; `run_metrics` and `node_metrics` are
    projections. That ordering means a wrong aggregation is recoverable by
    replaying, rather than being a permanent hole in the record.
    """

    def __init__(self, dsn: str) -> None:
        self._dsn = dsn
        self._pool: Any = None
        self.errors: list[str] = []

    async def start(self) -> PostgresSink:
        try:
            from psycopg_pool import AsyncConnectionPool

            self._pool = AsyncConnectionPool(self._dsn, min_size=1, max_size=4, open=False)
            await self._pool.open(wait=True, timeout=5)
        except Exception as exc:  # noqa: BLE001 - a metrics DB is not worth a failed run
            log.warning("postgres metrics sink unavailable (%s); metrics will not persist", exc)
            self._pool = None
        return self

    async def emit(self, event: MetricEvent) -> None:
        if self._pool is None:
            return
        try:
            async with self._pool.connection() as conn:
                await self._write_event(conn, event)
                await self._project(conn, event)
        except Exception as exc:  # noqa: BLE001
            self.errors.append(str(exc))
            log.debug("metrics write failed: %s", exc)

    async def _write_event(self, conn: Any, event: MetricEvent) -> None:
        payload = {k: v for k, v in event.payload.items() if k not in _BULK_KEYS}
        await conn.execute(
            "INSERT INTO metric_events (run_id, kind, payload, emitted_at) VALUES (%s, %s, %s, %s)",
            (event.run_id, str(event.kind), json.dumps(payload, default=str), event.emitted_at),
        )
        # Bulk content goes to its own table so the event log stays readable.
        for key in _BULK_KEYS:
            value = event.payload.get(key)
            if isinstance(value, str) and value:
                kind = event.payload.get("artifact_kind", key)
                await conn.execute(
                    "INSERT INTO run_artifacts (run_id, kind, content) VALUES (%s, %s, %s)",
                    (event.run_id, str(kind), value),
                )

    async def _project(self, conn: Any, event: MetricEvent) -> None:
        p = event.payload
        if event.kind is EventKind.RUN_STARTED:
            await conn.execute(
                "INSERT INTO run_metrics (run_id, repo, issue_ref, phase)"
                " VALUES (%s, %s, %s, 'CREATED') ON CONFLICT (run_id) DO NOTHING",
                (event.run_id, str(p.get("repo", "")), str(p.get("issue_ref", ""))),
            )
        elif event.kind is EventKind.PHASE_CHANGED:
            await conn.execute(
                "UPDATE run_metrics SET phase = %s, updated_at = now() WHERE run_id = %s",
                (str(p.get("dst", "")), event.run_id),
            )
        elif event.kind is EventKind.NODE_ENDED:
            await conn.execute(
                "INSERT INTO node_metrics"
                " (run_id, node, phase, attempt, latency_ms, ok, error, started_at, ended_at)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now())",
                (
                    event.run_id,
                    str(p.get("node", "")),
                    str(p.get("phase", "")),
                    int(p.get("attempt", 0) or 0),
                    int(p.get("latency_ms", 0) or 0),
                    bool(p.get("ok", True)),
                    p.get("error"),
                ),
            )
        elif event.kind is EventKind.RUN_ENDED:
            await conn.execute(
                "UPDATE run_metrics SET outcome = %s, attempts = %s, input_tokens = %s,"
                " output_tokens = %s, cost_usd = %s, tool_calls = %s, updated_at = now()"
                " WHERE run_id = %s",
                (
                    str(p.get("outcome", "")),
                    int(p.get("attempts", 0) or 0),
                    int(p.get("input_tokens", 0) or 0),
                    int(p.get("output_tokens", 0) or 0),
                    float(p.get("cost_usd", 0) or 0),
                    int(p.get("tool_calls", 0) or 0),
                    event.run_id,
                ),
            )

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


class RedisSink:
    """Publishes to a per-run channel for the SSE endpoint.

    Redacted before publishing: events leaving the process should not carry file
    contents scraped out of the target repository.
    """

    def __init__(self, url: str, run_id: str) -> None:
        self._url = url
        self._run_id = run_id
        self._client: Any = None
        self.errors: list[str] = []

    @property
    def channel(self) -> str:
        return f"fp:run:{self._run_id}"

    async def start(self) -> RedisSink:
        try:
            import redis.asyncio as redis

            self._client = redis.from_url(self._url)
            await self._client.ping()
        except Exception as exc:  # noqa: BLE001
            log.warning("redis unavailable (%s); live streaming disabled", exc)
            self._client = None
        return self

    async def emit(self, event: MetricEvent) -> None:
        if self._client is None:
            return
        try:
            await self._client.publish(self.channel, event.redacted().model_dump_json())
        except Exception as exc:  # noqa: BLE001
            self.errors.append(str(exc))

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


class FileArtifactSink:
    """Writes bulk artifacts to disk under `.fp/runs/<run_id>/`.

    Deliberately independent of Postgres: inspecting what a run changed should
    not require a database to be up, and `cat .fp/runs/<id>/diff.patch` is the
    fastest possible answer to "what did it do".
    """

    def __init__(self, root: Any, run_id: str) -> None:
        from pathlib import Path

        self.dir = Path(root) / "runs" / run_id
        self._counts: dict[str, int] = {}
        self.errors: list[str] = []
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # noqa: BLE001
            log.warning("cannot create artifact directory %s: %s", self.dir, exc)

    _SUFFIX = {"diff": ".patch", "test_output": ".txt", "context": ".md"}

    async def emit(self, event: MetricEvent) -> None:
        for key in _BULK_KEYS:
            value = event.payload.get(key)
            if not isinstance(value, str) or not value:
                continue
            kind = str(event.payload.get("artifact_kind", key))
            try:
                self._write(kind, value)
            except OSError as exc:  # noqa: BLE001
                self.errors.append(str(exc))

    def _write(self, kind: str, content: str) -> None:
        seen = self._counts.get(kind, 0)
        self._counts[kind] = seen + 1
        # Attempt 2's diff must not silently overwrite attempt 1's: the sequence
        # is the interesting part when a run needed repairs.
        stem = kind if seen == 0 else f"{kind}-{seen + 1}"
        (self.dir / f"{stem}{self._SUFFIX.get(kind, '.txt')}").write_text(content, encoding="utf-8")

    async def aclose(self) -> None:
        return None
