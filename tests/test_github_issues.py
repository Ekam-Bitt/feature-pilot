"""URL parsing and issue fetching for the GitHub ingestion path.

Everything here is offline: fetch tests monkeypatch the one subprocess seam
(`featurepilot.github.client`), never the network.
"""

from __future__ import annotations

import pytest

from featurepilot.github.issues import IssueRef, parse_issue_url


class TestParseIssueUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/pallets/click/issues/2951",
            "https://github.com/pallets/click/issues/2951/",
            "https://github.com/pallets/click/issues/2951#issuecomment-123",
            "https://github.com/pallets/click/issues/2951?foo=bar",
            "http://github.com/pallets/click/issues/2951",
            "https://www.github.com/pallets/click/issues/2951",
        ],
    )
    def test_accepts_issue_url_variants(self, url: str) -> None:
        assert parse_issue_url(url) == IssueRef(owner="pallets", repo="click", number=2951)

    def test_slug_is_owner_slash_repo(self) -> None:
        ref = parse_issue_url("https://github.com/Ekam-Bitt/featurepilot-fixture/issues/1")
        assert ref.slug == "Ekam-Bitt/featurepilot-fixture"
        assert ref.number == 1

    @pytest.mark.parametrize(
        "url",
        [
            "https://gitlab.com/pallets/click/issues/2951",  # wrong host
            "https://github.com/pallets/click/pull/2951",  # PR, not issue
            "https://github.com/pallets/click/issues/",  # no number
            "https://github.com/pallets/click/issues/abc",  # non-numeric
            "https://github.com/pallets/issues/2951",  # missing repo segment
            "not a url at all",
        ],
    )
    def test_rejects_non_issue_urls(self, url: str) -> None:
        with pytest.raises(ValueError, match="github.com/<owner>/<repo>/issues/<number>"):
            parse_issue_url(url)


class TestFetchIssue:
    REF = IssueRef(owner="pallets", repo="click", number=2951)

    def _fake(self, monkeypatch: pytest.MonkeyPatch, payload: dict[str, str]) -> list[list[str]]:
        from featurepilot.github import client

        calls: list[list[str]] = []

        def fake_gh_json(*args: str, token: str | None = None) -> dict[str, str]:
            calls.append(list(args))
            return payload

        monkeypatch.setattr(client, "gh_json", fake_gh_json)
        return calls

    def test_returns_issue_and_asks_gh_for_the_right_fields(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from featurepilot.github.issues import fetch_issue

        calls = self._fake(
            monkeypatch,
            {
                "title": "Bug in parser",
                "body": "It breaks.",
                "state": "OPEN",
                "url": "https://github.com/pallets/click/issues/2951",
            },
        )
        issue = fetch_issue(self.REF)
        assert issue.title == "Bug in parser"
        assert issue.text == "# Bug in parser\n\nIt breaks."
        [argv] = calls
        assert argv[:3] == ["issue", "view", "2951"]
        assert "--repo" in argv and "pallets/click" in argv

    def test_closed_issue_warns_but_returns(
        self, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        from featurepilot.github.issues import fetch_issue

        self._fake(
            monkeypatch,
            {"title": "Old bug", "body": "", "state": "CLOSED", "url": "u"},
        )
        with caplog.at_level("WARNING"):
            issue = fetch_issue(self.REF)
        assert issue.state == "CLOSED"
        assert any("closed" in r.message.lower() for r in caplog.records)
