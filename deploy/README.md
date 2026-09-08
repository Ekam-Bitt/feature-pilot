# Deploying Feature Pilot

Two ways to run this off a laptop:

| | Cost | Good for |
|---|---|---|
| **[GitHub Actions](../.github/workflows/solve.yml)** | free on public repos | one-off runs, reproducible demos |
| **Any free VM + tunnel** ([Oracle Always Free](terraform-oci/)) | free | an always-on API a frontend can call |

Both exist because of one constraint: the sandbox drives the Docker API
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

### Deploying a change

Nothing to run. A systemd timer on the host runs
[`scripts/self-deploy.sh`](scripts/self-deploy.sh), which resets to `main`,
syncs, restarts the API and health-checks it; it exits immediately when `main`
has not moved, so it is cheap to run often. CI then confirms the result over
HTTPS — `/health` reports the commit it is running, and
[`deploy.yml`](../.github/workflows/deploy.yml) polls for it.

Pull, not push: the host's SSH is open to a single address and a GitHub runner
is never at it. The alternative was opening port 22 to the internet so a runner
could reach in, which trades a real reduction in exposure for a nicer status
badge.

### Verifying a deployment

```bash
ssh ubuntu@<instance-ip>
cd /opt/featurepilot && uv run fpilot doctor
# expect: anthropic key ok, docker ok, postgres ok, redis ok, gh ok
uv run fpilot solve https://github.com/<owner>/<repo>/issues/<n> --push --draft
```

### When something is missing

Provisioning failures are silent from outside the box:

```bash
sudo tail -100 /var/log/featurepilot-provision.log   # provisioning
systemctl status featurepilot-api                    # the API
journalctl -u featurepilot-api -n 50                 # its logs
systemctl status featurepilot-deploy.timer           # the self-deploy timer
journalctl -u featurepilot-deploy -n 50              # what it last did
docker ps                                            # datastores + sandboxes
```

### Teardown

```bash
cd deploy/terraform-oci && terraform destroy
```

The instance is Always Free, so there is no billing reason to tear it down —
only a reason to rebuild it, which is one `terraform apply`.
