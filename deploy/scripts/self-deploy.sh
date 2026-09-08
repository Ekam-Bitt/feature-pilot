#!/usr/bin/env bash
# Bring the host to whatever main points at. Run by a systemd timer.
#
# Pull, not push. The host's SSH is open to one address and a CI runner is
# never at it, and the alternative — opening 22 to the internet so a runner can
# reach in — trades a real reduction in exposure for a nicer status badge. So
# the host watches the branch instead, and CI confirms the result over the
# HTTPS it already serves.
#
# Does nothing when main has not moved, so it is cheap to run often.
set -euo pipefail

TARGET="${FP_TARGET:-/opt/featurepilot}"
BRANCH="${FP_BRANCH:-main}"

cd "$TARGET"

git fetch --quiet origin "$BRANCH"
local_sha=$(git rev-parse HEAD)
remote_sha=$(git rev-parse "origin/$BRANCH")

if [ "$local_sha" = "$remote_sha" ]; then
  exit 0
fi

echo "deploying ${local_sha:0:9} -> ${remote_sha:0:9}"

# Reset rather than merge: this is a copy of main, not a place where work
# happens, and an unattended merge conflict has no good outcome. .env is
# gitignored, so credentials survive.
git reset --hard --quiet "origin/$BRANCH"

/usr/local/bin/uv sync --frozen --quiet

# Datastores rarely change, but a compose edit should land without a manual
# step. Idempotent when nothing changed.
docker compose up -d --wait --quiet-pull >/dev/null 2>&1 || true

sudo systemctl restart featurepilot-api

for attempt in $(seq 1 20); do
  if curl -sf --max-time 5 http://127.0.0.1:8080/health >/dev/null; then
    echo "healthy on attempt ${attempt}, now at $(git rev-parse --short HEAD)"
    exit 0
  fi
  sleep 3
done

echo "the API did not become healthy after deploying ${remote_sha:0:9}" >&2
exit 1
