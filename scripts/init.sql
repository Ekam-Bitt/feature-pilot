-- Bootstrap schema. LangGraph's AsyncPostgresSaver creates its own
-- checkpoint tables on first .setup(); everything here is Feature Pilot's own.

-- Unused in 1A, created now so the 1B vector store needs no image change.
CREATE EXTENSION IF NOT EXISTS vector;

-- One row per node execution, appended from the metrics event stream.
-- Deliberately denormalised: this table is read by dashboards and ad-hoc
-- SQL, so a self-contained row beats a join.
CREATE TABLE IF NOT EXISTS node_metrics (
    id              BIGSERIAL PRIMARY KEY,
    run_id          TEXT        NOT NULL,
    node            TEXT        NOT NULL,
    phase           TEXT        NOT NULL,
    attempt         INT         NOT NULL DEFAULT 0,
    model           TEXT,
    input_tokens    INT         NOT NULL DEFAULT 0,
    output_tokens   INT         NOT NULL DEFAULT 0,
    cache_read      INT         NOT NULL DEFAULT 0,
    cost_usd        NUMERIC(12, 6) NOT NULL DEFAULT 0,
    latency_ms      INT         NOT NULL DEFAULT 0,
    tool_calls      INT         NOT NULL DEFAULT 0,
    ok              BOOLEAN     NOT NULL DEFAULT TRUE,
    error           TEXT,
    started_at      TIMESTAMPTZ NOT NULL,
    ended_at        TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS node_metrics_run_idx ON node_metrics (run_id);

-- One row per run, upserted as the run progresses.
CREATE TABLE IF NOT EXISTS run_metrics (
    run_id              TEXT PRIMARY KEY,
    repo                TEXT        NOT NULL,
    issue_ref           TEXT        NOT NULL,
    phase               TEXT        NOT NULL,
    outcome             TEXT,
    attempts            INT         NOT NULL DEFAULT 0,
    input_tokens        INT         NOT NULL DEFAULT 0,
    output_tokens       INT         NOT NULL DEFAULT 0,
    cost_usd            NUMERIC(12, 6) NOT NULL DEFAULT 0,
    latency_ms          INT         NOT NULL DEFAULT 0,
    tool_calls          INT         NOT NULL DEFAULT 0,
    tests_passed        INT,
    tests_failed        INT,
    -- Deterministic hallucination signal: references to files/symbols the
    -- repo does not contain, over total references made.
    nonexistent_refs    INT         NOT NULL DEFAULT 0,
    total_refs          INT         NOT NULL DEFAULT 0,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Raw event log. The append-only source of truth the two tables above are
-- projections of; keeps a bad aggregation from being unrecoverable.
CREATE TABLE IF NOT EXISTS metric_events (
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT        NOT NULL,
    kind        TEXT        NOT NULL,
    payload     JSONB       NOT NULL,
    emitted_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS metric_events_run_idx ON metric_events (run_id, id);

-- Artifacts too large or too structured for the event payload: retrieved
-- context, final patch, raw test output.
CREATE TABLE IF NOT EXISTS run_artifacts (
    id          BIGSERIAL PRIMARY KEY,
    run_id      TEXT        NOT NULL,
    kind        TEXT        NOT NULL,
    content     TEXT        NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS run_artifacts_run_idx ON run_artifacts (run_id, kind);
