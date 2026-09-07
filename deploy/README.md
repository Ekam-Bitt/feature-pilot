# Deploying Feature Pilot

Three ways to run this off a laptop, in ascending order of commitment:

| | Cost | Good for |
|---|---|---|
| **[GitHub Actions](../.github/workflows/solve.yml)** | free on public repos | one-off runs, reproducible demos |
| **Any free VM + tunnel** (Oracle Always Free) | free | an always-on API a frontend can call |
| **[AWS EC2](terraform/)** | ~7¢/session, start/stop | Bedrock via instance role, no keys on the box |

All three exist because of one constraint: the sandbox drives the Docker API
directly (`docker.from_env`, `put_archive`, `exec`), so it needs a **real Docker
daemon**. Render, Railway, Heroku and Fargate hand you a managed container
instead, which is why they cannot host this at any price — a capability limit,
not a pricing one.

## Serving a browser frontend

The API already streams everything a UI needs — `node_started`, `tool_called`,
`model_called`, `phase_changed`, `artifact` — over SSE at
`GET /runs/{id}/stream`, replaying history first so a late-joining browser still
sees the whole run.

**While building the frontend you need none of this.** The API allows
`http://localhost:5173` by default, so a local dev server talks to a local
`fpilot serve`. When someone else has to load the page:

```bash
uv run fpilot serve
deploy/scripts/fp-tunnel.sh 8080     # prints an https://*.trycloudflare.com host
```

Before handing that URL to anyone, put the API in public mode — these are the
settings that keep a public box from falling over or emptying your account:

```bash
FP_ALLOW_LOCAL_REPOS=false                       # public issue URLs only
FP_API_CORS_ORIGINS=https://your-frontend.example
FP_MAX_CONCURRENT_RUNS=2                         # each run holds a 2 GB sandbox
FP_MAX_USD_PER_DAY=5                             # ceiling on runs using YOUR key
```

**How visitor credentials work.** A run uses the server's keys by default. If a
visitor supplies their own in the frontend's modal, they are used for that run
only: held in memory, never written to Postgres or `.fp/`, absent from the API's
responses and from every event that leaves the process. Two concurrent runs with
different keys cannot collide, because the key travels on the model instance
rather than through the process environment.

Runs on **your** key are forced to open **draft** pull requests, and count
against `FP_MAX_USD_PER_DAY`. A visitor using their own token publishes under
their own identity and is not capped by your budget.

## An always-free VM (Oracle)

Oracle's Always Free tier gives an ARM instance (2 OCPU / 12 GB as of 2026) at no
cost indefinitely — more than enough, and the only genuinely free always-on host
with a real Docker daemon. Create the instance, then on the box:

```bash
curl -fsSL <raw url>/deploy/scripts/provision-vm.sh | bash -s -- <your repo clone url>
```

It installs Docker, compose, gh and uv, clones the project, and prints the
remaining steps. Two Oracle-specific warnings: A1 capacity is often exhausted in
US regions (EU/APAC provision reliably), and their images ship a restrictive
iptables policy that can silently break container networking — the script checks
for it.

## AWS EC2

One host runs everything: the API under systemd, Postgres and Redis in compose,
and each run's sandbox on the host's Docker daemon. Models route through Bedrock
using the instance role, so **no API keys are written to the box**.

### Cost, honestly

This account is on the post-2025 **paid** plan: there is no free EC2 tier. The
deployment is therefore **start/stop**, not always-on.

| | |
|---|---|
| t4g.medium running | ~$0.034/hr → **~7¢ for a two-hour session** |
| 30 GB gp3, while stopped | ~$2.40/month |
| After `terraform destroy` | $0 (rebuild is one `apply`) |
| Bedrock tokens | dominates the above in real use |

An always-on host is ~$25/month and would exhaust a small credit balance in
weeks. `fp-up.sh` / `fp-down.sh` bracket a working session instead.

## One-time setup

**1. Secrets into SSM.** These live outside Terraform on purpose: they survive
`destroy`/`apply` cycles and never enter terraform state.

```bash
aws ssm put-parameter --name /featurepilot/github_token \
  --type SecureString --value "ghp_..." --region us-east-1

# Optional. Omit for a pure-Bedrock host (the instance role covers models).
aws ssm put-parameter --name /featurepilot/anthropic_api_key \
  --type SecureString --value "sk-ant-..." --region us-east-1
```

**2. Bedrock model access.** Once per account, before any model call works:
submit the Anthropic use-case form (AWS Console → Bedrock → Model access) and
enable the Claude models. Propagation takes ~15 minutes.

**3. Apply.**

```bash
cd deploy/terraform
terraform init
terraform apply \
  -var "my_ip_cidr=$(curl -s https://checkip.amazonaws.com)/32" \
  -var "key_name=<your-ec2-key-pair>"
```

First boot installs docker, gh, uv, and the project, then starts the API —
allow a few minutes.

## Daily use

```bash
deploy/scripts/fp-up.sh      # start; prints the tunnel command
ssh -N -L 8080:localhost:8080 ec2-user@<ip> &
curl localhost:8080/health
deploy/scripts/fp-down.sh    # stop billing
```

The API binds to `127.0.0.1` and **no security-group rule opens 8080** — it has
no authentication of its own, so an SSH tunnel is the access path. Only port 22,
from the single address in `my_ip_cidr`, is reachable.

## Verifying a deployment

```bash
ssh ec2-user@<ip>
cd /opt/featurepilot && uv run fpilot doctor
# expect: docker ok, postgres ok, redis ok, bedrock ok, gh ok
uv run fpilot solve https://github.com/<owner>/<repo>/issues/<n> --push --draft
```

## When something is missing

user-data failures are silent from outside the box:

```bash
sudo tail -100 /var/log/cloud-init-output.log   # provisioning
systemctl status featurepilot-api               # the API
journalctl -u featurepilot-api -n 50            # its logs
docker ps                                       # datastores + sandboxes
```

`user_data_replace_on_change = true` means editing the template and re-applying
rebuilds the instance rather than leaving it half-provisioned.

## Teardown

```bash
deploy/scripts/fp-down.sh          # stop, keep the disk
cd deploy/terraform && terraform destroy   # delete everything (SSM secrets stay)
```
