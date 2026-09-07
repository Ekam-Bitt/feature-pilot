output "instance_id" {
  description = "Pass to fp-up.sh / fp-down.sh."
  value       = aws_instance.featurepilot.id
}

output "public_ip" {
  description = "Changes on every stop/start unless an Elastic IP is attached."
  value       = aws_instance.featurepilot.public_ip
}

output "ssh" {
  description = "Shell on the box."
  value       = "ssh ec2-user@${aws_instance.featurepilot.public_ip}"
}

output "tunnel" {
  description = "The API is loopback-only; this is how you reach it."
  value       = "ssh -N -L 8080:localhost:8080 ec2-user@${aws_instance.featurepilot.public_ip}"
}

output "region" {
  description = "Read by fp-up.sh / fp-down.sh so they act on the right region."
  value       = var.region
}
