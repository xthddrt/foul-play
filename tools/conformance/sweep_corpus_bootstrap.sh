#!/bin/bash
# Cloud bootstrap for the v7 corpus_1m conformance sweep (Amazon Linux 2023).
# Env baked by the launcher: AWS creds/region, BUCKET, PREFIX, BOX_INDEX,
# NUM_BOXES, PERSPECTIVES, SHARD_SIZE, LIMIT_SHARDS, STRAGGLER, BUILD_WHEEL.
#
# BUILD_WHEEL=1 compiles poke-engine from source and PUBLISHES the wheel to
# s3://$BUCKET/$PREFIX/sweep/wheel/ so every scale-out box skips the ~8 min
# Rust build. BUILD_WHEEL=0 waits for that wheel and installs it.
set -uo pipefail
exec > /var/log/pokebot-sweep.log 2>&1
export HOME=/root

fail() {  # fail-loud: a dead box leaves a marker with the tail of this log
  echo "FATAL: $*"
  tail -c 40000 /var/log/pokebot-sweep.log > /tmp/fail.txt
  aws s3 cp /tmp/fail.txt "s3://$BUCKET/$PREFIX/conformance/FAILED_box_$(printf '%03d' "$BOX_INDEX").txt" || true
  shutdown -h now
  exit 1
}
trap 'fail "unexpected exit at line $LINENO"' ERR

dnf install -y gcc gcc-c++ python3.11 python3.11-devel tar gzip || fail "dnf"

cd /opt || fail "cd /opt"
aws s3 cp "s3://$BUCKET/$PREFIX/sweep/code.tar.gz" . || fail "code fetch"
tar xzf code.tar.gz || fail "code untar"
aws s3 cp "s3://$BUCKET/$PREFIX/MANIFEST.tsv.gz" /opt/MANIFEST.tsv.gz || fail "manifest"

python3.11 -m venv venv || fail "venv"
./venv/bin/pip install -q --upgrade pip
./venv/bin/pip install -q boto3 numpy || fail "pip base"

WHEEL_PREFIX="s3://$BUCKET/$PREFIX/sweep/wheel/"
if [ "${BUILD_WHEEL:-0}" = "1" ]; then
  curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal || fail "rustup"
  source "$HOME/.cargo/env"
  ./venv/bin/pip install -q maturin || fail "maturin"
  ./venv/bin/pip install /opt/poke-engine/poke-engine-py \
    --config-settings="build-args=--features poke-engine/terastallization --no-default-features" \
    || fail "engine build"
  # republish the exact artifact the sweep is running
  ./venv/bin/pip wheel /opt/poke-engine/poke-engine-py -w /opt/wheelhouse --no-deps \
    --config-settings="build-args=--features poke-engine/terastallization --no-default-features" \
    || fail "wheel pack"
  aws s3 cp /opt/wheelhouse/ "$WHEEL_PREFIX" --recursive --exclude '*' --include 'poke_engine*.whl' \
    || fail "wheel publish"
  echo "WHEEL PUBLISHED"
else
  for i in $(seq 1 60); do
    if aws s3 ls "$WHEEL_PREFIX" | grep -q '\.whl'; then break; fi
    sleep 20
  done
  mkdir -p /opt/wheelhouse
  aws s3 cp "$WHEEL_PREFIX" /opt/wheelhouse/ --recursive || fail "wheel fetch"
  ./venv/bin/pip install -q /opt/wheelhouse/poke_engine*.whl || fail "wheel install"
fi
./venv/bin/python -c "import poke_engine" || fail "engine import"

grep -v poke-engine /opt/foul-play/requirements.txt > /tmp/req.txt
./venv/bin/pip install -q -r /tmp/req.txt || fail "pip foul-play"

mkdir -p /opt/scratch
# CORPUS_PREFIX must be exported: sweep_corpus.py defaults it to the v7/farm2
# corpus, so a sweep of any OTHER corpus silently 404s every artifact fetch
# unless the launcher's value is passed through to the driver.
export BUCKET PREFIX CORPUS_PREFIX BOX_INDEX NUM_BOXES PERSPECTIVES SHARD_SIZE LIMIT_SHARDS STRAGGLER
export FP_DIR=/opt/foul-play
export MANIFEST=/opt/MANIFEST.tsv.gz
export SCRATCH=/opt/scratch
export WORKERS="${WORKERS:-$(nproc)}"
export FP_MEMBERSHIP_REPLAY=1

echo "=== sweeping: box $BOX_INDEX of $NUM_BOXES, workers $WORKERS, persp $PERSPECTIVES"
./venv/bin/python /opt/foul-play/tools/conformance/sweep_corpus.py 2>&1 | tee /opt/sweep.log
RC=${PIPESTATUS[0]}
aws s3 cp /opt/sweep.log "s3://$BUCKET/$PREFIX/conformance/logs/box_$(printf '%03d' "$BOX_INDEX").log" || true
if [ "$RC" != "0" ]; then fail "sweep driver rc=$RC"; fi
trap - ERR
echo "BOX COMPLETE"
shutdown -h now
