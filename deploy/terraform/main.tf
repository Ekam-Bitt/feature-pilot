# One EC2 host running the whole stack: the API on the host via systemd,
# Postgres and Redis in compose, and per-run sandboxes on the host's Docker
# daemon. Fargate cannot host this — featurepilot.sandbox.runner drives the
# Docker API directly (put_archive, exec), which needs a real daemon.
#
# Cost shape: this account has no free EC2 tier, so the design is start/stop.
# `fp-up.sh` / `fp-down.sh` bracket a session (~7c for two hours on
# t4g.medium); only the EBS volume accrues while stopped. `terraform destroy`
# takes that to zero, and user-data makes the rebuild one command.

data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

data "aws_caller_identity" "current" {}

# --- network ------------------------------------------------------------------
# Default VPC on purpose: a dedicated VPC would add a NAT gateway (~$32/mo,
# four times the instance) for no benefit to a single-host dev box.
data "aws_vpc" "default" {
  default = true
}

resource "aws_security_group" "featurepilot" {
  name        = "featurepilot"
  description = "SSH from one address; no inbound API port."
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.my_ip_cidr]
  }

  # Deliberately no 8080 rule. The API has no authentication of its own, so it
  # binds to 127.0.0.1 and is reached over an SSH tunnel:
  #   ssh -L 8080:localhost:8080 ec2-user@<ip>
  egress {
    description = "All outbound (GitHub, Bedrock, PyPI)"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = { Name = "featurepilot" }
}

# --- identity -----------------------------------------------------------------
resource "aws_iam_role" "featurepilot" {
  name = "featurepilot-instance"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ec2.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Bedrock via the instance role: no AWS keys are written to the box.
#
# Resource is "*" for the two invoke actions by design. A cross-region
# inference profile fans out to foundation models in several regions, so a
# correct ARN list must enumerate the profile plus every underlying model in
# every routed region — and it silently breaks whenever AWS adds a region to
# the profile. These two actions cannot do anything but call a model.
resource "aws_iam_role_policy" "bedrock_invoke" {
  name = "bedrock-invoke"
  role = aws_iam_role.featurepilot.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ]
        Resource = "*"
      },
      {
        Effect   = "Allow"
        Action   = ["bedrock:ListFoundationModels", "bedrock:ListInferenceProfiles"]
        Resource = "*"
      },
      {
        Effect = "Allow"
        Action = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
        Resource = [
          "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${var.ssm_prefix}",
          "arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter${var.ssm_prefix}/*",
        ]
      },
    ]
  })
}

# Session Manager: a way back in when SSH is the thing that broke.
resource "aws_iam_role_policy_attachment" "ssm_core" {
  role       = aws_iam_role.featurepilot.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "featurepilot" {
  name = "featurepilot-instance"
  role = aws_iam_role.featurepilot.name
}

# --- host ---------------------------------------------------------------------
resource "aws_instance" "featurepilot" {
  ami                  = data.aws_ssm_parameter.al2023_arm64.value
  instance_type        = var.instance_type
  key_name             = var.key_name
  iam_instance_profile = aws_iam_instance_profile.featurepilot.name

  vpc_security_group_ids = [aws_security_group.featurepilot.id]

  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    repo_url    = var.repo_url
    repo_branch = var.repo_branch
    region      = var.region
    ssm_prefix  = var.ssm_prefix
    use_bedrock = var.use_bedrock
  })
  # Re-running user-data means recreating the instance; that is the intent —
  # provisioning is meant to be reproducible, not patched in place.
  user_data_replace_on_change = true

  root_block_device {
    volume_size           = var.volume_size_gb
    volume_type           = "gp3"
    encrypted             = true
    delete_on_termination = true
  }

  metadata_options {
    http_tokens = "required" # IMDSv2 only
  }

  tags = { Name = "featurepilot" }
}
