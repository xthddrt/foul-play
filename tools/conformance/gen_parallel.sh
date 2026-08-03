#!/bin/bash
# Generate the fresh 10k holdout corpus in parallel over 8 disjoint index
# ranges (gen_corpus.js is resumable: it skips any game whose .log exists).
NODE=/Users/sallyliu/pokemon-ai/tools/node-v22.23.1-darwin-arm64/bin/node
OUT=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout2
cd /Users/sallyliu/pokemon-ai || exit 1
PIDS=""
for i in 0 1 2 3 4 5 6 7; do
  START=$((60001 + i * 1250))
  $NODE tools/gen_corpus.js --count 1250 --start $START --out "$OUT" --quiet \
    > "/tmp/gen_${i}.log" 2>&1 &
  PIDS="$PIDS $!"
done
wait $PIDS
echo "GEN_DONE logs=$(ls $OUT/*.log 2>/dev/null | wc -l | tr -d ' ')"
