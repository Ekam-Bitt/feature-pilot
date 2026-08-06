# Engineering Retrospective

How Feature Pilot was built, what it measured, and which of my assumptions the
measurements destroyed.

Written for someone deciding whether the engineering was sound — not a tutorial,
and not a feature list. The interesting content is the [failed
hypotheses](#failed-hypotheses), because that is where the architecture actually
came from.

For the system doing the work rather than the reasoning behind it, see
[example-run.md](example-run.md): one real bug, annotated end to end, every excerpt
copied from a run artifact.

---

## Original hypothesis

The plan was a multi-agent software-engineering assistant: LangGraph supervisor
over planner/retriever/coder/reviewer/debugger nodes, MCP tool servers, hybrid RAG
(BM25 + dense embeddings + reranking), Postgres/Redis, evaluation via LangSmith.

Two beliefs were baked into that plan, and neither was ever stated as a hypothesis
because both felt like background facts:

1. **Sophisticated retrieval would be necessary and would help.** Hybrid RAG was in
   the plan from day one, on the reasoning that better retrieval means better
   context means better patches.
2. **Orchestration would beat a single model call.** Planning, testing and repair
   were assumed to be worth their cost.

Both turned out to be wrong in interesting ways. Neither would have been caught by
building the plan as written.

The one deliberate call I would repeat: the plan was reviewed and cut before any
code, from a single 6-subsystem milestone down to a vertical slice (1A) with real
seams, deferring hybrid RAG to 1B. That decision is the reason the later pivots
were cheap.

## What was built

The shape the rest of this document keeps referring to:

```
Issue
  │
  ▼
┌── retrieval, decomposed ────────────────────────────────────────┐
│   Query Generator ──▶ Retriever ──▶ Ranker ──▶ Context Builder  │
│   query.py            filesystem.py  ranker.py  render_context  │
└─────────────────────────────────────────────────────────────────┘
  │
  ▼
Plan ──▶ [human approval] ──▶ Code ──▶ Test ──▶ Review ──▶ PR summary
                                 ▲        │
                                 └─ Debug ◀── red suite, up to 3 attempts
```

The boxed half is one module in the original plan and four here; that split is the
main architectural consequence of everything below. Findings 1 and 3–6 all live
inside the box. The row beneath it — the part that looks like the whole agent —
turned out to be where the money goes and where the least was learned.

---

## Timeline

| Stage | Outcome |
|---|---|
| Phase 1A build | Sandbox, MCP servers, 8-node graph, deterministic router, checkpointing, CLI, API, tracing |
| Fixture gate | **5/5 seeded issues solved**, no regressions, live checkpoint resume verified |
| One-shot baseline | **Tie at 40% of the cost.** The whole agent bought nothing measurable |
| Move to a real repo | `pallets/click`, 924k chars, 7.7× the one-prompt budget |
| First click runs | **Four consecutive failures**, four distinct causes, $3.77 spent, nothing learned about solve rate |
| Retrieval decomposed | Offline benchmark built. Every later result cost $0 |
| Module isolation | Query falsified; recall measured 100%; ranking was the whole bottleneck |
| Content ranker | click P@3 **0.33 → 0.83**, rich **0.17 → 0.50**, no path prior |
| First click solve | **2/2 tests, $1.22**, repair loop fired live for the first time |
| Attribution run | Changing only the retriever **flipped the outcome and left spend flat** |

---

## Experiments

### The fixture gate — necessary, insufficient

5/5 issues solved cleanly, no regressions, resume verified. It proved the pipeline
worked end to end. It proved nothing about whether the pipeline was *worth it*,
which the next experiment showed immediately.

### The one-shot baseline

| | solved | cost |
|---|:--:|---:|
| one-shot Claude, no tools/tests/repair | 5/5 | $0.2259 |
| full agent | 5/5 | $0.5798 |

The fixture could not tell the two apart, for two reasons I had built in without
noticing: 20 files fit in one prompt, so the control never needed retrieval; and
every defect was fixable first try, so the repair loop never engaged. A control
that never needs a second attempt cannot measure the value of being able to make
one.

`eval/baseline.py` now prints that caveat itself, so a reader cannot quote the tie
as a win.

### Four failures on click

| run | change | calls | tokens | cost | outcome |
|---|---|---:|---:|---:|---|
| 1 | baseline | 12 | — | $0.86 | token ceiling |
| 2 | windowed retrieval, ranged reads, transcript pruning | 19 | 406k | $0.87 | token ceiling |
| 3 | per-call context cut to 9k/18k | **39** | **680k** | **$2.04** | cost ceiling |
| 4 | timeout headroom | 0 | 0 | $0.00 | 817s API timeout |

Four attempts, four different failure modes, and no answer to the actual question.
That is the signature of proposing mechanisms rather than isolating them — and it
is what forced the decomposition that followed.

### The offline retrieval benchmark

The pivot that mattered. Retrieval is deterministic and ground truth comes from
real bugfix commits, so "does the retriever rank the file the fix touched" needs no
model, no container, and no money. It runs in seconds.

| strategy | P@1 | P@3 | MRR | impl rank | ctx bytes | calls |
|---|---:|---:|---:|---:|---:|---:|
| `filesystem` (control) | 0.17 | 0.25 | 0.232 | 3.2 | 183,796 | 16 |
| `filesystem+clean-query` | 0.17 | 0.25 | 0.228 | 4.2 | 127,445 | 16 |
| `clean-query+content-rank` | **0.42** | **0.67** | **0.542** | **2.4** | **120,988** | 43 |

Per repository, because an average hides a layout-specific win:

| | click (`src/`) | rich (flat, no `src/`) |
|---|---:|---:|
| control P@3 | 0.33 | 0.17 |
| best P@3 | **0.83** | **0.50** |

### The attribution run

One paid run per configuration, same case, only the retriever changed:

| retriever | solved | tokens | cost | calls |
|---|:--:|---:|---:|---:|
| `clean-query+content-rank` | **2/2 PASS** | 552,989 | $1.2190 | 31 |
| `filesystem` (control) | 0/2 FAIL | 583,029 | $1.2255 | 29 |

---

## How the architecture actually got decided

One figure for the whole process. Each row is an assumption I held confidently, the
cheapest experiment that could have falsified it, what came back, and what changed
in the code as a result.

| Assumption | Experiment | Result | Architectural change |
|---|---|---|---|
| Whole-file retrieval is the dominant cost | Window to ±40 lines, measure total tokens | **2.6%** — an upstream cap was already binding | Kept for relevance; stopped treating context size as a cost lever |
| Less context is cheaper | Cut per-call context 24k/60k → 9k/18k | **406k → 680k** tokens, 19 → 39 calls | Context floor restored; a **cost** ceiling replaced the token ceiling |
| Retrieval recall is poor | Ask whether the truth was in the candidate set at all — 6 cases, $0, minutes | **6/6 already present** | Search work abandoned outright; all effort moved to ranking |
| Query extraction is the accuracy bottleneck | Region-classify the extractor, re-run the benchmark | P@3 **±0.00**, context **−34%** | Kept as a context optimisation, explicitly not an accuracy one |
| Mention count identifies the implementation | Content-feature ranker vs control, two repositories | click P@3 **0.33 → 0.83** | Ranking became its own module with its own objective |
| Better retrieval is also cheaper | One paid run per retriever, single variable | Outcome flipped, **cost moved 0.5%** | Retrieval judged on correctness alone; cost work aimed at the coder (90% of spend) |

Read top to bottom, that table is the project. Five of six assumptions were wrong,
and the one that survived is the only one that changed a file.

---

## Failed hypotheses

The core of the project. Six confident beliefs, each disproved by the cheapest
experiment that could have disproved it.

### ❌ Whole-file retrieval is the dominant cost

`core.py` is ~35k tokens and retrieval returned whole files, so this looked
obvious. Windowing to ±40 lines around each match moved total tokens **2.6%**.

The cap upstream was already binding: `render_context` truncated to 24k characters
either way, so windowing changed *which* 24k the model saw, not *how much*. I built
the fix before checking whether the thing it optimised was the constraint.

### ❌ Reducing context reduces cost

Cutting per-call context from 24k/60k to 9k/18k took tokens from 406k to **680k**
and calls from 19 to **39**. With less state in hand the agent re-explores.

Stated carefully, because that run changed two variables: this is an interaction
effect under the current planner/coder coupling, not a causal law.

### ❌ Retrieval recall is poor

In **all 6** click cases the file the real fix touched was already in the candidate
set before ranking. Retrieval was not missing it; ranking was burying it.

The single most valuable measurement in the project. It made everything after it
targeted instead of speculative, and it took minutes.

### ❌ Query extraction is the accuracy bottleneck

The extractor was searching for `False`, `Hello`, `World`, `CliRunner`, and in one
case `Python`, `help`, `copyright`, `credits`, `license` — the interpreter's
start-up banner from a pasted console session. The cause was a heuristic that reads
well and is backwards: *backticked spans are the important ones*, when in a bug
report backticks mostly contain a reproduction script.

Fixing it properly, with region classification, changed P@3 by **exactly zero**. It
cut context 34%. The queries were genuinely bad *and* were not the bottleneck.

### ❌ Better retrieval reduces exploration and cost

The headline. **2/2 PASS vs 0/2 FAIL at 31 vs 29 calls and $1.219 vs $1.226.**

With poor retrieval the agent confidently patched the wrong place, failed its tests,
and the debugger correctly declined to retry. It wasn't lazy; it was misdirected —
and being misdirected costs the same as being right.

Stated at the width of the evidence, because it is the claim most worth not
overstating: **under this architecture, on this case, changing only the retriever
changed the outcome while leaving spend flat.** That is one controlled pair, not a
solve rate. Its value is that it is single-variable — the four runs before it were
not, which is exactly why they cost $3.77 and settled nothing.

I had also read an earlier 39→31 call drop as evidence retrieval was working. It
wasn't — that came from the budget and timeout fixes. The controlled run is what
separated them.

### ✅ The ranking objective is wrong

The one that survived. Scoring by "how many queried symbols does this file mention"
hands the win to tests and changelogs *by construction*: a unit test writes
`click.confirm(...)` five times where the implementation writes `def confirm(...)`
once, and a changelog mentions every symbol that ever existed.

Content features — **defines ≫ imports ≫ calls**, minus penalties for markup,
changelog shape and assertion density — took click's P@3 from 0.33 to 0.83 with no
embeddings, no vector store, and no reranker.

**Deliberately no path prior.** `src/**` beating `tests/**` would score perfectly
on a benchmark whose every answer lives in `src/` — that measures the benchmark,
not the ranker. A second repository with a flat layout exists specifically so that
shortcut cannot pass for a general result.

---

## Architectural pivots

### Retrieval became four modules, not one

```
Issue → Query Generator → Retriever → Ranker → Context Builder
```

Before this, any retrieval failure could have come from anywhere. After it, each
module is benchmarked alone — which is how module 1 got exonerated, module 2 got
measured at 100% recall, and module 3 got identified as the entire problem.

### A weak objective was gating the strong one

The content ranker only ever saw candidates surviving a top-18 pre-filter ranked by
the **old** mention-count objective. The correct file sat at rank 44 and 30 in two
cases and never reached the ranker.

`Retriever → top-18 → good ranker` instead of `Retriever → good ranker → top-18`.
Not a retrieval problem and not a ranking problem — pipeline ordering. Where the
truth did survive the cut, the ranker put it first, so the fix was letting it see
the candidates.

### One factory for production and the benchmark

The benchmark measured a query builder and a ranker that `open_run` never
constructed. Every improvement was simultaneously real and absent from the agent,
and a paid end-to-end run would have measured the old code and reported it as new.

Both callers now build strategies through `retrieval/strategies.py`. This was the
**second** instance of the same class of bug — the first was an error-prefix
convention duplicated between an MCP server and its client, which caused a rejected
path traversal to be recorded as a *successful* tool call. Two occurrences is a
pattern: shared conventions need a single home, not matching implementations.

### The budget instrument was measuring the wrong thing

Cumulative input tokens are a poor ceiling for a tool loop. The transcript is
re-sent every turn, so one retrieval context counted once per iteration — 19 calls
× the same 24k block. With prompt caching that prefix costs a tenth of list price,
which is why 406k tokens billed $0.87 rather than $1.22. Cost is the honest ceiling;
the token count is now only a runaway backstop.

---

## Cost analysis

**$8.43 total.**

| | spend | what it bought |
|---|---:|---|
| Fixture gate | $1.95 | 5/5 pass; proof the pipeline works |
| One-shot baseline | $0.27 | The finding that the fixture cannot discriminate |
| click diagnostics (4 failed runs) | $3.77 | Four failure modes; no answer on solve rate |
| click solve + attribution | $2.44 | First real solve, and the headline result |

The uncomfortable line is $3.77 for four failures that taught me about
*infrastructure* rather than about the agent. Every retrieval result after the
offline benchmark existed cost **$0** — P@3 0.33→0.83, the recall measurement, the
query falsification, the second repository, all free and repeatable in seconds.

Per-role attribution is what made the spend legible at all:

```
coder      $1.0961   (90%)
debugger   $0.0614
planner    $0.0355
reviewer   $0.0182
summarizer $0.0077
```

Without that breakdown, "the run was expensive" is unactionable. With it, the
target is obvious and the planner is provably not worth touching.

---

## What surprised me

**Prompt length is inversely related to signal density.** A 42-character commit
subject retrieved its target at rank 1; a 1,321-character real bug report retrieved
nothing. Terse text is nearly all signal, while a long report is mostly repro
script, console paste and issue-template boilerplate. I had assumed more context
was strictly better input.

**Green test suites hid the most serious bug in the project.** `put_archive`
preserves the archive's uid — 501 on macOS — while the container runs as 10001, so
every copied file was read-only and **the agent could not edit anything**. The
system was completely inert and 100+ tests passed, because my test never asserted
on its own setup command.

**Three bugs corrupted *scoring* silently rather than failing loudly.** Truncated
pytest node IDs collapsed 24 failures into 8 (they contain spaces, and I captured
`\S+`); toolchain drift aborted collection so 1 test ran instead of 1,705; and a
crashed suite read as "nothing failed" and dropped 9 good cases. Each looked like a
clean result.

**The offline benchmark and production disagreed about what a regex means.** The
definition pattern used `[[:space:]]`, which Python's `re` rejects, while production
called `grep` without `-E`, where `(def|class)` is a literal string. It failed in
both, differently, for different reasons.

**Every step toward realism found a correctness bug the previous layer's tests
could not.** Real container → real MCP → real model → real repository. That
progression, not any individual test suite, is what actually found the defects.

---

## What I would build differently

1. **Build the offline benchmark before the agent.** I spent $3.77 and four failed
   runs learning things a free, deterministic benchmark answered in seconds. The
   instrument should precede the thing it measures.

2. **Never build a fixture that fits in one prompt.** A 20-file fixture cannot
   distinguish an agent from a single model call, so the first honest evaluation was
   also the first useless one. Size the fixture so the control is genuinely
   handicapped.

3. **Assert on setup commands.** The uid bug hid behind a green suite purely
   because a test performed a write and never checked it succeeded.

4. **Give every shared convention one home, immediately.** Two separate drift bugs
   — an error prefix, and a retriever factory — both from "matching" implementations
   in two files.

5. **Make the budget ceiling cost-based from the start.** A token ceiling in a loop
   that re-sends its transcript charges a cached prefix once per iteration.

6. **Change one variable per experiment.** The run that "proved" smaller context
   increases exploration changed two things, so the conclusion is an interaction
   effect I cannot cleanly attribute.

7. **Check whether the constraint is binding before optimising it.** Windowed
   retrieval was correct, useful for relevance, and bought no tokens, because a cap
   upstream was already the limit.

---

## Transferable lessons

The seven above are things I would do differently *here*. These are the same
lessons with the project removed — the part I expect to carry into work that has
nothing to do with agents or retrieval.

**Build the instrument before the thing it measures.** An unmeasured system will
absorb any amount of plausible improvement without telling you whether it moved.
Four failed runs and $3.77 bought less than a deterministic benchmark that ran in
seconds and cost nothing, and the benchmark was buildable first.

**Falsify the cheapest hypothesis first, not the most interesting one.** "Is the
answer even in the candidate set?" took minutes and immediately closed off every
line of work aimed at search. The expensive hypothesis is rarely the one that
constrains the others.

**Confirm a constraint binds before optimising it.** A correct optimisation of a
non-binding variable produces a real improvement and zero effect, which is worse
than a failure because it looks like progress.

**One variable per experiment, or accept you have measured an interaction.** The
run that "proved" smaller context increases exploration changed two things, and the
conclusion is permanently weaker for it. Attribution is the whole value of a
controlled comparison; two variables destroy it for the same cost.

**A shared convention needs one home, not two implementations that agree.** Both
drift bugs here — an error prefix across an MCP server and its client, a retriever
factory across production and the benchmark — were "matching" code in two files.
Matching is a state, not a property; it decays silently and the failure surfaces
somewhere unrelated.

**Assert on your setup, not only your assertion.** The most serious bug in this
project sat behind 100+ passing tests because a test performed a write and never
checked that the write succeeded. Green means "nothing I checked was wrong."

**Prefer the measurement that can be wrong loudly.** Three defects here corrupted
scoring instead of crashing: truncated test IDs, aborted collection, a crashed
suite read as "nothing failed." Each produced a clean-looking number. Instruments
need failure modes that are visible, and a result you cannot parse must never be
recorded as a pass.

---

## Where it stopped, and why

Version 1.0 is declared complete with known limitations documented rather than
hidden. What remains is filed as issues, deliberately unbuilt:

- **`Query.paths` is extracted and ignored** — a reporter-named file path is
  parsed and never used. It is the last remaining retrieval miss.
- **The full ablation ladder is built but not fully run** — ~$44 across six rungs
  and six cases, unjustified while the single-rung result already answered the
  question.
- **Hybrid retrieval (embeddings, BM25, RRF, reranking) is not built** — content
  ranking reached P@3 0.83 without it, and the benchmark exists so the next strategy
  has to prove itself rather than be assumed better.
- **The ranker's weights are hand-picked and tuned on click.** The gain generalises
  in direction but not magnitude (0.83 vs 0.50 on rich).

Each of those strengthens an existing claim rather than unlocking a new one. The
project's differentiator is not the feature count; it is that the system was
measured rather than assembled, and that the measurements were allowed to overturn
the plan.
