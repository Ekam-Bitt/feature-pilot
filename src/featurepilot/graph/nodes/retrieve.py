"""Retrieval node.

Delegates to whatever `Retriever` the context holds and does not care which one.
That indirection is the whole reason Phase 1B is a config change: this node is
identical whether it is being served by grep or by a reranked hybrid index.

No model call — retrieval is mechanical here, so it costs nothing.
"""

from __future__ import annotations

from featurepilot.contracts import RetrieverOutput
from featurepilot.graph.nodes.base import Node
from featurepilot.graph.state import AgentState
from featurepilot.lifecycle import RunPhase


class RetrieveNode(Node):
    name = "retrieve"
    phase = RunPhase.PLANNING

    async def invoke(self, state: AgentState) -> RetrieverOutput:
        query = state.get("issue", "")
        # A rejected plan re-enters planning; fold the human's feedback into the
        # query so the second attempt searches for what they actually pointed at.
        decision = state.get("decision")
        if decision and decision.verdict == "reject" and decision.feedback:
            query = f"{query}\n\n{decision.feedback}"
        return await self.ctx.retriever.retrieve(query, k=self.ctx.settings.retrieval_top_k)

    def apply(self, state: AgentState, output: RetrieverOutput) -> dict[str, object]:  # type: ignore[override]
        current = state.get("phase", RunPhase.CREATED)
        # CREATED -> PLANNING on the first pass; a re-plan is already in PLANNING.
        phase = (
            current if current is RunPhase.PLANNING else self._transition(state, RunPhase.PLANNING)
        )
        return {"context": output, "phase": phase}
