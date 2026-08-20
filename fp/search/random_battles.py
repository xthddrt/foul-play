import logging
import os
import random
from copy import deepcopy

import constants
from constants import BattleType
from data import all_move_json, pokedex
from fp.battle import Battle, Pokemon
from data.pkmn_sets import COSMETIC_FORME_TO_BASE, RandomBattleTeamDatasets, TeamDatasets
from fp.search.helpers import log_pkmn_set, populate_pkmn_from_set
from fp.helpers import (
    POKEMON_TYPE_INDICES,
    is_super_effective,
    type_effectiveness_modifier,
    normalize_name,
)

logger = logging.getLogger(__name__)

SHOWDOWN_TEAM_CONSTRAINTS_CONTROL_OFF = "FP_CONTROL_NO_SHOWDOWN_TEAM_CONSTRAINTS"
# Negative control for the PS-exact conditional team sampler (ps_teams.py).
# Set to 1 to restore the independent marginal-count sampler below.
PS_TEAM_SAMPLER_CONTROL_OFF = "FP_CONTROL_NO_PS_TEAM_SAMPLER"
MAX_RANDOM_SAMPLING_ATTEMPTS = 100

# ENTRY-INTENT reweighting: a mon the opponent CHOSE to send in (voluntary
# switch, pivot follow-up, or faint replacement) is likelier to hold a set
# that justifies the entry against what it was sent in front of. Entry
# contexts are recorded by battle_modifier.switch_or_drag; here each
# candidate set's usage-count weight is multiplied by an entry-plausibility
# factor. Floored/capped so intent shifts the prior but can never exclude
# the truth (the SPEC-B5 lesson). ENTRY_INTENT_CONTROL_OFF=1 restores the
# pure count prior as the negative control.
ENTRY_INTENT_CONTROL_OFF = "FP_CONTROL_NO_ENTRY_INTENT"
ENTRY_INTENT_PRIORITY_KO = 5.0
ENTRY_INTENT_OUTSPEED_KO = 3.0
ENTRY_INTENT_THREAT = 0.5
ENTRY_INTENT_MIN_MULT = 0.5
ENTRY_INTENT_MAX_MULT = 6.0

# item -> (required category or None for both, damage multiplier): the only
# set-varying damage factors worth modeling in a likelihood feature
_ENTRY_ITEM_MULTIPLIERS = {
    "lifeorb": (None, 1.3),
    "choiceband": (constants.PHYSICAL, 1.5),
    "choicespecs": (constants.SPECIAL, 1.5),
    "lightball": (None, 2.0),
}


def _entry_move_damage(pkmn, move_name, item, ctx):
    """Rough expected damage of `move_name` into the recorded defender.

    A likelihood feature, not a damage calc: randbats stats are
    set-independent (fixed 85 EVs, neutral nature), so sets differ only by
    moves/item/ability and this stays accurate enough to rank entry intent.
    Returns (damage, priority).
    """
    move_data = all_move_json.get(move_name)
    if move_data is None:
        return 0.0, 0
    priority = move_data.get(constants.PRIORITY, 0)
    base_power = move_data.get("basePower", 0)
    category = move_data.get(constants.CATEGORY)
    if not base_power or category not in (constants.PHYSICAL, constants.SPECIAL):
        return 0.0, priority
    if category == constants.PHYSICAL:
        attack = pkmn.stats[constants.ATTACK]
        defense = ctx["vs_defense"]
    else:
        attack = pkmn.stats[constants.SPECIAL_ATTACK]
        defense = ctx["vs_special_defense"]
    damage = ((2 * pkmn.level / 5 + 2) * base_power * attack / max(defense, 1)) / 50 + 2
    move_type = move_data.get(constants.TYPE)
    if move_type in pkmn.types:
        damage *= 1.5
    damage *= type_effectiveness_modifier(move_type, ctx["vs_types"])
    item_rule = _ENTRY_ITEM_MULTIPLIERS.get(item or "")
    if item_rule is not None and (item_rule[0] is None or item_rule[0] == category):
        damage *= item_rule[1]
    return damage * 0.925, priority


def entry_intent_multipliers(pkmn, remaining_sets):
    contexts = getattr(pkmn, "entry_contexts", None)
    if not contexts or os.environ.get(ENTRY_INTENT_CONTROL_OFF) == "1":
        return [1.0] * len(remaining_sets)
    multipliers = []
    for pkmn_set in remaining_sets:
        multiplier = 1.0
        for ctx in contexts:
            advantage = 0.0
            best_frac = 0.0
            for move_name in pkmn_set.pkmn_moveset.moves:
                damage, priority = _entry_move_damage(
                    pkmn, move_name, pkmn_set.pkmn_set.item, ctx
                )
                if damage <= 0.0:
                    continue
                frac = damage / max(ctx["vs_hp"], 1)
                if frac >= 1.0 and priority > 0:
                    # revenge entry: KOs the defender before it can act
                    advantage = max(advantage, ENTRY_INTENT_PRIORITY_KO)
                elif frac >= 1.0 and pkmn.stats[constants.SPEED] > ctx["vs_speed"]:
                    advantage = max(advantage, ENTRY_INTENT_OUTSPEED_KO)
                best_frac = max(best_frac, min(frac, 1.0))
            multiplier *= 1.0 + advantage + ENTRY_INTENT_THREAT * best_frac
        multipliers.append(
            min(max(multiplier, ENTRY_INTENT_MIN_MULT), ENTRY_INTENT_MAX_MULT)
        )
    return multipliers


def entry_weighted_counts(pkmn, remaining_sets):
    # counts are GENERATOR-TRUTH since 2026-08-15: RandomBattleTeamDatasets
    # rebuilds every species' set structures and counts from the PS-exact
    # distribution at initialize (see _override_with_ps_distribution)
    multipliers = entry_intent_multipliers(pkmn, remaining_sets)
    return [
        s.pkmn_set.count * m for s, m in zip(remaining_sets, multipliers)
    ]


def _fresh_entry_context(user_active):
    return {
        "vs_hp": user_active.hp,
        "vs_maxhp": user_active.max_hp,
        "vs_types": list(user_active.types),
        "vs_defense": user_active.stats[constants.DEFENSE],
        "vs_special_defense": user_active.stats[constants.SPECIAL_DEFENSE],
        "vs_speed": user_active.stats[constants.SPEED],
    }


# a kill is "clean" when the killer's total cost in the exchange -
# retaliation (if our active gets to act first) plus recoil on the capped
# damage - stays under this fraction of the killer's max hp
ENTRY_INTENT_MINIMAL_LOSS_FRACTION = 0.2


def _clean_kill_moves(opponent_active, user_active, pkmn_set, ctx, our_best_hit):
    """Moves in `pkmn_set` that KO our active at no/minimal cost to the
    killer: the kill lands before our active can act (priority, or the
    killer outspeeds), or our active's best known retaliation is minimal;
    recoil is charged on the damage actually needed for the KO."""
    opponent_outspeeds = opponent_active.stats[constants.SPEED] > ctx["vs_speed"]
    clean = []
    for move_name in pkmn_set.pkmn_moveset.moves:
        damage, priority = _entry_move_damage(
            opponent_active, move_name, pkmn_set.pkmn_set.item, ctx
        )
        if damage < ctx["vs_hp"]:
            continue
        strikes_first = priority > 0 or opponent_outspeeds
        retaliation = 0.0 if strikes_first else our_best_hit
        move_data = all_move_json.get(move_name) or {}
        recoil = move_data.get("recoil")
        recoil_loss = (
            (recoil[0] / recoil[1]) * min(damage, ctx["vs_hp"]) if recoil else 0.0
        )
        loss_fraction = (retaliation + recoil_loss) / max(
            opponent_active.max_hp, 1
        )
        if loss_fraction <= ENTRY_INTENT_MINIMAL_LOSS_FRACTION:
            clean.append(move_name)
    return clean


def revenge_certain_sets(opponent_active, user_active, remaining_sets):
    """RETIRED 2026-08-20 -- NO LONGER CALLED. Kept for its tests and as the
    record of why. Do not re-wire it into `prepare_random_battles`.

    It hard-locked a just-entered opponent to a single clean-kill move, which
    is exactly what the SPEC-B5 lesson forbids (see the ENTRY-INTENT comment at
    the top of this file: intent may shift the prior but "can never exclude the
    truth"). Measured cost, ladder game 2667906697 / 2667910469 turn 5: it
    sampled Volcarona with ['fireblast'] only, so Quiver Dance -- present in
    38/38 of its candidate sets, i.e. CERTAIN -- had probability zero in all 8
    worlds. The opponent clicked Quiver Dance. Re-running that decision with
    complete sets moved the argmax off `switch chimecho` entirely (95.0% ->
    0.4%) and dropped the position from 0.744 to ~0.50: the lock was not
    mispricing a tail, it was inflating the whole evaluation.

    The premise is unsound, not merely mistuned. `_clean_kill_moves` tests
    whether a KO is AVAILABLE, never whether taking it NOW beats setting up --
    and against a mon it outspeeds and walls, the KO is still there next turn,
    so a setup sweeper strictly prefers to boost first. The graded
    entry-intent weights encode the same signal within a 0.5x-6x band and are
    what the sampler uses now.

    Original contract below.

    CERTAIN-REVENGE modeling: the opponent CHOSE this entry against our
    current active, the revenge window is still open (no move used since
    entering), and candidate sets carry a CLEAN-KILL move - one that KOs
    our active at no or minimal cost to the killer. Model them as certainly
    having and using it: return (qualifying_sets, revenge_move_names) so
    the sampler draws only those sets and locks the world's moveset to the
    counter move.

    The lock is only a reasonable inference when the counter is UNIQUELY
    their best line, so two extra gates apply:
    - UNIQUENESS: every qualifying set must have exactly ONE clean-kill
      move; a set with two is ambiguous about what they will click.
    - DISCRIMINATION: at least one candidate set must LACK a clean kill -
      the entry choice then actually reveals which set they hold. If every
      set kills the same way, the entry tells us nothing about the set.

    (None, None) when the premise does not hold; the graded entry-intent
    weights still apply. Tradeoff, stated: the lock also flattens their
    future in-tree turns for this decision's search, and retaliation is
    estimated without set-specific abilities/items on our own hit.
    """
    if os.environ.get(ENTRY_INTENT_CONTROL_OFF) == "1":
        return None, None
    contexts = getattr(opponent_active, "entry_contexts", None)
    if not contexts or contexts[-1].get("vs_name") != user_active.name:
        return None, None
    if getattr(opponent_active, "active_move_actions", 0) != 0:
        return None, None
    ctx = _fresh_entry_context(user_active)
    # our active's best known hit INTO the entering mon (stats are
    # set-independent in randbats; abilities/items are not modeled here)
    opponent_ctx = {
        "vs_hp": opponent_active.hp,
        "vs_maxhp": opponent_active.max_hp,
        "vs_types": list(opponent_active.types),
        "vs_defense": opponent_active.stats[constants.DEFENSE],
        "vs_special_defense": opponent_active.stats[constants.SPECIAL_DEFENSE],
        "vs_speed": opponent_active.stats[constants.SPEED],
    }
    our_best_hit = 0.0
    for mv in getattr(user_active, "moves", []):
        damage, _ = _entry_move_damage(
            user_active, mv.name, user_active.item, opponent_ctx
        )
        our_best_hit = max(our_best_hit, damage)
    qualifying = []
    revenge_moves = set()
    ambiguous = False
    heterogeneous = False
    for pkmn_set in remaining_sets:
        clean = _clean_kill_moves(
            opponent_active, user_active, pkmn_set, ctx, our_best_hit
        )
        if len(clean) == 1:
            qualifying.append(pkmn_set)
            revenge_moves.update(clean)
        elif len(clean) > 1:
            ambiguous = True
        else:
            heterogeneous = True
    if not qualifying or ambiguous or not heterogeneous:
        return None, None
    return qualifying, revenge_moves

_WEB_SETTERS = {
    "ariados",
    "smeargle",
    "masquerain",
    "kricketune",
    "leavanny",
    "galvantula",
    "vikavolt",
    "ribombee",
    "araquanid",
    "spidops",
}
_SCREEN_SETTERS = {"meowstic", "grimmsnarl", "ninetalesalola", "abomasnow"}
_SUN_SETTERS = {"ninetales", "torkoal", "groudon", "koraidon"}
_INCOMPATIBLE_POKEMON = (
    ({"blissey"}, {"chansey"}),
    ({"illumise"}, {"volbeat"}),
    (_WEB_SETTERS, _WEB_SETTERS),
    (_SCREEN_SETTERS, _SCREEN_SETTERS),
    ({"toxicroak"}, _SUN_SETTERS),
)
_EXTRA_FIRE_WEAK_ABILITIES = {"dryskin", "fluffy"}


# PS's TEAM-BASED MOVE CULLS (pokemon-showdown teams.ts:503-535), driven by the
# teamDetails it accumulates as it builds each member (teams.ts:1909-1919).
# Each entry is (moves that SET the flag, moves culled from later members once
# it is set).
#
# These culls are SOFT in PS: every branch bails with
# `if (moves.size + movePool.length <= this.maxMoveCount) return;` when removing
# the move would leave the mon short, so the duplicate survives occasionally.
# Measured over 100k generated teams: P(>=2 stealthrock) = 0.0000, but spikes
# 0.0109 and rapidspin 0.0081. The sampler below mirrors that -- it prefers
# candidate sets without the locked move and only falls back when NO candidate
# avoids it.
_TEAM_EXCLUSIVE = (
    (("stealthrock", "stoneaxe"), ("stealthrock",)),
    (("stickyweb",), ("stickyweb",)),
    (("toxicspikes",), ("toxicspikes",)),
    (("defog", "rapidspin", "mortalspin"), ("defog", "rapidspin")),
    (("healbell",), ("healbell",)),
)
# spikes is a COUNTER in PS, not a flag: only the THIRD user is culled
# (teams.ts:529 `teamDetails.spikes >= 2`).
_SPIKES_SETTERS = ("spikes", "ceaselessedge")


def _team_locked_moves(pkmn_list):
    """Moves PS would have culled from any further member of this team.

    Without this, each revealed opponent mon draws its set independently
    (`random.choices` per reserve) with no cross-mon state, so a world can hold
    two Stealth Rock users -- a team the generator can never produce. Seen in
    validation runs vg_t2 and vg_f2: 14 and 13 of the sampled worlds gave the
    opponent both an Azelf and a Camerupt carrying Stealth Rock.
    """
    locked, spikes_users = set(), 0
    for pkmn in pkmn_list:
        names = {m.name for m in getattr(pkmn, "moves", None) or []}
        if not names:
            continue
        for setters, culled in _TEAM_EXCLUSIVE:
            if names.intersection(setters):
                locked.update(culled)
        if names.intersection(_SPIKES_SETTERS):
            spikes_users += 1
    if spikes_users >= 2:
        locked.add("spikes")
    return locked


def _draw_set_respecting_team(pkmn, candidate_sets, weights, locked):
    """`random.choices` over the candidates, preferring ones that do not
    duplicate an already-locked team move. Falls back to the unfiltered draw
    when every candidate carries it -- which is PS's own behaviour, and is also
    what keeps a mon that has genuinely REVEALED the move samplable."""
    if locked:
        keep = [
            (s, w) for s, w in zip(candidate_sets, weights)
            if not locked.intersection(s.pkmn_moveset.moves)
        ]
        if keep:
            candidate_sets, weights = [k[0] for k in keep], [k[1] for k in keep]
    return random.choices(candidate_sets, weights=weights)[0]


def _forced_exclusive_moves(battle, revealed_pkmn_sets, skip=None):
    """Exclusive team moves some OTHER opponent mon MUST be holding.

    Two sources, both facts about this battle rather than draws: a move already
    seen on the field, and a move present in EVERY candidate set that survived
    the evidence for that mon. The second is what the reserve-side filter alone
    cannot see -- in validation game g15 a speed read ruled out Choice Scarf for
    Mamoswine, which killed its non-Stealth-Rock sets, so Stealth Rock became
    forced; the ACTIVE (Stonjourner) was still planned without that knowledge
    and duplicated it in 14 worlds.

    The active is drawn from `_stratified_active_plan`, decided ONCE before the
    world loop, so it cannot use the per-world lock the reserves use. Filtering
    its candidate list up front is the equivalent.
    """
    party = [battle.opponent.active] + list(battle.opponent.reserve)
    locked, spikes_users = set(), 0
    for pkmn in party:
        if pkmn is None or pkmn is skip:
            continue
        names = {m.name for m in getattr(pkmn, "moves", None) or []}
        candidates = revealed_pkmn_sets.get(pkmn.name) or []
        if candidates:
            common = set(candidates[0].pkmn_moveset.moves)
            for c in candidates[1:]:
                common &= set(c.pkmn_moveset.moves)
            names |= common
        for setters, culled in _TEAM_EXCLUSIVE:
            if names.intersection(setters):
                locked.update(culled)
        if names.intersection(_SPIKES_SETTERS):
            spikes_users += 1
    if spikes_users >= 2:
        locked.add("spikes")
    return locked


def _datasets_for(battle: Battle):
    if battle.battle_type == BattleType.RANDOM_BATTLE:
        return RandomBattleTeamDatasets
    elif battle.battle_type == BattleType.BATTLE_FACTORY:
        return TeamDatasets
    raise ValueError("Only random battles are supported")


def populate_pkmn_from_fallback_set(pkmn: Pokemon, datasets) -> bool:
    """Last resort when the evidence eliminated EVERY candidate set.

    Leaving the mon un-populated is the worst possible answer: it reaches the
    engine with 1-3 moves, ability NONE and item UNKNOWNITEM, i.e. a live
    threat priced as nearly harmless - and that happens in exactly the
    Zoroark / transform / dataset-drift cases that punish it hardest.

    Draw from the species' UNFILTERED set list (count-weighted) and merge:
    every revealed move is kept (they are facts, and they come first), the
    sampled set tops the list up to 4, and item/ability/tera are filled only
    where they are still unknown. Returns False when even the unfiltered list
    is empty, leaving the old behaviour.
    """
    unfiltered = datasets.get_pkmn_sets_from_pkmn_name(pkmn)
    if not unfiltered:
        return False

    known_moves = list(pkmn.moves)
    # HARD NEGATIVE EVIDENCE still applies (Sally 2026-08-20). The list above is
    # the RAW species list -- get_pkmn_sets_from_pkmn_name never runs
    # full_set_pkmn_can_have_set -- so drawing from it straight writes an item
    # or ability we positively DISPROVED. That evidence is direct observation,
    # not inference: battle_modifier.py:1427 adds every ability in
    # ABILITIES_REVEALED_ON_SWITCH_IN (intimidate, pressure, neutralizinggas,
    # sandstream, drought, drizzle, snowwarning) to impossible_abilities when a
    # switch-in announced none of them, and can_have_choice_item is cleared once
    # the mon used two different moves.
    #
    # Filter to sets that do not contradict any of it, and fall through to the
    # unfiltered draw only if that leaves nothing -- the whole point of this
    # function is that a live threat must never reach the engine un-populated.
    plausible = [
        s for s in unfiltered
        if s.pkmn_set.item not in pkmn.impossible_items
        and not (
            s.pkmn_set.item in constants.CHOICE_ITEMS
            and not pkmn.can_have_choice_item
        )
        and s.pkmn_set.ability not in pkmn.impossible_abilities
        and not (
            pkmn.terastallized
            and s.pkmn_set.tera_type is not None
            and s.pkmn_set.tera_type != pkmn.tera_type
        )
    ]
    pool = plausible or unfiltered
    if not plausible:
        logger.warning(
            "%s: even the unfiltered set list contradicts the evidence - "
            "drawing anyway rather than leaving it blank", pkmn.name,
        )
    sampled = random.choices(
        pool, weights=[s.pkmn_set.count for s in pool]
    )[0]
    logger.warning(
        "No candidate set survived the evidence for {} - falling back to an "
        "unfiltered set. revealed_moves={} item={} ability={}".format(
            pkmn.name,
            [m.name for m in known_moves],
            pkmn.item,
            pkmn.ability,
        )
    )
    populate_pkmn_from_set(pkmn, sampled, source="empty-candidate fallback")

    merged = list(known_moves)
    for mv in pkmn.moves:
        if len(merged) >= 4:
            break
        if all(mv.name != known.name for known in merged):
            merged.append(mv)
    pkmn.moves = merged[:4]
    return True


def get_all_remaining_sets_for_revealed_pkmn(battle: Battle) -> dict:
    datasets = _datasets_for(battle)

    revealed_pkmn = []
    for pkmn in battle.opponent.reserve:
        revealed_pkmn.append(pkmn)
    if battle.opponent.active is not None:
        revealed_pkmn.append(battle.opponent.active)

    ret = {}
    for pkmn in revealed_pkmn:
        sets = datasets.get_all_remaining_sets(pkmn)
        random.shuffle(sets)
        ret[pkmn.name] = sets

    return ret


def largest_remainder_allocation(weights, n: int) -> list[int]:
    """Allocate `n` indivisible worlds across `weights` proportionally.

    Hamilton / largest-remainder: everyone gets their floor share, and the
    leftover seats go to the largest fractional remainders (ties by index, so
    the result is DETERMINISTIC given the posterior). A candidate whose
    posterior is at least 1/(2n) gets a seat unless it loses an exact remainder
    TIE to a lower-index candidate (e.g. [1,1,1] over 2 worlds -> [1,1,0]) --
    an independent `random.choices` draw per world gives such a set only a
    ~1-e^-0.5 = 39% chance of appearing at all, and a decision that never sees
    a set cannot hedge against it.
    """
    n = int(n)
    weights = [max(0.0, float(w)) for w in weights]
    total = sum(weights)
    if n <= 0 or not weights:
        return [0] * len(weights)
    if total <= 0:
        # no information: spread the worlds as evenly as the count allows
        shares = [n / len(weights)] * len(weights)
    else:
        shares = [n * w / total for w in weights]
    allocation = [int(s) for s in shares]
    remaining = n - sum(allocation)
    order = sorted(
        range(len(weights)), key=lambda i: (-(shares[i] - allocation[i]), i)
    )
    for i in order[:remaining]:
        allocation[i] += 1
    return allocation


def _stratified_active_plan(sets, weights, num_battles):
    """`[(set, sample_chance)] * num_battles` -- one entry per world.

    SYSTEMATIC PPS SAMPLING with a random start. Set i is targeted
    n_i = p_i * num_battles seats and receives floor(n_i) or ceil(n_i) of them,
    so E[seats_i] = n_i EXACTLY, and any set with p_i > 0 is reachable on some
    decision -- including the tail that cannot afford a whole seat.

    Every world then carries the SAME chance, 1/num_battles: under systematic
    PPS the sample is self-weighting, because the Horvitz-Thompson weight
    p_i/pi_i collapses to 1/num_battles both for a set seated with certainty
    and for one seated with probability n_i < 1. So the chances still sum to
    exactly 1 -- the invariant `_aggregate_results` in fp/search/selection.py
    relies on when it weights each world's policy by `sample_chance`, and
    `pooled_share` is compared against ABSOLUTE thresholds there, so a deflated
    sum would silently tighten those gates -- while the estimator is now
    UNBIASED for the true posterior.

    REPLACES a deterministic largest-remainder pick that renormalized over the
    SEATED sets only (Sally 2026-08-20). That was biased twice over. It handed
    the unseated mass to whoever was already seated -- measured up to 8.7x
    inflation of the top set -- and because the allocation was a pure function
    of the weights, the SAME tail sets were dropped on every decision while the
    evidence was unchanged, so it never averaged out across turns the way
    sampling noise does. Mesprit reached 72.3% of posterior mass permanently
    unreachable, 19.7% of it holding a move that appeared in NO sampled world:
    the Volcarona/Quiver-Dance failure reached without any revenge lock.

    Tradeoff, stated: the plan is no longer deterministic, so two searches of
    the same position can sample different sets. That is the point -- the
    determinism WAS the defect -- but it means per-decision reproduction now
    needs the RNG seeded. Honest limit: num_battles worlds still cannot COVER
    more than num_battles sets on one decision; this fixes the persistence and
    the inflation, not the per-decision coverage.
    """
    if not sets:
        return []
    chance = 1.0 / num_battles
    total = float(sum(weights))
    if total <= 0:
        # no usable posterior: fall back to spreading the seats evenly
        return [(sets[i % len(sets)], chance) for i in range(num_battles)]

    start = random.random()
    plan = []
    cum_prev = 0.0
    for pkmn_set, weight in zip(sets, weights):
        cum = cum_prev + (weight / total) * num_battles
        seats = int(cum + start) - int(cum_prev + start)
        if seats > 0:
            plan.extend([(pkmn_set, chance)] * seats)
        cum_prev = cum
    # Cumulative float error can leave the plan a seat short or long; trim or
    # top up from the heaviest set so len(plan) == num_battles exactly.
    del plan[num_battles:]
    if len(plan) < num_battles:
        heaviest = sets[max(range(len(sets)), key=lambda k: weights[k])]
        plan.extend([(heaviest, chance)] * (num_battles - len(plan)))
    return plan


def prepare_random_battles(battle: Battle, num_battles: int) -> list[(Battle, float)]:
    revealed_pkmn_sets = get_all_remaining_sets_for_revealed_pkmn(deepcopy(battle))
    datasets = _datasets_for(battle)

    # STRATIFIED allocation of the worlds across the ACTIVE's candidate sets
    # (wave 2 item 5). The candidate list and its weights do not depend on the
    # world, so the split is decided once, up front, instead of being redrawn
    # independently `num_battles` times.
    active_plan = None
    root_active = battle.opponent.active
    if root_active is not None and revealed_pkmn_sets.get(root_active.name):
        # CERTAIN-REVENGE LOCK RETIRED (Sally 2026-08-20) -- see the banner on
        # `revenge_certain_sets`. The graded entry-intent weights below already
        # carry the "sent in to revenge-kill" signal, and carry it FLOORED AND
        # CAPPED, so they can shift the prior without ever zeroing the truth.
        plan_sets = revealed_pkmn_sets[root_active.name]
        plan_weights = entry_weighted_counts(root_active, plan_sets)
        # Plan around what the REST of the team is already forced to hold, so
        # the active cannot duplicate a hazard/removal move PS would have culled.
        forced = _forced_exclusive_moves(battle, revealed_pkmn_sets, skip=root_active)
        if forced:
            keep = [
                (s_, w) for s_, w in zip(plan_sets, plan_weights)
                if not forced.intersection(s_.pkmn_moveset.moves)
            ]
            if keep:      # else every candidate carries it -- PS bails too
                plan_sets = [k[0] for k in keep]
                plan_weights = [k[1] for k in keep]
        active_plan = _stratified_active_plan(plan_sets, plan_weights, num_battles)

    sampled_battles = []
    for index in range(num_battles):
        logger.info("Sampling battle {}".format(index))
        battle_copy = deepcopy(battle)

        sample_chance = 1 / num_battles
        active = battle_copy.opponent.active
        if revealed_pkmn_sets[active.name]:
            pkmn_full_set, sample_chance = active_plan[index]
            if getattr(active, "transformed_into", None):
                # a transformed mon's moves/stats/ability are COPIES that must
                # stay untouched; only its true item is worth sampling (the
                # matched sets are the base species', item-filtered - see
                # get_all_remaining_sets)
                if active.item == constants.UNKNOWN_ITEM:
                    active.item = pkmn_full_set.pkmn_set.item
                # ...and its TERA TYPE, which Transform does not copy in PS, so
                # the base species' (Ditto's) own tera type is the correct one.
                # `pkmn_full_set` is already the base species' set here -- the
                # information was present in the very object being sampled and
                # was simply dropped, leaving tera_type None so the serializer
                # defaulted it (poke_engine_helpers.py:384
                # `tera_type=pkmn.tera_type or "typeless"`). The engine's
                # typeless column is 1.0 against everything, so the search
                # priced the mon's tera arm as all-neutral AND STAB-less while
                # still offering it. Measured across all 1,520 archived ladder
                # games: 2,810 serializations carried tera_type=TYPELESS and
                # every single one was a transformed mon; reproduced live in
                # validation batch 20260820-155152 game g78, where a Ditto
                # copying Slowbro-Galar serialized TYPELESS in 24 worlds.
                if (
                    not active.terastallized
                    and not active.tera_type
                    and pkmn_full_set.pkmn_set.tera_type is not None
                ):
                    active.tera_type = pkmn_full_set.pkmn_set.tera_type
            else:
                populate_pkmn_from_set(active, pkmn_full_set)
        elif not getattr(active, "transformed_into", None):
            populate_pkmn_from_fallback_set(active, datasets)

        # Seeded with every mon's CONFIRMED moves so a mon that has actually
        # been seen using Stealth Rock locks it for the others, whatever order
        # the reserves happen to be walked in. These are the SAME objects the
        # loop populates, so each entry upgrades from confirmed-moves-only to
        # its full sampled set as it is drawn -- no re-append (a duplicate
        # reference would double-count the spikes counter).
        fixed_so_far = [p for p in battle_copy.opponent.reserve if p is not None]
        if battle_copy.opponent.active is not None:
            fixed_so_far = [battle_copy.opponent.active] + fixed_so_far

        # fainted reserves are included so that a pkmn revived by
        # Revival Blessing has a predicted set for the search
        for pkmn in battle_copy.opponent.reserve:
            if not revealed_pkmn_sets[pkmn.name]:
                if not getattr(pkmn, "transformed_into", None):
                    populate_pkmn_from_fallback_set(pkmn, datasets)
                continue
            # TEAM-AWARE draw: everything already fixed in THIS world (the
            # active's sampled set, earlier reserves, and every mon's genuinely
            # revealed moves) locks out the hazard/removal/screen moves PS
            # would have culled. Drawn independently, these produced teams the
            # generator cannot build.
            locked = _team_locked_moves(
                [p for p in fixed_so_far if p is not pkmn]
            )
            pkmn_full_set = _draw_set_respecting_team(
                pkmn,
                revealed_pkmn_sets[pkmn.name],
                entry_weighted_counts(pkmn, revealed_pkmn_sets[pkmn.name]),
                locked,
            )
            populate_pkmn_from_set(pkmn, pkmn_full_set)

        populate_randombattle_unrevealed_pkmn(battle_copy)
        battle_copy.opponent.lock_moves()
        sampled_battles.append((battle_copy, sample_chance))

    return sampled_battles


def sample_randombattle_pokemon(existing_pokemon: list[Pokemon]) -> Pokemon:
    def is_mega(pkmn: Pokemon):
        if normalize_name(pokedex.get(pkmn.name, {}).get("forme", "")).startswith(
            "mega"
        ):
            return True
        for mega_name, mega_item in pkmn.get_mega_pkmn_info():
            if pkmn.item == mega_item:
                return True

        return False

    existing_pokemon_names = {pkmn.name for pkmn in existing_pokemon}
    has_mega = any(is_mega(p) for p in existing_pokemon)
    use_showdown_constraints = not os.environ.get(SHOWDOWN_TEAM_CONSTRAINTS_CONTROL_OFF)

    sample_count = 0
    while True:
        sample_count += 1
        pkmn_name = random.choices(
            RandomBattleTeamDatasets.species_sample_names,
            weights=RandomBattleTeamDatasets.species_sample_weights,
        )[0]
        pkmn_sets = RandomBattleTeamDatasets.pkmn_sets[pkmn_name]
        pkmn_full_set = random.choices(
            pkmn_sets,
            weights=[s.pkmn_set.count for s in pkmn_sets],
        )[0]
        pkmn = Pokemon(pkmn_name, pkmn_full_set.pkmn_set.level)
        pkmn.ability = pkmn_full_set.pkmn_set.ability
        team = existing_pokemon + [pkmn]

        if use_showdown_constraints:
            ok = (
                pkmn_name not in existing_pokemon_names
                and not (is_mega(pkmn) and has_mega)
                and not _violates_showdown_team_constraints(team)
            )
        else:
            # Exact legacy path retained as the negative control: after the
            # tenth draw it knowingly gives up every constraint except names.
            ok = pkmn_name not in existing_pokemon_names
            if sample_count < 10 and is_mega(pkmn) and has_mega:
                ok = False
            if sample_count < 10 and _more_than_3_pokemon_weak_to_a_given_typing(
                team, use_team_building_types=False
            ):
                ok = False
            if sample_count < 10 and _more_than_1_species(
                team, use_team_building_names=False
            ):
                ok = False
            if sample_count < 10 and _more_than_2_pokemon_of_any_type(
                team, use_team_building_types=False
            ):
                ok = False
            if sample_count < 10 and _more_than_1_pokemon_with_4x_weakness(
                team, use_team_building_types=False
            ):
                ok = False

        if ok:
            break
        if use_showdown_constraints and sample_count == MAX_RANDOM_SAMPLING_ATTEMPTS:
            pkmn, pkmn_full_set = _sample_valid_randombattle_pokemon(
                existing_pokemon,
                existing_pokemon_names,
                has_mega,
                is_mega,
            )
            break

    populate_pkmn_from_set(pkmn, pkmn_full_set)

    # a sampled fill-in was never actually seen on the field
    # (Pokemon.__init__ already defaults revealed to False; this is
    # explicit because the search relies on it)
    pkmn.revealed = False
    return pkmn


#
# From P.S. documentation:
#
# Team generation currently uses this feature to prevent teams from having:
#   more than 1 species
#   more than 3 Pokemon weak to any given typing,
#   more than 2 Pokemon of any given type,
#   or more than 1 Pokemon that shares a 4x weakness
def _team_building_name(pkmn: Pokemon) -> str:
    name = getattr(pkmn, "base_name", pkmn.name)
    name = name if name in pokedex else pkmn.name
    # A COSMETIC forme carries no `baseSpecies` in pokedex.json, so the caller's
    # `pokedex[...].get("baseSpecies", name)` falls back to the forme itself and
    # Species Clause treats Florges-Blue and Florges-White as different species.
    # Same defect as the PS-sampler path fixed in ps_teams.Species; resolved
    # here too so the two samplers agree.
    return COSMETIC_FORME_TO_BASE.get(name, name)


def _team_building_types(pkmn: Pokemon) -> list[str]:
    # The generator reads species.types before applying its caps. Battle-time
    # Protean, Transform, and Double Shock types are irrelevant here.
    # pokemon-showdown/data/random-battles/gen9/teams.ts:1770-1808
    return pokedex[_team_building_name(pkmn)]["types"]


def _team_building_ability(pkmn: Pokemon) -> str:
    return normalize_name(pkmn.original_ability or pkmn.ability or "")


def _more_than_1_species(
    team: list[Pokemon], *, use_team_building_names: bool = True
) -> bool:
    if use_team_building_names:
        pkmn_species = {
            normalize_name(
                pokedex[_team_building_name(pkmn)].get(
                    "baseSpecies", _team_building_name(pkmn)
                )
            )
            for pkmn in team
        }
    else:
        pkmn_species = {pkmn.get_species() for pkmn in team}
    return len(pkmn_species) < len(team)


def _more_than_3_pokemon_weak_to_a_given_typing(
    team: list[Pokemon], *, use_team_building_types: bool = True
) -> bool:
    num_pkmn_weak_to_typing = {}
    for pkmn in team:
        types = _team_building_types(pkmn) if use_team_building_types else pkmn.types
        for t in POKEMON_TYPE_INDICES.keys():
            if is_super_effective(t, types):
                num_pkmn_weak_to_typing[t] = num_pkmn_weak_to_typing.get(t, 0) + 1

    if any(x > 3 for x in num_pkmn_weak_to_typing.values()):
        return True

    return False


def _more_than_2_pokemon_of_any_type(
    team: list[Pokemon], *, use_team_building_types: bool = True
) -> bool:
    num_of_each_type = {}
    for pkmn in team:
        types = _team_building_types(pkmn) if use_team_building_types else pkmn.types
        for pkmn_type in types:
            num_of_each_type[pkmn_type] = num_of_each_type.get(pkmn_type, 0) + 1

    if any(x > 2 for x in num_of_each_type.values()):
        return True

    return False


def _more_than_1_pokemon_with_4x_weakness(
    team: list[Pokemon], *, use_team_building_types: bool = True
) -> bool:
    num_of_each_4x_weakness = {}
    for pkmn in team:
        types = _team_building_types(pkmn) if use_team_building_types else pkmn.types
        for t in POKEMON_TYPE_INDICES.keys():
            if type_effectiveness_modifier(t, types) == 4:
                num_of_each_4x_weakness[t] = num_of_each_4x_weakness.get(t, 0) + 1

    if any(x > 1 for x in num_of_each_4x_weakness.values()):
        return True

    return False


def _more_than_4_pokemon_weak_to_freeze_dry(team: list[Pokemon]) -> bool:
    def weak_to_freeze_dry(pkmn: Pokemon) -> bool:
        types = _team_building_types(pkmn)
        ice_modifier = type_effectiveness_modifier("ice", types)
        return ice_modifier > 1 or ("water" in types and ice_modifier > 0.25)

    # pokemon-showdown/data/random-battles/gen9/teams.ts:1772-1775,1820-1824
    return sum(weak_to_freeze_dry(pkmn) for pkmn in team) > 4


def _more_than_3_pokemon_weak_to_fire_including_abilities(
    team: list[Pokemon],
) -> bool:
    def weak_to_fire(pkmn: Pokemon) -> bool:
        modifier = type_effectiveness_modifier("fire", _team_building_types(pkmn))
        return modifier > 1 or (
            modifier == 1 and _team_building_ability(pkmn) in _EXTRA_FIRE_WEAK_ABILITIES
        )

    # Dry Skin and Fluffy count only on the generated set when Fire is neutral.
    # pokemon-showdown/data/random-battles/gen9/teams.ts:1811-1818,1891-1894
    return sum(weak_to_fire(pkmn) for pkmn in team) > 3


def _more_than_1_level_100_pokemon(team: list[Pokemon]) -> bool:
    # pokemon-showdown/data/random-battles/gen9/teams.ts:1826-1829,1897-1898
    return sum(pkmn.level == 100 for pkmn in team) > 1


def _has_incompatible_pokemon(team: list[Pokemon]) -> bool:
    names = [_team_building_name(pkmn) for pkmn in team]
    for index, name in enumerate(names):
        for other_name in names[index + 1 :]:
            for group_a, group_b in _INCOMPATIBLE_POKEMON:
                if (name in group_a and other_name in group_b) or (
                    name in group_b and other_name in group_a
                ):
                    # pokemon-showdown/data/random-battles/gen9/teams.ts:1652-1717
                    # pokemon-showdown/data/random-battles/gen9/teams.ts:1831-1832
                    return True
    return False


def _violates_showdown_team_constraints(team: list[Pokemon]) -> bool:
    if (
        _more_than_1_species(team)
        or _more_than_3_pokemon_weak_to_a_given_typing(team)
        or _more_than_2_pokemon_of_any_type(team)
        or _more_than_1_pokemon_with_4x_weakness(team)
    ):
        return True
    if "gen9" not in RandomBattleTeamDatasets.pkmn_mode:
        return False
    return (
        _more_than_4_pokemon_weak_to_freeze_dry(team)
        or _more_than_3_pokemon_weak_to_fire_including_abilities(team)
        or _more_than_1_level_100_pokemon(team)
        or _has_incompatible_pokemon(team)
    )


def _sample_valid_randombattle_pokemon(
    existing_pokemon: list[Pokemon],
    existing_pokemon_names: set[str],
    has_mega: bool,
    is_mega,
):
    valid_candidates = []
    valid_weights = []
    for pkmn_name, species_weight in zip(
        RandomBattleTeamDatasets.species_sample_names,
        RandomBattleTeamDatasets.species_sample_weights,
    ):
        pkmn_sets = RandomBattleTeamDatasets.pkmn_sets[pkmn_name]
        total_set_weight = sum(pkmn_set.pkmn_set.count for pkmn_set in pkmn_sets)
        for pkmn_full_set in pkmn_sets:
            pkmn = Pokemon(pkmn_name, pkmn_full_set.pkmn_set.level)
            pkmn.ability = pkmn_full_set.pkmn_set.ability
            if (
                pkmn_name not in existing_pokemon_names
                and not (is_mega(pkmn) and has_mega)
                and not _violates_showdown_team_constraints(existing_pokemon + [pkmn])
            ):
                valid_candidates.append((pkmn, pkmn_full_set))
                valid_weights.append(
                    species_weight * pkmn_full_set.pkmn_set.count / total_set_weight
                )

    if not valid_candidates:
        raise ValueError("No valid random-battle Pokemon can complete this team")
    return random.choices(valid_candidates, weights=valid_weights)[0]


def _ps_sampler_enabled() -> bool:
    # gen9randombattle(+blitz) only: ps_teams is a port of the gen9 generator.
    # Battle factory and other dataset modes keep the marginal sampler.
    return (
        os.environ.get(PS_TEAM_SAMPLER_CONTROL_OFF) != "1"
        and "gen9randombattle" in (getattr(RandomBattleTeamDatasets, "pkmn_mode", "") or "")
    )


def _pokemon_from_ps_set(ps_set: dict) -> Pokemon:
    """PS random_team set dict -> a foul-play Pokemon, mirroring what
    populate_pkmn_from_set does for dataset sets. EVs/IVs are PS's own
    (HP shaving, Atk zeroing), so the fill-in's stats are the generator's."""
    evs = tuple(ps_set["evs"][k] for k in ("hp", "atk", "def", "spa", "spd", "spe"))
    ivs = tuple(ps_set["ivs"][k] for k in ("hp", "atk", "def", "spa", "spd", "spe"))
    # BATTLE-ONLY FORMES (Sally 2026-08-20). ps_set["species"] is getForme's
    # DISPLAY name, and for a battle-only forme that is its BASE -- getForme
    # returns species.battleOnly (_ps_team_loop.py:347-350, ported from
    # teams.ts:1449-1451). Naming the mon from it silently downgrades the
    # forme: the CROWNED set (Rusted Sword, Behemoth Blade, level 64) lands on
    # base Zacian, which is mono-Fairy with 120 Atk instead of Fairy/Steel with
    # 150. `speciesId` is the forme actually drawn, the pokedex and the engine
    # both carry ZACIANCROWNED/ZAMAZENTACROWNED, and it is also how a REVEALED
    # opponent of the same mon already arrives (via PS's detailschange) -- so
    # this is what makes the sampled and revealed copies agree.
    # Measured before the fix, validation run 20260820-141359: 2/1680 sampled
    # mon-instances were a Crowned set wearing the base forme's stats.
    # Terapagos needs nothing here: poke-engine does Tera Shift itself.
    name = normalize_name(ps_set["species"])
    sid = normalize_name(ps_set.get("speciesId") or "")
    if sid and sid != name and sid in pokedex:
        from fp.search import ps_teams

        if getattr(ps_teams.get_species(sid), "battleOnly", None):
            name = sid
    if name not in pokedex:
        name = ps_set["speciesId"]
    pkmn = Pokemon(name, ps_set["level"], evs=evs, ivs=ivs)
    pkmn.ability = normalize_name(ps_set["ability"]) if ps_set["ability"] else None
    # '' means the generator assigned NO item (item is None is knowledge,
    # not ignorance -- see battle_to_poke_engine_state)
    pkmn.item = normalize_name(ps_set["item"]) if ps_set["item"] else None
    pkmn.moves = []
    for move_id in ps_set["moves"]:
        pkmn.add_move(move_id)
    if ps_set.get("teraType"):
        pkmn.tera_type = normalize_name(ps_set["teraType"])
    # a sampled fill-in was never actually seen on the field
    pkmn.revealed = False
    return pkmn


def _ps_fill_ins(existing_pkmn: list[Pokemon], n_missing: int) -> list[Pokemon]:
    from fp.search import ps_teams

    existing = [
        {
            "speciesId": pkmn.name,
            "ability": pkmn.ability or "",
            "moves": [m.name for m in pkmn.moves],
            "level": pkmn.level,
        }
        for pkmn in existing_pkmn
    ]
    fill_sets = ps_teams.complete_team(existing)
    if len(fill_sets) != n_missing:
        raise ValueError(
            "PS sampler returned {} fill-ins, wanted {}".format(
                len(fill_sets), n_missing
            )
        )
    fills = []
    for ps_set in fill_sets:
        pkmn = _pokemon_from_ps_set(ps_set)
        log_pkmn_set(pkmn, source="ps-team-sampler")
        fills.append(pkmn)
    return fills


# take a Battle and fill in the unrevealed pkmn for the opponent
def populate_randombattle_unrevealed_pkmn(battle: Battle):
    num_revealed_pkmn = 0
    existing_pkmn = []
    for pkmn in battle.opponent.reserve:
        existing_pkmn.append(pkmn)
        num_revealed_pkmn += 1
    if battle.opponent.active is not None:
        existing_pkmn.append(battle.opponent.active)
        num_revealed_pkmn += 1

    if num_revealed_pkmn == 6:
        return

    logger.info("Sampling {} unrevealed pokemon".format(6 - num_revealed_pkmn))

    # PS-EXACT conditional completion: seed PS's sequential team-builder state
    # from the revealed mons and generate the remaining slots with the real
    # generator (Species Clause, type/weakness caps, cullMovePool hazard
    # dedup). The marginal sampler below stays as fallback and as the
    # FP_CONTROL_NO_PS_TEAM_SAMPLER=1 negative control.
    if _ps_sampler_enabled():
        try:
            fills = _ps_fill_ins(existing_pkmn, 6 - num_revealed_pkmn)
            battle.opponent.reserve.extend(fills)
            return
        except Exception:
            logger.warning(
                "PS-exact team sampler failed; falling back to the marginal "
                "sampler", exc_info=True
            )

    while num_revealed_pkmn < 6:
        pkmn = sample_randombattle_pokemon(existing_pkmn)
        existing_pkmn.append(pkmn)
        battle.opponent.reserve.append(pkmn)
        num_revealed_pkmn += 1
