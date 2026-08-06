"""Coder node.

The one genuinely agentic step: the model works with tools until it is done, then
summarises. Two model calls rather than one, deliberately — a single structured
call would force it to declare its edits before reading the code, and the
read-then-edit dependency is exactly what makes `edit_file`'s exact-match
requirement satisfiable.

Before each attempt the sandbox is restored to its snapshot, so attempt N+1
starts from clean code rather than compounding a broken patch.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from featurepilot import prompts
from featurepilot.config import Role
from featurepilot.contracts import CoderOutput
from featurepilot.graph.nodes.base import Node
from featurepilot.graph.nodes.planner import render_context
from featurepilot.graph.state import AgentState
from featurepilot.lifecycle import RunPhase

#: The coder needs to read, search, and write. It deliberately does not get
#: run_tests: testing is the tester's job, and letting the coder test invites it
#: to iterate silently past the attempt budget the router is meant to enforce.
CODER_TOOLS = ("read_file", "edit_file", "write_file", "glob", "grep")


def _plan_brief(state: AgentState) -> str:
    plan = state.get("plan")
    if plan is None:
        return "No plan is available. Work directly from the issue."
    lines = [f"Goal: {plan.summary}", ""]
    for i, step in enumerate(plan.steps, start=1):
        files = f"  (files: {', '.join(step.files)})" if step.files else ""
        lines.append(f"{i}. {step.description}{files}")
    if plan.open_questions:
        decision = state.get("decision")
        # A human may answer some, all, or none of the questions, so index with a
        # default rather than assuming the lists line up.
        given = decision.answers if decision else []
        lines.append("")
        lines.append("Answers to the questions raised while planning:")
        for i, question in enumerate(plan.open_questions):
            answer = given[i] if i < len(given) else ""
            lines.append(f"- {question} -> {answer or '(unanswered; use your judgement)'}")
    return "\n".join(lines)


def _repair_brief(state: AgentState) -> str | None:
    """What the debugger concluded, if this is a repair attempt."""
    diagnosis = state.get("diagnosis")
    if diagnosis is None:
        return None
    lines = [
        "This is a repair attempt. A previous patch failed its tests and the "
        "working tree has been restored to its original state, so make the "
        "complete correct change rather than a delta against the broken one.",
        "",
        f"Failure category: {diagnosis.failure_category}",
        f"Root cause: {diagnosis.root_cause}",
    ]
    if diagnosis.suggested_edits:
        lines.append("")
        lines.append("Suggested edits:")
        lines.extend(f"- {e.path}: {e.rationale}" for e in diagnosis.suggested_edits)
    review = state.get("review")
    if review and review.verdict == "reject" and review.blocking:
        lines.append("")
        lines.append("A reviewer also blocked the previous attempt for:")
        lines.extend(f"- {item}" for item in review.blocking)
    return "\n".join(lines)


class CoderNode(Node):
    name = "code"
    phase = RunPhase.CODING

    async def invoke(self, state: AgentState) -> CoderOutput:
        attempt = int(state.get("attempt", 0))

        # Restore before a repair attempt so the coder never sees the failed
        # patch. Without this, attempt 2 edits code attempt 1 already broke.
        if attempt > 0 and self.ctx.sandbox is not None:
            await self.ctx.sandbox.restore()

        parts = [
            f"## Issue\n\n{state.get('issue', '')}",
            f"## Approved plan\n\n{_plan_brief(state)}",
            f"## Code retrieved from the repository\n\n{render_context(state)}",
        ]
        if repair := _repair_brief(state):
            parts.append(f"## Why the previous attempt failed\n\n{repair}")

        messages: list[object] = [
            SystemMessage(content=prompts.load("coder")),
            HumanMessage(content="\n\n".join(parts)),
        ]

        tools = self.ctx.tools_for(*CODER_TOOLS)
        # The final attempt escalates: another failed attempt now costs more than
        # a better model would.
        escalate = attempt >= self.ctx.settings.max_attempts - 1
        transcript = await self.ctx.tool_loop(Role.CODER, messages, tools, escalate=escalate)

        summary = await self.ctx.call(
            Role.CODER,
            CoderOutput,
            [
                *transcript,
                HumanMessage(
                    content=(
                        "Summarise the change you just made: every file you edited "
                        "with the reason it needed changing, and any assumption a "
                        "reviewer should verify. Do not make further edits."
                    )
                ),
            ],
            escalate=escalate,
        )

        # The diff is authoritative from the sandbox, not from the model's
        # recollection — a model-reported diff is a hallucination risk on the one
        # artifact a human is going to read most closely.
        #
        # Copied rather than mutated in place: a node should not modify the object
        # it was handed, and `ctx.call` is free to return a shared instance.
        if self.ctx.sandbox is not None:
            return summary.model_copy(update={"diff": await self.ctx.sandbox.diff()})
        return summary

    def apply(self, state: AgentState, output: CoderOutput) -> dict[str, object]:  # type: ignore[override]
        return {
            "code": output,
            "attempt": self._next_attempt(state),
            "phase": self._transition(state, RunPhase.TESTING),
            # A fresh attempt invalidates the previous verdicts; leaving them
            # would let the router act on a stale rejection.
            "tests": None,
            "diagnosis": None,
            "review": None,
        }
