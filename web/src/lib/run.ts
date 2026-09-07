/**
 * The event stream, folded into what the page draws.
 *
 * All of the run view's logic lives here, as one pure function over a list of
 * events, for two reasons. The stream replays history before it follows —
 * `_events` in the API emits everything already recorded, then subscribes — so
 * a late-joining browser must reach the same state as one that watched from
 * the start. And every awkward case (a phase revisited by the repair loop, a
 * gate that opens twice, a payload missing a field) is then a test rather than
 * a bug hiding in a component.
 */

import type { FpEvent, PlanStep } from "@/lib/types";

export type FeedEntry = {
  key: string;
  kind: string;
  /** One line, already phrased for a reader. */
  text: string;
  detail?: string;
  failed?: boolean;
  at: string;
};

export type Gate = {
  summary: string;
  steps: PlanStep[];
  openQuestions: string[];
  confidence?: string;
};

export type Totals = {
  costUsd: number;
  inputTokens: number;
  outputTokens: number;
  modelCalls: number;
  toolCalls: number;
};

export type RunState = {
  phase: string;
  /**
   * When the run began, from the first event that carries a timestamp — not
   * page load. The stream replays history, so a browser opened late must not
   * report a clock that starts at zero.
   */
  startedAt: number | null;
  phasesEntered: string[];
  feed: FeedEntry[];
  totals: Totals;
  gate: Gate | null;
  finished: boolean;
  error: string | null;
  prUrl: string | null;
};

export const emptyRun = (): RunState => ({
  phase: "CREATED",
  startedAt: null,
  phasesEntered: [],
  feed: [],
  totals: { costUsd: 0, inputTokens: 0, outputTokens: 0, modelCalls: 0, toolCalls: 0 },
  gate: null,
  finished: false,
  error: null,
  prUrl: null,
});

const str = (v: unknown): string | undefined => (typeof v === "string" ? v : undefined);
const num = (v: unknown): number => (typeof v === "number" ? v : 0);

let seq = 0;
const nextKey = (kind: string) => `${kind}-${seq++}`;

const usd = (n: number) => `$${n.toFixed(4)}`;

/** Keys that identify what a call acted on, most specific first. */
const TOOL_SUBJECT = ["path", "pattern", "query", "command", "selector", "old"] as const;

const clip = (s: string, max = 64): string =>
  s.length <= max ? s : `${s.slice(0, max - 1)}…`;

/**
 * A tool call, phrased as the one line a reader wants.
 *
 * The interesting part is nested: the payload is
 * `{tool, node, ok, args: {...}}`, and reading the top level instead gives a
 * bare tool name — which turned a real run's 48 retrieval calls into 48
 * identical rows saying "grep".
 */
function toolLine(p: Record<string, unknown>): FeedEntry {
  const tool = str(p.tool) ?? str(p.name) ?? "tool";
  const args = (p.args ?? {}) as Record<string, unknown>;

  let subject = TOOL_SUBJECT.map((k) => str(args[k])).find((v) => v !== undefined);
  // An unfamiliar tool still has arguments worth showing; take the first
  // string one rather than rendering a bare name.
  subject ??= Object.values(args).map(str).find((v) => v !== undefined);

  const failed = p.ok === false;
  const error = str(p.error);
  const head = subject ? `${tool} ${clip(subject)}` : tool;

  return {
    key: nextKey("tool"),
    kind: "tool_called",
    text: failed ? `${head} — ${error ?? "failed"}` : head,
    detail: str(p.node),
    failed,
    at: "",
  };
}

function modelLine(p: Record<string, unknown>): FeedEntry {
  const role = str(p.role) ?? "model";
  const cost = num(p.cost_usd);
  const tokens = num(p.input_tokens) + num(p.output_tokens);
  return {
    key: nextKey("model"),
    kind: "model_called",
    text: `${role} thought — ${tokens.toLocaleString()} tokens, ${usd(cost)}`,
    detail: role,
    at: "",
  };
}

function nodeEndedLine(p: Record<string, unknown>): FeedEntry {
  const node = str(p.node) ?? "node";
  const ok = p.ok !== false;
  const ms = num(p.latency_ms);
  const error = str(p.error);
  const took = ms ? ` in ${(ms / 1000).toFixed(1)}s` : "";
  return {
    key: nextKey("node"),
    kind: "node_ended",
    text: ok
      ? `${node} finished${took}`
      : `${node} failed — ${error ?? "no reason given"}`,
    detail: node,
    failed: !ok,
    at: "",
  };
}

function readGate(payload: Record<string, unknown>): Gate | null {
  const pending = (payload.pending ?? payload) as Record<string, unknown>;
  const summary = str(pending.summary);
  // No payload means no card: an empty approval box asks a question the run
  // never actually asked.
  if (summary === undefined) return null;
  return {
    summary,
    steps: Array.isArray(pending.steps) ? (pending.steps as PlanStep[]) : [],
    openQuestions: Array.isArray(pending.open_questions)
      ? (pending.open_questions as string[])
      : [],
    confidence: str(pending.confidence),
  };
}

/**
 * One event, from an SSE name and whatever JSON came with it.
 *
 * The stream sends two shapes under the same names. Recorded events are
 * MetricEvents — `{run_id, kind, payload, emitted_at}`. But the API also
 * synthesises `awaiting_human` and `run_finished` in `_events`, and those are
 * bare objects: the plan directly under `pending`, or the run's whole public
 * status. Neither has a `kind`, so reading one from the body dropped them
 * both — no approval gate, and no diff at the end.
 *
 * The SSE event name is the authority. It always exists, and the server chose
 * it deliberately.
 */
export function toEvent(name: string, body: Record<string, unknown>): FpEvent {
  const payload =
    body.payload && typeof body.payload === "object"
      ? (body.payload as Record<string, unknown>)
      : body;
  return {
    run_id: String(body.run_id ?? ""),
    kind: name,
    payload,
    emitted_at: typeof body.emitted_at === "string" ? body.emitted_at : "",
  };
}

export function reduce(state: RunState, event: FpEvent): RunState {
  const p = event.payload ?? {};

  const at = event.emitted_at ? Date.parse(event.emitted_at) : Number.NaN;
  if (!Number.isNaN(at) && (state.startedAt === null || at < state.startedAt)) {
    state = { ...state, startedAt: at };
  }

  switch (event.kind) {
    case "phase_changed": {
      const dst = str(p.dst) ?? str(p.phase);
      if (!dst) return state;
      return {
        ...state,
        phase: dst,
        // A revisited phase is not a new one: the repair loop returns to
        // CODING, and the rail should not grow a second entry for it.
        phasesEntered: state.phasesEntered.includes(dst)
          ? state.phasesEntered
          : [...state.phasesEntered, dst],
        // Moving on is how a gate closes. Nothing else says "answered".
        gate: null,
      };
    }

    case "tool_called":
      return {
        ...state,
        feed: [...state.feed, { ...toolLine(p), at: event.emitted_at }],
        totals: { ...state.totals, toolCalls: state.totals.toolCalls + 1 },
      };

    case "model_called":
      return {
        ...state,
        feed: [...state.feed, { ...modelLine(p), at: event.emitted_at }],
        totals: {
          ...state.totals,
          costUsd: state.totals.costUsd + num(p.cost_usd),
          inputTokens: state.totals.inputTokens + num(p.input_tokens),
          outputTokens: state.totals.outputTokens + num(p.output_tokens),
          modelCalls: state.totals.modelCalls + 1,
        },
      };

    case "node_ended":
      return {
        ...state,
        feed: [...state.feed, { ...nodeEndedLine(p), at: event.emitted_at }],
      };

    case "awaiting_human": {
      const gate = readGate(p);
      return gate ? { ...state, gate } : state;
    }

    case "run_finished": {
      const phase = str(p.phase);
      return {
        ...state,
        finished: true,
        phase: phase ?? state.phase,
        error: str(p.error) ?? state.error,
        prUrl: str(p.pr_url) ?? state.prUrl,
        gate: null,
      };
    }

    // run_started, node_started and artifact carry nothing the page needs that
    // another event does not already say. An unknown kind lands here too:
    // the payload is open by design, so a new one must not break the view.
    default:
      return state;
  }
}

export const reduceAll = (state: RunState, events: FpEvent[]): RunState =>
  events.reduce(reduce, state);
