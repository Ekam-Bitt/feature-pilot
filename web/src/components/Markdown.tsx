import { parseBlocks, parseInline, type Span } from "@/lib/markdown";

/**
 * Renders the summariser's Markdown.
 *
 * Builds React elements from parsed values — never `dangerouslySetInnerHTML`.
 * The summary is model output and the model reads the target repository's own
 * text, so a route from a stranger's README to script execution on this page
 * is exactly what must not exist. React escapes text nodes, so a `<script>` in
 * a summary is shown, not run.
 */

function Inline({ text }: { text: string }) {
  return (
    <>
      {parseInline(text).map((span: Span, i) => {
        switch (span.kind) {
          case "strong":
            return (
              <strong key={i} className="font-semibold text-ink">
                {span.text}
              </strong>
            );
          case "em":
            return (
              <em key={i} className="italic">
                {span.text}
              </em>
            );
          case "code":
            return (
              <code
                key={i}
                className="rounded bg-paper-sunk px-1 py-px font-mono text-[0.9em] text-ink"
              >
                {span.text}
              </code>
            );
          default:
            return <span key={i}>{span.text}</span>;
        }
      })}
    </>
  );
}

export function Markdown({ source }: { source: string }) {
  return (
    <div className="space-y-4">
      {parseBlocks(source).map((block, i) => {
        switch (block.kind) {
          case "heading":
            return (
              <h4
                key={i}
                className={
                  block.level <= 2
                    ? "pt-2 text-[15px] font-semibold tracking-tight text-ink"
                    : "pt-1 text-[13.5px] font-semibold text-ink"
                }
              >
                <Inline text={block.text} />
              </h4>
            );

          case "ordered-list":
            return (
              <ol key={i} className="space-y-2">
                {block.items.map((item, j) => (
                  <li key={j} className="flex gap-3 text-[14px] leading-relaxed">
                    <span className="shrink-0 font-mono text-[12px] text-ink-muted">
                      {j + 1}
                    </span>
                    <span className="text-ink-secondary">
                      <Inline text={item} />
                    </span>
                  </li>
                ))}
              </ol>
            );

          case "unordered-list":
            return (
              <ul key={i} className="space-y-2">
                {block.items.map((item, j) => (
                  <li key={j} className="flex gap-3 text-[14px] leading-relaxed">
                    <span aria-hidden className="shrink-0 text-rule-strong">
                      —
                    </span>
                    <span className="text-ink-secondary">
                      <Inline text={item} />
                    </span>
                  </li>
                ))}
              </ul>
            );

          case "code":
            return (
              <pre
                key={i}
                className="overflow-x-auto border border-rule bg-paper-sunk/60 p-3 font-mono text-[12px] leading-relaxed text-ink-secondary"
              >
                {block.text}
              </pre>
            );

          default:
            return (
              <p key={i} className="text-[14px] leading-relaxed text-ink-secondary">
                <Inline text={block.text} />
              </p>
            );
        }
      })}
    </div>
  );
}
