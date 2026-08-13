#!/bin/bash
# Replay EVERY finding game collected across all waves against the current
# build. Any non-clean result is a regression (or an unfixed defect).
# Extend the SPEC list whenever a new wave produces findings.
set -uo pipefail
W=${REPRO_DIR:-/Users/sallyliu/pokemon-fast-bot/ladder-games/analysis/repro}
cd /Users/sallyliu/pokemon-fast-bot/foul-play || exit 1

SPEC=(
  # wave A — pre-existing regression set (fixed 2026-08-03/09)
  "50:545280" "51:551055" "51:556451" "51:556425" "53:573264" "53:576327"
  "55:599044" "55:591248" "56:601676"
  # wave B — Paradox/Knock Off, Ghost Curse, Ice Spinner, Weakness Policy
  "61:652745" "66:704809" "66:709679" "67:716139" "68:726620"
  # wave C — 80k run: hex/comatose, attract, confusion, burn, heals,
  # healblock, spa-drop, sitrus
  "69:731752" "71:753660" "72:765613" "73:773372" "74:785479" "74:789767"
  "75:796427" "76:800042" "76:805963" "76:804024" "76:806355" "76:807105"
  # wave D — 100k sweep: beatup, eq-grassy-moldbreaker, volatile-end, heals,
  # boosts, leechseed-illusion
  "78:829077" "79:836020" "80:843494" "80:847944" "80:848498" "82:860212"
  "82:861671" "83:874870" "83:873476" "85:897713" "85:890187"  # wave E — 100k round 3
  "87:911323" "88:922274" "92:961343" "92:962100" "94:987736" "95:994245" "96:1000871"
  # wave F — 100k rounds 4+5 (batched fix wave: Magician/WP, Weak Armor order,
  # Gulp Missile futuresight, Sitrus lastItem, burn/lum, Substitute-start,
  # multihit threshold-split DP, choicelock tracker)
  "97:1017724" "98:1027225" "99:1035875" "101:1058206" "102:1061370"
  "103:1076923" "104:1080740" "105:1092750" "106:1103465"
  "108:1127782" "112:1163620" "112:1166344" "112:1166504" "112:1168830"
  "113:1179026" "115:1191572" "116:1209620" "116:1203065"
  # wave G — 100k round 6 (Clear Smog onHit slot, Multiscale per-hit, Substitute
  # stale-verify)
  "122:1261709" "123:1273320" "124:1285787"
  # wave H — rounds 7+8 (Tera Blast / Weather Ball type resolution, JS Math.round
  # parity, Anger Shell tracker, Booster Energy, Sitrus/Lum pairs, slp priority).
  # The three still-open findings are deliberately NOT listed; see below.
  "129:1337294" "131:1352906" "131:1354274" "132:1365121"
  "137:1412380" "138:1428622" "139:1436400" "140:1445137" "140:1445270"
  "142:1463727" "142:1463749" "143:1479394" "144:1486002" "144:1486921"
  "146:1501404" "146:1504371" "146:1507717" "149:1538693"
  # wave I — the 5 residuals (drain/recoil decision threshold vs the pending
  # incoming move; Disguise busting on a MISS)
  "147:1514558" "136:1409286" "145:1495189"
  "151:1551151" "151:1553454" "151:1555658" "152:1568570" "154:1584639" "155:1597318"
  # wave J — round 9 (Beak Blast contact burn vs a fainting Cheek Pouch attacker,
  # multi-hit hit-1 KO boundary / Weakness Policy mid-move, Endeavor onTryImmunity
  # decision threshold / Gooey)
  "158:1626888" "161:1655904" "166:1705356"
  # wave K — round 10 (Disguise bust chip swallowing a boost secondary; Illusion HP
  # transfer re-scaling an ABSOLUTE-exact certificate through the impersonated
  # species' max HP)
  "170:1741197" "171:1754819"
)

# DEFERRED, expected 1 soft each — kept out of SPEC so the verdict tracks what
# is actionable. c119 t22: Effect Spore sleeps the attacker on hit 1 of Surging
# Strikes and PS stops the move (battle-actions.ts:890); the engine folds
# secondary rolls post-loop (gi.rs:5423) so the sleep-break cannot be expressed
# without branching the hit loop on the secondary. Audit + adversarial verify
# both landed on defer (2026-08-10). Recurrence ~1 per 6 sweep rounds — if a
# 0/0/0 round is blocked by exactly this class, model it then.
#   "119:1236604"
# STILL OPEN after the rounds 7+8 wave — kept out of SPEC so the verdict tracks
# what is actionable, and so a real regression cannot hide behind a known miss:

CLEAN=0; DIRTY=0; MISSING=0
for s in "${SPEC[@]}"; do
  n="${s%%:*}"; g="${s##*:}"
  L="$W/c$n/battle-gen9randombattle-synth${g}_synthopp.log"
  if [ ! -f "$L" ]; then printf "  synth%-8s (g%s): LOG MISSING\n" "$g" "$n"; MISSING=$((MISSING+1)); continue; fi
  OUT=$(FP_MEMBERSHIP_REPLAY=1 .venv/bin/python check_replays.py "$L" \
        --teams-dir "$W/c$n" --damage-tolerance 0 --quiet 2>/tmp/vaf_$g.err)
  F=$(echo "$OUT" | grep -am1 "FINDINGS:")
  WARN=$(grep -cvE '^logs matched|^$' /tmp/vaf_$g.err 2>/dev/null)
  if echo "$F" | grep -q "0 hard, 0 soft" && [ "${WARN:-0}" -eq 0 ]; then
    CLEAN=$((CLEAN+1))
  else
    DIRTY=$((DIRTY+1))
    printf "  NOT CLEAN synth%-8s (g%s): %s warnings=%s\n" "$g" "$n" "$F" "$WARN"
    echo "$OUT" | grep -aE "^\s+\[" | sed 's/^/        /'
  fi
done
echo ""
echo "clean=$CLEAN  not-clean=$DIRTY  missing=$MISSING  (of ${#SPEC[@]})"
[ "$DIRTY" -eq 0 ] && echo "VERDICT: all finding games clean" || echo "VERDICT: REGRESSION or unfixed defect"
