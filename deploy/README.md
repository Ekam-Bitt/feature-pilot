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

## An always-free VM (Oracle) — the recommended host

Oracle's Always Free tier gives an ARM instance at no cost indefinitely. It is
the only genuinely free always-on host with a real Docker daemon, and it runs
everything built here with no code changes: the same API, the same SSE stream,
the same per-run container sandbox.

**Why this and not a container platform.** Render, Railway, Koyeb, Fly and
Hugging Face Spaces all hand you a managed container with no daemon inside it,
and Docker-in-Docker needs privileges none of them grant. That rules them out
for the API at any price. They remain fine for hosting the *frontend*.

### Creating the instance

In the Oracle Cloud console → Compute → Instances → Create:

| Field | Value | Why |
|---|---|---|
| Shape | **VM.Standard.A1.Flex**, 2 OCPU / 12 GB | The Always Free ARM shape. Halved from 4/24 in 2026; 12 GB is still ample. |
| Image | Ubuntu 22.04 or 24.04 (ARM) | Oracle Linux works too — the script handles both. |
| Boot volume | 50 GB or more | The sandbox image is ~390 MB and each run clones a repository. |
| SSH key | your public key | Oracle does not offer password login. |

Two things that actually bite:

- **"Out of host capacity."** A1 capacity is frequently exhausted in US regions;
  EU and APAC (Frankfurt, Singapore, Tokyo) usually provision within minutes.
  Capacity is per-region and your home region is fixed at signup, so choose it
  with this in mind. Retrying the same request later does eventually succeed.
- **Signup wants a card** for identity verification. Always Free resources are
  not charged against it, but the check is unavoidable.

### Provisioning it

```bash
ssh ubuntu@<instance-ip>
curl -fsSL https://raw.githubusercontent.com/Ekam-Bitt/feature-pilot/main/deploy/scripts/provision-vm.sh \
  | bash -s -- https://github.com/Ekam-Bitt/feature-pilot.git main
```

It installs Docker, compose, gh, uv and cloudflared, adds swap, fixes the
iptables policy described below, clones the project, brings up the datastores,
installs `featurepilot-api` as a systemd service, and prints what is left to do.
It is safe to re-run — every step checks for its own result first.

Then, on the box: put your key in `.env`, `gh auth login`,
`sudo systemctl start featurepilot-api`, and confirm with `uv run fpilot doctor`
that every row reads ok.

### The one Oracle-specific trap

Oracle's images persist a default-REJECT `INPUT` policy that sits **above** the
chains Docker inserts. Containers start, and then nothing resolves — DNS fails
inside the sandbox and a run dies at the dependency install with an error that
points nowhere near the cause. The provisioning script reorders the rules; if
you hit it anyway, `sudo iptables -L INPUT -n` shows the REJECT sitting too
early.

Nothing inbound needs opening beyond SSH: the API is reached through an
outbound tunnel, so no Oracle security list rule and no dependence on the
instance keeping its public IP.

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
