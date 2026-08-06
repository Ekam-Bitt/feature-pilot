"""Terminal MCP server.

Commands run **inside the run's container**, never on the host, and go through
`Sandbox.exec` — so the allowlist applies and no shell is involved. This server
deliberately holds no policy of its own: duplicating the allowlist here would
create a second checkpoint that could drift out of step with the first.

Spawned per run; the run id arrives as FP_RUN_ID.
"""

from __future__ import annotations

import os
import re
import sys

from mcp.server.mcpserver import MCPServer

from featurepilot.mcp import as_error
from featurepilot.sandbox.runner import CommandNotAllowed, Sandbox, SandboxError

server = MCPServer(
    name="terminal",
    instructions=(
        "Run commands in the repository's sandbox. There is no network access and "
        "no shell: run one command per call, without pipes or redirection."
    ),
)

_sandbox: Sandbox | None = None


async def _box() -> Sandbox:
    global _sandbox
    if _sandbox is None:
        run_id = os.environ.get("FP_RUN_ID")
        if not run_id:
            raise SandboxError("FP_RUN_ID is not set; cannot attach to a sandbox")
        _sandbox = await Sandbox.attach(run_id)
    return _sandbox


#: Test output is the single largest thing the model reads. Keeping the tail
#: rather than the head matters: pytest puts the failure summary at the end.
MAX_OUTPUT_CHARS = 40_000


def _trim(text: str) -> str:
    if len(text) <= MAX_OUTPUT_CHARS:
        return text
    return (
        f"[output truncated — showing the last {MAX_OUTPUT_CHARS} characters]\n"
        + text[-MAX_OUTPUT_CHARS:]
    )


@server.tool(
    description=(
        "Run a single command in the repository sandbox. No shell, so pipes, "
        "redirection and chaining are unavailable — call once per command. "
        "There is no network access. Returns the exit code with stdout and stderr."
    )
)
async def run_command(command: str, timeout_seconds: int = 300) -> str:
    """Execute `command` in the sandbox."""
    try:
        box = await _box()
        result = await box.exec(command, timeout=timeout_seconds)
    except CommandNotAllowed as exc:
        return as_error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return as_error(f"running {command!r}: {exc}")
    header = f"exit code: {result.exit_code} (took {result.duration_ms} ms)"
    return f"{header}\n\n{_trim(result.combined)}" if result.combined else header


@server.tool(
    description=(
        "Run the test suite and get a structured summary: counts plus the "
        "identifier and message of each failing test. Prefer this over calling "
        "pytest through run_command."
    )
)
async def run_tests(target: str = "", timeout_seconds: int = 600) -> str:
    """Run pytest, optionally narrowed to `target` (a path or node id)."""
    command = "pytest -q --tb=short -p no:cacheprovider"
    if target:
        command = f"{command} {target}"
    try:
        box = await _box()
        result = await box.exec(command, timeout=timeout_seconds)
    except CommandNotAllowed as exc:
        return as_error(str(exc))
    except Exception as exc:  # noqa: BLE001
        return as_error(f"running tests: {exc}")

    summary = parse_pytest_summary(result.combined)
    if result.timed_out:
        summary = f"{summary}\n\nThe test run timed out — look for an unbounded loop."
    return f"{summary}\n\n--- output ---\n{_trim(result.combined)}"


#: pytest's terse tail, e.g. "17 failed, 68 passed, 1 warning in 1.48s".
_COUNT = re.compile(r"(\d+) (passed|failed|error|errors|skipped|xfailed|xpassed)")
_FAILED_LINE = re.compile(r"^FAILED (\S+)(?: - (.*))?$", re.MULTILINE)


def parse_pytest_summary(output: str) -> str:
    """Summarise a pytest run.

    Split out and importable so it can be unit-tested against captured output
    without a container — parsing someone else's stdout format is exactly the
    kind of thing that breaks quietly.
    """
    counts = {kind: int(n) for n, kind in _COUNT.findall(output)}
    failures = _FAILED_LINE.findall(output)

    if not counts and not failures:
        return "Could not parse a pytest summary from the output."

    passed = counts.get("passed", 0)
    failed = counts.get("failed", 0) + counts.get("error", 0) + counts.get("errors", 0)
    parts = [f"{passed} passed", f"{failed} failed"]
    if skipped := counts.get("skipped"):
        parts.append(f"{skipped} skipped")
    lines = [", ".join(parts)]

    if failures:
        lines.append("")
        lines.append("Failing tests:")
        for node, message in failures[:25]:
            lines.append(f"  - {node}" + (f"  ({message.strip()})" if message else ""))
        if len(failures) > 25:
            lines.append(f"  ... and {len(failures) - 25} more")
    elif failed == 0:
        lines.append("")
        lines.append("The suite is green.")
    return "\n".join(lines)


def main() -> None:
    try:
        server.run(transport="stdio")
    except KeyboardInterrupt:  # pragma: no cover
        sys.exit(0)


if __name__ == "__main__":
    main()
