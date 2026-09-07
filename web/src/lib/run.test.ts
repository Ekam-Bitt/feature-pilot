import { describe, expect, it } from "vitest";
import { emptyRun, reduce, reduceAll, toEvent } from "@/lib/run";
import type { FpEvent } from "@/lib/types";

/** Event shapes copied from a real run's SSE stream, not invented. */
const ev = (kind: string, payload: Record<string, unknown> = {}): FpEvent => ({
  run_id: "r1",
  kind,
  payload,
  emitted_at: "2026-09-08T00:00:00Z",
});

describe("phases", () => {
  it("records the phase a run has reached", () => {
    const s = reduceAll(emptyRun(), [
      ev("phase_changed", { src: "CREATED", dst: "PLANNING" }),
      ev("phase_changed", { src: "PLANNING", dst: "CODING" }),
    ]);
    expect(s.phase).toBe("CODING");
    expect(s.phasesEntered).toEqual(["PLANNING", "CODING"]);
  });

  it("keeps DEBUGGING out of the rail until the repair loop actually fires", () => {
    const quiet = reduceAll(emptyRun(), [ev("phase_changed", { dst: "TESTING" })]);
    expect(quiet.phasesEntered).not.toContain("DEBUGGING");

    const repaired = reduce(quiet, ev("phase_changed", { dst: "DEBUGGING" }));
    expect(repaired.phasesEntered).toContain("DEBUGGING");
  });

  it("does not list a phase twice when the repair loop revisits it", () => {
    const s = reduceAll(emptyRun(), [
      ev("phase_changed", { dst: "CODING" }),
      ev("phase_changed", { dst: "TESTING" }),
      ev("phase_changed", { dst: "DEBUGGING" }),
      ev("phase_changed", { dst: "CODING" }),
    ]);
    expect(s.phasesEntered.filter((p) => p === "CODING")).toHaveLength(1);
    expect(s.phase).toBe("CODING");
  });
});

describe("totals", () => {
  it("accumulates cost, tokens and calls from model calls", () => {
    const s = reduceAll(emptyRun(), [
      ev("model_called", {
        role: "planner", model: "anthropic/claude-sonnet-5",
        input_tokens: 6986, output_tokens: 313, cost_usd: 0.017102,
      }),
      ev("model_called", {
        role: "coder", model: "anthropic/claude-sonnet-5",
        input_tokens: 7707, output_tokens: 135, cost_usd: 0.016764,
      }),
    ]);
    expect(s.totals.modelCalls).toBe(2);
    expect(s.totals.inputTokens).toBe(14693);
    expect(s.totals.outputTokens).toBe(448);
    expect(s.totals.costUsd).toBeCloseTo(0.033866, 6);
  });

  it("counts tool calls separately from model calls", () => {
    const s = reduceAll(emptyRun(), [
      ev("tool_called", { tool: "read_file", node: "retrieve" }),
      ev("tool_called", { tool: "grep", node: "retrieve" }),
      ev("model_called", { role: "planner", cost_usd: 0.01 }),
    ]);
    expect(s.totals.toolCalls).toBe(2);
    expect(s.totals.modelCalls).toBe(1);
  });

  it("survives a model call with fields missing", () => {
    const s = reduce(emptyRun(), ev("model_called", { role: "coder" }));
    expect(s.totals.costUsd).toBe(0);
    expect(s.totals.modelCalls).toBe(1);
  });
});

describe("the feed", () => {
  it("keeps events in arrival order with a stable key", () => {
    const s = reduceAll(emptyRun(), [
      ev("tool_called", { tool: "grep" }),
      ev("node_ended", { node: "retrieve", latency_ms: 1241, ok: true }),
    ]);
    expect(s.feed.map((f) => f.kind)).toEqual(["tool_called", "node_ended"]);
    expect(new Set(s.feed.map((f) => f.key)).size).toBe(2);
  });

  it("describes a failed node so the feed shows the repair loop's cause", () => {
    const s = reduce(
      emptyRun(),
      ev("node_ended", { node: "test", ok: false, error: "3 failed" }),
    );
    expect(s.feed[0].failed).toBe(true);
    expect(s.feed[0].text).toContain("3 failed");
    expect(s.feed[0].detail).toBe("test");
  });
});

describe("the approval gate", () => {
  const plan = {
    kind: "plan_approval",
    summary: "Judge shipping on the payable amount.",
    steps: [{ description: "subtract both discounts", files: ["src/cart.py"] }],
    open_questions: ["Should discounts stack?"],
    confidence: "high",
  };

  it("opens when the run parks on a human", () => {
    const s = reduce(emptyRun(), ev("awaiting_human", { pending: plan }));
    expect(s.gate?.summary).toBe("Judge shipping on the payable amount.");
    expect(s.gate?.openQuestions).toEqual(["Should discounts stack?"]);
  });

  it("closes once the run moves on, so an answered plan stops asking", () => {
    const open = reduce(emptyRun(), ev("awaiting_human", { pending: plan }));
    const moved = reduce(open, ev("phase_changed", { dst: "CODING" }));
    expect(moved.gate).toBeNull();
  });

  it("ignores a gate with no payload rather than rendering an empty card", () => {
    const s = reduce(emptyRun(), ev("awaiting_human", {}));
    expect(s.gate).toBeNull();
  });
});

describe("finishing", () => {
  it("marks a run finished and carries the terminal phase", () => {
    const s = reduce(
      emptyRun(),
      ev("run_finished", { phase: "DONE", finished: true, pr_url: null }),
    );
    expect(s.finished).toBe(true);
    expect(s.phase).toBe("DONE");
  });

  it("keeps the feed and the reason when a run fails", () => {
    const s = reduceAll(emptyRun(), [
      ev("tool_called", { tool: "grep" }),
      ev("run_finished", { phase: "FAILED", finished: true, error: "budget exceeded" }),
    ]);
    expect(s.finished).toBe(true);
    expect(s.phase).toBe("FAILED");
    expect(s.error).toBe("budget exceeded");
    expect(s.feed).toHaveLength(1);
  });

  it("takes a pr_url from the finishing event", () => {
    const s = reduce(
      emptyRun(),
      ev("run_finished", { phase: "DONE", pr_url: "https://github.com/a/b/pull/9" }),
    );
    expect(s.prUrl).toBe("https://github.com/a/b/pull/9");
  });
});

describe("replay", () => {
  it("is idempotent, because the stream replays history before following it", () => {
    const events = [
      ev("run_started", {}),
      ev("phase_changed", { dst: "PLANNING" }),
      ev("model_called", { role: "planner", cost_usd: 0.02, input_tokens: 100 }),
    ];
    const once = reduceAll(emptyRun(), events);
    const twice = reduceAll(emptyRun(), [...events, ...events]);
    // Totals legitimately double; what must not change is the run's shape.
    expect(twice.phase).toBe(once.phase);
    expect(twice.phasesEntered).toEqual(once.phasesEntered);
  });

  it("ignores an event kind it has never seen", () => {
    const s = reduce(emptyRun(), ev("something_new_next_year", { x: 1 }));
    expect(s.feed).toHaveLength(0);
    expect(s.phase).toBe(emptyRun().phase);
  });
});

describe("tool lines, against real payloads", () => {
  // Copied verbatim from metric_events on a live run. The arguments are nested
  // under `args`, which a first pass at this got wrong — every line rendered
  // as a bare tool name, and 48 identical rows tell a reader nothing.
  const readFile = ev("tool_called", {
    ok: true,
    args: { path: "./src/shopsvc/pricing.py" },
    node: "retrieve",
    tool: "read_file",
    source: "mcp:filesystem",
    content_len: 1968,
  });

  it("names what the tool acted on", () => {
    const s = reduce(emptyRun(), readFile);
    expect(s.feed[0].text).toBe("read_file ./src/shopsvc/pricing.py");
  });

  it("attributes the call to its node", () => {
    const s = reduce(emptyRun(), readFile);
    expect(s.feed[0].detail).toBe("retrieve");
  });

  it("marks a failed tool call, since that is what precedes a repair", () => {
    const s = reduce(
      emptyRun(),
      ev("tool_called", {
        ok: false,
        args: { path: "nope.py" },
        node: "code",
        tool: "edit_file",
        error: "no such file",
      }),
    );
    expect(s.feed[0].failed).toBe(true);
    expect(s.feed[0].text).toContain("no such file");
  });

  it("falls back to the first argument when the shape is unfamiliar", () => {
    const s = reduce(
      emptyRun(),
      ev("tool_called", { tool: "run_tests", args: { selector: "tests/test_cart.py" } }),
    );
    expect(s.feed[0].text).toBe("run_tests tests/test_cart.py");
  });

  it("shows a bare tool name when it took no arguments", () => {
    const s = reduce(emptyRun(), ev("tool_called", { tool: "list_dir", args: {} }));
    expect(s.feed[0].text).toBe("list_dir");
  });

  it("truncates an argument long enough to break the line", () => {
    const long = "x".repeat(200);
    const s = reduce(emptyRun(), ev("tool_called", { tool: "grep", args: { pattern: long } }));
    expect(s.feed[0].text.length).toBeLessThan(90);
    expect(s.feed[0].text).toContain("…");
  });
});

describe("normalising what the stream actually sends", () => {
  /**
   * The stream carries two different shapes under the same SSE names.
   *
   * Recorded events are MetricEvents: {run_id, kind, payload, emitted_at}.
   * But `_events` also synthesises `awaiting_human` and `run_finished`
   * itself, and those are bare objects — the plan directly under `pending`,
   * or the run's whole public status — with no `kind` and no `payload`.
   *
   * Trusting `body.kind` therefore silently dropped both: the approval gate
   * never appeared and a finished run never showed its diff. The SSE event
   * name is the authority on what an event is.
   */
  it("takes the kind from the SSE name, not the body", () => {
    const s = reduce(
      emptyRun(),
      toEvent("awaiting_human", {
        run_id: "r1",
        pending: { kind: "plan_approval", summary: "Stack the promos.", steps: [] },
      }),
    );
    expect(s.gate?.summary).toBe("Stack the promos.");
  });

  it("reads a synthesised run_finished, which is the run's public status", () => {
    const s = reduce(
      emptyRun(),
      toEvent("run_finished", {
        run_id: "r1",
        phase: "DONE",
        finished: true,
        pr_url: null,
        error: null,
      }),
    );
    expect(s.finished).toBe(true);
    expect(s.phase).toBe("DONE");
  });

  it("still unwraps a recorded MetricEvent's payload", () => {
    const s = reduce(
      emptyRun(),
      toEvent("phase_changed", {
        run_id: "r1",
        kind: "phase_changed",
        payload: { src: "CREATED", dst: "PLANNING" },
        emitted_at: "2026-09-08T00:00:00Z",
      }),
    );
    expect(s.phase).toBe("PLANNING");
  });

  it("ignores the recorder's own awaiting_human, which carries no plan", () => {
    // {detail, reason} — a note that a human was asked, not the question.
    const s = reduce(
      emptyRun(),
      toEvent("awaiting_human", {
        run_id: "r1",
        kind: "awaiting_human",
        payload: { detail: "shipping is judged on the pre-discount subtotal", reason: "plan_approval" },
        emitted_at: "2026-09-08T00:00:00Z",
      }),
    );
    expect(s.gate).toBeNull();
  });
});

describe("elapsed time", () => {
  /**
   * A clock started when the page loaded read 00:08 for a run that had been
   * going seven minutes — the page had simply been opened late. The stream
   * replays history, so the first event's timestamp is when the run actually
   * began, and that survives a reload.
   */
  it("takes its start from the first event, not from page load", () => {
    const s = reduceAll(emptyRun(), [
      { run_id: "r1", kind: "run_started", payload: {}, emitted_at: "2026-09-08T00:00:00Z" },
      { run_id: "r1", kind: "tool_called", payload: { tool: "grep" }, emitted_at: "2026-09-08T00:02:00Z" },
    ]);
    expect(s.startedAt).toBe(Date.parse("2026-09-08T00:00:00Z"));
  });

  it("keeps the earliest, so a later event cannot move the start", () => {
    const s = reduceAll(emptyRun(), [
      { run_id: "r1", kind: "run_started", payload: {}, emitted_at: "2026-09-08T00:05:00Z" },
      { run_id: "r1", kind: "tool_called", payload: { tool: "grep" }, emitted_at: "2026-09-08T00:09:00Z" },
    ]);
    expect(s.startedAt).toBe(Date.parse("2026-09-08T00:05:00Z"));
  });

  it("stays null when the synthesised events carry no timestamp", () => {
    const s = reduce(emptyRun(), toEvent("run_finished", { run_id: "r1", phase: "DONE" }));
    expect(s.startedAt).toBeNull();
  });
});
