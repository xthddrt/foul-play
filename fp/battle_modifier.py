import os
import re
import json
from collections import Counter
from copy import deepcopy, copy
import logging

import constants
from constants import BattleType
from data import all_move_json
from data import pokedex
from data.pkmn_sets import (
    SmogonSets,
    RandomBattleTeamDatasets,
    TeamDatasets,
    PredictedPokemonSet,
)
from fp.battle import Pokemon, Battler, Battle
from fp.battle import base_species_name
from fp.battle import LastUsedMove
from fp.battle import DamageDealt
from fp.battle import StatRange
from fp.search.poke_engine_helpers import (
    poke_engine_get_damage_rolls,
    poke_engine_get_damage_roll_sets,
)
from fp.helpers import (
    normalize_name,
    type_effectiveness_modifier,
    random_battles_evs,
    maximum_ev,
)
from fp.helpers import get_pokemon_info_from_condition
from fp.helpers import calculate_stats
from fp.helpers import (
    is_not_very_effective,
    is_super_effective,
    is_neutral_effectiveness,
)
from fp.battle import boost_multiplier_lookup
from fp import hp_certificate
from fp.inference import (
    MOVE_END_STRINGS,
    can_have_priority_modified,
    can_have_speed_modified,
    is_opponent,
    get_move_information,
    check_speed_ranges,
    check_opponent_hiddenpower,
    check_choicescarf,
    get_damage_dealt,
    update_dataset_possibilities,
    check_heavydutyboots,
)


logger = logging.getLogger(__name__)

ITEMS_REVEALED_ON_SWITCH_IN = [
    # boosterenergy technically only revealed if pkmn has quarkdrive/protosynthesis
    # but if they don't have that it doesn't matter
    "boosterenergy",
    "airballoon",
]
ABILITIES_REVEALED_ON_SWITCH_IN = [
    "intimidate",
    "pressure",
    "neutralizinggas",
    "sandstream",
    "drought",
    "drizzle",
    "snowwarning",
]

# abilities whose |cant| line names the ABILITY HOLDER rather than the pokemon
# that failed to act, with the blocked pokemon in the trailing `[of]` tag:
#     this.add('cant', <holder>, 'ability: <Name>', move, `[of] ${blocked}`)
# data/abilities.ts:225 (Armor Tail), :805 (Damp), :864 (Dazzling),
# :3716 (Queenly Majesty). Truant (:5183) is deliberately excluded - it names
# the pokemon that cannot move and has no `[of]`.
ABILITIES_THAT_BLOCK_ANOTHER_POKEMONS_MOVE = {
    "armortail",
    "damp",
    "dazzling",
    "queenlymajesty",
}

# gen9 trapping abilities: the only three that can set `pokemon.trapped` on a
# foe (data/abilities.ts:197 arenatrap, :2507 magnetpull, :4147 shadowtag -
# each via onFoeTrapPokemon -> pokemon.tryTrap(true))
TRAPPING_ABILITIES = {
    "arenatrap",
    "magnetpull",
    "shadowtag",
}

# effects on the BOT's own active that trap it without any foe ability being
# involved. If any of these is present when PS rejects our switch, the
# rejection proves nothing about the opponent's ability.
#   - trapped:          Mean Look / Block / Spider Web / Anchor Shot /
#                       Spirit Shackle / Jaw Lock / Thousand Waves
#                       (data/conditions.ts:208-217)
#   - partiallytrapped: Wrap / Fire Spin / Infestation / ... (:246-251)
#   - ingrain (data/moves.ts:9630), noretreat (:12810), octolock (:12991)
#   - commanded / commanding: Dondozo+Tatsugiri (data/conditions.ts:820, :834 -
#     these even beat Shed Shell)
#   - skydrop: the carried pokemon (data/moves.ts:16736)
#   - any hard move lock (Outrage/recharge/two-turn charge): PS sets
#     `this.trapped = true` whenever getLockedMove() is truthy
#     (sim/pokemon.ts:1090-1093)
SELF_TRAPPING_VOLATILES = {
    "trapped",
    constants.PARTIALLY_TRAPPED,
    "ingrain",
    "noretreat",
    "octolock",
    "commanded",
    "commanding",
    "skydrop",
    constants.LOCKED_MOVE,
    "mustrecharge",
    "bide",
    "uproar",
    "rollout",
    "iceball",
}

# gens whose typechart has no `trapped` entry for Ghost, i.e. where a Ghost-type
# can still be trapped by Arena Trap / Shadow Tag / Magnet Pull
# (data/mods/gen5/typechart.ts:24-44 vs data/typechart.ts:202-204)
GENS_WITHOUT_GHOST_TRAP_IMMUNITY = {"gen1", "gen2", "gen3", "gen4", "gen5"}

SIDE_CONDITION_DEFAULT_DURATION = {
    constants.REFLECT: 5,
    constants.LIGHT_SCREEN: 5,
    constants.AURORA_VEIL: 5,
    constants.SAFEGUARD: 5,
    constants.MIST: 5,
    constants.TAILWIND: 4,
}

# moves whose HP cost shows up as a bare '-damage' line (no '[from]' tag) on the
# USER. PS only counts timesAttacked for damage dealt by another pokemon's move
# (sim/battle-actions.ts:990-996: `pokemon !== target` guards the increment), so
# these self-cost lines must not count as being attacked. Curse only costs HP
# for a Ghost-type user; non-ghost Curse emits boost lines and no '-damage'.
SELF_COST_DAMAGE_MOVES = {
    "substitute",
    "bellydrum",
    "curse",
    "filletaway",
    "shedtail",
    "clangoroussoul",
}



# --- opponent-model ledger -------------------------------------------------
OPPONENT_LEDGER_PATH = "/Users/sallyliu/pokemon-fast-bot/opponent_ledger.jsonl"


def record_opponent_action(battle, event, actual):
    """Join the search's opponent prediction with what the opponent actually
    did and append to the ledger. Ground truth for opponent-model calibration
    (switch keeps, veto gaps, entry intent). Rows with pred=None mean no
    search preceded the action (e.g. first turn, mid-turn forced switches).
    """
    try:
        from fp.search import selection

        pred = selection.last_opponent_prediction
        stale = (
            not pred
            or pred.get("battle_tag") != getattr(battle, "battle_tag", None)
            or pred.get("turn") != battle.turn
        )
        row = {
            "battle_tag": getattr(battle, "battle_tag", None),
            "turn": battle.turn,
            "event": event,
            "actual": actual,
            "opp_active": getattr(battle.opponent.active, "name", None),
            "opp_name": getattr(battle.opponent, "account_name", None),
            "opp_elo": getattr(battle.opponent, "account_rating", None),
            "our_active": getattr(battle.user.active, "name", None),
            "pred": None if stale else pred.get("dist"),
            "our_choice": None if stale else pred.get("our_choice"),
            "our_mcts_score": None if stale else pred.get("our_mcts_score"),
        }
        with open(OPPONENT_LEDGER_PATH, "a") as f:
            f.write(json.dumps(row) + "\n")
    except Exception:
        logger.debug("opponent ledger write failed", exc_info=True)



def crit_rate_for_generation(generation):
    if generation == "gen1":
        return 205 / 105
    elif generation in [
        "gen2",
        "gen3",
        "gen4",
        "gen5",
    ]:
        return 2.0
    else:
        return 1.5


def remove_volatile(pkmn, volatile):
    pkmn.volatile_statuses = [vs for vs in pkmn.volatile_statuses if vs != volatile]


def unlikely_to_have_choice_item(move_name):
    try:
        move_dict = all_move_json[move_name]
    except KeyError:
        return False

    if (
        constants.BOOSTS in move_dict
        and move_dict[constants.CATEGORY] == constants.STATUS
    ):
        return True
    elif move_name in ["substitute", "roost", "recover"]:
        return True

    return False


def _side_from_pokemon_identifier(battle, identifier):
    """Resolve a 'p1a: Nickname' protocol identifier to the Battler it belongs
    to. Returns None when the identifier is missing/unrecognised."""
    identifier = identifier.strip()
    if not identifier:
        return None
    if identifier.startswith(battle.user.name):
        return battle.user
    if identifier.startswith(battle.opponent.name):
        return battle.opponent
    return None


def _side_of_of_tag(battle, split_msg):
    """The side named by a trailing `[of] p1a: X` tag, or None if absent."""
    for msg in split_msg:
        if msg.startswith("[of]"):
            return _side_from_pokemon_identifier(battle, msg[len("[of]") :])
    return None


def zoroark_inference_allowed(battle) -> bool:
    """Whether the live-play "the thing in front of me must secretly be a
    Zoroark" heuristics may fire.

    Those heuristics (in `move` and `immune`) exist because live play never
    learns the opponent's roster: when the active does something its randbats
    set cannot explain, inventing a Zoroark and appending it to the reserve is
    the best available guess.  The replay checker runs with the synthetic
    corpus's exact-teams sidecar, which IS the roster, and it resolves Illusion
    spans itself from that sidecar (fp/replay/checker.py `_infer_illusion_spans`)
    -- so here the guess is strictly worse than the ground truth and actively
    destructive: `RandomBattleTeamDatasets` is a stale snapshot of the randbats
    sets the corpus was generated from, so ordinary moves (Hatterene's Encore,
    Espeon's Will-O-Wisp) read as impossible and conjure a Zoroark that is not
    on the team, replacing the real active and inflating the bench.

    `Battle.exact_roster_known` is set only by the checker when a sidecar is
    loaded; live play never sets it and keeps the heuristics."""
    return not getattr(battle, "exact_roster_known", False)


def _sidecar_species_moveset(battle, side, species):
    """`species`'s TRUE 4 moves on `side` from the checker's exact-teams sidecar.

    Keyed by SPECIES NAME, not by a reconstructed Pokemon object: the sidecar
    knows the whole team from turn 0, while `side.reserve` only ever holds the
    mons the protocol has already revealed.  An Illusion bearer that never takes
    a damaging hit is never revealed at all (PS only prints `|replace|` from the
    ability's onEnd, data/abilities.ts:2066-2077), so requiring a reserve object
    before consulting its moveset throws away ground truth already in hand.

    None means "no ground truth here" (live play, or a mon the sidecar has no
    entry for) and every caller must then leave the reconstruction's own
    inference alone."""
    if not species:
        return None
    exact_teams = getattr(battle, "exact_teams", None)
    if exact_teams is None:
        return None
    team = getattr(exact_teams, "value", exact_teams).get(side.name) or {}
    record = team.get(species)
    if not record:
        return None
    return {normalize_name(m) for m in (record.get("moves") or ())} or None


def _sidecar_moveset(battle, side, pkmn):
    """`pkmn`'s TRUE 4 moves from the checker's exact-teams sidecar, or None."""
    if pkmn is None:
        return None
    return _sidecar_species_moveset(battle, side, pkmn.name)


def _record_used_move(battle, side, pkmn, move_name):
    """Give a just-used move to the pokemon that really owns it.

    Illusion is the only gen9-randbats mechanism that makes the protocol print a
    |move| line against the wrong species: PS renders the actor through
    `toString()`, which returns the illusion's name (sim/pokemon.ts:531).  LIVE
    play has to accept the misattribution - with imperfect information, crediting
    the move to the species shown IS the best available inference until the
    disguise breaks, and that is what the `zoroark_inference_allowed` heuristics
    below fall back on.  The replay checker is not in that position: its
    exact-teams sidecar fixes every mon's 4 moves for the whole battle, so a move
    the shown species provably does not own cannot have come from it, and the
    only mon that can be standing there under another name is that side's
    Illusion bearer.  Credit the bearer and leave the disguise's own moveset
    intact.  Anything the bearer's true moveset does NOT explain either falls
    through to the old append (and its "More than 4 moves" warning), so a
    misattribution from some other cause stays loud.

    Without this the disguise accumulated a 5th/6th move and
    `poke_engine_helpers` truncated the list to the first 4 - harmless only while
    the phantoms happen to sort last.  They frequently do not: opponent-side
    moves are learned in REVEAL order and the sidecar top-up
    (`damage_membership.apply_exact_team`) stops once the list reaches 4, so a
    Volcanion that a Zoroark-Hisui had lent Hyper Voice and Poltergeist to went
    into the engine holding those two plus only two of its own four moves.

    Returns the Move recorded on `pkmn`, or None when nothing was recorded there.
    """
    true_moves = _sidecar_moveset(battle, side, pkmn)
    if (
        true_moves is not None
        and move_name not in true_moves
        and not getattr(pkmn, "transformed_into", None)
        and "transform" not in pkmn.volatile_statuses
    ):
        # The bearer is identified from the SIDECAR, not from `side.reserve`.
        # PS only announces a broken Illusion (`|replace|`) when the disguise
        # takes a damaging hit, so a Zoroark that ends its disguise by SWITCHING
        # OUT is never revealed, has no reserve object to find, and the old
        # `find_pokemon_in_reserves` lookup silently gave up and appended the
        # phantom to the disguise (synth79068: p2's unrevealed Zoroark-Hisui lent
        # Will-O-Wisp to Sandaconda; synth71462: an unrevealed Zoroark lent Nasty
        # Plot AND Dark Pulse to Appletun, pushing Appletun's real Recover out of
        # the 4 slots poke_engine_helpers reads).
        # The PROOF is unchanged and still needs both halves - the sidecar says
        # the shown species cannot own the move AND says that side's Illusion
        # bearer can - so a misattribution from any other cause still falls
        # through to the append and stays loud.
        for bearer_name in ("zoroark", "zoroarkhisui"):
            bearer_moves = _sidecar_species_moveset(battle, side, bearer_name)
            if bearer_moves is None or move_name not in bearer_moves:
                continue
            bearer = side.find_pokemon_in_reserves(bearer_name)
            if bearer is None:
                # Not revealed yet, so there is no object to credit.  Dropping is
                # right: the disguise's own 4 moves still come from the sidecar
                # top-up, and the bearer gets its true moveset the same way if it
                # is ever reconstructed.
                logger.info(
                    "{} cannot own {} - it belongs to the still-unrevealed {}; "
                    "dropping it rather than crediting the disguise".format(
                        pkmn.name, move_name, bearer_name
                    )
                )
            else:
                logger.info(
                    "{} cannot own {} - crediting the disguised {} instead".format(
                        pkmn.name, move_name, bearer.name
                    )
                )
                if bearer.get_move(move_name) is None:
                    bearer.add_move(move_name)
            return None
    return pkmn.add_move(move_name)


def _sidecar_species_record(battle, side, species):
    """`species`'s whole TRUE set on `side` from the checker's exact-teams
    sidecar, or None when there is no ground truth (live play, or a species the
    sidecar has no entry for).  Keyed by SPECIES NAME for the same reason
    `_sidecar_species_moveset` is: an Illusion bearer that never takes a
    damaging hit is never revealed and so has no reserve object to look up."""
    if not species:
        return None
    ref = getattr(battle, "exact_teams", None)
    exact_teams = getattr(ref, "value", ref)  # fp.battle.SharedByReference
    if not exact_teams:
        return None
    return (exact_teams.get(side.name) or {}).get(species)


def _sidecar_species_item(battle, side, species):
    """`species`'s TRUE held item, normalized; None when the set holds nothing
    OR when there is no ground truth -- callers must first establish that a
    record exists via `_sidecar_species_record`."""
    record = _sidecar_species_record(battle, side, species)
    if not record:
        return None
    return normalize_name(record["item"]) if record.get("item") else None


def _record_revealed_item(battle, side, pkmn, item):
    """Give a protocol-revealed item to the pokemon that really holds it.

    The ITEM cousin of `_record_used_move`, with the same proof shape.  Illusion
    makes PS print every line about the bearer against the species it is SHOWING
    (`toString()`, sim/pokemon.ts:531), and that includes every item fact: the
    Life Orb recoil chip, the Leftovers/Black Sludge heal, the Rocky Helmet
    `[of]` attribution, the Toxic Orb status, Poltergeist / Frisk / Air Balloon
    reveals, and the `-enditem` of a consumed berry or a Knocked Off item.  LIVE
    play must accept the misattribution - with imperfect information the shown
    species IS the best available inference - so with no sidecar this returns
    `pkmn` unchanged and nothing about real play moves.  The replay checker's
    exact-teams sidecar fixes every mon's held item for the whole battle, so an
    item the shown species provably does not hold cannot be its, and the only
    mon that can be standing there under another name is that side's Illusion
    bearer.

    Without this the leak OUTLIVES the disguise.  `illusion_end` already moves
    the item across on a `|replace|`, but PS only prints `|replace|` when the
    disguise takes a damaging hit (data/abilities.ts:2066-2077): a Zoroark that
    ends its disguise by SWITCHING OUT is never announced, and the reconstruction
    keeps ONE object per species -- the same object that is the real party member
    of the impersonated species.  synth73774: p2's Zoroark-Hisui spent turns 7-19
    as Glimmora, so its two Life Orb recoil chips set the tracked "glimmora"
    item to Life Orb; that is a protocol REVEAL, which `apply_exact_team`
    (fp/replay/damage_membership.py:2262-2267) correctly refuses to overwrite
    with the sidecar item, so when the REAL Glimmora came in on T19 and burned
    its Power Herb on Meteor Beam the engine state said it held Life Orb and no
    branch could consume a Power Herb.

    Returns the pokemon the item statement really concerns (usually `pkmn`), or
    None when it concerns a still-unrevealed Illusion bearer and must be dropped.
    """
    if pkmn is None or pkmn.item == item:
        # The reconstruction already independently knows this mon holds it --
        # e.g. a Trick/Bestow GAIN that `set_item` recorded.  A gained item is
        # absent from every sidecar set (which are start-of-battle), so without
        # this guard the later `-enditem` of it would read as unownable.
        return pkmn
    shown = _sidecar_species_record(battle, side, pkmn.name)
    if shown is None or _sidecar_species_item(battle, side, pkmn.name) == item:
        return pkmn
    # The PROOF needs both halves - the sidecar says the shown species cannot
    # hold the item AND says that side's Illusion bearer does - so an item
    # statement misattributed by any other cause still falls through unchanged.
    for bearer_name in ("zoroark", "zoroarkhisui"):
        if _sidecar_species_record(battle, side, bearer_name) is None:
            continue
        if _sidecar_species_item(battle, side, bearer_name) != item:
            continue
        bearer = side.find_pokemon_in_reserves(bearer_name)
        if bearer is None:
            # Not revealed yet, so there is no object to credit.  Dropping is
            # right: the disguise keeps whatever it had (the sidecar top-up
            # then fills its real item), and the bearer gets its true item the
            # same way if it is ever reconstructed.
            logger.info(
                "{} cannot hold {} - it belongs to the still-unrevealed {}; "
                "dropping it rather than crediting the disguise".format(
                    pkmn.name, item, bearer_name
                )
            )
        else:
            logger.info(
                "{} cannot hold {} - crediting the disguised {} instead".format(
                    pkmn.name, item, bearer.name
                )
            )
        return bearer
    return pkmn


def _ident_is_slotless(identifier: str) -> bool:
    """True when a protocol POKEMON id names a BENCHED pokemon.

    sim/SIM-PROTOCOL.md:166-172 - "A Pokemon ID is in the form `POSITION:
    NAME`. POSITION is the spot that the Pokemon is in ... An inactive Pokemon
    will not have a position letter."  sim/pokemon.ts:531-533 `toString()`
    implements exactly that: `this.isActive ? this.getSlot() +
    fullname.slice(2) : fullname`, where `fullname` is the bare `p2: Nickname`.
    So in singles `p2a: X` is always the active and `p2: X` is always someone
    on the bench - Heal Bell / Aromatherapy curing a benched ally
    (data/moves.ts:8252-8268 healbell onHit walks `target.side.pokemon` and
    calls `ally.cureStatus()`, which prints `-curestatus` against that ally,
    sim/pokemon.ts:1680-1682) or Revival Blessing reviving one
    (sim/battle.ts:2778-2793)."""
    position = identifier.split(":")[0].strip()
    return bool(position) and not position[-1].isalpha()


def _names_match(pkmn, protocol_name: str) -> bool:
    """Whether `pkmn` answers to the NAME half of a protocol id, allowing for the
    id carrying the base species while the object holds the forme."""
    name = normalize_name(protocol_name)
    return name in (
        pkmn.name,
        pkmn.base_name,
        normalize_name(pokedex.get(pkmn.name, {}).get("baseSpecies", "") or ""),
    )


def _find_bench_pokemon_by_protocol_name(side, protocol_name: str):
    """Resolve the NAME half of a slotless protocol id to a benched pokemon.

    That name is the pokemon's nickname, which in gen9 random battles is the
    BASE species (`data/random-battles/gen9/teams.ts:1598` and siblings set
    `name: species.baseSpecies`), so a benched Rotom-Fan is announced as
    `p2: Rotom` while the reconstructed reserve holds `rotomfan`.  Match the
    reserve entry's own name/forme first, then its base species; an ambiguous
    match (impossible under Species Clause) refuses rather than guesses."""
    name = normalize_name(protocol_name)
    exact = side.find_pokemon_in_reserves(name)
    if exact is not None:
        return exact
    # A COSMETIC forme (Gastrodon-East, Alcremie-Rainbow-Swirl) has no pokedex
    # entry of its own: PS synthesises it from the base species' `cosmeticFormes`
    # list and sets `baseSpecies` on the fly (sim/dex-species.ts:464-480), and the
    # bundled pokedex simply aliases the forme id to the BASE entry -- which has
    # no `baseSpecies` key but whose `name` IS the base species.  Reading both
    # keys resolves `p1: Gastrodon` to the tracked `gastrodoneast`
    # (synth95074 Heal Bell, synth94787 Alcremie).
    matches = [
        p
        for p in side.reserve
        if name
        in (
            normalize_name(pokedex.get(p.name, {}).get("baseSpecies", "") or ""),
            normalize_name(pokedex.get(p.name, {}).get("name", "") or ""),
        )
    ]
    return matches[0] if len(matches) == 1 else None


# PS re-enters runMove on every turn of a multi-turn lock (Outrage/Thrash/
# Petal Dance/Uproar/Rollout/Bide, and the RELEASE turn of every two-turn
# charge move) and tags that |move| line with the `lockedmove` condition:
# sim/battle-actions.ts:279-291
#     if (!externalMove) {
#         const lockedMove = pokemon.getLockedMove();
#         if (!lockedMove) { ...deductPP... } else {
#             sourceEffect = this.dex.conditions.get('lockedmove');
#         }
#     }
# and sim/battle-actions.ts:451 `if (sourceEffect) attrs += '|[from] ' +
# sourceEffect.fullname` (fullname of the lockedmove condition is the bare
# string 'lockedmove' - sim/dex-data.ts:116). PS emits the space-separated
# form; parts of the ecosystem (and older logs) drop the space, so accept both.
def is_lockedmove_continuation(split_msg):
    return any(msg in ("[from]lockedmove", "[from] lockedmove") for msg in split_msg)


# a `[from]` tag that is NOT the lockedmove marker means the |move| line was
# produced inside useMove (Sleep Talk/Copycat/Magic Bounce/... ) rather than by
# the pokemon's own runMove action
def has_non_lockedmove_from_tag(split_msg):
    return any(
        "[from]" in msg and msg not in ("[from]lockedmove", "[from] lockedmove")
        for msg in split_msg
    )


def request(battle, split_msg):
    if len(split_msg) >= 2:
        battle_json = json.loads(split_msg[2].strip("'"))
        logger.debug("Received battle JSON from server: {}".format(battle_json))
        battle.rqid = battle_json[constants.RQID]

        if battle_json.get(constants.FORCE_SWITCH):
            battle.force_switch = True
        else:
            battle.force_switch = False

        if battle_json.get(constants.WAIT):
            battle.wait = True
        else:
            battle.wait = False

        battle.request_json = battle_json


def inactive(battle, split_msg):
    regex_string = r"(\d+) sec this turn"
    if split_msg[2].startswith(constants.TIME_LEFT):
        capture = re.search(regex_string, split_msg[2])
        try:
            time_left = int(capture.group(1))
            battle.time_remaining = time_left
            logger.debug("Time left: {}".format(time_left))
        except ValueError:
            logger.warning("{} is not a valid int".format(capture.group(1)))
        except AttributeError:
            logger.warning(
                "'{}' does not match the regex '{}'".format(split_msg[2], regex_string)
            )


def inactiveoff(battle, _):
    battle.time_remaining = None


def user_just_switched_into_zoroark(battle, switch_or_drag):
    """
    some truly heinous shit going on here, can we ban this fucker?

    Two scenarios we can detect we are a zoroark:
      1. We switched and the last action we selected starts with `switch zoroark` (to account for both zoroarks)
      2. We were dragged (circle throw, etc) AND the active pkmn on the next turn is zoroark

    is it not sound to check for "we switched or dragged and the request JSON has zoroark as active?"
    No. If we switched into zoroark and then got circle-thrown out then the request JSON would not have
        zoroark as active but our switch needs to have been into zoroark.

    This doesn't need to deal with the first-turn switch-in of the user's Zoroark because the first-turn is
    instantiated from the request_json
    """

    return (
        # Scenario 1
        (
            switch_or_drag == "switch"
            and battle.user.last_selected_move.move.startswith("switch zoroark")
        )
        # Scenario 2
        or (
            switch_or_drag == "drag"
            and battle.request_json is not None
            and battle.request_json[constants.SIDE][constants.POKEMON][0][
                constants.DETAILS
            ].startswith("Zoroark")
            and battle.request_json[constants.SIDE][constants.POKEMON][0][
                constants.ACTIVE
            ]
        )
    )


def _switch_out_ability(battle, side, side_name):
    """The outgoing mon's ability for SWITCH-OUT purposes, widened by the
    full-knowledge sidecar the replay checker attaches (`battle.exact_teams`)
    whenever live tracking has revealed none.

    Regenerator is SILENT in the protocol -- PS emits no line for it -- so a mon
    that leaves at 5% and comes back at 38% reveals nothing until it is already
    back on the field.  With the ability still None the +1/3 heal below is
    skipped, and the turn-start reconstruction of the turn it RETURNS on hands
    the engine a mon a third of its max HP too low: synth76563 T30 put Slowbro at
    14/300 instead of 114/300, so the Stealth Rock it really survived killed it
    on entry, and the Draco Meteor that actually KO'd it -- together with that
    move's own -2 SpA self-drop -- existed in no branch.  The sidecar is already
    the authority the rest of the checker replays with (it is applied to every
    per-turn snapshot, and to the Substitute derivation's deepcopy at
    :2203-2209); this makes the LIVE walk agree with it on the one silent
    mechanic that changes HP.

    On the live ladder there is no sidecar, so this returns fp's tracked ability
    unchanged and nothing about real play moves."""
    pkmn = side.active
    if pkmn is None:
        return None
    if pkmn.ability:
        return pkmn.ability
    ref = getattr(battle, "exact_teams", None)
    exact_teams = getattr(ref, "value", ref)  # fp.battle.SharedByReference
    if not exact_teams:
        return pkmn.ability
    try:
        from fp.replay.damage_membership import _match_exact_mon

        pid = battle.opponent.name if side_name == "opponent" else battle.user.name
        rec = _match_exact_mon(exact_teams.get(pid) or {}, pkmn.name)
    except Exception:
        return pkmn.ability
    if rec and rec.get("ability"):
        return normalize_name(rec["ability"])
    return pkmn.ability


def switch(battle, split_msg):
    switch_or_drag(battle, split_msg, switch_or_drag="switch")


def drag(battle, split_msg):
    switch_or_drag(battle, split_msg, switch_or_drag="drag")


def switch_or_drag(battle, split_msg, switch_or_drag="switch"):
    if is_opponent(battle, split_msg):
        side_name = "opponent"
        side = battle.opponent
        other_side = battle.user
        logger.info("Opponent has switched - clearing the last used move")
        prev_alive = side.active is not None and side.active.hp > 0
        record_opponent_action(
            battle,
            "switch" if (switch_or_drag == "switch" and prev_alive) else
            ("forced_switch" if switch_or_drag == "switch" else "drag"),
            "switch " + normalize_name(split_msg[2].split(":")[-1].strip()),
        )
    else:
        side_name = "user"
        side = battle.user
        other_side = battle.opponent
        side.side_conditions[constants.TOXIC_COUNT] = 0

    baton_passed_boosts = None
    switch_keep_volatiles = []
    # the transferred substitute HP INTERVAL (low, high), or None
    passed_substitute_health = None
    if side.active is not None:
        # PS reverts NON-permanent forme changes when the pokemon leaves the
        # field: clearVolatile ends with setSpecies(this.baseSpecies)
        # (sim/pokemon.ts:1514-1565), so Meloetta-Pirouette re-enters as plain
        # Meloetta with base types/stats. Only formes entered via
        # |-formechange| carry this flag (see form_change); a permanent
        # |detailschange| forme (Terapagos-Stellar, Mimikyu-Busted, ...)
        # reassigned baseSpecies and stays. `battleOnly` in the dex names the
        # forme's out-of-battle species; fall back to base_name when absent.
        # Faint replacements pass through here too, matching PS's
        # clearVolatile on faint.
        if getattr(side.active, "reverts_forme_on_switch_out", False):
            reverted_forme = pokedex[side.active.name].get("battleOnly")
            if not isinstance(reverted_forme, str):
                reverted_forme = side.active.base_name
            reverted_forme = normalize_name(reverted_forme)
            if reverted_forme in pokedex and reverted_forme != side.active.name:
                logger.info(
                    "Reverting {} to {} on switch-out".format(
                        side.active.name, reverted_forme
                    )
                )
                side.active.name = reverted_forme
                # protocol-derived identity: this object has now also been
                # tracked under its reverted forme name
                side.active.forme_lineage.add(reverted_forme)
                side.active.base_stats = pokedex[reverted_forme][constants.BASESTATS]
                side.active.stats = calculate_stats(
                    side.active.base_stats,
                    side.active.level,
                    ivs=getattr(side.active, "ivs", (31,) * 6),
                    nature=side.active.nature,
                    evs=side.active.evs,
                )
                side.active.types = list(pokedex[reverted_forme][constants.TYPES])
            side.active.reverts_forme_on_switch_out = False
            # back on its base forme: exact-set overrides keyed to the base
            # species apply again
            side.active.forme_changed = False

        # a |detailschange|'s pending |-formechange| echo cannot span a
        # switch-out: PS emits the pair inside one `formeChange` call
        side.active.permanent_forme_echo_pending = None

        # set the pkmn's types back to their original value if the types were
        # changed.  PS switch-out runs `clearVolatile()`, whose final act is
        # `setSpecies(this.baseSpecies)` (sim/pokemon.ts) -- that rewrites
        # `pokemon.types` from the base species for a terastallized mon too
        # (only `terastallized` itself survives), so this revert is
        # unconditional.  For a terastallized mon the revert is a no-op anyway:
        # the typechange handler below never wrote `types` in the first place.
        if constants.TYPECHANGE in side.active.volatile_statuses:
            original_types = pokedex[side.active.name][constants.TYPES]
            logger.info(
                "{} had it's type changed - changing its types back to {}".format(
                    side.active.name, original_types
                )
            )
            side.active.types = original_types

        # if the target was transformed, reset its transformed attributes
        if constants.TRANSFORM in side.active.volatile_statuses:
            logger.info(
                "{} was transformed. Resetting its transformed attributes".format(
                    side.active.name
                )
            )
            side.active.stats = calculate_stats(
                side.active.base_stats, side.active.level
            )
            side.active.ability = side.active.original_ability
            side.active.moves = []
            side.active.types = pokedex[side.active.name][constants.TYPES]
            side.active.transformed_into = None

        if (
            side.active.original_ability is not None
            and side.active.ability != side.active.original_ability
        ):
            logger.info(
                "{}'s ability was modified to {} - setting it back to {} on switch-out".format(
                    side.active.name, side.active.ability, side.active.original_ability
                )
            )
            side.active.ability = side.active.original_ability
            side.active.original_ability = None

        if split_msg[-1] == "[from] Baton Pass":
            side.baton_passing = False
            logger.info(
                "Baton passing, preserving boosts: {}".format(dict(side.active.boosts))
            )
            baton_passed_boosts = deepcopy(side.active.boosts)

            if constants.SUBSTITUTE in side.active.volatile_statuses:
                logger.info("Baton passing, preserving substitute")
                switch_keep_volatiles.append(constants.SUBSTITUTE)
                # Baton Pass transfers the sub's current remaining HP as-is --
                # both ends of the interval, since the transfer adds no knowledge
                passed_substitute_health = _substitute_health_interval(side.active)
            if constants.LEECH_SEED in side.active.volatile_statuses:
                logger.info("Baton passing, preserving leechseed")
                switch_keep_volatiles.append(constants.LEECH_SEED)
        elif split_msg[-1] == "[from] Shed Tail":
            side.shed_tailing = False

            if constants.SUBSTITUTE in side.active.volatile_statuses:
                logger.info("Shed tailing, preserving substitute")
                switch_keep_volatiles.append(constants.SUBSTITUTE)
                # Shed Tail created the sub from the passer's maxhp (already
                # seeded to maxhp//4 on its `-start`), so carry that interval on
                passed_substitute_health = _substitute_health_interval(side.active)

        # gen5 rest turns are reset upon switching
        if battle.generation == "gen5" and side.active.status == constants.SLEEP:
            if side.active.rest_turns != 0:
                logger.info(
                    "{} switched while asleep and with non-zero rest turns, resetting rest turns to 3".format(
                        side.active.name
                    )
                )
                side.active.rest_turns = 3
            else:
                logger.info(
                    "{} switched while asleep, resetting sleep turns to 0".format(
                        side.active.name
                    )
                )
                side.active.sleep_turns = 0

        # gen3 rest turns are decremented by the number of consecutive sleep talks
        if battle.generation == "gen3" and side.active.status == constants.SLEEP:
            if side.active.rest_turns != 0:
                side.active.rest_turns += side.active.gen_3_consecutive_sleep_talks
                logger.info(
                    "gen3 {} switched with {} consecutive sleep talks. Incrementing rest turns by {}".format(
                        side.active.name,
                        side.active.gen_3_consecutive_sleep_talks,
                        side.active.gen_3_consecutive_sleep_talks,
                    )
                )
            elif side.active.sleep_turns != 0:
                logger.info(
                    "gen3 {} switched with {} consecutive sleep talks. Decrementing sleep turns by {}".format(
                        side.active.name,
                        side.active.gen_3_consecutive_sleep_talks,
                        side.active.gen_3_consecutive_sleep_talks,
                    )
                )
                side.active.sleep_turns -= side.active.gen_3_consecutive_sleep_talks

        side.active.gen_3_consecutive_sleep_talks = 0

        side.active.moves_used_since_switch_in.clear()

        # a move disabled via Disable/Cursed Body is restored on switch-out:
        # PS rebuilds moveSlots from baseMoveSlots and drops every volatile
        # WITHOUT emitting |-end|Disable (sim/pokemon.ts:1514-1541
        # clearVolatile), so the flag must be cleared here or the phantom
        # disable persists on every later root state
        for mv in side.active.moves:
            mv.disabled = False

        # reset the boost of the pokemon being replaced
        side.active.boosts.clear()

        # reset the volatile statuses of the pokemon being replaced
        # (also zeroes every timer in volatile_status_durations, incl. DISABLE)
        side.active.volatile_statuses.clear()
        side.active.volatile_status_durations.clear()

        # the outgoing pokemon's substitute is gone (a Baton Pass / Shed Tail
        # transfer captured its HP above before this reset)
        side.active.substitute_health = 0
        side.active.substitute_health_low = 0

        # reset toxic count for this side
        side.side_conditions[constants.TOXIC_COUNT] = 0

        # PS clears the OUTGOING mon's `statsRaisedThisTurn` on switch-out
        # (sim/battle-actions.ts:123 `oldActive.statsRaisedThisTurn = false`), and the
        # entrant starts false, so the side-level flag the engine keeps must drop here too.
        # Matters on turn 1, where the turn-boundary reset above does not run: a lead that
        # was Download/Intrepid-Sword-boosted and then pivots out must not leave its
        # replacement looking "raised this turn" to an Alluring Voice / Burning Jealousy.
        side.stats_raised_this_turn = False

        # if the side is alive and has regenerator, give it back 1/3 of it's maxhp
        if (
            "champions" not in battle.pokemon_format
            and side.active.hp > 0
            and not side.active.fainted
            and _switch_out_ability(battle, side, side_name) == "regenerator"
        ):
            health_healed = int(side.active.max_hp / 3)
            side.active.hp = min(side.active.hp + health_healed, side.active.max_hp)
            # `floor(max_hp / 3)` is an EXACT delta -- but only if max_hp is the
            # real one.  On an opponent whose max HP is still a randbats guess
            # the delta is off by however much the guess is off, so an exact-HP
            # certificate cannot survive this heal (synth08292: an Alomomola
            # certified at 220 came out at 363 and no longer matched its own
            # display).
            if not getattr(side.active, "max_hp_exact", False):
                hp_certificate.clear(side.active, "regenerator heal on a guessed max_hp")
            logger.info(
                "{} switched out with regenerator. Healing it to {}/{}".format(
                    side.active.name, side.active.hp, side.active.max_hp
                )
            )

        if side.active.name in ["cramorantgulping", "cramorantgorging"]:
            logger.info(
                "Resetting {} to 'cramorant' on switch out".format(side.active.name)
            )
            side.active.name = "cramorant"

    if side_name == "user" and user_just_switched_into_zoroark(battle, switch_or_drag):
        logger.info(
            "User switched/dragged into Zoroark - replacing the split_msg pokemon"
        )
        logger.info("Starting split_msg: {}".format(split_msg))
        request_json_zoroark = [
            p
            for p in battle.request_json[constants.SIDE][constants.POKEMON]
            if p[constants.DETAILS].startswith("Zoroark")
        ]
        assert len(request_json_zoroark) == 1
        request_json_zoroark = request_json_zoroark[0]
        split_msg[2] = f"{request_json_zoroark[constants.IDENT]}"
        split_msg[3] = f"{request_json_zoroark[constants.DETAILS]}"
        logger.info("New split_msg: {}".format(split_msg))

    # check if the pokemon exists in the reserves
    # if it does not, then the newly-created pokemon is used (for formats without team preview)
    nickname = split_msg[2]
    temp_pkmn = Pokemon.from_switch_string(split_msg[3], nickname=nickname)
    pkmn = side.find_pokemon_in_reserves(temp_pkmn.name)

    # ILLUSION entering AS THE SPECIES THAT IS ALREADY ACTIVE.  Species Clause makes
    # "the mon switching in is the species already standing on the field" impossible
    # for any real switch, so the only thing that produces it is a Zoroark whose
    # disguise happens to be the outgoing mon's own species (PS prints the entrant's
    # DETAILS from `pokemon.illusion`, sim/pokemon.ts:531-552).  The reserve lookup
    # above necessarily misses -- the genuine mon is `side.active`, not in `reserve` --
    # so without this a BLANK object is fabricated, and because `Pokemon.__eq__` is by
    # species the `pkmn != side.active` guard below then declines to bank the real
    # object and it is DISCARDED outright, taking every tracked per-mon value with it.
    # Measured (synth72642): the genuine Dondozo is dropped mid-Rest with rest_turns==1,
    # returns on T38 as a blank `slp` object with rest_turns==0, and the engine -- seeing
    # sleep_turns==0 -- gives it no wake branch at all, so its T39 `-curestatus|slp` +
    # `Rest` self-sleep is unreachable in every branch.  Reusing the object keeps the
    # party at one entry per species (a duplicate would blow the binding's 6-member
    # refusal) and hands the existing Zoroark rollback the right `hp_at_switch_in` /
    # `status_at_switch_in` to restore at the `|replace|`.  The append is undone by the
    # `side.reserve.remove(pkmn)` in the branch below, which is exactly the bookkeeping
    # a reserve hit already gets.
    if (
        pkmn is None
        and side.active is not None
        and not side.active.fainted
        and (
            side.active.name == temp_pkmn.name
            or base_species_name(side.active.name)
            == base_species_name(temp_pkmn.name)
        )
    ):
        pkmn = side.active
        side.reserve.append(pkmn)

    if pkmn is None:
        pkmn = Pokemon.from_switch_string(split_msg[3], nickname=nickname)

        # for standard battles gen4 and lower
        # we want to add the new pokemon to the datasets as they are revealed
        # because there is no teampreview
        if (
            battle.battle_type == BattleType.STANDARD_BATTLE
            and battle.generation in constants.NO_TEAM_PREVIEW_GENS
        ):
            SmogonSets.add_new_pokemon(pkmn.name)
            TeamDatasets.add_new_pokemon(pkmn.name)
            logger.info("Adding new pokemon '{}' to the datasets".format(pkmn.name))

        # some pokemon do not reveal their forme during team preview. Arceus, Silvally, Genesect, etc.
        # if this is the case, they would have been given a flag during team preview, and we can pull them out here
        unknown_forme_pkmn = side.find_reserve_pkmn_by_unknown_forme(temp_pkmn.name)
        if unknown_forme_pkmn:
            side.reserve.remove(unknown_forme_pkmn)
    else:
        if pkmn.name != temp_pkmn.name:
            logger.info("Renaming {} -> {}".format(pkmn.name, temp_pkmn.name))
            pkmn.name = temp_pkmn.name
            # protocol-derived identity: the log has now named this object by
            # the incoming forme too
            pkmn.forme_lineage.add(temp_pkmn.name)
        pkmn.nickname = temp_pkmn.nickname

        # Zoroark edge-case nonsense
        # if this pokemon turns out to be zoroark it may have permanent conditions change that need to be un-done after
        # finding out it is zoroark e.g. the HP value of this pokemon on switch-in is preserved so we can reset it if it
        # turns out to be zoroark
        pkmn.hp_at_switch_in = pkmn.hp
        pkmn.status_at_switch_in = pkmn.status
        pkmn.item_at_switch_in = pkmn.item

        side.reserve.remove(pkmn)

    split_hp_msg = split_msg[4].split("/")
    if is_opponent(battle, split_msg):
        # An exact display (`reportExactHP`, sim/pokemon.ts:2069-2070: the
        # shared health string IS `hp/maxhp`) states both values outright; the
        # percent-band machinery below exists only for `pct/100` displays.
        # Treating the exact numerator as a percent clamped every damage
        # display with numerator >= 100 back to FULL HP (see
        # hp_certificate.exact_display_hp).
        exact_display = hp_certificate.exact_display_hp(split_msg[4])
        if exact_display is not None:
            new_hp_percentage = exact_display[0] / max(1, exact_display[1])
            healed_off_field = exact_display[0] > (pkmn.hp or 0)
        else:
            new_hp_percentage = float(split_hp_msg[0]) / 100
            # `pkmn.hp` is a point estimate inside the band its last display stated;
            # "the HP moved while off the field" is `hp outside the NEW display's
            # band`, not `hp != max_hp * pct` (which fires on every rounding
            # difference and would now fire on every exact certificate as well).
            healed_off_field = not hp_certificate.display_contains(
                pkmn, split_hp_msg[0], pkmn.hp
            )
        if (
            healed_off_field
            and "regenerator"
            in [
                normalize_name(a)
                for a in pokedex[pkmn.name][constants.ABILITIES].values()
            ]
            and pkmn.ability is None
            and "champions" not in battle.pokemon_format
        ):
            logger.info(
                "{} switched out with {}% HP but now has {}% HP, setting its ability to regenerator".format(
                    pkmn.name,
                    pkmn.hp / pkmn.max_hp * 100,
                    new_hp_percentage * 100,
                )
            )
            pkmn.ability = "regenerator"
        if exact_display is not None:
            hp_certificate.set_exact_from_display(pkmn, exact_display)
        else:
            # A switch ENDS any exact-HP certificate: HP can change while a mon is
            # off the field (Regenerator, Healing Wish, a Wish landing on the
            # entrant) with no event we can arithmetically follow, so the entry
            # display is the only thing we know and it is an interval again.
            hp_certificate.apply_display(pkmn, split_hp_msg[0])
        switched_in_status = get_pokemon_info_from_condition(split_msg[4])[2]
    else:
        hp, maxhp, switched_in_status = get_pokemon_info_from_condition(split_msg[4])
        hp_certificate.set_exact(pkmn, hp)
        pkmn.max_hp = maxhp
        pkmn.max_hp_exact = True

    # The HP STATUS field of a |switch|/|drag| line belongs to the pokemon that
    # is ACTUALLY entering (sim/pokemon.ts:544-552 `getFullDetails` takes
    # `details` from the illusion but `health` from `this.getHealth()`, i.e. the
    # real mon), so it is authoritative for the status.  Normally it just
    # confirms what `-status`/`-curestatus` already tracked; it matters when the
    # entrant is a disguised Zoroark, because the reconstruction reuses ONE
    # object per species and the disguise species may carry a status from its
    # own earlier stay.  Without this, `illusion_end` hands that borrowed status
    # to the Zoroark at the |replace| and every later status move on it is
    # unreproducible (synth27501 T38: Blissey's Thunder Wave lands on a Zoroark
    # the reconstruction already believed paralysed, because the real Coalossal
    # had been paralysed on turn 2).  `status_at_switch_in` was captured above,
    # before this write, so the rollback of the impersonated mon still restores
    # the status IT last had.
    if pkmn.hp > 0 and pkmn.status != switched_in_status:
        logger.info(
            "{} switched in with status {} (was {})".format(
                pkmn.name, switched_in_status, pkmn.status
            )
        )
        pkmn.status = switched_in_status

    side.last_used_move = LastUsedMove(
        pokemon_name=None, move="switch {}".format(pkmn.name), turn=battle.turn
    )

    # pkmn != active is a special edge-case for Zoroark
    if side.active is not None and pkmn != side.active:
        side.reserve.append(side.active)

    side.active = pkmn

    # ENTRY-INTENT evidence: the opponent CHOSE to bring this mon in against
    # our active (voluntary switch, pivot follow-up, or faint replacement all
    # arrive as |switch|; |drag| does not). Which sets make that entry good
    # is informative - the sampler upweights them (fp/search/random_battles).
    if (
        switch_or_drag == "switch"
        and side is battle.opponent
        and other_side.active is not None
        and other_side.active.hp > 0
    ):
        alive_bench = sum(1 for p in side.reserve if p.hp > 0)
        unrevealed = 6 - (len(side.reserve) + 1)
        if alive_bench + unrevealed > 0:
            me = other_side.active
            pkmn.entry_contexts.append(
                {
                    "vs_name": me.name,
                    "vs_hp": me.hp,
                    "vs_maxhp": me.max_hp,
                    "vs_types": list(me.types),
                    "vs_defense": me.stats[constants.DEFENSE],
                    "vs_special_defense": me.stats[constants.SPECIAL_DEFENSE],
                    "vs_speed": me.stats[constants.SPEED],
                }
            )

    # every switch-in starts a fresh Fake Out / First Impression window: PS
    # resets activeMoveActions to 0 in switchIn (sim/battle-actions.ts:138) -
    # switch, drag, faint replacement and pivot follow-ups all route through
    # here. The entering pkmn also cannot have acted yet this turn.
    pkmn.active_move_actions = 0
    pkmn.moved_this_turn = False
    # PS switchIn also zeroes activeTurns (sim/battle-actions.ts:137); it is
    # only incremented by nextTurn AFTER residuals (sim/battle.ts:1762), so
    # this mon's entry turn contributes no Slow Start residual decrement.
    pkmn.active_turns = 0

    # every field-entry path (switch, drag, faint replacement,
    # pivot/baton-pass follow-up) goes through here: the pkmn has
    # now been seen by the opponent
    pkmn.revealed = True

    # zacian-crowned is technically still zacian before switching in for the first time
    # this is handled by set-prediction for the opponent, but for the bot's pkmn we
    # need to re-apply the stats that the P.S. server sends us because prior to the first
    # switch-in the stats would be for zacian, not zacian-crowned
    if side_name == "user" and pkmn.name in ["zaciancrowned", "zamazentacrowned"]:
        battle.user.re_initialize_active_pokemon_from_request_json(battle.request_json)

    for ability in ABILITIES_REVEALED_ON_SWITCH_IN:
        if battle.generation == "gen3" and ability == "pressure":
            # gen3 pressure is not revealed on switch-in
            continue

        if (
            (
                ability == "sandstream"
                and battle.weather
                in [constants.SAND, constants.HEAVY_RAIN, constants.DESOLATE_LAND]
            )
            or (
                ability == "drought"
                and battle.weather
                in [constants.SUN, constants.HEAVY_RAIN, constants.DESOLATE_LAND]
            )
            or (
                ability == "drizzle"
                and battle.weather
                in [constants.RAIN, constants.HEAVY_RAIN, constants.DESOLATE_LAND]
            )
            or (
                ability == "snowwarning"
                and battle.weather
                in [
                    constants.HAIL,
                    constants.SNOW,
                    constants.HEAVY_RAIN,
                    constants.DESOLATE_LAND,
                ]
            )
        ):
            logger.info(
                "Not adding {} to {}'s impossible abilities because the weather would not have triggered".format(
                    ability,
                    pkmn.name,
                )
            )
            continue

        if ability not in pkmn.impossible_abilities and (
            other_side.active is not None
            and other_side.active.ability != "neutralizinggas"
        ):
            logger.info(
                "{} switched in, adding {} to impossible abilities".format(
                    pkmn.name, ability
                )
            )
            pkmn.impossible_abilities.add(ability)

    for item in ITEMS_REVEALED_ON_SWITCH_IN:
        if item not in pkmn.impossible_items:
            logger.info(
                "{} switched in, adding {} to impossible items".format(pkmn.name, item)
            )
            pkmn.impossible_items.add(item)

    if baton_passed_boosts is not None:
        logger.info(
            "Applying baton passed boosts to {}: {}".format(
                side.active.name, dict(baton_passed_boosts)
            )
        )
        side.active.boosts = baton_passed_boosts
    for volatile in switch_keep_volatiles:
        logger.info("Keeping volatile on switch: {}".format(volatile))
        side.active.volatile_statuses.append(volatile)
    if passed_substitute_health is not None:
        logger.info(
            "Carrying substitute health interval {} onto {}".format(
                passed_substitute_health, side.active.name
            )
        )
        side.active.substitute_health_low, side.active.substitute_health = (
            passed_substitute_health
        )


_PAIN_SPLIT_TAG = "[from] move: Pain Split"


def sethp(battle, split_msg):
    # |-sethp|p2a: Jellicent|317/403|[from] move: Pain Split|[silent]
    pain_split = any(m.strip() == _PAIN_SPLIT_TAG for m in split_msg[4:])
    if is_opponent(battle, split_msg):
        pkmn = battle.opponent.active
        # `reportExactHP` display: the condition IS `hp/maxhp` (sim/pokemon.ts:
        # 2069-2070) -- no certificate arithmetic needed, the value is stated.
        exact_display = hp_certificate.exact_display_hp(split_msg[3])
        if exact_display is not None:
            pkmn.hp_pain_split_certified = False
            hp_certificate.set_exact_from_display(pkmn, exact_display)
            return
        shown = split_msg[3].split("/")[0]
        # When the OPPONENT used Pain Split its own `-sethp` comes SECOND (PS
        # emits target-then-user), so our line has already pinned the exact
        # average onto it.  Confirm that certificate against this display
        # instead of demoting it back to an estimate.
        if pain_split and getattr(pkmn, "hp_pain_split_certified", False):
            pkmn.hp_pain_split_certified = False
            if hp_certificate.certify(
                pkmn, pkmn.hp, "pain split (confirmed by -sethp)", shown
            ):
                return
        hp_certificate.apply_display(pkmn, shown)
    else:
        pkmn = battle.user.active
        hp_certificate.set_exact(pkmn, int(split_msg[3].split("/")[0]))
        pkmn.max_hp = int(split_msg[3].split("/")[1].split()[0])
        pkmn.max_hp_exact = True

        # PAIN SPLIT CERTIFICATE (PS data/moves.ts:13140-13148).  Both sides are
        # set to `averagehp = floor((targetHP + pokemon.hp) / 2)`, each clamped
        # into its own [1, maxhp] by `sethp` (sim/pokemon.ts:1661-1673).  OUR
        # side's `-sethp` carries the exact value, so whenever it is not clamped
        # (hp < max_hp) it IS `averagehp` -- and the opponent's post-Pain-Split
        # HP is then `min(averagehp, their max_hp)` exactly, with no reference to
        # the percent display at all.  PS emits the two lines in a fixed order
        # (target first, user second) but this works from either, because the
        # opponent's value is the same average whichever side moved: if their
        # `-sethp` came first this OVERWRITES its estimate, and if it comes next
        # it is checked against the certificate rather than replacing it.
        if pain_split and pkmn.hp < pkmn.max_hp:
            opp = battle.opponent.active
            if opp is not None and opp.max_hp:
                if hp_certificate.certify(
                    opp,
                    min(int(pkmn.hp), int(opp.max_hp)),
                    "pain split (both sides := floor((a+b)/2))",
                ):
                    opp.hp_pain_split_certified = True


def heal_or_damage(battle, split_msg):
    revived_by_revival_blessing = (
        len(split_msg) == 5 and split_msg[4] == "[from] move: Revival Blessing"
    )
    # a bare '-damage' (no '[from]') is the protocol's attribution for damage
    # dealt directly by the other side's move -- the only line a fixed-damage
    # HP certificate may be consumed on
    bare_damage = split_msg[1] == "-damage" and not any(
        m.startswith("[from]") for m in split_msg[4:]
    )
    if is_opponent(battle, split_msg):
        side = battle.opponent
        other_side = battle.user
        pkmn = battle.opponent.active
        if revived_by_revival_blessing:
            nickname = Pokemon.extract_nickname_from_pokemonshowdown_string(
                split_msg[2]
            )
            pkmn = side.find_reserve_pokemon_by_nickname(
                nickname
            ) or _find_bench_pokemon_by_protocol_name(side, nickname)

        # opponent hp is given as a percentage
        prior_hp, prior_exact = pkmn.hp, hp_certificate.is_exact(pkmn)
        # the display fold below clears the max-relative derivation, so capture
        # it here for `consume` (a halving of `hp == guessed max` must be
        # carried as a function of max HP, not frozen -- gate-5 B5)
        prior_fraction = getattr(pkmn, "hp_certificate_fraction", None)
        prior_halvings = getattr(pkmn, "hp_certificate_halvings", 0)
        if constants.FNT in split_msg[3]:
            hp_certificate.set_exact(pkmn, 0)
            hp_certificate.disarm(pkmn)
        elif (
            exact_display := hp_certificate.exact_display_hp(split_msg[3])
        ) is not None:
            # `reportExactHP` display (sim/pokemon.ts:2069-2070): the condition
            # IS `hp/maxhp`.  Server-stated truth, strictly stronger than any
            # percent band or pending fixed-damage certificate, so the
            # certificate machinery below (whose whole purpose is to recover
            # exactness a percent display hides) has nothing to add.
            hp_certificate.set_exact_from_display(pkmn, exact_display)
        else:
            shown = split_msg[3].split("/")[0]
            hp_certificate.apply_display(pkmn, shown)
            if revived_by_revival_blessing:
                # PS revives at `sethp(maxhp / 2)`, and `sethp` truncs
                # (sim/battle.ts:2791-2792, sim/pokemon.ts:1663) -- an exact
                # value, not a percent-derived one.  It is a FUNCTION of max HP,
                # so it must be re-evaluated (not merely re-checked) if max HP
                # is later corrected: `248 // 2 = 124` vs the true `247 // 2 =
                # 123` is a whole HP of fiction, and the display check caught it
                # (synth15945).
                hp_certificate.certify(
                    pkmn,
                    None,
                    "revival blessing (maxhp // 2)",
                    shown,
                    fraction=(1, 2),
                )
            elif bare_damage:
                hp_certificate.consume(
                    pkmn,
                    other_side.active,
                    prior_hp,
                    prior_exact,
                    shown,
                    prior_fraction=prior_fraction,
                    prior_halvings=prior_halvings,
                )
            else:
                hp_certificate.disarm(pkmn)

    else:
        side = battle.user
        other_side = battle.opponent
        pkmn = battle.user.active
        if revived_by_revival_blessing:
            nickname = Pokemon.extract_nickname_from_pokemonshowdown_string(
                split_msg[2]
            )
            pkmn = side.find_reserve_pokemon_by_nickname(
                nickname
            ) or _find_bench_pokemon_by_protocol_name(side, nickname)
        prior_hp, prior_exact = pkmn.hp, hp_certificate.is_exact(pkmn)
        if constants.FNT in split_msg[3]:
            hp_certificate.set_exact(pkmn, 0)
            hp_certificate.disarm(pkmn)
        else:
            hp, maxhp, _ = get_pokemon_info_from_condition(split_msg[3])
            hp_certificate.set_exact(pkmn, hp)
            pkmn.max_hp = maxhp
            pkmn.max_hp_exact = True
            if bare_damage:
                # our own HP is already exact; the value here is the REVERSE
                # direction of the Endeavor identity -- our exact post-hit HP
                # certifies the OPPONENT attacker's HP.
                hp_certificate.consume(
                    pkmn, other_side.active, prior_hp, prior_exact, None
                )
            else:
                hp_certificate.disarm(pkmn)

    # PS's Revival Blessing revives the target STATUSLESS (sim/battle.ts:2778-2793
    # `action.target.fainted = false; ...; action.target.status = '';
    # action.target.hp = 1; action.target.sethp(action.target.maxhp / 2)`), and the
    # `-heal` line it prints (:2793) carries only the new HP, so nothing else in the
    # protocol announces the cure.  Without clearing it here the revived mon keeps
    # the status it fainted with and every later re-application of that status is
    # unreproducible (the engine sees a pokemon that is already statused).
    if revived_by_revival_blessing:
        pkmn.status = None
        pkmn.rest_turns = 0
        pkmn.sleep_turns = 0

        # PS's `side.totalFainted` is CUMULATIVE: it increments on every faint
        # (sim/battle.ts:2551) and the revival branch (sim/battle.ts:2778-2793)
        # never decrements it. `Battler.num_fainted_pkmn()` counts CURRENTLY-fainted
        # party members instead, so the two drift apart by exactly one per revive.
        # PS emits exactly one `-heal ... [from] move: Revival Blessing` line per
        # revive (sim/battle.ts:2793), so counting those lines recovers the delta:
        # totalFainted == num_fainted_pkmn() + times_revived. Supreme Overlord's
        # `fallen` snapshot and Last Respects both read totalFainted, and without
        # this the engine under-counts every post-revive Kingambit by one powMod step.
        side.times_revived += 1

    # Rage Fist support: count direct move hits on this pokemon.
    # A '-damage' line with no '[from]' clause is the protocol's attribution
    # for damage dealt directly by the other side's move. All chip damage -
    # status/hazards/weather/items/recoil/confusion/crash - carries a '[from]'
    # tag and must not be counted.
    # PS counts neither substitute-absorbed nor substitute-breaking hits toward
    # Rage Fist (Substitute's onTryPrimaryHit returns HIT_SUBSTITUTE before the
    # timesAttacked counter runs), so those '-activate' / '-end' lines are
    # correctly not counted; only direct '-damage' move hits increment.
    # The HP cost of Substitute/Belly Drum/ghost-Curse/Fillet Away/Shed Tail/
    # Clangorous Soul is also a bare '-damage' but is dealt by the pokemon to
    # ITSELF; PS only increments timesAttacked for hits from another pokemon
    # (sim/battle-actions.ts:990-996 `pokemon !== target`), so consume the
    # expectation set by the |move| handler instead of counting.
    if split_msg[1] == "-damage" and not any(
        msg.startswith("[from]") for msg in split_msg[4:]
    ):
        if getattr(pkmn, "self_cost_damage_expected", False):
            pkmn.self_cost_damage_expected = False
        else:
            pkmn.times_attacked += 1

    # increase the amount of turns toxic has been active
    if (
        len(split_msg) == 5
        and constants.TOXIC in split_msg[3]
        and "[from] psn" in split_msg[4]
    ):
        side.side_conditions[constants.TOXIC_COUNT] += 1

    if (
        len(split_msg) == 6
        and split_msg[4].startswith("[from] item:")
        and other_side.name in split_msg[5]
    ):
        item = normalize_name(split_msg[4].split("item:")[-1])
        owner = _record_revealed_item(battle, other_side, other_side.active, item)
        if owner is not None:
            logger.info("Setting {}'s item to: {}".format(owner.name, item))
            owner.item = item

    if (
        len(split_msg) >= 5
        and split_msg[-1].startswith("[from]")
        and (
            split_msg[-1].endswith("Healing Wish")
            or split_msg[-1].endswith("Lunar Dance")
        )
    ):
        logger.info(
            "{} was healed from healing wish, setting side condition to 0".format(
                side.active.name
            )
        )
        side.side_conditions[constants.HEALING_WISH] = 0
        # PS's slot condition heals to FULL and CLEARS STATUS in one step
        # (data/moves.ts healingwish/lunardance `onSwap`: `target.heal(
        # target.maxhp); target.clearStatus();`) but announces only the
        # `-heal ... [from] move: Healing Wish` line -- there is NO `-curestatus`
        # to drive the normal cure path.  Without this the incoming mon keeps a
        # status PS already removed: synth03334 T27 switches Ursaluna in at
        # `72/100 brn`, the Healing Wish heal fires, and its T28 Facade is then
        # reconstructed as a burnt 140-BP hit instead of PS's clean 70-BP one.
        if pkmn.status is not None:
            logger.info(
                "{} was cured of {} by the healing wish".format(pkmn.name, pkmn.status)
            )
            if pkmn.status == constants.SLEEP:
                pkmn.rest_turns = 0
                pkmn.sleep_turns = 0
            elif pkmn.status == constants.TOXIC:
                side.side_conditions[constants.TOXIC_COUNT] = 0
            pkmn.status = None
        # a Healing Wish / Lunar Dance recipient is restored to FULL, which is an
        # identity (`hp == max_hp`), not a percent-derived estimate -- and one
        # that follows max HP if the sidecar later corrects it
        hp_certificate.certify(pkmn, None, "healing wish (hp := max_hp)", fraction=(1, 1))

    # set the ability for the other side (the side not taking damage, '-damage' only)
    if (
        len(split_msg) == 6
        and split_msg[4].startswith("[from] ability:")
        and other_side.name in split_msg[5]
        and split_msg[1] == "-damage"
    ):
        ability = normalize_name(split_msg[4].split("ability:")[-1])
        logger.info(
            "Setting {}'s ability to: {}".format(other_side.active.name, ability)
        )
        other_side.active.ability = ability

    # set the ability of the side (the side being healed, '-heal' only)
    if (
        len(split_msg) == 6
        and constants.ABILITY in split_msg[4]
        and other_side.name in split_msg[5]
        and split_msg[1] == "-heal"
    ):
        ability = normalize_name(split_msg[4].split(constants.ABILITY)[-1].strip(": "))
        logger.info("Setting {}'s ability to: {}".format(pkmn.name, ability))
        pkmn.ability = ability

    # give that pokemon an item if this string specifies one
    if len(split_msg) == 5 and constants.ITEM in split_msg[4] and pkmn.item is not None:
        item = normalize_name(split_msg[4].split(constants.ITEM)[-1].strip(": "))
        owner = _record_revealed_item(battle, side, pkmn, item)
        if owner is not None:
            logger.info("Setting {}'s item to: {}".format(owner.name, item))
            owner.item = item

    # gen 1 if you are trapping the opponent and hit yourself in confusion, the opponent is released
    if (
        battle.generation == "gen1"
        and split_msg[-1] == "[from] confusion"
        and (
            constants.PARTIALLY_TRAPPED in other_side.active.volatile_statuses
            or other_side.active.volatile_status_durations[constants.PARTIALLY_TRAPPED]
            > 0
        )
    ):
        logger.info(
            f"{pkmn.name} hit itself in confusion, releasing partially trapped volatile on {other_side.active.name}"
        )
        remove_volatile(other_side.active, constants.PARTIALLY_TRAPPED)
        other_side.active.volatile_status_durations[constants.PARTIALLY_TRAPPED] = 0


def faint(battle, split_msg):
    if is_opponent(battle, split_msg):
        side = battle.opponent
    else:
        side = battle.user

    hp_certificate.set_exact(side.active, 0)

    # PS deletes `terastallized` when a pokemon faints (sim/battle.ts:2565):
    # a mon later revived by Revival Blessing returns UN-terastallized (base
    # typing, no tera boosts). The side's tera is still spent for the battle,
    # so remember that on the Battler - the engine has no side-level flag and
    # derives can_use_tera() from "any party pkmn terastallized"
    # (poke-engine genx/state.rs:980-987); conversion re-flags a fainted slot
    # from tera_spent (see poke_engine_helpers._padded_party)
    if side.active.terastallized:
        logger.info(
            "{} fainted while terastallized - clearing terastallized "
            "and marking the side's tera as spent".format(side.active.name)
        )
        side.active.terastallized = False
        side.tera_spent = True


# abilities that fire at most ONCE PER BATTLE and survive switches. The engine
# tracks all three with a single `once_per_battle_ability_used` flag (a pkmn has
# exactly one ability): Intrepid Sword (PS data/abilities.ts:2204-2208
# pokemon.swordBoost), Dauntless Shield (:844-848 pokemon.shieldBoost) and Battle
# Bond (:353-360 source.bondTriggered).
ONCE_PER_BATTLE_ABILITIES = {"intrepidsword", "dauntlessshield", "battlebond"}

# PS never consumes a Stellar boost for Terapagos-Stellar
# (sim/battle-actions.ts:1778-1784).
STELLAR_BOOST_EXEMPT_SPECIES = {"terapagosstellar"}


def _mark_once_per_battle_ability_used(pkmn, ability):
    """Record that `pkmn` has spent its once-per-battle ability trigger."""
    if ability in ONCE_PER_BATTLE_ABILITIES and not pkmn.once_per_battle_ability_used:
        logger.info(
            "{} used its once-per-battle ability {} - it cannot fire again".format(
                pkmn.name, ability
            )
        )
        pkmn.once_per_battle_ability_used = True


def _maybe_consume_stellar_boost(pkmn, move_name):
    """Mirror PS spending a Stellar-tera'd pkmn's one-time boost for this move's
    type (pokemon.stellarBoostedTypes, sim/pokemon.ts:264; pushed while computing
    damage in sim/battle-actions.ts:1778-1784).

    Recorded optimistically on the |move| line and rolled back by
    `_rollback_stellar_boost` if the move turns out to have missed / failed / been
    immune, because PS only spends it when damage is actually computed.
    """
    pkmn.stellar_boost_pending_type = None
    if not pkmn.terastallized or normalize_name(pkmn.tera_type or "") != "stellar":
        return
    if pkmn.name in STELLAR_BOOST_EXEMPT_SPECIES:
        return
    move_data = all_move_json.get(move_name)
    if move_data is None:
        return
    if move_data.get(constants.CATEGORY) not in (constants.PHYSICAL, constants.SPECIAL):
        return
    move_type = normalize_name(move_data.get(constants.TYPE) or "")
    # a Stellar-typed move never consumes; neither does a typeless one
    if move_type in ("stellar", "typeless", ""):
        return
    if move_type in pkmn.stellar_boosted_types:
        return
    logger.info(
        "{} spent its one-time Stellar boost for {} moves".format(pkmn.name, move_type)
    )
    pkmn.stellar_boosted_types.add(move_type)
    pkmn.stellar_boost_pending_type = move_type


def _rollback_stellar_boost(pkmn):
    """Undo the optimistic consumption above: PS computed no damage, so the
    one-time boost is still unspent."""
    pending = getattr(pkmn, "stellar_boost_pending_type", None)
    if pending is not None:
        logger.info(
            "{}'s move did not connect - its Stellar boost for {} moves is "
            "still unspent".format(pkmn.name, pending)
        )
        pkmn.stellar_boosted_types.discard(pending)
        pkmn.stellar_boost_pending_type = None


def _disarm_hp_certificates(battle) -> None:
    """Drop pending fixed-damage HP certificates on BOTH actives.

    A certificate armed by a |move| line is only valid for the `-damage` that
    move produces.  Anything that means the move did not deal its damage to the
    target -- a miss, a fail, an immunity, a Substitute absorbing the hit, or
    simply the next |move| line -- must disarm it, or a later unrelated bare
    `-damage` would consume it and pin a fabricated 'exact' HP."""
    for battler in (battle.user, battle.opponent):
        if battler.active is not None:
            hp_certificate.disarm(battler.active)
            battler.active.hp_pain_split_certified = False


def miss(battle, split_msg):
    # |-miss|<attacker>|<target>. PS spends a Stellar boost inside the damage
    # calculation, which a missed move never reaches.
    missing_side = battle.opponent if is_opponent(battle, split_msg) else battle.user
    _rollback_stellar_boost(missing_side.active)
    # the fixed-damage certificate this move armed on its target never lands
    _disarm_hp_certificates(battle)


def fail(battle, split_msg):
    # a failed self-costing move (e.g. Substitute below 25% HP, second Belly
    # Drum) never emits its cost '-damage', so drop the pending expectation:
    # the next bare '-damage' on this pokemon is a real hit
    failing_side = battle.opponent if is_opponent(battle, split_msg) else battle.user
    failing_side.active.self_cost_damage_expected = False
    _rollback_stellar_boost(failing_side.active)
    _disarm_hp_certificates(battle)

    # |-fail|p2a: Dragapult|unboost|[from] ability: Clear Body|[of] p2a: Dragapult
    if (
        len(split_msg) > 5
        and split_msg[4].startswith("[from] ability: ")
        and split_msg[5].startswith("[of]")
    ):
        ability_side = (
            battle.user
            if split_msg[5].startswith(f"[of] {battle.user.name}")
            else battle.opponent
        )
        ability = normalize_name(split_msg[4].split("ability: ")[-1])
        logger.info(
            "Setting {}'s ability to: {}".format(ability_side.active.name, ability)
        )
        ability_side.active.ability = ability


def move(battle, split_msg):
    if is_opponent(battle, split_msg):
        side = battle.opponent
        pkmn = battle.opponent.active
        opposing_pkmn = battle.user.active
        record_opponent_action(
            battle, "move", normalize_name(split_msg[3].strip().lower())
        )
    else:
        side = battle.user
        pkmn = battle.user.active
        opposing_pkmn = battle.opponent.active

    # a bare '-damage' is only a self-cost while the pokemon's own
    # self-costing move is the most recent |move| processed; any new |move|
    # line (either side) invalidates pending expectations
    pkmn.self_cost_damage_expected = False
    opposing_pkmn.self_cost_damage_expected = False
    _disarm_hp_certificates(battle)

    move_name = normalize_name(split_msg[3].strip().lower())

    # A DANCER COPY THAT NEVER RAN.  `-activate ... ability: Dancer` armed this
    # marker (see activate()); PS then re-entered runMove and a Choice lock /
    # Gorilla Tactics aborted the copy in onBeforeMove, printing an UNTAGGED
    # `|move|<mon>|<Move>||[still]` + `|-fail|` (data/conditions.ts:342-345,
    # data/abilities.ts:1652-1655).  A copy that actually executed carries
    # `[from] ability: Dancer` and is handled by the from-tag branch below, so
    # an untagged line while this marker is set is always the aborted form.
    # Nothing about this mon's own moveset is revealed, no PP is spent and
    # `moveUsed()` is never reached (:279-292) -- record nothing.  Only
    # `activeMoveActions` really happened: runMove increments it on entry
    # (:217), before the BeforeMove event ran.  Without this the copied dance
    # was appended as a 5th/6th move and `poke_engine_helpers` truncated the
    # real moveset away (synth82810: a Choice-Scarf Gardevoir that Traced
    # Oricorio's Dancer grew Quiver Dance and Revelation Dance; synth80012's
    # Oricorio grew Swords Dance).
    dancer_copy_pending = getattr(pkmn, "dancer_copy_pending", False)
    pkmn.dancer_copy_pending = False
    if dancer_copy_pending and not has_non_lockedmove_from_tag(split_msg):
        logger.info(
            "{}'s Dancer copy of {} was aborted before it ran - not recording it".format(
                pkmn.name, move_name
            )
        )
        pkmn.active_move_actions += 1
        return

    # HP-CERTIFYING FIXED-DAMAGE MOVES (fp/hp_certificate.py): Endeavor states
    # `target.hp == attacker.hp` and the Super Fang family states
    # `target.hp -= max(1, target.hp // 2)`.  Arm the identity on the TARGET now
    # and let the bare `-damage` this move produces consume it -- a Substitute
    # absorb emits `-activate ... Substitute` instead and so never does.
    hp_certificate.arm(opposing_pkmn, move_name)

    zoroark_from_reserves = side.find_pokemon_in_reserves(
        "zoroark"
    ) or side.find_pokemon_in_reserves("zoroarkhisui")

    # SIDECAR-PROVEN disguise.  The two heuristics below are gated off wherever
    # the exact-teams sidecar is loaded (`zoroark_inference_allowed`), but the
    # sidecar PROVES the same thing outright: movesets are fixed for the whole
    # battle, so a move the shown species provably does not own and that side's
    # Illusion bearer provably does cannot have come from the shown species (PS
    # renders the actor through `toString()` -> the illusion's name,
    # sim/pokemon.ts:531).  `_record_used_move` already credits the MOVE to the
    # bearer, but it leaves the DISGUISE standing as the active, so every later
    # `-damage` -- and the `|faint|` -- lands on the wrong party member, and the
    # protocol never corrects it: PS adds the `|faint|` line while the illusion
    # is still up and only clears it afterwards (sim/battle.ts:2549 vs :2560),
    # so a bearer that dies disguised is never announced at all.
    # synth84252: p2's Zoroark spent T10-T13 wearing "Deoxys" and died there, so
    # the reconstruction fainted the REAL Deoxys-Defense; when it genuinely
    # switched in on T18 the engine was handed a 0-HP entrant, produced no Close
    # Combat resolution whatsoever, and the observed `-unboost def`/`-unboost
    # spd` + White Herb `-enditem` were unreachable in every branch.
    if (
        is_opponent(battle, split_msg)
        and not zoroark_inference_allowed(battle)
        and zoroark_from_reserves is not None
        and pkmn is not zoroark_from_reserves
        and "transform" not in pkmn.volatile_statuses
        and "from" not in split_msg[-1]
    ):
        shown_moves = _sidecar_moveset(battle, side, pkmn)
        bearer_moves = _sidecar_moveset(battle, side, zoroark_from_reserves)
        if (
            shown_moves is not None
            and bearer_moves is not None
            and move_name not in shown_moves
            and move_name in bearer_moves
        ):
            logger.info(
                "{} cannot own {} but {} can - the active is the disguised {}".format(
                    pkmn.name,
                    move_name,
                    zoroark_from_reserves.name,
                    zoroark_from_reserves.name,
                )
            )
            _switch_active_with_zoroark_from_reserves(side, zoroark_from_reserves)
            pkmn = zoroark_from_reserves

    # in battle factory we can deduce that there is a zoroark in front of us
    # if we see a move that is not in the known moveset and a zoroark is in the reserves
    if (
        is_opponent(battle, split_msg)
        and zoroark_inference_allowed(battle)
        and zoroark_from_reserves is not None
        and "transform" not in pkmn.volatile_statuses
        and battle.battle_type
        in [BattleType.BATTLE_FACTORY, BattleType.STANDARD_BATTLE]
        and move_name not in TeamDatasets.get_all_possible_moves(pkmn)
        and move_name in TeamDatasets.get_all_possible_moves(zoroark_from_reserves)
        and "from" not in split_msg[-1]
    ):
        logger.info(
            "{} using {} means it is {}".format(
                pkmn.name, move_name, zoroark_from_reserves.name
            )
        )
        _switch_active_with_zoroark_from_reserves(side, zoroark_from_reserves)

        # the rest of this function uses `pkmn`, so we need to set it to the correct pkmn
        pkmn = zoroark_from_reserves

    # in randombattles we can deduce that there is a zoroark in front of us
    # if we see a move that is not in the known moveset, even if there is no
    # zoroark is in the reserves
    if (
        is_opponent(battle, split_msg)
        and zoroark_inference_allowed(battle)
        and battle.battle_type == BattleType.RANDOM_BATTLE
        and "transform" not in pkmn.volatile_statuses
        and move_name not in RandomBattleTeamDatasets.get_all_possible_moves(pkmn)
        and "from" not in split_msg[-1]
    ):
        actual_zoroark = None
        zoroark_hisui = Pokemon("zoroarkhisui", 100)
        zoroark_regular = Pokemon("zoroark", 100)
        if (
            zoroark_from_reserves is not None
            and move_name
            in RandomBattleTeamDatasets.get_all_possible_moves(zoroark_from_reserves)
        ):
            actual_zoroark = zoroark_from_reserves

        elif (
            battle.generation not in constants.NO_TEAM_PREVIEW_GENS
            and zoroark_from_reserves is None
            and move_name
            in RandomBattleTeamDatasets.get_all_possible_moves(zoroark_hisui)
        ):
            actual_zoroark = zoroark_hisui
            actual_zoroark.level = RandomBattleTeamDatasets.predict_set(
                actual_zoroark
            ).pkmn_set.level
            side.reserve.append(actual_zoroark)

        elif (
            battle.generation not in constants.NO_TEAM_PREVIEW_GENS
            and zoroark_from_reserves is None
            and move_name
            in RandomBattleTeamDatasets.get_all_possible_moves(zoroark_regular)
        ):
            actual_zoroark = zoroark_regular
            actual_zoroark.level = RandomBattleTeamDatasets.predict_set(
                actual_zoroark
            ).pkmn_set.level
            side.reserve.append(actual_zoroark)

        if actual_zoroark is not None:
            logger.info(
                "{} using {} means it is {}".format(
                    pkmn.name, move_name, actual_zoroark.name
                )
            )
            _switch_active_with_zoroark_from_reserves(side, actual_zoroark)

            # the rest of this function uses `pkmn`, so we need to set it to the correct pkmn
            pkmn = actual_zoroark

    # PS increments activeMoveActions in runMove, once per queued move action
    # (sim/battle-actions.ts:217, incremented BEFORE the move executes), and it
    # feeds Fake Out / First Impression gating (fail when > 1). Every |move|
    # line the pokemon generates as its own action counts - including
    # locked-move continuations ('[from]lockedmove', still runMove) and the
    # calling line of Sleep Talk. A CALLED move's line ('[from]move: Sleep
    # Talk', '[from]ability: Magic Bounce', ...) is useMove-internal and does
    # not re-increment - EXCEPT a Dancer copy: Dancer re-enters runMove with
    # externalMove=true (sim/battle-actions.ts:343), so the copied dance's
    # |move| line ('[from] ability: Dancer') increments the counter
    # unconditionally at runMove entry (:217). externalMove skips moveUsed()
    # (:279-292), so a Dancer copy does NOT mark moved_this_turn. Reset on
    # switch-in lives in switch_or_drag (sim/battle-actions.ts:138).
    # `moved_this_turn` marks the same lines for the taunt/encore 'already
    # acted this turn' seed (see start_volatile_status); a consumed-but-aborted
    # action (|cant|) also sets both - see cant().
    if not has_non_lockedmove_from_tag(split_msg):
        pkmn.active_move_actions += 1
        pkmn.moved_this_turn = True
    elif any(
        msg in ("[from] ability: Dancer", "[from]ability: Dancer")
        for msg in split_msg
    ):
        pkmn.active_move_actions += 1

    # A Stellar-terastallized pkmn spends its one-time boost for this move's TYPE
    # (PS pokemon.stellarBoostedTypes, sim/pokemon.ts:264, pushed while computing
    # damage in sim/battle-actions.ts:1778-1784). Recorded here and rolled back by
    # the -miss / -fail / -immune handlers if the move never reached the damage
    # calculation. Forwarded to the engine, which hard-coded the set empty until
    # the binding-completeness pass and so re-granted the bonus at every root.
    _maybe_consume_stellar_boost(pkmn, move_name)

    # PS ends Glaive Rush's double-damage-taken drawback silently the next time
    # the holder attempts ANY action (data/moves.ts glaiverush condition:
    # onBeforeMove at priority 100 removeVolatile - it runs before every abort
    # check: flinch 8, slp/frz 10, recharge 11, par 1, so even a no-move turn
    # consumes it; the |cant| path is handled in cant()). There is no protocol
    # line for the removal, so clear it here on the holder's own action line.
    # A re-use of Glaive Rush clears the previous application first and the
    # fresh volatile is re-added by the |-singlemove| following this line.
    if "glaiverush" in pkmn.volatile_statuses:
        logger.info(
            "{} is acting - removing its spent glaiverush volatile".format(pkmn.name)
        )
        remove_volatile(pkmn, "glaiverush")

    # the HP cost of these moves arrives as a '-damage' with no '[from]' tag
    # right after this |move| line and must not count toward times_attacked
    # (sim/battle-actions.ts:990-996 only counts hits from another pokemon).
    # Curse only costs HP when the user is Ghost-type; for the others a failed
    # use emits |-fail|, which clears the expectation
    if move_name in SELF_COST_DAMAGE_MOVES and (
        move_name != "curse" or "ghost" in pkmn.types
    ):
        pkmn.self_cost_damage_expected = True

    if (
        any(msg == "[from]Sleep Talk" for msg in split_msg)
        and battle.generation == "gen3"
    ):
        pkmn.gen_3_consecutive_sleep_talks += 1
        logger.info(
            "{} gen3 consecutive sleep talks: {}".format(
                pkmn.name, pkmn.gen_3_consecutive_sleep_talks
            )
        )
    elif move_name != "sleeptalk":
        pkmn.gen_3_consecutive_sleep_talks = 0

    # in gen1, if you successfully hit with a partially trapping move, the volatile is applied here
    # cannot use the 'cant' message because a slow wrap still needs the volatile/duration applied
    # e.g. |move|p1a: Dragonite|Wrap|p2a: Tauros|
    # does not activate on a miss: |move|p1a: Dragonite|Wrap|p2a: Tauros|[miss]
    if (
        battle.generation == "gen1"
        and all_move_json.get(move_name, {}).get(constants.VOLATILE_STATUS)
        == constants.PARTIALLY_TRAPPED
        and not any(msg == "[miss]" for msg in split_msg)
    ):
        opposing_pkmn.volatile_status_durations[constants.PARTIALLY_TRAPPED] += 1
        if constants.PARTIALLY_TRAPPED not in opposing_pkmn.volatile_statuses:
            opposing_pkmn.volatile_statuses.append(constants.PARTIALLY_TRAPPED)

        logger.info(
            f"{pkmn.name} successfully used Wrap, incrementing partially trapped volatile on "
            f"{opposing_pkmn.name} to {opposing_pkmn.volatile_status_durations[constants.PARTIALLY_TRAPPED]}"
        )

    # in gen1 if you just moved, you are released from partially trapped
    if battle.generation == "gen1" and (
        pkmn.volatile_status_durations[constants.PARTIALLY_TRAPPED] > 0
        or constants.PARTIALLY_TRAPPED in pkmn.volatile_statuses
    ):
        logger.info(f"{pkmn.name} used a move, removing partially trapped volatile")
        remove_volatile(pkmn, constants.PARTIALLY_TRAPPED)
        pkmn.volatile_status_durations[constants.PARTIALLY_TRAPPED] = 0

    # gen1 stat modification glitches.
    # swordsdance and agility nullify the effects of burn and paralysis respectively
    # This is implemented by setting a custom volatile
    if battle.generation == "gen1":
        if (
            move_name == "swordsdance" or move_name == "meditate"
        ) and pkmn.status == constants.BURN:
            logger.info(
                "{} used swordsdance with burn, nullifying the effects of burn".format(
                    pkmn.name
                )
            )
            pkmn.volatile_statuses.append("gen1burnnullify")
        elif move_name == "agility" and pkmn.status == constants.PARALYZED:
            logger.info(
                "{} used agility while paralyzed, nullifying the effects of paralysis".format(
                    pkmn.name
                )
            )
            pkmn.volatile_statuses.append("gen1paralysisnullify")

    if split_msg[-1] == "[from]Sleep Talk" or split_msg[-1] == "[from]move: Sleep Talk":
        move_object = pkmn.get_move(move_name)
        if move_object is None:
            _record_used_move(battle, side, pkmn, move_name)
            logger.info(
                "Added unrevealed {} to {}'s moves because it was called by sleeptalk".format(
                    move_name, pkmn.name
                )
            )
        return

    elif has_non_lockedmove_from_tag(split_msg):
        if split_msg[-1].startswith("[from] ability:"):
            ability = normalize_name(split_msg[-1].split("ability: ")[-1])
            logger.info("Setting {}'s ability to: {}".format(pkmn.name, ability))
            pkmn.ability = ability
        return

    if "destinybond" in pkmn.volatile_statuses:
        logger.info("Removing destinybond from {}".format(pkmn.name))
        remove_volatile(pkmn, "destinybond")

    # legacy pre-gen5 modeling only: gen5+ ticks encore/taunt at end-of-turn
    # (see upkeep) because PS decrements both at residuals whether or not the
    # affected mon moved (encore onResidualOrder 16, taunt 15) and the engine's
    # end-of-turn arms consume the counters the same way
    # (genx/generate_instructions.rs:6687-6805)
    if (
        "encore" in pkmn.volatile_statuses
        and battle.generation in constants.TAUNT_ENCORE_DURATION_INCREMENT_ON_MOVE
    ):
        pkmn.volatile_status_durations["encore"] += 1
        logger.info(
            "Incrementing encore duration for {} to {}".format(
                pkmn.name, pkmn.volatile_status_durations["encore"]
            )
        )

    # remove a two-turn charge volatile when its move is used again (the
    # release turn of Fly/Phantom Force/Solar Beam/...): PS stores the charging
    # state as a volatile named after the move (data/moves.ts twoturnmove
    # condition) and removes it when the move executes. Restrict the strip to
    # moves with the `charge` flag (data/moves.ts flags.charge): a FAILED
    # re-use of Substitute/Magnet Rise/... also produces a |move| line whose
    # name matches the real tracked volatile, and stripping there deleted
    # live Substitute/Magnet Rise state
    if move_name in pkmn.volatile_statuses and all_move_json.get(move_name, {}).get(
        "flags", {}
    ).get("charge"):
        logger.info("Removing volatile status {} from {}".format(move_name, pkmn.name))
        remove_volatile(pkmn, move_name)

    if move_name == "struggle":
        logger.info("Not adding struggle to {}'s moves".format(pkmn.name))
        # Struggle is in no moveset, but it IS the mon's lastMove: PS calls
        # `moveUsed()` for it like any other move, and Encore reads that to FAIL
        # (struggle carries `failencore` and owns no moveSlot, data/moves.ts:18205ff
        # and :4725ff). Returning here without recording it left the PREVIOUS move
        # standing as last_used_move, and the state builder then serialized that
        # move's slot -- so the engine let Encore succeed against a Struggling mon and
        # redirected its next action into the stale slot (synth30440 T20: Gogoat's
        # Bulk Up became a redirected Milk Drink; synth46824 T60: Snorlax's Curse).
        side.last_used_move = LastUsedMove(
            pokemon_name=pkmn.name, move="struggle", turn=battle.turn
        )
        return

    if move_name == "healingwish":
        logger.info(
            "{} used healingwish, setting side_condition to 1".format(pkmn.name)
        )
        side.side_conditions[constants.HEALING_WISH] = 1

    pkmn.moves_used_since_switch_in.add(move_name)

    # add the move to it's moves if it hasn't been seen
    # decrement the PP by one
    # if the move is unknown, do nothing
    #
    # PS charges PP exactly ONCE per lock, on the turn the lock is initiated.
    # sim/battle-actions.ts:279-291 only calls `pokemon.deductPP(baseMove, ...)`
    # when `pokemon.getLockedMove()` is falsy; on a continuation turn it takes
    # the else-branch and merely sets `sourceEffect = conditions.get(
    # 'lockedmove')`. So a 3-turn Outrage costs 1 PP total, and the release
    # turn of a two-turn charge move (which is also a locked continuation)
    # costs 0 - the charge turn already paid.
    #
    # Pressure follows the same gate. The extra PP is deducted in useMoveInner
    # (sim/battle-actions.ts:472-484) behind
    #     if (!sourceEffect || callerMoveForPressure)
    # where `callerMoveForPressure = sourceEffect && sourceEffect.pp ? ... :
    # null`. On a continuation `sourceEffect` is the `lockedmove` CONDITION,
    # which has no `.pp`, so callerMoveForPressure is null and the guard is
    # false -> no Pressure drop either. On the initiating turn sourceEffect is
    # undefined, so Pressure adds its 1 on top of the base 1 (total 2).
    if is_lockedmove_continuation(split_msg):
        pp_to_decrement = 0
        logger.info(
            "{}'s {} is a lockedmove continuation - PS does not deduct PP".format(
                pkmn.name, move_name
            )
        )
    else:
        pp_to_decrement = 2 if opposing_pkmn.ability == "pressure" else 1
    move_object = pkmn.get_move(move_name)
    if move_object is None:
        new_move = _record_used_move(battle, side, pkmn, move_name)
        if new_move is not None:
            new_move.current_pp -= pp_to_decrement
    else:
        move_object.current_pp -= pp_to_decrement
        logger.info(
            "{} already has the move {}. Decrementing the PP by {}".format(
                pkmn.name, move_name, pp_to_decrement
            )
        )

    # if this pokemon used two different moves without switching,
    # set a flag to signify that it cannot have a choice item
    if (
        is_opponent(battle, split_msg)
        and side.last_used_move.pokemon_name == side.active.name
        and side.last_used_move.move != move_name
    ):
        logger.info(
            "{} used two different moves - it cannot have a choice item".format(
                pkmn.name
            )
        )
        pkmn.can_have_choice_item = False
        if pkmn.item in constants.CHOICE_ITEMS and pkmn.item_inferred:
            logger.warning(
                "{} has a choice item, but used two different moves - setting it's item to UNKNOWN".format(
                    pkmn.name
                )
            )
            pkmn.item = constants.UNKNOWN_ITEM

    if unlikely_to_have_choice_item(move_name):
        logger.info(
            "{} using {} makes it unlikely to have a choice item. Setting can_have_choice_item to False".format(
                pkmn.name, move_name
            )
        )
        pkmn.can_have_choice_item = False

    try:
        mv = all_move_json[move_name]
        move_type = mv[constants.TYPE]
        if mv[constants.CATEGORY] != constants.STATUS:
            logger.info(
                "{} used a {} move, removing {}gem from possible items".format(
                    pkmn.name, move_type, move_type
                )
            )
            pkmn.impossible_items.add("{}gem".format(move_type))
    except KeyError:
        pass

    try:
        if (
            all_move_json[move_name][constants.SELF][constants.VOLATILE_STATUS]
            == constants.LOCKED_MOVE
        ):
            logger.info("Adding lockedmove to {}".format(pkmn.name))
            pkmn.volatile_statuses.append(constants.LOCKED_MOVE)
    except KeyError:
        pass

    try:
        if all_move_json[move_name][constants.CATEGORY] == constants.STATUS:
            logger.info(
                "{} used a status-move. Adding `assaultvest` to impossible items".format(
                    pkmn.name
                )
            )
            pkmn.impossible_items.add(constants.ASSAULT_VEST)
    except KeyError:
        pass

    try:
        category = all_move_json[move_name][constants.CATEGORY]
        logger.info("Setting {}'s last used move: {}".format(pkmn.name, move_name))
        if not any(
            "[from]move: Sleep Talk" in msg or "[from]Sleep Talk" in msg
            for msg in split_msg
        ):
            side.last_used_move = LastUsedMove(
                pokemon_name=pkmn.name, move=move_name, turn=battle.turn
            )
    except KeyError:
        category = None
        if not any(
            "[from]move: Sleep Talk" in msg or "[from]Sleep Talk" in msg
            for msg in split_msg
        ):
            side.last_used_move = LastUsedMove(
                pokemon_name=pkmn.name, move=constants.DO_NOTHING_MOVE, turn=battle.turn
            )

    # if this pokemon used a damaging move, eliminate the possibility of guessing a lifeorb
    # the lifeorb will reveal itself if it has it
    if category in constants.DAMAGING_CATEGORIES and not any(
        [
            normalize_name(a) in ["sheerforce", "magicguard"]
            for a in pokedex[pkmn.name][constants.ABILITIES].values()
        ]
    ):
        logger.info(
            "{} used a damaging move - not guessing lifeorb anymore".format(pkmn.name)
        )
        pkmn.impossible_items.add(constants.LIFE_ORB)

    # there is nothing special in the protocol for "wish" - it must be extracted here.
    # `Battler.arm_wish` carries PS's `addSlotCondition` rule: a Wish used while one
    # is already pending on the slot FAILS and must not re-arm (sim/side.ts:472-474;
    # Wish has no `onRestart`).  The `[still]` test in the condition is NOT that
    # rule and does not catch it -- `|move|p2a: Farigiraf|Wish||[still]` puts the
    # tag at split_msg[5], so that test reads the empty target field and passes.
    # synth41888: Farigiraf wished on T24 and wished again on T25 into a `|-fail|`;
    # the re-arm left wish=(1,181) live in T26's pre-state and would have
    # manufactured a phantom `Heal SideTwo: 181` there.
    #
    # KNOWN RESIDUAL, named rather than silently left: a Wish that fails for a
    # reason OTHER than "the slot already holds one" (Heal Block, Taunt-adjacent
    # blocks) still arms here, because this handler sees one protocol line and the
    # `|-fail|` arrives on the next.  Closing it needs the lookahead shape
    # `check_choicescarf` already uses (`msg_lines[i+1:]`); not done here because
    # no corpus reproduction was in hand and an unreproduced fix is a guess.
    if move_name == constants.WISH and "still" not in split_msg[4]:
        if side.arm_wish(side.active.max_hp / 2):
            logger.info(
                "{} used wish - expecting {} health of recovery next turn".format(
                    side.active.name, side.active.max_hp / 2
                )
            )

    if move_name == "batonpass":
        side.baton_passing = True

    # |move|p1a: Slaking|Earthquake|p2a: Heatran
    if pkmn.ability == "truant" or pkmn.name == "slaking":
        if "truant" not in pkmn.volatile_statuses:
            logger.info("Adding 'truant' to {}'s volatiles".format(pkmn.name))
            pkmn.volatile_statuses.append("truant")


def setboost(battle, split_msg):
    if is_opponent(battle, split_msg):
        pkmn = battle.opponent.active
    else:
        pkmn = battle.user.active

    stat = constants.STAT_ABBREVIATION_LOOKUPS[split_msg[3].strip()]
    amount = int(split_msg[4].strip())

    pkmn.boosts[stat] = amount


def boost(battle, split_msg):
    if is_opponent(battle, split_msg):
        side = battle.opponent
    else:
        side = battle.user
    pkmn = side.active

    stat = constants.STAT_ABBREVIATION_LOOKUPS[split_msg[3].strip()]
    amount = int(split_msg[4].strip())

    # PS `Battle#boost` stamps `target.statsRaisedThisTurn = true` whenever any component
    # of the boost is positive (sim/battle.ts:2082), regardless of source -- own move,
    # item, ability, secondary. Alluring Voice / Burning Jealousy read it.
    if amount > 0:
        side.stats_raised_this_turn = True

    pkmn.boosts[stat] = min(pkmn.boosts[stat] + amount, constants.MAX_BOOSTS)
    logger.info(
        "{}'s {} was boosted by {} to {}".format(
            pkmn.name, stat, amount, pkmn.boosts[stat]
        )
    )


def unboost(battle, split_msg):
    if is_opponent(battle, split_msg):
        pkmn = battle.opponent.active
    else:
        pkmn = battle.user.active

    stat = constants.STAT_ABBREVIATION_LOOKUPS[split_msg[3].strip()]
    amount = int(split_msg[4].strip())

    pkmn.boosts[stat] = max(pkmn.boosts[stat] - amount, -1 * constants.MAX_BOOSTS)
    logger.info(
        "{}'s {} was unboosted by {} to {}".format(
            pkmn.name, stat, amount, pkmn.boosts[stat]
        )
    )


def status(battle, split_msg):
    if is_opponent(battle, split_msg):
        other_side = battle.user
        pkmn = battle.opponent.active
    else:
        pkmn = battle.user.active
        other_side = battle.opponent

    if len(split_msg) > 4 and "item: " in split_msg[4]:
        item = normalize_name(split_msg[4].split("item:")[-1])
        owner = _record_revealed_item(
            battle,
            battle.opponent if is_opponent(battle, split_msg) else battle.user,
            pkmn,
            item,
        )
        if owner is not None:
            owner.item = item

    if len(split_msg) == 5 and split_msg[3] == "slp":
        if split_msg[4] == "[from] move: Rest":
            logger.info("Setting rest_turns to 3 for {}".format(pkmn.name))
            pkmn.rest_turns = 3
        else:
            logger.info("Setting sleep_turns to 0 for {}".format(pkmn.name))
            pkmn.sleep_turns = 0

    status_name = split_msg[3].strip()
    logger.info("{} got status: {}".format(pkmn.name, status_name))
    pkmn.status = status_name

    if status_name is not None:
        logger.info(
            "No longer guessing lumberry because {} got status {}".format(
                pkmn.name, status_name
            )
        )
        pkmn.impossible_items.add("lumberry")

    # ["", "-status", "p1a: Caterpie", "brn", "[from] ability: Flame Body", "[of] p2a: Caterpie"]
    if (
        len(split_msg) > 5
        and split_msg[4].startswith("[from] ability: ")
        and split_msg[5].startswith("[of]")
        and split_msg[5].startswith(f"[of] {other_side.name}")
    ):
        ability = normalize_name(split_msg[4].split("ability: ")[-1])
        logger.info("Setting {}'s ability to: {}".format(pkmn.name, ability))
        other_side.active.ability = ability


# ---------------------------------------------------------------------------
# Substitute absorption: interval reconstruction (NOT a point estimate)
# ---------------------------------------------------------------------------
# A hit a Substitute absorbs never puts its magnitude on the wire, so the sub's
# remaining HP after one is a RANGE, not a number.  Everything below exists so
# that range is derived honestly:
#
#  * the absorbing hit is identified from the PROTOCOL BLOCK, not from the
#    attacker's `last_used_move` -- a DELAYED move (Future Sight / Doom Desire)
#    resolves in its own context, from a source that may not even be on the
#    field, and `last_used_move` then names an unrelated move (synth18317 T42:
#    `last_used_move = "switch meloetta"`, absorbed estimated as 0, and the sub
#    walked into T46 four turns stale at its full 82);
#  * the damage is bounded by the engine's FULL 16-roll set, whose crit arm is
#    selected by the `|-crit|` the protocol really does emit for a sub-absorbed
#    hit (PS `getDamage` -> `modifyDamage` adds `-crit` with
#    `suppressMessages` false, sim/battle-actions.ts:1814, and the substitute
#    condition calls `getDamage` without suppressing, data/moves.ts:18340);
#  * anything not derivable raises a NAMED refusal bucket and widens the
#    interval to the honest maximum instead of leaving a stale number behind
#    (HANDOFF section 4 rule 4).
#
# Refusal buckets, by name.  These are COUNTED and surfaced by the replay
# checker; the previous code swallowed every failure into a bare
# `except Exception: logger.warning`, which is the silent-no-op trap of HANDOFF
# section 4 rule 13 -- a stale substitute_health that nothing reports is
# indistinguishable from a correct one.
SUBSTITUTE_ABSORB_REFUSALS = Counter()

# DISJOINT negative controls.  Each flag forces exactly ONE of this wave's four
# substitute mechanisms back to its pre-wave behaviour and nothing else, so an
# arm measures that mechanism alone (HANDOFF section 4 rule 18: a flag that
# gates two things makes its arm's label a guess).  Read per call, never cached,
# so a probe can flip one inside a single process.
#   FP_CONTROL_SUB_LAST_USED_MOVE  -- identify the absorbing hit from the
#       attacker's `last_used_move` instead of the protocol block
#   FP_CONTROL_SUB_LIVE_TRACKING   -- derive from the live-tracking sets instead
#       of the full-knowledge sidecar
#   FP_CONTROL_SUB_POINT_ESTIMATE  -- collapse the 16-roll interval back to
#       round(0.925 * max_non_crit)
#   FP_CONTROL_SUB_REFUSAL_IS_ZERO -- treat an underivable absorbed hit as ZERO
#       damage and leave the tracked HP untouched (the pre-wave silent no-op)
#       instead of widening the interval to its honest bounds
def _control(name):
    return os.environ.get(name, "") not in ("", "0", "false", "False")


def reset_substitute_absorb_refusals():
    SUBSTITUTE_ABSORB_REFUSALS.clear()


# Buckets whose refusal is the CORRECT answer rather than a symptom of a defect,
# so they belong at debug level: nothing upstream is wrong and nothing can be
# derived.  PS runs the whole hit pipeline once per hit of a multi-hit move
# (sim/battle-actions.ts:889 `for (hit = 1; hit <= targetHits; hit++)`), so the
# substitute's `onTryPrimaryHit` calls `getDamage` and emits one
# `-activate ... [damage]` PER HIT with an independent roll
# (data/moves.ts:18335-18355), while `calculate_damage_rolls_full`
# (poke-engine-py/src/lib.rs:1540-1596) returns exactly ONE (noncrit, crit) pair
# per side with no hit index and no hit count -- the per-hit roll set the
# derivation would need does not exist in the API.  A delayed move resolves from
# a source that need not be on the field at all, which the active-vs-active
# preview cannot model.  Every bucket is still COUNTED in
# SUBSTITUTE_ABSORB_REFUSALS and exported per-name into the checker's stats
# (fp/replay/checker.py:2915-2919), so the gate loses no visibility.  Every other
# bucket stays at error level: those DO mean something upstream is wrong.
_EXPECTED_SUBSTITUTE_REFUSALS = frozenset(
    ("multi_hit_context", "delayed_move", "callback_damage")
)


def _refuse_substitute_absorb(reason, detail=""):
    SUBSTITUTE_ABSORB_REFUSALS[reason] += 1
    log = (
        logger.debug if reason in _EXPECTED_SUBSTITUTE_REFUSALS else logger.error
    )
    log(
        "REFUSING to derive an absorbed Substitute hit [%s]%s: the tracked "
        "substitute HP widens to its honest bounds instead",
        reason,
        (" " + detail) if detail else "",
    )
    return None


# Lines that CLOSE a hit context when walking backwards from an `-activate`.
# Reaching one without having found an opener means the absorbing hit cannot be
# identified from the block at all.
_HIT_CONTEXT_BOUNDARIES = frozenset(
    ("", "switch", "drag", "turn", "upkeep", "-clearallboost", "replace")
)
# `-end|<mon>|move: X` openers: the delayed moves that resolve outside any
# |move| line of their own (PS data/moves.ts futuresight/doomdesire condition
# onResidual -> trySpreadMoveHit).
_DELAYED_MOVE_ENDS = frozenset(("futuresight", "doomdesire"))


def _substitute_health_interval(pkmn):
    """(low, high) bounds of `pkmn`'s tracked Substitute HP.

    Tolerates a pokemon that only ever had the upper bound written (an older
    pickled state, a hand-built test fixture): the interval then degenerates to
    the single value, which is what the pre-interval code meant by it."""
    high = int(getattr(pkmn, "substitute_health", 0) or 0)
    low = int(getattr(pkmn, "substitute_health_low", 0) or 0)
    if low <= 0 or low > high:
        low = high
    return low, high


def _slot_of_tag(token):
    """"p2a: Tropius" -> "p2a"."""
    return token.split(":")[0].strip()


def _substitute_hit_context(msg_lines, msg_index, defender_tag):
    """Identify the hit whose damage the `-activate ... move: Substitute
    |[damage]` at `msg_lines[msg_index]` is reporting.

    Walks BACKWARDS to the line that opened the hit's context, which is the only
    place the protocol says what actually hit.  Returns a dict:

        {"kind": "move", "attacker_slot": "p2a", "move": "psyshock",
         "crit": bool, "hits": int}
        {"kind": "delayed", "move": "futuresight", "crit": bool, "hits": int}

    or None when no opener is reachable.  `hits` counts the `-activate` pings
    this same context produced for this defender: a multi-hit move emits one per
    landed hit, and the engine's damage preview has no hit index, so a caller
    must refuse rather than subtract a whole-move roll per hit.
    """
    if not msg_lines or msg_index is None:
        return None
    defender_slot = _slot_of_tag(defender_tag)
    crit = False
    hits = 0
    lo = None
    for i in range(msg_index - 1, -1, -1):
        parts = msg_lines[i].split("|")
        if len(parts) < 2:
            continue
        act = parts[1].strip()
        if act == "-crit" and len(parts) > 2 and _slot_of_tag(parts[2]) == defender_slot:
            crit = True
        elif act == "move" and len(parts) > 3:
            lo = i
            ctx = {
                "kind": "move",
                "attacker_slot": _slot_of_tag(parts[2]),
                "move": normalize_name(parts[3]),
                "crit": crit,
                "defender_slot": defender_slot,
            }
            break
        elif (
            act == "-end"
            and len(parts) > 3
            and normalize_name(parts[3].split(":")[-1]) in _DELAYED_MOVE_ENDS
        ):
            lo = i
            ctx = {
                "kind": "delayed",
                "move": normalize_name(parts[3].split(":")[-1]),
                "crit": crit,
                "defender_slot": defender_slot,
            }
            break
        elif act in _HIT_CONTEXT_BOUNDARIES:
            return None
    else:
        return None

    # count this context's absorption pings for this defender (forward, from the
    # opener to the next boundary)
    for j in range(lo + 1, len(msg_lines)):
        parts = msg_lines[j].split("|")
        if len(parts) < 2:
            continue
        act = parts[1].strip()
        if j != lo and (act in _HIT_CONTEXT_BOUNDARIES or act == "move"):
            break
        if (
            act == "-activate"
            and len(parts) > 4
            and _slot_of_tag(parts[2]) == defender_slot
            and constants.SUBSTITUTE in normalize_name(parts[3])
            and parts[4] == "[damage]"
        ):
            hits += 1
    ctx["hits"] = hits
    return ctx


def _exact_team_copy(battle):
    """A deepcopy of `battle` with the full-knowledge sidecar applied, when one
    is attached (`battle.exact_teams`, set by the replay checker).

    The absorbed-damage derivation MUST compute with the same exact sets the
    rest of the checker replays with.  Live tracking is a set of GUESSES about
    the opponent -- synth45492 T25 derived Grumpig's Psyshock from
    `UNKNOWNITEM` / the randbats default spread and got 42-50, while the sidecar
    (Choice Specs) says 63-74; the sub was left at 36 when it really held at
    most 19, and the Rapid Spin that broke it on T27 was unreproducible in every
    branch.  On the live ladder there is no sidecar and this is a plain
    deepcopy, exactly as before.
    """
    b = deepcopy(battle)
    ref = getattr(battle, "exact_teams", None)
    exact_teams = getattr(ref, "value", ref)  # fp.battle.SharedByReference
    if exact_teams and not _control("FP_CONTROL_SUB_LIVE_TRACKING"):
        from fp.replay import damage_membership

        damage_membership.apply_exact_teams(b, b.user.name, exact_teams)
    return b


def _substitute_absorbed_damage_interval(battle, defending_pkmn, ctx):
    """Closed bounds ``(lo, hi)`` on the damage the Substitute just absorbed, or
    None to REFUSE (named bucket already counted).

    PS (data/moves.ts substitute onTryPrimaryHit, :18340-18355) subtracts
    `getDamage(source, target, move)` from the sub's hp and emits
    `-activate ... move: Substitute|[damage]` only when the sub SURVIVES.  The
    magnitude is never sent, so the honest reconstruction is the engine's whole
    16-value roll set for that hit -- min and max, never a centre.
    """
    if _control("FP_CONTROL_SUB_LAST_USED_MOVE"):
        # NEGATIVE CONTROL: the pre-wave identification, which reads the
        # attacker's most-recent move and therefore cannot represent a hit that
        # was not the attacker's most-recent move.
        if defending_pkmn is battle.opponent.active:
            move = battle.user.last_used_move.move
        else:
            move = battle.opponent.last_used_move.move
        if not move:
            return _refuse_substitute_absorb("control_no_last_used_move", "")
        ctx = {
            "kind": "move",
            "move": move,
            "crit": False,
            "attacker_slot": "control",
            "defender_slot": "",
            "hits": 1,
        }
    if ctx is None:
        return _refuse_substitute_absorb("no_hit_context", defending_pkmn.name)
    if ctx["kind"] != "move":
        # A delayed move resolves from a source that is not necessarily the
        # active pokemon, and the damage preview API only models active-vs-active
        # -- there is nothing to derive it from without guessing which mon and
        # which stats.  REFUSE.
        return _refuse_substitute_absorb(
            "delayed_move", "{} into {}".format(ctx["move"], defending_pkmn.name)
        )
    if (
        ctx["hits"] != 1
        or all_move_json.get(ctx["move"], {}).get("multihit")
        or ctx["move"] == "beatup"
    ):
        # `calculate_damage_rolls_full` takes no hit index: for a multi-hit move
        # it previews ONE hit at the move's nominal `Choice::base_power`, which
        # is the base power of no particular strike -- Triple Axel's engine base
        # is 40 (poke-engine src/choices.rs:18434-18446) while the real per-hit
        # powers 20/40/60 are applied only on the search path, in
        # `build_multihit_damage_plans` (src/genx/generate_instructions.rs:
        # 14014-14025).  So the set cannot be subtracted for ANY multi-hit move,
        # whatever the ping count.  The old `hits != 1` test only caught a move
        # that produced MORE THAN ONE absorption ping and missed the common
        # shape where hit 1 is absorbed and hit 2 breaks the sub, which emits
        # exactly one `-activate` (synth82368 T27: Technician Cinccino's Triple
        # Axel into Enamorus' 61 hp sub previewed 96-114 -- hit 2's damage --
        # against an honest hit-1 set of 49-58, producing a bogus
        # [survival_contradiction] while both the sub HP and the engine were
        # right).  Beat Up is listed by name because PS builds its hit count in
        # `onModifyMove` (data/moves.ts:1165-1168, one hit per healthy team
        # member, each at its own basePowerCallback power), so the static dex
        # entry this reads carries no `multihit` key.
        return _refuse_substitute_absorb(
            "multi_hit_context", "{} x{}".format(ctx["move"], ctx["hits"])
        )
    if ctx["attacker_slot"] == ctx["defender_slot"]:
        # PS returns early when `target === source` (data/moves.ts:18336), so a
        # sub can never absorb its own owner's move: this opener is not the hit.
        return _refuse_substitute_absorb("self_move_context", ctx["move"])

    if battle.user.active is None or battle.opponent.active is None:
        return _refuse_substitute_absorb("no_active", ctx["move"])
    b = _exact_team_copy(battle)
    if defending_pkmn is battle.opponent.active:
        # the bot attacked the opponent's substitute
        sets, _ = poke_engine_get_damage_roll_sets(b, ctx["move"], "none", True)
    else:
        # the opponent attacked the bot's substitute
        _, sets = poke_engine_get_damage_roll_sets(b, "none", ctx["move"], False)
    if sets is None:
        # either a wheel without the full export, or a choice the engine says
        # deals no damage at all
        return _refuse_substitute_absorb("no_roll_set", ctx["move"])

    non_crit, crit_rolls = sets
    rolls = crit_rolls if ctx["crit"] else non_crit
    if not rolls:
        return _refuse_substitute_absorb("empty_roll_set", ctx["move"])
    lo, hi = int(min(rolls)), int(max(rolls))
    if hi <= 0:
        # An all-zero roll set is the CORRECT output of the active-vs-active
        # preview for a move whose damage the FORMULA does not produce: PS gives
        # these `basePower: 0` plus a `damageCallback` (data/moves.ts counter
        # :2987 / mirrorcoat :11986 / metalburst / comeuppance / superfang /
        # endeavor / psywave / finalgambit / ruination / naturesmadness /
        # guardianofalola), and poke-engine mirrors that with a `Choice` whose
        # base_power is left at 0, pushing the real amount from `choice_effects`
        # (src/genx/choice_effects.rs:2204 MIRRORCOAT doubles
        # `damage_dealt.damage`).  Nothing upstream is wrong and nothing is
        # derivable, so this is an EXPECTED refusal -- unlike a zero on a move
        # that DOES have base power, which keeps its error-level report.
        if all_move_json.get(ctx["move"], {}).get("basePower") == 0:
            return _refuse_substitute_absorb("callback_damage", ctx["move"])
        return _refuse_substitute_absorb("zero_damage", ctx["move"])
    if _control("FP_CONTROL_SUB_POINT_ESTIMATE"):
        # NEGATIVE CONTROL: the pre-wave point estimate -- the 0.925 "median"
        # of the max non-crit roll, collapsed to a degenerate interval.
        point = int(round(int(max(non_crit)) * 0.925))
        return point, point
    return max(0, lo), hi


def activate(battle, split_msg, msg_lines=None, msg_index=None):
    """`msg_lines`/`msg_index` are this line's position in the resolution block.

    They are optional so a caller with a single line in hand (unit tests, and
    any consumer that predates the substitute work) still works; without them
    the substitute-absorption reconstruction REFUSES rather than falling back to
    `last_used_move`, which cannot represent a delayed move.
    """
    if is_opponent(battle, split_msg):
        side = battle.opponent
        pkmn = battle.opponent.active
        other_pkmn = battle.user.active
    else:
        side = battle.user
        pkmn = battle.user.active
        other_pkmn = battle.opponent.active

    if (
        constants.SUBSTITUTE in normalize_name(split_msg[3])
        and len(split_msg) > 4
        and split_msg[4] == "[damage]"
    ):
        # NB: the real protocol line is `|-activate|<pkmn>|move: Substitute|[damage]`
        # (split_msg[3] == "move: Substitute"), so match on the substring rather
        # than an exact "substitute" compare.
        logger.info(
            "{}'s substitute took damage, setting substitute_hit to True".format(
                pkmn.name
            )
        )
        pkmn.substitute_hit = True
        # the hit landed on the substitute, so the mon's own HP did not move and
        # a fixed-damage identity armed on it never resolves
        hp_certificate.disarm(pkmn)

        # Whittle the tracked substitute HP INTERVAL by the absorbed hit.
        # `-activate` is only emitted when the sub survives (PS moves.ts
        # :18350-18355), so both bounds floor at 1.  Guard on an actually-tracked
        # sub so unit tests that call activate() with a bare state (and real
        # reconnect states) skip the engine damage calc.
        if (
            constants.SUBSTITUTE in pkmn.volatile_statuses
            and getattr(pkmn, "substitute_health", 0) > 0
        ):
            prior_hi = int(getattr(pkmn, "substitute_health", 0) or 0)
            prior_lo = int(getattr(pkmn, "substitute_health_low", prior_hi) or 0)
            if prior_lo <= 0 or prior_lo > prior_hi:
                prior_lo = prior_hi
            try:
                ctx = _substitute_hit_context(msg_lines, msg_index, split_msg[2])
                interval = _substitute_absorbed_damage_interval(battle, pkmn, ctx)
            except Exception as e:
                # LOUD, and counted: a derivation that blew up is a refusal, not
                # a no-op (HANDOFF section 4 rule 13).
                interval = _refuse_substitute_absorb(
                    "estimator_error", "{}: {!r}".format(pkmn.name, e)
                )
            if interval is None and _control("FP_CONTROL_SUB_REFUSAL_IS_ZERO"):
                # NEGATIVE CONTROL: the pre-wave behaviour, in which a
                # derivation that produced nothing left the tracked HP exactly
                # where it was -- a silent no-op that reads identically to a
                # correct derivation (HANDOFF section 4 rule 13).
                new_lo, new_hi = prior_lo, prior_hi
            elif interval is None:
                # REFUSED.  All that is still known is what the protocol itself
                # says: the sub survived (so it holds at least 1) and it cannot
                # have gained HP.  Widening to [1, prior_hi] keeps every later
                # assertion honestly undecidable instead of asserting against a
                # number nothing derived.
                new_lo, new_hi = 1, prior_hi
            elif interval[0] >= prior_hi and not _control(
                "FP_CONTROL_SUB_POINT_ESTIMATE"
            ):
                # CONTRADICTION, and it is reported rather than papered over: the
                # SMALLEST roll the engine allows this hit already meets the
                # LARGEST HP the sub could be holding, yet PS emitted `-activate`,
                # which it only does when the sub survived.  Something upstream
                # (the creation seed, an earlier absorption, the attacker's
                # reconstructed set) is wrong, so nothing here may be asserted.
                _refuse_substitute_absorb(
                    "survival_contradiction",
                    "{}: rolls >= {} vs tracked sub <= {}".format(
                        pkmn.name, interval[0], prior_hi
                    ),
                )
                new_lo, new_hi = 1, prior_hi
            else:
                absorbed_lo, absorbed_hi = interval
                # the sub SURVIVED, so the hit was strictly smaller than the HP it
                # was holding -- which bounds the absorbed damage from above by
                # more than the roll set does, and narrows the survivor's floor
                absorbed_hi = min(absorbed_hi, max(0, prior_hi - 1))
                new_lo = max(1, prior_lo - absorbed_hi)
                new_hi = max(1, prior_hi - absorbed_lo)
            if new_lo > new_hi:
                new_lo = new_hi
            logger.info(
                "{}'s substitute absorbed a hit: [{}, {}] -> [{}, {}]".format(
                    pkmn.name, prior_lo, prior_hi, new_lo, new_hi
                )
            )
            pkmn.substitute_health = new_hi
            pkmn.substitute_health_low = new_lo

    if split_msg[3].lower() == "move: poltergeist":
        item = normalize_name(split_msg[4])
        owner = _record_revealed_item(battle, side, pkmn, item)
        if owner is not None:
            logger.info("{} has the item {}".format(owner.name, item))
            owner.item = item

    if split_msg[3].lower().startswith("ability: "):
        ability = normalize_name(split_msg[3].split(":")[-1].strip())
        logger.info("Setting {}'s ability to {}".format(pkmn.name, ability))
        pkmn.ability = ability

        # DANCER: PS announces the copy with `|-activate|<mon>|ability: Dancer`
        # and then RE-ENTERS runMove for that mon with `externalMove: true`
        # (sim/battle-actions.ts:338-341).  The copy's own `|move|` line
        # normally carries `[from] ability: Dancer` (:448-457, built from
        # `sourceEffect`), but when a Choice lock or Gorilla Tactics aborts it
        # in onBeforeMove the line is printed by the CONDITION instead, as a
        # bare `addMove('move', pokemon, move.name)` + `attrLastMove('[still]')`
        # + `|-fail|` (data/conditions.ts:342-345, data/abilities.ts:1652-1655)
        # -- no `[from]` tag, and textually IDENTICAL to a self-chosen move that
        # was `[still]`ed (attrLastMove blanks the target field for any move
        # that plays no animation, sim/battle.ts:3128-3132, which is where the
        # corpus's thousands of legitimate `|move|X|Roost||[still]` lines come
        # from).  So the copy has to be recognised from THIS line, not from the
        # |move| line's text.  One-shot marker, consumed by `move()`/`cant()`.
        if ability == "dancer":
            pkmn.dancer_copy_pending = True

        # Battle Bond announces its once-per-battle trigger as
        # `|-activate|<pkmn>|ability: Battle Bond`
        _mark_once_per_battle_ability_used(pkmn, ability)

        if ability in ["mummy", "lingeringaroma"]:
            original_ability = normalize_name(split_msg[4])
            other_pkmn.ability = ability
            other_pkmn.original_ability = original_ability
            logger.info(
                "{}'s ability was changed from {} to {}".format(
                    other_pkmn.name, original_ability, ability
                )
            )

    elif split_msg[3].lower().startswith("item: ") and not any(
        i == "[consumed]" for i in split_msg
    ):
        item = normalize_name(split_msg[3].split(":")[-1].strip())
        owner = _record_revealed_item(battle, side, pkmn, item)
        if owner is not None:
            logger.info("Setting {}'s item to {}".format(owner.name, item))
            owner.item = item

    if split_msg[3].lower().startswith("move: "):
        move_name = normalize_name(split_msg[3].split(":")[-1].strip())
        if (
            move_name in all_move_json
            and all_move_json[move_name].get("volatileStatus")
            == constants.PARTIALLY_TRAPPED
        ):
            logger.info("{} was partially trapped by {}".format(pkmn.name, move_name))
            pkmn.volatile_statuses.append(constants.PARTIALLY_TRAPPED)

    if normalize_name(split_msg[3]) == constants.CONFUSION:
        # PS emits `|-activate|<mon>|confusion` on every before-move confusion
        # check where confusion did NOT wear off (data/conditions.ts:186 -
        # after the wear-off early-return at :180-185, before the 33% self-hit
        # roll at :187) and `|-end|<mon>|confusion` on wear-off, so counting
        # these -activate lines equals the engine's checks-survived counter
        # exactly (generate_instructions.rs ticks
        # volatile_status_durations.confusion once per surviving check).
        # Without this the root always re-seeds an aged confusion as fresh and
        # search overweights late self-hits. Clamp at 4: the engine's
        # MAX_CONFUSION_TURNS is 4 (a counter of 4 makes the next simulated
        # check force removal, and larger seeds are never produced by PS's
        # random(2,6)-1 = at most 4 surviving checks).
        if pkmn.volatile_status_durations[constants.CONFUSION] < 4:
            pkmn.volatile_status_durations[constants.CONFUSION] += 1
            logger.info(
                "Incremented confusion checks-survived count for {} to {}".format(
                    pkmn.name,
                    pkmn.volatile_status_durations[constants.CONFUSION],
                )
            )


def anim(battle, split_msg):
    if is_opponent(battle, split_msg):
        pkmn = battle.opponent.active
    else:
        pkmn = battle.user.active

    anim_name = normalize_name(split_msg[3].strip())
    if anim_name in pkmn.volatile_statuses:
        logger.info(
            "Removing volatile status {} from {} because of -anim".format(
                anim_name, pkmn.name
            )
        )
        remove_volatile(pkmn, anim_name)


def prepare(battle, split_msg):
    if is_opponent(battle, split_msg):
        pkmn = battle.opponent.active
    else:
        pkmn = battle.user.active

    being_prepared = normalize_name(split_msg[3])

    # Chilly Reception reuses the `-prepare` tag that real two-turn charge
    # moves (Fly/Dig/SolarBeam/...) use, but it is a different PS mechanic:
    # a priorityChargeCallback move (data/moves.ts:2396-2420) whose volatile
    # is added SILENTLY by `source.addVolatile('chillyreception')` before
    # this message, and whose `condition` has no onEnd - so when the
    # duration-1 volatile naturally expires, `Pokemon#removeVolatile`
    # (sim/pokemon.ts:2040-2051) emits no matching `-end` at all. Chilly
    # Reception's move data also has empty `flags` (no `charge`), so
    # `move()`'s charge-gated two-turn-release cleanup never strips it
    # either. Nothing downstream in this tracker ever reads
    # 'chillyreception', so don't track it - a mon that re-uses the move
    # without switching out (selfSwitch has no valid replacement, e.g. the
    # last alive party member) would otherwise hit a spurious "already has
    # the volatile status" warning on every subsequent use.
    if being_prepared == "chillyreception":
        logger.info(
            "{} used Chilly Reception - not tracking it as a persistent "
            "volatile (PS never emits a matching -end for it)".format(pkmn.name)
        )
        return

    if being_prepared in pkmn.volatile_statuses:
        logger.warning(
            "{} already has the volatile status {}".format(pkmn.name, being_prepared)
        )
    else:
        logger.info(
            "Adding the volatile status {} to {}".format(being_prepared, pkmn.name)
        )
        pkmn.volatile_statuses.append(being_prepared)


def terastallize(battle, split_msg):
    if is_opponent(battle, split_msg):
        pkmn = battle.opponent.active
    else:
        pkmn = battle.user.active

    pkmn.terastallized = True
    pkmn.tera_type = normalize_name(split_msg[3])
    logger.info(
        "{} terastallized. Tera type: {}, Original types: {}".format(
            pkmn.name, pkmn.tera_type, pkmn.types
        )
    )


def start_volatile_status(battle, split_msg):
    if is_opponent(battle, split_msg):
        pkmn = battle.opponent.active
        side = battle.opponent
    else:
        pkmn = battle.user.active
        side = battle.user

    volatile_status = normalize_name(split_msg[3].split(":")[-1])

    # for some reason futuresight is sent with the `-start` message
    # `-start` is typically reserved for volatile statuses
    if volatile_status == constants.FUTURE_SIGHT:
        side.future_sight = (3, pkmn.name)
        return

    if volatile_status.startswith("perish"):
        logger.info(
            "{} got {}. Removing other `perish` volatiles".format(
                pkmn.name, volatile_status
            )
        )
        logger.info("Starting volatiles: {}".format(pkmn.volatile_statuses))
        pkmn.volatile_statuses = [
            vs for vs in pkmn.volatile_statuses if not vs.startswith("perish")
        ]
        pkmn.volatile_statuses.append(volatile_status)
        logger.info("Ending volatiles: {}".format(pkmn.volatile_statuses))
        return

    if volatile_status not in pkmn.volatile_statuses:
        logger.info(
            "Starting the volatile status {} on {}".format(volatile_status, pkmn.name)
        )
        pkmn.volatile_statuses.append(volatile_status)

    if volatile_status == constants.SUBSTITUTE:
        if len(split_msg) >= 5 and split_msg[4] == "[from] move: Shed Tail":
            logger.info(
                "{} started a substitute from shed tail - setting shed_tailing to True".format(
                    pkmn.name
                )
            )
            side.shed_tailing = True
        logger.info(
            "{} started a substitute - setting substitute_hit to False".format(
                pkmn.name
            )
        )
        pkmn.substitute_hit = False
        # PS moves.ts substitute onStart: effectState.hp = Math.floor(maxhp/4)
        # (Shed Tail creates the sub from the passer's maxhp too - `pkmn` here is
        # the passer, so maxhp//4 is correct for both plain Substitute and Shed
        # Tail). This is the same seed the engine uses at creation.  Creation is
        # the one moment the value is EXACT, so the interval is a singleton.
        pkmn.substitute_health = pkmn.max_hp // 4
        pkmn.substitute_health_low = pkmn.max_hp // 4

    if volatile_status == constants.SLOW_START:
        # PS slowstart onStart: effectState.counter = 5 (data/abilities.ts),
        # decremented at each residual while active and ending with `-end` at
        # 0. The engine consumes the value as turns-remaining, decrementing at
        # its simulated end-of-turn and releasing at 0
        # (genx/generate_instructions.rs:6350-6370) with the same counter=5
        # apply seed, and `upkeep` decrements the root count each real turn.
        logger.info("{} started slow start - setting slow_start to 5".format(pkmn.name))
        pkmn.volatile_status_durations[constants.SLOW_START] = 5

    if (
        volatile_status in (constants.TAUNT, "encore")
        and battle.generation not in constants.TAUNT_ENCORE_DURATION_INCREMENT_ON_MOVE
    ):
        # PS taunt/encore onStart both run `if (!this.queue.willMove(target))
        # this.effectState.duration++` (data/moves.ts taunt/encore conditions):
        # when the target's action already resolved this turn - its own
        # |move|/|cant| line appeared BEFORE this `-start` in the block - the
        # lock lasts one extra end-of-turn. The engine's end-of-turn arm counts
        # UP and releases at counter==2, seeding -1 on its own slow-apply
        # branch (genx/generate_instructions.rs:1035-1071, 6687-6805); mirror
        # that seed here for the root count. The fast apply keeps seed 0,
        # which also normalizes any stale counter.
        seed = -1 if pkmn.moved_this_turn else 0
        pkmn.volatile_status_durations[volatile_status] = seed
        logger.info(
            "Seeding {} duration for {} to {} ({} this turn)".format(
                volatile_status,
                pkmn.name,
                seed,
                "already acted" if seed == -1 else "has not acted",
            )
        )

    if volatile_status == constants.DISABLE and len(split_msg) > 4:
        # `-start|<pkmn>|Disable|<Move>[|[from] ...]` - the disabled move name is
        # split_msg[4]. Mark that specific move disabled on the target and seed
        # the ~4-turn disable timer so the engine won't let it be re-selected.
        disabled_move_name = normalize_name(split_msg[4])
        for mv in pkmn.moves:
            if mv.name == disabled_move_name or (
                disabled_move_name == constants.HIDDEN_POWER
                and mv.name.startswith(constants.HIDDEN_POWER)
            ):
                mv.disabled = True
                logger.info("{} had {} disabled".format(pkmn.name, mv.name))
                break
        pkmn.volatile_status_durations[constants.DISABLE] = 4

    if volatile_status == constants.CONFUSION:
        logger.info("{} got confused, no longer guessing lumberry".format(pkmn.name))
        pkmn.impossible_items.add("lumberry")
        if split_msg[-1] == "[fatigue]":
            logger.info(
                "{} got confused from fatigue, removing lockedmove from volatile statuses".format(
                    pkmn.name
                )
            )
            remove_volatile(pkmn, constants.LOCKED_MOVE)
            side.active.volatile_status_durations[constants.LOCKED_MOVE] = 0

    if volatile_status == constants.DYNAMAX:
        pkmn.hp *= 2
        pkmn.max_hp *= 2
        hp_certificate.clear(pkmn, "dynamax HP doubling")
        logger.info(
            "{} started dynamax - doubling their HP to {}/{}".format(
                pkmn.name, pkmn.hp, pkmn.max_hp
            )
        )

    if constants.ABILITY in split_msg[3]:
        pkmn.ability = volatile_status

    if len(split_msg) == 6 and constants.ABILITY in normalize_name(split_msg[5]):
        pkmn.ability = normalize_name(split_msg[5].split("ability:")[-1])

    if volatile_status == constants.TYPECHANGE:
        # PS `Pokemon#setType` (sim/pokemon.ts:1925-1934) is a NO-OP on a
        # terastallized mon:
        #     // Terastallized Pokemon cannot have their base type changed
        #     // except via forme change
        #     if (this.terastallized) return false;
        # Almost every emitter guards on the return value
        # (`if (!target.setType(type)) return false;` -- Color Change
        # data/abilities.ts:561, Protean :3494, Libero :2314, Conversion
        # data/moves.ts:2806, Conversion 2 :2841, Camouflage :2176, Soak
        # :17193, Magic Powder :10751) so the `-start ... typechange` line is
        # never even emitted while terastallized.  Three emitters do NOT:
        #   * Reflect Type (data/moves.ts:14899) adds the message BEFORE
        #     calling setType,
        #   * Burn Up (:2110-2111) and Double Shock (:3963-3964) call setType
        #     unconditionally and then announce `pokemon.getTypes()`, which for
        #     a terastallized mon is the TERA type.
        # In all three PS leaves `pokemon.types` (the array `getTypes(false,
        # true)` reads for STAB) untouched.  Mirroring that no-op is required:
        # writing the announced types onto `pkmn.types` corrupts the
        # pre-terastallization types that decide 1.5x vs 2.0x tera STAB.
        if pkmn.terastallized:
            logger.info(
                "{} is terastallized - ignoring typechange announcement {} "
                "(PS setType is a no-op while terastallized)".format(
                    pkmn.name, split_msg[4] if len(split_msg) > 4 else ""
                )
            )
        else:
            if split_msg[4] == "[from] move: Reflect Type":
                pkmn_name = normalize_name(split_msg[5].split(":")[-1])
                new_types = deepcopy(pokedex[pkmn_name][constants.TYPES])
            else:
                new_types = [normalize_name(t) for t in split_msg[4].split("/")]

            logger.info("Setting {}'s types to {}".format(pkmn.name, new_types))
            pkmn.types = new_types


def end_volatile_status(battle, split_msg):
    if is_opponent(battle, split_msg):
        pkmn = battle.opponent.active
    else:
        pkmn = battle.user.active

    volatile_status = normalize_name(split_msg[3].split(":")[-1])
    if volatile_status == constants.SUBSTITUTE:
        logger.info("Substitute ended for {}".format(pkmn.name))
        pkmn.substitute_hit = False
        # PS moves.ts substitute: the volatile is removed once its hp hits 0
        pkmn.substitute_health = 0
        pkmn.substitute_health_low = 0

    if volatile_status == constants.DISABLE:
        # disable wore off: re-enable the move(s) and clear the timer. On the real
        # battle object only Disable ever marks an opponent move disabled, so
        # clearing every move is safe (the bot's own moves are re-derived from the
        # request JSON each turn regardless).
        for mv in pkmn.moves:
            mv.disabled = False
        pkmn.volatile_status_durations[constants.DISABLE] = 0
        logger.info("Disable ended for {}".format(pkmn.name))

    if volatile_status == "protosynthesis" or volatile_status == "quarkdrive":
        for vs in pkmn.volatile_statuses:
            if vs.startswith(volatile_status):
                logger.info("Removing {} from {}".format(vs, pkmn.name))
                pkmn.volatile_statuses.remove(vs)
    elif volatile_status.startswith("fallen"):
        # Supreme Overlord bookkeeping tags. PS announces the switch-in snapshot
        # as `|-start|<mon>|fallen{N}|[silent]` (data/abilities.ts:4727) and ends
        # it with `|-end|<mon>|fallen{effectState.fallen}|[silent]` (:4732). The
        # onEnd template is UNGUARDED: when the ability never activated
        # (side.totalFainted was 0 at switch-in, so the onStart gate at
        # :4723-4726 set no effectState.fallen) the `undefined` interpolates to
        # the literal tag `fallenundefined`. Remove whichever fallen* volatile
        # is tracked; a fallen-end with none tracked is the no-activation shape,
        # not a desync, so it must not hit the mismatch warning below.
        for vs in list(pkmn.volatile_statuses):
            if vs.startswith("fallen"):
                logger.info("Removing {} from {}".format(vs, pkmn.name))
                pkmn.volatile_statuses.remove(vs)
    elif volatile_status == "illusion":
        # PS's Illusion ability emits `-end|<mon>|Illusion` from its onEnd
        # handler (data/abilities.ts:2066-2077), always paired with a
        # `replace` message in the same event. Our tracker fully reconciles
        # species/hp/status/boosts/item off that `replace` line via the
        # dispatch table's "replace": illusion_end handler, and "illusion"
        # is never added to pkmn.volatile_statuses (it's ability state, not
        # a PS volatile), so this line is an expected no-op, not a desync.
        logger.info(
            "Illusion ended for {} (already handled via replace)".format(pkmn.name)
        )
    elif volatile_status == constants.FUTURE_SIGHT:
        # PS prints the two halves of a delayed attack against OPPOSITE sides:
        # `onTry` emits `|-start|<SOURCE>|move: Future Sight` (data/moves.ts:6420)
        # while the futuremove slot condition's `onEnd` emits
        # `|-end|<TARGET>|move: Future Sight` when it resolves
        # (data/conditions.ts:403).  The pending attack is tracked as
        # `side.future_sight` on the side that USED it - `start_volatile_status`
        # returns before touching `volatile_statuses` - so the mon this `-end`
        # names never had a `futuresight` volatile and its absence is not a
        # desync.  The countdown must NOT be cleared here: PS resolves the hit
        # two turns after the use (synth70040, `-start` T40 -> `-end` in T42's
        # residual), the tracker seeds 3 and `upkeep` decrements once per turn,
        # so the attacking side reads exactly (1, <attacker>) while this line is
        # parsed - the value the engine fires on
        # (genx/generate_instructions.rs:11309-11314) - and the same turn's
        # `upkeep` zeroes it.  Clearing early would also break the
        # `future_sight[0] != 1` guard that keeps the live-play Zoroark inference
        # from reading the delayed hit as an unexplained damage mismatch (:3334).
        logger.info(
            "Future Sight resolved into {} - tracked as side.future_sight on the "
            "attacking side, never as a volatile here".format(pkmn.name)
        )
    elif (
        volatile_status == constants.SLOW_START
        and volatile_status not in pkmn.volatile_statuses
    ):
        # Slow Start is ABILITY state in PS, not a volatile, and it announces its
        # end TWICE.  `onResidual` emits `|-end|<mon>|Slow Start` when the counter
        # reaches 0 and then DELETES the counter (data/abilities.ts:4308-4316);
        # the ability's `onEnd` emits a second `|-end|<mon>|Slow Start|[silent]`
        # whenever the ability stops applying (:4328-4331), guarded ONLY by
        # `if (pokemon.beingCalledBack) return;`.  A FAINT is not a call-back, so
        # a Regigigas that already outlasted its five turns and then faints prints
        # the silent `-end` with nothing left to end (synth70008: `-end` at the
        # counter expiry, `-end ... [silent]` right after `|faint|`).  A duplicate
        # announcement, not a desync.
        logger.info(
            "{} announced the end of an already-ended Slow Start - no-op".format(
                pkmn.name
            )
        )
        pkmn.volatile_status_durations[constants.SLOW_START] = 0
    elif len(split_msg) >= 5 and constants.PARTIALLY_TRAPPED in split_msg[4]:
        remove_volatile(pkmn, constants.PARTIALLY_TRAPPED)
        # both partial-trap release shapes hit this branch (PS
        # data/conditions.ts partiallytrapped: onEnd emits
        # `-end ... [partiallytrapped]` on outlast; onResidual emits
        # `-end ... [partiallytrapped] [silent]` when the trapper left the
        # field) and both mean the trap is over. remove_volatile does not
        # touch volatile_status_durations and only this mon switching out
        # otherwise clears them, so without zeroing here a later re-trap
        # would seed the engine with a stale mid-trap elapsed count.
        pkmn.volatile_status_durations[constants.PARTIALLY_TRAPPED] = 0
    elif volatile_status not in pkmn.volatile_statuses:
        logger.warning(
            "{} does not have the volatile status '{}'. Volatiles: {}".format(
                pkmn, volatile_status, pkmn.volatile_statuses
            )
        )
        # the volatile may have been removed earlier by a path that does not
        # touch volatile_status_durations - e.g. the `-anim` handler removes a
        # volatile whose name matches the animated MOVE (a mon animating the
        # move Confusion via Sleep Talk drops the CONFUSION volatile), leaving
        # its age counter stale. This `-end` is the authoritative signal that
        # the volatile is gone, so zero any leftover counter here too.
        if volatile_status in pkmn.volatile_status_durations:
            pkmn.volatile_status_durations[volatile_status] = 0
            logger.info(
                "Zeroing {}'s stale {} duration on mismatched -end".format(
                    pkmn.name, volatile_status
                )
            )
    else:
        logger.info(
            "Removing the volatile status {} from {}".format(volatile_status, pkmn.name)
        )
        remove_volatile(pkmn, volatile_status)
        if volatile_status in pkmn.volatile_status_durations:
            pkmn.volatile_status_durations[volatile_status] = 0
            logger.info(
                "Setting {}'s {} duration to 0".format(pkmn.name, volatile_status)
            )
        if volatile_status == constants.DYNAMAX:
            pkmn.hp /= 2
            pkmn.max_hp /= 2
            hp_certificate.clear(pkmn, "dynamax HP halving")
            logger.info(
                "{} ended dynamax - halving their HP to {}/{}".format(
                    pkmn.name, pkmn.hp, pkmn.max_hp
                )
            )


def curestatus(battle, split_msg):
    if is_opponent(battle, split_msg):
        side = battle.opponent
    else:
        side = battle.user

    pkmn_name = split_msg[2].split(":")[-1].strip()

    # A slotless id (`p2: Rotom`) is by definition NOT the active
    # (see `_ident_is_slotless`): Heal Bell / Aromatherapy cure the whole party
    # and print one `-curestatus` per benched ally.  Resolving those by species
    # matters because the id carries the BASE species while the reserve holds
    # the forme (`p2: Rotom` -> `rotomfan`); the old exact-name lookup missed
    # every such ally and then "defaulted to the active", clearing the status of
    # a pokemon PS never cured and leaving the benched one statused for the rest
    # of the game.  Refuse instead of defaulting when it cannot be resolved.
    if _ident_is_slotless(split_msg[2]):
        pkmn = _find_bench_pokemon_by_protocol_name(side, pkmn_name)
        if (
            pkmn is None
            and side.active is not None
            and _names_match(side.active, pkmn_name)
        ):
            pkmn = side.active
        if pkmn is None:
            logger.warning(
                "Benched pokemon {} cured but not found in the reserve - skipping".format(
                    normalize_name(pkmn_name)
                )
            )
            return
    else:
        # a slotted id is always the active in singles
        pkmn = side.active

    # even if rest wasn't the cause of sleep, this should be set to 0
    if pkmn.status == constants.SLEEP:
        logger.info(
            "{} is being cured of sleep, setting rest_turns & sleep_turns to 0".format(
                pkmn.name
            )
        )
        pkmn.rest_turns = 0
        pkmn.sleep_turns = 0
    elif pkmn.status == constants.TOXIC and pkmn is side.active:
        # TOXIC_COUNT is this side's ACTIVE's toxic stage (PS keeps it per
        # pokemon in `statusState.stage`); a benched ally's cure must not zero
        # the active's counter
        side.side_conditions[constants.TOXIC_COUNT] = 0

    pkmn.status = None


def cureteam(battle, split_msg):
    """Cure every pokemon on the opponent's team of it's status"""
    if is_opponent(battle, split_msg):
        side = battle.opponent
    else:
        side = battle.user

    side.active.status = None
    for pkmn in filter(lambda p: isinstance(p, Pokemon), side.reserve):
        pkmn.status = None
        pkmn.rest_turns = 0
        pkmn.sleep_turns = 0


def weather(battle, split_msg):
    # The weather message on its own `|-weather|RainDance` does not contain information about
    #  which side caused it unless it was from an ability
    #  `|-weather|RainDance|[from] ability: Drizzle|[of] p2a: Politoed`
    #
    # If that information is present, we can infer certain things about the Side
    side = None
    side_name = None
    if len(split_msg) == 5:
        if battle.opponent.name in split_msg[-1]:
            side = battle.opponent
            side_name = "opponent"
        else:
            side = battle.user
            side_name = "user"

    weather_name = normalize_name(split_msg[2].split(":")[-1].strip())
    logger.info("Weather {} is active".format(weather_name))
    # `|-weather|none` means CLEARED weather, not a weather called "none".  PS's
    # `field.weather` is `''` (falsy) in that state and `effectiveWeather()`
    # returns `''`, which is what every weather consumer tests against -- e.g.
    # Weather Ball's `onModifyMove` switch (data/moves.ts:20714-20731) doubles
    # base power only for a real weather.  Storing the literal string "none"
    # here makes `battle.weather` truthy in clear weather and silently doubles
    # Weather Ball.  Normalize to None so truthiness matches PS.
    battle.weather = None if weather_name == "none" else weather_name

    if weather_name == "none":
        logger.info("Resetting weather source to None")
        battle.weather_source = None
    elif side is not None and side_name is not None:
        battle.weather_source = f"{side_name}:{side.active.name}"

    if split_msg[-1] == "[upkeep]" and battle.weather_turns_remaining > 0:
        battle.weather_turns_remaining -= 1
    elif split_msg[-1] == "[upkeep]":
        logger.debug("Weather {} permanently active".format(weather_name))
    elif (
        len(split_msg) > 3
        and battle.generation in ["gen3", "gen4", "gen5"]
        and split_msg[3].startswith("[from] ability:")
    ):
        battle.weather_turns_remaining = -1
    elif (
        side is not None
        and weather_name == constants.SUN
        and side.active.item == "heatrock"
    ):
        logger.info("{} has heatrock, assuming 8 turns of sun".format(side.active.name))
        battle.weather_turns_remaining = 8
    elif (
        side is not None
        and weather_name == constants.RAIN
        and side.active.item == "damprock"
    ):
        logger.info(
            "{} has damprock, assuming 8 turns of rain".format(side.active.name)
        )
        battle.weather_turns_remaining = 8
    elif (
        side is not None
        and weather_name == constants.SAND
        and side.active.item == "smoothrock"
    ):
        logger.info(
            "{} has smoothrock, assuming 8 turns of sand".format(side.active.name)
        )
        battle.weather_turns_remaining = 8
    elif (
        side is not None
        and weather_name in constants.HAIL_OR_SNOW
        and side.active.item == "icyrock"
    ):
        logger.info("{} has icyrock, assuming 8 turns of hail".format(side.active.name))
        battle.weather_turns_remaining = 8
    else:
        battle.weather_turns_remaining = 5

    logger.info("Weather turns remaining: {}".format(battle.weather_turns_remaining))
    if battle.weather_turns_remaining == 0:
        logger.info(
            "Weather {} did not end when expected, giving 3 more turns".format(
                weather_name
            )
        )
        battle.weather_turns_remaining = 3
        if (
            battle.weather_source is not None
            and battle.weather_source != ""
            and battle.weather_source.startswith("opponent")
        ):
            side = battle.opponent
            pkmn_name = battle.weather_source.split(":")[-1]
            pkmn = (
                side.active
                if side.active.name == pkmn_name
                else side.find_pokemon_in_reserves(pkmn_name)
            )
            if pkmn is not None and pkmn.item == constants.UNKNOWN_ITEM:
                if weather_name == constants.SUN:
                    item = "heatrock"
                elif weather_name == constants.RAIN:
                    item = "damprock"
                elif weather_name == constants.SAND:
                    item = "smoothrock"
                elif weather_name in constants.HAIL_OR_SNOW:
                    item = "icyrock"
                else:
                    item = constants.UNKNOWN_ITEM

                logger.info(
                    "Weather not ending means that opponent's {} has a {}".format(
                        pkmn.name, item
                    )
                )
                pkmn.item = item

    if side is not None and len(split_msg) >= 5 and side.name in split_msg[4]:
        ability = normalize_name(split_msg[3].split(":")[-1].strip())
        logger.info("Setting {} ability to {}".format(side.active.name, ability))
        side.active.ability = ability


def fieldstart(battle, split_msg):
    """Set the battle's field condition"""
    field_name = normalize_name(split_msg[2].split(":")[-1].strip())

    # some field effects show up as a `-fieldstart` item but are separate from the other fields
    if field_name == constants.TRICK_ROOM:
        logger.info("Setting trickroom")
        battle.trick_room = True
        battle.trick_room_turns_remaining = 5
    elif field_name == constants.GRAVITY:
        logger.info("Setting gravity")
        battle.gravity = True
    else:
        logger.info("Setting the field to {}".format(field_name))
        battle.field = field_name
        battle.field_turns_remaining = 5


def fieldend(battle, split_msg):
    """Remove the battle's field condition"""
    field_name = normalize_name(split_msg[2].split(":")[-1].strip())

    # some field effects show up as a `-fieldend` item but are separate from the other fields
    if field_name == constants.TRICK_ROOM:
        logger.info("Removing trick room")
        battle.trick_room = False
        battle.trick_room_turns_remaining = 0
    elif field_name == constants.GRAVITY:
        logger.info("Removing gravity")
        battle.gravity = False
    else:
        logger.info("Setting the field to None")
        battle.field = None
        battle.field_turns_remaining = 0


def sidestart(battle, split_msg):
    # Inconsistencies in the protocol mean parse after the `:` to get the side condition
    # |-sidestart|p2: Name|Reflect
    # |-sidestart|p2: Name|move: Light Screen
    # |-sidestart|p2: Name|Spikes
    # |-sidestart|p1: Name|move: Stealth Rock
    #
    # Some side conditions have an explicit duration such as lightscreen, reflect, etc.
    # Others are incremented by 1

    condition = split_msg[3].split(":")[-1].strip()
    condition = normalize_name(condition)
    if is_opponent(battle, split_msg):
        side = battle.opponent
    else:
        side = battle.user

    if condition in SIDE_CONDITION_DEFAULT_DURATION:
        increment_amount = SIDE_CONDITION_DEFAULT_DURATION[condition]
        if (
            condition in ["reflect", "lightscreen", "auroraveil"]
            and side.active.item == "lightclay"
        ):
            increment_amount += 3

        side.side_conditions[condition] = increment_amount
        logger.info(
            "Setting side condition {} to {} for {}".format(
                condition, SIDE_CONDITION_DEFAULT_DURATION[condition], side.active.name
            )
        )
    else:
        side.side_conditions[condition] += 1
        logger.info(
            "Incremented side condition {} to {} for {}".format(
                condition, side.side_conditions[condition], side.active.name
            )
        )


def sideend(battle, split_msg):
    """Remove a side effect such as stealth rock or sticky web"""
    condition = split_msg[3].split(":")[-1].strip()
    condition = normalize_name(condition)

    if is_opponent(battle, split_msg):
        logger.info("Side condition {} ending for opponent".format(condition))
        battle.opponent.side_conditions[condition] = 0
    else:
        logger.info("Side condition {} ending for user".format(condition))
        battle.user.side_conditions[condition] = 0


def swapsideconditions(battle, _):
    user_sc = battle.user.side_conditions
    opponent_sc = battle.opponent.side_conditions
    for side_condition in constants.COURT_CHANGE_SWAPS:
        user_sc[side_condition], opponent_sc[side_condition] = (
            opponent_sc[side_condition],
            user_sc[side_condition],
        )


# `|-item|` effects that MOVE an item off another pokemon.  PS names the loser in the
# `[of]` tag for the ability steals (data/abilities.ts magician / pickpocket
# `this.add('-item', source, yourItem, '[from] ability: Magician', '[of] ' + target)`)
# and for the move steals (data/moves.ts thief / covet onAfterHit) and for Bestow
# (data/moves.ts:1257 `this.add('-item', target, myItem, '[from] move: Bestow',
# `[of] ${source}`)`, where the `[of]` is the GIVER).  `ability: Frisk` is
# deliberately absent: it is a pure KNOWLEDGE reveal of the foe's item and transfers
# nothing (synth33779 line 344 is a Frisk formatted exactly like the real Trick at
# 356-357 -- the handler must tell them apart).
_ITEM_STEAL_FROM_TAGS = (
    "ability: magician",
    "ability: pickpocket",
    "move: thief",
    "move: covet",
    "move: bestow",
)
# A Trick / Switcheroo SWAPS, and PS emits exactly ONE line per side, ALWAYS one for
# each (data/moves.ts:19888-19899): `-item <mon> <received>|[from] move: Trick` for a
# side that receives something, and `-enditem <mon> <lost>|[silent]|[from] move: Trick`
# for a side that ends up empty-handed.  Both halves therefore already land on a
# handler of their own -- `set_item` and `remove_item` -- and NO donor has to be
# inferred from the opposite line.  Inferring one is not just redundant, it is wrong
# whenever the two mons hold the SAME item (Leftovers <-> Leftovers, Choice Specs <->
# Choice Specs; common in randbats, where the item pool is small): the second line's
# donor guess then matches the mon the first line had just handed the item to and
# re-clears it.  See `_apply_item_transfer_donor_loss`.
_ITEM_SWAP_FROM_TAGS = ("move: trick", "move: switcheroo")


def _from_tag(split_msg) -> str:
    for msg in split_msg[4:]:
        if "[from]" in msg:
            return msg.split("[from]", 1)[1].strip().lower()
    return ""


def _apply_item_transfer_donor_loss(battle, split_msg, side, other_side, item) -> None:
    """The other half of an item TRANSFER: the pokemon that lost the item.

    `|-item|` names only the RECEIVER, so without this the donor keeps holding the item
    it no longer has -- synth10305's Basculin kept the Choice Band that Hoopa's Magician
    stole on T4, inflating its Wave Crash / Double-Edge by 1.5x for the rest of the
    game."""
    from_tag = _from_tag(split_msg)
    if not from_tag:
        return

    if any(from_tag.startswith(t) for t in _ITEM_STEAL_FROM_TAGS):
        # the `[of]` slot is the victim; it may be on either side
        of_ident = None
        for msg in split_msg[4:]:
            if msg.strip().startswith("[of]"):
                of_ident = msg.split("[of]", 1)[1].strip()
        if not of_ident:
            return
        victim_side = _side_from_pokemon_identifier(battle, of_ident)
        if victim_side is None:
            return
        victim = getattr(victim_side, "active", None)
        if victim is None or victim.item is None:
            return
        logger.info(
            "{} lost its item ({}) to {}".format(victim.name, victim.item, from_tag)
        )
        if victim.removed_item is None:
            victim.removed_item = victim.item
        victim.item = None
        victim.item_inferred = False
        return

    # Trick / Switcheroo (`_ITEM_SWAP_FROM_TAGS`) deliberately falls through and does
    # NOTHING here.  PS gives every side of a swap its own protocol line -- `-item` if
    # it received something, `-enditem <lost>|[silent]|[from] move: Trick` if it did not
    # (data/moves.ts:19888-19899) -- so `set_item` / `remove_item` already move both
    # halves.  The old inference here ("the other side's active is the donor if it still
    # holds the item just announced") was destructive on the SAME-ITEM swap that randbats
    # produces constantly: on `|-item|p2a: Golduck|Choice Specs|[from] move: Trick` /
    # `|-item|p1a: Espeon|Choice Specs|[from] move: Trick` the second line's guess
    # matched Golduck -- which line 1 had just legitimately given the Specs -- and
    # stripped it, leaving Golduck item-less for the rest of the reconstruction
    # (synth02950 T13-T17: four Grass Knot roll sets derived without the Choice Specs
    # 1.5x, plus a phantom "observed item loss" finding at T17; same shape in
    # synth00260, synth27809 and the Leftovers<->Leftovers swaps of synth39679).


def set_item(battle, split_msg):
    """Set the opponent's item"""
    if is_opponent(battle, split_msg):
        side = battle.opponent
        other_side = battle.user
    else:
        side = battle.user
        other_side = battle.opponent

    item = normalize_name(split_msg[3].strip())

    if (
        len(split_msg) >= 5
        and side.active.removed_item is None
        and item != side.active.item
        and side.active.item not in [constants.UNKNOWN_ITEM]
    ):
        logger.info("{}'s removed item is {}".format(side.active.name, item))
        side.active.removed_item = side.active.item

    # when the bot gets tricked we set the opponent's removed item
    if (
        len(split_msg) >= 5
        and "[from] move: Trick" in split_msg[4]
        and not is_opponent(battle, split_msg)
        and other_side.active.removed_item is None
    ):
        logger.info("Setting opponent's removed_item to {}".format(item))
        other_side.active.removed_item = item

    # for gen5 frisk only
    # the frisk message will (incorrectly imo) show the item as belonging to the
    # pokemon with frisk
    #
    # e.g. Furret is frisking the opponent:
    # |-item|p2a: Furret|Life Orb|[from] ability: Frisk|[of] p2a: Furret
    if (
        len(split_msg) == 6
        and split_msg[4] == "[from] ability: Frisk"
        and split_msg[2] in split_msg[5]
    ):
        logger.info(
            "{} frisked the opponent's item as {}".format(side.active.name, item)
        )
        # Frisk is a pure REVEAL of what the target already holds, so it is
        # subject to the Illusion misattribution `_record_revealed_item` undoes.
        owner = _record_revealed_item(battle, other_side, other_side.active, item)
        if owner is not None:
            logger.info("Setting {}'s item to {}".format(owner.name, item))
            owner.item = item
    else:
        # A `[from]` tag on a `-item` marks a TRANSFER (Trick / Switcheroo /
        # Bestow / Thief / Covet / Magician / Pickpocket / Recycle): the item
        # GAINED is absent from every sidecar set, which records the
        # start-of-battle hold, so the Illusion proof cannot speak to it and
        # must not be consulted.  A bare `-item` (Air Balloon) and gen9 Frisk
        # (`-item|<target>|<item>|[from] ability: Frisk|[of] <frisker>`, which
        # names the TARGET and so misses the gen5 branch above) are pure
        # REVEALS of an existing hold and are subject to it.
        pkmn = side.active
        if len(split_msg) < 5 or "ability: Frisk" in split_msg[4]:
            pkmn = _record_revealed_item(battle, side, pkmn, item)
            if pkmn is None:
                return
        logger.info("Setting {}'s item to {}".format(pkmn.name, item))
        pkmn.item = item

        # A `-item` line is direct PROTOCOL evidence of what this pokemon holds, so it
        # supersedes any earlier item GUESS. Leaving `item_inferred` set let downstream
        # heuristics overwrite a protocol-established item: synth33779's Cyclizar was
        # Tricked a Choice Band on T22 and the "used two different moves - it cannot
        # have a choice item" rule (which is about a choice LOCK the fresh item never
        # imposed on already-used moves) reset it to UNKNOWN, after which the exact-teams
        # sidecar refilled its ORIGINAL Heavy-Duty Boots and every T23 Knock Off roll was
        # computed at the wrong Attack.
        pkmn.item_inferred = False

        # a `-item` on this pokemon means it now genuinely HOLDS the item:
        # gen5+ Knock Off removes the item outright and PS has no
        # "cannot regain an item" latch - a later Trick/Switcheroo/Pickpocket/
        # Covet/Thief gain is a real, active held item. Clear the knock-off
        # marker or the engine conversion nulls the new item forever
        # (poke_engine_helpers.py itemless-conversion check)
        if pkmn.knocked_off:
            logger.info(
                "{} gained an item after a knock off - clearing knocked_off".format(
                    pkmn.name
                )
            )
            pkmn.knocked_off = False

        _apply_item_transfer_donor_loss(battle, split_msg, side, other_side, item)


def remove_item(battle, split_msg):
    """Remove the opponent's item"""
    if is_opponent(battle, split_msg):
        side = battle.opponent
    else:
        side = battle.user

    item = normalize_name(split_msg[3].strip())

    # `-enditem` names the DISGUISE when an Illusion bearer consumes its own
    # berry / Focus Sash / Power Herb or has its item Knocked Off, and the
    # damage here is worse than a wrong `item`: it also latches `removed_item`,
    # which `apply_exact_team` (correctly) refuses to overwrite, so the real
    # party member of the impersonated species stays item-less for the rest of
    # the battle.
    pkmn = _record_revealed_item(battle, side, side.active, item)
    if pkmn is None:
        return

    logger.info("Removing {}'s item: {}".format(pkmn.name, item))
    pkmn.item = None

    if pkmn.removed_item is None:
        logger.info("Setting {}'s removed item to {}".format(pkmn.name, item))
        pkmn.removed_item = item

    if "unburden" not in pkmn.volatile_statuses and "unburden" in [
        normalize_name(a) for a in pokedex[pkmn.name][constants.ABILITIES].values()
    ]:
        logger.info("Adding unburden volatile to {}".format(pkmn.name))
        pkmn.volatile_statuses.append("unburden")

    if len(split_msg) >= 5 and "knockoff" in normalize_name(split_msg[4]):
        logger.info("Knockoff removed {}'s item".format(pkmn.name))
        pkmn.knocked_off = True


def immune(battle, split_msg):
    if is_opponent(battle, split_msg):
        side = battle.opponent
        pkmn = side.active
    else:
        side = battle.user
        pkmn = side.active

    # the IMMUNE pkmn here is the target; the attacker is the other side's active.
    # PS computed no damage, so any Stellar boost it optimistically spent on its
    # |move| line is still unspent.
    attacking_side = battle.user if side is battle.opponent else battle.opponent
    _rollback_stellar_boost(attacking_side.active)
    _disarm_hp_certificates(battle)

    for msg in split_msg:
        if constants.ABILITY in normalize_name(msg):
            ability = normalize_name(msg.split(":")[-1])
            logger.info("Setting {}'s ability to {}".format(side.active.name, ability))
            side.active.ability = ability

    # the checker knows the exact roster and resolves Illusion itself; skip the
    # whole inference (including its engine round-trip) there
    if not zoroark_inference_allowed(battle):
        return

    zoroark_from_reserves = side.find_pokemon_in_reserves(
        "zoroark"
    ) or side.find_pokemon_in_reserves("zoroarkhisui")

    expected_damage_rolls, _ = poke_engine_get_damage_rolls(
        deepcopy(battle), battle.user.last_used_move.move, "none", True
    )

    # Zoroark checks
    if (
        is_opponent(battle, split_msg)
        and not side.active.name.startswith("zoroark")
        and battle.user.last_used_move.move in all_move_json
        and all_move_json[battle.user.last_used_move.move][constants.CATEGORY]
        != constants.STATUS
        and type_effectiveness_modifier(
            all_move_json[battle.user.last_used_move.move][constants.TYPE],
            side.active.types,
        )
        != 0
        and "from" not in split_msg[-1]
        and not all(x == 0 for x in expected_damage_rolls)
        and battle.user.future_sight[0] != 1
        and not (
            side.active.terastallized
            and type_effectiveness_modifier(
                all_move_json[battle.user.last_used_move.move][constants.TYPE],
                [side.active.tera_type],
            )
            == 0
        )
    ):
        # Battle Factory: Zoroark must be in the reserves
        # and must be immune to the last used move by the bot
        if (
            battle.battle_type == BattleType.BATTLE_FACTORY
            and zoroark_from_reserves is not None
            and type_effectiveness_modifier(
                all_move_json[battle.user.last_used_move.move][constants.TYPE],
                zoroark_from_reserves.types,
            )
            == 0
        ):
            logger.info(
                "{} was immune to {} when it shouldn't be - it is {}".format(
                    pkmn.name,
                    battle.user.last_used_move.move,
                    zoroark_from_reserves.name,
                )
            )
            _switch_active_with_zoroark_from_reserves(side, zoroark_from_reserves)

        # Random Battle: Zoroark may be in the reserves so we need to check the move type
        # that it was immune to
        elif battle.battle_type == BattleType.RANDOM_BATTLE:
            actual_zoroark = None
            zoroark_hisui = Pokemon("zoroarkhisui", 100)
            zoroark_regular = Pokemon("zoroark", 100)

            # zoroark was in the reserves - just use that one
            if (
                zoroark_from_reserves is not None
                and type_effectiveness_modifier(
                    all_move_json[battle.user.last_used_move.move][constants.TYPE],
                    zoroark_from_reserves.types,
                )
                == 0
            ):
                actual_zoroark = zoroark_from_reserves

            # hisui zoroark
            elif (
                zoroark_from_reserves is None
                and type_effectiveness_modifier(
                    all_move_json[battle.user.last_used_move.move][constants.TYPE],
                    zoroark_hisui.types,
                )
                == 0
                and zoroark_hisui.name in RandomBattleTeamDatasets.pkmn_sets
            ):
                actual_zoroark = zoroark_hisui
                actual_zoroark.level = RandomBattleTeamDatasets.predict_set(
                    actual_zoroark
                ).pkmn_set.level
                side.reserve.append(actual_zoroark)

            # regular zoroark
            elif (
                zoroark_from_reserves is None
                and type_effectiveness_modifier(
                    all_move_json[battle.user.last_used_move.move][constants.TYPE],
                    zoroark_regular.types,
                )
                == 0
                and zoroark_regular.name in RandomBattleTeamDatasets.pkmn_sets
            ):
                actual_zoroark = zoroark_regular
                actual_zoroark.level = RandomBattleTeamDatasets.predict_set(
                    actual_zoroark
                ).pkmn_set.level
                side.reserve.append(actual_zoroark)

            # if we found a zoroark from one of those branches
            if actual_zoroark is not None:
                logger.info(
                    "{} was immune to {} when it shouldn't be - it is {}".format(
                        pkmn.name,
                        battle.user.last_used_move.move,
                        actual_zoroark.name,
                    )
                )
                _switch_active_with_zoroark_from_reserves(side, actual_zoroark)


def _switch_active_with_zoroark_from_reserves(
    opponent_side: Battler, zoroark_from_reserves: Pokemon
):
    """
    This is called when we are 100% sure that the opponent's active pkmn is a zoroark
    This swaps the active pkmn with the zoroark from the reserves

    Assumptions:
        - The `zoroark_from_reserves` MUST be in `opponent_side.reserve`
    """
    pkmn = opponent_side.active

    # any moves used by this pkmn since switching in need to be removed because we cannot guarantee that they
    # belong to this pkmn
    for mv in pkmn.moves_used_since_switch_in:
        logger.info(
            "Removing {} from {}'s moves because it is {}".format(
                mv, pkmn.name, zoroark_from_reserves.name
            )
        )
        pkmn.remove_move(mv)
        if zoroark_from_reserves.get_move(mv) is None:
            zoroark_from_reserves.add_move(mv)

    # set attributes on zoroark that were on the pokemon that we thought was zoroark
    # and clear those attributes from the pokemon that we thought was zoroark
    pkmn_hp_percent = float(pkmn.hp) / pkmn.max_hp
    zoroark_from_reserves.hp = zoroark_from_reserves.max_hp * pkmn_hp_percent
    # a fraction transfer between two different max HPs is an estimate
    hp_certificate.clear(zoroark_from_reserves, "illusion HP transfer")
    zoroark_from_reserves.boosts = copy(pkmn.boosts)
    zoroark_from_reserves.status = pkmn.status
    zoroark_from_reserves.volatile_statuses = copy(pkmn.volatile_statuses)
    zoroark_from_reserves.terastallized = pkmn.terastallized
    zoroark_from_reserves.tera_type = pkmn.tera_type

    # the zoroark was the physical pokemon on the field all along, so the
    # actions counted while disguised belong to it - PS never resets
    # activeMoveActions on an illusion break (only switchIn does,
    # sim/battle-actions.ts:138)
    zoroark_from_reserves.active_move_actions = pkmn.active_move_actions
    zoroark_from_reserves.moved_this_turn = pkmn.moved_this_turn
    zoroark_from_reserves.active_turns = pkmn.active_turns
    pkmn.active_move_actions = 0
    pkmn.moved_this_turn = False
    pkmn.active_turns = 0
    pkmn.boosts.clear()
    pkmn.status = None
    pkmn.volatile_statuses.clear()
    pkmn.volatile_status_durations.clear()

    # ...and the HP the disguise ACCUMULATED belongs to the zoroark too (the
    # fraction transfer above already gave it away), so the impersonated mon
    # rolls back to what IT last held.  `switch_or_drag` captures
    # `hp_at_switch_in` BEFORE the |switch| line's health -- which PS takes from
    # the REAL entrant, not the illusion (sim/pokemon.ts:544-552) -- overwrites
    # it, so it is the impersonated mon's own last true HP.  `illusion_end` does
    # exactly this on a |replace|; without it here the disguise keeps the
    # zoroark's HP and enters its own next stay already half dead.
    if pkmn.hp != pkmn.hp_at_switch_in:
        pkmn.hp = pkmn.hp_at_switch_in
        hp_certificate.clear(pkmn, "illusion rollback")

    if pkmn.terastallized:
        pkmn.terastallized = False
        pkmn.tera_type = None

    zoroark_from_reserves.zoroark_disguised_as = pkmn.name

    # swap the pkmn places
    opponent_side.reserve.append(pkmn)
    opponent_side.active = zoroark_from_reserves
    opponent_side.reserve.remove(zoroark_from_reserves)

    # the zoroark was on the field all along (disguised) and some inference
    # paths construct a brand-new zoroark object here: it has been seen
    zoroark_from_reserves.revealed = True

    # discovering the zoroark means its illusion no longer fools us
    zoroark_from_reserves.illusion_broken = True


def update_ability(battle, split_msg):
    if is_opponent(battle, split_msg):
        side = battle.opponent
        other_side = battle.user
    else:
        side = battle.user
        other_side = battle.opponent

    ability = normalize_name(split_msg[3])

    # Intrepid Sword / Dauntless Shield announce their once-per-battle boost as
    # `|-ability|<pkmn>|Intrepid Sword|boost` on switch-in. Seeing the line is the
    # proof that the trigger has been spent, so it can never fire again this
    # battle - the engine re-armed it at every search root while the binding
    # hard-coded the flag.
    _mark_once_per_battle_ability_used(side.active, ability)

    if len(split_msg) >= 6 and (
        "ability:" in split_msg[4] or "ability:" in split_msg[5]
    ):
        original_ability = normalize_name(split_msg[4].split(":")[-1])
        logger.info(
            "Setting {}'s original ability to {}".format(
                side.active.name, original_ability
            )
        )
        side.active.original_ability = original_ability

        if split_msg[5].startswith("[of]") and other_side.name in split_msg[5]:
            logger.info(
                "Setting {}'s ability to {}".format(other_side.active.name, ability)
            )
            other_side.active.ability = ability
    elif ability == "asone":
        # PS announces BOTH formes' ability as the bare `As One`
        # (data/abilities.ts asoneglastrier / asonespectrier onStart:
        # `this.add('-ability', pokemon, 'As One')`), so the forme has to come
        # from the SPECIES.  An Imposter Ditto that copied the ability keeps the
        # name `ditto`; the identity it copied is on `transformed_into`, which
        # the `-transform` handler stamps BEFORE this line arrives (PS emits
        # `|-transform|...|[from] ability: Imposter` first, then the copied
        # ability's onStart).  Without it synth147595's Ditto -- a copy of
        # Calyrex-SHADOW -- was given asoneglastrier, i.e. Chilling Neigh in
        # place of Grim Neigh, on top of the warning.
        species = getattr(side.active, "transformed_into", None) or side.active.name
        if species == "calyrexice":
            ability = "asoneglastrier"
        elif species == "calyrexshadow":
            ability = "asonespectrier"
        else:
            logger.warning(
                "Unknown asone ability for {} - defaulting to asoneglastrier".format(
                    side.active.name
                )
            )
            ability = "asoneglastrier"
    elif side.active.ability in ["asoneglastrier", "asonespectrier"]:
        logger.info(
            "{} has the ability {}, will not change to {}".format(
                side.active.name, side.active.ability, ability
            )
        )
        ability = side.active.ability

    logger.info("Setting {}'s ability to {}".format(side.active.name, ability))
    side.active.ability = ability


def illusion_end(battle, split_msg):
    if is_opponent(battle, split_msg):
        side = battle.opponent
    else:
        side = battle.user

    # Live play normally learns the USER's own Zoroark at its switch-in
    # (`user_just_switched_into_zoroark` rewrites the |switch| line from the
    # request), so this swap only ever fired for the opponent.  A REPLAY has no
    # `last_selected_move` to recognise scenario 1 with, so the user's disguised
    # Zoroark is never identified and its whole stay is recorded against the
    # impersonated species: PS prints the |replace| and everything after it
    # against `p1a: Zoroark` while the reconstruction still has the disguise
    # active, so the Zoroark's Knock Off / status / boosts land on the wrong
    # party member (synth31992: the Zoroark's Life Orb is knocked off, marking
    # the real Salamence `knocked_off`, and its Heavy-Duty Boots are gone for
    # the rest of the battle).  The swap is identical on both sides -- the only
    # asymmetry, the opponent's percentage HP, is handled by the hp FRACTION
    # below, which is exact either way.
    if (
        side.active.name not in ["zoroark", "zoroarkhisui"]
        and side.active.zoroark_disguised_as is None
    ):
        logger.info("Illusion ending for {}".format(side.name))
        hp_percent = float(side.active.hp) / side.active.max_hp
        previous_boosts = side.active.boosts
        previous_status = side.active.status
        previous_item = side.active.item
        # The disguise OBJECT is also the real party member of the impersonated
        # species, so its item is the zoroark's only if it CHANGED while the disguise
        # stood on the field.  synth89936: the real Dialga's Leftovers were Knocked Off
        # on T7, and a zoroark wearing Dialga's face on T30 inherited that `None` --
        # which, unlike UNKNOWN_ITEM, `apply_exact_team`
        # (fp/replay/damage_membership.py:2261) correctly refuses to overwrite -- so the
        # zoroark reached the engine holding nothing and the T31 `-enditem|p2a: Zoroark|
        # Choice Specs|[from] move: Knock Off` had no branch that could remove it.
        # Unchanged item => leave both mons alone: the disguise keeps its own knowledge
        # and the zoroark stays UNKNOWN for the sidecar / inference to fill.
        item_moved_under_disguise = previous_item != getattr(
            side.active, "item_at_switch_in", constants.UNKNOWN_ITEM
        )
        # Everything the disguise ACCUMULATED while it stood on the field
        # happened to the physical zoroark, so it moves across with the swap --
        # not just hp/boosts/status/item.  The sleep counters go with the sleep
        # status they belong to (without them the revealed zoroark reads as
        # having just fallen asleep and the engine produces no wake branch at
        # all, so synth25500 T53's observed wake-and-attack -- and its Bitter
        # Malice `-unboost` -- was unreachable), and the volatiles go with the
        # mon that is actually carrying them (the same turn's `-end move: Heal
        # Block` had nothing to end).
        previous_volatiles = list(side.active.volatile_statuses)
        previous_volatile_durations = dict(side.active.volatile_status_durations)
        previous_sleep_turns = side.active.sleep_turns
        previous_rest_turns = side.active.rest_turns
        # The tera goes with the physical mon too.  A side terastallizes at most
        # once (PS sim/battle-actions.ts:1946 `for (const ally of
        # pokemon.side.pokemon) ally.canTerastallize = null`), so a
        # `|-terastallize|` printed against the disguise belongs to the ZOROARK
        # and must not be left stamped on the impersonated party member -- which
        # is a real mon that comes back later.  `_switch_active_with_zoroark_
        # from_reserves` already does exactly this (fp/battle_modifier.py:3923
        # and :3953); the |replace| path did not.  synth167736: Zoroark-Hisui
        # teras Normal on T2 wearing Serperior's face, and the genuine Serperior
        # entered on T17 still typed NORMAL, so Stomping Tantrum was computed
        # unresisted (rolls 62-74 instead of the Grass-resisted 31-37) and its
        # 62-hp Substitute absorption came out [survival_contradiction].
        previous_terastallized = side.active.terastallized
        previous_tera_type = side.active.tera_type

        zoroark_from_switch_string = Pokemon.from_switch_string(split_msg[3])
        zoroark_reserve_index = None
        for index, pkmn in enumerate(side.reserve):
            if pkmn == zoroark_from_switch_string:
                zoroark_reserve_index = index
                break

        pkmn_disguised_as = side.active
        if item_moved_under_disguise:
            pkmn_disguised_as.item = constants.UNKNOWN_ITEM
        side.reserve.append(pkmn_disguised_as)
        if zoroark_reserve_index is not None:
            reserve_zoroark = side.reserve.pop(zoroark_reserve_index)
            side.active = reserve_zoroark
        else:
            side.active = zoroark_from_switch_string

        # the zoroark was on the field all along (disguised): it has been seen
        # (illusion_broken is stamped unconditionally at the end of this handler)
        side.active.revealed = True

        # the moves that have been used since this pkmn switched-in need
        # to be un-associated with the pkmn being disguised as and need to
        # be associated with the new pkmn instead
        for mv in pkmn_disguised_as.moves_used_since_switch_in:
            pkmn_disguised_as.remove_move(mv)
            if side.active.get_move(mv) is None:
                side.active.add_move(mv)

        # `moves_used_since_switch_in` is cleared on every switch-out
        # (switch_or_drag), so it only remembers the disguise's LATEST stay: if
        # the same disguised mon switched out and back in earlier without the
        # illusion ever breaking, moves it used during an EARLIER stay already
        # slipped past the loop above and are still sitting on
        # `pkmn_disguised_as`. It has just been demoted to the reserve (no
        # longer anyone's currently-selected action), so where the checker's
        # exact-teams sidecar is loaded, drop anything left that is not really
        # in this species' fixed randbats set - e.g. carbink otherwise
        # permanently keeping a disguised Zoroark's Will-O-Wisp and Hyper
        # Voice from an earlier stay, pushing it past 4 tracked moves.
        exact_teams = getattr(battle, "exact_teams", None)
        exact_mon = (
            (exact_teams.value.get(side.name) or {}).get(pkmn_disguised_as.name)
            if exact_teams is not None
            else None
        )
        if exact_mon is not None:
            true_moves = {normalize_name(m) for m in exact_mon.get("moves", ())}
            for mv in [m.name for m in pkmn_disguised_as.moves]:
                if mv not in true_moves:
                    pkmn_disguised_as.remove_move(mv)

        # the pokemon that we thought was active needs some attributes reset to
        # whatever the values were at switch-in as any changes that happened to zoroark
        # since switching in have not happened to the actual pokemon
        if pkmn_disguised_as.hp_at_switch_in != pkmn_disguised_as.hp:
            logger.info(
                "Resetting {}'s HP {} to its value at switch-in: {}/{} ({}%)".format(
                    pkmn_disguised_as.name,
                    int(pkmn_disguised_as.hp),
                    pkmn_disguised_as.hp_at_switch_in,
                    pkmn_disguised_as.max_hp,
                    round(
                        100
                        * pkmn_disguised_as.hp_at_switch_in
                        / pkmn_disguised_as.max_hp,
                        1,
                    ),
                )
            )
            pkmn_disguised_as.hp = pkmn_disguised_as.hp_at_switch_in
            hp_certificate.clear(pkmn_disguised_as, "illusion rollback")
        if pkmn_disguised_as.status_at_switch_in != pkmn_disguised_as.status:
            logger.info(
                "Resetting {}'s status {} to its value at switch-in: {}".format(
                    pkmn_disguised_as.name,
                    pkmn_disguised_as.status,
                    pkmn_disguised_as.status_at_switch_in,
                )
            )
            pkmn_disguised_as.status = pkmn_disguised_as.status_at_switch_in

        # any volatile statuses and duration counters accrued since switch-in
        # happened to the disguised zoroark, not to this pokemon: clear them so
        # a later genuine switch-in of this mon does not forward phantom
        # volatiles and stale age counters (confusion/healblock/throatchop/
        # syrupbomb/trap) to the engine. This mirrors the clearing done in
        # _switch_active_with_zoroark_from_reserves, which also TRANSFERS them
        # to the revealed zoroark first -- done here too, below.
        pkmn_disguised_as.volatile_statuses.clear()
        pkmn_disguised_as.volatile_status_durations.clear()
        # NOT the sleep counters: unlike the volatiles, the reconstruction keeps
        # ONE object for the disguise species, and that object is also the REAL
        # party member of that species -- whose own sleep counter is carried on
        # it across its own stays.  Zeroing it here erases the real mon's count
        # (synth29948: the genuine Regigigas returns asleep on T30, and with a
        # zeroed counter the engine gives it no wake branch, so its Body Slam
        # `par` becomes unreachable).  The zoroark gets a COPY below; nothing is
        # taken away from the mon going back to reserve.

        side.active.hp = hp_percent * side.active.max_hp
        hp_certificate.clear(side.active, "illusion HP transfer")
        side.active.boosts = previous_boosts
        side.active.status = previous_status
        if item_moved_under_disguise:
            side.active.item = previous_item
        side.active.volatile_statuses = previous_volatiles
        side.active.volatile_status_durations.update(previous_volatile_durations)
        side.active.sleep_turns = previous_sleep_turns
        side.active.rest_turns = previous_rest_turns
        side.active.terastallized = previous_terastallized
        side.active.tera_type = previous_tera_type
        if pkmn_disguised_as.terastallized:
            pkmn_disguised_as.terastallized = False
            pkmn_disguised_as.tera_type = None

        # a |replace| is not a switch-in: the zoroark was the acting pokemon
        # all along, so the move actions counted under the disguise carry over
        # (PS resets activeMoveActions only in switchIn,
        # sim/battle-actions.ts:138)
        side.active.active_move_actions = pkmn_disguised_as.active_move_actions
        side.active.moved_this_turn = pkmn_disguised_as.moved_this_turn
        side.active.active_turns = pkmn_disguised_as.active_turns
        pkmn_disguised_as.active_move_actions = 0
        pkmn_disguised_as.moved_this_turn = False
        pkmn_disguised_as.active_turns = 0

    # `|replace|` always means the active zoroark's illusion just ended.
    # By this point side.active is the actual zoroark on either side
    # (the user's own zoroark is always tracked truthfully, and the
    # opponent branch above swaps the real zoroark in)
    side.active.illusion_broken = True
    side.active.zoroark_disguised_as = None


def form_change(battle, split_msg):
    if is_opponent(battle, split_msg):
        side = battle.opponent
        is_user = False
    else:
        side = battle.user
        is_user = True

    msg_type = split_msg[1].strip()
    new_forme = normalize_name(split_msg[3].split(",")[0])

    logger.info("Form Change: {} -> {}".format(side.active.name, split_msg[3]))
    side.active.forme_change(split_msg[3])

    # |-formechange| is PS's NON-permanent forme path (formeChange with
    # isPermanent falsy, sim/pokemon.ts:1479-1485): baseSpecies is untouched,
    # so the forme reverts on switch-out via clearVolatile's
    # setSpecies(this.baseSpecies) (sim/pokemon.ts:1514-1565) - e.g.
    # Meloetta-Pirouette, Cramorant-Gulping, Morpeko-Hangry. |detailschange|
    # is the permanent path (baseSpecies reassigned: Mega evolution,
    # Terapagos-Stellar, Mimikyu-Busted) and never reverts on switch-out.
    #
    # ...EXCEPT that the permanent path can also emit a |-formechange|. In
    # sim/pokemon.ts the `isPermanent` branch assigns `this.baseSpecies =
    # rawSpecies` and adds `detailschange` (:1449-1451), and then, when the
    # source is a Status, adds a SECOND line for the same species:
    #     } else if (source.effectType === 'Status') {
    #         // Shaymin-Sky -> Shaymin
    #         this.battle.add('-formechange', this, species.name, message);
    # (sim/pokemon.ts:1474-1476). The only gen9 instance is Shaymin-Sky losing
    # its forme to freeze -- data/conditions.ts:92-93 calls
    # `target.formeChange('Shaymin', this.effect, /*isPermanent*/ true)`.
    # Reading that trailing echo as the non-permanent path made the mon revert
    # to `shayminsky` on switch-out, so its next |switch|p2a: Shaymin| matched
    # nothing in the reserves and a SECOND Shaymin object was built:
    # synth15853's opponent party reached seven rows and the binding refused
    # 24 times.
    #
    # The echo is recognised structurally, not by species: a |detailschange|
    # arms `permanent_forme_echo_pending` with the species it named, and only
    # a |-formechange| for that SAME species consumes it. The marker is
    # single-use and is cleared on switch-out, so a later, genuinely
    # non-permanent |-formechange| cannot borrow it.
    echo_of_permanent_change = (
        msg_type == "-formechange"
        and getattr(side.active, "permanent_forme_echo_pending", None) == new_forme
    )
    side.active.permanent_forme_echo_pending = (
        new_forme if msg_type == "detailschange" else None
    )
    if echo_of_permanent_change and not os.environ.get(
        "FP_CONTROL_PERMANENT_FORME_ECHO_REVERTS"
    ):
        # CONTROL ONLY, never set in a measurement run: setting the flag
        # restores the pre-fix reading (the echo treated as non-permanent) so
        # the fix can be shown capable of failing.
        side.active.reverts_forme_on_switch_out = False
    else:
        side.active.reverts_forme_on_switch_out = msg_type == "-formechange"

    if is_user:
        side.re_initialize_active_pokemon_from_request_json(battle.request_json)


def zpower(battle, split_msg):
    if is_opponent(battle, split_msg):
        side = battle.opponent
    else:
        side = battle.user

    logger.info("{} Used a Z-Move, setting item to None".format(side.active.name))
    side.active.item = None


def clearnegativeboost(battle, split_msg):
    if is_opponent(battle, split_msg):
        pkmn = battle.opponent.active
    else:
        pkmn = battle.user.active

    for stat, value in pkmn.boosts.items():
        if value < 0:
            logger.info("Setting {}'s {} boost to 0".format(pkmn.name, stat))
            pkmn.boosts[stat] = 0


def clearboost(battle, split_msg):
    if is_opponent(battle, split_msg):
        pkmn = battle.opponent.active
    else:
        pkmn = battle.user.active

    for stat, value in pkmn.boosts.items():
        logger.info("Setting {}'s {} boost to 0".format(pkmn.name, stat))
        pkmn.boosts[stat] = 0


def clearallboost(battle, _):
    pkmn = battle.user.active
    for stat, value in pkmn.boosts.items():
        if value != 0:
            logger.info("Setting {}'s {} boost to 0".format(pkmn.name, stat))
            pkmn.boosts[stat] = 0

    pkmn = battle.opponent.active
    for stat, value in pkmn.boosts.items():
        if value != 0:
            logger.info("Setting {}'s {} boost to 0".format(pkmn.name, stat))
            pkmn.boosts[stat] = 0


def singleturn(battle, split_msg):
    if is_opponent(battle, split_msg):
        side = battle.opponent
    else:
        side = battle.user

    move_name = normalize_name(split_msg[3].split(":")[-1])
    if move_name in constants.PROTECT_VOLATILE_STATUSES:
        # increment by 2 because the `upkeep` function will decrement by 1 on every end-of-turn
        side.side_conditions[constants.PROTECT] += 2
        logger.info(
            "{} used a protect move, set protect side condition to {}".format(
                side.active.name, side.side_conditions[constants.PROTECT]
            )
        )

    # |-singleturn|p1a: Skarmory|move: Roost
    elif move_name == constants.ROOST:
        # set to 2 because the `upkeep` function will decrement by 1 on every end-of-turn
        side.active.volatile_statuses.append(constants.ROOST)
        logger.info(
            "{} has acquired the 'roost' volatilestatus".format(side.active.name)
        )


def mustrecharge(battle, split_msg):
    # Bot's side does not get mustrecharge because the request JSON
    # will contain the only available `recharge` move
    if is_opponent(battle, split_msg):
        side = battle.opponent
        logger.info("{} must recharge".format(side.active.name))
        side.active.volatile_statuses.append("mustrecharge")
    else:
        side = battle.user

    # Truant and mustrecharge together means that you only recharge next turn
    if "truant" in side.active.volatile_statuses:
        logger.info(
            "{} must recharge with truant, removing truant".format(side.active.name)
        )
        remove_volatile(side.active, "truant")


def _bot_active_is_grounded(battle):
    """Mirror of sim/pokemon.ts:2153-2164 `isGrounded()` for the bot's own
    active (whose types/item/ability/volatiles we know exactly). PS returns
    `null` for Levitate, which is falsy - treated as ungrounded here."""
    pkmn = battle.user.active
    volatiles = set(pkmn.volatile_statuses)
    if battle.gravity or "ingrain" in volatiles or "smackdown" in volatiles:
        return True
    if pkmn.item == "ironball":
        return True
    if "flying" in pkmn.types:
        return False
    if pkmn.ability == "levitate":
        return False
    if "magnetrise" in volatiles or "telekinesis" in volatiles:
        return False
    return pkmn.item != "airballoon"


def _trapping_abilities_that_could_trap_bot(battle, opponent_active):
    """The opponent species' candidate abilities that are trapping abilities
    AND could actually have trapped our active. Each trapper's own gate:
        arenatrap   data/abilities.ts:197-202  `pokemon.isGrounded()`
        magnetpull  data/abilities.ts:2507-2511 `pokemon.hasType('Steel')`
        shadowtag   data/abilities.ts:4147-4151 `!pokemon.hasAbility('shadowtag')`
    Only drop a candidate when it is PROVABLY impossible - over-dropping could
    pin the wrong ability, under-dropping only weakens the inference."""
    species_abilities = {
        normalize_name(a)
        for a in pokedex[opponent_active.name][constants.ABILITIES].values()
    }
    candidates = species_abilities & TRAPPING_ABILITIES

    if "magnetpull" in candidates and "steel" not in battle.user.active.types:
        candidates.discard("magnetpull")
    if "shadowtag" in candidates and battle.user.active.ability == "shadowtag":
        candidates.discard("shadowtag")
    if "arenatrap" in candidates and not _bot_active_is_grounded(battle):
        candidates.discard("arenatrap")

    return species_abilities, candidates


def _infer_opponent_trapping_ability(battle):
    """A rejected switch is proof that PS set `pokemon.trapped` on our active.
    Attribute it to the opponent's ability only when nothing on our own side
    already explains the trap."""
    bot_active = battle.user.active
    opponent_active = battle.opponent.active
    if bot_active is None or opponent_active is None:
        return

    self_trap = set(bot_active.volatile_statuses) & SELF_TRAPPING_VOLATILES
    if self_trap:
        logger.info(
            "Switch rejected as trapped, but {} has {} - no ability inference".format(
                bot_active.name, sorted(self_trap)
            )
        )
        return

    # gen6+ Ghost-types are immune to trapping: all three trapping abilities go
    # through `pokemon.tryTrap(true)`, which runs `runStatusImmunity('trapped')`
    # (sim/pokemon.ts:1614), and the modern typechart gives Ghost
    # `damageTaken.trapped: 3` (data/typechart.ts:202-204). gen5 and below have
    # no such entry (data/mods/gen5/typechart.ts:24-44 re-lists ghost's
    # damageTaken without `trapped`), so ghosts ARE ability-trappable there.
    if (
        "ghost" in bot_active.types
        and battle.generation not in GENS_WITHOUT_GHOST_TRAP_IMMUNITY
    ):
        logger.info(
            "Switch rejected as trapped, but {} is a Ghost-type (immune to "
            "ability trapping) - no ability inference".format(bot_active.name)
        )
        return

    # Shed Shell clears `trapped` at onTrapPokemonPriority -10
    # (data/items.ts:5635-5638), which runs after every trapping ability's
    # default-priority handler, so an ability trap cannot survive it
    if bot_active.item == "shedshell":
        logger.info(
            "Switch rejected as trapped, but {} holds a Shed Shell - no "
            "ability inference".format(bot_active.name)
        )
        return

    # a suppressed ability cannot trap
    if (
        bot_active.ability == "neutralizinggas"
        or opponent_active.ability == "neutralizinggas"
        or "gastroacid" in opponent_active.volatile_statuses
    ):
        logger.info(
            "Switch rejected as trapped while abilities are suppressed - no "
            "ability inference"
        )
        return

    species_abilities, candidates = _trapping_abilities_that_could_trap_bot(
        battle, opponent_active
    )
    if not candidates:
        logger.warning(
            "Switch rejected as trapped but {} has no trapping ability that "
            "could have caused it (candidates: {}) - no inference".format(
                opponent_active.name, sorted(species_abilities)
            )
        )
        return

    if opponent_active.ability is not None:
        if opponent_active.ability not in candidates:
            logger.warning(
                "Switch rejected as trapped but {}'s ability is already known "
                "to be {} - not overwriting".format(
                    opponent_active.name, opponent_active.ability
                )
            )
        return

    for ability in species_abilities - candidates:
        if ability not in opponent_active.impossible_abilities:
            logger.info(
                "{} trapped us - adding {} to its impossible abilities".format(
                    opponent_active.name, ability
                )
            )
            opponent_active.impossible_abilities.add(ability)

    if len(candidates) == 1:
        ability = next(iter(candidates))
        logger.info(
            "{} trapped us and {} is its only possible trapping ability - "
            "setting its ability".format(opponent_active.name, ability)
        )
        opponent_active.ability = ability
    else:
        logger.info(
            "{} trapped us - its ability is one of {}".format(
                opponent_active.name, sorted(candidates)
            )
        )


def error(battle, split_msg):
    """
    |error|[Unavailable choice] Can't switch: The active Pokémon is trapped

    PS rejects a switch whose chooser is trapped in sim/side.ts:983-995:

        if (pokemon.trapped) {
            return this.emitChoiceError(`Can't switch: The active Pokémon is trapped`,
                { pokemon, update: req => { ...delete req.maybeTrapped; req.trapped = true... } });
        }

    and emitChoiceError (sim/side.ts:525-534) writes
    `|error|[Unavailable choice] <message>` when that request patch changed
    something and `|error|[Invalid choice] <message>` when it did not, then
    re-emits the corrected |request|. Accept either tag.

    That rejection is real information: PS only sets `pokemon.trapped` (as
    opposed to `maybeTrapped`) once the trap is confirmed, and in gen9 the only
    ways a foe's ACTIVE can do it are Arena Trap / Shadow Tag / Magnet Pull.
    """
    message = "|".join(split_msg[2:]).strip()
    if "can't switch:" not in message.lower() or not message.endswith("is trapped"):
        logger.info("Unhandled |error| from PS: {}".format(message))
        return

    logger.info("PS rejected our switch as trapped: {}".format(message))
    _infer_opponent_trapping_ability(battle)


def cant(battle, split_msg):
    if is_opponent(battle, split_msg):
        side = battle.opponent
        other_side = battle.user
        opponent = True
    else:
        side = battle.user
        other_side = battle.opponent
        opponent = False

    # Consume any pending Dancer-copy marker (see activate()/move()).  A copy
    # aborted by par/slp/frz/flinch/Truant prints `|cant|` instead of a `|move|`
    # line (corpus: `|-activate|p1a: Oricorio|ability: Dancer` / `|cant|p1a:
    # Oricorio|par`, after which Oricorio still takes its OWN untagged action),
    # and an ability-flavored `|cant|` names the BLOCKER rather than the dancer
    # -- clear BOTH actives so the one-shot marker can never leak onto a real
    # move.  Deliberately only clears: this line's own accounting is unchanged.
    for _dancer_side in (battle.user, battle.opponent):
        if _dancer_side.active is not None:
            _dancer_side.active.dancer_copy_pending = False

    # Ability-flavored |cant| lines name the ABILITY HOLDER in the pokemon
    # slot, not the pokemon that failed to act. PS emits them as
    #     this.add('cant', <holder>, 'ability: <Name>', move, `[of] ${blocked}`)
    # from data/abilities.ts:225 (Armor Tail), :805 (Damp), :864 (Dazzling),
    # :3716 (Queenly Majesty). The `[of]` argument is the pokemon whose move
    # was being run - it is the event target of
    # `runEvent('TryMove', pokemon, target, move)` (sim/battle-actions.ts:487).
    # Truant (data/abilities.ts:5183) is NOT in this family: it names the
    # pokemon that cannot move and carries no `[of]`.
    #
    # These blocks fire at the TryMove event, which useMoveInner runs AFTER it
    # has already emitted the |move| line (sim/battle-actions.ts:457 addMove,
    # :485-490 TryMove; the ability's `attrLastMove('[still]')` just annotates
    # that line). So the real protocol is
    #     |move|p2a: Mightyena|Sucker Punch||[still]
    #     |cant|p1a: Tsareena|ability: Queenly Majesty|Sucker Punch|[of] p2a: Mightyena
    # and by the time we get here move() has ALREADY charged the blocked mon
    # its runMove costs: activeMoveActions (:217), moveUsed/moveThisTurn
    # (:291), deductPP (:282) and last_used_move. Re-applying any of that -
    # to either side - double-counts. The only new information on this line is
    # that the named mon holds the blocking ability.
    if (
        len(split_msg) > 3
        and split_msg[3].startswith("ability: ")
        and normalize_name(split_msg[3].split("ability: ")[-1])
        in ABILITIES_THAT_BLOCK_ANOTHER_POKEMONS_MOVE
    ):
        blocking_ability = normalize_name(split_msg[3].split("ability: ")[-1])
        blocked_side = _side_of_of_tag(battle, split_msg)
        if blocked_side is None:
            blocked_side = other_side
        logger.info(
            "{} blocked {}'s move with {} - revealing the ability and leaving "
            "the blocked pokemon's already-counted action alone".format(
                side.active.name, blocked_side.active.name, blocking_ability
            )
        )
        side.active.ability = blocking_ability
        return

    side.last_used_move = LastUsedMove(
        pokemon_name=side.active.name,
        move=side.last_used_move.move,
        turn=battle.turn,
    )

    # a |cant| is a consumed-but-aborted move action: PS already incremented
    # activeMoveActions in runMove BEFORE the abort check ran
    # (sim/battle-actions.ts:217 increments on entry; sleep/para/flinch/
    # recharge/Truant aborts come later via BeforeMove/cant), so a full-para
    # first turn still burns the Fake Out window. It also means the mon will
    # not move again this turn (PS `!this.queue.willMove(target)`), which is
    # what the taunt/encore apply seed keys on.
    side.active.active_move_actions += 1
    side.active.moved_this_turn = True

    # Glaive Rush's drawback ends on ANY action attempt: its onBeforeMove
    # (priority 100) removes the volatile before the abort handlers run
    # (flinch 8, slp/frz 10, recharge 11, par 1 - data/moves.ts glaiverush,
    # data/conditions.ts), so a |cant| turn consumes it too, silently
    if "glaiverush" in side.active.volatile_statuses:
        logger.info(
            "{} is acting - removing its spent glaiverush volatile".format(
                side.active.name
            )
        )
        remove_volatile(side.active, "glaiverush")

    # An INTERRUPTED two-turn release drops the charge.  PS keeps the charging
    # state as `twoturnmove` plus a volatile named after the move (data/
    # conditions.ts:287-322 twoturnmove: `onStart` does `attacker.addVolatile(
    # effect.id)`, `onEnd` does `target.removeVolatile(this.effectState.move)`),
    # and `twoturnmove.onMoveAborted` (:320-322) removes itself -- which runs
    # `onEnd` and takes the move-named volatile with it.  MoveAborted fires
    # exactly when BeforeMove returned falsy (sim/battle-actions.ts:255-257),
    # i.e. on every |cant| a BeforeMove handler emitted: par/slp/frz/flinch/
    # recharge/Truant/Disable/Taunt/Imprison/Heal Block/Gravity.  The
    # release-turn strip in `move()` only fires on the release |move| line, and
    # an interrupted release emits none, so without this the charge volatile
    # lives on forever and the mon's NEXT use of that move is reconstructed as
    # a release instead of a fresh charge (synth29603 T18 / synth19275 T45 /
    # synth14496 T21: Meteor Beam charges, is stopped by full paralysis, and
    # the observed charge-turn `-boost spa 1` then appears in no engine branch).
    #
    # `nopp` is deliberately excluded: it is emitted from the deductPP failure
    # AFTER BeforeMove already succeeded (:283-286), so no MoveAborted runs.
    # Ability blocks (Queenly Majesty / Dazzling / Armor Tail / Damp) fire at
    # TryMove, later still, and have already returned above.
    if not (len(split_msg) == 4 and split_msg[3] == "nopp"):
        for _vol in list(side.active.volatile_statuses):
            if all_move_json.get(_vol, {}).get("flags", {}).get("charge"):
                logger.info(
                    "{}'s two-turn {} release was interrupted - removing the "
                    "charge volatile".format(side.active.name, _vol)
                )
                remove_volatile(side.active, _vol)

    # |cant|p1a: Slaking|ability: Truant
    if len(split_msg) == 4 and split_msg[3] == "ability: Truant":
        logger.info(
            "{} got 'cant' from truant, removing truant volatile".format(
                side.active.name
            )
        )
        remove_volatile(side.active, "truant")

    # |cant|p2a: Tauros|recharge
    if len(split_msg) == 4 and split_msg[3] == "recharge":
        logger.info(
            "{} got 'cant' from recharge, removing mustrecharge volatile".format(
                side.active.name
            )
        )
        if opponent and "mustrecharge" not in side.active.volatile_statuses:
            logger.warning(
                "{} did not have mustrecharge but recharged".format(side.active.name)
            )

        remove_volatile(side.active, "mustrecharge")

    # `|cant|POKEMON|REASON|MOVE`: only the 4th field is the blocked mon's OWN
    # move.  The old `len(split_msg) == 4` form read the REASON instead, and the
    # only 4-field `move: ` reason gen9 emits is Throat Chop
    # (data/moves.ts:19407,19413 `this.add('cant', pokemon, 'move: Throat Chop')`
    # - no move argument), so it credited the ATTACKER's Throat Chop to the
    # VICTIM: 9 different species picked up a phantom `throatchop` in corpus B,
    # and once the list hits 5 `poke_engine_helpers` truncates, which can evict a
    # real move.  The shape the comment was written for
    # (`|cant|p2a: Politoed|move: Taunt|Toxic`, data/moves.ts taunt/healblock/
    # disable onBeforeMove `this.add('cant', attacker, 'move: X', move)`) has 5
    # fields and never reached the branch, so its genuine reveal was being
    # dropped.  Routed through `_record_used_move` so a disguised Illusion bearer
    # blocked by Taunt does not stamp its move on the disguise.  PS aborts before
    # `deductPP` on a BeforeMove block, so no PP is charged.
    if len(split_msg) == 5 and split_msg[3].startswith("move: "):
        move_name = normalize_name(split_msg[4])
        move_object = side.active.get_move(move_name)
        if move_object is None:
            _record_used_move(battle, side, side.active, move_name)
            logger.info(
                "Adding {} to {}'s moves from 'cant'".format(
                    move_name, side.active.name
                )
            )

    if len(split_msg) == 4 and split_msg[3] == constants.SLEEP:
        logger.info("{} got 'cant' from sleep".format(side.active.name))
        if side.active.rest_turns > 1:
            side.active.rest_turns -= 1
            logger.info(
                "Decrementing {}'s rest_turns to {}".format(
                    side.active.name, side.active.rest_turns
                )
            )
        elif side.active.rest_turns == 1:
            logger.critical(
                "{} has rest_turns==1 and got 'cant' from sleep".format(
                    side.active.name
                )
            )
            exit(1)
        else:
            side.active.sleep_turns += 1
            logger.info(
                "Incrementing {}'s sleep_turns to {}".format(
                    side.active.name, side.active.sleep_turns
                )
            )

    # gen1 if you get `cant` from full paralysis while the opponent is partiallytrapped, they are freed
    if (
        battle.generation == "gen1"
        and len(split_msg) == 4
        and split_msg[3] == constants.PARALYZED
        and (
            constants.PARTIALLY_TRAPPED in other_side.active.volatile_statuses
            or other_side.active.volatile_status_durations[constants.PARTIALLY_TRAPPED]
            > 0
        )
    ):
        logger.info(
            f"{side.active.name} got 'cant' while target {other_side.active.name} was partially trapped, "
            f"removing partiallytrapped volatile from {other_side.active.name}"
        )
        remove_volatile(other_side.active, constants.PARTIALLY_TRAPPED)
        other_side.active.volatile_status_durations[constants.PARTIALLY_TRAPPED] = 0


def upkeep(battle, _):
    if battle.trick_room:
        battle.trick_room_turns_remaining -= 1
        logger.info(
            "Trick Room turns remaining: {}".format(battle.trick_room_turns_remaining)
        )

    if battle.field is not None and battle.field_turns_remaining > 0:
        battle.field_turns_remaining -= 1
        logger.info(
            "{} turns remaining: {}".format(battle.field, battle.field_turns_remaining)
        )

    if battle.field is not None and battle.field_turns_remaining == 0:
        logger.info(
            "{} did not end when expected, giving 3 more turns".format(battle.field)
        )
        battle.field_turns_remaining = 3

    if constants.ROOST in battle.user.active.volatile_statuses:
        logger.info(
            "Removing 'roost' from {}'s volatiles".format(battle.user.active.name)
        )
        battle.user.active.volatile_statuses = [
            v for v in battle.user.active.volatile_statuses if v != constants.ROOST
        ]

    if constants.ROOST in battle.opponent.active.volatile_statuses:
        logger.info(
            "Removing 'roost' from {}'s volatiles".format(battle.opponent.active.name)
        )
        battle.opponent.active.volatile_statuses = [
            v for v in battle.opponent.active.volatile_statuses if v != constants.ROOST
        ]

    for side in [battle.user, battle.opponent]:
        side_string = "opponent" if side == battle.opponent else "user"

        if "taunt" in side.active.volatile_statuses:
            if battle.generation in constants.TAUNT_DURATION_INCREMENT_END_OF_TURN:
                # legacy gen3/4 modeling: unchanged
                side.active.volatile_status_durations[constants.TAUNT] += 1
                logger.info(
                    "Incrementing taunt duration for {} to {}".format(
                        side_string,
                        side.active.volatile_status_durations[constants.TAUNT],
                    )
                )
            elif side.active.volatile_status_durations[constants.TAUNT] < 2:
                # gen5+: PS decrements taunt at every residual whether or not
                # the taunted mon moved (data/moves.ts taunt onResidualOrder
                # 15); the engine's end-of-turn arm counts UP from the apply
                # seed (fast=0 / slow=-1) and releases at counter==2
                # (genx/generate_instructions.rs:6687-6746). Cap at 2 - PS
                # emits the release `-end` during residuals BEFORE `upkeep`,
                # so the release turn never ticks here, and a missed `-end`
                # must not push the seed into the engine's never-release arm.
                side.active.volatile_status_durations[constants.TAUNT] += 1
                logger.info(
                    "Incrementing taunt duration for {} to {}".format(
                        side_string,
                        side.active.volatile_status_durations[constants.TAUNT],
                    )
                )

        if (
            "encore" in side.active.volatile_statuses
            and battle.generation
            not in constants.TAUNT_ENCORE_DURATION_INCREMENT_ON_MOVE
            and side.active.volatile_status_durations["encore"] < 2
        ):
            # same end-of-turn semantics as taunt above (PS encore
            # onResidualOrder 16; engine releases at counter==2,
            # genx/generate_instructions.rs:6752-6805). Pre-gen5 keeps the
            # legacy move-use tick in `move`.
            side.active.volatile_status_durations["encore"] += 1
            logger.info(
                "Incrementing encore duration for {} to {}".format(
                    side_string,
                    side.active.volatile_status_durations["encore"],
                )
            )

        if constants.LOCKED_MOVE in side.active.volatile_statuses:
            side.active.volatile_status_durations[constants.LOCKED_MOVE] += 1
            logger.info(
                "Incremented lockedmove for {} to {}".format(
                    side_string,
                    side.active.volatile_status_durations[constants.LOCKED_MOVE],
                )
            )

        if side.side_conditions[constants.REFLECT] > 0:
            side.side_conditions[constants.REFLECT] -= 1
            logger.info(
                "Decrementing reflect for {} to {}".format(
                    side_string, side.side_conditions[constants.REFLECT]
                )
            )
            if side.side_conditions[constants.REFLECT] == 0:
                logger.info(
                    "reflect did not end for {} when expected, giving it 3 more turns".format(
                        side_string
                    )
                )
                side.side_conditions[constants.REFLECT] = 3

        if side.side_conditions[constants.LIGHT_SCREEN] > 0:
            side.side_conditions[constants.LIGHT_SCREEN] -= 1
            logger.info(
                "Decrementing lightscreen for {} to {}".format(
                    side_string, side.side_conditions[constants.LIGHT_SCREEN]
                )
            )
            if side.side_conditions[constants.LIGHT_SCREEN] == 0:
                logger.info(
                    "lightscreen did not end for {} when expected, giving it 3 more turns".format(
                        side_string
                    )
                )
                side.side_conditions[constants.LIGHT_SCREEN] = 3

        if side.side_conditions[constants.AURORA_VEIL] > 0:
            side.side_conditions[constants.AURORA_VEIL] -= 1
            logger.info(
                "Decrementing auroraveil for {} to {}".format(
                    side_string, side.side_conditions[constants.AURORA_VEIL]
                )
            )
            if side.side_conditions[constants.AURORA_VEIL] == 0:
                logger.info(
                    "auroraveil did not end for {} when expected, giving it 3 more turns".format(
                        side_string
                    )
                )
                side.side_conditions[constants.AURORA_VEIL] = 3

        if side.side_conditions[constants.TAILWIND] > 0:
            side.side_conditions[constants.TAILWIND] -= 1
            logger.info(
                "Decrementing tailwind for {} to {}".format(
                    side_string, side.side_conditions[constants.TAILWIND]
                )
            )

        if side.side_conditions[constants.MIST] > 0:
            side.side_conditions[constants.MIST] -= 1
            logger.info(
                "Decrementing mist for {} to {}".format(
                    side_string, side.side_conditions[constants.MIST]
                )
            )

        if side.side_conditions[constants.SAFEGUARD] > 0:
            side.side_conditions[constants.SAFEGUARD] -= 1
            logger.info(
                "Decrementing safeguard for {} to {}".format(
                    side_string, side.side_conditions[constants.SAFEGUARD]
                )
            )

        pkmn = side.active
        if constants.YAWN in pkmn.volatile_statuses:
            previous_duration = pkmn.volatile_status_durations[constants.YAWN]
            if previous_duration == 0:
                pkmn.volatile_status_durations[constants.YAWN] = 1
            elif previous_duration == 1:
                pkmn.volatile_status_durations[constants.YAWN] = 0
                remove_volatile(pkmn, constants.YAWN)
                logger.info("Removed yawn volatile from {}".format(pkmn.name))
            else:
                raise ValueError(
                    "Got yawn duration {} for {}".format(previous_duration, pkmn.name)
                )
            logger.info(
                "{} had yawn at the end of the turn, changed duration from {} to {}".format(
                    pkmn.name,
                    previous_duration,
                    pkmn.volatile_status_durations[constants.YAWN],
                )
            )
        if constants.SLOW_START in pkmn.volatile_statuses:
            # PS slowstart onResidual (data/abilities.ts:4308-4315) is gated
            # on `pokemon.activeTurns`: switchIn zeroes it
            # (sim/battle-actions.ts:137) and nextTurn increments it AFTER
            # residuals (sim/battle.ts:1762), so the counter does NOT tick at
            # the upkeep of the turn the mon entered on (mid-turn switch,
            # drag, or pivot follow-up). Leads and post-upkeep faint
            # replacements are at >=1 by their first upkeep and always tick.
            if pkmn.active_turns > 0:
                pkmn.volatile_status_durations[constants.SLOW_START] -= 1
                logger.info(
                    "Decremented slow start duration for {} to {}".format(
                        pkmn.name, pkmn.volatile_status_durations[constants.SLOW_START]
                    )
                )
            else:
                logger.info(
                    "Skipping slow start decrement for {} (entered this turn)".format(
                        pkmn.name
                    )
                )

        # PS decrements effect durations at the end of every turn
        # (sim/battle.ts:516 `handler.state.duration--`; Disable's condition is
        # data/moves.ts:3664-3674, duration 5 minus one if the target already
        # moved). The engine reads the seeded value as turns-REMAINING and ticks
        # it inside search, so the root value must shrink each completed real
        # turn too. Floored at 0: PS can hold the volatile one turn longer than
        # the 4-turn seed and |-end|Disable zeroes it regardless.
        if (
            constants.DISABLE in pkmn.volatile_statuses
            and pkmn.volatile_status_durations[constants.DISABLE] > 0
        ):
            pkmn.volatile_status_durations[constants.DISABLE] -= 1
            logger.info(
                "Decremented disable duration for {} to {}".format(
                    pkmn.name, pkmn.volatile_status_durations[constants.DISABLE]
                )
            )

        # Partial-trap (Wrap/Bind/Fire Spin/...) and Magnet Rise are consumed by
        # the engine as an elapsed-EOT count - "chips taken / turns levitated so
        # far" - which it ticks UP in-search (generate_instructions.rs:6628-6739:
        # partiallytrapped chips + `+= 1` until release at `>= 4`; magnetrise
        # match 0..=3 -> `+= 1`, 4 -> release). PS ticks both every end-of-turn
        # (conditions.ts partiallytrapped onResidual baseMaxhp/8; moves.ts
        # magnetrise onResidualOrder 18), so grow the root count each completed
        # real turn, capped at 4 (the folded 5-turn model / the engine's release
        # threshold; magnetrise panics on a seed > 4). gen1's partial trap is
        # applied and released at move-time (see `move`), so it is excluded.
        if (
            battle.generation != "gen1"
            and constants.PARTIALLY_TRAPPED in pkmn.volatile_statuses
            and pkmn.volatile_status_durations[constants.PARTIALLY_TRAPPED] < 4
        ):
            pkmn.volatile_status_durations[constants.PARTIALLY_TRAPPED] += 1
            logger.info(
                "Incremented partiallytrapped elapsed count for {} to {}".format(
                    pkmn.name,
                    pkmn.volatile_status_durations[constants.PARTIALLY_TRAPPED],
                )
            )

        if (
            constants.MAGNET_RISE in pkmn.volatile_statuses
            and pkmn.volatile_status_durations[constants.MAGNET_RISE] < 4
        ):
            pkmn.volatile_status_durations[constants.MAGNET_RISE] += 1
            logger.info(
                "Incremented magnetrise elapsed count for {} to {}".format(
                    pkmn.name,
                    pkmn.volatile_status_durations[constants.MAGNET_RISE],
                )
            )

        # Heal Block (Psychic Noise: duration 2, data/moves.ts:8288-8292
        # durationCallback), Throat Chop (duration 2, data/moves.ts:19392-19420)
        # and Syrup Bomb (duration 4 = three residual Speed drops,
        # data/moves.ts:18764-18782) all tick at PS end-of-turn, and the engine
        # consumes each as an elapsed-EOT count ticked up in-search
        # (generate_instructions.rs: healblock 0 -> 1 then release at 1,
        # panic above 1; throatchop release at 1, a higher seed never releases;
        # syrupbomb 0|1|2 -> drop+tick, release at 3, panic above 3). Grow the
        # root count each completed real turn like magnetrise above, capped at
        # each release counter so a missed `-end` can never push the seed into
        # a panic/never-release arm. PS emits the release `-end` during
        # residuals, BEFORE `upkeep`, so the release turn never ticks here.
        for volatile, release_counter in (
            (constants.HEAL_BLOCK, 1),
            (constants.THROAT_CHOP, 1),
            (constants.SYRUP_BOMB, 3),
        ):
            if (
                volatile in pkmn.volatile_statuses
                and pkmn.volatile_status_durations[volatile] < release_counter
            ):
                pkmn.volatile_status_durations[volatile] += 1
                logger.info(
                    "Incremented {} elapsed count for {} to {}".format(
                        volatile,
                        pkmn.name,
                        pkmn.volatile_status_durations[volatile],
                    )
                )

        if (
            battle.generation == "gen3"
            and pkmn.status == constants.SLEEP
            and side.last_used_move.move != "sleeptalk"
        ):
            pkmn.gen_3_consecutive_sleep_talks = 0
            logger.info(
                "{} is asleep but didn't use sleeptalk, decrementing gen_3_consecutive_sleep_talks to 0".format(
                    pkmn.name
                )
            )

    if battle.user.side_conditions[constants.PROTECT] > 0:
        battle.user.side_conditions[constants.PROTECT] -= 1
        logger.info(
            "Setting protect to {} for the bot".format(
                battle.user.side_conditions[constants.PROTECT]
            )
        )

    if battle.opponent.side_conditions[constants.PROTECT] > 0:
        battle.opponent.side_conditions[constants.PROTECT] -= 1
        logger.info(
            "Setting protect to {} for the opponent".format(
                battle.opponent.side_conditions[constants.PROTECT]
            )
        )

    if battle.user.wish[0] > 0:
        battle.user.wish = (battle.user.wish[0] - 1, battle.user.wish[1])
        logger.info("Decrementing wish to {} for the bot".format(battle.user.wish[0]))

    if battle.opponent.wish[0] > 0:
        battle.opponent.wish = (battle.opponent.wish[0] - 1, battle.opponent.wish[1])
        logger.info(
            "Decrementing wish to {} for the opponent".format(battle.opponent.wish[0])
        )

    if battle.user.future_sight[0] > 0:
        battle.user.future_sight = (
            battle.user.future_sight[0] - 1,
            battle.user.future_sight[1],
        )
        logger.info(
            "Decrementing future_sight to {} for the bot".format(
                battle.user.future_sight[0]
            )
        )

    if battle.opponent.future_sight[0] > 0:
        battle.opponent.future_sight = (
            battle.opponent.future_sight[0] - 1,
            battle.opponent.future_sight[1],
        )
        logger.info(
            "Decrementing future_sight to {} for the opponent".format(
                battle.opponent.future_sight[0]
            )
        )

    # If a pkmn has less than maxhp during upkeep,
    # we do not want to guess leftovers/blacksludge anymore when it is time to guess an item
    # leftovers and blacksludge will reveal themselves at the end of the turn if they exist
    opp_pkmn = battle.opponent.active
    # ONLY valid once this mon has actually SURVIVED a residual phase below max
    # HP without healing. Being below max HP at upkeep proves nothing on its own:
    # a mon that switched in and took hazard damage is below max at upkeep and
    # will still heal at the residual step, and a mon that healed and then took
    # further damage this turn is also below max.
    #
    # The old unconditional form was an unsound INFERENCE, the same class of
    # defect as a false reverse-damage elimination. Measured: it excluded the
    # opponent's TRUE set (SPEC-B4 worked example -- Floatzel, item unknown,
    # `leftovers` already in impossible_items, true item Leftovers).
    #
    # `saw_residual_without_heal` is set by the residual handler only after an
    # end-of-turn completes with this mon below max HP and no item heal observed.
    below_max = opp_pkmn.hp < opp_pkmn.max_hp
    prev_hp = getattr(opp_pkmn, "_last_upkeep_hp", None)
    # Sound only across TWO upkeeps: Leftovers fires once per residual phase, so
    # a mon that sat below max through a full turn boundary and did not gain HP
    # cannot be holding it. A single below-max reading is consistent with having
    # just switched into hazards, or with healing and then taking more damage.
    saw_residual_without_heal = (
        below_max and prev_hp is not None and opp_pkmn.hp <= prev_hp
    )
    opp_pkmn._last_upkeep_hp = opp_pkmn.hp

    if below_max and saw_residual_without_heal:
        logger.info(
            "{} completed a residual phase below maxhp without healing - "
            "leftovers/blacksludge ruled out".format(opp_pkmn.name)
        )
        opp_pkmn.impossible_items.add(constants.LEFTOVERS)
        opp_pkmn.impossible_items.add(constants.BLACK_SLUDGE)

    if opp_pkmn.status is None:
        opp_pkmn.impossible_items.add("flameorb")
        opp_pkmn.impossible_items.add("toxicorb")


def mega(battle, split_msg):
    if is_opponent(battle, split_msg):
        side = battle.opponent
    else:
        side = battle.user

    side.active.is_mega = True
    forced_mega_ability = normalize_name(
        pokedex[side.active.name][constants.ABILITIES]["0"]
    )
    side.active.ability = forced_mega_ability
    logger.info(
        "Mega-Pokemon: {} with ability {}".format(side.active.name, forced_mega_ability)
    )


def transform(battle, split_msg):
    if is_opponent(battle, split_msg):
        side = battle.opponent
        other_side = battle.user
    else:
        side = battle.user
        other_side = battle.opponent

    transformed_into_name = other_side.active.name
    logger.info(
        "{} transformed into {}".format(side.active.name, transformed_into_name)
    )
    side.active.boosts = deepcopy(other_side.active.boosts)
    logger.info(
        "Copied {}'s boosts: {}".format(side.active.name, dict(side.active.boosts))
    )

    if constants.TRANSFORM not in side.active.volatile_statuses:
        side.active.volatile_statuses.append(constants.TRANSFORM)

    transformed_into = other_side.active
    side.active.stats = deepcopy(transformed_into.stats)
    side.active.moves = deepcopy(transformed_into.moves)
    side.active.types = deepcopy(transformed_into.types)
    side.active.boosts = deepcopy(transformed_into.boosts)

    # record the copied species identity. Showdown's transformInto also copies
    # the target's species-id and weight (pokemon.ts:1276-1386); we don't store
    # a separate weight/name on the Pokemon object, so `transformed_into` is the
    # single source from which id/weight/base-types are resolved at engine
    # conversion (otherwise a transformed Ditto reaches the engine as 4.0kg base
    # Ditto and Heavy Slam/Low Kick BP + any stat recalc are wrong).
    side.active.transformed_into = transformed_into_name

    for mv in side.active.moves:
        mv.current_pp = 5

    if split_msg[-1].startswith("[from]") and "ability:" in split_msg[-1]:
        side.active.original_ability = normalize_name(
            split_msg[-1].split("ability:")[-1].strip()
        )
    elif side.active.ability is not None:
        side.active.original_ability = side.active.ability

    # PS transformInto copies the target's CURRENT ability (sim/pokemon.ts
    # transformInto). When the target is the bot's own active and its object
    # has not been refreshed from a request yet (log replay / reconnect), the
    # request JSON still carries the authoritative ability - copying None here
    # would let later fill-if-unknown paths backfill the transformer's own
    # ability (e.g. a Ditto stuck on imposter instead of the copied contrary)
    transformed_into_ability = transformed_into.ability
    if (
        transformed_into_ability is None
        and other_side is battle.user
        and battle.request_json
    ):
        for request_pkmn in battle.request_json.get(constants.SIDE, {}).get(
            constants.POKEMON, []
        ):
            if request_pkmn.get(constants.ACTIVE):
                transformed_into_ability = normalize_name(
                    request_pkmn.get(constants.REQUEST_DICT_ABILITY) or ""
                ) or None
                break
        if transformed_into_ability is not None:
            logger.info(
                "Filling transform-copied ability for {} from the request JSON: {}".format(
                    side.active.name, transformed_into_ability
                )
            )

    side.active.ability = deepcopy(transformed_into_ability)


def turn(battle, split_msg):
    battle.turn = int(split_msg[2])
    logger.info("")
    logger.info("Turn: {}".format(battle.turn))

    # PS `nextTurn` clears `statsRaisedThisTurn` on every active mon -- but the whole block
    # is guarded by `if (this.turn !== 1)` (sim/battle.ts:1673-1675), and `|turn|N` is
    # emitted by `nextTurn` AFTER it incremented `this.turn`, so the comparison here is
    # against the turn number just parsed. That exception is the entire reason a turn-start
    # state can carry the flag at all: a `|start|` switch-in ability boost survives into
    # turn 1's action resolution (synth75383 T1: Porygon2's Download, then Alluring Voice).
    if battle.turn != 1:
        battle.user.stats_raised_this_turn = False
        battle.opponent.stats_raised_this_turn = False

    # a new turn opens a fresh action window: nobody has acted yet. (The
    # taunt/encore apply seed in start_volatile_status keys on this; switch-in
    # also clears it for the entering pkmn in switch_or_drag.)
    # The |turn| line is emitted by PS's nextTurn immediately after it
    # incremented every active mon's activeTurns (sim/battle.ts:1762/1782),
    # so mirror that increment here: leads go 0 -> 1 at |turn|1 (and DO get
    # the end-of-turn-1 Slow Start decrement), while a mon that entered
    # mid-turn was still at 0 during its entry turn's upkeep.
    for side in (battle.user, battle.opponent):
        if side.active is not None:
            side.active.moved_this_turn = False
            side.active.active_turns += 1


def noinit(battle, split_msg):
    if split_msg[2] == "rename":
        battle.battle_tag = split_msg[3]
        logger.info("Renamed battle to {}".format(battle.battle_tag))


def update_battle(battle: Battle, msg: str):
    msg_lines = msg.split("\n")
    for line in msg_lines:
        split_msg = line.split("|")
        if len(split_msg) < 2:
            continue

        action = split_msg[1].strip()
        if action == "request":
            request(battle, split_msg)
            process_battle_updates(battle)
            return not battle.wait
        else:
            battle.msg_list.append(line)

    return False


def process_battle_updates(battle: Battle):
    msg_lines = battle.msg_list
    check_speed_ranges(battle, msg_lines)
    for i, line in enumerate(msg_lines):
        split_msg = line.split("|")
        if len(split_msg) < 2:
            continue

        action = split_msg[1].strip()

        battle_modifiers_lookup = {
            "switch": switch,
            "faint": faint,
            "-fail": fail,
            "drag": drag,
            "-heal": heal_or_damage,
            "-damage": heal_or_damage,
            "-sethp": sethp,
            "move": move,
            "-setboost": setboost,
            "-boost": boost,
            "-unboost": unboost,
            "-status": status,
            "-activate": activate,
            "-anim": anim,
            "-prepare": prepare,
            "-start": start_volatile_status,
            "-singlemove": start_volatile_status,
            "-end": end_volatile_status,
            "-curestatus": curestatus,
            "-cureteam": cureteam,
            "-weather": weather,
            "-fieldstart": fieldstart,
            "-fieldend": fieldend,
            "-sidestart": sidestart,
            "-sideend": sideend,
            "-swapsideconditions": swapsideconditions,
            "-item": set_item,
            "-enditem": remove_item,
            "-immune": immune,
            "-miss": miss,
            "-ability": update_ability,
            "detailschange": form_change,
            "replace": illusion_end,
            "-formechange": form_change,
            "-transform": transform,
            "-mega": mega,
            "-terastallize": terastallize,
            "-zpower": zpower,
            "-clearnegativeboost": clearnegativeboost,
            "-clearboost": clearboost,
            "-clearallboost": clearallboost,
            "-singleturn": singleturn,
            "-mustrecharge": mustrecharge,
            "upkeep": upkeep,
            "cant": cant,
            "error": error,
            "inactive": inactive,
            "inactiveoff": inactiveoff,
            "turn": turn,
            "noinit": noinit,
        }

        function_to_call = battle_modifiers_lookup.get(action)
        if function_to_call is not None:
            if function_to_call is activate:
                # `-activate` is the only handler that has to know WHICH hit it
                # is reporting, and the protocol only says so in the lines
                # AROUND it (a Substitute absorption carries no attacker, no
                # move and no magnitude of its own).  Hand it its position in
                # the block rather than letting it infer the attacker from
                # `last_used_move`, which a delayed move makes wrong.
                function_to_call(battle, split_msg, msg_lines, i)
            else:
                function_to_call(battle, split_msg)

        if action == "move" and is_opponent(battle, split_msg):
            if normalize_name(split_msg[3].strip()) == constants.HIDDEN_POWER:
                check_opponent_hiddenpower(battle, msg_lines[i + 1])
            check_choicescarf(battle, msg_lines)
            damage_dealt = get_damage_dealt(battle, split_msg, msg_lines[i + 1 :])
            if damage_dealt:
                update_dataset_possibilities(battle, damage_dealt, "damage_dealt")

        elif action == "move" and not is_opponent(battle, split_msg):
            damage_dealt = get_damage_dealt(battle, split_msg, msg_lines[i + 1 :])
            if damage_dealt:
                update_dataset_possibilities(battle, damage_dealt, "damage_received")

        elif action == "switch" and is_opponent(battle, split_msg):
            check_heavydutyboots(battle, msg_lines[i + 1 :])

    battle.msg_list.clear()


async def async_update_battle(battle, msg):
    return update_battle(battle, msg)
