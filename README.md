# Feature Pilot

[![CI](https://github.com/Ekam-Bitt/feature-pilot/actions/workflows/ci.yml/badge.svg)](https://github.com/Ekam-Bitt/feature-pilot/actions/workflows/ci.yml)
![tests](https://img.shields.io/badge/tests-358%20offline%20%2B%2028%20gated-brightgreen)
![python](https://img.shields.io/badge/python-3.13-blue)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**[Findings](#the-findings-in-one-line-each)** ·
**[Example run](docs/example-run.md)** ·
**[Retrospective](docs/engineering-retrospective.md)** ·
**[Architecture](#architecture)** ·
**[Evaluation](#evaluation-methodology)** ·
**[Quickstart](#quickstart)**

An autonomous software-engineering agent. Point it at a repository and an issue;
it explores the codebase, writes a plan for you to approve, edits code in an
isolated container, runs the test suite, repairs its own failures, and hands back
a diff with a PR summary.

```
Issue → Retrieve → Plan → [your approval] → Code → Test → ⟲ Debug → Review → PR summary
```

Built on LangGraph, with tools discovered dynamically over MCP and all execution
confined to a throwaway Docker container.

**Validated on a real repository.** It completed an end-to-end repair of a genuine
bug in [`pallets/click`](https://github.com/pallets/click) — 924k characters of
source, 7.7× more than fits in the context window it would need to read everything
— for about $1.22, with the repair loop firing live. That is one of six
ground-truth cases run end to end; the other five are built and unrun.

But the more useful part of this project is *why* it works, and the several
confident assumptions that turned out to be wrong on the way. The
[one-line summary](#the-findings-in-one-line-each) is below and the
[full detail](#engineering-findings) further down; that's the part worth reading.

Two documents go deeper than this one:

- **[docs/example-run.md](docs/example-run.md)** — one real bug, annotated end to
  end: what retrieval surfaced, the first patch, the failure that killed it, the
  second patch, and the passing suite. Every excerpt is a copied artifact.
- **[docs/engineering-retrospective.md](docs/engineering-retrospective.md)** — the
  full narrative: what was tried, what was disproved, what it cost, and what I
  would build differently.

---

## Research question

**Which components of an autonomous software-engineering agent materially improve
correctness, and which merely add complexity?**

Every experiment here answers part of that. Four of them answered in the opposite
direction to what I expected, which is why the architecture looks the way it does
rather than the way it was planned.

## Results at a glance

| | Outcome |
|---|---|
| Real repository | End-to-end repair of a genuine `pallets/click` bug, repair loop fired live — 1 of 6 ground-truth cases run |
| Retrieval benchmark | P@3 **0.33 → 0.83** on click, **0.25 → 0.67** across both repositories, offline and free |
| Second repository | Generalises in direction, not magnitude — rich 0.17 → 0.50, flat layout, no `src/` to exploit |
| Correctness driver | **Ranking, not recall.** The right file was already in the candidate set in 6/6 cases |
| Cost finding | Better retrieval bought correctness and **not** cost — $1.219 to pass vs $1.226 to fail |
| Toy fixture | 5/5 solved — and a one-shot prompt with no tools, tests or repair loop tied it at 40% of the cost |
| Tests | 358 offline (no Docker, no datastore, no API key) + 28 gated behind real infrastructure |
| Total spend | **$8.43**, of which $3.77 bought four failed runs and one lesson about instruments |

## The findings in one line each

❌ marks a belief the measurement destroyed, ✅ one that survived, ⚠ a defect in
how the work was being measured. Each links to the experiment that produced it.

| | Finding |
|---|---|
| ❌ | [Whole-file retrieval was not the dominant cost](#1-whole-file-retrieval-was-not-the-dominant-cost) — windowing moved total tokens 2.6%, because a cap upstream was already binding |
| ❌ | [Reducing context increased total cost](#2-reducing-context-increased-total-cost) — 406k → 680k tokens and 19 → 39 calls; with less state the agent re-explores |
| ❌ | [Cumulative token budgets are the wrong instrument for a tool loop](#3-cumulative-token-budgets-are-the-wrong-instrument-for-a-tool-loop) — a re-sent transcript bills a cached prefix once per turn |
| ❌ | [Candidate recall was already perfect](#4-candidate-recall-was-already-perfect) — 6/6 before ranking, so improving search was worth nothing |
| ✅ | [The ranking objective was wrong, not the search](#5-the-ranking-objective-was-wrong-not-the-search) — content features beat mention-counting with no embeddings, vector store or reranker |
| ❌ | [Query cleaning improved context size, not accuracy](#6-query-cleaning-improved-context-size-not-accuracy) — 34% less context, P@3 changed by exactly zero |
| ⚠ | [A weak objective was gating the strong one](#7-a-weak-objective-was-gating-the-strong-one) — pipeline ordering, neither a retrieval nor a ranking bug |
| ⚠ | [Offline and production disagreed about what a regex means](#8-offline-and-production-disagreed-about-what-a-regex-means) — the benchmark and the system it measured had divergent semantics |
| ❌ | [Better retrieval buys correctness, not efficiency](#9-better-retrieval-buys-correctness-not-efficiency) — the headline: outcome flipped, spend did not move |
| ❌ | [Prompt length is inversely related to signal density](#10-prompt-length-is-inversely-related-to-signal-density) — a 42-character commit subject beat a 1,321-character bug report |
| ⚠ | [On the toy fixture, the whole agent tied a one-shot prompt](#11-on-the-toy-fixture-the-whole-agent-tied-a-one-shot-prompt) — a property of the fixture, and the reason the project moved to a real repository |

---

## What was actually measured

Rather than treat the agent as one black box, it's decomposed into subsystems that
are benchmarked independently.

**Retrieval, offline, no model calls** — two repositories, ground truth from real
bugfix commits:

| strategy | P@1 | P@3 | MRR | impl rank | ctx bytes | retrieval calls |
|---|---:|---:|---:|---:|---:|---:|
| `filesystem` (control) | 0.17 | 0.25 | 0.232 | 3.2 | 183,796 | 16 |
| `filesystem+clean-query` | 0.17 | 0.25 | 0.228 | 4.2 | 127,445 | 16 |
| `clean-query+content-rank` | **0.42** | **0.67** | **0.542** | **2.4** | **120,988** | 43 |

Per repository, because an average would hide a layout-specific win:

| | click (`src/` layout) | rich (flat, no `src/`) |
|---|---:|---:|
| control P@3 | 0.33 | 0.17 |
| best P@3 | **0.83** | **0.50** |

**End to end, one paid run per configuration**, same case, only the retriever
changed:

| retriever | solved | tokens | cost | model calls |
|---|:--:|---:|---:|---:|
| `clean-query+content-rank` | **2/2 PASS** | 552,989 | $1.2190 | 31 |
| `filesystem` (control) | 0/2 FAIL | 583,029 | $1.2255 | 29 |

---

## Engineering Findings

Every one of these came from an experiment that contradicted what I expected.

### 1. Whole-file retrieval was not the dominant cost

The first real-repository run died at 417k tokens against a 400k ceiling, and
`core.py` alone is ~35k tokens — so returning whole files looked like the obvious
culprit. Windowing retrieval to ±40 lines around each match changed total tokens
by 2.6%.

The cap upstream was already binding. `render_context` truncated to 24k characters
regardless, so windowing improved *which* 24k the model saw without changing *how
much*. Worth keeping for relevance; worthless for cost.

### 2. Reducing context increased total cost

Cutting per-call context from 24k/60k to 9k/18k characters took tokens from 406k
to **680k** and calls from 19 to **39**. With less state in hand the agent
re-explores.

Per-call context and iteration count trade against each other, so trimming
context is not a cost lever. Stated carefully: this is an interaction effect under
the current planner/coder coupling, not a clean causal law — that run changed two
variables.

### 3. Cumulative token budgets are the wrong instrument for a tool loop

The loop re-sends its transcript every turn, so one retrieval context counted once
per iteration — 19 calls × the same 24k block. With prompt caching that prefix
costs a tenth of list price, which is why 406k tokens billed $0.87 instead of
$1.22. Cost is the honest ceiling; the token count is now only a runaway backstop.

### 4. Candidate recall was already perfect

In all 6 click cases, the file the real fix touched was **already in the candidate
set** before ranking. Retrieval wasn't missing it; ranking was burying it.

That single measurement is what made the rest of the work targeted instead of
speculative — there was no point improving search at all.

### 5. The ranking objective was wrong, not the search

Ranking by "how many queried symbols does this file mention" hands the win to
tests and changelogs *by construction*: a unit test writes `click.confirm(...)`
five times where the implementation writes `def confirm(...)` once, and a
changelog mentions every symbol that ever existed.

Replacing the objective with content features — **defines ≫ imports ≫ calls**,
minus penalties for markup, changelog shape and assertion density — took click's
P@3 from 0.33 to 0.83. No embeddings, no vector store, no reranker, and
deliberately **no path prior**: `src/**` beating `tests/**` would score perfectly
on a benchmark whose every answer lives in `src/`, which measures the benchmark
rather than the ranker.

### 6. Query cleaning improved context size, not accuracy

Bug reports contain reproduction scripts and pasted terminal sessions, and the
heuristic "backticked spans are the important ones" is precisely backwards for
those. The extractor was searching for `False`, `Hello`, `World`, `CliRunner`, and
in one case `Python`, `help`, `copyright`, `credits`, `license` — the interpreter's
start-up banner.

Fixing it with region classification (prose / code / console / REPL banner /
traceback, each weighted) changed P@3 by **exactly zero**. It cut context 34%.
The queries were bad *and* were not the accuracy bottleneck.

### 7. A weak objective was gating the strong one

The content ranker was only shown candidates that survived a top-18 pre-filter
ranked by the *old* mention-count objective. The correct file sat at rank 44 and
30 in two cases and never reached the ranker at all.

This wasn't a retrieval problem or a ranking problem — it was pipeline ordering.
`Retriever → top-18 → good ranker` instead of `Retriever → good ranker → top-18`.

### 8. Offline and production disagreed about what a regex means

The definition pattern used `[[:space:]]`, a POSIX class Python's `re` rejects, so
every definition search silently failed in the offline benchmark. Production
called `grep` without `-E`, where `(def|class)` is a literal string — so it would
have failed there too, differently, for a different reason.

**The benchmark and the system it measures had divergent semantics.** Both now use
ERE, with the pattern verified against both engines by a test.

### 9. Better retrieval buys correctness, not efficiency

The headline result. Holding everything else constant and changing only the
retriever: **2/2 PASS versus 0/2 FAIL, at 31 versus 29 model calls and $1.219
versus $1.226.**

With poor retrieval the agent confidently patched the wrong place, failed its
tests, and the debugger correctly declined to retry. It was misdirected, not lazy —
and being misdirected costs the same as being right.

Stated at the width of the evidence: **under this architecture, on this case,
changing only the retriever changed the outcome while leaving spend flat.** One
controlled pair is not a solve rate. What it is — and what the four earlier runs
were not — is a single-variable comparison, which is the only kind that can
attribute anything.

### 10. Prompt length is inversely related to signal density

A 42-character commit subject retrieved its target at rank 1. A 1,321-character
real bug report retrieved nothing. Terse text is almost all signal; a long report
is mostly repro script, console paste and issue-template boilerplate.

### 11. On the toy fixture, the whole agent tied a one-shot prompt

Before any of the above, the full pipeline scored 5/5 against a one-shot Claude
call with no tools, no tests and no repair loop — which also scored 5/5, at 40% of
the cost.

That result is a property of the fixture, not the architecture: 20 files fit in
one prompt, so the control never needed retrieval, and every defect was fixable
first try so the repair loop never engaged. It's why the project moved to a real
repository, and `eval/baseline.py` prints that caveat itself so nobody quotes the
tie as a win.

---

## Architecture

Four seams, each built up front because retrofitting it would mean rewriting every
node:

| Seam | Where | What it buys |
|---|---|---|
| Typed contracts | `contracts.py` | Routing reads typed fields, not prose. Nodes testable without a model. |
| Run lifecycle | `lifecycle.py` | `RunPhase` state machine with a legal-transition table; illegal transitions raise. Resume reads one enum. |
| `ToolRegistry` | `tools/registry.py` | Nodes never import LangChain or MCP types. Fakes in tests, no MCP process needed. |
| `Retriever` protocol | `retrieval/base.py` | The graph is retrieval-agnostic; swapping strategies is a config value. |

**The supervisor is a pure function, not an LLM call.** The next node follows from
`(phase, last typed output)` — deterministic, unit-testable, and free. An LLM
router earns its place when a decision is ambiguous; a red suite going to the
debugger never is.

Retrieval is decomposed further, because that's where the measurement pointed:

```
Issue → Query Generator → Retriever → Ranker → Context Builder
        (query.py)                    (ranker.py)
```

Modules are benchmarked independently. Module 1 was fixed and falsified as the
bottleneck; module 2's recall measured 100%; module 3 was the whole problem.

**Proof the seams are real:** `tests/test_graph.py` runs a complete agent loop —
retrieve, plan, approve, code, test, debug, re-code, test, review, summarise — with
**no network and no container**. If any abstraction were decorative, that file
could not exist.

## Safety

- **Agent commands never reach a shell.** Parsed with `shlex`, passed as argv
  straight to `execve`, so chaining is structurally impossible rather than
  blocklist-dependent. A Docker test proves it: `echo $(id -u)` returns the
  literal string.
- **Executable allowlist**, with a regression test asserting `bash`, `sh`, `curl`,
  `wget`, `nc`, `ssh`, `sudo` are absent.
- **Path validation**, 28 pure tests — rejects `..` escapes, absolute paths outside
  the worktree, null bytes, and `/workshop` (which a naive `/work` prefix check
  accepts).
- **Network cut** after dependency install, verified unreachable.
- Non-root uid 10001, `cap_drop=ALL`, `no-new-privileges`, 2 GB / 2 CPU / pids caps.
- **A reaper** removes orphaned containers at startup, label-scoped.

## Evaluation methodology

Scoring is **baseline-aware** (FAIL_TO_PASS / PASS_TO_PASS). A baseline suite runs
before any edit; success means *fixed something, broke nothing*. Requiring a fully
green suite was wrong — with five independent seeded defects, no single correct
patch could ever achieve it.

Three integrity rules, each closing a way to pass dishonestly:

1. **Test edits are reverted before scoring** — for the agent *and* the baseline.
2. **A shrinking suite is disqualifying.** Deleting a passing test creates no
   regression, so a pass/fail diff cannot see it; the collected count can.
3. **Unparseable output is never success.** "We couldn't tell" ≠ "it passed."

Real-repository cases follow SWE-bench: revert a bugfix commit's **source only**,
keep its tests, so FAIL_TO_PASS is known rather than guessed. Every end-to-end case
is verified in a container — red with the bug, green with the real fix — before it
enters the set.

Two confounds are measured rather than assumed away: issues that name the file the
fix must touch (retrieval handed over, not tested), and text that may describe the
fix. Both are reported per case so results can be segmented instead of averaged.

## Requirements

- Docker (datastores + the per-run sandbox)
- Python 3.13, via [`uv`](https://docs.astral.sh/uv/)
- An Anthropic API key

## Quickstart

```bash
cp .env.example .env      # then set ANTHROPIC_API_KEY
docker compose up -d      # postgres + redis
uv sync
uv run fpilot doctor      # check everything is reachable
uv run fpilot solve --issue fixtures/issues/01-off-by-one.md
```

`ANTHROPIC_API_KEY` is the only value you must set. Everything else has a working
local default — embeddings run offline, tracing is a no-op without a LangSmith key,
and pointing the `FP_MODEL_*` settings at `ollama/...` removes the hosted
dependency entirely.

**Variable naming.** Third-party services keep their conventional names
(`ANTHROPIC_API_KEY`, `LANGSMITH_API_KEY`, `DATABASE_URL`, `REDIS_URL`), so a key
you already have exported works untouched and the `langsmith` CLI reads the same
variable the app traces with. Only settings that are genuinely ours take the `FP_`
prefix — `MAX_ATTEMPTS` unprefixed would collide with anything.

## Running the evaluations

```bash
# Retrieval, offline — seconds, no model calls, no container
uv run python -m eval.retrieval_bench --per-case

# Build ground-truth cases from a real repository
git clone https://github.com/pallets/click /tmp/oss-eval/click
uv run python -m eval.oss_build --limit 200 --want 6

# Stage-by-stage ablation: code-only → +retrieve → +plan → +test → +debug → full
uv run python -m eval.ablation --suite click --rungs full

# Fixture gate, and the one-shot control it is compared against
uv run python -m eval.gate
uv run python -m eval.baseline

# What did the agent spend its tool calls on?
uv run python -m eval.search_profile
```

## Observability

Tracing is automatic when `LANGSMITH_API_KEY` is set — LangGraph instruments its
own nodes, and `tracing.py` adds spans for the parts LangChain cannot see (MCP tool
execution, retrieval, the containerised test run). Metrics are attributed per role
and per node, which is what made "the coder is 96% of spend" visible and therefore
actionable.

For querying traces from the terminal, this repo expects LangChain's skills and
CLI, which are not vendored here:

```bash
git clone https://github.com/langchain-ai/langsmith-skills /tmp/ls-skills
mkdir -p .claude/skills && cp -r /tmp/ls-skills/config/skills/* .claude/skills/
curl -fsSL https://cli.langsmith.com/install.sh | sh
```

## Tests

```bash
uv run pytest              # 358 tests: seams + units. No Docker, no datastore, no API calls
uv run pytest -m docker    # 26 tests: real container, real MCP servers
uv run pytest -m postgres  # 2 tests: checkpoint round-trip against the compose Postgres
uv run pytest -m llm       # real model calls (costs tokens)
```

The default suite deliberately runs without Docker or a live MCP server. If a node
can't be exercised against a fake `ToolRegistry` and a stub `Retriever`, the
abstraction is decorative — that constraint *is* the test.

That constraint is also what makes CI cheap: every push runs `ruff check`,
`ruff format --check`, `mypy src eval` and the offline suite, with no daemon, no
datastore and no API key. CI reports **350 passed, 8 skipped** — the 8 need a local
fixture virtualenv that isn't committed. `mypy` covers the evaluation harness as
well as the agent, because three of this project's bugs corrupted *scoring*
silently, and a harness that miscounts is worse than no harness.

The first CI run failed, which was the point of adding it: 24 API tests were
reading `ANTHROPIC_API_KEY` out of a developer's `.env` without declaring it, so
they passed on my machine and could not construct their subject on a clean runner.
An autouse fixture now forces a dummy credential, which also guarantees no test can
reach a real provider by accident.

## Status

**v1.0.** Complete, with limitations documented below rather than hidden. Remaining
work is filed as issues and deliberately unbuilt — each strengthens an existing
claim rather than unlocking a new one.

## Known limitations

- **The ranker's weights are hand-picked and tuned on click.** The gain generalises
  in direction (rich improves ~3× on P@3 with no `src/` directory to exploit) but
  not in magnitude (0.83 vs 0.50). They are unlearned, and the offline benchmark is
  how any change to them gets tested.
- **Better retrieval does not reduce cost** (Finding 9). A run is ~$1.22 on click
  whether it succeeds or fails.
- **A reporter-named file path is extracted and then ignored.** `Query.paths`
  exists and nothing consumes it; that is the last remaining retrieval miss.
- **The ablation ladder is built but not fully run.** At ~$1.22 per run, all six
  rungs across six cases is ~$44, which has not been spent.
- **Phase 1B (embeddings, BM25, hybrid, reranking) is deliberately not built.**
  Content-based ranking got P@3 to 0.83 without it, and the offline benchmark exists
  so the next strategy has to prove itself rather than be assumed better.
