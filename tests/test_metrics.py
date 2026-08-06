"""Metrics recorder: the event stream, and the budget ceiling that stops a
looping agent from billing indefinitely."""

from __future__ import annotations

import pytest

from featurepilot.config import Role, Settings
from featurepilot.lifecycle import RunPhase
from featurepilot.metrics.events import (
    CompositeSink,
    EventKind,
    InMemorySink,
    MetricEvent,
)
from featurepilot.metrics.recorder import BudgetExceeded, MetricsRecorder


class TestNodeInstrumentation:
    async def test_node_emits_started_and_ended(
        self, recorder: MetricsRecorder, sink: InMemorySink
    ) -> None:
        async with recorder.node("planner", RunPhase.PLANNING):
            pass
        assert [e.kind for e in sink.events] == [EventKind.NODE_STARTED, EventKind.NODE_ENDED]
        assert sink.of_kind(EventKind.NODE_ENDED)[0].payload["ok"] is True

    async def test_node_records_ended_even_on_failure(
        self, recorder: MetricsRecorder, sink: InMemorySink
    ) -> None:
        """A node that blew up is exactly the one you want timing and error text
        for, so the ended event must survive the exception."""
        with pytest.raises(RuntimeError, match="boom"):
            async with recorder.node("coder", RunPhase.CODING):
                raise RuntimeError("boom")
        ended = sink.of_kind(EventKind.NODE_ENDED)[0]
        assert ended.payload["ok"] is False
        assert "boom" in ended.payload["error"]

    async def test_latency_accumulates_across_attempts(self, recorder: MetricsRecorder) -> None:
        for _ in range(2):
            async with recorder.node("coder", RunPhase.CODING):
                pass
        assert "coder" in recorder.totals.per_node_ms


class TestBudget:
    async def test_token_ceiling_raises(self, sink: InMemorySink) -> None:
        settings = Settings(anthropic_api_key="sk-ant-test", max_tokens_per_run=100, _env_file=None)  # type: ignore[call-arg]
        rec = MetricsRecorder("r", sink, settings)
        rec.guard()  # fine at zero
        await rec.record_model_call(Role.CODER, "anthropic/claude-sonnet-5", 80, 40)
        with pytest.raises(BudgetExceeded, match="token ceiling"):
            rec.guard()

    async def test_cost_ceiling_raises(self, sink: InMemorySink) -> None:
        settings = Settings(anthropic_api_key="sk-ant-test", max_usd_per_run=0.0, _env_file=None)  # type: ignore[call-arg]
        rec = MetricsRecorder("r", sink, settings)
        with pytest.raises(BudgetExceeded, match="cost ceiling"):
            rec.guard()

    async def test_remaining_tokens_never_negative(self, sink: InMemorySink) -> None:
        settings = Settings(anthropic_api_key="sk-ant-test", max_tokens_per_run=10, _env_file=None)  # type: ignore[call-arg]
        rec = MetricsRecorder("r", sink, settings)
        await rec.record_model_call(Role.CODER, "anthropic/claude-sonnet-5", 50, 50)
        assert rec.remaining_tokens == 0

    async def test_unknown_model_prices_at_zero_rather_than_raising(
        self, recorder: MetricsRecorder
    ) -> None:
        """A missing price entry is a metrics gap, never a failed run."""
        await recorder.record_model_call(Role.CODER, "ollama/some-local-model", 10, 10)
        assert recorder.totals.cost_usd >= 0.0
        assert recorder.totals.model_calls == 1


class TestHallucinationSignal:
    def test_rate_is_zero_when_nothing_referenced(self, recorder: MetricsRecorder) -> None:
        assert recorder.totals.nonexistent_ref_rate == 0.0

    def test_rate_computes(self, recorder: MetricsRecorder) -> None:
        recorder.record_refs(total=8, nonexistent=2)
        assert recorder.totals.nonexistent_ref_rate == 0.25

    def test_rate_accumulates_across_nodes(self, recorder: MetricsRecorder) -> None:
        recorder.record_refs(total=4, nonexistent=1)
        recorder.record_refs(total=4, nonexistent=1)
        assert recorder.totals.total_refs == 8
        assert recorder.totals.nonexistent_ref_rate == 0.25


class TestToolAccounting:
    async def test_registry_ledger_drains_into_events(
        self, recorder: MetricsRecorder, fake_registry, sink: InMemorySink
    ) -> None:
        """Nodes carry no tool instrumentation; the registry already logged it."""
        await fake_registry.call("read_file", path="README.md")
        await recorder.record_tool_calls(fake_registry.calls)
        assert recorder.totals.tool_calls == 1
        assert sink.of_kind(EventKind.TOOL_CALLED)[0].payload["tool"] == "read_file"


class TestSinks:
    async def test_composite_isolates_a_failing_sink(self, sink: InMemorySink) -> None:
        """Telemetry breaking must never break a run."""

        class Broken:
            async def emit(self, event: MetricEvent) -> None:
                raise ConnectionError("redis is down")

            async def aclose(self) -> None:
                return None

        composite = CompositeSink([Broken(), sink])
        await composite.emit(MetricEvent(run_id="r", kind=EventKind.RUN_STARTED))
        assert len(sink.events) == 1, "healthy sink should still receive the event"
        assert composite.errors and "redis is down" in composite.errors[0]

    def test_redaction_strips_content_bearing_keys(self) -> None:
        """Events leaving the process shouldn't carry file contents scraped out
        of the target repo."""
        event = MetricEvent(
            run_id="r",
            kind=EventKind.TOOL_CALLED,
            payload={"tool": "read_file", "content": "SECRET", "diff": "x"},
        )
        clean = event.redacted()
        assert "content" not in clean.payload
        assert "diff" not in clean.payload
        assert clean.payload["tool"] == "read_file"
        assert clean.payload["_redacted"] == ["content", "diff"]
        assert event.payload["content"] == "SECRET", "original must not be mutated"


class TestRunSummary:
    async def test_run_ended_carries_the_totals(
        self, recorder: MetricsRecorder, sink: InMemorySink
    ) -> None:
        await recorder.record_model_call(Role.PLANNER, "anthropic/claude-sonnet-5", 100, 50)
        recorder.record_refs(total=4, nonexistent=1)
        await recorder.run_ended("success")
        payload = sink.of_kind(EventKind.RUN_ENDED)[0].payload
        assert payload["outcome"] == "success"
        assert payload["input_tokens"] == 100
        assert payload["nonexistent_ref_rate"] == 0.25
