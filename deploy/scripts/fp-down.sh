#!/usr/bin/env bash
# Stop the instance. Compute billing ends; the EBS volume (~$2.40/mo for 30 GB)
# continues until `terraform destroy`.
set -euo pipefail

cd "$(dirname "$0")/../terraform"

ID=$(terraform output -raw instance_id)
REGION=$(terraform output -raw region)

echo "stopping $ID..."
aws ec2 stop-instances --instance-ids "$ID" --region "$REGION" >/dev/null
aws ec2 wait instance-stopped --instance-ids "$ID" --region "$REGION"
echo "stopped. compute billing has ended; EBS persists until terraform destroy."
