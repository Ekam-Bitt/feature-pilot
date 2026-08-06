"""Retrieval seam.

Nodes ask a `Retriever` and never know which strategy answered. That is what
makes Phase 1B a config change rather than a graph rewrite:

    FilesystemRetriever   (1A)  grep / glob / read_file
    EmbeddingRetriever    (1B)  AST chunks + dense vectors
    HybridRetriever       (1B)  BM25 + dense, RRF-fused, reranked

All three return `RetrieverOutput`, so precision@k is directly comparable
across stages — which is the point of staging them.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from featurepilot.contracts import RetrieverOutput


@runtime_checkable
class Retriever(Protocol):
    """Strategy interface for finding code relevant to a query."""

    #: Identifies the implementation in `RetrieverOutput.strategy` and in
    #: metrics, so a stage's numbers are attributable after the fact.
    name: str

    async def retrieve(self, query: str, *, k: int = 8) -> RetrieverOutput: ...

    async def prepare(self) -> None:
        """Build whatever the strategy needs before first use.

        A no-op for filesystem search; an index build for the 1B strategies.
        The graph calls this during RunPhase.INDEXING, which is why that phase
        exists in the enum from day one.
        """
        ...
