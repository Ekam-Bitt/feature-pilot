"""Test doubles.

Library code, not fixture wiring — conftest.py exposes these as fixtures. Kept
importable so a test can construct a variant directly (a filesystem with
specific contents, a retriever with specific chunks) rather than being limited
to the default fixture.

These exist to make the seam constraint enforceable: every node must be
exercisable against a fake ToolRegistry and a stub Retriever, with no Docker
daemon and no MCP subprocess. If a node can only run against the real thing, the
abstraction is decorative.
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any

from featurepilot.contracts import RetrievedChunk, RetrieverOutput
from featurepilot.tools.registry import Tool, ToolRegistry, ToolResult


class FakeFileSystem:
    """In-memory stand-in for the filesystem MCP server.

    Same tool names and same ToolResult shape as the real server — including its
    edit semantics (exactly one match required) — so a node cannot tell the
    difference. That indistinguishability is the property being tested.
    """

    def __init__(self, files: dict[str, str] | None = None) -> None:
        self.files: dict[str, str] = dict(files or {})
        self.writes: list[str] = []

    async def read_file(self, path: str) -> ToolResult:
        if path not in self.files:
            return ToolResult.error(f"no such file: {path}")
        return ToolResult(self.files[path])

    async def write_file(self, path: str, content: str) -> ToolResult:
        self.files[path] = content
        self.writes.append(path)
        return ToolResult(f"wrote {len(content)} bytes to {path}")

    async def edit_file(self, path: str, old: str, new: str) -> ToolResult:
        if path not in self.files:
            return ToolResult.error(f"no such file: {path}")
        body = self.files[path]
        occurrences = body.count(old)
        if occurrences != 1:
            return ToolResult.error(
                f"expected exactly one occurrence of the old string in {path}, found {occurrences}"
            )
        self.files[path] = body.replace(old, new)
        self.writes.append(path)
        return ToolResult(f"edited {path}")

    async def glob(self, pattern: str) -> ToolResult:
        hits = [p for p in sorted(self.files) if fnmatch.fnmatch(p, pattern)]
        return ToolResult("\n".join(hits))

    async def grep(self, pattern: str) -> ToolResult:
        rx = re.compile(pattern)
        hits = [
            f"{path}:{i + 1}:{line}"
            for path, body in sorted(self.files.items())
            for i, line in enumerate(body.splitlines())
            if rx.search(line)
        ]
        return ToolResult("\n".join(hits))

    def as_registry(self) -> ToolRegistry:
        reg = ToolRegistry()
        reg.register_all(
            [
                Tool(
                    "read_file", "Read a file.", {}, self.read_file, read_only=True, source="fake"
                ),
                Tool("write_file", "Write a file.", {}, self.write_file, source="fake"),
                Tool("edit_file", "Replace a string.", {}, self.edit_file, source="fake"),
                Tool("glob", "Match paths.", {}, self.glob, read_only=True, source="fake"),
                Tool("grep", "Search contents.", {}, self.grep, read_only=True, source="fake"),
            ]
        )
        return reg


DEFAULT_FILES: dict[str, str] = {
    "src/cart.py": "def total(items):\n    return sum(i.price for i in items)\n",
    "tests/test_cart.py": "def test_total():\n    assert total([]) == 0\n",
    "README.md": "# demo\n",
}


class StubRetriever:
    """Satisfies the Retriever protocol with fixed results, so a node can be
    tested for how it *uses* retrieval without any index existing."""

    name = "stub"

    def __init__(self, chunks: list[RetrievedChunk] | None = None) -> None:
        self._chunks = (
            chunks
            if chunks is not None
            else [
                RetrievedChunk(
                    path="src/cart.py",
                    start_line=1,
                    end_line=2,
                    score=1.0,
                    why="stub always returns this",
                    content=DEFAULT_FILES["src/cart.py"],
                )
            ]
        )
        self.queries: list[str] = []
        self.prepared = False

    async def prepare(self) -> None:
        self.prepared = True

    async def retrieve(self, query: str, *, k: int = 8) -> RetrieverOutput:
        self.queries.append(query)
        chunks = self._chunks[:k]
        return RetrieverOutput(
            files=sorted({c.path for c in chunks}),
            chunks=chunks,
            confidence=1.0,
            strategy=self.name,
        )


class ScriptedModel:
    """Returns pre-set contract instances instead of calling a provider.

    Nodes receive their model call as an injected callable, so this replaces it
    wholesale. That's what keeps the default suite free of network calls while
    still exercising real node logic.
    """

    def __init__(self, *responses: Any) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append({"args": args, "kwargs": kwargs})
        if not self._responses:
            raise AssertionError("ScriptedModel exhausted: an unexpected extra call was made")
        return self._responses.pop(0)

    @property
    def call_count(self) -> int:
        return len(self.calls)


class FakeSandbox:
    """Stands in for the container.

    Records snapshot/restore calls, because "does the repair loop start from a
    clean tree" is a property the graph tests need to assert and a real container
    would make slow to check.
    """

    def __init__(self, test_outputs: list[tuple[int, str]] | None = None) -> None:
        #: (exit_code, output) per test run, consumed in order. The default is a
        #: red run followed by a green one — the repair loop's shape.
        self._outputs = test_outputs or [
            (1, "FAILED tests/test_cart.py::test_x - assert 1 == 2\n1 failed, 84 passed in 1s"),
            (0, "85 passed in 1s"),
        ]
        self.restores = 0
        self.snapshots = 0
        self.commands: list[str] = []
        self.diff_text = "--- a/src/shopsvc/cart.py\n+++ b/src/shopsvc/cart.py\n+fixed\n"

    async def snapshot(self) -> None:
        self.snapshots += 1

    async def restore(self) -> None:
        self.restores += 1

    async def diff(self) -> str:
        return self.diff_text

    async def changed_files(self) -> list[str]:
        return ["src/shopsvc/cart.py"]

    async def exec(self, command: str, *, timeout: int | None = None) -> Any:
        from featurepilot.sandbox.runner import ExecResult

        self.commands.append(command)
        exit_code, output = self._outputs.pop(0) if self._outputs else (0, "85 passed in 1s")
        return ExecResult(
            command=command, exit_code=exit_code, stdout=output, stderr="", duration_ms=5
        )
