"""MCP servers and dynamic tool discovery.

The pytest-summary parser is pure and tested against captured output: parsing
another tool's stdout is exactly the kind of thing that breaks silently after an
upstream version bump.

The discovery tests are `docker`-marked because they spawn the real servers,
which attach to a real container. They are the ones that prove discovery is
actually dynamic rather than a hardcoded list wearing an MCP costume.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from featurepilot.config import Settings
from featurepilot.mcp.client import DEFAULT_SERVERS, MCPToolLoader, _text_of
from featurepilot.mcp.servers.terminal_server import parse_pytest_summary
from featurepilot.sandbox.runner import Sandbox
from featurepilot.tools.registry import ToolRegistry

FIXTURE_REPO = Path(__file__).resolve().parents[1] / "fixtures" / "target-repo"

RED_OUTPUT = """\
FAILED tests/test_cart.py::TestPromoStacking::test_two_codes_stack_additively - assert 5000 == 7500
FAILED tests/test_pricing.py::TestTierSelection::test_tier_applies_at_exactly_five
17 failed, 68 passed, 1 warning in 1.48s
"""

GREEN_OUTPUT = "85 passed, 1 warning in 1.05s\n"


class TestPytestSummary:
    def test_reports_counts(self) -> None:
        summary = parse_pytest_summary(RED_OUTPUT)
        assert "68 passed" in summary
        assert "17 failed" in summary

    def test_lists_failing_node_ids(self) -> None:
        summary = parse_pytest_summary(RED_OUTPUT)
        assert (
            "tests/test_pricing.py::TestTierSelection::test_tier_applies_at_exactly_five" in summary
        )

    def test_includes_assertion_messages_when_present(self) -> None:
        """The message is often enough to diagnose without reading the traceback."""
        assert "assert 5000 == 7500" in parse_pytest_summary(RED_OUTPUT)

    def test_green_run_says_so_explicitly(self) -> None:
        summary = parse_pytest_summary(GREEN_OUTPUT)
        assert "85 passed" in summary
        assert "green" in summary

    def test_errors_count_as_failures(self) -> None:
        """A collection error is a red suite, not a passing one."""
        summary = parse_pytest_summary("2 errors, 3 passed in 0.2s")
        assert "2 failed" in summary

    def test_unparseable_output_is_reported_not_faked(self) -> None:
        """Better to say so than to report a confident '0 passed, 0 failed'."""
        assert "Could not parse" in parse_pytest_summary("segmentation fault")

    def test_long_failure_lists_are_capped(self) -> None:
        lines = "\n".join(f"FAILED tests/t.py::test_{i}" for i in range(40))
        summary = parse_pytest_summary(f"{lines}\n40 failed, 0 passed in 1s")
        assert "and 15 more" in summary


class TestResultFlattening:
    def test_joins_text_blocks(self) -> None:
        class Block:
            def __init__(self, text: str) -> None:
                self.text = text

        class Result:
            content = [Block("first"), Block("second")]

        assert _text_of(Result()) == "first\nsecond"

    def test_non_text_blocks_are_visible_not_dropped(self) -> None:
        """Silently dropping a block would give the model a misleadingly empty
        tool result."""

        class Image:
            type = "image"

        class Result:
            content = [Image()]

        assert _text_of(Result()) == "[image content]"

    def test_empty_content(self) -> None:
        class Result:
            content: list[object] = []

        assert _text_of(Result()) == ""


class TestServerSpecs:
    def test_run_id_is_passed_to_the_server(self) -> None:
        """Without FP_RUN_ID the server has no sandbox to attach to."""
        params = DEFAULT_SERVERS[0].params("run-abc")
        assert params.env is not None
        assert params.env["FP_RUN_ID"] == "run-abc"

    def test_servers_are_started_as_modules(self) -> None:
        params = DEFAULT_SERVERS[0].params("r")
        assert params.args[:1] == ["-m"]
        assert "featurepilot.mcp.servers" in params.args[1]

    def test_phase_1a_ships_filesystem_and_terminal(self) -> None:
        assert {s.name for s in DEFAULT_SERVERS} == {"filesystem", "terminal"}


# --------------------------------------------------------------------------
# Integration: real servers, real container.
#
# Each test opens its environment with `async with` *inside the test body*,
# rather than via a yielding fixture. stdio_client runs on anyio task groups,
# and a yielding async fixture enters and exits the cancel scope in different
# tasks — which raises "Attempted to exit cancel scope in a different task".
# Assertions are grouped by concern so this costs five containers, not fifteen.
# --------------------------------------------------------------------------


@asynccontextmanager
async def mcp_env(settings: Settings) -> AsyncIterator[tuple[Sandbox, ToolRegistry]]:
    box = Sandbox(FIXTURE_REPO, settings=settings)
    await box.start()
    await box.snapshot()
    loader = MCPToolLoader(box.run_id)
    try:
        await loader.connect()
        yield box, await loader.discover()
    finally:
        await loader.aclose()
        await box.destroy()


@pytest.mark.docker
class TestDynamicDiscovery:
    async def test_tools_are_advertised_by_the_servers(self, settings: Settings) -> None:
        """Discovery is the point: these names, descriptions and schemas come
        from the servers over MCP and are never written down on the client side."""
        async with mcp_env(settings) as (_box, registry):
            names = set(registry.names())
            assert {"read_file", "edit_file", "write_file", "glob", "grep"} <= names
            assert {"run_command", "run_tests"} <= names

            assert registry.get("read_file").source == "mcp:filesystem"
            assert registry.get("run_tests").source == "mcp:terminal"

            # A tool with no schema is a tool the model will call wrongly.
            edit = registry.get("edit_file")
            assert len(edit.description) > 40
            assert "old_string" in str(edit.input_schema)

    async def test_reads(self, settings: Settings) -> None:
        async with mcp_env(settings) as (_box, registry):
            ok = await registry.call("read_file", path="src/shopsvc/cart.py")
            assert ok.ok
            assert "SHIPPING_FLAT" in ok.content
            lines = ok.content.splitlines()
            # A header names the slice, so the model knows what it received and can
            # ask for a different range rather than assuming it saw the whole file.
            assert lines[0].startswith("src/shopsvc/cart.py (lines 1-")
            assert any(line.startswith("    2  ") for line in lines), "line gutter intact"

    async def test_ranged_reads(self, settings: Settings) -> None:
        """A whole-file read of click's core.py is ~35k tokens. Ranges are how the
        coder avoids paying that to see ten lines."""
        async with mcp_env(settings) as (_box, registry):
            result = await registry.call(
                "read_file", path="src/shopsvc/cart.py", offset=10, limit=5
            )
            assert result.ok
            lines = result.content.splitlines()
            assert lines[0].startswith("src/shopsvc/cart.py (lines 10-14 of ")
            # Numbering reflects the real position in the file, not the slice, so
            # quoting a line back to edit_file still refers to the right place.
            assert lines[1].startswith("   10  ")
            assert len([ln for ln in lines[1:] if ln.strip()]) <= 5

            missing = await registry.call("read_file", path="src/nope.py")
            assert not missing.ok
            assert "no such file" in missing.content

    async def test_path_traversal_is_refused_across_the_process_boundary(
        self, settings: Settings
    ) -> None:
        """The path check must hold through MCP, not only in-process — and the
        result must be marked as a failure so it is not logged as a success."""
        async with mcp_env(settings) as (_box, registry):
            result = await registry.call("read_file", path="../../etc/passwd")
            assert not result.ok, "a refused traversal must not record as success"
            assert "rejected" in result.content

            snapshot = await registry.call("read_file", path="/snapshot.git/HEAD")
            assert not snapshot.ok, "the snapshot git dir must be unreachable"

    async def test_edits(self, settings: Settings) -> None:
        async with mcp_env(settings) as (box, registry):
            applied = await registry.call(
                "edit_file",
                path="src/shopsvc/cart.py",
                old_string="SHIPPING_FLAT = 5_000",
                new_string="SHIPPING_FLAT = 6_000",
            )
            assert applied.ok, applied.content
            assert "6_000" in await box.read_text("src/shopsvc/cart.py")
            assert "src/shopsvc/cart.py" in await box.changed_files()

            # Editing one of several identical lines at random is worse than failing.
            await box.write_text("dup.py", "x = 1\nx = 1\n")
            ambiguous = await registry.call(
                "edit_file", path="dup.py", old_string="x = 1", new_string="x = 2"
            )
            assert not ambiguous.ok
            assert "2 times" in ambiguous.content

            absent = await registry.call(
                "edit_file",
                path="src/shopsvc/cart.py",
                old_string="this text does not exist anywhere",
                new_string="x",
            )
            assert not absent.ok
            assert "not found" in absent.content

    async def test_content_with_quotes_and_newlines_round_trips(self, settings: Settings) -> None:
        """Why file writes bypass the shell: real code contains quotes,
        backslashes and newlines that any quoting scheme eventually mangles."""
        async with mcp_env(settings) as (box, registry):
            nasty = 'x = "it\'s \\"quoted\\"\\n"\ny = `backtick`\nz = $(echo hi)\n'
            result = await registry.call("write_file", path="nasty.py", content=nasty)
            assert result.ok, result.content
            assert await box.read_text("nasty.py") == nasty

    async def test_search_and_terminal(self, settings: Settings) -> None:
        async with mcp_env(settings) as (box, registry):
            globbed = await registry.call("glob", pattern="test_*.py")
            assert globbed.ok
            assert "tests/test_cart.py" in globbed.content

            grepped = await registry.call("grep", pattern="FREE_SHIPPING_THRESHOLD")
            assert grepped.ok
            assert "cart.py" in grepped.content

            # The allowlist has to hold through MCP too.
            denied = await registry.call("run_command", command="curl https://example.com")
            assert not denied.ok
            assert "allowlist" in denied.content

            await box.install_dependencies()
            tests = await registry.call("run_tests")
            assert "17 failed" in tests.content
            assert "68 passed" in tests.content
            assert "Failing tests:" in tests.content

            # Nodes carry no tool instrumentation; the registry ledger is the source.
            assert {c["tool"] for c in registry.calls} >= {"glob", "grep", "run_tests"}
            assert registry.calls[0]["source"] == "mcp:filesystem"
