"""Per-run dependencies.

LangGraph nodes are callables of `(state) -> update`, which leaves nowhere to
pass a sandbox or a tool registry. Rather than reaching for module-level globals
— which would make two concurrent runs share a container — each node is
constructed with this context and closes over it.

That also keeps the `AgentNode` protocol honest: a node's collaborators are
explicit constructor arguments, so a test can hand it fakes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol

from featurepilot.config import Role, Settings
from featurepilot.graph.router import FULL, Stages
from featurepilot.retrieval.base import Retriever
from featurepilot.tools.registry import ToolRegistry

if TYPE_CHECKING:
    from pydantic import BaseModel

    from featurepilot.metrics.recorder import MetricsRecorder
    from featurepilot.sandbox.runner import Sandbox


class StructuredCall(Protocol):
    """The one model-calling seam.

    Nodes never import the LLM module directly; they call this. Tests replace it
    with a scripted stand-in, which is what keeps the default suite free of
    network calls while still exercising real node logic.
    """

    async def __call__[T: BaseModel](
        self,
        role: Role,
        output_model: type[T],
        messages: list[object],
        *,
        escalate: bool = False,
    ) -> T: ...


class ToolLoopCall(Protocol):
    """The agentic seam: let a model work with tools until it stops.

    Separate from `StructuredCall` because the two do different things — this one
    returns a transcript (including tool results), not a contract. The coder needs
    both: work with tools, then summarise what it did.
    """

    async def __call__(
        self,
        role: Role,
        messages: list[object],
        registry: ToolRegistry,
        *,
        escalate: bool = False,
        max_iterations: int = 12,
    ) -> list[object]: ...


@dataclass(slots=True)
class RunContext:
    settings: Settings
    registry: ToolRegistry
    retriever: Retriever
    recorder: MetricsRecorder
    call: StructuredCall
    tool_loop: ToolLoopCall
    sandbox: Sandbox | None = None
    #: Skip the human approval gate (`--yes`). The plan still records
    #: open_questions; they just don't block.
    auto_approve: bool = False
    #: Which stages are active. Defaults to the whole pipeline; the ablation
    #: harness varies it to measure what each stage contributes.
    stages: Stages = FULL
    #: Populated by nodes so the reviewer and PR summary can mention what the
    #: coder assumed without re-deriving it.
    notes: list[str] = field(default_factory=list)

    def tools_for(self, *names: str) -> ToolRegistry:
        """Narrow tool surface for one node.

        The planner should not be able to write files. Narrower surfaces
        measurably reduce wrong-tool selection, and they make an accidental write
        during planning impossible rather than merely unlikely.
        """
        return self.registry.subset([n for n in names if self.registry.has(n)])
