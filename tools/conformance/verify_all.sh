#!/bin/bash
# Full regression: every finding game from rounds 1-6 against the current build.
cd /Users/sallyliu/pokemon-fast-bot/foul-play || exit 1
A=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout2
B=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout3
C=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout4
D=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout5
E=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout6
F=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout7
G=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout8
H=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout9
I=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout10
J=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout11
K=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout12
FAIL=0
check() {
  local corpus=$1 g=$2
  local LOG="$corpus/battle-gen9randombattle-synth${g}_synthopp.log"
  [ -f "$LOG" ] || { echo "  synth$g: MISSING"; return; }
  local OUT W F1
  OUT=$(FP_MEMBERSHIP_REPLAY=1 .venv/bin/python check_replays.py "$LOG" \
        --teams-dir "$corpus" --damage-tolerance 0 --quiet 2>/tmp/va_$g.txt)
  F1=$(echo "$OUT" | grep -m1 'FINDINGS:')
  W=$(grep -cvE '^logs matched|^$' /tmp/va_$g.txt 2>/dev/null)
  case "$F1$W" in
    *"0 hard, 0 soft"0) ;;
    *) FAIL=$((FAIL+1)); echo "  !! synth$g: $F1 | warnings: $W";;
  esac
}
echo "=== round 6 (gate9 + diag30b) ==="
for g in 130342 135109 136017 137727 137996 132960; do check $H $g; done
for g in 142136 141915 145429 147555 148214; do check $I $g; done
for g in 150248 152043 155202; do check $J $g; done
for g in 165815 165894 168701; do check $K $g; done
echo "=== round 5 (E/F/G) ==="
for g in 101418 104420 101167 101554 101720 104369; do check $E $g; done
for g in 111880 112113 116131 118590; do check $F $g; done
for g in 121371 129506 129970 129748; do check $G $g; done
echo "=== round 4 (D) ==="
for g in 98165 93570 95171 95705 97522 97985 98720 99472; do check $D $g; done
echo "=== round 3 (C) ==="
for g in 84252 86252 87046 89936 81171 88058 82368; do check $C $g; done
echo "=== round 2 (B) ==="
for g in 70422 76563 75916 76272 77424 70104 71053 72182 73533 75181 79271 72642 75383 75071 73774; do check $B $g; done
echo "=== round 1 (A) ==="
for g in 61746 60415 63566 64195 66915 67220 68846 69151 63793 65735 60262; do check $A $g; done
echo "TOTAL NON-CLEAN: $FAIL"

echo "=== round 7 (gate attempt 2) ==="
M=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout13
N=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout14
O=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout15
P=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout16
for g in 170016 173111 173863 175989 176064 177397 179436; do check $M $g; done
for g in 181081 184764 188593 189323; do check $N $g; done
for g in 194692 194889 195487 195800 197392; do check $O $g; done
for g in 201582 201762 202076 203675 203684 203784 204129 205395 206196 209162; do check $P $g; done

echo "=== round 8 (gate attempt 3) ==="
Q=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout17
R=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout18
S=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout19
T=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout20
for g in 210836 211526 213459; do check $Q $g; done
for g in 222712 224619 229623 222884 226284 226341 229653; do check $R $g; done
for g in 235750 236572 237447 231620 235737; do check $S $g; done
for g in 247654; do check $T $g; done

echo "=== round 9 (gate attempt 4 / wave 12) ==="
U=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout21
V=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout22
W=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout23
X=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout24
for g in 255690 257312 250868 252421 257004 257619; do check $U $g; done
for g in 260116; do check $V $g; done
for g in 271522 272122 278377 278925; do check $W $g; done
for g in 284909 286069; do check $X $g; done

echo "=== round 10 (gate attempt 5 / wave 13) ==="
Y=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout25
Z=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout26
AA=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout27
AB=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout28
for g in 292619; do check $Y $g; done
for g in 308864; do check $Z $g; done
for g in 314041 317904 315121 313658; do check $AA $g; done
for g in 326469 327926 320336 325657; do check $AB $g; done

echo "=== round 11 (gate attempt 6 / wave 14) ==="
AC=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout30
AD=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout31
AE=/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout32
for g in 341522 347699; do check $AC $g; done
for g in 350507; do check $AD $g; done
for g in 362661 360917; do check $AE $g; done
echo "GRAND TOTAL NON-CLEAN: $FAIL"
