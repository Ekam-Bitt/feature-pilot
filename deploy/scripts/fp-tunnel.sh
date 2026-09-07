#!/usr/bin/env bash
# Publish a locally-running API on an HTTPS hostname, for a frontend that other
# people can open.
#
# You do not need this to build the frontend: the API allows http://localhost:5173
# by default, so a local dev server talks to a local API with no tunnel at all.
# Reach for this when someone else has to load the page.
#
# A quick tunnel needs no Cloudflare account and gives a random
# *.trycloudflare.com hostname that lasts as long as this process. It also
# means anyone with the URL can start runs, so read the note below first.
set -euo pipefail

PORT="${1:-8080}"

command -v cloudflared >/dev/null || {
  echo "cloudflared not found: brew install cloudflared" >&2
  exit 1
}

curl -sf "http://127.0.0.1:${PORT}/health" >/dev/null || {
  echo "nothing healthy on port ${PORT} — start the API first: uv run fpilot serve" >&2
  exit 1
}

cat <<'NOTE'
Before you hand the URL to anyone, the API should be in public mode:

  FP_ALLOW_LOCAL_REPOS=false   # only public issue URLs, never a server path
  FP_MAX_USD_PER_DAY=5         # ceiling on runs that use YOUR key
  FP_MAX_CONCURRENT_RUNS=2     # each run holds a 2 GB sandbox
  FP_API_CORS_ORIGINS=https://your-frontend.pages.dev

The hostname below changes every time this process restarts. Put it in the
frontend's API base URL, and add the frontend's own origin to
FP_API_CORS_ORIGINS or the browser will refuse to read the event stream.
NOTE

exec cloudflared tunnel --url "http://127.0.0.1:${PORT}"
