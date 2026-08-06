"""Metrics recording and budget enforcement.

The recorder is the single place that knows how much a run has spent, which is
why the budget guard lives here rather than in the graph. An agent that loops is
an agent that bills, so the ceiling is load-bearing rather than polish: nodes
call `guard()` before each model call and the run fails cleanly instead of
quietly burning tokens.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from featurepilot.config import Role, Settings
from featurepilot.lifecycle import RunPhase
from featurepilot.metrics.events import EventKind, EventSink, MetricEvent


class BudgetExceeded(RuntimeError):
    """Raised when a run hits its token or dollar ceiling. Terminal: the graph
    transitions to FAILED rather than retrying, because a retry would by
    definition also exceed the budget."""


@dataclass(slots=True)
class RoleSpend:
    """What one role cost. Keyed by role rather than node because a node may make
    several calls and the escalation path swaps the model underneath."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cost_usd: float = 0.0
    calls: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(slots=True)
class RunTotals:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cost_usd: float = 0.0
    tool_calls: int = 0
    model_calls: int = 0
    attempts: int = 0
    #: Deterministic hallucination signal — references to files or symbols the
    #: repo does not contain, over total references made. Available in 1A, well
    #: before any LLM-judge harness exists.
    total_refs: int = 0
    nonexistent_refs: int = 0
    per_node_ms: dict[str, int] = field(default_factory=dict)
    #: Tokens, cost and call count attributed to the role that spent them.
    #: Latency alone cannot answer "what would dropping the reviewer save"; an
    #: ablation needs the spend broken out, and so does any per-node cost work.
    per_role: dict[str, RoleSpend] = field(default_factory=dict)
    #: Tool invocations per node, so a node's tool appetite is visible without
    #: re-reading a trace.
    tool_calls_by_node: dict[str, int] = field(default_factory=dict)
    #: Reviewer verdicts, for an acceptance rate across runs.
    reviews_approved: int = 0
    reviews_rejected: int = 0

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    @property
    def nonexistent_ref_rate(self) -> float:
        return self.nonexistent_refs / self.total_refs if self.total_refs else 0.0

    @property
    def review_acceptance_rate(self) -> float:
        """Share of reviews that approved. A rate near 1.0 across many runs
        suggests the reviewer is a rubber stamp; near 0.0 suggests it is blocking
        work the tests already cleared."""
        seen = self.reviews_approved + self.reviews_rejected
        return self.reviews_approved / seen if seen else 0.0

    def cost_share(self) -> dict[str, float]:
        """Fraction of spend per role, largest first. The input to deciding which
        node to re-tier."""
        total = self.cost_usd
        if not total:
            return {}
        share = {role: spend.cost_usd / total for role, spend in self.per_role.items()}
        return dict(sorted(share.items(), key=lambda kv: -kv[1]))


def _price(model: str, input_tokens: int, output_tokens: int) -> float:
    """Best-effort cost. Unknown models price at 0 rather than raising —
    a missing price entry must never fail a run.
    """
    try:
        from litellm import cost_per_token

        prompt_cost, completion_cost = cost_per_token(
            model=model,
            prompt_tokens=input_tokens,
            completion_tokens=output_tokens,
        )
        return float(prompt_cost + completion_cost)
    except Exception:  # noqa: BLE001 — pricing is advisory
        return 0.0


class MetricsRecorder:
    def __init__(self, run_id: str, sink: EventSink, settings: Settings) -> None:
        self.run_id = run_id
        self.sink = sink
        self.settings = settings
        self.totals = RunTotals()

    async def _emit(self, kind: EventKind, **payload: Any) -> None:
        await self.sink.emit(MetricEvent(run_id=self.run_id, kind=kind, payload=payload))

    # --- lifecycle --------------------------------------------------------

    async def run_started(self, repo: str, issue_ref: str) -> None:
        await self._emit(EventKind.RUN_STARTED, repo=repo, issue_ref=issue_ref)

    async def phase_changed(self, src: RunPhase, dst: RunPhase) -> None:
        await self._emit(EventKind.PHASE_CHANGED, src=str(src), dst=str(dst))

    async def awaiting_human(self, reason: str, detail: str = "") -> None:
        await self._emit(EventKind.AWAITING_HUMAN, reason=reason, detail=detail)

    async def run_ended(self, outcome: str, error: str | None = None) -> None:
        await self._emit(
            EventKind.RUN_ENDED,
            outcome=outcome,
            error=error,
            input_tokens=self.totals.input_tokens,
            output_tokens=self.totals.output_tokens,
            cost_usd=round(self.totals.cost_usd, 6),
            tool_calls=self.totals.tool_calls,
            model_calls=self.totals.model_calls,
            attempts=self.totals.attempts,
            nonexistent_ref_rate=round(self.totals.nonexistent_ref_rate, 4),
            review_acceptance_rate=round(self.totals.review_acceptance_rate, 4),
            per_role={
                role: {
                    "calls": spend.calls,
                    "input_tokens": spend.input_tokens,
                    "output_tokens": spend.output_tokens,
                    "cost_usd": round(spend.cost_usd, 6),
                }
                for role, spend in self.totals.per_role.items()
            },
            tool_calls_by_node=dict(self.totals.tool_calls_by_node),
        )

    @asynccontextmanager
    async def node(self, name: str, phase: RunPhase, attempt: int = 0) -> AsyncIterator[None]:
        """Wrap a node execution: emits started/ended and records latency.

        Records the `ended` event on failure too — a node that blew up is
        exactly the one you want timing and error text for.
        """
        await self._emit(EventKind.NODE_STARTED, node=name, phase=str(phase), attempt=attempt)
        started = time.perf_counter()
        error: str | None = None
        try:
            yield
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            self.totals.per_node_ms[name] = self.totals.per_node_ms.get(name, 0) + elapsed_ms
            await self._emit(
                EventKind.NODE_ENDED,
                node=name,
                phase=str(phase),
                attempt=attempt,
                latency_ms=elapsed_ms,
                ok=error is None,
                error=error,
            )

    # --- accounting -------------------------------------------------------

    async def record_model_call(
        self,
        role: Role,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cache_read: int = 0,
    ) -> None:
        cost = _price(model, input_tokens, output_tokens)
        self.totals.input_tokens += input_tokens
        self.totals.output_tokens += output_tokens
        self.totals.cache_read += cache_read
        self.totals.cost_usd += cost
        self.totals.model_calls += 1

        spend = self.totals.per_role.setdefault(str(role), RoleSpend())
        spend.input_tokens += input_tokens
        spend.output_tokens += output_tokens
        spend.cache_read += cache_read
        spend.cost_usd += cost
        spend.calls += 1
        await self._emit(
            EventKind.MODEL_CALLED,
            role=str(role),
            model=model,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read=cache_read,
            cost_usd=round(cost, 6),
        )

    async def record_tool_calls(
        self, calls: Iterable[dict[str, Any]], node: str | None = None
    ) -> None:
        """Drain the ToolRegistry ledger. Nodes need no tool instrumentation
        of their own because the registry already logged every invocation."""
        for call in calls:
            self.totals.tool_calls += 1
            if node:
                by_node = self.totals.tool_calls_by_node
                by_node[node] = by_node.get(node, 0) + 1
            await self._emit(EventKind.TOOL_CALLED, node=node, **call)

    def record_review(self, approved: bool) -> None:
        """Track reviewer verdicts so an acceptance rate exists across runs."""
        if approved:
            self.totals.reviews_approved += 1
        else:
            self.totals.reviews_rejected += 1

    def record_refs(self, total: int, nonexistent: int) -> None:
        self.totals.total_refs += total
        self.totals.nonexistent_refs += nonexistent

    async def artifact(self, kind: str, content: str) -> None:
        """Persist a large output: retrieved context, final patch, test output.
        Kept out of the event payload proper so the live stream stays small."""
        await self._emit(EventKind.ARTIFACT, artifact_kind=kind, content=content)

    # --- budget -----------------------------------------------------------

    def guard(self) -> None:
        """Raise if the run has spent its allowance. Called before each model
        call, so the ceiling binds even when a loop misbehaves."""
        if self.totals.total_tokens >= self.settings.max_tokens_per_run:
            raise BudgetExceeded(
                f"run {self.run_id} hit the token ceiling: "
                f"{self.totals.total_tokens} >= {self.settings.max_tokens_per_run}"
            )
        if self.totals.cost_usd >= self.settings.max_usd_per_run:
            raise BudgetExceeded(
                f"run {self.run_id} hit the cost ceiling: "
                f"${self.totals.cost_usd:.4f} >= ${self.settings.max_usd_per_run:.2f}"
            )

    @property
    def remaining_tokens(self) -> int:
        return max(0, self.settings.max_tokens_per_run - self.totals.total_tokens)
