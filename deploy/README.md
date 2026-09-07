# Deploying Feature Pilot on AWS

One EC2 host runs everything: the API under systemd, Postgres and Redis in
compose, and each run's sandbox on the host's Docker daemon. Models route
through Bedrock using the instance role, so **no API keys are written to the
box**.

## Why a single instance and not Fargate

`featurepilot.sandbox.runner` drives the Docker API directly — it creates a
container per run, `put_archive`s the repository in, and `exec`s inside it.
That needs a real Docker daemon, which Fargate does not provide. An EC2 host
gives the sandbox a daemon and the API a home for the price of one instance.

## Cost, honestly

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
