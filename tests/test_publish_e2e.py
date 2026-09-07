"""End-to-end publish against the real fixture repo.

Gated behind `-m github`: it talks to GitHub for real — fetches an issue,
shallow-clones the repo, publishes a fabricated one-line diff as a DRAFT PR,
asserts the URL, and deletes the PR and branch on the way out. No LLM, no
Docker: this isolates the publish path, the run pipeline is validated elsewhere.

The target is the user-owned fixture repo, so no fork indirection is needed —
which also means `ensure_fork` runs its idempotent "already exists" path.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from featurepilot.contracts import PRSummary
from featurepilot.github.client import run_gh
from featurepilot.github.clone import clone_root_for, shallow_clone
from featurepilot.github.issues import IssueRef, fetch_issue, parse_issue_url
from featurepilot.github.publish import publish_run

pytestmark = pytest.mark.github

MAP = json.loads((Path("fixtures") / "issues" / "github_map.json").read_text())
REPO = MAP["repo"]
ISSUE_NUMBER = MAP["issues"]["01-off-by-one.md"]


def test_fetch_clone_publish_roundtrip(tmp_path: Path) -> None:
    ref = parse_issue_url(f"https://github.com/{REPO}/issues/{ISSUE_NUMBER}")
    assert ref == IssueRef(REPO.split("/")[0], REPO.split("/")[1], ISSUE_NUMBER)

    issue = fetch_issue(ref)
    assert issue.title  # the fixture issue exists and has content

    clone = shallow_clone(ref, tmp_path / "clone")
    readme = next(p.name for p in clone.path.iterdir() if p.name.lower().startswith("readme"))

    # A fabricated one-line diff: append a marker line to the README. No model
    # involved — this test isolates publishing, not solving.
    original = (clone.path / readme).read_text()
    marker = "<!-- feature-pilot publish e2e -->"
    diff = (
        f"diff --git a/{readme} b/{readme}\n"
        f"--- a/{readme}\n"
        f"+++ b/{readme}\n"
        f"@@ -{original.count(chr(10))},1 +{original.count(chr(10))},2 @@\n"
        f" {original.splitlines()[-1]}\n"
        f"+{marker}\n"
    )
    pr = PRSummary(
        title="e2e: publish path check (auto-closed)",
        body="Opened by the gated publish e2e test; closed by the same test.",
        test_plan="None — this PR only proves the publish path works.",
    )

    result = publish_run(clone, diff, pr, draft=True)
    try:
        assert result.pr_url.startswith("https://github.com/")
        assert f"issue-{ISSUE_NUMBER}" in result.branch
    finally:
        run_gh("pr", "close", result.pr_url, "--delete-branch")


def test_clone_root_convention() -> None:
    assert str(clone_root_for("abc")).startswith(".fp")
