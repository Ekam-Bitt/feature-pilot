"""MCP servers and the discovery client.

`ERROR_PREFIX` is the one convention shared across this boundary. The servers
return recoverable problems as *text* rather than raising, so the model can read
"the old string appears twice" and fix it — but the client still needs to mark
those results as failures for the metrics ledger.

It lives here, in one place, because the alternative is a string literal
duplicated between each server and the client. That drifts the moment someone
writes `f"Error reading {path}"` instead of `f"Error: ..."` — and then a rejected
path traversal is recorded as a *successful* tool call, which is exactly the bug
this module exists to prevent.
"""

from __future__ import annotations

ERROR_PREFIX = "Error:"


def as_error(message: str) -> str:
    """Format a recoverable tool failure for the model to read and act on."""
    return f"{ERROR_PREFIX} {message}"


def is_error_text(text: str) -> bool:
    """Whether a tool's text output represents a failure."""
    return text.lstrip().startswith(ERROR_PREFIX)
