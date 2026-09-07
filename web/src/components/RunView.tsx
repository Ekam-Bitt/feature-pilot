"use client";

import { useCallback, useEffect, useReducer, useState } from "react";
import { Diff } from "@/components/Diff";
import { Feed } from "@/components/Feed";
import { GateCard } from "@/components/GateCard";
import { Masthead } from "@/components/Masthead";
import { PhaseRail } from "@/components/PhaseRail";
import { StatRow } from "@/components/StatRow";
import { ApiError, approveRun, getArtifacts, getRun, publishRun, streamUrl } from "@/lib/api";
import {
  emptyRun,
  eventKey,
  gateFromStatus,
  reduce,
  toEvent,
  type RunState,
} from "@/lib/run";
import type { FpEvent, RunArtifacts, RunStatus } from "@/lib/types";

const clock = (seconds: number): string =>
  `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;

type RunAction = { event: FpEvent } | { status: RunStatus };

/** Events and polled status both fold into the same state. */
function runReducer(state: RunState, action: RunAction): RunState {
  return "event" in action
    ? reduce(state, action.event)
    : gateFromStatus(state, action.status);
}

export function RunView({ runId }: { runId: string }) {
  const [state, dispatch] = useReducer(runReducer, undefined, emptyRun);
  const [status, setStatus] = useState<RunStatus | null>(null);
  const [artifacts, setArtifacts] = useState<RunArtifacts | null>(null);
  const [gateBusy, setGateBusy] = useState(false);
  const [publishing, setPublishing] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [now, setNow] = useState(0);

  // The stream replays everything already recorded before it follows, so a
  // browser opened late reaches the same state as one that watched from the
  // start.
  //
  // That replay is also a hazard, and it bit hard. EventSource reconnects on
  // its own whenever the server closes the connection, which it does the
  // moment a run finishes — so a finished run reconnected every few seconds,
  // replayed its history, and the additive totals climbed without end: $0.16
  // of real spend read as $11. Two things stop it. Every event is applied at
  // most once, by identity; and the stream is closed for good once the run is
  // over, so there is nothing left to reconnect.
  useEffect(() => {
    const source = new EventSource(streamUrl(runId));
    const applied = new Set<string>();

    const listen = (name: string) => (e: MessageEvent) => {
      try {
        const event = toEvent(name, JSON.parse(e.data) as Record<string, unknown>);
        const key = eventKey(event);
        if (applied.has(key)) return;
        applied.add(key);
        dispatch({ event });

        if (name === "run_finished") {
          // Nothing more will happen, and leaving it open invites the
          // reconnect loop this whole comment is about.
          source.close();
        }
      } catch {
        /* a keepalive, or JSON this version does not model */
      }
    };
    for (const kind of [
      "run_started",
      "phase_changed",
      "node_started",
      "node_ended",
      "tool_called",
      "model_called",
      "awaiting_human",
      "artifact",
      "run_finished",
    ]) {
      source.addEventListener(kind, listen(kind) as EventListener);
    }
    source.onerror = () => {
      // A finished run closes the stream; that is not a failure worth showing.
      if (source.readyState === EventSource.CLOSED) source.close();
    };
    return () => source.close();
  }, [runId]);

  // The stream carries what happened; this carries what is true now — the
  // phase after a restart, the pr_url after publishing, and whether the run
  // is still queued behind another.
  useEffect(() => {
    let alive = true;
    const poll = async () => {
      try {
        const next = await getRun(runId);
        if (!alive) return;
        setStatus(next);
        // The stream is the livelier channel and the less reliable one; this
        // is the fallback that keeps a parked run answerable without it.
        dispatch({ status: next });
      } catch (err) {
        if (alive && err instanceof ApiError && err.status === 404) {
          setNotice(
            "The agent has no record of this run. It restarts with an empty memory, so a run started before a restart cannot be rejoined.",
          );
        }
      }
    };
    void poll();
    const id = setInterval(poll, 2500);
    return () => {
      alive = false;
      clearInterval(id);
    };
  }, [runId]);

  const finished = state.finished || status?.finished === true;

  // Artifacts appear only at the end; the sandbox produces the diff last.
  useEffect(() => {
    if (!finished || artifacts?.diff) return;
    void getArtifacts(runId)
      .then(setArtifacts)
      .catch(() => undefined);
  }, [finished, runId, artifacts?.diff]);

  // The clock lives in state because reading Date.now() in the render body is
  // an impure read. Nothing is set synchronously here either — that triggers a
  // cascading render — so the first value arrives on the first tick and the
  // clock shows an ellipsis until then.
  useEffect(() => {
    if (finished) return;
    const id = setInterval(() => setNow(Date.now()), 500);
    return () => clearInterval(id);
  }, [finished]);

  const decide = useCallback(
    async (verdict: "approve" | "reject", feedback: string, answers: string[]) => {
      setGateBusy(true);
      try {
        await approveRun(runId, verdict, feedback, answers);
      } catch (err) {
        setNotice(err instanceof ApiError ? err.message : "Could not send that.");
      } finally {
        setGateBusy(false);
      }
    },
    [runId],
  );

  // Read out before the callback: an optional chain in a dependency list
  // defeats the compiler's memoization, and this is a plain value anyway.
  // Draft by default — the server forces it for runs on its own credentials.
  const wantsDraft = status?.draft ?? true;

  const publish = useCallback(async () => {
    setPublishing(true);
    setNotice(null);
    try {
      const url = await publishRun(runId, wantsDraft);
      setStatus((s) => (s ? { ...s, pr_url: url } : s));
    } catch (err) {
      setNotice(err instanceof ApiError ? err.message : "Could not publish.");
    } finally {
      setPublishing(false);
    }
  }, [runId, wantsDraft]);

  const [slug, issueNumber] = (status?.issue_ref ?? "").split("#");
  const issueUrl = issueNumber ? `https://github.com/${slug}/issues/${issueNumber}` : null;

  const prUrl = status?.pr_url ?? state.prUrl;
  const phase = state.phase === "CREATED" ? (status?.phase ?? "CREATED") : state.phase;
  const error = state.error ?? status?.error ?? null;

  return (
    <>
      <Masthead subdued />
      <main className="mx-auto w-full max-w-3xl grow px-6 py-10">
        <div className="flex flex-wrap items-baseline justify-between gap-x-6 gap-y-2">
          <h1 className="font-mono text-[14px] text-ink">
            {issueUrl ? (
              <a
                href={issueUrl}
                target="_blank"
                rel="noreferrer"
                className="underline decoration-rule-strong underline-offset-4 hover:text-accent"
              >
                {status?.issue_ref}
              </a>
            ) : (
              (status?.issue_ref ?? "…")
            )}
          </h1>
          <span
            className="text-[13px] text-ink-muted"
            style={{ fontVariantNumeric: "tabular-nums" }}
            aria-hidden={finished}
          >
            {finished
              ? "finished"
              : state.startedAt && now
                ? clock(Math.max(0, Math.floor((now - state.startedAt) / 1000)))
                : "…"}
          </span>
        </div>

        <div className="mt-4">
          <PhaseRail
            phase={phase}
            entered={state.phasesEntered}
            finished={Boolean(finished)}
          />
        </div>

        {status?.queued && (
          <p className="mt-5 border-l-2 border-rule-strong pl-3 text-[13.5px] leading-relaxed text-ink-secondary">
            Waiting for a free slot. Each run holds its own container, so this host
            runs a couple at a time and queues the rest.
          </p>
        )}

        {notice && (
          <p className="mt-5 border-l-2 border-bad pl-3 text-[13.5px] leading-relaxed text-bad">
            {notice}
          </p>
        )}

        {state.gate && !finished && (
          <div className="mt-7">
            <GateCard gate={state.gate} busy={gateBusy} onDecide={decide} />
          </div>
        )}

        <div className="mt-7">
          <StatRow totals={state.totals} />
        </div>

        <div className="mt-1">
          <Feed entries={state.feed} live={!finished} />
        </div>

        {finished && phase === "FAILED" && (
          <section className="mt-8 border-l-2 border-bad pl-4">
            <h2 className="text-[13px] font-medium uppercase tracking-[0.08em] text-bad">
              It could not finish
            </h2>
            <p className="mt-2 text-[13.5px] leading-relaxed text-ink-secondary">
              {error ?? "The run ended without a patch. The feed above is what happened."}
            </p>
          </section>
        )}

        {finished && artifacts?.pr_summary && (
          <section className="mt-10 border-t border-rule pt-8">
            <h2 className="text-[12px] uppercase tracking-[0.08em] text-ink-muted">
              What it wrote
            </h2>
            <h3 className="mt-3 text-[19px] font-semibold leading-snug tracking-tight text-ink">
              {artifacts.pr_summary.title}
            </h3>
            <p className="mt-3 whitespace-pre-wrap text-[14px] leading-relaxed text-ink-secondary">
              {artifacts.pr_summary.body}
            </p>

            {artifacts.test_summary && (
              // Whole, not the first line. The first line is raw counts — "72
              // passed, 13 failed" — which reads as a broken patch on a
              // repository that was already failing 17 tests before the agent
              // touched it. The rest is the part that is true: what this patch
              // fixed, and whether it broke anything.
              <pre className="mt-5 overflow-x-auto whitespace-pre-wrap border-l-2 border-rule-strong pl-4 font-mono text-[12px] leading-relaxed text-ink-secondary">
                {artifacts.test_summary.trim()}
              </pre>
            )}

            {artifacts.diff && (
              <div className="mt-6">
                <Diff diff={artifacts.diff} />
              </div>
            )}

            <div className="mt-7 flex flex-wrap items-center gap-x-5 gap-y-3">
              {prUrl ? (
                <a
                  href={prUrl}
                  target="_blank"
                  rel="noreferrer"
                  className="bg-ink px-5 py-2.5 text-[13.5px] font-medium text-paper hover:opacity-85"
                >
                  Open the pull request
                </a>
              ) : (
                <button
                  type="button"
                  onClick={publish}
                  disabled={publishing || !status?.publishable}
                  className="bg-ink px-5 py-2.5 text-[13.5px] font-medium text-paper transition-opacity hover:opacity-85 disabled:opacity-30"
                >
                  {publishing ? "Publishing…" : "Open the pull request"}
                </button>
              )}
              <span className="text-[12.5px] text-ink-muted">
                {prUrl
                  ? prUrl.replace("https://github.com/", "")
                  : "Forks the repository, pushes a branch, and opens it."}
              </span>
            </div>
          </section>
        )}
      </main>
    </>
  );
}
