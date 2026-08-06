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

from featurepilot.contracts import RetrievedChunk, RetrieverOutput
from featurepilot.tools.registry import ToolRegistry
from featurepilot.tracing import traced

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


class FilesystemRetriever:
    """Search-based retrieval over the sandbox worktree."""

    name = "filesystem"

    def __init__(self, registry: ToolRegistry, *, max_files: int = 6) -> None:
        self._registry = registry
        self._max_files = max_files

    async def prepare(self) -> None:
        """No index to build. Present because the protocol promises it, and
        because Phase 1B's strategies do have work to do here."""
        return None

    @traced("filesystem_retrieve", run_type="retriever")
    async def retrieve(self, query: str, *, k: int = 8) -> RetrieverOutput:
        terms = candidate_terms(query)
        if not terms:
            return RetrieverOutput(strategy=self.name, confidence=0.0)

        # path -> the terms that matched in it. Counting distinct terms rather
        # than raw hits stops one repeated word from dominating the ranking.
        matches: dict[str, set[str]] = {}
        for term in terms:
            result = await self._registry.call("grep", pattern=re.escape(term))
            if not result.ok:
                continue
            for line in result.content.splitlines():
                path = _path_of(line)
                if path and _is_searchable(path):
                    matches.setdefault(path, set()).add(term)

        if not matches:
            return RetrieverOutput(strategy=self.name, confidence=0.0)

        ranked = sorted(matches.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        chosen = ranked[: min(self._max_files, k)]

        chunks: list[RetrievedChunk] = []
        for path, hit_terms in chosen:
            read = await self._registry.call("read_file", path=path)
            if not read.ok:
                continue
            body = _strip_gutter(read.content)
            chunks.append(
                RetrievedChunk(
                    path=path,
                    start_line=1,
                    end_line=body.count("\n") + 1,
                    score=len(hit_terms) / len(terms),
                    why=f"matches {', '.join(sorted(hit_terms)[:4])}",
                    content=body,
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


#: grep output is `path:line:text`; a Windows-style drive letter is not a concern
#: inside a Linux container, so the first colon is the separator.
def _path_of(line: str) -> str | None:
    head, _, _ = line.partition(":")
    return head.strip() or None


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
