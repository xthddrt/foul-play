#!/bin/bash
# Launch the corpus C gate sweep on a c7a.24xlarge (96 vCPU) spot instance —
# sized to coexist with the 3x c7a.16xlarge training fleet inside the 300-vCPU
# quota (192 + 96 = 288).
set -euo pipefail
AWS=/Users/sallyliu/.awscli-venv/bin/aws
SCRATCH=/private/tmp/claude-501/-Users-sallyliu-pokemon-fast-bot/fc72f840-aeb0-4489-af6c-e74d1aacba56/scratchpad
KEY_ID=$($AWS configure get aws_access_key_id)
SECRET=$($AWS configure get aws_secret_access_key)

cat > "$SCRATCH/userdata_sweepC.sh" <<EOF
#!/bin/bash
export HOME=/root
export AWS_ACCESS_KEY_ID=$KEY_ID
export AWS_SECRET_ACCESS_KEY=$SECRET
export AWS_DEFAULT_REGION=us-east-2
export S3_BUCKET=pokebot-valuenet-389825051723
export CORPUS_TAR=corpusC.tar.gz
export NAME=corpusC
aws s3 cp "s3://\$S3_BUCKET/sweep_bootstrap.sh" /root/sweep_bootstrap.sh
bash /root/sweep_bootstrap.sh
EOF

$AWS ec2 run-instances \
  --image-id ami-028ba4d4ccb4b7b72 \
  --instance-type c7a.24xlarge \
  --key-name pokebot \
  --security-group-ids sg-024075dbfb3236454 \
  --instance-market-options 'MarketType=spot,SpotOptions={SpotInstanceType=one-time,InstanceInterruptionBehavior=terminate}' \
  --block-device-mappings '[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":60,"VolumeType":"gp3"}}]' \
  --tag-specifications 'ResourceType=instance,Tags=[{Key=Name,Value=pokebot-sweep-corpusC}]' \
  --user-data "file://$SCRATCH/userdata_sweepC.sh" \
  --query 'Instances[0].[InstanceId,InstanceType]' --output text
