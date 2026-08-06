"""Node seam.

The graph depends on a shape, not on role names. Every node satisfies
`AgentNode`, which makes PlannerNode, CriticNode, and later MemoryNode /
EvaluatorNode interchangeable as graph vertices.

The concrete payoff is strategy comparison: two PlannerNode implementations can
be selected by config and scored head-to-head on the same fixture issues with
no graph surgery. That matters because "which planning strategy works better"
is an empirical question this project should be able to answer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel

from featurepilot.lifecycle import RunPhase, assert_transition

if TYPE_CHECKING:
    from featurepilot.graph.context import RunContext
    from featurepilot.graph.state import AgentState


@runtime_checkable
class AgentNode[OutT: BaseModel](Protocol):
    """One vertex of the graph.

    Two methods rather than one, deliberately:

    - `invoke` does the work (usually a model call) and returns its typed
      contract from `featurepilot.contracts` — never a dict, never partial state.
    - `apply` turns that contract into a state update, including the phase
      transition.

    Splitting them keeps each testable on its own: `apply` is pure, so the state
    machine can be verified with no model at all, and `invoke` can be checked
    against a scripted model with no assertions about state plumbing.

    Generic in the output type because `apply` *consumes* it: a node whose
    `apply` accepted only `PRSummary` would be narrowing a contravariant
    parameter, which is unsound and which mypy correctly rejects.
    """

    #: Stable identifier used in routing, metrics, and traces.
    name: str

    #: The phase this node runs in. Used for metrics and to assert the run is
    #: where the router thinks it is.
    phase: RunPhase

    async def invoke(self, state: AgentState) -> OutT: ...

    def apply(self, state: AgentState, output: OutT) -> dict[str, object]: ...


class Node:
    """Shared wiring for the concrete nodes.

    Not required by the protocol — a node only has to match the shape — but it
    saves every node repeating the context handshake.
    """

    name: str = "node"
    phase: RunPhase = RunPhase.CREATED

    def __init__(self, ctx: RunContext) -> None:
        self.ctx = ctx

    async def invoke(self, state: AgentState) -> BaseModel:  # pragma: no cover - abstract
        raise NotImplementedError

    def apply(self, state: AgentState, output: BaseModel) -> dict[str, object]:
        """Default: record nothing and stay put. Nodes override."""
        return {}

    # --- helpers ----------------------------------------------------------

    def _next_attempt(self, state: AgentState) -> int:
        return int(state.get("attempt", 0)) + 1

    def _transition(self, state: AgentState, dst: RunPhase) -> RunPhase:
        """Phase changes go through the lifecycle table, so an impossible
        transition raises here rather than silently misrouting the run.

        The source is coerced for the same reason the router coerces it: after a
        checkpoint round-trip it may be a plain string.
        """
        current = state.get("phase", RunPhase.CREATED)
        return assert_transition(RunPhase(str(current)), dst)
