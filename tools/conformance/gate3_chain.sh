#!/bin/bash
# Gate attempt 3: wait for holdout20 to finish, package + upload all four
# corpora, launch four parallel sweeps (falling back across instance types when
# spot capacity is short), report every verdict.
AWS=/Users/sallyliu/.awscli-venv/bin/aws
BUCKET=pokebot-valuenet-389825051723
SCRATCH=/private/tmp/claude-501/-Users-sallyliu-pokemon-fast-bot/fc72f840-aeb0-4489-af6c-e74d1aacba56/scratchpad
KEY_ID=$($AWS configure get aws_access_key_id)
SECRET=$($AWS configure get aws_secret_access_key)

echo "waiting for holdout20..."
for i in $(seq 1 60); do
  N=$(find /Users/sallyliu/pokemon-ai/synthetic-corpus-holdout20 -name 'battle-*.log' 2>/dev/null | wc -l | tr -d ' ')
  [ "$N" -ge 10000 ] && break
  sleep 60
done
echo "corpora ready"

# Fresh code tarball, then one corpus tarball each (deleted locally right after
# upload — the disk filled up once already).
cd "$SCRATCH" || exit 1
rm -f code.tar.gz
tar -czf code.tar.gz -C /Users/sallyliu/pokemon-fast-bot \
  --exclude 'poke-engine/target' --exclude 'poke-engine/poke-engine-py/target' \
  --exclude 'poke-engine/.git' --exclude 'foul-play/.venv' --exclude 'foul-play/logs' \
  --exclude 'foul-play/.git' --exclude '__pycache__' --exclude 'valuenet/.git' \
  poke-engine foul-play valuenet
$AWS s3 cp code.tar.gz "s3://$BUCKET/code.tar.gz" --only-show-errors
rm -f code.tar.gz

for n in 17 18 19 20; do
  D="/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout$n"
  (cd "$D" && find . -maxdepth 1 \( -name 'battle-*.log' -o -name '*.teams.json' \) | sed 's|^\./||' > /tmp/gt$n.txt \
    && tar -czf "$SCRATCH/g$n.tar.gz" -T /tmp/gt$n.txt)
  $AWS s3 cp "$SCRATCH/g$n.tar.gz" "s3://$BUCKET/g$n.tar.gz" --only-show-errors
  rm -f "$SCRATCH/g$n.tar.gz"
  echo "uploaded g$n"
done

launch() {
  local n=$1
  cat > "$SCRATCH/ud_g$n.sh" <<EOF
#!/bin/bash
export HOME=/root
export AWS_ACCESS_KEY_ID=$KEY_ID
export AWS_SECRET_ACCESS_KEY=$SECRET
export AWS_DEFAULT_REGION=us-east-2
export S3_BUCKET=$BUCKET
export CORPUS_TAR=g$n.tar.gz
export NAME=g$n
aws s3 cp "s3://\$S3_BUCKET/sweep_bootstrap.sh" /root/sweep_bootstrap.sh
bash /root/sweep_bootstrap.sh
EOF
  for a in $(seq 1 30); do
    for t in c7a.16xlarge c7a.24xlarge c7a.12xlarge m7a.16xlarge c7a.8xlarge; do
      IID=$($AWS ec2 run-instances --image-id ami-028ba4d4ccb4b7b72 --instance-type $t \
        --key-name pokebot --security-group-ids sg-024075dbfb3236454 \
        --instance-market-options 'MarketType=spot,SpotOptions={SpotInstanceType=one-time,InstanceInterruptionBehavior=terminate}' \
        --block-device-mappings '[{"DeviceName":"/dev/xvda","Ebs":{"VolumeSize":60,"VolumeType":"gp3"}}]' \
        --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=pokebot-g$n}]" \
        --user-data "file://$SCRATCH/ud_g$n.sh" --query 'Instances[0].InstanceId' \
        --output text 2>/dev/null)
      if [ -n "$IID" ] && [ "$IID" != "None" ]; then echo "launched g$n ($IID, $t)"; return 0; fi
    done
    sleep 90
  done
  echo "g$n: could not launch"; return 1
}

for n in 17 18 19 20; do launch $n; done

for i in $(seq 1 60); do
  sleep 120
  DONE=0
  for n in 17 18 19 20; do
    $AWS s3 ls "s3://$BUCKET/gate/g$n/SWEEP_DONE_g$n" >/dev/null 2>&1 && DONE=$((DONE+1))
  done
  echo "t=$((i*2))m done=$DONE/4"
  [ "$DONE" -ge 4 ] && break
done

PASS=0
echo "============ GATE ATTEMPT 3 VERDICTS ============"
for n in 17 18 19 20; do
  AGG=$($AWS s3 cp "s3://$BUCKET/gate/g$n/aggregate.txt" - 2>/dev/null)
  F=$(echo "$AGG" | grep -m1 'ENGINE FINDINGS:')
  W=$(echo "$AGG" | grep -m1 'TRACKER WARNINGS')
  D=$(echo "$AGG" | grep -m1 'diverged:')
  echo "g$n | $F |$D | $W"
  if echo "$F" | grep -q 'FINDINGS: 0 hard, 0 soft' && echo "$W" | grep -qE '\): 0 lines'; then
    PASS=$((PASS+1))
  else
    echo "$AGG" | sed -n '/^rows:/,/^====/p' | head -30
  fi
done
echo "================================================="
if [ "$PASS" -ge 4 ]; then
  echo "  *** GATE PASSED: 4 consecutive clean 10k sweeps ***"
else
  echo "  GATE ATTEMPT 3: $PASS/4 clean — counter resets"
fi
