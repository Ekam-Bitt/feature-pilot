import { describe, expect, it } from "vitest";
import { parseBlocks, parseInline } from "@/lib/markdown";

describe("blocks", () => {
  it("splits paragraphs on blank lines", () => {
    const blocks = parseBlocks("First one.\n\nSecond one.");
    expect(blocks.map((b) => b.kind)).toEqual(["paragraph", "paragraph"]);
  });

  it("joins a wrapped paragraph into one", () => {
    const [block] = parseBlocks("a sentence that the model\nwrapped across lines");
    expect(block).toEqual({
      kind: "paragraph",
      text: "a sentence that the model wrapped across lines",
    });
  });

  it("reads a numbered list, which is what the summariser mostly writes", () => {
    const [block] = parseBlocks(
      "1. **Priority is respected**: higher values first\n2. **Deterministic**: ordered by name",
    );
    expect(block.kind).toBe("ordered-list");
    expect(block.kind === "ordered-list" && block.items).toHaveLength(2);
  });

  it("reads a bulleted list", () => {
    const [block] = parseBlocks("- one\n- two\n* three");
    expect(block.kind).toBe("unordered-list");
    expect(block.kind === "unordered-list" && block.items).toHaveLength(3);
  });

  it("keeps a fenced code block verbatim, including its blank lines", () => {
    const [block] = parseBlocks("```python\ndef f():\n\n    return 1\n```");
    expect(block).toEqual({
      kind: "code",
      language: "python",
      text: "def f():\n\n    return 1",
    });
  });

  it("does not treat a hash inside a fence as a heading", () => {
    const [block] = parseBlocks("```\n# not a heading\n```");
    expect(block.kind).toBe("code");
  });

  it("reads headings and their level", () => {
    const blocks = parseBlocks("## Test plan\n\n### Details");
    expect(blocks).toEqual([
      { kind: "heading", level: 2, text: "Test plan" },
      { kind: "heading", level: 3, text: "Details" },
    ]);
  });

  it("survives an unclosed fence rather than losing the rest", () => {
    const [block] = parseBlocks("```\nstill code");
    expect(block).toEqual({ kind: "code", language: "", text: "still code" });
  });

  it("returns nothing for nothing", () => {
    expect(parseBlocks("")).toEqual([]);
    expect(parseBlocks("   \n\n  ")).toEqual([]);
  });
});

describe("inline", () => {
  it("leaves plain text alone", () => {
    expect(parseInline("just words")).toEqual([{ kind: "text", text: "just words" }]);
  });

  it("reads bold", () => {
    expect(parseInline("a **bold** word")).toEqual([
      { kind: "text", text: "a " },
      { kind: "strong", text: "bold" },
      { kind: "text", text: " word" },
    ]);
  });

  it("reads inline code, which is most of what these summaries contain", () => {
    expect(parseInline("the `order()` method")).toEqual([
      { kind: "text", text: "the " },
      { kind: "code", text: "order()" },
      { kind: "text", text: " method" },
    ]);
  });

  it("does not read markup inside code", () => {
    expect(parseInline("`(-priority, name)`")).toEqual([
      { kind: "code", text: "(-priority, name)" },
    ]);
  });

  it("reads italics without mistaking them for bold", () => {
    expect(parseInline("*emphasis* and **strength**")).toEqual([
      { kind: "em", text: "emphasis" },
      { kind: "text", text: " and " },
      { kind: "strong", text: "strength" },
    ]);
  });

  it("leaves an unmatched marker as the character it is", () => {
    expect(parseInline("2 * 3 = 6")).toEqual([{ kind: "text", text: "2 * 3 = 6" }]);
  });

  it("never yields html, whatever it is given", () => {
    /**
     * The summary is model output, and a repository's own issue text feeds the
     * model. Treating any of it as markup would be a route from a stranger's
     * README to script execution on this page, so the renderer emits React
     * elements and nothing else — this asserts the parser keeps tags as text.
     */
    const spans = parseInline("<img src=x onerror=alert(1)> and <b>bold</b>");
    expect(spans.every((s) => s.kind === "text")).toBe(true);
    expect(spans.map((s) => s.text).join("")).toBe(
      "<img src=x onerror=alert(1)> and <b>bold</b>",
    );
  });
});
