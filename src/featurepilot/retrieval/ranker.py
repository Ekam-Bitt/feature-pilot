"""Ranking — deciding which candidate files matter, from their content.

The third of four retrieval modules:

    Issue -> Query Generator -> Retriever -> [Ranker] -> Context Builder

Modules 1 and 2 have been measured and largely exonerated. Cleaning the query
changed accuracy by exactly zero (P@3 stayed at 0.33 on click) while cutting
context by a third, and the terms it now produces — `click.confirm`,
`HelpFormatter`, `write_usage` — are the right ones. The candidates coming back
are matching the right *concepts* in the wrong *files*.

**The objective was wrong, not the search.** Ranking by "how many queried terms
does this file mention" hands the win to tests and changelogs by construction: a
unit test calls `click.confirm(...)` five times where the implementation writes
`def confirm(...)` once, and a changelog mentions every symbol that ever existed.
The ranker was answering its question correctly.

So features here describe **content**, never pathname. `src/**` beating
`tests/**` would score well on a benchmark whose every answer lives in `src/`,
which measures the benchmark rather than the ranker. Whether a file *defines* the
symbol, *imports* it, or merely *calls* it is a property of the file, and holds in
any repository and any layout.

Weights are hand-picked and unlearned. They encode one claim: a bug fix changes
the code that defines the behaviour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Markup extensions. Document *type*, not repository layout — a `.md` file is
#: prose wherever it lives.
MARKUP_SUFFIXES = frozenset({".md", ".rst", ".txt", ".adoc"})

#: A changelog is recognised by its shape, not its name: many version-like
#: headings and little else. Filename is used only as a weak confirmation, so a
#: repository that calls it `NEWS` or `RELEASES` is still caught.
_VERSION_HEADING = re.compile(r"^\s*#{0,3}\s*v?\d+\.\d+(\.\d+)?\b", re.MULTILINE)
_CHANGELOG_NAMES = ("changes", "changelog", "history", "news", "releases")

_ASSERT = re.compile(r"^\s*(assert\b|self\.assert|expect\()", re.MULTILINE)
_TEST_DEF = re.compile(r"^\s*(?:async\s+)?def\s+test_", re.MULTILINE)


@dataclass(frozen=True, slots=True)
class Weights:
    """How much each content signal is worth.

    Unlearned. Adjusting these is a hypothesis, and the offline benchmark is how
    it gets tested — `eval/retrieval_bench.py` runs in seconds and costs nothing,
    so there is no excuse for tuning them by intuition.
    """

    #: The dominant signal. A fix changes the definition.
    defines: float = 6.0
    #: Importing a symbol means participating in its machinery.
    imports: float = 1.5
    #: Calling it is what tests, docs and examples all do. Deliberately small —
    #: this is the signal that was previously carrying the whole ranking.
    calls: float = 0.4
    #: Breadth of the match, kept so a file touching many query terms still rises.
    distinct_terms: float = 1.0
    #: Penalties. Prose and changelogs describe code without being it.
    markup: float = -3.0
    changelog: float = -5.0
    #: Assertion-heavy files exercise the API rather than implement it.
    test_like: float = -3.5


DEFAULT_WEIGHTS = Weights()


@dataclass(slots=True)
class Features:
    """Content signals for one candidate file."""

    path: str
    defines: int = 0
    imports: int = 0
    calls: int = 0
    distinct_terms: int = 0
    assert_lines: int = 0
    total_lines: int = 1
    is_markup: bool = False
    is_changelog: bool = False
    is_test_like: bool = False
    matched: frozenset[str] = field(default_factory=frozenset)

    @property
    def assert_density(self) -> float:
        return self.assert_lines / max(1, self.total_lines)

    def score(self, weights: Weights = DEFAULT_WEIGHTS) -> float:
        total = (
            weights.defines * self.defines
            + weights.imports * self.imports
            + weights.calls * min(self.calls, 8)  # saturate: 40 calls is not 5x better
            + weights.distinct_terms * self.distinct_terms
        )
        if self.is_markup:
            total += weights.markup
        if self.is_changelog:
            total += weights.changelog
        if self.is_test_like:
            total += weights.test_like
        return total


def _definition_patterns(term: str) -> re.Pattern[str]:
    """Match a definition of `term`, in Python and in several other languages.

    The last dotted segment is used: a query for `click.confirm` should match
    `def confirm(` in the module that provides it.
    """
    name = re.escape(term.split(".")[-1])
    return re.compile(
        rf"""(?:
            ^\s*(?:async\s+)?def\s+{name}\b          # python function
          | ^\s*class\s+{name}\b                      # python class
          | ^\s*{name}\s*[:=]                         # module-level assignment
          | ^\s*(?:export\s+)?function\s+{name}\b     # js/ts
          | ^\s*func\s+{name}\b                       # go
        )""",
        re.MULTILINE | re.VERBOSE,
    )


def extract(path: str, content: str, terms: frozenset[str]) -> Features:
    """Compute content features for one candidate. Pure and cheap."""
    suffix = "." + path.rsplit(".", 1)[-1] if "." in path else ""
    stem = path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
    lines = content.count("\n") + 1

    feat = Features(
        path=path,
        total_lines=lines,
        is_markup=suffix in MARKUP_SUFFIXES,
        matched=terms,
        distinct_terms=len(terms),
    )

    headings = len(_VERSION_HEADING.findall(content))
    # Either shape alone is weak evidence; a changelog has both a suggestive name
    # and a pile of version headings.
    feat.is_changelog = headings >= 5 and (
        any(n in stem for n in _CHANGELOG_NAMES) or headings > lines / 12
    )

    feat.assert_lines = len(_ASSERT.findall(content))
    feat.is_test_like = bool(_TEST_DEF.search(content)) or feat.assert_density > 0.04

    for term in terms:
        name = term.split(".")[-1]
        if _definition_patterns(term).search(content):
            feat.defines += 1
        if re.search(rf"^\s*(?:from|import)\b.*\b{re.escape(name)}\b", content, re.MULTILINE):
            feat.imports += 1
        feat.calls += len(re.findall(rf"\b{re.escape(name)}\s*\(", content))

    return feat


def rank(
    candidates: dict[str, tuple[str, frozenset[str]]],
    weights: Weights = DEFAULT_WEIGHTS,
) -> list[tuple[str, float, Features]]:
    """Order candidates best-first.

    `candidates` maps path -> (content, matched terms). Returns (path, score,
    features) so a ranking decision is inspectable — the features are why a file
    placed where it did, and without them a bad ranking is just a bad number.
    """
    scored = [
        (path, feats.score(weights), feats)
        for path, (content, terms) in candidates.items()
        for feats in (extract(path, content, terms),)
    ]
    scored.sort(key=lambda row: (-row[1], row[0]))
    return scored


def looks_like_implementation(feats: Features) -> bool:
    """Whether a file is implementation rather than prose or tests.

    A diagnostic, not a ranking signal: `eval/retrieval_bench.py` reports how far
    down the list the first implementation file appears, which is more sensitive
    than P@3 for the problem being worked on. Decided from content so the metric
    does not smuggle in the path assumption the ranker deliberately avoids.
    """
    return not (feats.is_markup or feats.is_changelog or feats.is_test_like)
