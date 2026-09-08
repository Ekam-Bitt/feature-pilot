"""Turn a finished run's diff into a fork, a branch, and a pull request.

Everything here operates on the host clone (the sandbox tar strips .git, so
the container never had one) and runs after the graph is DONE — the container
may already be gone, and nothing here needs it.
"""

from __future__ import annotations

import logging
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from featurepilot.contracts import PRSummary
from featurepilot.github import client
from featurepilot.github.clone import ClonedRepo
from featurepilot.github.issues import IssueRef

log = logging.getLogger(__name__)

#: Branch prefix; deterministic per issue so republish updates, never piles up.
_BRANCH_PREFIX = "feature-pilot"
_SLUG_MAX = 40


class PublishError(RuntimeError):
    """Publishing failed; the run and its artifacts are unharmed."""


def branch_name(number: int, title: str) -> str:
    """`feature-pilot/issue-<n>-<slug>` — deterministic per issue."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:_SLUG_MAX].rstrip("-")
    base = f"{_BRANCH_PREFIX}/issue-{number}"
    return f"{base}-{slug}" if slug else base


def apply_patch(clone: Path, diff: str) -> None:
    """Apply a worktree-relative unified diff at the clone root.

    `--check` first so a mismatch leaves the tree untouched; `--3way` as the
    fallback for oddities (a shallow clone still has the HEAD blobs 3way
    needs). Failure raises PublishError carrying git's own explanation.
    """
    # The patch file lives outside the clone — writing it inside would dirty
    # the very tree it is about to patch.
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as handle:
        handle.write(diff)
        patch = Path(handle.name)
    try:
        try:
            client.run_git(clone, "apply", "--check", str(patch))
            client.run_git(clone, "apply", str(patch))
        except client.GhError as exc:
            log.info("plain git apply failed (%s); retrying --3way", exc.stderr.strip())
            try:
                client.run_git(clone, "apply", "--3way", str(patch))
            except client.GhError as exc3:
                raise PublishError(
                    f"patch does not apply to the clone: {exc3.stderr.strip()}"
                ) from exc3
    finally:
        patch.unlink(missing_ok=True)


#: How long to wait for a fresh fork to become visible. GitHub forks are
#: asynchronous; a push straight after `repo fork` races the copy.
_FORK_POLL_ATTEMPTS = 10
_FORK_POLL_DELAY_S = 3.0


def ensure_fork(ref: IssueRef, *, token: str | None = None) -> str:
    """Fork upstream under the token's user; return the fork slug.

    Idempotent: an existing fork is success. Polls until the fork resolves so
    the subsequent push does not race GitHub's asynchronous copy. When the
    token's user owns upstream there is nothing to fork — GitHub refuses
    self-forks — so the branch goes straight to upstream.
    """
    login = client.gh_json("api", "user", token=token)["login"]
    if login == ref.owner:
        return ref.slug
    try:
        client.run_gh("repo", "fork", ref.slug, "--clone=false", token=token)
    except client.GhError as exc:
        if "already exists" not in exc.stderr:
            raise PublishError(f"could not fork {ref.slug}: {exc.stderr.strip()}") from exc
    fork = f"{login}/{ref.repo}"
    for _ in range(_FORK_POLL_ATTEMPTS):
        try:
            client.gh_json("repo", "view", fork, "--json", "name", token=token)
            return fork
        except client.GhError:
            time.sleep(_FORK_POLL_DELAY_S)
    raise PublishError(f"fork {fork} did not become visible after forking {ref.slug}")


@dataclass(frozen=True, slots=True)
class PublishResult:
    pr_url: str
    branch: str
    fork: str
    #: True when an open PR for this branch already existed and was updated
    #: by the force-push rather than a new one being created.
    reused_pr: bool


def _ensure_identity(clone: Path, *, token: str | None = None) -> None:
    """Commits need an author; fall back to the noreply address when the
    environment has none configured (a fresh VM, CI)."""
    try:
        client.run_git(clone, "config", "user.email")
        return
    except client.GhError:
        pass
    user = client.gh_json("api", "user", token=token)
    login = user["login"]
    client.run_git(clone, "config", "user.name", login)
    client.run_git(
        clone, "config", "user.email", f"{user.get('id', 0)}+{login}@users.noreply.github.com"
    )


def publish_run(
    clone: ClonedRepo,
    diff: str,
    pr: PRSummary,
    *,
    token: str | None = None,
    draft: bool = False,
    fork_url_override: str | None = None,
) -> PublishResult:
    """Branch → patch → commit → fork → force-push → PR. Idempotent per issue.

    The branch is reset to the run's recorded base commit every time, so a
    republish updates the one PR rather than stacking commits or branches.
    `fork_url_override` is the test seam: a local bare repo takes the push
    while gh calls are faked.
    """
    if not diff.strip():
        raise PublishError("the run produced an empty diff — nothing to publish")

    status = client.run_git(clone.path, "status", "--porcelain").strip()
    if status:
        raise PublishError(
            f"clone at {clone.path} has uncommitted changes — refusing to publish:\n{status}"
        )
    try:
        client.run_git(clone.path, "rev-parse", "--verify", f"{clone.head_sha}^{{commit}}")
    except client.GhError as exc:
        raise PublishError(
            f"clone no longer contains the run's base commit {clone.head_sha[:12]}; "
            "the diff was computed against that tree — re-run rather than publish"
        ) from exc

    branch = branch_name(clone.ref.number, pr.title)
    client.run_git(clone.path, "checkout", "-B", branch, clone.head_sha)
    apply_patch(clone.path, diff)
    _ensure_identity(clone.path, token=token)
    body = f"{pr.body}\n\n## Test plan\n{pr.test_plan}\n\nCloses #{clone.ref.number}"
    client.run_git(clone.path, "add", "-A")
    client.run_git(clone.path, "commit", "-m", f"{pr.title}\n\n{body}")

    fork = ensure_fork(clone.ref, token=token)
    fork_url = fork_url_override or f"https://github.com/{fork}"
    try:
        client.run_git(clone.path, "remote", "add", "fork", fork_url)
    except client.GhError:
        client.run_git(clone.path, "remote", "set-url", "fork", fork_url)
    # The only call here that talks to GitHub, and so the only one needing a
    # credential. A freshly provisioned host has no git credential helper, so
    # without this the push fails with "could not read Username".
    client.run_git(clone.path, "push", "-f", "fork", branch, token=token)

    head = f"{fork.split('/')[0]}:{branch}"
    existing = client.gh_json(
        "pr",
        "list",
        "--repo",
        clone.ref.slug,
        "--head",
        head,
        "--state",
        "open",
        "--json",
        "url",
        token=token,
    )
    if existing:
        return PublishResult(pr_url=existing[0]["url"], branch=branch, fork=fork, reused_pr=True)

    args = [
        "pr",
        "create",
        "--repo",
        clone.ref.slug,
        "--base",
        clone.default_branch,
        "--head",
        head,
        "--title",
        pr.title,
        "--body",
        body,
    ]
    if draft:
        args.append("--draft")
    url = client.run_gh(*args, token=token).strip().splitlines()[-1]
    return PublishResult(pr_url=url, branch=branch, fork=fork, reused_pr=False)
