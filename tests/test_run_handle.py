"""RunHandle carries what the post-run publish step needs."""

from __future__ import annotations

from pathlib import Path

from featurepilot.metrics.events import InMemorySink
from featurepilot.run import RunHandle


def test_handle_carries_repo_path_for_publishing() -> None:
    handle = RunHandle(
        run_id="r1",
        thread_id="t1",
        graph=object(),
        ctx=object(),
        sink=InMemorySink(),
        repo_path=Path("/tmp/clone/widget"),
    )
    assert handle.repo_path == Path("/tmp/clone/widget")
