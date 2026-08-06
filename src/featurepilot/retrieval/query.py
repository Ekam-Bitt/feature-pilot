"""Query generation — turning an issue report into search terms.

The first of four retrieval modules:

    Issue -> [Query Generator] -> Retriever -> Ranker -> Context Builder

Separated because measurement showed the failure lives *here*, not downstream. On
click, retrieval scored P@3 = 0.33, and the terms being searched were `False`,
`Hello`, `World`, `CliRunner`, `runner.invoke` — and in one case `Python`, `help`,
`copyright`, `credits`, `license`, which is the interpreter's start-up banner from
a pasted console session. The retriever was working correctly on a garbage query.

The cause was a heuristic that reads well and is wrong: *backticked spans are the
important ones*. In a bug report, backticks and fences mostly contain a
reproduction script or a terminal transcript, so that rule systematically
prefers test scaffolding over the code under discussion.

The fix classifies regions before extracting, rather than accumulating exclusions.
A word's value depends on where it appears: `confirm` in prose is a symbol,
`copyright` in a REPL banner is noise, and no stop-word list can tell them apart
because the word alone does not carry that information.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass
from enum import StrEnum

# --- region classification ------------------------------------------------


class Region(StrEnum):
    """What kind of text a line belongs to."""

    PROSE = "prose"
    CODE = "code"  # fenced block or indented sample
    CONSOLE = "console"  # shell command line, `$ pytest ...`
    REPL_BANNER = "repl_banner"  # interpreter start-up chatter
    TRACEBACK = "traceback"


#: Weight per region. A term's score is its weight where it was found; the same
#: word can be valuable in prose and worthless in a banner.
REGION_WEIGHT: dict[Region, float] = {
    Region.PROSE: 1.0,
    # Code blocks are not worthless: they name the API being exercised. But the
    # scaffolding around it (fixtures, literals, assertions) is noise, so bare
    # identifiers here score low and dotted API references score high — see
    # `_terms_in`.
    Region.CODE: 0.4,
    # A shell line names commands and paths, not the symbols under discussion.
    Region.CONSOLE: 0.2,
    # `Python 3.12`, `Type "help", "copyright", "credits" or "license"`. Pure
    # noise that a naive extractor treats as five strong identifiers.
    Region.REPL_BANNER: 0.0,
    # Frames name real code, but mostly the library's own internals rather than
    # the defect. Worth a little.
    Region.TRACEBACK: 0.25,
}

_FENCE = re.compile(r"^\s*(```|~~~)")
_CONSOLE_LINE = re.compile(r"^\s*(?:\$|#|>|PS[ >]|C:\\\\)\s+\S")
_REPL_LINE = re.compile(r"^\s*(?:>>>|\.\.\.)\s?")
_REPL_BANNER = re.compile(
    r"""^\s*(?:
        Python\s+\d+\.\d+          # Python 3.12.1 (main, ...)
      | Type\s+"?help"?           # Type "help", "copyright", ...
      | \[GCC | \[Clang | \[MSC
    )""",
    re.VERBOSE,
)
_TRACEBACK_START = re.compile(r"^\s*Traceback \(most recent call last\)")


def classify(text: str) -> list[tuple[Region, str]]:
    """Label every line of an issue with the region it belongs to.

    Exported and pure so region detection can be tested against real reports
    without touching retrieval.
    """
    out: list[tuple[Region, str]] = []
    in_fence = False
    in_traceback = False

    for line in text.splitlines():
        if _FENCE.match(line):
            in_fence = not in_fence
            in_traceback = False
            continue

        if _TRACEBACK_START.match(line):
            in_traceback = True
            out.append((Region.TRACEBACK, line))
            continue
        if in_traceback:
            # A traceback ends at the exception line, which is not indented.
            if line.strip() and not line.startswith((" ", "\t")):
                in_traceback = False
                out.append((Region.TRACEBACK, line))
                continue
            out.append((Region.TRACEBACK, line))
            continue

        if _REPL_BANNER.match(line):
            out.append((Region.REPL_BANNER, line))
            continue
        if _CONSOLE_LINE.match(line):
            out.append((Region.CONSOLE, line))
            continue
        if _REPL_LINE.match(line):
            out.append((Region.CODE, _REPL_LINE.sub("", line)))
            continue

        if in_fence:
            out.append((Region.CODE, line))
            continue
        # A markdown indented code block, but not a list continuation.
        if (
            line.startswith(("    ", "\t"))
            and line.strip()
            and not line.lstrip().startswith(("-", "*"))
        ):
            out.append((Region.CODE, line))
            continue
        out.append((Region.PROSE, line))
    return out


# --- term extraction ------------------------------------------------------

#: A path is the strongest possible signal: the reporter named a file.
_PATH = re.compile(r"\b((?:[\w.-]+/)+[\w.-]+\.\w{1,4})\b")
#: `module.function` or `Class.method` — names an API rather than a local.
_DOTTED = re.compile(r"\b([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)+)\b")
#: snake_case, CamelCase, SCREAMING_CASE.
_IDENTIFIER = re.compile(r"\b([A-Za-z_]\w{3,})\b")
#: Inline code in prose. Valuable *because* it is in prose, not because of the
#: backticks — the same span inside a fence gets CODE weight.
_INLINE_CODE = re.compile(r"`([^`\n]{2,60})`")

#: Multipliers by what the token looks like, independent of region.
PATH_BONUS = 3.0
DOTTED_BONUS = 2.5
SHAPE_BONUS = 1.6  # has _ or . or mixed case: looks like a symbol
PLAIN_PENALTY = 0.5  # an ordinary lowercase word

#: Words that are never worth a search in a code repository, either because they
#: match everything or because they carry no location information. Kept short:
#: region weighting does most of the work, and a long list is a sign the
#: classification is doing too little.
_STOPWORD_TEXT = """
    the and for with that this from have has been are was were will would should
    when what which where why how our their your its not but all any can could
    into than then them they there these those you use used using also more most
    only over some such very via each other about after before between both
    because during under while does did done doing get got give given make made
    take see say just like way well even still back much many issue bug expected
    actual steps reproduce notes error errors happened output result results
    true false none null self args kwargs return returns print import from class
    def test tests case cases line lines file files code python click example
    version help copyright credits license traceback module main
    """

#: A prose block is more readable and diffable than 100 quoted list items.
STOPWORDS = frozenset(_STOPWORD_TEXT.split())


@dataclass(frozen=True, slots=True)
class Query:
    """Search terms with the evidence for each, so a bad query is diagnosable."""

    terms: tuple[str, ...]
    weights: dict[str, float]
    #: Paths the reporter named outright. Worth trying before any search.
    paths: tuple[str, ...]

    def __len__(self) -> int:
        return len(self.terms)


def _shape_multiplier(token: str) -> float:
    if "/" in token:
        return PATH_BONUS
    if "." in token:
        return DOTTED_BONUS
    if "_" in token or not token.islower():
        return SHAPE_BONUS
    return PLAIN_PENALTY


def _terms_in(region: Region, line: str) -> dict[str, float]:
    """Candidate terms from one line, scored before the region weight applies."""
    found: dict[str, float] = {}

    for path in _PATH.findall(line):
        found[path] = max(found.get(path, 0.0), PATH_BONUS)

    # Inline code counts only in prose. Inside a fence every token is "code", so
    # the marker carries no extra information there.
    if region is Region.PROSE:
        for span in _INLINE_CODE.findall(line):
            for token in _IDENTIFIER.findall(span):
                if token.lower() not in STOPWORDS:
                    found[token] = max(found.get(token, 0.0), _shape_multiplier(token) * 1.5)

    for dotted in _DOTTED.findall(line):
        if dotted.split(".")[-1].lower() not in STOPWORDS:
            found[dotted] = max(found.get(dotted, 0.0), DOTTED_BONUS)

    for token in _IDENTIFIER.findall(line):
        if token.lower() in STOPWORDS:
            continue
        # Inside a code sample, a bare identifier is usually a local, a fixture,
        # or a literal — `Hello`, `runner`, `result`. Dotted references above are
        # where the API surface actually shows up.
        if region is Region.CODE and "." not in token and "_" not in token:
            continue
        found[token] = max(found.get(token, 0.0), _shape_multiplier(token))

    return found


def build(text: str, limit: int = 12) -> Query:
    """Extract search terms from an issue report, best first."""
    scored: defaultdict[str, float] = defaultdict(float)
    paths: list[str] = []

    for region, line in classify(text):
        weight = REGION_WEIGHT[region]
        if weight == 0.0:
            continue
        for token, base in _terms_in(region, line).items():
            scored[token] += base * weight
            if "/" in token and token not in paths:
                paths.append(token)

    ranked = [t for t, _ in sorted(scored.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]
    return Query(
        terms=tuple(ranked),
        weights={t: round(scored[t], 3) for t in ranked},
        paths=tuple(paths[:6]),
    )
