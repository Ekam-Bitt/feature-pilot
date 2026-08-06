"""Characterise how the agent searches, from persisted tool calls.

A measurement, not a change. The click runs failed four different ways while
costing $3.77, and the useful question is no longer "what should we optimise" but
"what did it actually spend its calls on". Every tool call is already persisted
with its arguments, so this answers that from data already paid for rather than
from another run.

What it looks for, in order of how cheap the fix would be:

- **Duplicate calls** — the exact same tool with the exact same arguments. Pure
  waste: a cache would remove them with no change to any algorithm.
- **Repeated file reads** — the same file read more than once, possibly at
  different ranges. A file cache removes these.
- **Low-yield greps** — patterns matching so much that the result is noise, or so
  little that the term was never worth searching. Both indicate bad term
  selection rather than bad retrieval.
- **Per-node distribution** — whether the searching is the retriever's doing or
  the coder's, which decides where a fix belongs.

Usage:
    uv run python -m eval.search_profile                 # every run
    uv run python -m eval.search_profile --run d4f132cad8f3
"""

from __future__ import annotations

import argparse
import json
import subprocess
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

#: Below this, a grep result is a near-miss: the term existed but told us almost
#: nothing. Above the high mark, the pattern matched so widely the result is noise.
LOW_YIELD_CHARS = 120
NOISE_CHARS = 2_000


def _fetch(run_id: str | None) -> list[dict[str, Any]]:
    """Read tool-call events out of Postgres via the compose container.

    Uses `docker exec` rather than a psycopg connection so the profiler works
    without the project's virtualenv being importable — it is a diagnostic, and a
    diagnostic that needs the system healthy to run is not much use.
    """
    where = f"and run_id = '{run_id}'" if run_id else ""
    # The run id is folded into the JSON rather than concatenated with a
    # separator: a tab does not survive the shell -> psql hop intact, and any
    # printable delimiter risks colliding with the payload's own contents.
    sql = (
        "select jsonb_set(payload, '{run_id}', to_jsonb(run_id))::text "
        f"from metric_events where kind = 'tool_called' {where} order by id"
    )
    proc = subprocess.run(  # noqa: S603
        [
            "docker",
            "exec",
            "multi-agentsdeassistant-postgres-1",
            "psql",
            "-U",
            "featurepilot",
            "-d",
            "featurepilot",
            "-tAc",
            sql,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise SystemExit(f"could not read metrics: {proc.stderr.strip()[:200]}")

    rows: list[dict[str, Any]] = []
    for line in proc.stdout.splitlines():
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            rows.append(payload)
    return rows


@dataclass(slots=True)
class Profile:
    run_id: str
    calls: int = 0
    #: (tool, canonical args) seen more than once — removable by a cache.
    duplicates: Counter[str] = field(default_factory=Counter)
    files_read: Counter[str] = field(default_factory=Counter)
    greps: list[tuple[str, int]] = field(default_factory=list)
    by_node: Counter[str] = field(default_factory=Counter)
    by_tool: Counter[str] = field(default_factory=Counter)
    failed: int = 0

    @property
    def duplicate_calls(self) -> int:
        """Calls that repeated an earlier identical call."""
        return sum(n - 1 for n in self.duplicates.values() if n > 1)

    @property
    def unique_files(self) -> int:
        return len(self.files_read)

    @property
    def repeated_reads(self) -> int:
        return sum(n - 1 for n in self.files_read.values() if n > 1)

    @property
    def noisy_greps(self) -> list[tuple[str, int]]:
        return [(p, n) for p, n in self.greps if n >= NOISE_CHARS]

    @property
    def empty_greps(self) -> list[tuple[str, int]]:
        return [(p, n) for p, n in self.greps if n < LOW_YIELD_CHARS]

    @property
    def wasted_fraction(self) -> float:
        """Share of calls that a cache alone would have removed."""
        return self.duplicate_calls / self.calls if self.calls else 0.0


def profile(rows: list[dict[str, Any]]) -> dict[str, Profile]:
    out: dict[str, Profile] = {}
    for row in rows:
        run = str(row.get("run_id", "?"))
        prof = out.setdefault(run, Profile(run_id=run))
        tool = str(row.get("tool", "?"))
        args = row.get("args") or {}
        node = str(row.get("node") or "?")

        prof.calls += 1
        prof.by_node[node] += 1
        prof.by_tool[tool] += 1
        if not row.get("ok", True):
            prof.failed += 1

        # Canonical form so argument order cannot hide a duplicate.
        key = f"{tool}({json.dumps(args, sort_keys=True)})"
        prof.duplicates[key] += 1

        if tool == "read_file" and (path := args.get("path")):
            prof.files_read[str(path)] += 1
        if tool == "grep" and (pattern := args.get("pattern")):
            prof.greps.append((str(pattern), int(row.get("content_len") or 0)))
    return out


def render(prof: Profile) -> str:
    lines = [
        f"run {prof.run_id}",
        f"  tool calls          {prof.calls}"
        + (f"  ({prof.failed} failed)" if prof.failed else ""),
        f"  by node             {dict(prof.by_node)}",
        f"  by tool             {dict(prof.by_tool)}",
        "",
        f"  identical repeats   {prof.duplicate_calls}"
        f"  ({prof.wasted_fraction:.0%} of all calls — a cache removes these)",
        f"  unique files read   {prof.unique_files}",
        f"  repeated reads      {prof.repeated_reads}",
    ]
    worst = [(k, n) for k, n in prof.duplicates.most_common(4) if n > 1]
    if worst:
        lines.append("")
        lines.append("  most-repeated calls:")
        lines += [f"    {n}x  {k[:96]}" for k, n in worst]

    if prof.greps:
        noisy, empty = prof.noisy_greps, prof.empty_greps
        lines += [
            "",
            f"  greps               {len(prof.greps)}"
            f"  ({len(noisy)} noisy >{NOISE_CHARS}c, {len(empty)} near-empty <{LOW_YIELD_CHARS}c)",
        ]
        if noisy:
            lines.append("  widest patterns (term selection, not retrieval):")
            for pattern, size in sorted(noisy, key=lambda kv: -kv[1])[:6]:
                lines.append(f"    {size:>7,}c  {pattern!r}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Profile the agent's search behaviour.")
    parser.add_argument("--run", help="Single run id. Default: every run, busiest first.")
    parser.add_argument("--top", type=int, default=3, help="How many runs to show.")
    args = parser.parse_args()

    profiles = profile(_fetch(args.run))
    if not profiles:
        print("no tool calls recorded. Run something first, or check DATABASE_URL.")
        return 1

    ranked = sorted(profiles.values(), key=lambda p: -p.calls)[: args.top]
    for prof in ranked:
        print(render(prof))
        print()

    total_calls = sum(p.calls for p in profiles.values())
    total_dupes = sum(p.duplicate_calls for p in profiles.values())
    print(f"across {len(profiles)} run(s): {total_calls} calls, {total_dupes} identical repeats")
    if total_calls:
        print(
            f"a cache alone would remove {total_dupes / total_calls:.0%} of all tool calls, "
            "changing no algorithm"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
