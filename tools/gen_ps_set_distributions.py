"""Generate per-species SET DISTRIBUTIONS from the PS-exact generator.

    .venv/bin/python tools/gen_ps_set_distributions.py [N_TEAMS]

Writes data/ps/gen9randombattle_set_dist.json: for every species in the
gen9randombattle pool, the empirical distribution of complete sets
(moves, item, ability, teraType) — i.e. the GENERATOR-TRUE prior for what a
revealed mon is running.

WHY: the observed-count dataset (RandomBattleTeamDatasets) is built from
REVEALED sets, and moves that rarely get clicked are systematically
under-counted. Measured 2026-08-15: Mienshao's Triple Axel carries 7.2% of
the dataset's weight-mass vs ~27% generator truth — so no sampled world gave
Mienshao the move that 4x-KO'd Noivern (game 2665304553 turn 5). Same class
as the Gogoat-Earthquake case. This file is the correction: battle-time
candidate sets keep the dataset's STRUCTURES (and its evidence filtering)
but are REWEIGHTED to these probabilities.

WHOLE-TEAM SAMPLING (Sally 2026-08-20). Sets are bucketed out of complete
`ps_teams.random_team()` draws, NOT out of per-species
`random_set(team_details={}, is_lead=False)` calls. The old form asked the
generator a question no real game ever asks — "what set would this mon roll
as the FIRST member of an EMPTY team?" — and the answer is not the marginal:

  * team_details={} means PS's team-level culls at teams.ts:516-523 never
    fire and its enforcements at :788-796 always do, so every
    team-conditional set had probability ZERO. Corviknight came out
    P(defog)=1.000 against a true 0.866; Heatran P(stealthrock)=1.000 vs
    0.870. 34 species lost 10-17% of their real mass.
  * is_lead=False means the lead-only branches (:1203, :1312-1315,
    :1372-1375, :1399-1403) never fire, so Focus Sash and Eviolite were
    impossible on the one mon PS guarantees IS the lead — 100% of ariados /
    galvantula / kricketune lead sets were unreachable.

Bucketing whole teams makes the table the true unconditional marginal,
including leads (slot 0 of every team) and team-conditional sets at their
real rates. It does NOT make it conditional on what the opponent has already
shown — that needs a teamDetails-keyed table or conditional redraw, and is
tracked separately.

Sampling cost is proportional to coverage of the RAREST species, not the
mean: species are near-uniform in gen9 randbats (~509 of them, 6 slots per
team), so N_TEAMS teams give a species about 6*N/509 draws. 255k teams
~= 3000 draws/species ~= 7 minutes.

Keys are foul-play-normalized: moves sorted, item/ability/tera via
normalize_name, '' item -> "none". `n_per_species` is written as a per-species
MAP now (counts differ by species); the loader falls back to summing a
species' own counts, so the file stays readable by older code.
"""
import collections
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
FP = os.path.dirname(HERE)
sys.path.insert(0, FP)

from fp.helpers import normalize_name  # noqa: E402
from fp.search import ps_teams  # noqa: E402
from fp.search._ps_team_loop import RANDOM_SETS  # noqa: E402


def set_key(mon: dict) -> str:
    return "|".join([
        ",".join(sorted(normalize_name(m) for m in mon["moves"])),
        normalize_name(mon["item"]) if mon["item"] else "none",
        normalize_name(mon["ability"]),
        normalize_name(mon["teraType"]),
    ])


def main(n_teams=255000):
    ps_teams.seed(20260820)
    out = collections.defaultdict(collections.Counter)
    t0 = time.time()
    for i in range(n_teams):
        for mon in ps_teams.random_team():
            out[mon["speciesId"]][set_key(mon)] += 1
        if (i + 1) % 25000 == 0:
            el = time.time() - t0
            print(f"{i + 1}/{n_teams} teams  {el:.0f}s  "
                  f"({(i + 1) / el:.0f}/s)", flush=True)

    pool = sorted(RANDOM_SETS.keys())
    dist = {sid: dict(out[sid]) for sid in pool if out.get(sid)}
    per_species = {sid: sum(dist[sid].values()) for sid in dist}
    missing = [sid for sid in pool if sid not in dist]
    thin = sorted((n, s) for s, n in per_species.items())[:5]
    print(f"species covered: {len(dist)}/{len(pool)}  missing={len(missing)}")
    print(f"draws/species: min={min(per_species.values())} "
          f"mean={sum(per_species.values()) // len(per_species)} "
          f"max={max(per_species.values())}")
    print(f"thinnest: {thin}")
    if missing:
        print(f"MISSING (kept on observed data by the loader): {missing[:20]}")

    path = os.path.join(FP, "data", "ps", "gen9randombattle_set_dist.json")
    with open(path, "w") as f:
        json.dump({"n_per_species": per_species, "seed": 20260820,
                   "n_teams": n_teams, "source": "whole-team",
                   "dist": dist}, f, separators=(",", ":"))
    print(f"wrote {path} ({os.path.getsize(path) // 1024} KB, "
          f"{len(dist)} species from {n_teams} teams)")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 255000)
