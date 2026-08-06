"""Phase 1A retrieval: grep, glob, read.

Not a stub. This is how Claude Code itself navigates an unfamiliar repository,
and it is a legitimate baseline — which matters, because Phase 1B's precision@k
numbers are only meaningful measured against something real.

The strategy: pull candidate identifiers out of the issue text, grep for them,
score files by how many distinct terms they match, and return the best few. No
index, no embeddings, no warm-up.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Callable

from featurepilot.contracts import RetrievedChunk, RetrieverOutput
from featurepilot.retrieval import query as query_module
from featurepilot.retrieval import ranker as ranker_module
from featurepilot.tools.registry import ToolRegistry
from featurepilot.tracing import traced

#: Turns issue text into ordered search terms. Two implementations exist:
#: `candidate_terms` (the original, kept as the benchmark's control) and
#: `region_aware_terms`, which classifies console pastes and repro scripts
#: before extracting.
QueryBuilder = Callable[[str], list[str]]

#: Finds where a symbol is *defined*, rather than where it is mentioned.
#:
#: A definition is the single strongest signal that a file is where a fix goes,
#: and it is obtainable by search — no need to read candidates to find out.
#: That matters twice over: reading every candidate cost 2.3x the bytes
#: scanned, and a pre-filter over the *mention* count buried real definitions
#: at rank 30 and 44 where nothing downstream could rescue them.
#: Written to mean the same thing in POSIX ERE and in Python `re`: `[ \t]`
#: rather than `[[:space:]]`, which Python rejects outright. The offline
#: benchmark evaluates patterns with `re` while production shells out to
#: `grep -E`, so a dialect difference makes the benchmark quietly unfaithful.
DEFINITION_PATTERN = r"^[ \t]*(def|class|async def)[ \t]+{name}"

#: Orders candidates given path -> (content, matched terms).
RankFn = Callable[
    [dict[str, tuple[str, frozenset[str]]]],
    list[tuple[str, float, ranker_module.Features]],
]

#: Identifier-shaped tokens: snake_case, CamelCase, dotted paths, SKUs.
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}(?:[./][A-Za-z0-9_]+)*")

#: Backticked or quoted spans in an issue are almost always the load-bearing
#: terms — a symbol, a path, or a literal value the reporter copied in.
_QUOTED = re.compile(r"[`'\"]([^`'\"\n]{3,60})[`'\"]")

#: Prose that would match half the repository. Deliberately short: over-filtering
#: throws away real signal, and a term that appears everywhere scores low anyway.
_STOPWORD_TEXT = """
    the and for with that this from have has been are was were will would should
    when what which where why how our their your its not but all any can could
    into than then them they there these those you use used using also more most
    only over some such very via each other about after before between both
    because during under while does did done doing get got give given make made
    take taken see seen say said just like way well even still back much many
    issue bug expected actual steps reproduce notes labels error errors happened
    call calls called customer order orders test tests case cases code line lines
    file files function method class value values return returns result results
    working works worked broken breaks broke missing wrong correct correctly
    fails failed failing passes passed passing returned showing shows shown
    appears appear adding added buying bought applied applies apply happening
    complained flagged escalated """

#: A prose block above is more readable and diffable than 90 quoted items.
_STOPWORDS = frozenset(_STOPWORD_TEXT.split())


def candidate_terms(text: str, limit: int = 12) -> list[str]:
    """Extract search terms from an issue, best first.

    Quoted and backticked spans rank above bare identifiers because a reporter who
    typed `TEA-001` or `available_units` is pointing at the code. Split out and
    pure so retrieval quality can be tuned against real issues without a
    container.
    """
    scored: Counter[str] = Counter()

    for match in _QUOTED.findall(text):
        token = match.strip()
        if not token or " " in token and len(token.split()) > 3:
            continue
        scored[token] += 5

    for match in _IDENTIFIER.findall(text):
        lowered = match.lower()
        if lowered in _STOPWORDS or len(match) < 4:
            continue
        # A leading capital or an underscore/dot suggests an actual symbol
        # rather than an English word.
        weight = 3 if ("_" in match or "." in match or not match.islower()) else 1
        scored[match] += weight

    return [term for term, _ in scored.most_common(limit)]


def region_aware_terms(text: str, limit: int = 12) -> list[str]:
    """Region-classifying query generation. See `retrieval/query.py`."""
    return list(query_module.build(text, limit=limit).terms)


class FilesystemRetriever:
    """Search-based retrieval over the sandbox worktree."""

    name = "filesystem"

    def __init__(
        self,
        registry: ToolRegistry,
        *,
        max_files: int = 6,
        query_builder: QueryBuilder = candidate_terms,
        ranker: RankFn | None = None,
    ) -> None:
        self._registry = registry
        self._max_files = max_files
        # Injected so query generation and retrieval can be scored separately.
        # Measurement showed the failure was in the query, not the search, and
        # a benchmark cannot attribute that unless the two are swappable.
        self._build_query = query_builder
        # None keeps the original objective (count of distinct matching terms),
        # which is the benchmark's control. A ranker replaces the objective
        # without touching query generation or search, so the two stay
        # separately attributable.
        self._ranker = ranker

    async def prepare(self) -> None:
        """No index to build. Present because the protocol promises it, and
        because Phase 1B's strategies do have work to do here."""
        return None

    @traced("filesystem_retrieve", run_type="retriever")
    async def retrieve(self, query: str, *, k: int = 8) -> RetrieverOutput:
        terms = self._build_query(query)
        if not terms:
            return RetrieverOutput(strategy=self.name, confidence=0.0)

        # path -> the terms that matched in it. Counting distinct terms rather
        # than raw hits stops one repeated word from dominating the ranking.
        matches: dict[str, set[str]] = {}
        # Files that *define* a queried symbol. Promoted ahead of the mention-count
        # pre-filter: a definition site is where a fix goes, and letting the weaker
        # signal decide who reaches the ranker is what buried the answer before.
        definers: set[str] = set()
        # path -> the line numbers that matched. Kept because returning whole
        # files does not survive a real repository: click's core.py is ~35k
        # tokens, and six of those blew a 400k-token run budget on one case.
        hit_lines: dict[str, set[int]] = {}
        for term in terms:
            result = await self._registry.call("grep", pattern=re.escape(term))
            if not result.ok:
                continue
            for line in result.content.splitlines():
                path, lineno = _locate(line)
                if path and _is_searchable(path):
                    matches.setdefault(path, set()).add(term)
                    if lineno:
                        hit_lines.setdefault(path, set()).add(lineno)

            # One extra grep per term buys the definition signal directly. Only
            # worth it when a ranker exists to use it.
            if self._ranker is None:
                continue
            name = term.rsplit(".", maxsplit=1)[-1]
            if not name.isidentifier():
                continue
            defined = await self._registry.call(
                "grep", pattern=DEFINITION_PATTERN.format(name=re.escape(name))
            )
            if not defined.ok:
                continue
            for line in defined.content.splitlines():
                path, lineno = _locate(line)
                if path and _is_searchable(path):
                    definers.add(path)
                    matches.setdefault(path, set()).add(term)
                    if lineno:
                        hit_lines.setdefault(path, set()).add(lineno)

        if not matches:
            return RetrieverOutput(strategy=self.name, confidence=0.0)

        if self._ranker is None:
            ranked = sorted(matches.items(), key=lambda kv: (-len(kv[1]), kv[0]))
            chosen = [(path, terms) for path, terms in ranked]
        else:
            # Reading candidates before ranking is the cost of a content-based
            # objective: you cannot tell a definition from a call without
            # looking. Bounded to the files that matched at all.
            bodies: dict[str, tuple[str, frozenset[str]]] = {}
            for path, hit_terms in sorted(matches.items(), key=lambda kv: (-len(kv[1]), kv[0]))[
                : self._max_files * 3
            ]:
                read = await self._registry.call("read_file", path=path)
                if read.ok:
                    bodies[path] = (_strip_gutter(read.content), frozenset(hit_terms))
            chosen = [(path, set(bodies[path][1])) for path, _, _ in self._ranker(bodies)]
        chosen = chosen[: min(self._max_files, k)]

        chunks: list[RetrievedChunk] = []
        for path, hit_terms in chosen:
            read = await self._registry.call("read_file", path=path)
            if not read.ok:
                continue
            lines = _strip_gutter(read.content).splitlines()
            why = f"matches {', '.join(sorted(hit_terms)[:4])}"
            score = len(hit_terms) / len(terms)
            for start, end in _windows(sorted(hit_lines.get(path, set())), len(lines)):
                chunks.append(
                    RetrievedChunk(
                        path=path,
                        start_line=start,
                        end_line=end,
                        score=score,
                        why=why,
                        content="\n".join(lines[start - 1 : end]),
                    )
                )

        # Confidence is the share of search terms the best file accounted for.
        # Honest rather than flattering: a single weak match reports low.
        best = max((len(t) for _, t in chosen), default=0)
        return RetrieverOutput(
            files=[c.path for c in chunks],
            chunks=chunks,
            confidence=round(best / len(terms), 3),
            strategy=self.name,
        )


#: Lines of context either side of a match. Wide enough that the enclosing
#: function is usually visible — the coder needs the exact surrounding text to
#: build an `edit_file` call that matches byte-for-byte — and still ~100x smaller
#: than handing over click's core.py.
WINDOW = 40

#: Two matches closer than this share one window rather than producing two
#: overlapping ones.
MERGE_GAP = 20


def _locate(line: str) -> tuple[str | None, int | None]:
    """Split a grep hit into (path, line number).

    grep emits `path:line:text`. A Windows drive letter is not a concern inside a
    Linux container, so the first two colons are the separators.
    """
    head, _, rest = line.partition(":")
    path = head.strip() or None
    number, _, _ = rest.partition(":")
    try:
        return path, int(number)
    except ValueError:
        return path, None


def _windows(hits: list[int], total: int) -> list[tuple[int, int]]:
    """Merge nearby hit lines into 1-indexed inclusive line ranges.

    With no hit lines (grep matched but the numbers were unparseable) the whole
    file is returned — losing the location is a reason to be generous, not to
    return nothing.
    """
    if not hits:
        return [(1, total)] if total else []
    spans: list[tuple[int, int]] = []
    for hit in hits:
        start, end = max(1, hit - WINDOW), min(total, hit + WINDOW)
        if spans and start - spans[-1][1] <= MERGE_GAP:
            spans[-1] = (spans[-1][0], max(spans[-1][1], end))
        else:
            spans.append((start, end))
    return spans


_SKIP_SUFFIXES = (".pyc", ".so", ".lock", ".png", ".jpg", ".pdf")
_SKIP_DIRS = ("__pycache__/", ".venv/", "node_modules/", ".git/")


def _is_searchable(path: str) -> bool:
    if path.endswith(_SKIP_SUFFIXES):
        return False
    return not any(part in path for part in _SKIP_DIRS)


def _strip_gutter(text: str) -> str:
    """Remove the `read_file` line-number gutter.

    The gutter helps a model quote text back for an edit, but a retrieved chunk
    is context — leaving numbers in it invites the model to copy them into a
    patch, and then `edit_file` fails on a no-match.
    """
    lines = []
    for line in text.splitlines():
        match = re.match(r"^\s*\d+\s{2}(.*)$", line)
        lines.append(match.group(1) if match else line)
    return "\n".join(lines)
