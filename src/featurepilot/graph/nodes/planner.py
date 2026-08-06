"""Planner and the human approval gate.

The planner gets a read-only tool surface. That is not decoration: a planner that
can write files will eventually write one, and a "plan" that has already half-
implemented itself makes the approval gate meaningless.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage
from langgraph.types import interrupt

from featurepilot import prompts
from featurepilot.config import Role
from featurepilot.contracts import HumanDecision, PlannerOutput
from featurepilot.graph.nodes.base import Node
from featurepilot.graph.state import AgentState
from featurepilot.lifecycle import RunPhase

#: Read-only. The planner reads code and reasons; it does not touch it.
PLANNER_TOOLS = ("read_file", "glob", "grep")


def render_context(state: AgentState, *, max_chars: int = 24_000) -> str:
    """Format retrieved chunks for a prompt.

    Bounded because context crowds out reasoning: past a point, more retrieved
    code makes the model worse, not better. Chunks arrive ranked, so truncating
    the tail drops the least relevant material first.
    """
    context = state.get("context")
    if context is None or not context.chunks:
        return "No code was retrieved. Search the repository before planning."

    blocks: list[str] = []
    budget = max_chars
    for chunk in context.chunks:
        body = chunk.content
        header = f"--- {chunk.path} ({chunk.why}) ---"
        if len(body) + len(header) > budget:
            remaining = max(0, budget - len(header) - 40)
            if remaining < 200:
                blocks.append(f"{header}\n[omitted — read this file if you need it]")
                continue
            body = body[:remaining] + "\n[... truncated, read the file for the rest]"
        block = f"{header}\n{body}"
        blocks.append(block)
        budget -= len(block)
    return "\n\n".join(blocks)


class PlannerNode(Node):
    name = "plan"
    phase = RunPhase.PLANNING

    async def invoke(self, state: AgentState) -> PlannerOutput:
        messages: list[object] = [
            SystemMessage(content=prompts.load("planner")),
            HumanMessage(
                content=(
                    f"## Issue\n\n{state.get('issue', '')}\n\n"
                    f"## Code retrieved from the repository\n\n{render_context(state)}"
                )
            ),
        ]
        # A rejected plan comes back here; give the model the objection rather
        # than letting it produce the same plan again.
        if (decision := state.get("decision")) and decision.verdict == "reject":
            previous = state.get("plan")
            messages.append(
                HumanMessage(
                    content=(
                        "Your previous plan was rejected by a reviewer.\n\n"
                        f"Previous plan: {previous.summary if previous else '(unavailable)'}\n\n"
                        f"Their feedback: {decision.feedback or '(none given)'}\n\n"
                        "Produce a revised plan that addresses it."
                    )
                )
            )
        return await self.ctx.call(Role.PLANNER, PlannerOutput, messages)

    def apply(self, state: AgentState, output: PlannerOutput) -> dict[str, object]:  # type: ignore[override]
        # Skipping the gate is only safe when nothing is genuinely ambiguous;
        # open questions always park on a human, --yes or not.
        skip = self.ctx.auto_approve and not output.open_questions
        target = RunPhase.CODING if skip else RunPhase.WAITING_APPROVAL
        return {
            "plan": output,
            "phase": self._transition(state, target),
            # Clear any prior rejection so the router does not re-read it and
            # bounce straight back into planning.
            "decision": HumanDecision(verdict="approve") if skip else None,
        }


class ApprovalNode(Node):
    """The human-in-the-loop gate.

    `interrupt()` suspends the graph and persists the checkpoint, so the process
    can exit entirely while a person reads the plan. Resuming feeds the decision
    back in as this call's return value.
    """

    name = "approval"
    phase = RunPhase.WAITING_APPROVAL

    async def invoke(self, state: AgentState) -> HumanDecision:
        plan = state.get("plan")
        summary = plan.summary if plan else ""
        payload: dict[str, object] = {
            "kind": "plan_approval",
            "summary": summary,
            "steps": [s.model_dump() for s in plan.steps] if plan else [],
            "files": list(plan.files_needed) if plan else [],
            "open_questions": list(plan.open_questions) if plan else [],
            "confidence": plan.confidence if plan else "low",
        }
        await self.ctx.recorder.awaiting_human("plan_approval", summary)

        raw = interrupt(payload)
        # Resume values arrive from the API/CLI, so validate rather than trust.
        if isinstance(raw, HumanDecision):
            return raw
        if isinstance(raw, dict):
            return HumanDecision.model_validate(raw)
        if isinstance(raw, str):
            approved = raw.strip().lower() in {"y", "yes", "approve", ""}
            return HumanDecision(verdict="approve" if approved else "reject")
        return HumanDecision(verdict="approve")

    def apply(self, state: AgentState, output: HumanDecision) -> dict[str, object]:  # type: ignore[override]
        target = RunPhase.PLANNING if output.verdict == "reject" else RunPhase.CODING
        return {"decision": output, "phase": self._transition(state, target)}
