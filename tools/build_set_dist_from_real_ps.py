"""Rebuild data/ps/gen9randombattle_set_dist.json from REAL PS generator output.

Supersedes tools/gen_ps_set_distributions.py, which sampled foul-play's OWN
Python port (fp/search/ps_teams.py). Measured 2026-08-20 against a 100k-team
real-PS corpus, the port's ability/item/tera marginals are statistically
indistinguishable from PS (mean TVD 0.002/0.009/0.014, none over 0.10), but its
MOVE distribution is not: observed TVD exceeds the bootstrapped null p95 for 5
of 6 high-moveset species (thundurus 0.132 vs 0.117, goodra 0.125 vs 0.103,
mesprit, mew, ogerpon). The concrete failure is pincurchin -- every real PS
pincurchin either takes the Curse set or carries a hazard, while the port
produced hazard-less discharge/recover/scald/thunderbolt once in ~3665 draws,
which is exactly the set the sampling auditor flagged as illegal.

Reading the real generator removes that divergence and every other port
divergence at once, permanently, instead of re-porting rules one at a time.

  node ladder-games/validation/gen_ps_reference.js N > shard.json   (x M)
  python3 tools/build_set_dist_from_real_ps.py shard*.json -o data/ps/gen9randombattle_set_dist.json

The consumer (data/pkmn_sets.py _override_with_ps_distribution) unions these
counts over the observed sets with a floor of 1, so a rare-but-legal cell this
corpus never drew stays samplable rather than going to P=0.
"""
import argparse
import collections
import json


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("shards", nargs="+")
    ap.add_argument("-o", "--out", required=True)
    a = ap.parse_args()

    dist = collections.defaultdict(collections.Counter)
    n_teams = 0
    for path in a.shards:
        with open(path) as f:
            d = json.load(f)
        n_teams += d["teams"]
        for sp, rec in d["species"].items():
            dist[sp].update(rec["sets"])

    out = {
        "n_teams": n_teams,
        "source": "real-ps-generator",
        "n_per_species": {sp: sum(c.values()) for sp, c in dist.items()},
        "dist": {sp: dict(c) for sp, c in dist.items()},
    }
    with open(a.out, "w") as f:
        json.dump(out, f)
    tot = sum(out["n_per_species"].values())
    thin = sum(1 for v in out["n_per_species"].values() if v < 200)
    print("%d teams -> %d species, %d mon draws (%.0f/species); %d species "
          "under the 200-draw reweight threshold"
          % (n_teams, len(dist), tot, tot / max(1, len(dist)), thin))


if __name__ == "__main__":
    main()
