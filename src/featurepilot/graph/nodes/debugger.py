"""Debugger node.

Diagnoses a red suite and decides whether another attempt is worth making. It
gets read-only tools: its job is to explain the failure, not to fix it — the
coder does that, from a clean tree, on the next pass.

The `retry` decision is the one that matters. Being optimistically wrong costs a
whole attempt to arrive back at the same place, so the prompt pushes toward
admitting when it is stuck.
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage, SystemMessage

from featurepilot import prompts
from featurepilot.config import Role
from featurepilot.contracts import DebuggerOutput
from featurepilot.graph.nodes.base import Node
from featurepilot.graph.nodes.describe import describe_tests
from featurepilot.graph.state import AgentState
from featurepilot.lifecycle import RunPhase

DEBUGGER_TOOLS = ("read_file", "glob", "grep")

#: Enough failure detail to diagnose, not so much that it crowds out reasoning.
MAX_TEST_OUTPUT = 12_000


def _failure_brief(state: AgentState) -> str:
    tests = state.get("tests")
    if tests is None:
        return "No test results are available."
    lines = [describe_tests(tests, detail=True)]
    raw = tests.raw_output
    if len(raw) > MAX_TEST_OUTPUT:
        # Keep the tail — pytest puts the summary and tracebacks at the end.
        raw = f"[trimmed to the last {MAX_TEST_OUTPUT} characters]\n" + raw[-MAX_TEST_OUTPUT:]
    lines += ["", "Test output:", raw]
    return "\n".join(lines)


class DebuggerNode(Node):
    name = "debug"
    phase = RunPhase.DEBUGGING

    async def invoke(self, state: AgentState) -> DebuggerOutput:
        code = state.get("code")
        attempt = int(state.get("attempt", 0))
        remaining = max(0, self.ctx.settings.max_attempts - attempt)

        parts = [
            f"## Issue\n\n{state.get('issue', '')}",
            (
                "## The patch that was applied\n\n```diff\n"
                f"{(code.diff if code else '') or '(no diff captured)'}\n```"
            ),
            f"## Test results\n\n{_failure_brief(state)}",
            (
                f"## Budget\n\n{remaining} coding attempt(s) remain after this "
                "diagnosis. Set retry to false if another attempt would not "
                "plausibly succeed."
            ),
        ]
        if code and code.assumptions:
            parts.append(
                "## Assumptions the coder recorded\n\n"
                + "\n".join(f"- {a}" for a in code.assumptions)
            )

        messages: list[object] = [
            SystemMessage(content=prompts.load("debugger")),
            HumanMessage(content="\n\n".join(parts)),
        ]
        return await self.ctx.call(Role.DEBUGGER, DebuggerOutput, messages)

    def apply(self, state: AgentState, output: DebuggerOutput) -> dict[str, object]:  # type: ignore[override]
        attempt = int(state.get("attempt", 0))
        exhausted = attempt >= self.ctx.settings.max_attempts

        if not output.retry or exhausted:
            reason = (
                f"attempt budget exhausted after {attempt} attempts"
                if exhausted
                else f"not retryable ({output.failure_category}): {output.root_cause}"
            )
            return {
                "diagnosis": output,
                "phase": self._transition(state, RunPhase.FAILED),
                "error": reason,
            }
        return {"diagnosis": output, "phase": self._transition(state, RunPhase.CODING)}
