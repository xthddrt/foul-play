#!/bin/bash
# RANDOM-ACTION VALIDATION CORPUS (v7/random100k): generate 10 fresh 10k
# corpora with gen_random_corpus.js --policy uniform (seeds 5,010,001-5,110,000,
# disjoint by range from every gate corpus <=1.9M and from the 200-game canary
# at 5,000,001+), then sweep all ten against MERGED MAIN at damage-tolerance 0
# with FP_MEMBERSHIP_REPLAY=1. gate5_chain.sh flow with three deltas:
#   * REGION defaults to us-east-1: the v7 duel fleet (30x m7a.4xlarge spot,
#     480 vCPU) saturates us-east-2's spot quota.
#   * All boxes are 16-vCPU (4xlarge) so both phases fit us-east-1's 300-vCPU
#     spot quota deterministically (10x16=160) instead of relying on type
#     fallback under quota pressure.
#   * Results live under s3://$BUCKET/v7/random100k/ -- never mixed with gate/.
set -uo pipefail
AWS=/Users/sallyliu/.awscli-venv/bin/aws
REGION="${REGION:-us-east-1}"
BUCKET=pokebot-valuenet-389825051723
PFX="${PFX:-v7/random100k}"
SCRATCH="${SCRATCH:-/tmp}"
KEY_ID=$($AWS configure get aws_access_key_id)
SECRET=$($AWS configure get aws_secret_access_key)
COUNT=10
KS=$(seq 1 $COUNT)
HERE="$(cd "$(dirname "$0")" && pwd)"

case "$REGION" in
  us-east-2) SG="${SG:-sg-024075dbfb3236454}" ;;
  us-east-1) SG="${SG:-sg-095f0d0b0b2e3bf03}" ;;
  us-west-2) SG="${SG:-sg-06f5f185337f11a7f}" ;;
  *) SG="${SG:?set SG for $REGION}" ;;
esac
if [ -z "${AMI:-}" ]; then
  AMI=$($AWS ec2 describe-images --region "$REGION" --owners amazon \
    --filters "Name=name,Values=al2023-ami-2023.*-kernel-6.1-x86_64" \
    --query 'reverse(sort_by(Images,&CreationDate))[0].ImageId' --output text)
fi
echo "region=$REGION sg=$SG ami=$AMI"

# ENGINE PROVENANCE: refuse to pack a dirty or wrong-SHA tree.
WANT_SHA="${WANT_SHA:-52daba058555b32b602542f8c6fc70b8f5ba360b}"
GOT_SHA=$(git -C /Users/sallyliu/pokemon-fast-bot/poke-engine rev-parse HEAD)
DIRTY=$(git -C /Users/sallyliu/pokemon-fast-bot/poke-engine status --porcelain | wc -l | tr -d ' ')
if [ "$GOT_SHA" != "$WANT_SHA" ] || [ "$DIRTY" != "0" ]; then
  echo "FATAL: poke-engine tree is $GOT_SHA (dirty files: $DIRTY), want clean $WANT_SHA" >&2
  exit 1
fi
echo "engine=$GOT_SHA (clean)" | $AWS s3 cp - "s3://$BUCKET/$PFX/ENGINE_SHA.txt"

# REPACK BEFORE THE ROUND (same discipline as gate5: a sweep that does not
# test the current tree is worse than no sweep).
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

# Stage this round's scripts under their own keys (gate/ keys untouched).
$AWS s3 cp "$HERE/gen_random_corpus.js" "s3://$BUCKET/gen_random_corpus.js" --only-show-errors
$AWS s3 cp "$HERE/genc_bootstrap.sh" "s3://$BUCKET/randc_bootstrap.sh" --only-show-errors
$AWS s3 cp /Users/sallyliu/pokemon-fast-bot/valuenet/cloud/sweep_bootstrap.sh \
  "s3://$BUCKET/rand_sweep_bootstrap.sh" --only-show-errors

SEED_BASE="${SEED_BASE:-5010001}"
CTAG="${CTAG:-u}"
EXTRA_GEN_ARGS="${EXTRA_GEN_ARGS:-}"
# PS build the gen boxes extract. Default = the legacy gate snapshot; the exam
# passes PS_KEY=ps-6a1836d.tar.gz (packed from the verified pinned checkout).
PS_KEY="${PS_KEY:-ps.tar.gz}"
seed_for() { echo $((SEED_BASE + ($1 - 1) * 10000)); }

launch() { # $1=name-tag  $2=user-data-file  -> instance id (spot, 16-vCPU types)
  local tag="$1" ud="$2" id=""
  for attempt in 1 2 3 4 5; do
  for t in c7a.4xlarge m7a.4xlarge c6a.4xlarge c7i.4xlarge; do
    id=$($AWS ec2 run-instances --region "$REGION" --image-id "$AMI" \
      --instance-type "$t" --key-name pokebot --security-group-ids "$SG" \
      --instance-market-options 'MarketType=spot,SpotOptions={SpotInstanceType=one-time}' \
      --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=60,VolumeType=gp3}' \
      --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$tag}]" \
      --user-data "file://$ud" --query 'Instances[0].InstanceId' --output text 2>/dev/null)
    [ -n "$id" ] && [ "$id" != "None" ] && { echo "$id"; return 0; }
  done
  sleep 45
  done
  return 1
}

echo "=== phase 1: generating 10 fresh uniform-policy corpora (100,000 games)"
for k in $KS; do
  $AWS s3 rm "s3://$BUCKET/$PFX/genc/GENC_DONE_$CTAG$k" >/dev/null 2>&1 || true
  cat > "$SCRATCH/ud_randc_$k.sh" <<EOF
#!/bin/bash
export HOME=/root
export AWS_ACCESS_KEY_ID=$KEY_ID
export AWS_SECRET_ACCESS_KEY=$SECRET
export AWS_DEFAULT_REGION=$REGION
export S3_BUCKET=$BUCKET
export CORPUS_N=$CTAG$k
export BASE_SEED=$(seed_for "$k")
export GEN_KEY=gen_random_corpus.js
export GEN_ARGS="--policy uniform $EXTRA_GEN_ARGS"
export MARK=$PFX/genc
export TAR_DEST=s3://$BUCKET/$PFX/g$CTAG$k.tar.gz
export PS_KEY=$PS_KEY
aws s3 cp "s3://$BUCKET/randc_bootstrap.sh" /root/genc_bootstrap.sh
bash /root/genc_bootstrap.sh
EOF
  id=$(launch "random100k-genc-$CTAG$k" "$SCRATCH/ud_randc_$k.sh") \
    && echo "  corpus g$CTAG$k (seed $(seed_for "$k")): $id" \
    || { echo "  FAILED to launch generator $CTAG$k"; exit 1; }
done

echo "=== waiting for corpora (up to 60 min)"
DONE=0
for i in $(seq 1 60); do
  DONE=0
  for k in $KS; do
    $AWS s3 ls "s3://$BUCKET/$PFX/genc/GENC_FAILED_$CTAG$k" >/dev/null 2>&1 && { echo "GENC_FAILED_$CTAG$k — aborting"; exit 1; }
    $AWS s3 ls "s3://$BUCKET/$PFX/genc/GENC_SHORT_$CTAG$k" >/dev/null 2>&1 && { echo "GENC_SHORT_$CTAG$k — aborting"; exit 1; }
    $AWS s3 ls "s3://$BUCKET/$PFX/genc/GENC_DONE_$CTAG$k" >/dev/null 2>&1 && DONE=$((DONE+1))
  done
  [ "$DONE" -ge "$COUNT" ] && { echo "all $COUNT corpora ready ($(date -u +%H:%M:%SZ))"; break; }
  sleep 60
done
[ "$DONE" -ge "$COUNT" ] || { echo "corpora not ready in 60 min — aborting"; exit 1; }

echo "=== phase 2: sweeping all ten in parallel"
for k in $KS; do
  $AWS s3 rm "s3://$BUCKET/$PFX/g$CTAG$k/SWEEP_DONE_g$CTAG$k" >/dev/null 2>&1 || true
  cat > "$SCRATCH/ud_randsweep_$k.sh" <<EOF
#!/bin/bash
export HOME=/root
export AWS_ACCESS_KEY_ID=$KEY_ID
export AWS_SECRET_ACCESS_KEY=$SECRET
export AWS_DEFAULT_REGION=$REGION
export S3_BUCKET=$BUCKET
export CORPUS_TAR=$PFX/g$CTAG$k.tar.gz
export NAME=g$CTAG$k
export GATE_PREFIX=$PFX
export FP_MEMBERSHIP_REPLAY=1
aws s3 cp "s3://$BUCKET/rand_sweep_bootstrap.sh" /root/sweep_bootstrap.sh
bash /root/sweep_bootstrap.sh
EOF
  id=$(launch "random100k-sweep-g$CTAG$k" "$SCRATCH/ud_randsweep_$k.sh") \
    && echo "  sweep g$CTAG$k: $id" || { echo "  FAILED to launch sweep $CTAG$k"; exit 1; }
done

echo "=== waiting for sweeps (up to 120 min)"
DONE=0
for i in $(seq 1 120); do
  DONE=0
  for k in $KS; do
    $AWS s3 ls "s3://$BUCKET/$PFX/g$CTAG$k/SWEEP_DONE_g$CTAG$k" >/dev/null 2>&1 && DONE=$((DONE+1))
  done
  [ "$DONE" -ge "$COUNT" ] && { echo "all $COUNT sweeps done ($(date -u +%H:%M:%SZ))"; break; }
  sleep 60
done

echo ""
echo "================ RANDOM100K VERDICTS ================"
for k in $KS; do
  echo "--- g$CTAG$k"
  $AWS s3 cp "s3://$BUCKET/$PFX/g$CTAG$k/aggregate.txt" - 2>/dev/null \
    | grep -aE "ENGINE FINDINGS|^  (item|boost|status|immunity|heal|damage|move):|^rows:|^  \[|diverged:|TRACKER WARNINGS" \
    || echo "  (no aggregate — sweep may have failed)"
done
echo "====================================================="
