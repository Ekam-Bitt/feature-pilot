terraform {
  required_version = ">= 1.9"

  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "~> 7.0"
    }
  }
}

# Reads ~/.oci/config, which `oci setup bootstrap` writes — so authenticating
# the CLI once authenticates Terraform too, with no keys in this repo.
provider "oci" {
  config_file_profile = var.oci_profile
  region              = var.region
}
