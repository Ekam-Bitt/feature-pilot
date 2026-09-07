/**
 * The patch, coloured by line kind.
 *
 * Not a syntax highlighter: what matters in a diff is which lines moved, and
 * a language-aware highlighter would spend a dependency to make that harder to
 * see. Green and red carry direction; the file headers carry structure.
 */
export function Diff({ diff }: { diff: string }) {
  const lines = diff.split("\n");
  return (
    <div className="overflow-x-auto border border-rule bg-paper-sunk/60">
      <pre className="min-w-full py-2 font-mono text-[12px] leading-[1.55]">
        {lines.map((line, i) => {
          const kind = line.startsWith("+++") || line.startsWith("---")
            ? "meta"
            : line.startsWith("diff --git") || line.startsWith("index ")
              ? "meta"
              : line.startsWith("@@")
                ? "hunk"
                : line.startsWith("+")
                  ? "add"
                  : line.startsWith("-")
                    ? "del"
                    : "ctx";
          return (
            <div
              key={i}
              className={
                "px-3 " +
                (kind === "add"
                  ? "bg-good/[0.07] text-good"
                  : kind === "del"
                    ? "bg-bad/[0.06] text-bad"
                    : kind === "hunk"
                      ? "text-accent"
                      : kind === "meta"
                        ? "text-ink-muted"
                        : "text-ink-secondary")
              }
            >
              {line || " "}
            </div>
          );
        })}
      </pre>
    </div>
  );
}
