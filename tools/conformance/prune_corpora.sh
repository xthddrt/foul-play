#!/bin/bash
# Prune consumed corpora down to just the games the regression suite replays.
# Every corpus is regenerable byte-for-byte from its seed range
# (gen_corpus.js --start <seed>), and the tarballs are in S3, so the only
# thing that must survive locally is the finding games verify_all.sh checks.
KEEP_2="61746 60415 63566 64195 66915 67220 68846 69151 63793 65735 60262"
KEEP_3="70422 76563 75916 76272 77424 70104 71053 72182 73533 75181 79271 72642 75383 75071 73774"
KEEP_4="84252 86252 87046 89936 81171 88058 82368"
KEEP_5="98165 93570 95171 95705 97522 97985 98720 99472"
KEEP_6="101418 104420 101167 101554 101720 104369"
KEEP_7="111880 112113 116131 118590"
KEEP_8="121371 129506 129970 129748"
KEEP_9="130342 135109 136017 137727 137996 132960 170016"
KEEP_10="142136 141915 145429 147555 148214"
KEEP_11="150248 152043 155202"
KEEP_12="165815 165894 168701"
KEEP_13="170016 173111 173863 175989 176064 177397 179436"
KEEP_14="181081 184764 188593 189323"
KEEP_15="194692 194889 195487 195800 197392"
KEEP_16="201582 201762 202076 203675 203684 203784 204129 205395 206196 209162"

freed_before=$(df -k /Users/sallyliu | tail -1 | awk '{print $4}')
for n in 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16; do
  DIR="/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout$n"
  [ -d "$DIR" ] || continue
  eval "KEEP=\$KEEP_$n"
  KEEPRE=$(echo $KEEP | tr ' ' '|')
  find "$DIR" -maxdepth 1 -type f \( -name '*.log' -o -name '*.teams.json' \) \
    | grep -vE "synth(${KEEPRE})_" | while read -r f; do rm -f "$f"; done
  echo "holdout$n: kept $(find "$DIR" -name '*.log' | wc -l | tr -d ' ') logs"
done
freed_after=$(df -k /Users/sallyliu | tail -1 | awk '{print $4}')
echo "freed $(( (freed_after - freed_before) / 1048576 )) GiB"
df -h /Users/sallyliu | tail -1
