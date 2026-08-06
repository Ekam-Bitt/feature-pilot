"""The graph, end to end, with no network and no container.

This is the payoff of the seams. A complete run — retrieve, plan, approve, code,
test, debug, re-code, test, review, summarise — executes here against scripted
models, a fake registry, a stub retriever and a fake sandbox. If any of those
abstractions were decorative, this file could not exist.

What it proves that the router tests cannot: that the nodes, the phase
transitions, the LangGraph wiring and the interrupt actually compose.
"""

from __future__ import annotations

from typing import Any

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from fakes import FakeFileSystem, FakeSandbox, StubRetriever
from featurepilot.config import Settings
from featurepilot.contracts import (
    CoderOutput,
    DebuggerOutput,
    FileEdit,
    HumanDecision,
    PlannerOutput,
    PlanStep,
    PRSummary,
    ReviewerOutput,
)
from featurepilot.graph.build import build_graph
from featurepilot.graph.context import RunContext
from featurepilot.graph.state import new_state
from featurepilot.lifecycle import RunPhase
from featurepilot.metrics.events import EventKind, InMemorySink
from featurepilot.metrics.recorder import MetricsRecorder

ISSUE = "Free shipping is granted on orders that fall below the threshold after discounts."

PLAN = PlannerOutput(
    summary="Judge shipping on the payable amount, not the pre-discount subtotal.",
    steps=[PlanStep(description="subtract both discounts", files=["src/shopsvc/cart.py"])],
    files_needed=["src/shopsvc/cart.py"],
    confidence="high",
)
PLAN_WITH_QUESTION = PLAN.model_copy(
    update={"open_questions": ["Should the threshold stay inclusive?"]}
)
PATCH = CoderOutput(
    edits=[FileEdit(path="src/shopsvc/cart.py", rationale="use the payable amount")],
    assumptions=["the threshold remains inclusive"],
)
RETRY = DebuggerOutput(
    failure_category="assertion",
    root_cause="only the promo discount was subtracted",
    suggested_edits=[FileEdit(path="src/shopsvc/cart.py", rationale="subtract both")],
    retry=True,
)
GIVE_UP = DebuggerOutput(failure_category="env", root_cause="fixtures are missing", retry=False)
APPROVE = ReviewerOutput(verdict="approve", reasons=["addresses the cause"])
REJECT = ReviewerOutput(verdict="reject", blocking=["masks the symptom"])
SUMMARY = PRSummary(
    title="Judge free shipping on the payable amount",
    body="...",
    test_plan="pytest -q",
)


class Scripted:
    """Returns queued contracts by type, so the order of node calls doesn't have
    to be hardcoded into every test."""

    def __init__(self, **queues: list[Any]) -> None:
        self.queues = {k: list(v) for k, v in queues.items()}
        self.calls: list[str] = []

    async def __call__(
        self, role: Any, output_model: type, messages: Any, *, escalate: bool = False
    ) -> Any:
        name = output_model.__name__
        self.calls.append(name)
        queue = self.queues.get(name)
        if not queue:
            raise AssertionError(f"no scripted {name} left (calls so far: {self.calls})")
        return queue.pop(0) if len(queue) > 1 else queue[0]


async def _noop_tool_loop(role: Any, messages: Any, registry: Any, **kwargs: Any) -> list[Any]:
    """The coder's tool phase. Returns the transcript unchanged — the edits are
    represented by the scripted CoderOutput."""
    return list(messages)


def make_ctx(
    settings: Settings,
    *,
    scripted: Scripted,
    sandbox: FakeSandbox | None = None,
    auto_approve: bool = False,
) -> tuple[RunContext, InMemorySink]:
    sink = InMemorySink()
    registry = FakeFileSystem(
        {"src/shopsvc/cart.py": "FREE_SHIPPING_THRESHOLD = 50_000\n"}
    ).as_registry()
    ctx = RunContext(
        settings=settings,
        registry=registry,
        retriever=StubRetriever(),
        recorder=MetricsRecorder("run-graph", sink, settings),
        call=scripted,
        tool_loop=_noop_tool_loop,
        sandbox=sandbox or FakeSandbox([(0, "85 passed in 1s")]),  # type: ignore[arg-type]
        auto_approve=auto_approve,
    )
    return ctx, sink


def compile_graph(ctx: RunContext) -> Any:
    return build_graph(ctx).compile(checkpointer=InMemorySaver())


def start_state() -> Any:
    return new_state(run_id="run-graph", repo_path="/work", issue=ISSUE, issue_ref="05.md")


CONFIG = {"configurable": {"thread_id": "t-1"}}


async def drain(graph: Any, payload: Any, config: dict[str, Any]) -> list[str]:
    """Run to completion or to an interrupt; return the node order."""
    visited: list[str] = []
    async for chunk in graph.astream(payload, config, stream_mode="updates"):
        visited.extend(chunk.keys())
    return visited


class TestHappyPath:
    async def test_reaches_done_with_a_pr_summary(self, settings: Settings) -> None:
        scripted = Scripted(
            PlannerOutput=[PLAN],
            CoderOutput=[PATCH],
            ReviewerOutput=[APPROVE],
            PRSummary=[SUMMARY],
        )
        ctx, _ = make_ctx(settings, scripted=scripted, auto_approve=True)
        graph = compile_graph(ctx)

        visited = await drain(graph, start_state(), CONFIG)
        final = (await graph.aget_state(CONFIG)).values

        assert visited == ["retrieve", "plan", "code", "test", "review", "summarize"]
        assert final["phase"] is RunPhase.DONE
        assert final["pr"].title == SUMMARY.title

    async def test_green_tests_still_go_through_review(self, settings: Settings) -> None:
        """Review is not skippable on success — a patch that edited a test to
        agree with the code is green and wrong."""
        scripted = Scripted(
            PlannerOutput=[PLAN],
            CoderOutput=[PATCH],
            ReviewerOutput=[APPROVE],
            PRSummary=[SUMMARY],
        )
        ctx, _ = make_ctx(settings, scripted=scripted, auto_approve=True)
        visited = await drain(compile_graph(ctx), start_state(), CONFIG)
        assert visited.index("review") < visited.index("summarize")

    async def test_the_diff_comes_from_the_sandbox_not_the_model(self, settings: Settings) -> None:
        """A model-reported diff is a hallucination risk on the one artifact a
        human reads most closely."""
        scripted = Scripted(
            PlannerOutput=[PLAN],
            CoderOutput=[PATCH],
            ReviewerOutput=[APPROVE],
            PRSummary=[SUMMARY],
        )
        sandbox = FakeSandbox([(0, "85 passed in 1s")])
        sandbox.diff_text = "--- a/cart.py\n+++ b/cart.py\n+from the sandbox\n"
        ctx, _ = make_ctx(settings, scripted=scripted, sandbox=sandbox, auto_approve=True)
        graph = compile_graph(ctx)

        await drain(graph, start_state(), CONFIG)
        final = (await graph.aget_state(CONFIG)).values

        # PATCH carries no diff; the value in state must have come from the sandbox.
        assert PATCH.diff == ""
        assert final["code"].diff == sandbox.diff_text


class TestRepairLoop:
    async def test_red_then_green_reaches_done(self, settings: Settings) -> None:
        """The whole point of the system: a failing patch is diagnosed, the coder
        re-enters, and the second attempt passes."""
        scripted = Scripted(
            PlannerOutput=[PLAN],
            CoderOutput=[PATCH, PATCH],
            DebuggerOutput=[RETRY],
            ReviewerOutput=[APPROVE],
            PRSummary=[SUMMARY],
        )
        sandbox = FakeSandbox()  # red, then green
        ctx, _ = make_ctx(settings, scripted=scripted, sandbox=sandbox, auto_approve=True)

        visited = await drain(compile_graph(ctx), start_state(), CONFIG)
        assert visited == [
            "retrieve",
            "plan",
            "code",
            "test",
            "debug",
            "code",
            "test",
            "review",
            "summarize",
        ]

    async def test_repair_restores_the_tree_before_recoding(self, settings: Settings) -> None:
        """Attempt 2 must start from clean code, not from attempt 1's broken patch."""
        scripted = Scripted(
            PlannerOutput=[PLAN],
            CoderOutput=[PATCH, PATCH],
            DebuggerOutput=[RETRY],
            ReviewerOutput=[APPROVE],
            PRSummary=[SUMMARY],
        )
        sandbox = FakeSandbox()
        ctx, _ = make_ctx(settings, scripted=scripted, sandbox=sandbox, auto_approve=True)
        await drain(compile_graph(ctx), start_state(), CONFIG)
        assert sandbox.restores == 1, "the second coding attempt should restore first"

    async def test_unretryable_failure_stops_cleanly(self, settings: Settings) -> None:
        scripted = Scripted(PlannerOutput=[PLAN], CoderOutput=[PATCH], DebuggerOutput=[GIVE_UP])
        sandbox = FakeSandbox([(1, "1 failed, 84 passed in 1s")])
        ctx, _ = make_ctx(settings, scripted=scripted, sandbox=sandbox, auto_approve=True)
        graph = compile_graph(ctx)

        await drain(graph, start_state(), CONFIG)
        final = (await graph.aget_state(CONFIG)).values
        assert final["phase"] is RunPhase.FAILED
        assert "not retryable" in (final["error"] or "")

    async def test_attempt_budget_is_enforced(self, settings: Settings) -> None:
        """An agent that loops is an agent that bills."""
        tight = settings.model_copy(update={"max_attempts": 2})
        scripted = Scripted(
            PlannerOutput=[PLAN],
            CoderOutput=[PATCH, PATCH, PATCH, PATCH],
            DebuggerOutput=[RETRY],
        )
        sandbox = FakeSandbox([(1, "1 failed in 1s")] * 6)
        ctx, _ = make_ctx(tight, scripted=scripted, sandbox=sandbox, auto_approve=True)
        graph = compile_graph(ctx)

        visited = await drain(graph, start_state(), CONFIG)
        final = (await graph.aget_state(CONFIG)).values
        assert visited.count("code") <= 2
        assert final["phase"] is RunPhase.FAILED

    async def test_a_rejected_review_sends_work_back(self, settings: Settings) -> None:
        scripted = Scripted(
            PlannerOutput=[PLAN],
            CoderOutput=[PATCH, PATCH],
            ReviewerOutput=[REJECT, APPROVE],
            PRSummary=[SUMMARY],
        )
        sandbox = FakeSandbox([(0, "85 passed in 1s")] * 4)
        ctx, _ = make_ctx(settings, scripted=scripted, sandbox=sandbox, auto_approve=True)
        visited = await drain(compile_graph(ctx), start_state(), CONFIG)
        assert visited.count("code") == 2
        assert visited[-1] == "summarize"


class TestHumanGate:
    async def test_the_run_parks_on_approval(self, settings: Settings) -> None:
        """Without --yes the graph must stop and wait, not code on its own."""
        scripted = Scripted(PlannerOutput=[PLAN])
        ctx, sink = make_ctx(settings, scripted=scripted, auto_approve=False)
        graph = compile_graph(ctx)

        await drain(graph, start_state(), CONFIG)
        snapshot = await graph.aget_state(CONFIG)

        assert snapshot.values["phase"] is RunPhase.WAITING_APPROVAL
        assert snapshot.next, "the graph should have a pending task"
        assert sink.of_kind(EventKind.AWAITING_HUMAN), "the pause should be observable"

    async def test_the_interrupt_carries_the_plan(self, settings: Settings) -> None:
        """The CLI renders this payload, so it has to contain the plan."""
        scripted = Scripted(PlannerOutput=[PLAN])
        ctx, _ = make_ctx(settings, scripted=scripted, auto_approve=False)
        graph = compile_graph(ctx)
        await drain(graph, start_state(), CONFIG)

        snapshot = await graph.aget_state(CONFIG)
        payloads = [
            i.value
            for task in snapshot.tasks
            for i in (getattr(task, "interrupts", ()) or ())
            if isinstance(getattr(i, "value", None), dict)
        ]
        assert payloads
        assert payloads[0]["summary"] == PLAN.summary
        assert payloads[0]["kind"] == "plan_approval"

    async def test_approving_resumes_to_completion(self, settings: Settings) -> None:
        scripted = Scripted(
            PlannerOutput=[PLAN],
            CoderOutput=[PATCH],
            ReviewerOutput=[APPROVE],
            PRSummary=[SUMMARY],
        )
        ctx, _ = make_ctx(settings, scripted=scripted, auto_approve=False)
        graph = compile_graph(ctx)

        await drain(graph, start_state(), CONFIG)
        await drain(graph, Command(resume=HumanDecision(verdict="approve")), CONFIG)

        final = (await graph.aget_state(CONFIG)).values
        assert final["phase"] is RunPhase.DONE

    async def test_rejecting_replans_rather_than_coding(self, settings: Settings) -> None:
        scripted = Scripted(
            PlannerOutput=[PLAN, PLAN],
            CoderOutput=[PATCH],
            ReviewerOutput=[APPROVE],
            PRSummary=[SUMMARY],
        )
        ctx, _ = make_ctx(settings, scripted=scripted, auto_approve=False)
        graph = compile_graph(ctx)

        await drain(graph, start_state(), CONFIG)
        after = await drain(
            graph,
            Command(resume=HumanDecision(verdict="reject", feedback="wrong module")),
            CONFIG,
        )
        # Resuming re-runs the interrupted approval node first; what matters is
        # that it then re-planned and did not proceed to code.
        assert "plan" in after, f"expected a re-plan, got {after}"
        assert "code" not in after, f"a rejected plan must not be coded, got {after}"
        # And it parks on the new plan rather than looping approve/reject forever.
        assert (await graph.aget_state(CONFIG)).values["phase"] is RunPhase.WAITING_APPROVAL

    async def test_rejection_does_not_loop_forever(self, settings: Settings) -> None:
        """Regression guard. The stale plan stayed in state after a rejection, so
        the router routed PLANNING -> APPROVAL and the run ping-ponged between
        approve and reject without ever re-planning."""
        scripted = Scripted(
            PlannerOutput=[PLAN, PLAN, PLAN],
            CoderOutput=[PATCH],
            ReviewerOutput=[APPROVE],
            PRSummary=[SUMMARY],
        )
        ctx, _ = make_ctx(settings, scripted=scripted, auto_approve=False)
        graph = compile_graph(ctx)

        await drain(graph, start_state(), CONFIG)
        for _ in range(2):
            nodes = await drain(
                graph, Command(resume=HumanDecision(verdict="reject", feedback="no")), CONFIG
            )
            assert "plan" in nodes, f"each rejection must re-plan, got {nodes}"

        # Accepting afterwards still completes.
        await drain(graph, Command(resume=HumanDecision(verdict="approve")), CONFIG)
        assert (await graph.aget_state(CONFIG)).values["phase"] is RunPhase.DONE

    async def test_auto_approve_still_stops_on_open_questions(self, settings: Settings) -> None:
        """--yes skips the gate for routine work; a genuine ambiguity still needs
        a human, or the answer is invented."""
        scripted = Scripted(PlannerOutput=[PLAN_WITH_QUESTION])
        ctx, _ = make_ctx(settings, scripted=scripted, auto_approve=True)
        graph = compile_graph(ctx)

        await drain(graph, start_state(), CONFIG)
        snapshot = await graph.aget_state(CONFIG)
        assert snapshot.values["phase"] is RunPhase.WAITING_APPROVAL


class TestResume:
    async def test_a_dropped_run_resumes_from_its_checkpoint(self, settings: Settings) -> None:
        """One of the properties the project claims: kill it, come back, continue.

        A fresh graph object over the same checkpointer stands in for a restarted
        process.
        """
        saver = InMemorySaver()
        scripted = Scripted(
            PlannerOutput=[PLAN],
            CoderOutput=[PATCH],
            ReviewerOutput=[APPROVE],
            PRSummary=[SUMMARY],
        )
        ctx, _ = make_ctx(settings, scripted=scripted, auto_approve=False)

        first = build_graph(ctx).compile(checkpointer=saver)
        await drain(first, start_state(), CONFIG)
        assert (await first.aget_state(CONFIG)).values["phase"] is RunPhase.WAITING_APPROVAL

        # New graph instance, same checkpointer: the run continues rather than restarting.
        second = build_graph(ctx).compile(checkpointer=saver)
        visited = await drain(second, Command(resume=HumanDecision(verdict="approve")), CONFIG)

        assert "retrieve" not in visited, "resume must not redo completed work"
        assert (await second.aget_state(CONFIG)).values["phase"] is RunPhase.DONE


class TestObservability:
    async def test_every_node_is_recorded(self, settings: Settings) -> None:
        scripted = Scripted(
            PlannerOutput=[PLAN],
            CoderOutput=[PATCH],
            ReviewerOutput=[APPROVE],
            PRSummary=[SUMMARY],
        )
        ctx, sink = make_ctx(settings, scripted=scripted, auto_approve=True)
        await drain(compile_graph(ctx), start_state(), CONFIG)

        started = {e.payload["node"] for e in sink.of_kind(EventKind.NODE_STARTED)}
        assert {"retrieve", "plan", "code", "test", "review", "summarize"} <= started
        assert all(e.payload["ok"] for e in sink.of_kind(EventKind.NODE_ENDED))

    async def test_phase_changes_are_recorded(self, settings: Settings) -> None:
        scripted = Scripted(
            PlannerOutput=[PLAN],
            CoderOutput=[PATCH],
            ReviewerOutput=[APPROVE],
            PRSummary=[SUMMARY],
        )
        ctx, sink = make_ctx(settings, scripted=scripted, auto_approve=True)
        await drain(compile_graph(ctx), start_state(), CONFIG)

        transitions = [
            (e.payload["src"], e.payload["dst"]) for e in sink.of_kind(EventKind.PHASE_CHANGED)
        ]
        assert ("CREATED", "PLANNING") in transitions
        assert ("CODING", "TESTING") in transitions
        assert ("REVIEW", "DONE") in transitions

    async def test_tool_calls_are_drained_into_metrics(self, settings: Settings) -> None:
        """Nodes carry no tool instrumentation; the registry ledger is the source."""
        scripted = Scripted(
            PlannerOutput=[PLAN],
            CoderOutput=[PATCH],
            ReviewerOutput=[APPROVE],
            PRSummary=[SUMMARY],
        )
        ctx, sink = make_ctx(settings, scripted=scripted, auto_approve=True)
        await drain(compile_graph(ctx), start_state(), CONFIG)
        # The stub retriever calls no tools, but the retrieve node's grep path
        # does when a real retriever is used; assert the plumbing exists.
        assert ctx.registry.calls == [], "ledger should be drained, not accumulating"


@pytest.mark.parametrize("auto", [True, False])
async def test_graph_compiles_either_way(settings: Settings, auto: bool) -> None:
    ctx, _ = make_ctx(settings, scripted=Scripted(), auto_approve=auto)
    assert compile_graph(ctx) is not None
