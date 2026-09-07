import type { Totals } from "@/lib/run";

/**
 * A KPI row, not a chart.
 *
 * Four headline numbers whose job is magnitude-at-a-glance; a bar chart of
 * four unrelated measures would be a category error. The values use the
 * font's proportional figures rather than tabular: tabular gives every digit
 * the width of a zero, which reads loose at this size, and these are
 * standalone values rather than a column that must align vertically. The
 * ticking clock in the run header is the exception, and does use tabular.
 */

const compact = (n: number): string =>
  n >= 10_000 ? `${(n / 1000).toFixed(1)}K` : n.toLocaleString();

function Tile({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-w-[7rem] flex-1 px-4 py-3 first:pl-0">
      <div className="text-[11px] uppercase tracking-[0.08em] text-ink-muted">{label}</div>
      <div className="mt-1 text-[19px] font-semibold leading-none text-ink">{value}</div>
    </div>
  );
}

export function StatRow({ totals }: { totals: Totals }) {
  return (
    <div className="flex divide-x divide-rule border-y border-rule">
      <Tile label="Spent" value={`$${totals.costUsd.toFixed(4)}`} />
      <Tile label="Tokens" value={compact(totals.inputTokens + totals.outputTokens)} />
      <Tile label="Model calls" value={String(totals.modelCalls)} />
      <Tile label="Tool calls" value={String(totals.toolCalls)} />
    </div>
  );
}
