# Feature Pilot

A multi-agent software engineering assistant. Point it at a repository and an
issue; it explores the codebase, writes a plan for you to approve, edits code in
an isolated container, runs the test suite, repairs its own failures, and hands
back a diff with a PR summary.

Built on LangGraph, with tools discovered dynamically over MCP and all code
execution confined to a throwaway Docker container.

```
Issue → Planner → [your approval] → Coder → Tester → Debugger ⟲ Coder → Reviewer → PR summary
```

## Status

**Phase 1A — proving the loop.** The autonomous repair loop, sandbox, MCP tool
layer, checkpointing, and human approval gates. Retrieval is direct filesystem
search (`grep` / `glob` / `read_file`) — the same way Claude Code navigates code.

Phase 1B swaps in hybrid RAG (AST chunking, BM25 + dense embeddings, reciprocal
rank fusion, reranking, symbol graph) behind the existing `Retriever` protocol,
so no graph node changes. See `docs/` for the staged plan.

## Requirements

- Docker (datastores + the per-run sandbox)
- Python 3.13, via [`uv`](https://docs.astral.sh/uv/)
- An Anthropic API key

## Quickstart

```bash
cp .env.example .env      # then set ANTHROPIC_API_KEY
docker compose up -d      # postgres + redis
uv sync
uv run fpilot solve --issue fixtures/target-repo/issues/01-off-by-one.md
```

`ANTHROPIC_API_KEY` is the only value you must set. Everything else has a
working local default — embeddings run offline via `fastembed`, tracing is a
no-op without a LangSmith key, and pointing the `FP_MODEL_*` settings at
`ollama/...` removes the hosted dependency entirely.

**Variable naming.** Third-party services keep their conventional names
(`ANTHROPIC_API_KEY`, `LANGSMITH_API_KEY`, `DATABASE_URL`, `REDIS_URL`), so a key
you already have exported works untouched and the `langsmith` CLI reads the same
variable the app traces with. Only settings that are genuinely ours take the
`FP_` prefix — `MAX_ATTEMPTS` unprefixed would collide with anything.

## Design

Four seams carry the architecture, each chosen because retrofitting it later
would mean rewriting every node:

| Seam | What it buys |
|---|---|
| `contracts.py` — typed Pydantic node I/O | Routing reads fields, not prose. Nodes are independently testable. |
| `lifecycle.py` — explicit `RunPhase` state machine | Deterministic resume, retries, and metrics from one enum. |
| `ToolRegistry` | Nodes never import LangChain or MCP types. Fake tools in tests, no MCP process needed. |
| `Retriever` protocol | The graph is retrieval-agnostic; 1A→1B is a config value. |

The supervisor is a **pure function**, not an LLM call: the next node is
determined by `(phase, last typed output)`. That keeps routing deterministic,
unit-testable, and free.

## Cost

Infrastructure is free — everything runs locally in Docker. The only spend is
model tokens, and roles are tiered (Haiku for classification, Sonnet for
plan/code/review, Opus only on a final retry) with per-run token and dollar
ceilings enforced in `config.py`.

## Observability

Tracing is automatic when `LANGSMITH_API_KEY` is set — LangGraph instruments its
own nodes, and `featurepilot/tracing.py` adds spans for the parts LangChain cannot
see (MCP tool execution, retrieval, the containerised test run).

For querying traces from the terminal, this repo expects LangChain's skills and
CLI, which are not vendored here:

```bash
git clone https://github.com/langchain-ai/langsmith-skills /tmp/ls-skills
mkdir -p .claude/skills && cp -r /tmp/ls-skills/config/skills/* .claude/skills/
curl -fsSL https://cli.langsmith.com/install.sh | sh     # the `langsmith` CLI
```

## Tests

```bash
uv run pytest                    # seam + unit tests; no Docker, no API calls
uv run pytest -m docker          # sandbox isolation tests
uv run pytest -m llm             # real model calls (costs tokens)
```

The default suite deliberately runs without Docker or a live MCP server. If a
node can't be exercised against a fake `ToolRegistry` and a stub `Retriever`,
the abstraction is decorative — that constraint is the test.
