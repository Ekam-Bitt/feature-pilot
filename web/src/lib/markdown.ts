/**
 * Just enough Markdown for what the summariser writes.
 *
 * Two deliberate limits.
 *
 * It parses to a structure, never to HTML. The PR summary is model output, and
 * the model reads the target repository's own issue text — so treating any of
 * it as markup would run a line from a stranger's README to script execution
 * on this page. The renderer builds React elements from these values, and
 * React escapes text nodes, so a `<script>` in a summary is displayed rather
 * than run.
 *
 * And it covers the shapes that actually appear — paragraphs, headings,
 * numbered and bulleted lists, fenced code, bold, italic, inline code —
 * rather than pretending to be CommonMark. Anything else survives as the text
 * it is, which is the right failure: an unrendered asterisk is a blemish,
 * a swallowed sentence is a lie.
 */

export type Span =
  | { kind: "text"; text: string }
  | { kind: "strong"; text: string }
  | { kind: "em"; text: string }
  | { kind: "code"; text: string };

export type Block =
  | { kind: "paragraph"; text: string }
  | { kind: "heading"; level: number; text: string }
  | { kind: "ordered-list"; items: string[] }
  | { kind: "unordered-list"; items: string[] }
  | { kind: "code"; language: string; text: string };

const HEADING = /^(#{1,6})\s+(.*)$/;
const ORDERED = /^\s*\d+[.)]\s+(.*)$/;
const UNORDERED = /^\s*[-*+]\s+(.*)$/;
const FENCE = /^\s*```(.*)$/;

export function parseBlocks(source: string): Block[] {
  const lines = source.replace(/\r\n?/g, "\n").split("\n");
  const blocks: Block[] = [];

  let paragraph: string[] = [];
  let items: string[] = [];
  let listKind: "ordered-list" | "unordered-list" | null = null;

  const flushParagraph = () => {
    const text = paragraph.join(" ").trim();
    if (text) blocks.push({ kind: "paragraph", text });
    paragraph = [];
  };

  const flushList = () => {
    if (listKind && items.length) blocks.push({ kind: listKind, items });
    items = [];
    listKind = null;
  };

  const flush = () => {
    flushParagraph();
    flushList();
  };

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    const fence = FENCE.exec(line);
    if (fence) {
      flush();
      const language = fence[1].trim();
      const body: string[] = [];
      i++;
      // An unclosed fence runs to the end rather than discarding the rest.
      while (i < lines.length && !FENCE.test(lines[i])) {
        body.push(lines[i]);
        i++;
      }
      blocks.push({ kind: "code", language, text: body.join("\n").replace(/\n+$/, "") });
      continue;
    }

    if (!line.trim()) {
      flush();
      continue;
    }

    const heading = HEADING.exec(line);
    if (heading) {
      flush();
      blocks.push({ kind: "heading", level: heading[1].length, text: heading[2].trim() });
      continue;
    }

    const ordered = ORDERED.exec(line);
    if (ordered) {
      flushParagraph();
      if (listKind !== "ordered-list") flushList();
      listKind = "ordered-list";
      items.push(ordered[1].trim());
      continue;
    }

    const unordered = UNORDERED.exec(line);
    if (unordered) {
      flushParagraph();
      if (listKind !== "unordered-list") flushList();
      listKind = "unordered-list";
      items.push(unordered[1].trim());
      continue;
    }

    // A wrapped continuation of the item above, not a new paragraph.
    if (listKind && items.length) {
      items[items.length - 1] += ` ${line.trim()}`;
      continue;
    }

    paragraph.push(line.trim());
  }

  flush();
  return blocks;
}

//: Code first, so markers inside a span of code are left alone.
const INLINE = /(`[^`]+`|\*\*[^*]+\*\*|\*[^*\n]+\*)/;

export function parseInline(text: string): Span[] {
  const spans: Span[] = [];

  for (const piece of text.split(INLINE)) {
    if (!piece) continue;

    if (piece.startsWith("`") && piece.endsWith("`") && piece.length > 2) {
      spans.push({ kind: "code", text: piece.slice(1, -1) });
    } else if (piece.startsWith("**") && piece.endsWith("**") && piece.length > 4) {
      spans.push({ kind: "strong", text: piece.slice(2, -2) });
    } else if (piece.startsWith("*") && piece.endsWith("*") && piece.length > 2) {
      spans.push({ kind: "em", text: piece.slice(1, -1) });
    } else if (spans.length && spans[spans.length - 1].kind === "text") {
      // Runs of plain text stay one span, so an unmatched marker reads as the
      // character it is rather than splitting the sentence.
      spans[spans.length - 1] = {
        kind: "text",
        text: spans[spans.length - 1].text + piece,
      };
    } else {
      spans.push({ kind: "text", text: piece });
    }
  }

  return spans;
}
