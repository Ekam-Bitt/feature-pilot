"""The Phase 1A evaluation set.

Deliberately lives *outside* `fixtures/target-repo/`. The target repo is copied
into the sandbox where the agent can read anything in it, so an expected-failure
map stored there would be answer-key leakage — the agent could read which tests
it needs to flip instead of diagnosing the defect.

`expected_failures` is the exact set of tests that are red before a fix and green
after, verified by applying each fix by hand. That makes three things checkable:

- **Solvability** — the fixture is known-fixable, so a failure is the agent's.
- **Attribution** — no test appears under two issues, so a failure report points
  at one defect rather than an ambiguous set.
- **Scoring** — "did it fix the issue" is a set comparison, not a judgement call.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

ISSUES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "issues"
TARGET_REPO = Path(__file__).resolve().parents[1] / "fixtures" / "target-repo"

#: The same fixture, published so a demo is reproducible by anyone and so the
#: GitHub path (clone a URL, read a real issue, push a branch, open a PR) is
#: exercised for real rather than simulated.
GITHUB_REPO = "Ekam-Bitt/featurepilot-fixture"
GITHUB_URL = f"https://github.com/{GITHUB_REPO}"

#: Total tests in the fixture suite when everything passes.
TOTAL_TESTS = 85


@dataclass(frozen=True, slots=True)
class Case:
    """One issue, plus everything needed to score an attempt at it."""

    issue: str
    #: Issue number on GITHUB_REPO, so a run can be driven from the real issue
    #: rather than the local markdown copy.
    github_issue: int
    #: Tests that must go from red to green. Verified by hand-applying the fix.
    expected_failures: frozenset[str]
    #: Files a correct fix is expected to touch. Used to score scope discipline
    #: — a patch spraying across unrelated modules is a worse fix even if green.
    expected_files: frozenset[str]
    difficulty: str
    notes: str = ""
    #: True when the obvious first fix is incomplete, so a correct run must go
    #: through the debugger and re-enter the coder.
    requires_repair_loop: bool = False
    tags: frozenset[str] = field(default_factory=frozenset)

    @property
    def issue_path(self) -> Path:
        return ISSUES_DIR / self.issue

    def read(self) -> str:
        return self.issue_path.read_text(encoding="utf-8")


CASES: tuple[Case, ...] = (
    Case(
        issue="01-off-by-one.md",
        github_issue=1,
        expected_failures=frozenset(
            {
                "tests/test_pricing.py::TestTierSelection::test_tier_applies_at_exactly_five",
                "tests/test_pricing.py::TestTierSelection::test_middle_tier_applies_at_exactly_ten",
                "tests/test_pricing.py::TestTierSelection::test_top_tier_applies_at_exactly_twenty",
                "tests/test_pricing.py::TestDiscountAmount::test_five_percent_at_the_boundary",
                "tests/test_pricing.py::TestDiscountAmount::test_ten_percent_at_the_boundary",
                "tests/test_pricing.py::TestDiscountAmount::test_fifteen_percent_at_the_boundary",
                "tests/test_pricing.py::TestAcrossLines::test_sums_per_line_independently",
            }
        ),
        expected_files=frozenset({"src/shopsvc/pricing.py"}),
        difficulty="easy",
        notes="One comparison operator. The baseline should also solve this.",
        tags=frozenset({"off-by-one", "single-file"}),
    ),
    Case(
        issue="02-unknown-sku-crash.md",
        github_issue=2,
        expected_failures=frozenset(
            {
                "tests/test_inventory.py::TestAvailableUnits::test_unknown_sku_is_zero_not_an_error",
                "tests/test_inventory.py::TestAvailability::test_unknown_sku_is_unavailable",
                "tests/test_inventory.py::TestReserve::test_reserving_unknown_sku_raises",
                "tests/test_inventory.py::TestRestock::test_restock_creates_a_ledger_entry",
            }
        ),
        expected_files=frozenset({"src/shopsvc/inventory.py"}),
        difficulty="easy",
        notes=(
            "A None guard in one function. Four tests fail through three "
            "different call paths, so the fix must go in the shared helper "
            "rather than at each call site."
        ),
        tags=frozenset({"null-guard", "single-file"}),
    ),
    Case(
        issue="03-missing-draft-orders.md",
        github_issue=3,
        expected_failures=frozenset(
            {
                "tests/test_repository.py::TestOrderHistory::test_returns_every_order_for_the_customer",
                "tests/test_repository.py::TestOrderHistory::test_includes_a_draft_with_no_items",
            }
        ),
        expected_files=frozenset({"src/shopsvc/repository.py"}),
        difficulty="medium",
        notes="INNER JOIN drops item-less orders; needs LEFT JOIN. Requires reading SQL.",
        tags=frozenset({"sql", "single-file"}),
    ),
    Case(
        issue="04-promo-stacking.md",
        github_issue=4,
        expected_failures=frozenset(
            {
                "tests/test_cart.py::TestPromoStacking::test_two_codes_stack_additively",
                "tests/test_cart.py::TestPromoStacking::test_stack_is_capped",
            }
        ),
        expected_files=frozenset({"src/shopsvc/promotions.py", "src/shopsvc/cart.py"}),
        difficulty="hard",
        notes=(
            "Cross-module: promotions.py needs a stacking function that enforces "
            "the cap, and cart.py has to call it instead of indexing [0]. A fix "
            "confined to one file cannot pass both tests."
        ),
        tags=frozenset({"cross-module"}),
    ),
    Case(
        issue="05-free-shipping-threshold.md",
        github_issue=5,
        expected_failures=frozenset(
            {
                "tests/test_cart.py::TestShippingUsesThePayableAmount::test_promo_discount_drops_order_below_threshold",
                "tests/test_cart.py::TestShippingUsesThePayableAmount::test_quantity_discount_drops_order_below_threshold",
            }
        ),
        expected_files=frozenset({"src/shopsvc/cart.py"}),
        difficulty="hard",
        requires_repair_loop=True,
        notes=(
            "Verified two-step. Subtracting only the promo discount from the "
            "shipping basis fixes the first test and leaves the second red, so a "
            "correct run passes through the debugger and back into the coder. "
            "The inclusive-threshold tests guard against flipping >= to > while "
            "fixing it."
        ),
        tags=frozenset({"repair-loop", "boundary"}),
    ),
)

BY_ISSUE: dict[str, Case] = {case.issue: case for case in CASES}


def case_for(issue: str) -> Case:
    """Look up a case by issue filename or by path."""
    key = Path(issue).name
    try:
        return BY_ISSUE[key]
    except KeyError:
        raise KeyError(f"unknown issue {issue!r}; known: {sorted(BY_ISSUE)}") from None


def all_expected_failures() -> frozenset[str]:
    return frozenset().union(*(case.expected_failures for case in CASES))
