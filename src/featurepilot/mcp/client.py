"""MCP session management and dynamic tool discovery.

This is the layer that makes "agents discover tools through MCP" literal rather
than decorative: nothing here names a tool. Each server is spawned as a stdio
subprocess, asked what it offers via `list_tools()`, and whatever comes back is
registered into the `ToolRegistry`. Adding a tool to a server is a zero-code
change on the agent side — no schema to copy, no dispatch table to extend.

    MCP stdio servers -> MCPToolLoader -> ToolRegistry -> nodes

Sessions are held open for the run's lifetime. Spawning a subprocess per tool
call would cost more than the calls themselves.
"""

from __future__ import annotations

import logging
import sys
from contextlib import AsyncExitStack
from dataclasses import dataclass
from types import TracebackType
from typing import Any

from mcp import ClientSession, StdioServerParameters, stdio_client

from featurepilot.mcp import is_error_text
from featurepilot.tools.registry import Tool, ToolRegistry, ToolResult

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ServerSpec:
    """How to start one MCP server."""

    name: str
    module: str

    def params(self, run_id: str, env: dict[str, str] | None = None) -> StdioServerParameters:
        return StdioServerParameters(
            command=sys.executable,
            args=["-m", self.module],
            # FP_RUN_ID tells the server which sandbox to attach to. Passing the
            # parent environment through keeps provider keys and PATH intact.
            env={**(env or {}), "FP_RUN_ID": run_id},
        )


#: The Phase 1A server set. GitHub, Postgres and docs-search join in Phase 2 by
#: appending here — the loader needs no change to pick them up.
DEFAULT_SERVERS: tuple[ServerSpec, ...] = (
    ServerSpec(name="filesystem", module="featurepilot.mcp.servers.filesystem_server"),
    ServerSpec(name="terminal", module="featurepilot.mcp.servers.terminal_server"),
)


def _text_of(result: Any) -> str:
    """Flatten an MCP tool result into text.

    Content blocks are a union and servers may return several; anything without
    a `.text` is described rather than dropped, so a non-text block shows up as a
    visible placeholder instead of silently vanishing.
    """
    content = getattr(result, "content", None) or []
    parts: list[str] = []
    for block in content:
        if (text := getattr(block, "text", None)) is not None:
            parts.append(str(text))
        else:
            parts.append(f"[{getattr(block, 'type', 'unknown')} content]")
    # rstrip only: leading whitespace is meaningful for code and for the
    # line-number gutter that read_file emits.
    return "\n".join(parts).rstrip()


class MCPToolLoader:
    """Owns the MCP subprocesses for one run and populates a ToolRegistry."""

    def __init__(
        self,
        run_id: str,
        *,
        servers: tuple[ServerSpec, ...] = DEFAULT_SERVERS,
        env: dict[str, str] | None = None,
    ) -> None:
        self.run_id = run_id
        self.servers = servers
        self.env = env
        self._stack = AsyncExitStack()
        self._sessions: dict[str, ClientSession] = {}

    async def __aenter__(self) -> MCPToolLoader:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._stack.aclose()
        self._sessions.clear()

    async def connect(self) -> None:
        """Start every server and complete the MCP handshake."""
        for spec in self.servers:
            read, write = await self._stack.enter_async_context(
                stdio_client(spec.params(self.run_id, self.env))
            )
            session = await self._stack.enter_async_context(ClientSession(read, write))
            await session.initialize()
            self._sessions[spec.name] = session
            log.debug("mcp server %s connected", spec.name)

    async def discover(self, registry: ToolRegistry | None = None) -> ToolRegistry:
        """Ask every connected server what it offers and register it.

        Note what is absent: no mapping from tool name to a local function. The
        registry is built from the servers' own advertisements.
        """
        registry = registry or ToolRegistry()
        for server_name, session in self._sessions.items():
            listing = await session.list_tools()
            for mcp_tool in listing.tools:
                registry.register(
                    self._as_tool(server_name, session, mcp_tool),
                    # Later servers may legitimately shadow earlier ones; the
                    # registry logs the source so a surprise is traceable.
                    replace=True,
                )
            log.info(
                "discovered %d tools from mcp server %s: %s",
                len(listing.tools),
                server_name,
                ", ".join(t.name for t in listing.tools),
            )
        return registry

    def _as_tool(self, server_name: str, session: ClientSession, mcp_tool: Any) -> Tool:
        annotations = getattr(mcp_tool, "annotations", None)
        read_only = bool(getattr(annotations, "read_only_hint", False))

        async def invoke(**kwargs: Any) -> ToolResult:
            try:
                result = await session.call_tool(mcp_tool.name, kwargs)
            except Exception as exc:  # noqa: BLE001 - a tool failure is data
                return ToolResult.error(f"{mcp_tool.name} failed: {exc}")
            text = _text_of(result)
            # Two failure channels: `is_error` for protocol-level problems, and
            # the ERROR_PREFIX text convention for recoverable ones the model is
            # meant to read (see featurepilot.mcp).
            failed = bool(getattr(result, "is_error", False)) or is_error_text(text)
            return ToolResult(content=text, ok=not failed)

        return Tool(
            name=mcp_tool.name,
            description=(mcp_tool.description or "").strip(),
            input_schema=getattr(mcp_tool, "input_schema", None) or {},
            fn=invoke,
            read_only=read_only,
            source=f"mcp:{server_name}",
        )


async def registry_for_run(
    run_id: str,
    *,
    servers: tuple[ServerSpec, ...] = DEFAULT_SERVERS,
    env: dict[str, str] | None = None,
) -> tuple[ToolRegistry, MCPToolLoader]:
    """Connect, discover, and hand back both the registry and its loader.

    The loader is returned rather than hidden because the caller has to close it
    — the subprocesses live as long as the run does.
    """
    loader = MCPToolLoader(run_id, servers=servers, env=env)
    await loader.connect()
    registry = await loader.discover()
    return registry, loader
