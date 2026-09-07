"use client";

import { useState } from "react";
import type { Gate } from "@/lib/run";

/**
 * The approval gate — the moment this is not a black box.
 *
 * The plan is shown as the planner wrote it, including its open questions,
 * which are the interesting part: the agent asking rather than guessing. Each
 * gets a field, and the answers go back in order, which is the contract
 * `HumanDecision.answers` describes.
 */
export function GateCard({
  gate,
  busy,
  onDecide,
}: {
  gate: Gate;
  busy: boolean;
  onDecide: (
    verdict: "approve" | "reject",
    feedback: string,
    answers: string[],
  ) => void;
}) {
  const [answers, setAnswers] = useState<string[]>(() =>
    gate.openQuestions.map(() => ""),
  );
  const [feedback, setFeedback] = useState("");
  const [rejecting, setRejecting] = useState(false);

  return (
    <section className="border border-accent/40 bg-accent-sunk/60 p-5">
      <div className="flex items-baseline justify-between gap-4">
        <h2 className="text-[13px] font-medium uppercase tracking-[0.08em] text-accent">
          Your approval
        </h2>
        {gate.confidence && (
          <span className="text-[12px] text-ink-muted">
            planner confidence: {gate.confidence}
          </span>
        )}
      </div>

      <p className="mt-3 text-[15px] leading-relaxed text-ink">{gate.summary}</p>

      {gate.steps.length > 0 && (
        <ol className="mt-4 space-y-2">
          {gate.steps.map((step, i) => (
            <li key={i} className="flex gap-3 text-[13.5px] leading-relaxed">
              <span className="shrink-0 font-mono text-[12px] text-ink-muted">
                {i + 1}
              </span>
              <span className="text-ink-secondary">
                {step.description}
                {step.files && step.files.length > 0 && (
                  <span className="ml-2 font-mono text-[11.5px] text-ink-muted">
                    {step.files.join(", ")}
                  </span>
                )}
              </span>
            </li>
          ))}
        </ol>
      )}

      {gate.openQuestions.length > 0 && (
        <div className="mt-5 space-y-3">
          <p className="text-[12px] uppercase tracking-[0.08em] text-ink-muted">
            It needs an answer
          </p>
          {gate.openQuestions.map((question, i) => (
            <label key={i} className="block">
              <span className="text-[13.5px] text-ink">{question}</span>
              <input
                value={answers[i] ?? ""}
                onChange={(e) => {
                  const next = [...answers];
                  next[i] = e.target.value;
                  setAnswers(next);
                }}
                className="mt-1 w-full border-b border-rule-strong bg-transparent py-1 text-[13.5px] outline-none focus:border-accent"
                placeholder="Your answer"
              />
            </label>
          ))}
        </div>
      )}

      {rejecting && (
        <label className="mt-4 block">
          <span className="text-[12px] uppercase tracking-[0.08em] text-ink-muted">
            What should change
          </span>
          <textarea
            value={feedback}
            onChange={(e) => setFeedback(e.target.value)}
            rows={3}
            className="mt-1 w-full border border-rule-strong bg-paper p-2 text-[13.5px] outline-none focus:border-accent"
            placeholder="The planner sees this and tries again."
          />
        </label>
      )}

      <div className="mt-5 flex items-center gap-3">
        <button
          type="button"
          disabled={busy}
          onClick={() => onDecide("approve", "", answers)}
          className="bg-ink px-4 py-2 text-[13px] font-medium text-paper transition-opacity hover:opacity-85 disabled:opacity-40"
        >
          {busy ? "Sending…" : "Approve and continue"}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            if (!rejecting) return setRejecting(true);
            onDecide("reject", feedback, answers);
          }}
          className="text-[13px] text-ink-secondary underline decoration-rule-strong underline-offset-4 hover:text-ink disabled:opacity-40"
        >
          {rejecting ? "Send changes" : "Request changes"}
        </button>
      </div>
    </section>
  );
}
