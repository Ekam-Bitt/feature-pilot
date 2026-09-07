# An Always Free Oracle VM running the whole stack: the API as a systemd
# service, Postgres and Redis in compose, and each run's sandbox on the host's
# Docker daemon.
#
# This host exists because of one requirement. The sandbox drives the Docker
# API directly — it creates a container per run, `put_archive`s the repository
# in, and `exec`s inside it — so it needs a real daemon. Render, Railway,
# Koyeb, Fly and Hugging Face Spaces all hand you a managed container without
# one, which rules them out at any price. Oracle's Always Free tier is the only
# genuinely free always-on host that provides it.
#
# Unlike the AWS console, OCI does not conjure networking for you: a VCN, an
# internet gateway, a route table, a security list and a subnet all have to be
# declared before an instance can have a public address.

locals {
  compartment_id = var.compartment_ocid != "" ? var.compartment_ocid : var.tenancy_ocid
}

data "oci_identity_availability_domains" "ads" {
  compartment_id = var.tenancy_ocid
}

# Latest Canonical Ubuntu built for the ARM shape. Pinning an image OCID would
# rot; filtering keeps this working as Oracle publishes new ones.
data "oci_core_images" "ubuntu_arm" {
  compartment_id           = local.compartment_id
  operating_system         = "Canonical Ubuntu"
  operating_system_version = "24.04"
  shape                    = "VM.Standard.A1.Flex"
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"
}

# --- network ------------------------------------------------------------------

resource "oci_core_vcn" "fp" {
  compartment_id = local.compartment_id
  display_name   = "featurepilot"
  cidr_blocks    = ["10.0.0.0/16"]
  dns_label      = "featurepilot"
}

resource "oci_core_internet_gateway" "fp" {
  compartment_id = local.compartment_id
  vcn_id         = oci_core_vcn.fp.id
  display_name   = "featurepilot-igw"
  enabled        = true
}

resource "oci_core_route_table" "fp" {
  compartment_id = local.compartment_id
  vcn_id         = oci_core_vcn.fp.id
  display_name   = "featurepilot-rt"

  route_rules {
    destination       = "0.0.0.0/0"
    destination_type  = "CIDR_BLOCK"
    network_entity_id = oci_core_internet_gateway.fp.id
  }
}

resource "oci_core_security_list" "fp" {
  compartment_id = local.compartment_id
  vcn_id         = oci_core_vcn.fp.id
  display_name   = "featurepilot-sl"

  # Outbound is what this host actually needs: GitHub, PyPI, the model API,
  # and the tunnel it dials out to.
  egress_security_rules {
    destination      = "0.0.0.0/0"
    destination_type = "CIDR_BLOCK"
    protocol         = "all"
  }

  ingress_security_rules {
    source      = var.my_ip_cidr
    source_type = "CIDR_BLOCK"
    protocol    = "6" # TCP
    description = "SSH"

    tcp_options {
      min = 22
      max = 22
    }
  }

  # Never 8080. The API binds to loopback; what faces the internet is Caddy on
  # 443, and only when `serve_https` says so.
  dynamic "ingress_security_rules" {
    for_each = var.serve_https ? [80, 443] : []

    content {
      source      = "0.0.0.0/0"
      source_type = "CIDR_BLOCK"
      protocol    = "6" # TCP
      description = ingress_security_rules.value == 80 ? "ACME challenge" : "HTTPS"

      tcp_options {
        min = ingress_security_rules.value
        max = ingress_security_rules.value
      }
    }
  }
}

resource "oci_core_subnet" "fp" {
  compartment_id    = local.compartment_id
  vcn_id            = oci_core_vcn.fp.id
  display_name      = "featurepilot-public"
  cidr_block        = "10.0.1.0/24"
  route_table_id    = oci_core_route_table.fp.id
  security_list_ids = [oci_core_security_list.fp.id]
  dns_label         = "public"
}

# --- host ---------------------------------------------------------------------

resource "oci_core_instance" "fp" {
  compartment_id      = local.compartment_id
  availability_domain = data.oci_identity_availability_domains.ads.availability_domains[var.availability_domain_index].name
  display_name        = "featurepilot"
  shape               = "VM.Standard.A1.Flex"

  shape_config {
    ocpus         = var.ocpus
    memory_in_gbs = var.memory_in_gbs
  }

  source_details {
    source_type             = "image"
    source_id               = data.oci_core_images.ubuntu_arm.images[0].id
    boot_volume_size_in_gbs = var.boot_volume_size_in_gbs
  }

  create_vnic_details {
    subnet_id        = oci_core_subnet.fp.id
    assign_public_ip = true
    display_name     = "featurepilot-vnic"
  }

  metadata = {
    ssh_authorized_keys = file(pathexpand(var.ssh_public_key_path))
    user_data = base64encode(templatefile("${path.module}/cloud_init.yaml.tftpl", {
      repo_url    = var.repo_url
      repo_branch = var.repo_branch
    }))
  }

  # Re-provisioning means a new instance, which is the intent: the setup script
  # is the source of truth, not whatever state a box drifted into.
  lifecycle {
    ignore_changes = [source_details[0].source_id]
  }
}
