"""Reviewer and PR summariser.

The reviewer runs on a *green* suite, which is the point: passing tests are
necessary and not sufficient. The failure mode it exists to catch is a patch that
edited a test to agree with the code — green, and worthless.

It gets read-only tools so it can check the blast radius (other callers of
anything that moved) rather than only reading the diff.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from featurepilot import prompts
from featurepilot.config import Role
from featurepilot.contracts import PRSummary, ReviewerOutput
from featurepilot.graph.nodes.base import Node
from featurepilot.graph.nodes.describe import describe_tests, verdict_hint
from featurepilot.graph.state import AgentState
from featurepilot.lifecycle import RunPhase

REVIEWER_TOOLS = ("read_file", "glob", "grep")


def _diff_of(state: AgentState) -> str:
    code = state.get("code")
    return (code.diff if code else "") or "(no diff was captured)"


def _touched_tests(state: AgentState) -> list[str]:
    """Files under a tests/ path that the patch modified.

    Surfaced explicitly because "did it edit the tests" is the single most
    important question about a green patch, and a reviewer reading a long diff
    can miss it.
    """
    code = state.get("code")
    if code is None:
        return []
    return [e.path for e in code.edits if "test" in e.path.lower()]


class ReviewerNode(Node):
    name = "review"
    phase = RunPhase.REVIEW

    async def invoke(self, state: AgentState) -> ReviewerOutput:
        tests = state.get("tests")
        plan = state.get("plan")
        code = state.get("code")

        parts = [
            f"## Issue\n\n{state.get('issue', '')}",
            f"## Plan that was approved\n\n{plan.summary if plan else '(none)'}",
            f"## The patch\n\n```diff\n{_diff_of(state)}\n```",
            f"## Test results\n\n{describe_tests(tests)}",
            f"## Where that leaves the patch\n\n{verdict_hint(tests)}",
        ]
        if code and code.assumptions:
            parts.append(
                "## Assumptions the coder recorded\n\n"
                + "\n".join(f"- {a}" for a in code.assumptions)
            )
        if touched := _touched_tests(state):
            parts.append(
                "## Note\n\nThis patch modified test files: "
                + ", ".join(touched)
                + ". Check whether it changed a test to agree with the code rather "
                "than fixing the code to satisfy the requirement."
            )
        context = state.get("context")
        if context and context.confidence < 0.3:
            parts.append(
                "## Note\n\nRetrieval confidence was low, so the coder may have "
                "worked from thin context. Look harder at whether relevant code "
                "was missed."
            )

        messages: list[object] = [
            SystemMessage(content=prompts.load("reviewer")),
            HumanMessage(content="\n\n".join(parts)),
        ]
        return await self.ctx.call(Role.REVIEWER, ReviewerOutput, messages)

    def apply(self, state: AgentState, output: ReviewerOutput) -> dict[str, object]:  # type: ignore[override]
        attempt = int(state.get("attempt", 0))
        rejected = output.verdict == "reject" and bool(output.blocking)
        if rejected and attempt < self.ctx.settings.max_attempts:
            return {"review": output, "phase": self._transition(state, RunPhase.CODING)}
        # Out of attempts, or approved. Either way the run proceeds to a summary;
        # a rejected-but-final patch is handed back with its objections recorded
        # rather than silently discarded.
        return {"review": output, "phase": state.get("phase", RunPhase.REVIEW)}


class SummarizeNode(Node):
    """Writes the PR description. The run's terminal step."""

    name = "summarize"
    phase = RunPhase.REVIEW

    async def invoke(self, state: AgentState) -> PRSummary:
        review = state.get("review")
        tests = state.get("tests")
        code = state.get("code")

        parts = [
            f"## Issue\n\n{state.get('issue', '')}",
            f"## The patch\n\n```diff\n{_diff_of(state)}\n```",
            f"## Test results\n\n{describe_tests(tests)}",
            (
                "## Note\n\nDescribe only what this patch changed. Tests that were "
                "already failing before it are caused by unrelated defects and are "
                "not this pull request's concern — do not present them as unresolved "
                "problems with this change."
            ),
        ]
        if code and code.assumptions:
            parts.append(
                "## Assumptions to mention\n\n" + "\n".join(f"- {a}" for a in code.assumptions)
            )
        if review and review.reasons:
            parts.append(
                "## Reviewer observations\n\n" + "\n".join(f"- {r}" for r in review.reasons)
            )
        if review and review.verdict == "reject" and review.blocking:
            parts.append(
                "## Unresolved review objections\n\n"
                + "\n".join(f"- {b}" for b in review.blocking)
                + "\n\nSay plainly in the body that these remain unresolved."
            )

        messages: list[object] = [
            SystemMessage(content=prompts.load("pr_summary")),
            HumanMessage(content="\n\n".join(parts)),
        ]
        return await self.ctx.call(Role.SUMMARIZER, PRSummary, messages)

    def apply(self, state: AgentState, output: PRSummary) -> dict[str, object]:  # type: ignore[override]
        return {"pr": output, "phase": self._transition(state, RunPhase.DONE)}
