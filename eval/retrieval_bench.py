"""Offline retrieval benchmark — the project's primary source of truth.

Every experiment so far measured the whole pipeline at once, so a failure could
have come from retrieval, planning, coding, the tool loop, or scoring. This
isolates one subsystem and asks one question:

    given an issue, does the retriever rank the file that actually needs fixing?

Nothing else. No model calls, no container, no API spend — the corpus is read from
disk into memory and retrieval is deterministic, so this runs in seconds and can
be re-run after every change. That inverts the economics of the whole project:
the bottleneck question is answerable for free, while a single end-to-end run
costs $1-2 and confounds five subsystems.

Metrics, and why each is here:

- **P@1 / P@3** — is the right file first, or near the top.
- **MRR** — credits a strategy for ranking the truth 2nd over 8th, which P@1
  cannot see.
- **context bytes** — two strategies with identical P@3 are not equivalent if one
  returns 3 KB and the other 80 KB. This is what downstream token cost is made of.
- **files returned** — breadth. A strategy that returns everything scores well on
  recall and helps nobody.
- **search effort** (grep/read calls, bytes scanned) — predicts how much work the
  retriever itself costs before the coder sees anything.

Usage:
    uv run python -m eval.retrieval_bench                  # every strategy
    uv run python -m eval.retrieval_bench --strategy filesystem
"""

from __future__ import annotations

import argparse
import asyncio
import fnmatch
import json
import re
import statistics
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from eval.oss import OSSCase
from featurepilot.contracts import RetrieverOutput
from featurepilot.retrieval.base import Retriever
from featurepilot.retrieval.ranker import extract, looks_like_implementation
from featurepilot.retrieval.strategies import KNOWN, build_retriever
from featurepilot.tools.registry import Tool, ToolRegistry, ToolResult

OSS_CASES_FILE = Path(__file__).resolve().parent / "oss_cases.json"
RETRIEVAL_CASES_FILE = Path(__file__).resolve().parent / "retrieval_cases.json"
RESULTS = Path(__file__).resolve().parent / "results"

#: Extensions the corpus loads. Retrieval only ever needs to rank source and docs.
CORPUS_SUFFIXES = {".py", ".md", ".rst", ".toml", ".cfg", ".txt"}
SKIP_PARTS = {".venv", "__pycache__", ".pytest_cache", ".git", "node_modules"}


# --------------------------------------------------------------------------
# In-memory corpus: the same tool surface the agent gets, without a container.
# --------------------------------------------------------------------------


class MemoryCorpus:
    """A repository in memory, exposed through the real `ToolRegistry` interface.

    Deliberately the same tool names and result shapes the MCP servers provide, so
    a retriever cannot tell the difference — which is what makes an offline score
    meaningful rather than a simulation of one.
    """

    def __init__(self, root: Path) -> None:
        self.files: dict[str, str] = {}
        for path in sorted(root.rglob("*")):
            rel = path.relative_to(root)
            if any(part in SKIP_PARTS for part in rel.parts):
                continue
            if path.is_file() and path.suffix in CORPUS_SUFFIXES:
                try:
                    self.files[str(rel)] = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
        #: Bytes returned by tool calls, i.e. what the retriever had to scan.
        self.bytes_scanned = 0

    async def read_file(self, path: str, offset: int = 0, limit: int = 0) -> ToolResult:
        key = path.removeprefix("./")
        if key not in self.files:
            return ToolResult.error(f"Error: no such file: {path}")
        lines = self.files[key].splitlines()
        start = max(1, offset or 1)
        end = min(len(lines), start + limit - 1) if limit else len(lines)
        body = "\n".join(
            f"{i:>5}  {line}" for i, line in enumerate(lines[start - 1 : end], start=start)
        )
        self.bytes_scanned += len(body)
        return ToolResult(body)

    async def grep(self, pattern: str, path: str = ".") -> ToolResult:
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return ToolResult.error(f"Error: bad pattern: {exc}")
        hits = [
            f"{name}:{i + 1}:{line}"
            for name, body in sorted(self.files.items())
            for i, line in enumerate(body.splitlines())
            if rx.search(line)
        ]
        out = "\n".join(hits[:2000])
        self.bytes_scanned += len(out)
        return ToolResult(out or "No matches.", ok=bool(hits))

    async def glob(self, pattern: str) -> ToolResult:
        hits = [p for p in sorted(self.files) if fnmatch.fnmatch(p, pattern)]
        out = "\n".join(hits)
        self.bytes_scanned += len(out)
        return ToolResult(out or "No files match.")

    def as_registry(self) -> ToolRegistry:
        reg = ToolRegistry()
        reg.register_all(
            [
                Tool("read_file", "Read a file.", {}, self.read_file, read_only=True),
                Tool("grep", "Search contents.", {}, self.grep, read_only=True),
                Tool("glob", "Match paths.", {}, self.glob, read_only=True),
            ]
        )
        return reg


# --------------------------------------------------------------------------
# Scoring
# --------------------------------------------------------------------------


@dataclass(slots=True)
class CaseScore:
    strategy: str
    case: str
    repo: str
    truth: list[str]
    ranked: list[str]
    context_bytes: int
    files_returned: int
    grep_calls: int
    read_calls: int
    bytes_scanned: int
    #: 1-indexed rank of the first implementation file (not prose, not tests).
    #: More sensitive than P@3 for a ranking problem: it shows how far down the
    #: list real code appears even when the *right* file is missed entirely.
    #: 0 means none was returned.
    impl_rank: int = 0

    def hit_at(self, k: int) -> bool:
        return bool(set(self.ranked[:k]) & set(self.truth))

    @property
    def reciprocal_rank(self) -> float:
        """1/rank of the first correct file, 0 if it never appears.

        Rewards ranking the answer 2nd over 8th — a distinction P@1 discards and
        which matters, because the coder reads the top of the list first.
        """
        for i, path in enumerate(self.ranked, start=1):
            if path in self.truth:
                return 1.0 / i
        return 0.0


@dataclass(slots=True)
class StrategyReport:
    strategy: str
    scores: list[CaseScore] = field(default_factory=list)

    def _mean(self, fn: Callable[[CaseScore], float]) -> float:
        return statistics.mean(fn(s) for s in self.scores) if self.scores else 0.0

    @property
    def p_at_1(self) -> float:
        return self._mean(lambda s: float(s.hit_at(1)))

    @property
    def p_at_3(self) -> float:
        return self._mean(lambda s: float(s.hit_at(3)))

    @property
    def mrr(self) -> float:
        return self._mean(lambda s: s.reciprocal_rank)

    @property
    def avg_context_bytes(self) -> float:
        return self._mean(lambda s: float(s.context_bytes))

    @property
    def avg_files(self) -> float:
        return self._mean(lambda s: float(s.files_returned))

    @property
    def avg_effort(self) -> float:
        return self._mean(lambda s: float(s.grep_calls + s.read_calls))

    @property
    def avg_scanned(self) -> float:
        return self._mean(lambda s: float(s.bytes_scanned))

    @property
    def avg_impl_rank(self) -> float:
        """Mean rank of the first implementation file; misses count as 9."""
        return self._mean(lambda s: float(s.impl_rank or 9))


# --------------------------------------------------------------------------
# Strategies. Each entry builds a Retriever over a registry.
# --------------------------------------------------------------------------

StrategyFactory = Callable[[ToolRegistry], Retriever]

#: Built from the same factory the agent uses. Sharing it is the point: these
#: rows are only meaningful if they describe the code that actually runs.
STRATEGIES: dict[str, StrategyFactory] = {
    name: (lambda reg, n=name: build_retriever(n, reg))  # type: ignore[misc]
    for name in KNOWN
}


async def score_case(strategy: str, factory: StrategyFactory, case: BenchCase) -> CaseScore:
    corpus = MemoryCorpus(case.directory)
    registry = corpus.as_registry()
    retriever = factory(registry)
    await retriever.prepare()

    result: RetrieverOutput = await retriever.retrieve(case.issue, k=8)

    # Rank order, de-duplicated: a file's best chunk decides its position.
    ranked: list[str] = []
    for chunk in result.chunks:
        if chunk.path.removeprefix("./") not in ranked:
            ranked.append(chunk.path.removeprefix("./"))

    # Where does real code first appear? Decided from content, so the metric
    # does not assume the layout the ranker deliberately ignores.
    impl_rank = 0
    for position, path in enumerate(ranked, start=1):
        body = corpus.files.get(path, "")
        if looks_like_implementation(extract(path, body, frozenset())):
            impl_rank = position
            break

    calls = registry.calls
    return CaseScore(
        strategy=strategy,
        case=case.label,
        repo=case.repo,
        truth=sorted(case.truth),
        ranked=ranked,
        context_bytes=sum(len(c.content) for c in result.chunks),
        files_returned=len(ranked),
        grep_calls=sum(1 for c in calls if c["tool"] == "grep"),
        read_calls=sum(1 for c in calls if c["tool"] == "read_file"),
        bytes_scanned=corpus.bytes_scanned,
        impl_rank=impl_rank,
    )


@dataclass(slots=True)
class BenchCase:
    """A case the benchmark can score, from either case file."""

    repo: str
    label: str
    directory: Path
    issue: str
    truth: frozenset[str]


def _load_cases() -> list[BenchCase]:
    """Every case from both files: validated click cases and retrieval-only ones.

    Two repositories rather than one because click keeps its package under `src/`,
    so a ranker that merely preferred `src/**` would look perfect while having
    learned nothing. `rich` has no `src/` directory, so that shortcut scores zero
    there — results are reported per repository so a layout-shaped win cannot hide
    in an average.
    """
    cases: list[BenchCase] = []
    cases.extend(_load_validated())
    cases.extend(_load_retrieval_only())
    if not cases:
        raise SystemExit(
            "no cases. Build them with:\n"
            "  uv run python -m eval.oss_build --limit 200 --want 6\n"
            "  uv run python -m eval.retrieval_cases --repo Textualize/rich "
            "--clone /tmp/oss-eval/rich"
        )
    return cases


def _load_retrieval_only() -> list[BenchCase]:
    if not RETRIEVAL_CASES_FILE.is_file():
        return []
    payload = json.loads(RETRIEVAL_CASES_FILE.read_text())
    out: list[BenchCase] = []
    for repo, raws in payload.items():
        for raw in raws:
            directory = (
                Path(__file__).resolve().parents[1]
                / ".fp"
                / "oss"
                / f"{repo.split('/')[-1]}-{raw['sha'][:9]}"
            )
            if not directory.is_dir():
                continue
            out.append(
                BenchCase(
                    repo=repo,
                    label=raw["sha"][:9],
                    directory=directory,
                    issue=raw["issue"],
                    truth=frozenset(raw["source_files"]),
                )
            )
    return out


def _load_validated() -> list[BenchCase]:
    if not OSS_CASES_FILE.is_file():
        return []
    payload = json.loads(OSS_CASES_FILE.read_text())
    out: list[BenchCase] = []
    for raw in payload["cases"]:
        case = OSSCase(
            repo=payload["repo"],
            sha=raw["sha"],
            title=raw["title"],
            issue=raw["issue"],
            issue_source=raw["issue_source"],
            source_files=frozenset(raw["source_files"]),
        )
        if not case.case_dir.is_dir():
            continue
        out.append(
            BenchCase(
                repo=payload["repo"],
                label=case.sha[:9],
                directory=case.case_dir,
                issue=case.issue,
                truth=frozenset(raw["source_files"]),
            )
        )
    return out


def _per_repo(reports: list[StrategyReport]) -> str:
    """Break every strategy down by repository.

    The whole reason a second repository exists: an average over both would let a
    gain that only works on click's `src/**` layout look general.
    """
    repos = sorted({s.repo for r in reports for s in r.scores})
    lines: list[str] = []
    for repo in repos:
        lines.append(f"\n{repo}")
        header = f"  {'strategy':<26}{'P@1':>6}{'P@3':>6}{'MRR':>7}{'implR':>7}{'cases':>7}"
        lines += [header, "  " + "-" * (len(header) - 2)]
        for report in reports:
            subset = StrategyReport(
                strategy=report.strategy,
                scores=[s for s in report.scores if s.repo == repo],
            )
            if not subset.scores:
                continue
            lines.append(
                f"  {subset.strategy:<26}{subset.p_at_1:>6.2f}{subset.p_at_3:>6.2f}"
                f"{subset.mrr:>7.3f}{subset.avg_impl_rank:>7.1f}{len(subset.scores):>7}"
            )
    return "\n".join(lines)


def _table(reports: list[StrategyReport]) -> str:
    header = (
        f"{'strategy':<26}{'P@1':>6}{'P@3':>6}{'MRR':>7}{'implR':>7}"
        f"{'ctx bytes':>11}{'files':>7}{'effort':>8}{'scanned':>10}"
    )
    rows = [header, "-" * len(header)]
    for r in reports:
        rows.append(
            f"{r.strategy:<26}{r.p_at_1:>6.2f}{r.p_at_3:>6.2f}{r.mrr:>7.3f}"
            f"{r.avg_impl_rank:>7.1f}{r.avg_context_bytes:>11,.0f}{r.avg_files:>7.1f}"
            f"{r.avg_effort:>8.1f}{r.avg_scanned:>10,.0f}"
        )
    return "\n".join(rows)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Offline retrieval benchmark.")
    parser.add_argument("--strategy", nargs="*", help="Subset of strategy names.")
    parser.add_argument("--per-case", action="store_true", help="Show every case.")
    args = parser.parse_args()

    cases = _load_cases()
    names = [n for n in STRATEGIES if not args.strategy or n in args.strategy]
    if not names:
        print(f"no such strategy. Available: {', '.join(STRATEGIES)}")
        return 2

    print(f"{len(cases)} case(s), {len(names)} strategy(ies) — no model calls\n")

    reports: list[StrategyReport] = []
    for name in names:
        report = StrategyReport(strategy=name)
        for case in cases:
            report.scores.append(await score_case(name, STRATEGIES[name], case))
        reports.append(report)

        if args.per_case:
            print(f"=== {name} ===")
            for score in report.scores:
                mark = "HIT " if score.hit_at(3) else "miss"
                print(
                    f"  {mark} {score.case}  rr={score.reciprocal_rank:.2f}  "
                    f"want={score.truth}  got={score.ranked[:4]}"
                )
            print()

    print(_table(reports))
    print(_per_repo(reports))

    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / "retrieval-bench.json"
    path.write_text(
        json.dumps(
            {
                r.strategy: {
                    "p_at_1": round(r.p_at_1, 4),
                    "p_at_3": round(r.p_at_3, 4),
                    "mrr": round(r.mrr, 4),
                    "avg_context_bytes": round(r.avg_context_bytes),
                    "avg_files": round(r.avg_files, 2),
                    "avg_effort": round(r.avg_effort, 2),
                    "avg_bytes_scanned": round(r.avg_scanned),
                    "cases": [
                        {
                            "case": s.case,
                            "rr": round(s.reciprocal_rank, 4),
                            "truth": s.truth,
                            "ranked": s.ranked,
                        }
                        for s in r.scores
                    ],
                }
                for r in reports
            },
            indent=2,
        )
        + "\n"
    )
    print(f"\nwritten to {path.relative_to(Path.cwd())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
