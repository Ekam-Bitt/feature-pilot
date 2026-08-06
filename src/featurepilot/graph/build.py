"""Graph assembly.

Every node is wrapped identically: metrics around it, `invoke` for the work,
`apply` for the state transition. Nodes therefore contain no instrumentation and
no routing — which is why they stay testable in isolation.

Routing is one conditional edge per node, all pointing at the same pure `route`
function. There is no supervisor vertex: a vertex would serialise every
transition through an extra hop for a decision that is already determined by
`(phase, last typed output)`.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from langgraph.graph import END, START, StateGraph

from featurepilot.graph.context import RunContext
from featurepilot.graph.nodes.base import AgentNode
from featurepilot.graph.nodes.coder import CoderNode
from featurepilot.graph.nodes.debugger import DebuggerNode
from featurepilot.graph.nodes.planner import ApprovalNode, PlannerNode
from featurepilot.graph.nodes.retrieve import RetrieveNode
from featurepilot.graph.nodes.reviewer import ReviewerNode, SummarizeNode
from featurepilot.graph.nodes.tester import TesterNode
from featurepilot.graph.router import (
    APPROVAL,
    CODE,
    DEBUG,
    FINISH,
    PLAN,
    RETRIEVE,
    REVIEW,
    SUMMARIZE,
    TEST,
    route,
)
from featurepilot.graph.state import AgentState
from featurepilot.lifecycle import RunPhase
from featurepilot.metrics.recorder import BudgetExceeded

log = logging.getLogger(__name__)

NodeFn = Callable[[AgentState], Awaitable[dict[str, Any]]]


def _wrap(node: AgentNode[Any], ctx: RunContext) -> NodeFn:
    """Adapt an AgentNode to what LangGraph expects.

    One place handles metrics, tool-call accounting, and failure translation, so
    adding a node means writing the node — not remembering six pieces of
    boilerplate.
    """

    async def run(state: AgentState) -> dict[str, Any]:
        attempt = int(state.get("attempt", 0))
        async with ctx.recorder.node(node.name, node.phase, attempt):
            try:
                output = await node.invoke(state)
            except BudgetExceeded as exc:
                # Terminal by definition: a retry would also exceed the budget.
                log.warning("run %s stopped on budget: %s", state.get("run_id"), exc)
                return {"phase": RunPhase.FAILED, "error": str(exc)}
            update = node.apply(state, output)

        # Reviewer verdicts feed an acceptance rate. Read from the output here
        # rather than inside the node so the node stays free of instrumentation.
        if (review := update.get("review")) is not None:
            ctx.recorder.record_review(getattr(review, "verdict", "") == "approve")

        # The registry logged every tool call; drain it once per node rather than
        # instrumenting each node individually.
        if ctx.registry.calls:
            drained, ctx.registry.calls[:] = list(ctx.registry.calls), []
            await ctx.recorder.record_tool_calls(drained, node=node.name)

        await _emit_artifacts(ctx, update)

        phase = update.get("phase")
        if isinstance(phase, RunPhase) and phase != state.get("phase"):
            await ctx.recorder.phase_changed(state.get("phase", RunPhase.CREATED), phase)
        return update

    run.__name__ = f"node_{node.name}"
    return run


async def _emit_artifacts(ctx: RunContext, update: dict[str, Any]) -> None:
    """Persist the outputs a human will want to read after the run.

    Done here rather than in each node so adding a node cannot forget it. The
    motivating case: inspecting what a run changed previously meant paying to run
    it again, because the diff existed only in memory.
    """
    code = update.get("code")
    if code is not None and code.diff:
        await ctx.recorder.artifact("diff", code.diff)

    tests = update.get("tests")
    if tests is not None and tests.raw_output:
        await ctx.recorder.artifact("test_output", tests.raw_output)

    context = update.get("context")
    if context is not None and context.chunks:
        rendered = "\n\n".join(
            f"--- {c.path} ({c.why}, score {c.score:.2f}) ---\n{c.content}" for c in context.chunks
        )
        await ctx.recorder.artifact("context", rendered)

    pr = update.get("pr")
    if pr is not None:
        await ctx.recorder.artifact(
            "pr_summary", f"# {pr.title}\n\n{pr.body}\n\n## Test plan\n\n{pr.test_plan}"
        )


def build_graph(ctx: RunContext) -> StateGraph[AgentState, None, AgentState, AgentState]:
    """Wire the graph. Compilation (and the checkpointer) is the caller's job."""
    nodes: dict[str, AgentNode[Any]] = {
        RETRIEVE: RetrieveNode(ctx),
        PLAN: PlannerNode(ctx),
        APPROVAL: ApprovalNode(ctx),
        CODE: CoderNode(ctx),
        TEST: TesterNode(ctx),
        DEBUG: DebuggerNode(ctx),
        REVIEW: ReviewerNode(ctx),
        SUMMARIZE: SummarizeNode(ctx),
    }

    graph = StateGraph(AgentState)
    for name, node in nodes.items():
        # add_node's overloads cannot infer NodeInputT from a bare async
        # callable over a total=False TypedDict; the runtime contract is right.
        graph.add_node(name, _wrap(node, ctx))  # type: ignore[call-overload]

    def _route(state: AgentState) -> str:
        destination = route(state, max_attempts=ctx.settings.max_attempts, stages=ctx.stages)
        return END if destination == FINISH else destination

    graph.add_conditional_edges(START, _route, [*nodes, END])
    for name in nodes:
        graph.add_conditional_edges(name, _route, [*nodes, END])

    return graph
