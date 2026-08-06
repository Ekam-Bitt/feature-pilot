"""ToolRegistry -> LangChain, and back.

The last link in the seam:

    nodes -> ToolRegistry -> [this] -> LangChain bind_tools -> the model

Nodes never import LangChain tool types, so replacing LangChain means rewriting
this one module rather than every node. It is also where the tool list is made
byte-stable, which is a prompt-caching requirement: tools render first in the
prompt, so an unstable order silently drops the cache hit rate to zero with no
error to notice.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from langchain_core.messages import AIMessage, ToolMessage

from featurepilot.tools.registry import ToolRegistry, ToolResult

log = logging.getLogger(__name__)

#: A tool call returning more than this is truncated. Tool output is the largest
#: thing in an agent's context and the least information-dense per character.
MAX_TOOL_CHARS = 40_000


def _schema_for(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalise an MCP input schema into what bind_tools expects.

    MCP servers may advertise a bare `{}` for a no-argument tool; providers
    reject a parameterless schema without an explicit object type.
    """
    schema = dict(raw) if raw else {}
    schema.setdefault("type", "object")
    schema.setdefault("properties", {})
    return schema


def as_langchain_tools(registry: ToolRegistry) -> list[dict[str, Any]]:
    """Render the registry as LangChain/OpenAI-style tool specs.

    Iteration order comes from the registry, which sorts by name — so the
    rendered list is identical across turns and the provider cache can hit.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description,
                "parameters": _schema_for(tool.input_schema),
            },
        }
        for tool in registry
    ]


def _render(result: ToolResult) -> str:
    text = result.content or ("(no output)" if result.ok else "(failed with no output)")
    if len(text) > MAX_TOOL_CHARS:
        # Keep the tail: pytest and tracebacks put the conclusion at the end.
        text = f"[truncated to the last {MAX_TOOL_CHARS} characters]\n" + text[-MAX_TOOL_CHARS:]
    return text


async def execute_tool_calls(registry: ToolRegistry, message: AIMessage) -> list[ToolMessage]:
    """Run every tool call on `message` and return the results in order.

    All calls in one assistant message are answered — dropping any would leave a
    dangling tool_call id, which providers reject. A tool that fails comes back as
    a normal ToolMessage carrying the error text, because a failure the model can
    read is a failure it can recover from.
    """
    results: list[ToolMessage] = []
    for call in message.tool_calls:
        name = call.get("name", "")
        call_id = str(call.get("id") or name)
        args = call.get("args") or {}
        if not isinstance(args, dict):
            # Some providers hand back a JSON string instead of an object.
            try:
                args = json.loads(str(args))
            except (TypeError, ValueError):
                args = {}

        if not registry.has(name):
            results.append(
                ToolMessage(
                    content=f"No tool named {name!r}. Available: {', '.join(registry.names())}",
                    tool_call_id=call_id,
                    status="error",
                )
            )
            continue

        try:
            result = await registry.call(name, **args)
        except TypeError as exc:
            # Wrong argument names — the model can correct this from the message.
            results.append(
                ToolMessage(
                    content=f"{name} rejected those arguments: {exc}",
                    tool_call_id=call_id,
                    status="error",
                )
            )
            continue
        except Exception as exc:  # noqa: BLE001 - a tool failure is data, not a crash
            log.warning("tool %s raised: %s", name, exc)
            results.append(
                ToolMessage(
                    content=f"{name} failed: {exc}",
                    tool_call_id=call_id,
                    status="error",
                )
            )
            continue

        results.append(
            ToolMessage(
                content=_render(result),
                tool_call_id=call_id,
                status="success" if result.ok else "error",
            )
        )
    return results
