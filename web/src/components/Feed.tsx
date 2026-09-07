"use client";

import { useEffect, useRef } from "react";
import type { FeedEntry } from "@/lib/run";

/**
 * The gears.
 *
 * Monospace, because every line here is something the machine did. Tool calls
 * are the bulk of it — a real run emitted 49 of them against 6 model calls —
 * so they are the quietest row, and model calls and node boundaries are what
 * the eye should catch.
 *
 * It follows the tail only while the reader is already at the bottom: yanking
 * the viewport away from someone who scrolled up to read a failure is the
 * fastest way to make a live feed useless.
 */
export function Feed({ entries, live }: { entries: FeedEntry[]; live: boolean }) {
  const boxRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);

  useEffect(() => {
    const box = boxRef.current;
    if (!box || !pinnedRef.current) return;
    box.scrollTop = box.scrollHeight;
  }, [entries.length]);

  return (
    <div
      ref={boxRef}
      onScroll={(e) => {
        const el = e.currentTarget;
        pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      }}
      className="max-h-[26rem] overflow-y-auto font-mono text-[12.5px] leading-relaxed"
      aria-live={live ? "polite" : "off"}
      aria-label="Run activity"
    >
      {entries.length === 0 && (
        <p className="py-6 font-sans text-[13px] text-ink-muted">
          Waiting for the first step. Cloning the repository and starting the sandbox
          takes a moment.
        </p>
      )}
      <ul>
        {entries.map((entry) => (
          <li
            key={entry.key}
            className="feed-enter flex items-baseline gap-2.5 border-b border-rule/60 py-[5px]"
          >
            <span
              aria-hidden
              className={
                "w-2 shrink-0 " +
                (entry.failed
                  ? "text-bad"
                  : entry.kind === "model_called"
                    ? "text-accent"
                    : "text-rule-strong")
              }
            >
              {entry.failed ? "×" : entry.kind === "model_called" ? "◆" : "·"}
            </span>
            {entry.detail && (
              <span className="w-[4.5rem] shrink-0 truncate text-[11.5px] text-ink-muted">
                {entry.detail}
              </span>
            )}
            <span
              className={
                "min-w-0 flex-1 truncate " +
                (entry.failed
                  ? "text-bad"
                  : entry.kind === "tool_called"
                    ? "text-ink-secondary"
                    : "text-ink")
              }
              title={entry.text}
            >
              {entry.text}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}
