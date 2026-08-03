#!/bin/bash
NODE=/Users/sallyliu/pokemon-ai/tools/node-v22.23.1-darwin-arm64/bin/node
cd /Users/sallyliu/pokemon-ai || exit 1
for spec in "holdout21:250001" "holdout22:260001" "holdout23:270001" "holdout24:280001"; do
  DIR="/Users/sallyliu/pokemon-ai/synthetic-corpus-${spec%%:*}"; BASE="${spec##*:}"
  mkdir -p "$DIR"; PIDS=""
  for i in 0 1 2 3 4 5 6 7; do
    $NODE tools/gen_corpus.js --count 1250 --start $((BASE + i * 1250)) --out "$DIR" --quiet \
      > "$DIR/.gen_$i.log" 2>&1 &
    PIDS="$PIDS $!"
  done
  wait $PIDS
  echo "${spec%%:*}: $(find "$DIR" -name 'battle-*.log' | wc -l | tr -d ' ') games"
done
echo GATE4_CORPORA_DONE
