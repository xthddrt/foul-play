#!/bin/bash
# Parameterized single-corpus generator: env CORPUS_N, BASE_SEED.
cd /root
exec > /root/genc.log 2>&1
fail() { aws s3 cp /root/genc.log "s3://$S3_BUCKET/gate/genc/GENC_FAILED_$CORPUS_N" || true; shutdown -h now; }
trap fail ERR
set -e
echo "$(date -u +%H:%M:%SZ) boot on $(hostname), CORPUS_N=$CORPUS_N BASE_SEED=$BASE_SEED"
curl -fsSL -o node.tar.gz https://nodejs.org/dist/v22.23.1/node-v22.23.1-linux-x64.tar.gz
tar xzf node.tar.gz
export PATH=/root/node-v22.23.1-linux-x64/bin:$PATH
aws s3 cp "s3://$S3_BUCKET/ps.tar.gz" . && tar xzf ps.tar.gz && find /root/pokemon-showdown -name "._*" -delete
echo "$(date -u +%H:%M:%SZ) ps extracted, appledouble remaining: $(find /root/pokemon-showdown -name '._*' | wc -l)"
aws s3 cp "s3://$S3_BUCKET/gen_corpus.js" /root/gen_corpus.js
cd /root/pokemon-showdown && npm ci --no-audit --no-fund && echo "$(date -u +%H:%M:%SZ) npm ci done; dist shipped in tarball: $(ls dist/sim/battle-stream.js)"
cd /root
export PS_DIR=/root/pokemon-showdown
GEN=/root/gen_corpus.js
OUT=/root/holdout$CORPUS_N; mkdir -p $OUT
for i in $(seq 0 49); do
  node $GEN --count 200 --start $((BASE_SEED + i*200)) --out $OUT --quiet > $OUT/.gen_$i.log 2>&1 &
done
wait
CNT=$(find $OUT -name 'battle-*.log' | wc -l | tr -d ' ')
echo "holdout$CORPUS_N: $CNT games"
if [ "$CNT" -lt 10000 ]; then echo "=== worker 0 log ==="; head -c 4000 $OUT/.gen_0.log; echo; echo "=== worker 1 log ==="; head -c 2000 $OUT/.gen_1.log; fi
(cd $OUT && find . -maxdepth 1 \( -name 'battle-*.log' -o -name '*.teams.json' \) | sed 's|^\./||' > /tmp/l.txt && tar -czf /root/g$CORPUS_N.tar.gz -T /tmp/l.txt)
aws s3 cp /root/g$CORPUS_N.tar.gz "s3://$S3_BUCKET/g$CORPUS_N.tar.gz"
if [ "$CNT" -ge 10000 ]; then touch /root/done.marker && aws s3 cp /root/done.marker "s3://$S3_BUCKET/gate/genc/GENC_DONE_$CORPUS_N"; else aws s3 cp /root/genc.log "s3://$S3_BUCKET/gate/genc/GENC_SHORT_$CORPUS_N"; fi
shutdown -h now
