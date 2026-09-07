output "public_ip" {
  description = "Ephemeral by default, so it can change if the instance is recreated."
  value       = oci_core_instance.fp.public_ip
}

output "ssh" {
  value = "ssh ubuntu@${oci_core_instance.fp.public_ip}"
}

output "tunnel" {
  description = "The API is loopback-only; this is how to reach it from a browser."
  value       = "ssh -N -L 8080:localhost:8080 ubuntu@${oci_core_instance.fp.public_ip}"
}

output "provisioning_log" {
  description = "cloud-init is silent from outside; read this if something is missing."
  value       = "ssh ubuntu@${oci_core_instance.fp.public_ip} 'sudo tail -50 /var/log/featurepilot-provision.log'"
}
