#!/bin/bash
# Gate attempt 5, fully cloud: generate 4 FRESH 10k corpora (40,000 games,
# seeds 610001-650000 — disjoint from every corpus used through g56), then
# sweep all four in parallel at damage-tolerance 0, then print verdicts.
#
# Usage: bash gate5_chain.sh            (corpora 57-60)
#        FIRST=61 bash gate5_chain.sh   (next batch of 40k, seeds 650001+)
#
# Corpus n uses base seed 250001 + (n-21)*10000.
set -uo pipefail
AWS=/Users/sallyliu/.awscli-venv/bin/aws
# Region is overridable so sweeps can run where the training fleet is NOT:
# spot vCPU quota is per-region, and a 200k round in us-east-1 leaves
# us-east-2's ~496 vCPU entirely to the ablation training boxes.
REGION="${REGION:-us-east-2}"
BUCKET=pokebot-valuenet-389825051723
SCRATCH="${SCRATCH:-/tmp}"
KEY_ID=$($AWS configure get aws_access_key_id)
SECRET=$($AWS configure get aws_secret_access_key)
FIRST="${FIRST:-57}"
COUNT="${COUNT:-4}"
NS=$(seq "$FIRST" $((FIRST + COUNT - 1)))

# REPACK BEFORE EVERY ROUND. gate5_chain used to reuse whatever
# s3://$BUCKET/code.tar.gz happened to be there, and nothing refreshed it:
# rounds 7 and 8 both swept a bundle from 2026-08-09 17:00, which predated the
# Dancer/choicelock tracker fix (20:32) and ~180 engine patches (next morning).
# 18 of round 8's 41 findings were therefore ALREADY FIXED locally -- pure
# re-reports of stale code. A sweep that does not test the current tree is
# worse than no sweep: it manufactures work and hides real regressions.
if [ "${SKIP_PACK:-0}" != "1" ]; then
  echo "packing current tree -> s3://$BUCKET/code.tar.gz ..." >&2
  ( cd "$SCRATCH" && rm -f code.tar.gz \
    && tar -czf code.tar.gz -C /Users/sallyliu/pokemon-fast-bot \
      --exclude 'poke-engine/target' --exclude 'poke-engine/poke-engine-py/target' \
      --exclude 'poke-engine/.git' --exclude 'foul-play/.venv' --exclude 'foul-play/logs' \
      --exclude 'foul-play/.git' --exclude '__pycache__' --exclude 'valuenet/.git' \
      poke-engine foul-play valuenet \
    && $AWS s3 cp code.tar.gz "s3://$BUCKET/code.tar.gz" --only-show-errors \
    && rm -f code.tar.gz ) || { echo "FATAL: code pack/upload failed" >&2; exit 1; }
  echo "packed at $(date -u +%H:%M:%SZ)" >&2
fi
case "$REGION" in
  us-east-2) SG="${SG:-sg-024075dbfb3236454}" ;;
  us-east-1) SG="${SG:-sg-095f0d0b0b2e3bf03}" ;;
  us-west-2) SG="${SG:-sg-06f5f185337f11a7f}" ;;
  *) SG="${SG:?set SG for $REGION}" ;;
esac
# explicit AMI: this IAM user lacks ssm:GetParameters, so resolve:ssm:... fails.
# Refresh with: aws ec2 describe-images --owners amazon \
#   --filters "Name=name,Values=al2023-ami-2023*-x86_64" --query 'sort_by(Images,&CreationDate)[-1].ImageId'
if [ -z "${AMI:-}" ]; then
  case "$REGION" in
    us-east-2) AMI=ami-0742fcde83639b335 ;;
    *) AMI=$($AWS ec2 describe-images --region "$REGION" --owners amazon \
         --filters "Name=name,Values=al2023-ami-2023.*-kernel-6.1-x86_64" \
         --query 'reverse(sort_by(Images,&CreationDate))[0].ImageId' --output text) ;;
  esac
fi

seed_for() { echo $((250001 + ($1 - 21) * 10000)); }

launch() { # $1=name-tag  $2=user-data-file  -> instance id (spot, type fallback)
  local tag="$1" ud="$2" id=""
  for attempt in 1 2 3 4 5; do
  for t in c7a.16xlarge c7a.12xlarge c7a.8xlarge m7a.16xlarge m7a.12xlarge c7i.16xlarge c6a.16xlarge; do
    id=$($AWS ec2 run-instances --region "$REGION" --image-id "$AMI" \
      --instance-type "$t" --key-name pokebot --security-group-ids "$SG" \
      --instance-market-options 'MarketType=spot,SpotOptions={SpotInstanceType=one-time}' \
      --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=60,VolumeType=gp3}' \
      --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$tag}]" \
      --user-data "file://$ud" --query 'Instances[0].InstanceId' --output text 2>/dev/null)
    [ -n "$id" ] && [ "$id" != "None" ] && { echo "$id"; return 0; }
  done
  sleep 45   # spot capacity churns; retry the whole type list
  done
  return 1
}

echo "=== phase 1: generating 4 fresh corpora (40,000 games)"
for n in $NS; do
  $AWS s3 rm "s3://$BUCKET/gate/genc/GENC_DONE_$n" >/dev/null 2>&1 || true
  cat > "$SCRATCH/ud_genc_$n.sh" <<EOF
#!/bin/bash
export HOME=/root
export AWS_ACCESS_KEY_ID=$KEY_ID
export AWS_SECRET_ACCESS_KEY=$SECRET
export AWS_DEFAULT_REGION=$REGION
export S3_BUCKET=$BUCKET
export CORPUS_N=$n
export BASE_SEED=$(seed_for "$n")
aws s3 cp "s3://$BUCKET/genc_bootstrap.sh" /root/genc_bootstrap.sh
bash /root/genc_bootstrap.sh
EOF
  id=$(launch "gate5-genc-$n" "$SCRATCH/ud_genc_$n.sh") \
    && echo "  corpus $n (seed $(seed_for "$n")): $id" \
    || { echo "  FAILED to launch generator $n"; exit 1; }
done

echo "=== waiting for corpora (up to 90 min)"
for i in $(seq 1 90); do
  DONE=0
  for n in $NS; do
    $AWS s3 ls "s3://$BUCKET/gate/genc/GENC_FAILED_$n" >/dev/null 2>&1 && { echo "GENC_FAILED_$n — aborting"; exit 1; }
    $AWS s3 ls "s3://$BUCKET/gate/genc/GENC_SHORT_$n" >/dev/null 2>&1 && { echo "GENC_SHORT_$n — aborting"; exit 1; }
    $AWS s3 ls "s3://$BUCKET/gate/genc/GENC_DONE_$n" >/dev/null 2>&1 && DONE=$((DONE+1))
  done
  [ "$DONE" -ge "$COUNT" ] && { echo "all 4 corpora ready ($(date -u +%H:%M:%SZ))"; break; }
  sleep 60
done
[ "$DONE" -ge "$COUNT" ] || { echo "corpora not ready in 90 min — aborting"; exit 1; }

echo "=== phase 2: sweeping all four in parallel"
for n in $NS; do
  $AWS s3 rm "s3://$BUCKET/gate/g$n/SWEEP_DONE_g$n" >/dev/null 2>&1 || true
  cat > "$SCRATCH/ud_sweep_$n.sh" <<EOF
#!/bin/bash
export HOME=/root
export AWS_ACCESS_KEY_ID=$KEY_ID
export AWS_SECRET_ACCESS_KEY=$SECRET
export AWS_DEFAULT_REGION=$REGION
export S3_BUCKET=$BUCKET
export CORPUS_TAR=g$n.tar.gz
export NAME=g$n
export FP_MEMBERSHIP_REPLAY=1
aws s3 cp "s3://$BUCKET/sweep_bootstrap.sh" /root/sweep_bootstrap.sh
bash /root/sweep_bootstrap.sh
EOF
  id=$(launch "gate5-sweep-$n" "$SCRATCH/ud_sweep_$n.sh") \
    && echo "  sweep g$n: $id" || { echo "  FAILED to launch sweep $n"; exit 1; }
done

echo "=== waiting for sweeps (up to 90 min)"
for i in $(seq 1 90); do
  DONE=0
  for n in $NS; do
    $AWS s3 ls "s3://$BUCKET/gate/g$n/SWEEP_DONE_g$n" >/dev/null 2>&1 && DONE=$((DONE+1))
  done
  [ "$DONE" -ge "$COUNT" ] && { echo "all 4 sweeps done ($(date -u +%H:%M:%SZ))"; break; }
  sleep 60
done

echo ""
echo "================ GATE 5 VERDICTS ================"
for n in $NS; do
  echo "--- g$n"
  $AWS s3 cp "s3://$BUCKET/gate/g$n/aggregate.txt" - 2>/dev/null \
    | grep -aE "ENGINE FINDINGS|^  (item|boost|status|immunity|heal|damage|move):|^rows:|^  \[|diverged:|TRACKER WARNINGS" \
    || echo "  (no aggregate — sweep may have failed)"
done
echo "================================================="
