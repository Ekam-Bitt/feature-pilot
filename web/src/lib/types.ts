/**
 * The API's wire shapes.
 *
 * Written to mirror `featurepilot.metrics.events.MetricEvent` and the run
 * endpoints, and kept deliberately loose where the backend is: `payload` is
 * documented there as open on purpose, so that adding a measurement is not a
 * schema migration. Narrowing it here would re-impose exactly the coupling the
 * backend avoided.
 */

export type FpEvent = {
  run_id: string;
  kind: string;
  payload: Record<string, unknown>;
  emitted_at: string;
};

/** Phases in rail order. DEBUGGING is absent: it is shown only if entered. */
export const RAIL = ["PLANNING", "CODING", "TESTING", "REVIEW", "DONE"] as const;

export type RunStatus = {
  run_id: string;
  repo: string;
  issue_ref: string;
  phase: string;
  finished: boolean;
  awaiting_human: boolean;
  pending: Record<string, unknown> | null;
  error: string | null;
  pr_url: string | null;
  publishable: boolean;
  queued: boolean;
  /** The draft choice made when the run started; publishing reuses it. */
  draft: boolean;
};

export type PrSummary = { title: string; body: string; test_plan: string };

export type RunArtifacts = {
  run_id: string;
  diff: string | null;
  pr_summary: PrSummary | null;
  test_summary: string | null;
};

export type PlanStep = { description?: string; files?: string[] };

export type StartRunBody = {
  issue_url: string;
  auto_approve: boolean;
  auto_publish: boolean;
  draft: boolean;
  anthropic_api_key?: string;
  github_token?: string;
};
