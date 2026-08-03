"""Substitute value duels v2: played-out 1v1s scored by RESIDUAL STATE.

Each sub-carrying gen9 randbats mon duels a fixed usage-weighted panel of
opponent species, once with no sub and once behind a standing sub (both at
75% HP, the sub's cost). Every turn both sides play their search-preferred
move; chance branches are sampled by probability. Final score (0..2.5):
  0                      subject fainted
  hp_fraction            otherwise (1.0 = full HP anchor)
  + 0.25                 if a substitute still stands
  + 0.125 * stage        per net positive boost stage
The per-mon (sub_up - no_sub) delta is the marginal value of a standing sub.
"""

import json
import random
import sys
import concurrent.futures as cf
from collections import defaultdict

from data.pkmn_sets import RandomBattleTeamDatasets
from fp.battle import Battle, Pokemon
from fp.search.helpers import populate_pkmn_from_set
from fp.search.poke_engine_helpers import battle_to_poke_engine_state
from poke_engine import State, monte_carlo_tree_search, generate_instructions

MAX_TURNS = 40


def residual_score(state):
    side = state.side_one
    active = side.pokemon[int(side.active_index)]
    if active.hp <= 0:
        return 0.0
    score = active.hp / max(active.maxhp, 1)
    if any("substitute" in str(v).lower() for v in side.volatile_statuses):
        score += 0.25
    for attr in (
        "attack_boost",
        "defense_boost",
        "special_attack_boost",
        "special_defense_boost",
        "speed_boost",
    ):
        stages = getattr(side, attr)
        if stages > 0:
            score += 0.125 * stages
    return min(score, 2.5)


def playout(args):
    key, state_str, search_ms, seed = args
    rng = random.Random(seed)
    state = State.from_string(state_str)
    for _ in range(MAX_TURNS):
        s1_active = state.side_one.pokemon[int(state.side_one.active_index)]
        s2_active = state.side_two.pokemon[int(state.side_two.active_index)]
        if s1_active.hp <= 0 or s2_active.hp <= 0:
            break
        res = monte_carlo_tree_search(state, search_ms, threads=1)
        s1_arms = [m for m in res.side_one if m.visits > 0]
        s2_arms = [m for m in res.side_two if m.visits > 0]
        if not s1_arms or not s2_arms:
            break
        s1_move = max(s1_arms, key=lambda m: m.visits).move_choice
        s2_move = max(s2_arms, key=lambda m: m.visits).move_choice
        try:
            branches = generate_instructions(state, s1_move, s2_move)
        except Exception:
            break
        if not branches:
            break
        weights = [max(b.percentage, 0.0) for b in branches]
        if sum(weights) <= 0:
            break
        branch = rng.choices(branches, weights=weights)[0]
        state = state.apply_instructions(branch)
    return key, residual_score(state)


def main():
    search_ms = int(sys.argv[1]) if len(sys.argv) > 1 else 150
    panel_size = int(sys.argv[2]) if len(sys.argv) > 2 else 96
    limit_subjects = int(sys.argv[3]) if len(sys.argv) > 3 else 0

    rng = random.Random(20260801)
    RandomBattleTeamDatasets.initialize("gen9randombattle")
    raw = json.load(open("data/pkmn_sets_cache/gen9randombattle.json"))

    subjects = []
    for species, sets in raw.items():
        variants = defaultdict(int)
        for key, c in sets.items():
            if "substitute" in key:
                variants[key] += c
        if not variants:
            continue
        top_key = max(variants, key=variants.get)
        for pred in RandomBattleTeamDatasets.pkmn_sets.get(species, []):
            parts = top_key.split(",")
            if set(pred.pkmn_moveset.moves) == set(parts[3:7]) and (
                pred.pkmn_set.item or ""
            ) == parts[1]:
                subjects.append((species, pred))
                break
    subjects.sort()
    if limit_subjects:
        subjects = subjects[:limit_subjects]

    panel = []
    names = RandomBattleTeamDatasets.species_sample_names
    weights = RandomBattleTeamDatasets.species_sample_weights
    seen = set()
    while len(panel) < panel_size:
        name = rng.choices(names, weights=weights)[0]
        if name in seen:
            continue
        seen.add(name)
        preds = RandomBattleTeamDatasets.pkmn_sets.get(name)
        if not preds:
            continue
        panel.append((name, max(preds, key=lambda p: p.pkmn_set.count)))

    def build_state(subject_species, subject_set, opp_species, opp_set, sub_up):
        battle = Battle("duel")
        battle.battle_type = None
        me = Pokemon(subject_species, subject_set.pkmn_set.level or 80)
        populate_pkmn_from_set(me, subject_set)
        opp = Pokemon(opp_species, opp_set.pkmn_set.level or 80)
        populate_pkmn_from_set(opp, opp_set)
        me.hp = int(me.max_hp * 0.75)
        if sub_up:
            me.volatile_statuses.append("substitute")
            me.substitute_health = me.max_hp // 4
        battle.user.active = me
        battle.user.reserve = []
        battle.opponent.active = opp
        battle.opponent.reserve = []
        return battle_to_poke_engine_state(battle).to_string()

    tasks = []
    task_index = 0
    for species, pred in subjects:
        for sub_up in (False, True):
            for opp_name, opp_pred in panel:
                if opp_name == species:
                    continue
                s = build_state(species, pred, opp_name, opp_pred, sub_up)
                tasks.append(((species, sub_up), s, search_ms, 7000 + task_index))
                task_index += 1

    results = defaultdict(list)
    with cf.ProcessPoolExecutor(max_workers=8) as ex:
        for key, value in ex.map(playout, tasks, chunksize=4):
            results[key].append(value)

    dump = {}
    for (species, sub_up), scores in results.items():
        dump.setdefault(species, {})["sub_up" if sub_up else "no_sub"] = scores
    json.dump(
        dump,
        open(
            "/private/tmp/claude-501/-Users-sallyliu-pokemon-fast-bot/fc72f840-aeb0-4489-af6c-e74d1aacba56/scratchpad/sub_duel_scores.json",
            "w",
        ),
    )
    header = ("species", "ns_mean", "su_mean", "d_mean", "ns_win", "su_win", "d_win")
    print("%-22s %8s %8s %8s %8s %8s %8s" % header)
    rows = []
    for species, _ in subjects:
        a = results.get((species, False), [])
        b = results.get((species, True), [])
        sa = sum(a) / len(a) if a else float("nan")
        sb = sum(b) / len(b) if b else float("nan")
        wa = sum(1 for x in a if x > 0) / len(a) if a else float("nan")
        wb = sum(1 for x in b if x > 0) / len(b) if b else float("nan")
        rows.append((species, sa, sb, wa, wb))
    for species, sa, sb, wa, wb in sorted(rows, key=lambda r: -(r[2] - r[1])):
        print(
            "%-22s %8.3f %8.3f %+8.3f %7.1f%% %7.1f%% %+7.1f"
            % (species, sa, sb, sb - sa, 100 * wa, 100 * wb, 100 * (wb - wa))
        )


if __name__ == "__main__":
    main()
