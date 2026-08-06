"""Filesystem MCP server.

Runs as a stdio subprocess on the host and operates inside the run's container by
attaching to its sandbox. Attaching rather than reimplementing container access
means path validation and ownership handling live in exactly one place.

Spawned per run; the run id arrives as FP_RUN_ID.

Errors are returned as text, not raised. A missing file or an ambiguous edit is
information the model should read and act on — an exception would surface as an
opaque tool failure and give it nothing to correct.
"""

from __future__ import annotations

import os
import sys

from mcp.server.mcpserver import MCPServer

from featurepilot.mcp import as_error
from featurepilot.sandbox.runner import Sandbox, SandboxError

server = MCPServer(
    name="filesystem",
    instructions=(
        "Read and edit files in the repository you are working on. All paths are "
        "relative to the repository root."
    ),
)

_sandbox: Sandbox | None = None


async def _box() -> Sandbox:
    """Attach lazily and once. The container already exists — the graph created
    it — so this process only needs a handle to it."""
    global _sandbox
    if _sandbox is None:
        run_id = os.environ.get("FP_RUN_ID")
        if not run_id:
            raise SandboxError("FP_RUN_ID is not set; cannot attach to a sandbox")
        _sandbox = await Sandbox.attach(run_id)
    return _sandbox


#: Truncation ceiling for a single read.
#:
#: Lowered from 60k after a real repository: one read of click's core.py is
#: ~35k tokens, and a coder loop accumulating a handful of those exhausted a
#: 400k-token run budget before finishing. A truncated read now says so and
#: points at the range arguments, so the model can narrow rather than guess.
MAX_READ_CHARS = 12_000


@server.tool(
    description=(
        "Read a file from the repository. Returns the contents with line numbers, "
        "so you can quote exact text back to edit_file.\n\n"
        "Large files must be read in ranges: pass offset (1-indexed first line) "
        "and limit (how many lines). Use grep first to find the line you want, "
        "then read a window around it. Reading a whole large file wastes budget "
        "you will need for the edit."
    )
)
async def read_file(path: str, offset: int = 0, limit: int = 0) -> str:
    """Read `path`, optionally the `limit` lines starting at `offset`."""
    try:
        box = await _box()
        body, first_line, total = await box.read_range(path, offset=offset, limit=limit)
    except FileNotFoundError:
        return as_error(f"no such file: {path}. Use glob to find the correct path.")
    except Exception as exc:  # noqa: BLE001 - surface to the model, don't crash the server
        return as_error(f"reading {path}: {exc}")

    truncated = len(body) > MAX_READ_CHARS
    if truncated:
        body = body[:MAX_READ_CHARS]
    numbered = "\n".join(
        f"{i:>5}  {line}" for i, line in enumerate(body.splitlines(), start=first_line)
    )
    header = f"{path} (lines {first_line}-{first_line + body.count(chr(10))} of {total})"
    if truncated:
        numbered += (
            f"\n\n[truncated at {MAX_READ_CHARS} characters. Read a narrower range: "
            f"read_file(path={path!r}, offset=<line>, limit=200)]"
        )
    return f"{header}\n{numbered}" if numbered else f"{path} is empty."


@server.tool(
    description=(
        "Replace an exact string in a file. The old_string must appear exactly "
        "once and match byte-for-byte including indentation, so read the file "
        "first and copy from it. Prefer this over write_file for edits."
    )
)
async def edit_file(path: str, old_string: str, new_string: str) -> str:
    """Replace one occurrence of `old_string` with `new_string` in `path`."""
    try:
        box = await _box()
        return await box.edit_text(path, old_string, new_string)
    except FileNotFoundError:
        return as_error(f"no such file: {path}")
    except ValueError as exc:
        # Zero or several matches — the model can fix this itself.
        return as_error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return as_error(f"editing {path}: {exc}")


@server.tool(
    description=(
        "Write a file, replacing it entirely if it exists. Use edit_file for "
        "changes to existing files; this is for creating new ones."
    )
)
async def write_file(path: str, content: str) -> str:
    """Write `content` to `path`."""
    try:
        box = await _box()
        await box.write_text(path, content)
    except Exception as exc:  # noqa: BLE001
        return as_error(f"writing {path}: {exc}")
    return f"wrote {len(content)} characters to {path}"


@server.tool(
    description=(
        "Find files by name pattern, e.g. '*.py' or 'test_*.py'. Returns paths "
        "relative to the repository root."
    )
)
async def glob(pattern: str) -> str:
    """List files whose name matches `pattern`."""
    try:
        box = await _box()
        hits = await box.glob(pattern)
    except Exception as exc:  # noqa: BLE001
        return as_error(f"globbing {pattern}: {exc}")
    if not hits:
        return f"No files match {pattern}."
    return "\n".join(hits)


@server.tool(
    description=(
        "Search file contents with a regular expression. Returns matching lines "
        "as path:line:text. Use this to locate code before reading whole files."
    )
)
async def grep(pattern: str, path: str = ".") -> str:
    """Search for `pattern` under `path`."""
    try:
        box = await _box()
        result = await box.grep(pattern, path)
    except Exception as exc:  # noqa: BLE001
        return as_error(f"searching for {pattern}: {exc}")
    if not result.stdout.strip():
        return f"No matches for {pattern}."
    lines = result.stdout.splitlines()
    if len(lines) > 200:
        kept = "\n".join(lines[:200])
        return f"{kept}\n\n[{len(lines) - 200} more matches — narrow the pattern]"
    return result.stdout


def main() -> None:
    try:
        server.run(transport="stdio")
    except KeyboardInterrupt:  # pragma: no cover
        sys.exit(0)


if __name__ == "__main__":
    main()
