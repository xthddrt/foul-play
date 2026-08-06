import logging
from collections import Counter
from copy import deepcopy

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
from fp.battle import DamageDealt
from fp.battle import StatRange
from fp.battle import boost_multiplier_lookup
from fp.search.poke_engine_helpers import poke_engine_get_damage_rolls
from fp.search.poke_engine_helpers import poke_engine_get_damage_roll_sets
from fp.helpers import (
    normalize_name,
    random_battles_evs,
    maximum_ev,
    calculate_stats,
)
from fp.helpers import get_pokemon_info_from_condition
from fp.helpers import (
    is_not_very_effective,
    is_super_effective,
    is_neutral_effectiveness,
)


logger = logging.getLogger(__name__)

MOVE_END_STRINGS = {"move", "switch", "upkeep", "-miss", ""}

# {(check_type, move, reason): count} -- damage observations the reverse damage
# calc DECLINED to act on. Modelled on `hp_certificate.CERTIFICATE_REFUSALS`: a
# refusal changes nothing externally, but a channel that refuses constantly is
# a channel whose premises are wrong, and that has to be visible without a
# rework cycle. Unlike CERTIFICATE_REFUSALS (dumped by the corpus sweep at
# fp/replay/checker.py and check_replays.py), this counter has no sweep
# consumer yet -- every refusal also logs at WARNING, so visibility is via
# logs; wire it into a stats dump if a live run ever needs the aggregate.
DAMAGE_CHECK_REFUSALS = Counter()


def reset_damage_check_refusals():
    DAMAGE_CHECK_REFUSALS.clear()


# ---------------------------------------------------------------------------
# CRIT / HITCOUNT DISPATCH HYGIENE (sampling wave 2, item 1)
#
# `|-crit|` and `|-hitcount|` used to have NO handler at all: every consumer
# that needed them re-scanned the protocol block on its own terms
# (`get_damage_dealt` forward from the |move| line and ignoring which mon the
# crit named, `_substitute_hit_context` backwards from the `-activate` and
# matching the defender slot).  Two independent derivations of the same fact
# can disagree, and a mis-selected crit arm poisons an elimination in the worst
# possible direction: the true set is the STRONGEST candidate, so reading a
# crit that did not happen prunes exactly it.  A FALSE elimination is the one
# failure mode this whole wave must never have.
#
# So the two lines are stamped ONCE, onto the pokemon the protocol names, and
# every consumer reads the stamp.  The flags describe the CURRENT hit context:
# `clear_hit_flags` runs at every `|move|` line and at `|turn|`, and
# `stamp_hit_flags` replays the context's own `-crit`/`-hitcount` lines onto
# the named targets.  Stamping is idempotent, so the dispatch-table handlers
# re-applying it as the block is walked line-by-line changes nothing.
#
# These live here rather than in battle_modifier because `get_damage_dealt` is
# called at the |move| line -- BEFORE the block's later lines have been
# dispatched -- so it has to stamp its own slice; battle_modifier imports this
# module, not the other way around.
# ---------------------------------------------------------------------------
def side_from_pokemon_identifier(battle, identifier):
    """Resolve a 'p1a: Nickname' protocol identifier to the Battler it belongs
    to. Returns None when the identifier is missing/unrecognised."""
    identifier = (identifier or "").strip()
    if not identifier:
        return None
    if identifier.startswith(battle.user.name):
        return battle.user
    if identifier.startswith(battle.opponent.name):
        return battle.opponent
    return None


def _active_from_identifier(battle, identifier):
    side = side_from_pokemon_identifier(battle, identifier)
    if side is None:
        return None
    return side.active


def clear_hit_flags(battle):
    """Open a fresh hit context: no crit, no hitcount, on either side.

    Every pokemon on both sides is cleared (not just the actives) so a mon that
    switches in mid-turn can never inherit a stamp from an earlier turn.
    """
    for side in (battle.user, battle.opponent):
        pkmn_list = list(side.reserve)
        if side.active is not None:
            pkmn_list.append(side.active)
        for pkmn in pkmn_list:
            pkmn.crit_this_turn = False
            pkmn.hitcount_this_turn = None


def crit(battle, split_msg):
    """`|-crit|<defender>` -- stamp the crit onto the mon it names."""
    if len(split_msg) < 3:
        return
    pkmn = _active_from_identifier(battle, split_msg[2])
    if pkmn is not None:
        pkmn.crit_this_turn = True


def hitcount(battle, split_msg):
    """`|-hitcount|<defender>|<n>` -- stamp how many strikes landed."""
    if len(split_msg) < 4:
        return
    pkmn = _active_from_identifier(battle, split_msg[2])
    if pkmn is None:
        return
    try:
        pkmn.hitcount_this_turn = int(split_msg[3].strip())
    except ValueError:
        return
    _rule_out_loaded_dice(battle, split_msg[2], pkmn.hitcount_this_turn)


# Loaded Dice forces a 2-5 hit move to roll 4 or 5 (PS items.ts onModifyMove).
# So 2 or 3 landed hits PROVE the attacker is not holding it -- the same kind of
# hard negative evidence as the whiteherb/chestoberry eliminations, feeding the
# same `impossible_items` -> data/pkmn_sets.py:518 hard filter.
#
# Two conditions make it a proof rather than a guess, and both are required:
#   * the defender SURVIVED. A multi-hit move stops early when the target
#     faints, so a Loaded Dice user can legitimately show hitcount 2 on a KO.
#   * the move is a genuine [2,5] roller. Fixed-count moves (Triple Axel 3,
#     Population Bomb 10) are modified differently or not at all by the item.
# The positive direction (4-5 hits is ~3.3x likelier with the item) is a
# LIKELIHOOD RATIO, not a certainty, and impossible_items has no weighting
# concept -- so it is deliberately not implemented here.
def _rule_out_loaded_dice(battle, defender_identifier, hits):
    if hits is None or hits > 3:
        return
    defender_side = side_from_pokemon_identifier(battle, defender_identifier)
    if defender_side is None:
        return
    defender = defender_side.active
    if defender is None or defender.hp <= 0:
        return  # fainted: the sequence was cut short, hits proves nothing
    attacker_side = (
        battle.opponent if defender_side is battle.user else battle.user
    )
    # only the OPPONENT's item is unknown and worth narrowing
    if attacker_side is not battle.opponent:
        return
    attacker = attacker_side.active
    if attacker is None or attacker.item != constants.UNKNOWN_ITEM:
        return
    move_name = normalize_name(attacker_side.last_used_move.move or "")
    if not move_name:
        return
    if all_move_json.get(move_name, {}).get("multihit") != [2, 5]:
        return
    attacker.impossible_items.add("loadeddice")


def stamp_hit_flags(battle, lines):
    """Stamp the `-crit`/`-hitcount` of ONE hit context onto their targets.

    `lines` are the protocol lines following the context's opener; the scan
    stops at the same `MOVE_END_STRINGS` boundary every other consumer uses.
    """
    for line in lines or ():
        split_line = line.split("|")
        if len(split_line) < 2 or split_line[1] in MOVE_END_STRINGS:
            break
        action = split_line[1].strip()
        if action == "-crit":
            crit(battle, split_line)
        elif action == "-hitcount":
            hitcount(battle, split_line)


def can_have_priority_modified(battle, pokemon, move_name):
    return (
        "prankster"
        in [
            normalize_name(a)
            for a in pokedex[pokemon.name][constants.ABILITIES].values()
        ]
        or (move_name == "grassyglide" and battle.field == constants.GRASSY_TERRAIN)
        or (
            move_name in all_move_json
            and all_move_json[move_name][constants.CATEGORY] == constants.STATUS
            and "myceliummight"
            in [
                normalize_name(a)
                for a in pokedex[pokemon.name][constants.ABILITIES].values()
            ]
        )
    )


def _is_called_move_line(ln):
    """True if a |move| line was produced inside useMove rather than by the
    pokemon's own action (Sleep Talk's inner move, a Dancer copy, a Magic Bounce
    reflection). Such a line does not occupy an action slot, so counting it
    would push the action count past 2 and throw away the turn's ordering fact.
    `[from]lockedmove` (an Outrage continuation) IS a real action and is kept.
    """
    # deferred import: fp.battle_modifier imports fp.inference at module load
    from fp.battle_modifier import has_non_lockedmove_from_tag

    return has_non_lockedmove_from_tag(ln.split("|"))


def can_have_speed_modified(battle, pokemon):
    return (
        (
            pokemon.item is None
            # None = unrevealed, or revealed AS unburden: both mean the 2x may
            # be live. Bailing only on None would let a known-unburden mon with
            # its item gone through, and check_speed_ranges never divides an
            # opponent speed ABILITY back out -- the bound would be corrupt.
            and pokemon.ability in (None, "unburden")
            and "unburden"
            in [
                normalize_name(a)
                for a in pokedex[pokemon.name][constants.ABILITIES].values()
            ]
        )
        or (
            battle.weather == constants.RAIN
            and pokemon.ability is None
            and "swiftswim"
            in [
                normalize_name(a)
                for a in pokedex[pokemon.name][constants.ABILITIES].values()
            ]
        )
        or (
            battle.weather == constants.SUN
            and pokemon.ability is None
            and "chlorophyll"
            in [
                normalize_name(a)
                for a in pokedex[pokemon.name][constants.ABILITIES].values()
            ]
        )
        or (
            battle.weather == constants.SAND
            and pokemon.ability is None
            and "sandrush"
            in [
                normalize_name(a)
                for a in pokedex[pokemon.name][constants.ABILITIES].values()
            ]
        )
        or (
            battle.weather in constants.HAIL_OR_SNOW
            and pokemon.ability is None
            and "slushrush"
            in [
                normalize_name(a)
                for a in pokedex[pokemon.name][constants.ABILITIES].values()
            ]
        )
        or (
            battle.field == constants.ELECTRIC_TERRAIN
            and pokemon.ability is None
            and "surgesurfer"
            in [
                normalize_name(a)
                for a in pokedex[pokemon.name][constants.ABILITIES].values()
            ]
        )
        or (
            pokemon.status == constants.PARALYZED
            and pokemon.ability is None
            and "quickfeet"
            in [
                normalize_name(a)
                for a in pokedex[pokemon.name][constants.ABILITIES].values()
            ]
        )
    )


def is_opponent(battle, split_msg):
    return not split_msg[2].startswith(battle.user.name)


def get_move_information(m):
    # Given a |move| line from the PS protocol, extract the user of the move and the move object
    try:
        split_move_line = m.split("|")
        return split_move_line[2], all_move_json[normalize_name(split_move_line[3])]
    except KeyError:
        logger.warning(
            "Unknown move {} - using standard 0 priority move".format(
                normalize_name(m.split("|")[3])
            )
        )
        return m.split("|")[2], {constants.ID: "unknown", constants.PRIORITY: 0}


def _opponent_could_move_last_regardless(battle):
    """True if the opponent could have selected a negative-priority move.

    A `|cant|<opponent>|flinch` proves the opponent's action came after ours,
    which is only a SPEED fact if nothing else could have put it last. Any
    negative-priority candidate (Roar, Dragon Tail, Circle Throw, Trick Room,
    Teleport, ...) breaks the proof. Conservative outside random battles, where
    there is no candidate-move dataset to consult.
    """
    if battle.opponent.active is None:
        return True
    for mv in battle.opponent.active.moves:
        if mv.name in all_move_json and all_move_json[mv.name][constants.PRIORITY] < 0:
            return True
    if battle.battle_type != BattleType.RANDOM_BATTLE:
        return True
    possible_moves = RandomBattleTeamDatasets.get_all_possible_moves(
        battle.opponent.active
    )
    if not possible_moves:
        return True
    for mv in possible_moves:
        if mv in all_move_json and all_move_json[mv][constants.PRIORITY] < 0:
            return True
    return False


def check_speed_ranges(battle, msg_lines):
    """
    Intention:
        This function is intended to set the min or max possible speed that the opponent's
        active Pokemon could possibly have given a turn that just happened.

        For example: if both the bot and the opponent use an equal priority move but the
        opponent moves first, then the opponent's min_speed attribute will be set to the
        bots actual speed. This is because the opponent must have at least that much speed
        for it to have gone first.

        These min/max speeds are set without knowledge of items. If the opponent goes first
        when having a choice scarf then min speed will still be set to the bots speed. When
        it comes time to guess a Pokemon's possible set(s), the item must be taken into account
        as well when determining the final speed of a Pokemon. Abilities are NOT taken into
        consideration because their speed modifications are subject to certain conditions
        being present, whereas a choice scarf ALWAYS boosts speed.

        If there is a situation where an ability could have modified the turn order (either by
        changing a move's priority or giving a Pokemon more speed) then this check should be
        skipped. Examples are:
            - either side switched
            - the opponent COULD have a speed-boosting weather ability AND that weather is up
            - the opponent COULD have prankster and it used a status move
            - Grassy Glide is used when Grassy Terrain is up
    """
    seen_move = False
    actions = []
    for ln in msg_lines:
        if ln.startswith("|move|"):
            seen_move = True
            if not _is_called_move_line(ln):
                actions.append(get_move_information(ln))

        # A |switch|/|drag| BEFORE the first |move| line means the mons that
        # resolved the moves are not the ones this check would compare - bail.
        #
        # A |switch|/|drag| AFTER a |move| line cannot have changed the
        # ordering of the |move| lines already emitted: it is a pivot
        # follow-up (U-turn/Volt Switch/Flip Turn/Parting Shot/Chilly
        # Reception), an Eject Button/Red Card, or a faint replacement. Turn
        # order was fixed before any of them. `check_speed_ranges` runs BEFORE
        # any line of this block is applied (process_battle_updates), so the
        # battle state read below is still the state those moves were ordered
        # under. Bailing on these threw away a large fraction of randbats
        # turns' speed evidence.
        if (ln.startswith("|switch|") or ln.startswith("|drag|")) and not seen_move:
            return

        # A |cant| line occupies the pokemon's action slot in the protocol, so
        # its POSITION is an ordering fact even though no move resolved. Two
        # shapes on the OPPONENT are usable:
        #   - `recharge`: a fixed 0-priority pseudo-action (no move choice to
        #     be wrong about), so it can stand in for the opponent's action.
        #   - `flinch`: the opponent demonstrably acted after our hit. Sound
        #     only if it could not have selected a negative-priority move
        #     (Roar/Dragon Tail/... would explain the ordering without speed).
        # Every other shape (slp/par/frz/truant/nopp/ability blocks) keeps the
        # original bail: we do not know what priority the blocked move had.
        if ln.startswith("|cant|"):
            split_cant = ln.split("|")
            cant_actor = split_cant[2] if len(split_cant) > 2 else ""
            cant_reason = split_cant[3].strip() if len(split_cant) > 3 else ""
            if not cant_actor.startswith(battle.opponent.name) or cant_reason not in (
                "recharge",
                "flinch",
            ):
                return
            if cant_reason == "flinch" and _opponent_could_move_last_regardless(battle):
                return
            actions.append(
                (cant_actor, {constants.ID: cant_reason, constants.PRIORITY: 0})
            )
            continue

        # if anyone hit themselves in confusion
        # skip this check as we don't know if they used a priority move
        if ln.startswith("|-activate|") and ln.endswith("confusion"):
            return

        # If anyone used a custapberry, skip this check
        if ln.startswith("|-enditem|") and (
            "custapberry" in normalize_name(ln) or "Custap Berry" in ln
        ):
            return

        # If anyone had quick claw activate, skip this check
        if "quickclaw" in normalize_name(ln) or "Quick Claw" in ln:
            return

        # If anyone had quick claw activate, skip this check
        if "quickdraw" in normalize_name(ln) or "Quick Draw" in ln:
            return

    # A "switch" selection without a |switch| line in this block happens when
    # answering Revival Blessing's revive prompt: the fainted target is healed
    # in the party without switching in, so the |switch| check above does not
    # catch it and last_selected_move is not a move that can be looked up
    if battle.user.last_selected_move.move.startswith("switch "):
        return

    moves = actions
    number_of_moves = len(moves)
    if number_of_moves not in [1, 2]:
        return

    if (
        number_of_moves == 1
        and moves[0][0].startswith(battle.opponent.name)
        and moves[0][1][constants.ID] != "pursuit"
    ):
        moves.append(
            (
                "{}a: {}".format(battle.opponent.name, battle.user.active.name),
                all_move_json[normalize_name(battle.user.last_selected_move.move)],
            )
        )

    # if the bot knocked out the opponent there's nothing to do here
    elif number_of_moves == 1:
        return

    if (
        moves[0][1][constants.PRIORITY] != moves[1][1][constants.PRIORITY]
        or moves[0][1][constants.ID] == "encore"
    ):
        return

    bot_went_first = moves[0][0].startswith(battle.user.name)

    if (
        battle.opponent.active is None
        or battle.opponent.active.item == "choicescarf"
        or can_have_speed_modified(battle, battle.opponent.active)
        or (
            not bot_went_first
            and can_have_priority_modified(
                battle, battle.opponent.active, moves[0][1][constants.ID]
            )
        )
        or (
            bot_went_first
            and can_have_priority_modified(
                battle, battle.user.active, moves[0][1][constants.ID]
            )
        )
    ):
        return

    # Our own side's effective speed. `get_effective_speed` covers boosts, the
    # weather/terrain speed abilities, Unburden/Quick Feet, Tailwind, Choice
    # Scarf, paralysis and BOTH paradox speed volatiles - the hand-rolled chain
    # this replaced knew only Tailwind/paralysis/Scarf/protosynthesis, so any
    # speed ability on our side silently produced a too-tight opponent bound.
    speed_threshold = battle.get_effective_speed(battle.user)

    # `get_effective_speed` uses the gen7+ paralysis multiplier; older gens
    # quarter speed instead of halving it
    if (
        battle.user.active.status == constants.PARALYZED
        and battle.user.active.ability != "quickfeet"
        and battle.generation in ["gen4", "gen5", "gen6"]
    ):
        speed_threshold = int(speed_threshold / 2)

    # Divide out the opponent's known modifiers to get a threshold on their RAW
    # speed stat, which is what `speed_range` stores. Opponent speed ABILITIES
    # and Choice Scarf do not appear here: the guards above bail out entirely
    # when either could be in play.
    speed_threshold = int(
        speed_threshold
        / boost_multiplier_lookup[battle.opponent.active.boosts[constants.SPEED]]
    )

    if any(
        vs in battle.opponent.active.volatile_statuses
        for vs in ["quarkdrivespe", "protosynthesisspe"]
    ):
        speed_threshold = int(speed_threshold / 1.5)

    if battle.opponent.side_conditions[constants.TAILWIND]:
        speed_threshold = int(speed_threshold / 2)

    if battle.opponent.active.status == constants.PARALYZED:
        if battle.generation in ["gen4", "gen5", "gen6"]:
            speed_threshold = int(speed_threshold * 4)
        else:
            speed_threshold = int(speed_threshold * 2)

    # we want to swap which attribute gets updated in trickroom because the slower pokemon goes first
    if battle.trick_room:
        bot_went_first = not bot_went_first

    if bot_went_first:
        opponent_max_speed = min(
            battle.opponent.active.speed_range.max, speed_threshold
        )
        battle.opponent.active.speed_range = StatRange(
            min=battle.opponent.active.speed_range.min, max=opponent_max_speed
        )
        logger.info(
            "Updated {}'s max speed to {}".format(
                battle.opponent.active.name, battle.opponent.active.speed_range.max
            )
        )

    else:
        opponent_min_speed = max(
            battle.opponent.active.speed_range.min, speed_threshold
        )
        battle.opponent.active.speed_range = StatRange(
            min=opponent_min_speed, max=battle.opponent.active.speed_range.max
        )
        logger.info(
            "Updated {}'s min speed to {}".format(
                battle.opponent.active.name, battle.opponent.active.speed_range.min
            )
        )


def check_opponent_hiddenpower(battle, msg_line):
    """
    `msg_line` is should be the line *after* |-move|...|Hidden Power|...
    and is meant to be called for the opponent's pkmn only

    This function checks if the move was resisted, super-effective, or neutral.
    It then updates pkmn.hidden_power_possibilities based on that information
    """
    attacker = battle.opponent.active
    defender_types = battle.user.active.types
    logger.info(
        "Checking hiddenpower possibilities for opponent's {}".format(attacker.name)
    )
    logger.info(
        "Starting hiddenpower possibilities {}".format(
            attacker.hidden_power_possibilities
        )
    )

    next_line_split_msg = msg_line.split("|")
    if next_line_split_msg[1] == "-resisted":
        logger.info("{} resisted hiddenpower".format(defender_types))
        for t in list(attacker.hidden_power_possibilities):
            if not is_not_very_effective(t, defender_types):
                attacker.hidden_power_possibilities.remove(t)

    elif next_line_split_msg[1] == "-supereffective":
        logger.info("{} was weak to hiddenpower".format(defender_types))
        for t in list(attacker.hidden_power_possibilities):
            if not is_super_effective(t, defender_types):
                attacker.hidden_power_possibilities.remove(t)

    elif next_line_split_msg[1] == "-damage":
        logger.info("{} was neutral to hiddenpower".format(defender_types))
        for t in list(attacker.hidden_power_possibilities):
            if not is_neutral_effectiveness(t, defender_types):
                attacker.hidden_power_possibilities.remove(t)

    else:
        logger.info(
            "Cannot update hiddenpower possibilities with: {}".format(
                next_line_split_msg[1]
            )
        )
        return

    logger.info(
        "Remaining hiddenpower possibilities: {}".format(
            attacker.hidden_power_possibilities
        )
    )


def check_choicescarf(battle, msg_lines):
    if battle.generation in ["gen1", "gen2", "gen3"] or (
        battle.user.last_selected_move.move.startswith("switch ")
    ):
        return

    # Same positional scan as `check_speed_ranges`: a |switch|/|drag| AFTER a
    # |move| line is a pivot follow-up / faint replacement and cannot have
    # changed the ordering of the moves already emitted. `check_choicescarf`
    # is only ever reached at the OPPONENT's |move| line and only proceeds
    # when the opponent moved first, so every later line of the block
    # (including such a switch) is still unapplied when this runs.
    seen_move = False
    for ln in msg_lines:
        if ln.startswith("|move|"):
            seen_move = True
        if (ln.startswith("|switch|") or ln.startswith("|drag|")) and not seen_move:
            return
        if ln.startswith("|cant|") or (
            ln.startswith("|-activate|") and ln.endswith("confusion")
        ):
            return

    moves = [
        get_move_information(m)
        for m in msg_lines
        if m.startswith("|move|") and not _is_called_move_line(m)
    ]
    number_of_moves = len(moves)

    if number_of_moves not in [1, 2]:
        return

    bot_went_first = moves[0][0].startswith(battle.user.name)

    # NEGATIVE ARM: the bot went first, so a scarf can be RULED OUT whenever
    # even the scarfed opponent would have outsped us. Only sound where the
    # opponent's spread is exactly known (random battles), and only outside
    # trick room (there a scarf makes the opponent MORE likely to move last,
    # so moving last proves nothing). Both |move| lines must be present: an
    # opponent that never acted has an unknown selected priority.
    if bot_went_first and (
        number_of_moves != 2
        or battle.trick_room
        or battle.battle_type != BattleType.RANDOM_BATTLE
    ):
        return

    if number_of_moves == 1:
        moves.append(
            (
                "{}a: {}".format(battle.opponent.name, battle.user.active.name),
                all_move_json[normalize_name(battle.user.last_selected_move.move)],
            )
        )

    if moves[0][1][constants.PRIORITY] != moves[1][1][constants.PRIORITY]:
        return

    opponent_move_index = 1 if bot_went_first else 0
    user_move_index = 0 if bot_went_first else 1

    battle_copy = deepcopy(battle)
    if (
        battle.opponent.active is None
        or battle.opponent.active.item != constants.UNKNOWN_ITEM
        or not battle.opponent.active.can_have_choice_item
        or can_have_speed_modified(battle, battle.opponent.active)
        or can_have_priority_modified(
            battle, battle.opponent.active, moves[opponent_move_index][1][constants.ID]
        )
        or can_have_priority_modified(
            battle, battle.user.active, moves[user_move_index][1][constants.ID]
        )
        or (
            battle_copy.user.active.ability == "unburden"
            and battle_copy.user.active.item is None
        )
    ):
        return

    if battle.battle_type == BattleType.RANDOM_BATTLE:
        evs = ",".join(str(ev) for ev in random_battles_evs())
        battle_copy.opponent.active.set_spread(
            "serious", evs
        )  # random battles have known spreads
    else:
        if battle.trick_room:
            battle_copy.opponent.active.set_spread(
                "quiet", "0,0,0,0,0,0"
            )  # assume as slow as possible in trickroom
        else:
            battle_copy.opponent.active.set_spread(
                "jolly", f"0,0,0,0,0,{maximum_ev()}"
            )  # assume as fast as possible
    opponent_effective_speed = battle_copy.get_effective_speed(battle_copy.opponent)
    bot_effective_speed = battle_copy.get_effective_speed(battle_copy.user)

    if bot_went_first:
        # a scarf would have made the opponent strictly faster than us, yet it
        # moved second: it cannot be holding one. (Strict `>` keeps speed ties,
        # which are coin-flips, out of the elimination.)
        if int(opponent_effective_speed * 1.5) > bot_effective_speed:
            logger.info(
                "Opponent {} moved second but a choicescarf would have outsped "
                "us - ruling out choicescarf".format(battle.opponent.active.name)
            )
            battle.opponent.active.impossible_items.add("choicescarf")
        return

    if battle.trick_room:
        has_scarf = opponent_effective_speed > bot_effective_speed
    else:
        has_scarf = bot_effective_speed > opponent_effective_speed

    if has_scarf:
        logger.info(
            "Opponent {} could not have gone first - setting it's item to choicescarf".format(
                battle.opponent.active.name
            )
        )
        battle.opponent.active.item = "choicescarf"
        battle.opponent.active.item_inferred = True


_DAMAGE_CAP_MARKERS = ("focus sash", "ability: sturdy", "move: endure")


def _damage_was_capped(lines, defender_name):
    """True if a Focus Sash / Sturdy / Endure marker capped this hit at 1hp.

    PS clamps the damage it PRINTS to the target's remaining hp, so a capped
    hit's observed delta is only a LOWER bound on the roll that was computed.
    Treating it as the roll prunes exactly the strongest (true) candidate sets.
    """
    for line in lines:
        split_line = line.split("|")
        if len(split_line) < 2 or split_line[1] in MOVE_END_STRINGS:
            break
        lowered = line.lower()
        if defender_name in line and any(m in lowered for m in _DAMAGE_CAP_MARKERS):
            return True
    return False


def get_damage_dealt(battle, split_msg, next_messages):
    move_name = normalize_name(split_msg[3])

    if is_opponent(battle, split_msg):
        attacking_side = battle.opponent
        defending_side = battle.user
    else:
        attacking_side = battle.user
        defending_side = battle.opponent

    # item 1: the crit arm comes from the UNIFORM stamp, never from a private
    # re-scan.  This call is what makes the flags available at the |move| line
    # (the block's own `-crit` lines have not been dispatched yet at this
    # point), and it is idempotent with the dispatch-table handlers.
    stamp_hit_flags(battle, next_messages)
    critical_hit = bool(getattr(defending_side.active, "crit_this_turn", False))

    for line in next_messages:
        next_line_split = line.split("|")
        # if one of these strings appears in index 1 then
        # exit out since we are done with this pokemon's move
        if len(next_line_split) < 2 or next_line_split[1] in MOVE_END_STRINGS:
            break

        # if '-damage' appears, we want to parse the percentage damage dealt
        elif (
            next_line_split[1] == "-damage"
            and defending_side.name in next_line_split[2]
        ):
            final_health, maxhp, _ = get_pokemon_info_from_condition(next_line_split[3])
            lethal = maxhp == 0
            # maxhp can be 0 if the targetted pokemon fainted
            # the message would be: "0 fnt"
            if maxhp == 0:
                maxhp = defending_side.active.max_hp

            damage_dealt = (
                defending_side.active.hp / defending_side.active.max_hp
            ) * maxhp - final_health
            damage_percentage = round(damage_dealt / maxhp, 4)

            # OUR hp is request-exact on both ends, so the delta is an integer
            # FACT rather than a percentage estimate
            exact_damage = None
            if defending_side is battle.user:
                exact_damage = int(defending_side.active.hp) - int(final_health)

            logger.info(
                "{} did {}% damage to {} with {}".format(
                    attacking_side.active.name,
                    damage_percentage * 100,
                    defending_side.active.name,
                    move_name,
                )
            )
            return DamageDealt(
                attacker=attacking_side.active.name,
                defender=defending_side.active.name,
                move=move_name,
                percent_damage=damage_percentage,
                crit=critical_hit,
                exact_damage=exact_damage,
                lethal=lethal,
                capped=_damage_was_capped(next_messages, defending_side.name),
            )


def _use_exact_membership(damage_dealt, check_type):
    """Can this observation be tested as EXACT roll-set membership?

    Only when the opponent hit US (our hp is request-exact on both ends, so the
    delta is an integer) with a single-strike move. Multi-hit moves sum an
    unknown number of independent rolls and keep the old band test.
    """
    return (
        check_type == "damage_dealt"
        and getattr(damage_dealt, "exact_damage", None) is not None
        and damage_dealt.move in all_move_json
        and "multihit" not in all_move_json[damage_dealt.move]
    )


def _refuse(check_type, move, reason, detail=""):
    """Record a damage check we declined to act on instead of silently dropping it.

    A refusal is not a neutral event: a channel that refuses constantly is
    producing false eliminations that the would-empty guard is absorbing. The
    counter (modelled on `hp_certificate.CERTIFICATE_REFUSALS`, but read from
    logs rather than a sweep dump) makes one ladder run enough to see WHICH
    channel is wrong.
    """
    DAMAGE_CHECK_REFUSALS[(check_type, move, reason)] += 1
    logger.warning(
        "Refusing damage check: check_type=%s move=%s reason=%s %s",
        check_type,
        move,
        reason,
        detail,
    )


def _do_check(
    battle,
    battle_copy,
    possibilites,
    check_type,
    damage_dealt,
    bot_went_first,
    check_lower_bound,
    allow_emptying=False,
):
    actual_damage_dealt = damage_dealt.percent_damage * battle_copy.user.active.max_hp
    use_membership = _use_exact_membership(damage_dealt, check_type)
    exact_damage = getattr(damage_dealt, "exact_damage", None)

    indicies_to_remove = []
    candidate_ranges = []
    num_starting_possibilites = len(possibilites)
    for i in range(num_starting_possibilites):
        p = possibilites[i]
        if isinstance(p, PredictedPokemonSet):
            p = p.pkmn_set


        if not battle.opponent.active.ability:
            battle_copy.opponent.active.ability = p.ability
        if battle.opponent.active.item == constants.UNKNOWN_ITEM:
            battle_copy.opponent.active.item = p.item
        battle_copy.opponent.active.set_spread(
            p.nature, ",".join(str(x) for x in p.evs)
        )

        rolls = None
        if use_membership:
            _, opponent_roll_sets = poke_engine_get_damage_roll_sets(
                battle_copy,
                battle_copy.user.last_selected_move.move,
                damage_dealt.move,
                bot_went_first,
            )
            if opponent_roll_sets:
                rolls = list(
                    opponent_roll_sets[1]
                    if damage_dealt.crit
                    else opponent_roll_sets[0]
                )

        if rolls:
            # EXACT: the delta must be one of the 16 rolls the engine
            # computes for this candidate (+-1hp for PS rounding). This is
            # the distinction the band test could not make: a Choice Band
            # roll set and an item-less one overlap inside +-10hp.
            max_roll = max(rolls)
            if not check_lower_bound:
                # a lethal / Sash-capped hit only proves the roll was AT LEAST
                # the observed delta
                is_invalid = exact_damage > max_roll + 1
            else:
                is_invalid = not any(abs(r - exact_damage) <= 1 for r in rolls)
            candidate_ranges.append((p, min(rolls), max_roll))
            if is_invalid:
                logger.debug(
                    "{} is invalid: exact damage {} is not in roll set [{}, {}]".format(
                        p, exact_damage, min(rolls), max_roll
                    )
                )
                indicies_to_remove.append(i)
            continue

        if check_type == "damage_received":
            actual_damage_dealt = (
                damage_dealt.percent_damage * battle_copy.opponent.active.max_hp
            )

            if bot_went_first:
                opponent_move = constants.DO_NOTHING_MOVE
            else:
                opponent_move = battle_copy.opponent.last_used_move.move

            damage, _ = poke_engine_get_damage_rolls(
                battle_copy, damage_dealt.move, opponent_move, bot_went_first
            )
        elif check_type == "damage_dealt":
            _, damage = poke_engine_get_damage_rolls(
                battle_copy,
                battle_copy.user.last_selected_move.move,
                damage_dealt.move,
                bot_went_first,
            )
        else:
            raise ValueError("Invalid check_type: {}".format(check_type))

        if damage_dealt.crit:
            max_damage = damage[1]
        else:
            max_damage = damage[0]

        damage = [max_damage * 0.85, max_damage]
        candidate_ranges.append((p, damage[0], damage[1]))
        lower_bound_violated = check_lower_bound and (
            actual_damage_dealt < (damage[0] * 0.975 - 5)
        )
        upper_bound_violated = actual_damage_dealt > (damage[1] * 1.025 + 5)
        if lower_bound_violated or upper_bound_violated:
            logger.debug(
                "{} is invalid based on reverse damage calc. damage_dealt={}, lower={}, upper={}".format(
                    p, actual_damage_dealt, damage[0], damage[1]
                )
            )
            indicies_to_remove.append(i)

    if len(indicies_to_remove) == num_starting_possibilites and not allow_emptying:
        _refuse(
            check_type,
            damage_dealt.move,
            "would_empty",
            "observed={} exact={} ranges={}".format(
                actual_damage_dealt, exact_damage, candidate_ranges
            ),
        )
        return

    # PERSIST the verdict. `possibilites` here is a FRESH list built by
    # `get_pkmn_sets_from_pkmn_name` (data/pkmn_sets.py `ret = []`), so the
    # pops below are thrown away the moment this function returns - the whole
    # reverse-damage pipeline bought nothing. The signature ledger on the
    # pokemon is the durable channel, and it is already fully plumbed on the
    # consumer side (`full_set_pkmn_can_have_set`).
    #
    # Gated on `not exact_roster_known` like every other live-only heuristic.
    # The replay checker also stubs out `update_dataset_possibilities` (see
    # fp/replay/checker.py), so today this ledger is unreachable there anyway --
    # but that stub is an unrelated crash workaround, and relying on it to keep
    # a live-only inference out of the checker is a silent coupling. This gate
    # is the load-bearing one; it survives the stub being removed.
    if (
        battle.battle_type == BattleType.RANDOM_BATTLE
        and not getattr(battle, "exact_roster_known", False)
        and hasattr(battle.opponent.active, "rejected_set_signatures")
    ):
        for i in indicies_to_remove:
            element = possibilites[i]
            if isinstance(element, PredictedPokemonSet):
                battle.opponent.active.rejected_set_signatures.add(
                    element.mechanics_signature()
                )

    for i in reversed(indicies_to_remove):
        possibilites.pop(i)


def _candidate_set_max_hp(pkmn, predicted_set):
    pkmn_set = predicted_set.pkmn_set
    stats = calculate_stats(
        pkmn.base_stats,
        pkmn_set.level if pkmn_set.level is not None else pkmn.level,
        ivs=pkmn_set.ivs,
        evs=pkmn_set.evs,
        nature=pkmn_set.nature,
    )
    return int(stats[constants.HITPOINTS])


def sieve_sets_by_max_hp(battle, pkmn, max_hp_is_admissible, reason):
    """Items 3/4 consumer: drop candidate sets whose max HP this observation
    rules out.

    Every candidate set fixes max HP deterministically (level + HP EVs/IVs), so
    a statement about max HP is a direct filter on the candidate list. Applying
    each observation EXACTLY at the moment it happens - rather than folding it
    into a running band first - is both lossless (the admissible max HPs are a
    lattice, not an interval) and free: the intersection across events
    accumulates in `rejected_set_signatures`, the ledger the wave-1 damage
    channel already uses, which carries the would-empty guard, the relaxation
    ladder and the "clear the ledger rather than be worse off than no evidence"
    backstop.

    Live-only, behind the same gates as every other live inference: the replay
    checker knows the true roster and must not have its candidate list touched.
    """
    if (
        battle.battle_type != BattleType.RANDOM_BATTLE
        or getattr(battle, "exact_roster_known", False)
        or not hasattr(pkmn, "rejected_set_signatures")
        or getattr(pkmn, "max_hp_exact", False)
    ):
        return

    candidates = RandomBattleTeamDatasets.get_pkmn_sets_from_pkmn_name(pkmn)
    if not candidates:
        return
    doomed = []
    for candidate in candidates:
        try:
            candidate_max_hp = _candidate_set_max_hp(pkmn, candidate)
        except Exception:
            continue
        if not max_hp_is_admissible(candidate_max_hp):
            doomed.append(candidate)
    if not doomed:
        return
    if len(doomed) == len(candidates):
        # the observation contradicts every set the dataset knows: its PREMISE
        # is wrong (a residual we mis-attributed, a modifier we do not model),
        # not the dataset. Drop the observation (wave-1 discipline).
        _refuse("max_hp", reason, "would_empty", pkmn.name)
        return
    for candidate in doomed:
        pkmn.rejected_set_signatures.add(candidate.mechanics_signature())
    logger.info(
        "max-hp sieve (%s) eliminated %d/%d candidate sets for %s",
        reason,
        len(doomed),
        len(candidates),
        pkmn.name,
    )


def _block_is_confounded(battle, preceding_lines):
    """True if something earlier in this block invalidates the roll computation.

    The reverse damage calc rebuilds the state as it stands AFTER the whole
    block, so an opponent boost or item consumption that resolved BEFORE the
    checked move was not in effect when the damage was rolled. Refusing beats
    eliminating the true set on a premise the model cannot represent.
    """
    for line in preceding_lines or ():
        split_line = line.split("|")
        if len(split_line) < 3:
            continue
        if split_line[1] in ("-boost", "-unboost", "-enditem") and split_line[
            2
        ].startswith(battle.opponent.name):
            return True
    return False


def update_dataset_possibilities(
    battle,
    damage_dealt,
    check_type,
    preceding_lines=(),
):
    if (
        battle.wait
        or battle.generation in {"gen1", "gen2"}
        or battle.opponent.active is None
        or battle.opponent.active.hp <= 0
        or battle.opponent.active.name
        in ["ditto", "shedinja", "terapagosterastal", "meloetta", "meloettapirouette"]
        or battle.user.active.name
        in ["ditto", "shedinja", "terapagosterastal", "meloetta", "meloettapirouette"]
        or damage_dealt.move not in all_move_json
        or all_move_json[damage_dealt.move][constants.CATEGORY] == constants.STATUS
        or "multiaccuracy" in all_move_json[damage_dealt.move]
        or damage_dealt.move.startswith(constants.HIDDEN_POWER)
        or damage_dealt.percent_damage == 0
        or (
            check_type == "damage_dealt"
            and battle.opponent.last_used_move.move != damage_dealt.move
        )
        or (
            check_type == "damage_received"
            and battle.user.last_used_move.move != damage_dealt.move
        )
        or damage_dealt.move
        in [
            "pursuit",
            "struggle",
            "counter",
            "mirrorcoat",
            "metalburst",
            "foulplay",
            "meteorbeam",
            "electroshot",
            "ficklebeam",
            "lashout",
            "ragefist",
            "shellsidearm",
            "futuresight",
        ]
    ):
        return

    if _block_is_confounded(battle, preceding_lines):
        _refuse(check_type, damage_dealt.move, "confound")
        return

    battle_copy = deepcopy(battle)

    if battle.battle_type == BattleType.RANDOM_BATTLE:
        possibilites = RandomBattleTeamDatasets.get_pkmn_sets_from_pkmn_name(
            battle.opponent.active
        )
        smogon_possibilities = None
        allow_emptying = False
    elif battle.battle_type == BattleType.BATTLE_FACTORY:
        possibilites = TeamDatasets.get_pkmn_sets_from_pkmn_name(battle.opponent.active)
        smogon_possibilities = None
        allow_emptying = False
    else:
        possibilites = TeamDatasets.get_pkmn_sets_from_pkmn_name(battle.opponent.active)
        smogon_possibilities = SmogonSets.get_pkmn_sets_from_pkmn_name(
            battle.opponent.active
        )
        allow_emptying = True

    # The lower bound is only unusable when the observation was CLAMPED: PS
    # prints damage capped at the target's remaining hp, so a KO ('0 fnt') or a
    # Focus Sash / Sturdy / Endure survival reports less than the roll that was
    # computed. The old proxy for this ("the damage happens to be within 2% of
    # the target's hp") was both too loose - it silently discarded the lower
    # bound on any near-full-hp reading, throwing away pruning power - and too
    # tight: it did not recognise a capped hit at all, so a capped observation
    # pruned the TRUE (strongest) set, which is exactly what empties the list.
    check_lower_bound = not (
        getattr(damage_dealt, "lethal", False) or getattr(damage_dealt, "capped", False)
    )
    if check_type == "damage_dealt":
        bot_went_first = (
            battle.user.last_used_move.turn == battle.opponent.last_used_move.turn
        )
    elif check_type == "damage_received":
        bot_went_first = (
            battle.opponent.last_used_move.turn != battle.user.last_used_move.turn
        )
    else:
        raise ValueError("Invalid check_type: {}".format(check_type))

    logger.debug(f"{check_type=}")
    logger.debug(f"{check_lower_bound=}")
    logger.debug(f"{bot_went_first=}")

    _do_check(
        battle,
        battle_copy,
        possibilites,
        check_type,
        damage_dealt,
        bot_went_first,
        check_lower_bound,
        allow_emptying=allow_emptying,
    )

    if smogon_possibilities is not None:
        _do_check(
            battle,
            battle_copy,
            smogon_possibilities,
            check_type,
            damage_dealt,
            bot_went_first,
            check_lower_bound,
            allow_emptying=False,  # never completely empty smogon stats
        )


def check_heavydutyboots(battle, msg_lines):
    side_to_check = battle.opponent

    if (
        battle.generation not in ["gen8", "gen9"]
        or side_to_check.active.item != constants.UNKNOWN_ITEM
        or "magicguard"
        in [
            normalize_name(a)
            for a in pokedex[side_to_check.active.name][constants.ABILITIES].values()
        ]
    ):
        return

    if side_to_check.side_conditions[constants.STEALTH_ROCK] > 0:
        pkmn_took_stealthrock_damage = False
        for line in msg_lines:
            split_line = line.split("|")

            # |-damage|p2a: Weedle|88/100|[from] Stealth Rock
            if (
                len(split_line) > 4
                and split_line[1] == "-damage"
                and split_line[2].startswith(side_to_check.name)
                and split_line[4] == "[from] Stealth Rock"
            ):
                pkmn_took_stealthrock_damage = True

        if not pkmn_took_stealthrock_damage:
            logger.info("{} has heavydutyboots".format(side_to_check.active.name))
            side_to_check.active.item = "heavydutyboots"
            side_to_check.active.item_inferred = True
        else:
            logger.info(
                "{} was affected by stealthrock, it cannot have heavydutyboots".format(
                    side_to_check.active.name
                )
            )
            side_to_check.active.impossible_items.add(constants.HEAVY_DUTY_BOOTS)

    elif (
        side_to_check.side_conditions[constants.SPIKES] > 0
        and "levitate"
        not in [
            normalize_name(a)
            for a in pokedex[side_to_check.active.name][constants.ABILITIES].values()
        ]
        and not side_to_check.active.has_type("flying")
        and side_to_check.active.ability != "levitate"
    ):
        pkmn_took_spikes_damage = False
        for line in msg_lines:
            split_line = line.split("|")

            # |-damage|p2a: Weedle|88/100|[from] Spikes
            if (
                len(split_line) > 4
                and split_line[1] == "-damage"
                and split_line[2].startswith(side_to_check.name)
                and split_line[4] == "[from] Spikes"
            ):
                pkmn_took_spikes_damage = True

        if not pkmn_took_spikes_damage:
            logger.info("{} has heavydutyboots".format(side_to_check.active.name))
            side_to_check.active.item = "heavydutyboots"
            side_to_check.active.item_inferred = True
        else:
            logger.info(
                "{} was affected by spikes, it cannot have heavydutyboots".format(
                    side_to_check.active.name
                )
            )
            side_to_check.active.impossible_items.add(constants.HEAVY_DUTY_BOOTS)
    elif (
        side_to_check.side_conditions[constants.TOXIC_SPIKES] > 0
        and side_to_check.active.status is None
        and not side_to_check.active.has_type("flying")
        and not side_to_check.active.has_type("poison")
        and not side_to_check.active.has_type("steel")
        and side_to_check.active.ability != "levitate"
        and "levitate"
        not in [
            normalize_name(a)
            for a in pokedex[side_to_check.active.name][constants.ABILITIES].values()
        ]
        and side_to_check.active.ability not in constants.IMMUNE_TO_POISON_ABILITIES
    ):
        pkmn_took_toxicspikes_poison = False
        for line in msg_lines:
            split_line = line.split("|")

            # a pokemon can be toxic-ed from sources other than toxicspikes
            # stopping at one of these strings ensures those other sources aren't considered
            if len(split_line) < 2 or split_line[1] in {"move", "upkeep", ""}:
                break

            # |-status|p2a: Pikachu|psn
            if (
                split_line[1] == "-status"
                and (
                    split_line[3] == constants.POISON
                    or split_line[3] == constants.TOXIC
                )
                and split_line[2].startswith(side_to_check.name)
            ):
                pkmn_took_toxicspikes_poison = True

        if not pkmn_took_toxicspikes_poison:
            logger.info("{} has heavydutyboots".format(side_to_check.active.name))
            side_to_check.active.item = "heavydutyboots"
            side_to_check.active.item_inferred = True
        else:
            logger.info(
                "{} was affected by toxicspikes, it cannot have heavydutyboots".format(
                    side_to_check.active.name
                )
            )
            side_to_check.active.impossible_items.add(constants.HEAVY_DUTY_BOOTS)

    elif (
        side_to_check.side_conditions[constants.STICKY_WEB] > 0
        and not side_to_check.active.has_type("flying")
        and "levitate"
        not in [
            normalize_name(a)
            for a in pokedex[side_to_check.active.name][constants.ABILITIES].values()
        ]
    ):
        pkmn_was_affected_by_stickyweb = False
        for line in msg_lines:
            split_line = line.split("|")

            # |-activate|p2a: Gengar|move: Sticky Web
            if (
                len(split_line) == 4
                and split_line[1] == "-activate"
                and split_line[2].startswith(side_to_check.name)
                and split_line[3] == "move: Sticky Web"
            ):
                pkmn_was_affected_by_stickyweb = True

        if not pkmn_was_affected_by_stickyweb:
            logger.info("{} has heavydutyboots".format(side_to_check.active.name))
            side_to_check.active.item = "heavydutyboots"
            side_to_check.active.item_inferred = True
        else:
            logger.debug(
                "{} was affected by sticky web, it cannot have heavydutyboots".format(
                    side_to_check.active.name
                )
            )
            side_to_check.active.impossible_items.add(constants.HEAVY_DUTY_BOOTS)
