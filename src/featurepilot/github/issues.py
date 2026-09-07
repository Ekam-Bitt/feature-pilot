"""Issue URLs and issue text."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from featurepilot.github import client

log = logging.getLogger(__name__)

#: The one shape we accept. Anchored on the host so a lookalike domain or a
#: /pull/ URL fails loudly rather than cloning the wrong thing.
_ISSUE_URL = re.compile(
    r"^https?://(?:www\.)?github\.com/(?P<owner>[^/\s]+)/(?P<repo>[^/\s]+)"
    r"/issues/(?P<number>\d+)/?(?:[?#].*)?$"
)


@dataclass(frozen=True, slots=True)
class IssueRef:
    """A parsed `owner/repo#number` reference."""

    owner: str
    repo: str
    number: int

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"


def parse_issue_url(url: str) -> IssueRef:
    """Parse a public GitHub issue URL, or raise with the expected shape."""
    match = _ISSUE_URL.match(url.strip())
    if match is None:
        raise ValueError(
            f"not a GitHub issue URL: {url!r} "
            "(expected https://github.com/<owner>/<repo>/issues/<number>)"
        )
    return IssueRef(owner=match["owner"], repo=match["repo"], number=int(match["number"]))


@dataclass(frozen=True, slots=True)
class FetchedIssue:
    """An issue as fetched from GitHub, ready to feed the pipeline."""

    title: str
    body: str
    state: str
    url: str

    @property
    def text(self) -> str:
        # Same framing the CLI has always produced for --github issues.
        return f"# {self.title}\n\n{self.body}"


def fetch_issue(ref: IssueRef, *, token: str | None = None) -> FetchedIssue:
    """Fetch title/body/state via gh.

    A closed issue is worth a warning, not a refusal — re-solving a closed
    issue is a legitimate benchmark move, and the human gate is still ahead.
    """
    data = client.gh_json(
        "issue",
        "view",
        str(ref.number),
        "--repo",
        ref.slug,
        "--json",
        "title,body,state,url",
        token=token,
    )
    issue = FetchedIssue(
        title=data["title"], body=data["body"], state=data["state"], url=data["url"]
    )
    if issue.state.upper() != "OPEN":
        log.warning("issue %s#%d is %s", ref.slug, ref.number, issue.state.lower())
    return issue
