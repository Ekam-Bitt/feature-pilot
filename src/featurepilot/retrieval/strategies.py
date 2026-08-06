"""One place that builds retrievers.

Both the running agent and the offline benchmark construct retrievers, and for a
while they did it separately. The benchmark measured a query builder and a content
ranker while production quietly used neither — so every improvement was real and
absent from the agent at the same time, and a paid end-to-end run would have
measured the old code and reported it as the new.

Shared factory, therefore. A strategy exists once, and the two callers can only
disagree about which name to ask for.
"""

from __future__ import annotations

from typing import Final

from featurepilot.retrieval.base import Retriever
from featurepilot.retrieval.filesystem import (
    FilesystemRetriever,
    candidate_terms,
    region_aware_terms,
)
from featurepilot.retrieval.ranker import rank
from featurepilot.tools.registry import ToolRegistry

#: Strategy names, ordered worst to best as measured by
#: `eval/retrieval_bench.py`. Every one stays available: the earlier rungs are the
#: benchmark's controls, and deleting a control makes the next comparison
#: unfalsifiable.
CONTROL: Final = "filesystem"
CLEAN_QUERY: Final = "filesystem+clean-query"
CONTENT_RANK: Final = "clean-query+content-rank"

#: What a run uses unless told otherwise. Measured better on both repositories in
#: the benchmark — P@3 0.33 -> 0.83 on click and 0.17 -> 0.50 on rich — at the
#: cost of roughly 2.5x the retrieval tool calls.
DEFAULT: Final = CONTENT_RANK

KNOWN: Final = (CONTROL, CLEAN_QUERY, CONTENT_RANK)


def build_retriever(kind: str, registry: ToolRegistry) -> Retriever:
    """Construct the named retrieval strategy.

    Phase 1B adds `embedding` and `hybrid` here. The graph is retrieval-agnostic,
    so a new strategy changes no node — that is what the `Retriever` protocol buys.
    """
    if kind == CONTROL:
        return FilesystemRetriever(registry, query_builder=candidate_terms)
    if kind == CLEAN_QUERY:
        return FilesystemRetriever(registry, query_builder=region_aware_terms)
    if kind == CONTENT_RANK:
        return FilesystemRetriever(registry, query_builder=region_aware_terms, ranker=rank)
    raise NotImplementedError(
        f"unknown retriever {kind!r}. Available: {', '.join(KNOWN)}. "
        "`embedding` and `hybrid` arrive in Phase 1B."
    )
