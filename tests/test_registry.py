"""ToolRegistry behaviour, including the two properties the rest of the system
quietly depends on: stable iteration order (prompt caching) and a shared call
ledger (metrics without per-node instrumentation)."""

from __future__ import annotations

import pytest

from fakes import FakeFileSystem
from featurepilot.tools.registry import Tool, ToolNotFound, ToolRegistry, ToolResult


async def _ok(**_kwargs: object) -> ToolResult:
    """Accepts any kwargs — the registry forwards caller args verbatim."""
    return ToolResult("ok")


def _tool(name: str, *, read_only: bool = False) -> Tool:
    return Tool(name, f"{name} description", {}, _ok, read_only=read_only)


class TestRegistration:
    def test_duplicate_registration_is_rejected(self) -> None:
        """Two servers advertising the same tool name is a real misconfiguration
        — surfacing it beats silently preferring whichever loaded last."""
        reg = ToolRegistry()
        reg.register(_tool("read_file"))
        with pytest.raises(ValueError, match="already registered"):
            reg.register(_tool("read_file"))

    def test_replace_is_explicit(self) -> None:
        reg = ToolRegistry()
        reg.register(_tool("read_file"))
        reg.register(_tool("read_file"), replace=True)
        assert len(reg) == 1

    def test_missing_tool_error_lists_alternatives(self) -> None:
        reg = ToolRegistry()
        reg.register(_tool("grep"))
        with pytest.raises(ToolNotFound, match="grep"):
            reg.get("gerp")


class TestIterationOrder:
    def test_iteration_is_sorted_regardless_of_insertion_order(self) -> None:
        """Byte-stability of the rendered tool list is a prompt-cache
        requirement. Unstable ordering silently drops the hit rate to zero, with
        no error to notice — so it gets a test."""
        a, b = ToolRegistry(), ToolRegistry()
        for name in ["write_file", "grep", "read_file"]:
            a.register(_tool(name))
        for name in ["read_file", "write_file", "grep"]:
            b.register(_tool(name))
        assert [t.name for t in a] == [t.name for t in b] == ["grep", "read_file", "write_file"]


class TestSubset:
    def test_subset_narrows_the_surface(self) -> None:
        """Nodes get only the tools their role needs — the planner shouldn't be
        able to write files."""
        reg = ToolRegistry()
        reg.register_all([_tool("read_file", read_only=True), _tool("write_file")])
        planner_tools = reg.subset(["read_file"])
        assert planner_tools.names() == ["read_file"]
        assert not planner_tools.has("write_file")

    async def test_subset_shares_the_call_ledger(self) -> None:
        """Accounting must stay whole across subsets, or per-node tool surfaces
        would silently under-report tool usage."""
        reg = ToolRegistry()
        reg.register(_tool("read_file"))
        child = reg.subset(["read_file"])
        await child.call("read_file")
        assert len(reg.calls) == 1

    def test_subset_of_unknown_tool_raises(self) -> None:
        reg = ToolRegistry()
        with pytest.raises(ToolNotFound):
            reg.subset(["nope"])


class TestInvocation:
    async def test_call_records_the_invocation(self) -> None:
        reg = ToolRegistry()
        reg.register(_tool("grep"))
        await reg.call("grep", pattern="x")
        (entry,) = reg.calls
        assert entry["tool"] == "grep"
        assert entry["args"] == {"pattern": "x"}
        assert entry["ok"] is True

    async def test_failures_are_returned_not_raised(self) -> None:
        """A missing file or failing command is information the model should
        read and adapt to, not an exception that kills the run."""
        fs = FakeFileSystem({})
        reg = fs.as_registry()
        result = await reg.call("read_file", path="nope.py")
        assert result.ok is False
        assert "no such file" in result.content
        assert reg.calls[0]["ok"] is False


class TestFakeFileSystemFidelity:
    """The fake has to behave like the real server or the seam tests prove
    nothing. Its edit semantics in particular must match."""

    async def test_edit_requires_a_unique_match(self) -> None:
        fs = FakeFileSystem({"a.py": "x = 1\nx = 1\n"})
        reg = fs.as_registry()
        result = await reg.call("edit_file", path="a.py", old="x = 1", new="x = 2")
        assert result.ok is False
        assert "found 2" in result.content

    async def test_edit_applies_and_tracks_writes(self) -> None:
        fs = FakeFileSystem({"a.py": "x = 1\n"})
        reg = fs.as_registry()
        assert (await reg.call("edit_file", path="a.py", old="x = 1", new="x = 2")).ok
        assert fs.files["a.py"] == "x = 2\n"
        assert fs.writes == ["a.py"]

    async def test_grep_returns_path_line_content(self) -> None:
        fs = FakeFileSystem({"a.py": "import os\nimport sys\n"})
        reg = fs.as_registry()
        result = await reg.call("grep", pattern=r"^import sys")
        assert result.content == "a.py:2:import sys"
