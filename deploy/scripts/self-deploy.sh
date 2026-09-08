#!/usr/bin/env bash
# Bring the host to whatever main points at. Run by a systemd timer.
#
# Pull, not push. The host's SSH is open to one address and a CI runner is
# never at it, and the alternative — opening 22 to the internet so a runner can
# reach in — trades a real reduction in exposure for a nicer status badge. So
# the host watches the branch instead, and CI confirms the result over the
# HTTPS it already serves.
#
# Does nothing when the API is already serving main's commit, so it is cheap
# to run often.
set -euo pipefail

TARGET="${FP_TARGET:-/opt/featurepilot}"
BRANCH="${FP_BRANCH:-main}"
UV="${FP_UV:-/usr/local/bin/uv}"

cd "$TARGET"

# The commit the process is actually serving, or nothing if it is not up.
#
# Not `git rev-parse HEAD`. HEAD advances the moment `git reset` runs, and if
# anything between that and the restart fails — a dependency sync, say — the
# checkout says "deployed" while the process still runs the old code, and a
# gate that read HEAD would never try again. /health reports the commit the
# process loaded for exactly this reason, and it cannot change without a
# restart. An API that is not answering is, by the same measure, not on main.
running_commit() {
  curl -sf --max-time 5 http://127.0.0.1:8080/health | jq -r '.commit // empty' 2>/dev/null || true
}

git fetch --quiet origin "$BRANCH"
remote_sha=$(git rev-parse "origin/$BRANCH")
running_sha=$(running_commit)

if [ "$running_sha" = "$remote_sha" ]; then
  exit 0
fi

from=${running_sha:0:9}
echo "deploying ${from:-nothing} -> ${remote_sha:0:9}"

# Reset rather than merge: this is a copy of main, not a place where work
# happens, and an unattended merge conflict has no good outcome. .env is
# gitignored, so credentials survive.
git reset --hard --quiet "origin/$BRANCH"

"$UV" sync --frozen --quiet

# Datastores rarely change, but a compose edit should land without a manual
# step. Idempotent when nothing changed.
docker compose up -d --wait --quiet-pull >/dev/null 2>&1 || true

sudo systemctl restart featurepilot-api

# Up is not enough; it has to be up on the commit we just deployed.
for attempt in $(seq 1 20); do
  if [ "$(running_commit)" = "$remote_sha" ]; then
    echo "healthy on attempt ${attempt}, now at ${remote_sha:0:9}"
    exit 0
  fi
  sleep 3
done

echo "the API did not come up on ${remote_sha:0:9}" >&2
exit 1
