#!/usr/bin/env bash
# Start the stopped instance and print the tunnel command.
#
# This exists because the account has no free EC2 tier: an always-on host burns
# credits for hours nobody is using. Start it for a session, stop it after.
set -euo pipefail

cd "$(dirname "$0")/../terraform"

ID=$(terraform output -raw instance_id)
REGION=$(terraform output -raw region)

echo "starting $ID..."
aws ec2 start-instances --instance-ids "$ID" --region "$REGION" >/dev/null
aws ec2 wait instance-running --instance-ids "$ID" --region "$REGION"

IP=$(aws ec2 describe-instances --instance-ids "$ID" --region "$REGION" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)

# The public IP changes on every start without an Elastic IP, so read it now
# rather than trusting the last terraform output.
cat <<MSG

running at $IP

  shell:  ssh ec2-user@$IP
  tunnel: ssh -N -L 8080:localhost:8080 ec2-user@$IP

The API needs ~30s after boot for systemd to bring it up. Stop billing with:
  $(dirname "$0")/fp-down.sh
MSG
