#!/bin/bash
# Conformance round supervisor. GOAL (Sally 2026-08-10): TWO CONSECUTIVE 100k
# rounds at 0 hard / 0 soft / 0 diverged. Not two clean rounds ever -- two in a
# row, so a fix wave in between resets the counter to zero.
#
# Runs the happy path by itself: a clean round immediately launches the next
# one. It exits ONLY when a human/agent decision is actually required --
# findings to fix, a stall, or the goal reached -- so no polling loop is needed
# on the other side; the exit itself is the notification.
#
#   FIRST=127 STREAK=0 nohup bash round_supervisor.sh > supervisor_rounds.log 2>&1 &
#
# Exit codes: 0 = GOAL REACHED, 10 = findings need a fix wave, 20 = stalled.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
AWS="/Users/sallyliu/.awscli-venv/bin/aws"
FIRST="${FIRST:?set FIRST (first corpus index of the round now running)}"
COUNT="${COUNT:-10}"
STREAK="${STREAK:-0}"            # consecutive clean rounds so far
NEED="${NEED:-2}"
STALL_MIN="${STALL_MIN:-45}"     # no instance + no verdict for this long = stalled

log() { echo "[$(date -u +%H:%M:%SZ)] $*"; }
state() { echo "{\"first\":$FIRST,\"streak\":$STREAK,\"need\":$NEED}" > "$HERE/round_state.json"; }

log "supervisor up: round FIRST=$FIRST, streak=$STREAK/$NEED"
state

while true; do
  # round 7 (the one running when this supervisor was written) logs to
  # round7.log; every round this supervisor launches itself uses round_f<FIRST>.
  if [ "$FIRST" = "127" ] && [ -f "$HERE/round7.log" ]; then
    LOG="$HERE/round7.log"
  else
    LOG="$HERE/round_f${FIRST}.log"
  fi
  last_change=$(date -u +%s)

  # --- wait for this round's verdict block, watching for stalls -------------
  while true; do
    if grep -q "GATE 5 VERDICTS" "$LOG" 2>/dev/null; then
      n=$(grep -c "^--- g" "$LOG")
      [ "$n" -ge "$COUNT" ] && break
    fi
    live=$($AWS ec2 describe-instances --region us-east-2 \
      --filters "Name=tag:Name,Values=gate5-*" "Name=instance-state-name,Values=running,pending" \
      --query 'length(Reservations[].Instances[])' --output text 2>/dev/null)
    now=$(date -u +%s)
    sz=$(wc -c < "$LOG" 2>/dev/null || echo 0)
    if [ "${sz:-0}" != "${last_sz:-}" ]; then last_sz="$sz"; last_change=$now; fi
    if [ "${live:-0}" -eq 0 ] && [ $(( (now - last_change) / 60 )) -ge "$STALL_MIN" ]; then
      log "STALL: no gate5 instances and no log growth for ${STALL_MIN}m — needs attention"
      exit 20
    fi
    sleep 60
  done

  # --- tally ---------------------------------------------------------------
  H=$(grep -oE "ENGINE FINDINGS: [0-9]+ hard" "$LOG" | grep -oE "[0-9]+" | paste -sd+ - | bc)
  S=$(grep -oE "[0-9]+ soft" "$LOG" | grep -oE "^[0-9]+" | paste -sd+ - | bc)
  D=$(grep -oE "diverged: [0-9]+" "$LOG" | grep -oE "[0-9]+" | paste -sd+ - | bc)
  H=${H:-0}; S=${S:-0}; D=${D:-0}
  log "round FIRST=$FIRST verdict: $H hard, $S soft, $D diverged"

  if [ "$H" -eq 0 ] && [ "$S" -eq 0 ] && [ "$D" -eq 0 ]; then
    STREAK=$((STREAK + 1)); state
    log "CLEAN round — streak $STREAK/$NEED"
    if [ "$STREAK" -ge "$NEED" ]; then
      log "GOAL REACHED: $NEED consecutive clean 100k rounds"
      exit 0
    fi
    FIRST=$((FIRST + COUNT)); state
    log "launching next round FIRST=$FIRST automatically (no fix needed)"
    FIRST=$FIRST COUNT=$COUNT nohup bash "$HERE/gate5_chain.sh" \
      > "$HERE/round_f${FIRST}.log" 2>&1 &
    sleep 120
  else
    # a fix wave resets the streak: the two clean rounds must BOTH postdate the
    # last engine change, otherwise "consecutive" proves nothing about the fix
    STREAK=0; state
    log "FINDINGS — fix wave required. streak reset to 0."
    grep -E "^--- g|ENGINE FINDINGS|\[boost\]|\[volatile\]|\[item\]|\[heal\]|\[status\]|synth" "$LOG" | tail -40
    exit 10
  fi
done
