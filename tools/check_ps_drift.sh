#!/bin/bash
# Is the set-sampling stack stale against upstream Pokemon Showdown?
#
#   bash foul-play/tools/check_ps_drift.sh
#
# READ-ONLY: fetches upstream, reports which of the three drift layers moved,
# and prints the exact refresh recipe. Never modifies anything itself.
#
# Layers:
#   1. data/random-battles/gen9/  (set DATA -> vendored sets.json + the
#      precomputed set distribution both go stale; the sampler then believes
#      last vintage's sets — the Jumpluff failure mode)
#   2. teams.ts                   (generator LOGIC -> the ps_teams port drifts)
#   3. sim/ + data/moves.ts etc.  (battle MECHANICS -> engine conformance)
set -euo pipefail
FP="$(cd "$(dirname "$0")/.." && pwd)"
WS="$(dirname "$FP")"
PS="$WS/pokemon-showdown"
[ -d "$PS/.git" ] || { echo "no pokemon-showdown checkout at $PS" >&2; exit 2; }

cd "$PS"
git fetch -q origin
PIN=$(git rev-parse --short HEAD)
TIP=$(git rev-parse --short origin/master)
echo "pinned:   $PIN ($(git log -1 --format=%cs HEAD))"
echo "upstream: $TIP ($(git log -1 --format=%cs origin/master))"
[ "$PIN" = "$TIP" ] && { echo "UP TO DATE — nothing to do."; exit 0; }

sets_changed=$(git diff --name-only HEAD origin/master -- data/random-battles/gen9/ | wc -l | tr -d ' ')
logic_changed=$(git diff --name-only HEAD origin/master -- data/random-battles/gen9/teams.ts data/random-battles/gen9/sets.json | grep -c teams.ts || true)
sim_changed=$(git diff --name-only HEAD origin/master -- sim/ data/moves.ts data/abilities.ts data/items.ts data/rulesets.ts | wc -l | tr -d ' ')

echo
if [ "$sets_changed" -gt 0 ]; then
  echo "LAYER 1 — SET DATA CHANGED ($sets_changed file(s) under data/random-battles/gen9/)."
  echo "  The sampler is now believing a STALE set vintage. Refresh recipe:"
  echo "    git -C '$PS' merge --ff-only origin/master   # move the pin"
  echo "    cp '$PS/data/random-battles/gen9/sets.json' '$FP/data/ps/gen9randombattle_sets.json'"
  echo "    (cd '$FP' && .venv/bin/python tools/gen_ps_set_distributions.py 3000)"
  echo "    commit foul-play (vendored sets + dist) and update the pin in the workspace README"
else
  echo "layer 1 (set data): clean"
fi
if [ "$logic_changed" -gt 0 ]; then
  echo "LAYER 2 — teams.ts GENERATOR LOGIC CHANGED: the fp/search/_ps_* port may"
  echo "  be stale. Diff the spans listed in PS_SAMPLER_PORT.md, port the change,"
  echo "  and re-run the acceptance statistics before trusting the sampler."
else
  echo "layer 2 (generator logic): clean"
fi
if [ "$sim_changed" -gt 0 ]; then
  echo "LAYER 3 — sim/mechanics changed ($sim_changed file(s)): engine conformance"
  echo "  may be affected; the next fresh conformance sweep is the arbiter."
else
  echo "layer 3 (sim mechanics): clean"
fi