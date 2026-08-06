"""Domain-level tool interface.

Nodes depend on this and nothing below it:

    nodes -> ToolRegistry -> LangChainToolAdapter -> MCPToolLoader -> MCP servers

Two payoffs. Replacing LangChain means writing one adapter rather than touching
every node. And tests can populate a registry with fakes, so a node is
exercisable with no MCP process and no container running — which is the
`tests/test_seams.py` constraint that proves this layer isn't decorative.

Tool *names* here are a stable domain vocabulary (`read_file`, `run_command`).
Whether they arrive from an MCP server, a local Python function, or a stub is
not a node's concern.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from featurepilot.tracing import traced

JsonSchema = dict[str, Any]
ToolFn = Callable[..., Awaitable["ToolResult"]]


@dataclass(slots=True)
class ToolResult:
    """Uniform tool return. `ok=False` carries the error as content rather than
    raising, so a model can read the failure and adapt instead of the graph
    dying on a recoverable problem (a missing file, a failing command)."""

    content: str
    ok: bool = True
    meta: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def error(cls, message: str, **meta: Any) -> ToolResult:
        return cls(content=message, ok=False, meta=meta)


@dataclass(slots=True)
class Tool:
    name: str
    description: str
    input_schema: JsonSchema
    fn: ToolFn
    #: Read-only tools are safe to run in parallel and need no approval gate.
    read_only: bool = False
    #: Where this came from ("mcp:filesystem", "fake", ...). Metrics + debugging.
    source: str = "local"

    async def __call__(self, **kwargs: Any) -> ToolResult:
        return await self.fn(**kwargs)


class ToolNotFound(KeyError):
    def __init__(self, name: str, available: list[str]) -> None:
        super().__init__(f"no tool named {name!r}; registered: {sorted(available)}")
        self.name = name


class ToolRegistry:
    """A name -> Tool map with an async invoke path.

    Intentionally not a singleton: one registry is built per run, so a run's
    tools are scoped to its own sandbox and worktree.
    """

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}
        #: Appended on every invocation. The metrics recorder drains this, so
        #: tool-call accounting needs no instrumentation inside the nodes.
        self.calls: list[dict[str, Any]] = []

    # --- population -------------------------------------------------------

    def register(self, tool: Tool, *, replace: bool = False) -> None:
        if tool.name in self._tools and not replace:
            raise ValueError(
                f"tool {tool.name!r} already registered from "
                f"{self._tools[tool.name].source!r}; pass replace=True to override"
            )
        self._tools[tool.name] = tool

    def register_all(self, tools: list[Tool], *, replace: bool = False) -> None:
        for tool in tools:
            self.register(tool, replace=replace)

    # --- access -----------------------------------------------------------

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolNotFound(name, list(self._tools)) from None

    def has(self, name: str) -> bool:
        return name in self._tools

    def names(self) -> list[str]:
        return sorted(self._tools)

    def subset(self, names: list[str]) -> ToolRegistry:
        """A registry exposing only `names`.

        Used to give each node the narrow tool surface it needs — the Planner
        gets read-only tools, the Coder gets writes. Narrower surfaces measurably
        reduce wrong-tool selection.
        """
        child = ToolRegistry()
        for name in names:
            child.register(self.get(name))
        child.calls = self.calls  # share the ledger so accounting stays whole
        return child

    def __iter__(self) -> Iterator[Tool]:
        # Sorted so the rendered tool list is byte-stable across turns.
        # Unstable ordering silently destroys prompt-cache hit rate.
        return iter(sorted(self._tools.values(), key=lambda t: t.name))

    def __len__(self) -> int:
        return len(self._tools)

    # --- invocation -------------------------------------------------------

    @traced("tool_call", run_type="tool")
    async def call(self, name: str, **kwargs: Any) -> ToolResult:
        tool = self.get(name)
        result = await tool(**kwargs)
        self.calls.append(
            {
                "tool": name,
                "source": tool.source,
                "args": kwargs,
                "ok": result.ok,
                "content_len": len(result.content),
            }
        )
        return result
