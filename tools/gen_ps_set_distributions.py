"""Generate per-species SET DISTRIBUTIONS from the PS-exact generator.

    .venv/bin/python tools/gen_ps_set_distributions.py [N_per_species]

Writes data/ps/gen9randombattle_set_dist.json: for every species in the
gen9randombattle pool, the empirical distribution of complete sets
(moves, item, ability, teraType) over N draws of `ps_teams.random_set` —
i.e. the GENERATOR-TRUE prior for what a revealed mon is running.

WHY: the observed-count dataset (RandomBattleTeamDatasets) is built from
REVEALED sets, and moves that rarely get clicked are systematically
under-counted. Measured 2026-08-15: Mienshao's Triple Axel carries 7.2% of
the dataset's weight-mass vs ~27% generator truth — so no sampled world gave
Mienshao the move that 4x-KO'd Noivern (game 2665304553 turn 5). Same class
as the Gogoat-Earthquake case. This file is the correction: battle-time
candidate sets keep the dataset's STRUCTURES (and its evidence filtering)
but are REWEIGHTED to these probabilities.

Sets are drawn with is_lead=False (the mid-game entry regime; lead-only
culls like Fast Bulky Setup's Booster Energy rule differ slightly for the
opening mon — accepted approximation). Keys are foul-play-normalized:
moves sorted, item/ability/tera via normalize_name, '' item -> "none".
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
FP = os.path.dirname(HERE)
sys.path.insert(0, FP)

from fp.helpers import normalize_name  # noqa: E402
from fp.search import ps_teams  # noqa: E402
from fp.search._ps_team_loop import RANDOM_SETS  # noqa: E402


def main(n_per_species=3000):
    ps_teams.seed(20260815)
    out = {}
    species_ids = sorted(RANDOM_SETS.keys())
    for i, sid in enumerate(species_ids):
        counts = {}
        for _ in range(n_per_species):
            s = ps_teams._GEN.random_set(sid, team_details={}, is_lead=False)
            key = "|".join([
                ",".join(sorted(s["moves"])),
                normalize_name(s["item"]) if s["item"] else "none",
                normalize_name(s["ability"]),
                normalize_name(s["teraType"]),
            ])
            counts[key] = counts.get(key, 0) + 1
        out[sid] = counts
        if (i + 1) % 50 == 0:
            print(f"{i + 1}/{len(species_ids)} species", flush=True)
    path = os.path.join(FP, "data", "ps", "gen9randombattle_set_dist.json")
    with open(path, "w") as f:
        json.dump({"n_per_species": n_per_species, "seed": 20260815,
                   "dist": out}, f, separators=(",", ":"))
    print(f"wrote {path} ({os.path.getsize(path) // 1024} KB, "
          f"{len(species_ids)} species x {n_per_species} draws)")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 3000)
