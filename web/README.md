# Feature Pilot — web

Paste a public GitHub issue link; watch the agent plan, patch, test, repair, and
open the pull request.

The interesting claim this project makes is not "it opened a PR" — it is *here
is the reasoning, the tool calls, the failing test, the repair, and then the
PR*. That claim is invisible in a terminal only its author runs, which is what
this exists for.

## Running it

Two halves. From the repository root:

```bash
docker compose up -d --wait     # postgres + redis
uv run fpilot serve             # the API on 127.0.0.1:8080
```

Then here:

```bash
npm install
npm run dev                     # http://localhost:3000
```

`NEXT_PUBLIC_FP_API_URL` names the API and defaults to `http://localhost:8080`,
so a fresh clone runs with no env file.

## Verifying it

```bash
npm run verify   # tsc --noEmit, vitest, next build
npm run smoke    # drives the real UI against a real API, and screenshots it
```

The split is deliberate. Everything with logic lives in one pure reducer —
`src/lib/run.ts`, a stream of events in, the page's state out — because that is
where the awkward cases are: a phase revisited by the repair loop, a gate that
opens twice, events replayed before the stream follows, a payload missing a
field. `vitest` owns those.

`npm run smoke` exists because that is not enough. It found the bug that
mattered most here: the API sends **two different JSON shapes** under the same
SSE event names — recorded `MetricEvent`s (`{kind, payload}`) and events the
stream synthesises itself (`awaiting_human`, `run_finished`) which are bare
objects with no `kind` at all. Reading the kind from the body dropped both
silently: no approval gate, and no diff at the end. Types passed. Unit tests
passed. Only looking at the page showed it.

## Deploying

Vercel, with **root directory `web/`**. Set `NEXT_PUBLIC_FP_API_URL` to the
API's public HTTPS URL, and add the Vercel origin to the API's
`FP_API_CORS_ORIGINS` or the browser will refuse to read the event stream.

The API must be served over HTTPS. A page on `https://` cannot call an
`http://` API, so the host needs a tunnel — see `deploy/README.md`, which walks
through `cloudflared` and the free always-on host it runs on.

## What is not here

- **A runs list.** The API holds run records in memory, so a restart empties
  it, and a history page that is sometimes blank is worse than none.
- **Rejoining a parked run after an API restart.** The graph checkpoints to
  Postgres, but the record the browser polls does not survive; the page says so
  rather than spinning.
- **Dark mode.** The palette is built on warm paper. An automatic inversion
  would leave every ink and rule value unconsidered, so the design commits to
  one look until a dark one is actually designed.
