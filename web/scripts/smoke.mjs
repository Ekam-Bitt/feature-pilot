/**
 * Drives the real UI against a real API and screenshots what it drew.
 *
 * `vitest` covers the reducer, `tsc` covers the types, and neither would have
 * caught what this did: the stream sends two different JSON shapes under the
 * same SSE names, so the approval gate silently never rendered. Looking at the
 * page is the only check that finds that class of bug.
 *
 * Needs both halves running:
 *   docker compose up -d --wait && uv run fpilot serve     (repository root)
 *   npm run dev                                            (here)
 *
 * Usage: node scripts/smoke.mjs [issue-url] [out-dir]
 */
import { chromium } from "playwright";
import { mkdir } from "node:fs/promises";

const ISSUE =
  process.argv[2] ?? "https://github.com/Ekam-Bitt/featurepilot-fixture/issues/1";
const OUT = process.argv[3] ?? "./smoke";
const UI = process.env.UI_URL ?? "http://localhost:3000";

await mkdir(OUT, { recursive: true });

const browser = await chromium.launch({ args: ["--no-sandbox"] });
const page = await browser.newPage({ viewport: { width: 1180, height: 1100 } });

const problems = [];
page.on("console", (m) => m.type() === "error" && problems.push(m.text()));
page.on("pageerror", (e) => problems.push(String(e)));

const step = (msg) => console.log(`· ${msg}`);

try {
  await page.goto(UI, { waitUntil: "networkidle" });
  await page.getByText("Watch it open the pull request").waitFor();
  await page.screenshot({ path: `${OUT}/1-home.png`, fullPage: true });
  step("home");

  await page.fill("#issue", ISSUE);
  await page.getByRole("button", { name: /solve it/i }).click();
  await page.waitForURL(/\/runs\/[0-9a-f]+/, { timeout: 20_000 });
  step(`run ${page.url()}`);

  // The approval gate is the point of the interactive default.
  await page.getByText("Your approval").waitFor({ timeout: 240_000 });
  await page.screenshot({ path: `${OUT}/2-gate.png`, fullPage: true });
  step("approval gate");
  await page.getByRole("button", { name: /approve and continue/i }).click();

  await page.getByText("What it wrote").waitFor({ timeout: 360_000 });
  await page.waitForTimeout(1_000);
  await page.screenshot({ path: `${OUT}/3-done.png`, fullPage: true });
  step("finished with a diff");
} finally {
  console.log(problems.length ? `console errors: ${problems.join(" | ")}` : "no console errors");
  await browser.close();
}
