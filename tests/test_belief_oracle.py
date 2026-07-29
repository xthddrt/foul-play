"""Falsification controls for the Phase-1 belief-prior oracle."""

from collections import Counter, defaultdict
from copy import deepcopy
import json
from pathlib import Path
import random
import unittest

from fp.search.belief_oracle import (
    compare,
    iter_sidecar_teams,
    load_empirical_teams,
    split_halves,
)


CORPUS_DIR = Path(__file__).parents[2] / "synthetic-corpus"
REQUIRED_MON_KEYS = {
    "species",
    "level",
    "gender",
    "shiny",
    "ability",
    "item",
    "moves",
    "evs",
    "ivs",
    "stats",
    "teraType",
}


def _generator_species(mon):
    if mon.get("ability") == "Imposter" and mon.get("moves") == ["transform"]:
        return "Ditto"
    return mon["species"]


def _by_species(teams):
    by_species = defaultdict(list)
    for team in teams:
        for mon in team:
            by_species[_generator_species(mon)].append(mon)
    return by_species


def _weighted_choice(counter, rng):
    values = sorted(counter)
    weights = [counter[value] for value in values]
    return rng.choices(values, weights=weights, k=1)[0]


def _weighted_sample_without_replacement(counter, count, rng):
    remaining = Counter(counter)
    chosen = []
    for _ in range(count):
        choice = _weighted_choice(remaining, rng)
        chosen.append(choice)
        del remaining[choice]
    return chosen


def uniform_species_sampler(empirical, seed):
    """W1: flatten species frequencies but retain genuine conditional sets."""

    rng = random.Random(seed)
    by_species = _by_species(empirical)
    species = sorted(by_species)
    candidate = []
    for _ in empirical:
        # Sampling without replacement prevents the team-constraint family
        # from becoming an accidental second explanation for the rejection.
        team_species = rng.sample(species, 6)
        candidate.append(
            [deepcopy(rng.choice(by_species[name])) for name in team_species]
        )
    return candidate


def independent_marginals_sampler(empirical, seed):
    """W2: preserve species exactly while destroying within-set dependence."""

    rng = random.Random(seed)
    by_species = _by_species(empirical)
    move_counts = {}
    item_counts = {}
    ability_counts = {}
    tera_counts = {}
    for species, mons in by_species.items():
        move_counts[species] = Counter(
            move for mon in mons for move in set(mon["moves"])
        )
        item_counts[species] = Counter(mon["item"] for mon in mons)
        ability_counts[species] = Counter(mon["ability"] for mon in mons)
        tera_counts[species] = Counter(mon["teraType"] for mon in mons)

    candidate = []
    # Reusing each empirical species roster makes family 1 an exact control:
    # any rejection must come from conditional properties, not sampling noise.
    for empirical_team in empirical:
        team = []
        for empirical_mon in empirical_team:
            species = _generator_species(empirical_mon)
            mon = deepcopy(empirical_mon)
            if species == "Ditto":
                # Transform is Ditto's entire set, so it has no marginals to
                # decorrelate.  Keeping it exact also preserves the marker used
                # to recover transformed Ditto records from captured sidecars.
                team.append(mon)
                continue
            if len(move_counts[species]) >= 4:
                mon["moves"] = _weighted_sample_without_replacement(
                    move_counts[species], 4, rng
                )
            else:
                # Ditto has only Transform; there is no independent four-move
                # alternative to construct for this degenerate species.
                mon["moves"] = list(empirical_mon["moves"])
            mon["item"] = _weighted_choice(item_counts[species], rng)
            mon["ability"] = _weighted_choice(ability_counts[species], rng)
            mon["teraType"] = _weighted_choice(tera_counts[species], rng)
            team.append(mon)
        candidate.append(team)
    return candidate


def level_jitter_sampler(empirical):
    """W3: keep every real draw but violate deterministic species levels."""

    return [
        [{**mon, "level": mon["level"] + 1} for mon in team]
        for team in empirical
    ]


class BeliefOracleTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not CORPUS_DIR.exists():
            raise unittest.SkipTest(f"synthetic corpus is absent: {CORPUS_DIR}")
        # 5,000 games are 10,000 teams.  Loading once keeps the complete oracle
        # control suite comfortably below the requested one-minute guard.
        cls.empirical = load_empirical_teams(CORPUS_DIR, limit_games=5000)

    def result(self, report, family):
        return next(result for result in report.statistics if result.family == family)

    def test_loader_yields_both_documented_teams(self):
        sidecar = sorted(CORPUS_DIR.glob("*.teams.json"))[0]
        teams = list(iter_sidecar_teams(sidecar.parent, limit=1))
        self.assertEqual(len(teams), 2)
        self.assertEqual([len(team) for team in teams], [6, 6])
        for team in teams:
            for mon in team:
                self.assertTrue(REQUIRED_MON_KEYS.issubset(mon))

        # Confirm stable ordering really selected the sidecar inspected above.
        with sidecar.open(encoding="utf-8") as sidecar_file:
            payload = json.load(sidecar_file)
        self.assertEqual(teams[0], payload["teams"]["p1"]["team"])
        self.assertEqual(teams[1], payload["teams"]["p2"]["team"])

    def test_self_consistency_passes_on_10000_teams(self):
        first, second = split_halves(self.empirical, seed=20260729)
        report = compare(first, second)
        self.assertEqual(report.verdict, "PASS", report.format_table())

    def test_w1_uniform_species_rejected_by_family_1(self):
        candidate = uniform_species_sampler(self.empirical, seed=101)
        report = compare(self.empirical, candidate)
        self.assertEqual(report.verdict, "REJECT")
        self.assertEqual(self.result(report, 1).verdict, "REJECT")

    def test_w2_independent_marginals_rejected_by_move_pairs(self):
        candidate = independent_marginals_sampler(self.empirical, seed=202)
        report = compare(self.empirical, candidate)
        self.assertEqual(report.verdict, "REJECT")
        self.assertEqual(self.result(report, 1).verdict, "PASS")
        self.assertEqual(self.result(report, 4).verdict, "REJECT")

    def test_w3_level_jitter_rejected_by_family_2(self):
        candidate = level_jitter_sampler(self.empirical)
        report = compare(self.empirical, candidate)
        self.assertEqual(report.verdict, "REJECT")
        self.assertEqual(self.result(report, 2).verdict, "REJECT")

    def test_same_inputs_produce_identical_report(self):
        first_a, second_a = split_halves(self.empirical, seed=303)
        first_b, second_b = split_halves(self.empirical, seed=303)
        self.assertEqual(
            compare(first_a, second_a),
            compare(first_b, second_b),
        )


if __name__ == "__main__":
    unittest.main()
