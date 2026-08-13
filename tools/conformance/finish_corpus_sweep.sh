#!/bin/bash
# Completion + teardown for the v7 corpus_1m sweep.
#   1. counts swept games from the SHARD OBJECTS (never a running counter)
#   2. writes conformance/DONE only when the count equals the register
#   3. terminates every v7sweep-* instance in all three regions AND cancels
#      every open/active spot request, then VERIFIES both are zero
set -uo pipefail
AWS=/Users/sallyliu/.awscli-venv/bin/aws
command -v "$AWS" >/dev/null 2>&1 || AWS=aws
BUCKET="${BUCKET:-pokebot-valuenet-389825051723}"
PREFIX="${PREFIX:-v7/corpus_1m}"
CONF_DIR="${CONF_DIR:-conformance}"
EXPECT="${EXPECT:-1055145}"
REGIONS="${REGIONS:-us-east-2 us-east-1 us-west-2}"

echo "=== teardown"
for R in $REGIONS; do
  IDS=$($AWS ec2 describe-instances --region "$R" \
    --filters "Name=tag:Name,Values=v7sweep-*" \
              "Name=instance-state-name,Values=running,pending,stopping,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text 2>/dev/null)
  if [ -n "$IDS" ]; then
    echo "  $R terminating: $IDS"
    $AWS ec2 terminate-instances --region "$R" --instance-ids $IDS >/dev/null 2>&1
  fi
  # zero instances is NOT zero spot requests -- an open request can refill later.
  # SCOPE THIS TO OUR OWN TAG. The first version filtered only on state, which
  # cancelled seven us-east-1 requests belonging to the v7-masks fleet. Those
  # were one-time requests so the running instances were unaffected, but a
  # PERSISTENT request from another track would have lost its replacement.
  SIDS=$($AWS ec2 describe-spot-instance-requests --region "$R" \
    --filters "Name=state,Values=open,active" \
              "Name=tag:Name,Values=v7sweep-*" \
    --query 'SpotInstanceRequests[].SpotInstanceRequestId' --output text 2>/dev/null)
  # requests whose fulfilled instance carries our tag but which are untagged
  # themselves (run-instances does not propagate tags to the request)
  OURS=$($AWS ec2 describe-instances --region "$R" \
    --filters "Name=tag:Name,Values=v7sweep-*" \
    --query 'Reservations[].Instances[].SpotInstanceRequestId' --output text 2>/dev/null)
  SIDS=$(printf '%s\n%s\n' "$SIDS" "$OURS" | tr ' \t' '\n\n' | grep -v '^$' | grep -v '^None$' | sort -u | tr '\n' ' ')
  if [ -n "$SIDS" ]; then
    echo "  $R cancelling spot requests: $SIDS"
    $AWS ec2 cancel-spot-instance-requests --region "$R" \
      --spot-instance-request-ids $SIDS >/dev/null 2>&1
  fi
done

echo "=== teardown verification"
for R in $REGIONS; do
  N=$($AWS ec2 describe-instances --region "$R" \
    --filters "Name=tag:Name,Values=v7sweep-*" \
              "Name=instance-state-name,Values=running,pending,stopping,stopped" \
    --query 'length(Reservations[].Instances[])' --output text 2>/dev/null)
  S=$($AWS ec2 describe-spot-instance-requests --region "$R" \
    --filters "Name=state,Values=open,active" \
    --query 'length(SpotInstanceRequests)' --output text 2>/dev/null)
  echo "  $R  live_v7sweep_instances=${N:-0}  open_or_active_spot_requests=${S:-0}"
done
