# Feature Pilot on the web — design

**Status:** approved 2026-09-08.

## Why

The agent already does the whole job — a public issue URL in, a pull request
out — and the HTTP API already streams every step over SSE. What is missing is
somewhere to *watch* it. The interesting claim this project makes is not "it
opened a PR", it is "here is the reasoning, the tools, the failing test, the
repair, and then the PR" — and that claim is invisible in a terminal that only
the author ever runs.

So: a page where anyone pastes an issue link and watches the gears turn, the
way Claude Code makes its own work legible.

## Shape

A Next.js app in `web/`, deployed on Vercel with root directory `web/`. Kept in
this repository because the interesting artifact is the whole system, and a
split repository would make the frontend look like a separate toy.

- Next.js App Router, TypeScript `strict`, Tailwind. No component library:
  every element here is bespoke, and a library would be 40 kB of unused
  abstractions plus a design language that is not ours.
- `NEXT_PUBLIC_FP_API_URL` names the API — `http://localhost:8080` in
  development, the cloudflared HTTPS hostname in production. A page served over
  HTTPS cannot call a plain-HTTP API, so the tunnel is not optional in
  production; it is the only path a browser will take.

### Aesthetic: editorial light

Off-white ground, near-black text, one restrained accent. Generous whitespace,
a grotesque for prose and a monospace for anything the machine produced —
events, diffs, test output. Thin rules rather than boxes and shadows. The page
should read like a studio's writing that happens to contain a live process,
not like a dashboard.

The reason is fit, not taste: the content is dense, mechanical, and fast-moving,
and it is far easier to read dense mono against calm paper than against another
dark surface competing for attention.

## Screens

Two. A runs list is deliberately absent — `RunManager` holds records in memory,
so a restart empties it, and a history page that is sometimes blank is worse
than no history page.

### 1. Home

Masthead, one sentence of what this is, and a single wide input for the issue
URL. Beneath it, quietly:

- **Approve the plan interactively** — default **on**. The approval gate is the
  moment that distinguishes this from a black box, so the default should show
  it rather than skip it.
- **Open the pull request as a draft** — default on.
- **Use your own keys** — opens a modal for an Anthropic key and a GitHub
  token. Held in `sessionStorage`, sent once in the `POST /runs` body. This
  mirrors the backend contract exactly: a session's keys override the server's
  for that run only, and are never persisted or echoed back.

Submitting posts to `/runs` and routes to `/runs/[id]`.

### 2. Run view

Four zones, in the order a person actually asks the questions:

1. **Header** — the issue reference, linked to GitHub; elapsed time; a status
   word. *What am I looking at?*
2. **Phase rail** — `PLANNING → CODING → TESTING → REVIEW → DONE`, the current
   one alive. `DEBUGGING` appears only if the run enters it, because the repair
   loop firing is a real event and pre-drawing it would be a lie.
   *Where has it got to?*
3. **The gears** — a live feed from `EventSource(/runs/{id}/stream)`: tool calls
   as monospace one-liners, model calls with role and cost, node boundaries with
   latency, and running totals accumulated client-side. *What is it doing?*
4. **The payoff** — on `DONE`: the diff, the PR summary, and a publish button
   that ends in a real pull request URL. *What did it produce?*

**The approval gate.** When `awaiting_human` arrives, a card renders the plan —
summary, steps, and any open questions with fields to answer them — offering
Approve or Request changes, posting to `/runs/{id}/approve`. The stream
continues through the gate, so the page never reloads.

**Failure is content, not an error state.** A `FAILED` run keeps its feed and
shows why. Watching a repair attempt fail is part of the story.

## One backend change this design requires

The SSE stream redacts `diff` and file contents from everything leaving the
process — deliberately, since Redis and LangSmith are downstream — and
`GET /runs/{id}` never carried them. So a browser presently **cannot obtain the
patch at all**, which makes zone 4 impossible.

Add `GET /runs/{id}/artifacts` returning `{diff, pr_summary, test_summary}` from
the run record, which already caches exactly these for publishing. It is a
deliberate, single, authenticated-by-obscurity-free hole in the redaction
policy: the diff is the product, and the person who started the run is the
person being shown it.

Also: the CORS default gains `localhost:3000`, because Next's dev server is not
Vite's `5173`.

## Testing

The part with logic is a pure reducer: a stream of `MetricEvent`s in, UI state
out — phases entered, feed entries, running totals, whether a gate is open.
That is TDD'd with vitest, and it is where every ordering and edge case lives
(events arriving out of order, replay before follow, a gate opening twice).

Components get `tsc --noEmit` and a production build. The real verification is
a live run against the Oracle host, watched in a browser.

## Out of scope, on purpose

- Runs history — needs run persistence first.
- Authentication — the deployment's protection is the spend ceiling, the
  concurrency cap, and forced draft PRs under the server's identity.
- Streaming the diff as it is written — the sandbox produces it at the end.

## Known gaps

- Closing the tab before approving parks the run server-side. It is resumable
  from the CLI, but the page cannot rejoin it, and there is no UI for that.
- A restart of the API loses the run record, so an open run view will start
  reporting "unknown run" rather than reconnecting.
