"""Phase 1A retrieval.

Pure: the retriever talks to a ToolRegistry, so a fake filesystem is enough. The
interesting assertions run against the *real* fixture issues, because term
extraction that works on invented text and fails on real bug reports is worthless.
"""

from __future__ import annotations

import pytest

from eval.dataset import CASES, TARGET_REPO, case_for
from fakes import FakeFileSystem
from featurepilot.retrieval.base import Retriever
from featurepilot.retrieval.filesystem import (
    FilesystemRetriever,
    _strip_gutter,
    candidate_terms,
)


def real_corpus() -> dict[str, str]:
    """The actual fixture repo, served from memory.

    Hand-written stubs would test the stubs' vocabulary rather than the
    retriever: the real issues name real symbols, and a corpus missing them makes
    any precision number meaningless. This is the same code the agent sees,
    minus the container.
    """
    root = TARGET_REPO
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if any(part in {".venv", "__pycache__", ".pytest_cache", ".git"} for part in rel.parts):
            continue
        if path.is_file() and path.suffix in {".py", ".md", ".toml"}:
            files[str(rel)] = path.read_text(encoding="utf-8")
    return files


CORPUS = real_corpus()


@pytest.fixture
def retriever() -> FilesystemRetriever:
    return FilesystemRetriever(FakeFileSystem(CORPUS).as_registry())


class TestProtocolConformance:
    def test_satisfies_the_retriever_protocol(self, retriever: FilesystemRetriever) -> None:
        """1B swaps implementations by config, which only works if they are
        interchangeable at the type level."""
        assert isinstance(retriever, Retriever)

    def test_names_itself_in_its_output(self, retriever: FilesystemRetriever) -> None:
        """Stage-over-stage comparisons need results attributable to a strategy."""
        assert retriever.name == "filesystem"


class TestTermExtraction:
    def test_prefers_quoted_and_backticked_spans(self) -> None:
        terms = candidate_terms("The `available_units` call breaks for 'NOPE-999'.")
        assert terms[0] in {"available_units", "NOPE-999"}
        assert {"available_units", "NOPE-999"} <= set(terms)

    def test_drops_prose(self) -> None:
        terms = candidate_terms("This should have been working when the customer called.")
        assert not {"should", "working", "customer"} & set(terms)

    def test_finds_snake_case_and_constants(self) -> None:
        terms = candidate_terms("FREE_SHIPPING_THRESHOLD is compared against the subtotal")
        assert "FREE_SHIPPING_THRESHOLD" in terms

    def test_empty_text_yields_nothing(self) -> None:
        assert candidate_terms("") == []

    def test_is_bounded(self) -> None:
        """An unbounded term list means one grep per word — slow and noisy."""
        assert len(candidate_terms(" ".join(f"symbol_{i}" for i in range(100)))) <= 12

    @pytest.mark.parametrize("case", CASES, ids=lambda c: c.issue)
    def test_real_issues_yield_terms(self, case) -> None:  # noqa: ANN001
        """Extraction has to work on the actual bug reports, not just on text
        written to make it look good."""
        terms = candidate_terms(case.read())
        assert len(terms) >= 3, f"{case.issue} produced too few terms: {terms}"


class TestRetrieval:
    async def test_finds_the_file_the_issue_is_about(self, retriever: FilesystemRetriever) -> None:
        issue = case_for("01-off-by-one.md").read()
        result = await retriever.retrieve(issue)
        assert "src/shopsvc/pricing.py" in result.files

    async def test_finds_inventory_for_the_sku_issue(self, retriever: FilesystemRetriever) -> None:
        issue = case_for("02-unknown-sku-crash.md").read()
        result = await retriever.retrieve(issue)
        assert "src/shopsvc/inventory.py" in result.files

    async def test_finds_cart_for_the_shipping_issue(self, retriever: FilesystemRetriever) -> None:
        issue = case_for("05-free-shipping-threshold.md").read()
        result = await retriever.retrieve(issue)
        assert "src/shopsvc/cart.py" in result.files

    async def test_chunks_explain_why_they_matched(self, retriever: FilesystemRetriever) -> None:
        """Retrieval quality is inspectable without re-running the retriever,
        and it is how 1B stages get compared."""
        result = await retriever.retrieve("available_units returns None for an unknown sku")
        assert result.chunks
        assert all(chunk.why for chunk in result.chunks)

    async def test_ranks_by_distinct_term_matches(self, retriever: FilesystemRetriever) -> None:
        """Counting distinct terms, not raw hits, stops one repeated word from
        dominating the ranking."""
        result = await retriever.retrieve(
            "shipping_for FREE_SHIPPING_THRESHOLD payable shipping threshold"
        )
        assert result.files[0] == "src/shopsvc/cart.py"

    async def test_no_terms_returns_empty_rather_than_everything(
        self, retriever: FilesystemRetriever
    ) -> None:
        result = await retriever.retrieve("the and for with")
        assert result.files == []
        assert result.confidence == 0.0

    async def test_no_matches_reports_zero_confidence(self, retriever: FilesystemRetriever) -> None:
        result = await retriever.retrieve("`quantum_flux_capacitor` is misaligned")
        assert result.confidence == 0.0

    async def test_confidence_is_honest_not_flattering(
        self, retriever: FilesystemRetriever
    ) -> None:
        """A single weak match should report low, so the planner and reviewer can
        treat thin context with suspicion."""
        strong = await retriever.retrieve(
            "tier_for min_quantity basis_points TIERS quantity_discount"
        )
        weak = await retriever.retrieve("`tier_for` and lots of unrelated_symbol_xyz words")
        assert strong.confidence > weak.confidence

    async def test_respects_k(self, retriever: FilesystemRetriever) -> None:
        result = await retriever.retrieve("sku stock_level tier_for shipping_for", k=1)
        assert len(result.chunks) <= 1

    async def test_prepare_is_a_noop(self, retriever: FilesystemRetriever) -> None:
        await retriever.prepare()  # must not raise


class TestGutterStripping:
    def test_removes_line_numbers(self) -> None:
        """A chunk keeps numbers only if we want the model copying them into an
        edit and getting a no-match."""
        assert _strip_gutter("    1  def f():\n    2      return 1") == "def f():\n    return 1"

    def test_preserves_code_indentation(self) -> None:
        assert _strip_gutter("   12          nested = True") == "        nested = True"

    def test_leaves_text_without_a_gutter_alone(self) -> None:
        body = "def f():\n    return 1"
        assert _strip_gutter(body) == body

    def test_does_not_eat_a_leading_number_in_real_code(self) -> None:
        """`10  ` inside source is not a gutter; only the two-space gutter form
        should be stripped."""
        assert _strip_gutter("x = 10  # ten") == "x = 10  # ten"
