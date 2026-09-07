variable "oci_profile" {
  description = "Profile in ~/.oci/config. `oci setup bootstrap` writes DEFAULT."
  type        = string
  default     = "DEFAULT"
}

variable "tenancy_ocid" {
  description = "Your tenancy OCID (the `tenancy` line in ~/.oci/config)."
  type        = string
}

variable "compartment_ocid" {
  description = "Compartment to build in. Empty means the root compartment (the tenancy)."
  type        = string
  default     = ""
}

variable "region" {
  description = "Must be your home region: Always Free compute is only available there."
  type        = string
}

variable "availability_domain_index" {
  description = <<-EOT
    Which availability domain to try. A1 capacity is the one genuinely
    unreliable part of this: "Out of host capacity" is common and per-AD, so
    a region with several domains is worth retrying across before waiting.
  EOT
  type        = number
  default     = 0
}

variable "ssh_public_key_path" {
  description = "Public key authorised on the instance. Oracle has no password login."
  type        = string
  default     = "~/.ssh/id_ed25519.pub"
}

variable "my_ip_cidr" {
  description = "Your address as a /32. SSH is opened to this and nothing else."
  type        = string

  validation {
    condition     = can(cidrhost(var.my_ip_cidr, 0))
    error_message = "my_ip_cidr must be valid CIDR, e.g. 203.0.113.4/32."
  }
}

variable "repo_url" {
  description = "Clone URL, fetched by cloud-init."
  type        = string
  default     = "https://github.com/Ekam-Bitt/feature-pilot.git"
}

variable "repo_branch" {
  description = "Branch to deploy."
  type        = string
  default     = "main"
}

# --- Always Free envelope ----------------------------------------------------
# These defaults are the free allowance, not preferences. The A1 shape gives
# 2 OCPU / 12 GB free (halved from 4/24 in 2026) and 200 GB of block volume
# across the tenancy. Raising either starts costing money on a Pay As You Go
# account, and fails outright on an Always Free one.

variable "ocpus" {
  description = "OCPUs. 2 is the Always Free maximum for A1."
  type        = number
  default     = 2
}

variable "memory_in_gbs" {
  description = "Memory. 12 is the Always Free maximum for A1."
  type        = number
  default     = 12
}

variable "boot_volume_size_in_gbs" {
  description = "Boot volume. 50 is the minimum Oracle allows; the sandbox image is ~390 MB."
  type        = number
  default     = 50
}

variable "serve_https" {
  description = <<-EOT
    Open 80 and 443 so Caddy can answer the public frontend directly and get a
    certificate. Off by default: it publishes an API that has no
    authentication of its own, whose protection is public mode, the daily
    spend ceiling, the concurrency cap, and draft-only pull requests.

    It exists because a Cloudflare quick tunnel buffers a response until it
    completes, and a server-sent event stream never completes — 70 events
    arrived through the tunnel from a run that had finished, and zero from one
    still going. Terminating TLS on the host removes the proxy from the path
    rather than arguing with it.
  EOT
  type        = bool
  default     = false
}
