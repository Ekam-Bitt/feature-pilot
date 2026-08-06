"""Structured metric events.

Nodes emit events; sinks decide what to do with them. The indirection earns its
keep because three consumers want the same stream:

- Postgres, for the aggregates a dashboard reads
- the SSE endpoint, so the CLI/UI can render live progress
- the LangSmith artifact log

Emitting events rather than writing rows directly means adding a consumer is a
new sink, not another instrumentation pass through every node.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol

from pydantic import BaseModel, Field


class EventKind(StrEnum):
    RUN_STARTED = "run_started"
    PHASE_CHANGED = "phase_changed"
    NODE_STARTED = "node_started"
    NODE_ENDED = "node_ended"
    TOOL_CALLED = "tool_called"
    MODEL_CALLED = "model_called"
    #: Emitted when the graph parks on a human (plan approval, diff review).
    AWAITING_HUMAN = "awaiting_human"
    ARTIFACT = "artifact"
    RUN_ENDED = "run_ended"


class MetricEvent(BaseModel):
    """One thing that happened. `payload` is deliberately open — sinks that
    care about a field read it, sinks that don't pass it through. Locking the
    payload into per-kind models would make adding a measurement a schema
    migration, which is the opposite of the point."""

    run_id: str
    kind: EventKind
    payload: dict[str, Any] = Field(default_factory=dict)
    emitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def redacted(self) -> MetricEvent:
        """Copy with obviously-sensitive payload keys removed.

        Tool args can contain file contents and, in principle, secrets read out
        of the target repo. Sinks that leave the process (Redis, LangSmith)
        should publish this rather than the raw event.
        """
        drop = {"content", "raw_output", "diff", "file_text"}
        clean = {k: v for k, v in self.payload.items() if k not in drop}
        if len(clean) != len(self.payload):
            clean["_redacted"] = sorted(set(self.payload) & drop)
        return self.model_copy(update={"payload": clean})


class EventSink(Protocol):
    """Where events go. Sinks must not raise: telemetry failing is never a
    reason for a run to fail."""

    async def emit(self, event: MetricEvent) -> None: ...

    async def aclose(self) -> None: ...


class InMemorySink:
    """Test sink, and the reason assertions about metrics need no database."""

    def __init__(self) -> None:
        self.events: list[MetricEvent] = []

    async def emit(self, event: MetricEvent) -> None:
        self.events.append(event)

    async def aclose(self) -> None:
        return None

    def of_kind(self, kind: EventKind) -> list[MetricEvent]:
        return [e for e in self.events if e.kind is kind]


class CompositeSink:
    """Fan out to several sinks, isolating failures.

    One sink being down (Redis restarted, Postgres unreachable) must not lose
    the others or break the run, so every emit is individually guarded.
    """

    def __init__(self, sinks: Sequence[EventSink]) -> None:
        self._sinks = list(sinks)
        self.errors: list[str] = []

    async def emit(self, event: MetricEvent) -> None:
        for sink in self._sinks:
            try:
                await sink.emit(event)
            except Exception as exc:  # noqa: BLE001 — telemetry must not raise
                self.errors.append(f"{type(sink).__name__}: {exc}")

    async def aclose(self) -> None:
        for sink in self._sinks:
            try:
                await sink.aclose()
            except Exception as exc:  # noqa: BLE001
                self.errors.append(f"{type(sink).__name__}.aclose: {exc}")
