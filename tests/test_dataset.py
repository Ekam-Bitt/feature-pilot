"""The eval set validates itself against the fixture repo.

An eval set that has drifted from reality is worse than none: it reports
confident numbers about the wrong thing. So the expected-failure map is checked
against what pytest actually reports, rather than trusted because it was correct
when written.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from eval.dataset import CASES, TARGET_REPO, TOTAL_TESTS, all_expected_failures, case_for

FIXTURE_PYTHON = TARGET_REPO / ".venv" / "bin" / "python"
needs_fixture_env = pytest.mark.skipif(
    not FIXTURE_PYTHON.exists(),
    reason="fixture venv absent; run `uv pip install --python fixtures/target-repo/.venv -e ...`",
)


class TestDatasetShape:
    def test_every_issue_file_exists(self) -> None:
        for case in CASES:
            assert case.issue_path.is_file(), f"missing issue file: {case.issue_path}"

    def test_every_issue_is_non_trivial(self) -> None:
        """A one-line issue can't exercise planning."""
        for case in CASES:
            assert len(case.read()) > 400, f"{case.issue} looks too thin to plan against"

    def test_no_test_belongs_to_two_issues(self) -> None:
        """Overlap would make a failure report ambiguous about which defect
        caused it, which defeats per-issue scoring."""
        counts = Counter(t for case in CASES for t in case.expected_failures)
        overlapping = {test: n for test, n in counts.items() if n > 1}
        assert not overlapping, f"tests claimed by multiple issues: {overlapping}"

    def test_expected_files_are_real(self) -> None:
        for case in CASES:
            for rel in case.expected_files:
                assert (TARGET_REPO / rel).is_file(), f"{case.issue} names missing {rel}"

    def test_exactly_one_case_needs_the_repair_loop(self) -> None:
        """The 1A exit gate checks debugger re-entry, so at least one case must
        reliably require it — and only one is needed to keep runs cheap."""
        repair = [c.issue for c in CASES if c.requires_repair_loop]
        assert len(repair) == 1, f"expected exactly one repair-loop case, got {repair}"

    def test_difficulty_spread(self) -> None:
        levels = {c.difficulty for c in CASES}
        assert {"easy", "medium", "hard"} <= levels, f"thin difficulty spread: {levels}"

    def test_the_answer_key_is_not_inside_the_target_repo(self) -> None:
        """The agent can read anything in the target repo, so the expected-failure
        map living there would be leakage."""
        assert not (TARGET_REPO / "issues").exists(), "issues must live outside the target repo"
        dataset = Path(__file__).resolve().parents[1] / "eval" / "dataset.py"
        assert TARGET_REPO not in dataset.parents


@needs_fixture_env
class TestAgainstReality:
    """Runs the fixture suite and compares against the map."""

    @staticmethod
    def _failing_tests(repo: Path) -> set[str]:
        report = repo / ".report.json"
        proc = subprocess.run(  # noqa: S603
            [
                str(repo / ".venv" / "bin" / "python"),
                "-m",
                "pytest",
                "-q",
                "--tb=no",
                "-p",
                "no:cacheprovider",
            ],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=300,
        )
        report.unlink(missing_ok=True)
        failing: set[str] = set()
        for line in proc.stdout.splitlines():
            if line.startswith("FAILED "):
                failing.add(line.removeprefix("FAILED ").split(" ")[0].strip())
        return failing

    def test_the_fixture_fails_exactly_where_documented(self) -> None:
        """Any extra failure is an accidental defect; any missing one means a
        seeded defect stopped being reachable."""
        actual = self._failing_tests(TARGET_REPO)
        expected = set(all_expected_failures())
        assert actual == expected, (
            f"unexpected failures: {sorted(actual - expected)}\n"
            f"expected but passing: {sorted(expected - actual)}"
        )

    def test_total_test_count_is_current(self) -> None:
        proc = subprocess.run(  # noqa: S603
            [str(FIXTURE_PYTHON), "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
            cwd=TARGET_REPO,
            capture_output=True,
            text=True,
            timeout=300,
        )
        collected = [ln for ln in proc.stdout.splitlines() if "::" in ln]
        assert len(collected) == TOTAL_TESTS, (
            f"dataset says {TOTAL_TESTS} tests, fixture collects {len(collected)}"
        )

    def test_fixture_is_solvable(self, tmp_path: Path) -> None:
        """Applies every documented fix to a copy and asserts the suite goes
        fully green. Without this, a failed run could always be blamed on an
        unfixable fixture."""
        work = tmp_path / "repo"
        shutil.copytree(TARGET_REPO, work, symlinks=True)

        pricing = work / "src/shopsvc/pricing.py"
        pricing.write_text(
            pricing.read_text().replace(
                "if quantity > tier.min_quantity:", "if quantity >= tier.min_quantity:"
            )
        )
        inv = work / "src/shopsvc/inventory.py"
        inv.write_text(
            inv.read_text().replace(
                "    return stock_level(sku) + 0",
                "    level = stock_level(sku)\n    return 0 if level is None else level",
            )
        )
        repo_py = work / "src/shopsvc/repository.py"
        repo_py.write_text(
            repo_py.read_text().replace(
                "FROM orders o\n        JOIN order_items oi",
                "FROM orders o\n        LEFT JOIN order_items oi",
            )
        )
        promos = work / "src/shopsvc/promotions.py"
        promos.write_text(
            promos.read_text()
            + "\n\ndef combined_discount(codes: list[str], subtotal: int) -> int:\n"
            "    total_bp = 0\n"
            "    for code in codes:\n"
            "        promotion = lookup(code)\n"
            "        if is_applicable(promotion, subtotal):\n"
            "            total_bp += promotion.basis_points\n"
            "    total_bp = min(total_bp, MAX_TOTAL_BASIS_POINTS)\n"
            "    return subtotal * total_bp // 10_000\n"
        )
        cart = work / "src/shopsvc/cart.py"
        cart.write_text(
            cart.read_text()
            .replace(
                "    return promotions.discount_for(cart.promo_codes[0], cart.subtotal)",
                "    return promotions.combined_discount(cart.promo_codes, cart.subtotal)",
            )
            .replace(
                "    shipping = shipping_for(subtotal)",
                "    shipping = shipping_for(subtotal - quantity_disc - promo_disc)",
            )
        )

        proc = subprocess.run(  # noqa: S603
            [str(FIXTURE_PYTHON), "-m", "pytest", "-q", "--tb=short", "-p", "no:cacheprovider"],
            cwd=work,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert proc.returncode == 0, (
            f"fixture is not solvable as documented:\n{proc.stdout[-3000:]}"
        )

    @pytest.mark.parametrize("case", CASES, ids=lambda c: c.issue)
    def test_each_case_failures_are_currently_red(self, case) -> None:  # noqa: ANN001
        """Per-case view of the same check, so a broken case names itself."""
        actual = self._failing_tests(TARGET_REPO)
        missing = set(case.expected_failures) - actual
        assert not missing, (
            f"{case.issue}: documented failures are already passing: {sorted(missing)}"
        )


def test_case_lookup_accepts_a_path() -> None:
    assert case_for("fixtures/issues/01-off-by-one.md").difficulty == "easy"


def test_case_lookup_rejects_unknown() -> None:
    with pytest.raises(KeyError, match="unknown issue"):
        case_for("99-nope.md")


def test_dataset_is_json_serialisable() -> None:
    """The baseline harness writes results alongside case metadata."""
    payload = [
        {
            "issue": c.issue,
            "difficulty": c.difficulty,
            "expected_failures": sorted(c.expected_failures),
            "expected_files": sorted(c.expected_files),
            "requires_repair_loop": c.requires_repair_loop,
        }
        for c in CASES
    ]
    assert json.loads(json.dumps(payload)) == payload
