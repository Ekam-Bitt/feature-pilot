#!/usr/bin/env bash
# Provision a fresh VM to run Feature Pilot, including the API as a service.
#
# Written for Oracle Cloud's Always Free ARM instance (2 OCPU / 12 GB, free
# indefinitely), which is the cheapest host that can actually run this: the
# sandbox drives the Docker API directly, so it needs a real daemon, and no
# free container platform provides one.
#
# Safe to re-run: every step checks for its own result first.
#
# Usage, on the box:
#   curl -fsSL <raw-url>/deploy/scripts/provision-vm.sh | bash -s -- \
#     https://github.com/Ekam-Bitt/feature-pilot.git [branch] [target-dir]
set -euo pipefail

REPO_URL="${1:?usage: provision-vm.sh <repo clone url> [branch] [target dir]}"
BRANCH="${2:-main}"
TARGET="${3:-/opt/featurepilot}"
# Whose service this becomes. cloud-init runs as root, where SUDO_USER is
# either unset or root, so an explicit override is the only reliable signal —
# without it the API would be installed as a root service on a fresh boot.
RUN_USER="${FP_RUN_USER:-${SUDO_USER:-$USER}}"

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

# --- packages ---------------------------------------------------------------
# Oracle offers both Ubuntu and Oracle Linux for the ARM shape; handle either
# rather than making the image choice load-bearing.
if command -v apt-get >/dev/null; then
  log "installing packages (apt)"
  sudo apt-get update -y
  sudo apt-get install -y docker.io docker-compose-v2 git curl jq
  if ! command -v gh >/dev/null; then
    sudo mkdir -p /etc/apt/keyrings
    curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg |
      sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
    sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" |
      sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
    sudo apt-get update -y && sudo apt-get install -y gh
  fi
else
  log "installing packages (dnf)"
  sudo dnf install -y docker git curl jq
  if ! command -v gh >/dev/null; then
    sudo dnf config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo
    sudo dnf install -y gh
  fi
  if [ ! -f /usr/local/lib/docker/cli-plugins/docker-compose ]; then
    sudo mkdir -p /usr/local/lib/docker/cli-plugins
    sudo curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose \
      "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)"
    sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
  fi
fi

sudo systemctl enable --now docker
sudo usermod -aG docker "$RUN_USER"

# --- Oracle's firewall ------------------------------------------------------
# Oracle's images ship a default-REJECT INPUT policy persisted by iptables,
# which sits *above* the chains Docker adds and silently breaks container
# networking — containers start, then nothing resolves. Docker's rules have to
# come first. Nothing inbound needs opening: the API is reached through an
# outbound tunnel, and SSH is already permitted.
if sudo iptables -S INPUT 2>/dev/null | grep -q -- "-j REJECT"; then
  log "moving Docker's iptables rules above Oracle's REJECT policy"
  sudo iptables -D INPUT -j REJECT --reject-with icmp-host-prohibited 2>/dev/null || true
  sudo iptables -A INPUT -j REJECT --reject-with icmp-host-prohibited 2>/dev/null || true
  if command -v netfilter-persistent >/dev/null; then
    sudo netfilter-persistent save
  fi
fi

# --- swap -------------------------------------------------------------------
# 12 GB is comfortable, but a run holds a 2 GB sandbox while Postgres, Redis
# and a dependency install are all resident. Swap turns a rare spike into slow
# rather than an OOM kill mid-run.
if ! sudo swapon --show | grep -q /swapfile; then
  log "adding 2G swap"
  sudo fallocate -l 2G /swapfile
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

# --- uv ---------------------------------------------------------------------
# System-wide so the systemd unit and an interactive shell share one binary.
if [ ! -x /usr/local/bin/uv ]; then
  log "installing uv"
  curl -fsSL https://astral.sh/uv/install.sh | sudo env UV_INSTALL_DIR=/usr/local/bin sh
fi

# --- project ----------------------------------------------------------------
log "fetching $REPO_URL ($BRANCH)"
if [ -d "$TARGET/.git" ]; then
  sudo git -C "$TARGET" fetch --all --quiet
  sudo git -C "$TARGET" checkout "$BRANCH" --quiet
  sudo git -C "$TARGET" pull --quiet
else
  sudo git clone --branch "$BRANCH" "$REPO_URL" "$TARGET"
fi
sudo chown -R "$RUN_USER:$RUN_USER" "$TARGET"

cd "$TARGET"
log "syncing dependencies"
/usr/local/bin/uv sync

if [ ! -f .env ]; then
  cp .env.example .env
  log ".env created from the example — it still needs ANTHROPIC_API_KEY"
fi

log "starting datastores"
sudo -u "$RUN_USER" docker compose up -d --wait || {
  echo "compose failed. If containers cannot reach the network, it is almost" >&2
  echo "always the iptables policy above: sudo iptables -L INPUT -n" >&2
  exit 1
}

# --- the API as a service ---------------------------------------------------
log "installing the featurepilot-api service"
sudo tee /etc/systemd/system/featurepilot-api.service >/dev/null <<UNIT
[Unit]
Description=Feature Pilot API
After=docker.service network-online.target
Requires=docker.service

[Service]
Type=simple
User=$RUN_USER
WorkingDirectory=$TARGET
EnvironmentFile=$TARGET/.env
ExecStart=/usr/local/bin/uv run fpilot serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable featurepilot-api

# --- cloudflared ------------------------------------------------------------
# The public path in and out. Outbound-only, so no Oracle security list rule
# and no dependence on the instance's public IP staying the same.
if ! command -v cloudflared >/dev/null; then
  log "installing cloudflared"
  ARCH=$(uname -m); case "$ARCH" in aarch64) CF=arm64 ;; *) CF=amd64 ;; esac
  sudo curl -fsSL -o /usr/local/bin/cloudflared \
    "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-${CF}"
  sudo chmod +x /usr/local/bin/cloudflared
fi

cat <<NEXT

$(log "provisioned")

Docker group membership needs a new login to take effect:  exit, then ssh back in.

Remaining steps:

  1. Credentials and public mode:
       cd $TARGET && nano .env
         ANTHROPIC_API_KEY=sk-ant-...
         FP_ALLOW_LOCAL_REPOS=false
         FP_API_CORS_ORIGINS=https://your-frontend.pages.dev
         FP_MAX_CONCURRENT_RUNS=2
         FP_MAX_USD_PER_DAY=5
  2. gh auth login                      # so publishing works
  3. sudo systemctl start featurepilot-api
  4. /usr/local/bin/uv run fpilot doctor   # every row should read ok
  5. Publish it:
       cloudflared tunnel --url http://127.0.0.1:8080
     Put the printed hostname in the frontend, and that hostname's origin in
     FP_API_CORS_ORIGINS. For a URL that survives restarts, use a named tunnel
     (needs a domain on Cloudflare) or Tailscale Funnel (free, stable *.ts.net).
NEXT
