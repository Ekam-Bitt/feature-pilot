"""Tracing spans for the parts LangChain cannot see.

LangGraph instruments itself, so nodes and model calls appear in LangSmith with
no work from us. But roughly half of what this system does is *not* LangChain:
tool executions go through the MCP registry, retrieval goes through a `Retriever`,
and tests run inside a container. Without spans for those, a trace shows the model
asking to read a file and then a mysterious gap where the answer came from.

`@traced` fills the gaps. It degrades to a plain passthrough when the `langsmith`
package is absent or no key is set, so the `tracing` extra stays optional and the
"works with only ANTHROPIC_API_KEY" contract holds.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from typing import Any, Literal, TypeVar

F = TypeVar("F", bound=Callable[..., Any])

#: LangSmith's span kinds. Using the right one makes the UI group and render
#: the span correctly instead of showing everything as a generic step.
RunType = Literal["tool", "chain", "llm", "retriever", "embedding", "prompt", "parser"]


def _enabled() -> bool:
    return os.environ.get("LANGSMITH_TRACING", "").lower() == "true"


def traced(name: str, run_type: RunType = "tool") -> Callable[[F], F]:
    """Record a span for a function LangChain would not otherwise see.


    The decorator is applied at import time but reads `LANGSMITH_TRACING` at call
    time, because `configure_tracing()` runs after imports. Deciding at import
    would mean tracing is always off.
    """

    def decorate(fn: F) -> F:
        try:
            from langsmith import traceable
        except ImportError:
            return fn

        wrapped = traceable(name=name, run_type=run_type)(fn)

        import functools

        if _is_async(fn):

            @functools.wraps(fn)
            async def async_gate(*args: Any, **kwargs: Any) -> Any:
                if not _enabled():
                    return await fn(*args, **kwargs)
                return await wrapped(*args, **kwargs)

            return async_gate  # type: ignore[return-value]

        @functools.wraps(fn)
        def sync_gate(*args: Any, **kwargs: Any) -> Any:
            if not _enabled():
                return fn(*args, **kwargs)
            return wrapped(*args, **kwargs)

        return sync_gate  # type: ignore[return-value]

    return decorate


def _is_async(fn: Callable[..., Any]) -> bool:
    import inspect

    return inspect.iscoroutinefunction(fn)


def flush() -> None:
    """Wait for queued traces to upload.

    The SDK batches uploads on a background thread. A CLI process that exits as
    soon as the run finishes can terminate before that thread drains, and the
    traces are silently lost — the run looks untraced for no visible reason. Long
    server processes never notice; short-lived commands always would.
    """
    if not _enabled():
        return
    try:
        from langsmith import get_cached_client

        get_cached_client().flush()
    except Exception:  # noqa: BLE001 - telemetry must never fail a run
        return
