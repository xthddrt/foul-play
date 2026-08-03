#!/bin/bash
# Generic gate-sweep monitor. Usage: monitor_sweep.sh <NAME> <INSTANCE_ID>
AWS=/Users/sallyliu/.awscli-venv/bin/aws
BUCKET=pokebot-valuenet-389825051723
NAME=$1
IID=$2
for i in $(seq 1 30); do
  sleep 180
  if $AWS s3 ls "s3://$BUCKET/gate/$NAME/SWEEP_DONE_$NAME" >/dev/null 2>&1; then
    echo "SWEEP $NAME COMPLETE after ~$((i * 3)) min"
    $AWS s3 cp "s3://$BUCKET/gate/$NAME/aggregate.txt" - | head -40
    exit 0
  fi
  STATE=$($AWS ec2 describe-instances --instance-ids "$IID" \
    --query 'Reservations[0].Instances[0].State.Name' --output text 2>/dev/null)
  echo "t=$((i * 3))m state=$STATE"
  if [ "$STATE" != "running" ] && [ "$STATE" != "pending" ]; then
    echo "INSTANCE ENDED WITHOUT SWEEP_DONE"
    exit 1
  fi
done
echo "TIMEOUT"
exit 1
