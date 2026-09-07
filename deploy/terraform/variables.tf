variable "region" {
  description = "AWS region. Must be one where the Claude inference profiles are active."
  type        = string
  default     = "us-east-1"
}

variable "instance_type" {
  description = <<-EOT
    4 GB is the floor, not a preference: a run needs the 2 GB sandbox
    (FP_SANDBOX_MEMORY) plus the API, Postgres, and Redis on the same host.
    A 1 GB t4g.micro cannot hold a sandbox and will OOM mid-run.
  EOT
  type        = string
  default     = "t4g.medium"
}

variable "my_ip_cidr" {
  description = "Your public IP as a /32. SSH is open to this and nothing else."
  type        = string

  validation {
    condition     = can(cidrhost(var.my_ip_cidr, 0))
    error_message = "my_ip_cidr must be valid CIDR, e.g. 203.0.113.4/32."
  }
}

variable "key_name" {
  description = "Existing EC2 key pair name for SSH."
  type        = string
}

variable "repo_url" {
  description = "Clone URL for this project, cloned to /opt/featurepilot by user-data."
  type        = string
  default     = "https://github.com/Ekam-Bitt/feature-pilot.git"
}

variable "volume_size_gb" {
  description = "Root EBS size. This is the one cost that accrues while stopped (~$0.08/GB-mo)."
  type        = number
  default     = 30
}

variable "ssm_prefix" {
  description = "SSM Parameter Store prefix holding secrets. Created out-of-band, never in state."
  type        = string
  default     = "/featurepilot"
}
