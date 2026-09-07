import { RAIL } from "@/lib/types";

/**
 * Where the run has got to.
 *
 * DEBUGGING is not in the rail's fixed list: the repair loop firing is a real
 * event, and drawing the step in advance would promise a failure that may
 * never come. It is inserted after TESTING only once the run enters it.
 */
export function PhaseRail({
  phase,
  entered,
  finished,
}: {
  phase: string;
  entered: string[];
  finished: boolean;
}) {
  const steps: string[] = [...RAIL];
  if (entered.includes("DEBUGGING")) {
    steps.splice(steps.indexOf("TESTING") + 1, 0, "DEBUGGING");
  }
  if (phase === "FAILED") steps[steps.length - 1] = "FAILED";

  return (
    <ol className="flex flex-wrap items-center gap-x-1 gap-y-2 text-[12px]">
      {steps.map((step, i) => {
        const isCurrent = step === phase && !finished;
        const isPast = entered.includes(step) && !isCurrent;
        const isFail = step === "FAILED" && phase === "FAILED";
        return (
          <li key={step} className="flex items-center gap-1">
            {i > 0 && <span className="mr-1 text-rule-strong">·</span>}
            <span
              className={
                isFail
                  ? "font-medium text-bad"
                  : isCurrent
                    ? "pulse font-medium text-accent"
                    : isPast
                      ? "text-ink-secondary"
                      : "text-ink-muted/60"
              }
            >
              {step.toLowerCase()}
            </span>
          </li>
        );
      })}
    </ol>
  );
}
