#!/bin/bash
# Launch spot boxes for the v7 corpus_1m conformance sweep.
#
#   REGION=us-east-2 N=1 FIRST_INDEX=0 NUM_BOXES=1 BUILD_WHEEL=1 \
#   CONF_DIR=conformance_canary SHARD_SIZE=64 LIMIT_SHARDS=32 \
#   bash launch_sweep_corpus.sh
#
# Prints one "<index> <instance-id> <type>" line per box that actually booted.
# Insufficient-capacity failures are tolerated silently: whatever fills, fills.
set -uo pipefail
AWS=/Users/sallyliu/.awscli-venv/bin/aws
command -v "$AWS" >/dev/null 2>&1 || AWS=aws
REGION="${REGION:-us-east-2}"
BUCKET="${BUCKET:-pokebot-valuenet-389825051723}"
PREFIX="${PREFIX:-v7/corpus_1m}"
CORPUS_PREFIX="${CORPUS_PREFIX:-v7/farm2/boxes}"
N="${N:-1}"                       # how many boxes to launch in this call
FIRST_INDEX="${FIRST_INDEX:-0}"   # BOX_INDEX of the first one
NUM_BOXES="${NUM_BOXES:-1}"       # denominator of the shard split (global)
SHARD_SIZE="${SHARD_SIZE:-500}"
LIMIT_SHARDS="${LIMIT_SHARDS:-0}"
STRAGGLER="${STRAGGLER:-0}"
CONF_DIR="${CONF_DIR:-conformance}"
PERSPECTIVES="${PERSPECTIVES:-p1,p2}"
BUILD_WHEEL="${BUILD_WHEEL:-0}"
TYPES="${TYPES:-m7a.8xlarge c7a.8xlarge m7a.4xlarge c7a.4xlarge m7i.4xlarge c6a.8xlarge}"
SCRATCH="${SCRATCH:-/tmp}"
KEY_ID=$($AWS configure get aws_access_key_id)
SECRET=$($AWS configure get aws_secret_access_key)

case "$REGION" in
  us-east-2) SG="${SG:-sg-024075dbfb3236454}"; AMI="${AMI:-ami-0742fcde83639b335}" ;;
  us-east-1) SG="${SG:-sg-095f0d0b0b2e3bf03}" ;;
  us-west-2) SG="${SG:-sg-06f5f185337f11a7f}" ;;
  *) SG="${SG:?set SG for $REGION}" ;;
esac
if [ -z "${AMI:-}" ]; then
  AMI=$($AWS ec2 describe-images --region "$REGION" --owners amazon \
    --filters "Name=name,Values=al2023-ami-2023.*-kernel-6.1-x86_64" \
    --query 'reverse(sort_by(Images,&CreationDate))[0].ImageId' --output text)
fi

for i in $(seq 0 $((N - 1))); do
  IDX=$((FIRST_INDEX + i))
  UD="$SCRATCH/ud_sweepc_${REGION}_$IDX.sh"
  cat > "$UD" <<EOF
#!/bin/bash
export HOME=/root
export AWS_ACCESS_KEY_ID=$KEY_ID
export AWS_SECRET_ACCESS_KEY=$SECRET
export AWS_DEFAULT_REGION=$REGION
export BUCKET=$BUCKET
export PREFIX=$PREFIX
export CORPUS_PREFIX=$CORPUS_PREFIX
export BOX_INDEX=$IDX
export NUM_BOXES=$NUM_BOXES
export SHARD_SIZE=$SHARD_SIZE
export LIMIT_SHARDS=$LIMIT_SHARDS
export STRAGGLER=$STRAGGLER
export CONF_DIR=$CONF_DIR
export PERSPECTIVES=$PERSPECTIVES
export BUILD_WHEEL=$([ "$i" = "0" ] && echo "$BUILD_WHEEL" || echo 0)
aws s3 cp "s3://$BUCKET/$PREFIX/sweep/bootstrap.sh" /root/bootstrap.sh
bash /root/bootstrap.sh
EOF
  id=""
  for t in $TYPES; do
    id=$($AWS ec2 run-instances --region "$REGION" --image-id "$AMI" \
      --instance-type "$t" --key-name pokebot --security-group-ids "$SG" \
      --instance-market-options 'MarketType=spot,SpotOptions={SpotInstanceType=one-time}' \
      --block-device-mappings 'DeviceName=/dev/xvda,Ebs={VolumeSize=80,VolumeType=gp3}' \
      --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=v7sweep-$IDX}]" \
      --user-data "file://$UD" --query 'Instances[0].InstanceId' --output text 2>/dev/null)
    if [ -n "$id" ] && [ "$id" != "None" ]; then echo "$IDX $id $t $REGION"; break; fi
    id=""
  done
  [ -z "$id" ] && echo "$IDX NOCAPACITY - $REGION" >&2
done
