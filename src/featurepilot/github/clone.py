"""Shallow clones of the target repository.

One fresh clone per run, keyed by run_id under the artifact root: the diff the
sandbox produces is computed against exactly this tree, so publishing later
can verify it still applies to the commit it was made from. `.fp` is both
gitignored and excluded from the sandbox tar, so a clone can never leak into
the container copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from featurepilot.github import client
from featurepilot.github.issues import IssueRef
from featurepilot.run import ARTIFACT_ROOT


@dataclass(frozen=True, slots=True)
class ClonedRepo:
    """A local clone plus the facts publishing needs about it."""

    path: Path
    ref: IssueRef
    head_sha: str
    default_branch: str


def clone_root_for(run_id: str) -> Path:
    """Where a run's clone lives. Derivable from the run_id alone, so resume
    and standalone publish need no extra bookkeeping."""
    return ARTIFACT_ROOT / "clones" / run_id


def shallow_clone(
    ref: IssueRef, dest_root: Path, *, token: str | None = None, url_override: str | None = None
) -> ClonedRepo:
    """`git clone --depth 1` into `dest_root/<repo>`.

    `url_override` is the test seam: a local `file://` repository stands in
    for github.com so the real git transport is exercised offline.
    """
    dest_root.mkdir(parents=True, exist_ok=True)
    url = url_override or f"https://github.com/{ref.slug}"
    client.run_git(dest_root, "clone", "--depth", "1", url, ref.repo)
    path = dest_root / ref.repo
    head_sha = client.run_git(path, "rev-parse", "HEAD").strip()
    default_branch = client.run_git(path, "branch", "--show-current").strip()
    return ClonedRepo(path=path, ref=ref, head_sha=head_sha, default_branch=default_branch)


def existing_clone(run_id: str, *, clones_root: Path | None = None) -> Path | None:
    """The sole clone directory for a run, if one exists (resume support)."""
    root = (clones_root or ARTIFACT_ROOT / "clones") / run_id
    if not root.is_dir():
        return None
    subdirs = [p for p in root.iterdir() if p.is_dir()]
    return subdirs[0] if len(subdirs) == 1 else None


def reattach_clone(
    run_id: str, ref: IssueRef, *, clones_root: Path | None = None
) -> ClonedRepo | None:
    """Recover a run's ClonedRepo from disk, for resume and re-publish.

    The base facts come from origin's default branch, not the current HEAD —
    a previous publish leaves the worktree parked on the feature branch.
    """
    path = existing_clone(run_id, clones_root=clones_root)
    if path is None:
        return None
    origin_head = client.run_git(path, "rev-parse", "--abbrev-ref", "origin/HEAD").strip()
    default_branch = origin_head.split("/", 1)[1]
    head_sha = client.run_git(path, "rev-parse", origin_head).strip()
    return ClonedRepo(path=path, ref=ref, head_sha=head_sha, default_branch=default_branch)
