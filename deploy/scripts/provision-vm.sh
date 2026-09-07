#!/usr/bin/env bash
# Provision any fresh ARM or x86 Ubuntu / Amazon Linux VM to run Feature Pilot.
#
# Written for Oracle Cloud's Always Free ARM instance (2 OCPU / 12 GB, free
# indefinitely), which is the cheapest host that can actually run this: the
# sandbox drives the Docker API directly, so it needs a real daemon, which
# managed container platforms do not provide at any price.
#
# Run it ON the box, as a user with sudo:
#   curl -fsSL <raw url to this file> | bash -s -- https://github.com/you/feature-pilot.git
set -xeuo pipefail

REPO_URL="${1:?usage: provision-vm.sh <repo clone url>}"
TARGET="${2:-/opt/featurepilot}"

if command -v apt-get >/dev/null; then
  sudo apt-get update -y
  sudo apt-get install -y docker.io docker-compose-v2 git curl
  sudo systemctl enable --now docker
  # gh is not in Ubuntu's default repositories.
  sudo mkdir -p /etc/apt/keyrings
  curl -fsSL https://cli.github.com/packages/githubcli-archive-keyring.gpg |
    sudo tee /etc/apt/keyrings/githubcli-archive-keyring.gpg >/dev/null
  sudo chmod go+r /etc/apt/keyrings/githubcli-archive-keyring.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/githubcli-archive-keyring.gpg] https://cli.github.com/packages stable main" |
    sudo tee /etc/apt/sources.list.d/github-cli.list >/dev/null
  sudo apt-get update -y && sudo apt-get install -y gh
else
  sudo dnf install -y docker git
  sudo systemctl enable --now docker
  sudo dnf config-manager --add-repo https://cli.github.com/packages/rpm/gh-cli.repo
  sudo dnf install -y gh
  sudo mkdir -p /usr/local/lib/docker/cli-plugins
  ARCH=$(uname -m); [ "$ARCH" = "aarch64" ] && ARCH=aarch64 || ARCH=x86_64
  sudo curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose \
    "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-${ARCH}"
  sudo chmod +x /usr/local/lib/docker/cli-plugins/docker-compose
fi

sudo usermod -aG docker "$USER"

curl -fsSL https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sudo -E sh

sudo git clone "$REPO_URL" "$TARGET" 2>/dev/null || sudo git -C "$TARGET" pull
sudo chown -R "$USER:$USER" "$TARGET"
cd "$TARGET"
uv sync

# Oracle's images ship a default-DROP iptables policy that silently breaks
# container networking; Docker's own rules go before it or nothing resolves.
if command -v iptables >/dev/null && sudo iptables -L INPUT | grep -q "REJECT.*all"; then
  echo "NOTE: this image has a restrictive iptables policy. If containers cannot"
  echo "reach the network, flush the INPUT REJECT rule or move Docker's rules above it."
fi

cat <<'NEXT'

Provisioned. Remaining steps, in order:

  1. cp .env.example .env    and set ANTHROPIC_API_KEY (or Bedrock model ids)
  2. gh auth login           so publishing works with your identity
  3. Public mode, if this will be reachable by other people:
       FP_ALLOW_LOCAL_REPOS=false
       FP_API_CORS_ORIGINS=https://your-frontend.example
       FP_MAX_USD_PER_DAY=5
  4. docker compose up -d --wait
  5. uv run fpilot doctor    every row should read ok
  6. uv run fpilot serve     (or install the systemd unit in deploy/README.md)
NEXT
