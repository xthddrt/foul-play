from collections import defaultdict
from collections import namedtuple

import constants
import logging
import os

from config import FoulPlayConfig
from data import all_move_json
from data import pokedex

from fp.helpers import (
    get_pokemon_info_from_condition,
    possible_hidden_power_types,
    random_battles_evs,
)
from fp.helpers import normalize_name
from fp.helpers import calculate_stats
from fp import hp_certificate


logger = logging.getLogger(__name__)


def base_species_name(name):
    """Forme-family key: the pokedex `baseSpecies` when the entry names one
    (terapagosterastal -> terapagos, mimikyubusted -> mimikyu), else the species
    itself.  Used to recognise two tracked objects as the same physical mon."""
    if not name:
        return name
    base = pokedex.get(name, {}).get("baseSpecies")
    return normalize_name(base) if base else name


LastUsedMove = namedtuple("LastUsedMove", ["pokemon_name", "move", "turn"])
DamageDealt = namedtuple(
    "DamageDealt",
    [
        "attacker",
        "defender",
        "move",
        "percent_damage",
        "crit",
        # EXACT integer hp delta, only ever set when the defender is OUR side
        # (the request json makes our hp exact, so `hp_before - final_health`
        # is an integer fact rather than a percentage estimate). None means
        # "no exact reading available".
        "exact_damage",
        # the damage line read '0 fnt': PS clamps damage to remaining hp, so
        # the observation is a LOWER bound on the true roll, not the roll
        "lethal",
        # a Focus Sash / Sturdy / Endure marker capped the hit at 1hp, same
        # weakening of the observation as `lethal`
        "capped",
    ],
    defaults=(None, False, False),
)
StatRange = namedtuple("Range", ["min", "max"])


# Based on the format, this dict controls which pokemon will be replaced during team preview
# Some pokemon's forms are not revealed in team preview
smart_team_preview = {
    "gen8ou": {
        "urshifu": "urshifurapidstrike"  # urshifu banned in gen8ou
    },
    "gen9ou": {
        "urshifu": "urshifurapidstrike"  # urshifu banned in gen9ou
    },
    "gen9nationaldex": {
        "urshifu": "urshifurapidstrike"  # urshifu banned in gen9nationaldex
    },
    "gen9battlefactory": {
        "zacian": "zaciancrowned"  # only zaciancrowned is used in gen9battlefactory
    },
}


boost_multiplier_lookup = {
    -6: 2 / 8,
    -5: 2 / 7,
    -4: 2 / 6,
    -3: 2 / 5,
    -2: 2 / 4,
    -1: 2 / 3,
    0: 2 / 2,
    1: 3 / 2,
    2: 4 / 2,
    3: 5 / 2,
    4: 6 / 2,
    5: 7 / 2,
    6: 8 / 2,
}


class SharedByReference:
    """A holder `deepcopy` ALIASES instead of copying.

    `Battle` objects are deep-copied per turn (the replay checker snapshots one
    for every armed turn, and several derivations copy one per event), so
    hanging a large read-only structure -- the replay checker's full-knowledge
    teams sidecar -- straight off the battle would multiply it by every copy.
    Wrapping it here keeps one instance for the whole log; the wrapper is
    read-only by convention and must never be mutated through a copy.
    """

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value

    def __deepcopy__(self, memo):
        return self

    def __copy__(self):
        return self


class Battle:
    def __init__(self, battle_tag):
        self.battle_tag = battle_tag
        self.user = Battler()
        self.opponent = Battler()
        self.weather = None
        self.weather_turns_remaining = -1
        self.weather_source = ""
        self.field = None
        self.field_turns_remaining = 0
        self.trick_room = False
        self.trick_room_turns_remaining = 0
        self.gravity = False
        self.team_preview = False

        self.turn = False

        self.started = False
        self.rqid = None

        self.force_switch = False
        self.wait = False

        self.battle_type = None
        self.decisions_made = 0
        self.pokemon_format = ""
        self.generation = None
        self.time_remaining = None

        self.request_json = None
        self.msg_list = []
        # `SharedByReference` around the replay checker's full-knowledge teams
        # sidecar, or None off-corpus.  Read by anything that must DERIVE with
        # the exact sets rather than with live-tracking guesses (see
        # fp.battle_modifier._exact_team_copy).
        self.exact_teams = None

    def initialize_team_preview(self, opponent_pokemon, battle_type):
        self.user.reserve.insert(0, self.user.active)
        self.user.active = None

        for pkmn_string in opponent_pokemon:
            pokemon = Pokemon.from_switch_string(pkmn_string)

            if pokemon.name in smart_team_preview.get(battle_type, {}):
                new_pokemon_name = smart_team_preview[battle_type][pokemon.name]
                logger.info(
                    "Smart team preview: Replaced {} with {}".format(
                        pokemon.name, new_pokemon_name
                    )
                )
                pokemon = Pokemon(new_pokemon_name, pokemon.level)

            elif pkmn_string.endswith("-*"):
                pokemon.unknown_forme = True

            self.opponent.reserve.append(pokemon)

        # team preview reveals every pkmn's species to both players
        for pkmn in self.user.reserve + self.opponent.reserve:
            pkmn.revealed = True

        self.started = True

    def during_team_preview(self): ...

    def start_non_team_preview_battle(self, user_json, opponent_switch_string):
        self.user.initialize_first_turn_user_from_json(user_json)

        pkmn_information = opponent_switch_string.split("|")[3]
        pkmn = Pokemon.from_switch_string(pkmn_information)
        pkmn.revealed = True
        self.opponent.active = pkmn

        self.started = True
        self.rqid = user_json[constants.RQID]

    def mega_evolve_possible(self):
        return (
            any(g in self.generation for g in constants.MEGA_EVOLVE_GENERATIONS)
            or "nationaldex" in self.pokemon_format
            or "champions" in self.pokemon_format
        )

    def get_effective_speed(self, battler):
        boosted_speed = battler.active.calculate_boosted_stats()[constants.SPEED]

        if self.weather == constants.SUN and battler.active.ability == "chlorophyll":
            boosted_speed *= 2
        elif self.weather == constants.RAIN and battler.active.ability == "swiftswim":
            boosted_speed *= 2
        elif self.weather == constants.SAND and battler.active.ability == "sandrush":
            boosted_speed *= 2
        elif (
            self.weather in constants.HAIL_OR_SNOW
            and battler.active.ability == "slushrush"
        ):
            boosted_speed *= 2

        if (
            self.field == constants.ELECTRIC_TERRAIN
            and battler.active.ability == "surgesurfer"
        ):
            boosted_speed *= 2

        if battler.active.ability == "unburden" and not battler.active.item:
            boosted_speed *= 2
        elif (
            battler.active.ability == "quickfeet" and battler.active.status is not None
        ):
            boosted_speed *= 1.5

        if battler.side_conditions[constants.TAILWIND]:
            boosted_speed *= 2

        if "choicescarf" == battler.active.item:
            boosted_speed *= 1.5

        if (
            constants.PARALYZED == battler.active.status
            and battler.active.ability != "quickfeet"
        ):
            boosted_speed *= 0.5

        if any(
            vs in battler.active.volatile_statuses
            for vs in ["quarkdrivespe", "protosynthesisspe"]
        ):
            boosted_speed *= 1.5

        return int(boosted_speed)


class Battler:
    def __init__(self):
        self.active = None
        self.reserve = []
        self.side_conditions = defaultdict(lambda: 0)

        self.name = None
        self.trapped = False
        self.baton_passing = False
        self.shed_tailing = False
        self.wish = (0, 0)
        self.future_sight = (0, "")

        # {screen name: name of the pokemon that set it}. Reflect / Light
        # Screen / Aurora Veil run 5 turns, or 8 with Light Clay, so how long
        # the screen actually lasts is evidence about the SETTER's item -- and
        # the setter may well be off the field by the time the screen ends.
        self.screen_setters = {}

        # True once this side's terastallization has been consumed by a pkmn
        # that later FAINTED. PS deletes `terastallized` on faint
        # (sim/battle.ts:2565) and the faint handler mirrors that on the
        # pokemon, but the tera itself stays spent for the battle - the engine
        # derives can_use_tera() from "any party pkmn terastallized"
        # (poke-engine genx/state.rs:980-987), so conversion re-flags a fainted
        # slot when this is set (see poke_engine_helpers._padded_party)
        self.tera_spent = False

        # Cumulative count of Revival Blessing revives on this side, never reset.
        # Bridges this side's `num_fainted_pkmn()` (currently fainted) to PS's
        # `side.totalFainted` (cumulative, never decremented by a revive -
        # sim/battle.ts:2551): totalFainted == num_fainted_pkmn() + times_revived.
        # Incremented in battle_modifier.heal_or_damage and forwarded to the engine
        # by poke_engine_helpers, which is what Supreme Overlord / Last Respects read.
        self.times_revived = 0

        # PS `statsRaisedThisTurn` on this side's active (sim/battle.ts:2082, set on any
        # positive boost from any source). Cleared at the turn boundary and on switch-out,
        # EXCEPT on turn 1 -- PS's `nextTurn` reset is guarded by `if (this.turn !== 1)`
        # (sim/battle.ts:1673-1675), so a switch-in ability boost applied during `|start|`
        # (Download / Intrepid Sword / Dauntless Shield) is still "raised this turn" when
        # turn 1's moves resolve. Forwarded to the engine by poke_engine_helpers; it is
        # what Alluring Voice / Burning Jealousy's 100% confuse/burn secondary reads.
        self.stats_raised_this_turn = False

        self.account_name = None

        self.team_dict = None

        # last_selected_move: The last move that was selected (Bot only)
        # last_used_move: The last move that was observed publicly (Bot and Opponent)
        # they may seem the same, but `last_selected_move` is important in situations where the bot selects
        #   a move but gets knocked out before it can use it
        self.last_selected_move = LastUsedMove("", "", 0)
        self.last_used_move = LastUsedMove("", "", 0)

    def possible_mega_evolutions(self):
        result = {}
        for pkmn in self.reserve + [self.active]:
            megas_possible = pkmn.get_mega_pkmn_info()
            for m in megas_possible:
                if pkmn.hp and (
                    pkmn.item == constants.UNKNOWN_ITEM or pkmn.item == m[1]
                ):
                    if pkmn.name not in result:
                        result[pkmn.name] = []
                    result[pkmn.name].append(m)
        return result

    def num_fainted_pkmn(self):
        num_fainted = 0
        for pkmn in self.reserve + [self.active]:
            if not pkmn.is_alive():
                num_fainted += 1

        return num_fainted

    def mega_revealed(self):
        return self.active.is_mega or any(p.is_mega for p in self.reserve)

    def arm_wish(self, healed_hp):
        """Mirror of `Side#addSlotCondition` for Wish (sim/side.ts:462-492).

        Wish is a SLOT condition, and `addSlotCondition` returns false when the
        slot already holds it and the condition has no `onRestart` -- Wish has
        none (data/moves.ts:20909-20945) -- so a second Wish while one is
        pending FAILS and must not re-arm or refresh anything.  Returns whether
        the wish was armed.

        The pending wish is `self.wish[0] > 0`.  `upkeep` decrements it, so it
        is 1 throughout the turn AFTER the wish was used (during which PS's
        `onResidual` still has not removed the slot condition, so a fresh Wish
        still fails) and 0 from the turn after that (PS's `onResidual` removed
        it at the previous turn's residual, so a fresh Wish succeeds).

        This is the arming half of the lifecycle.  The clearing half must not be
        gated on an observable heal: `onEnd` calls `this.heal(...)` and only adds
        the `|-heal|` line `if (damage)`, and `Battle#heal` returns false for a
        target already at full HP (sim/battle.ts:2272 `if (target.hp >=
        target.maxhp) return false;`), so a wish that resolves onto a full-HP mon
        leaves NO protocol line while the slot condition is removed regardless.
        The unconditional `upkeep` decrement is what implements that, and nothing
        may make it conditional on a `-heal`.
        """
        if os.environ.get("FP_CONTROL_WISH_REARM_ON_FAIL"):
            # CONTROL ONLY, never set in a measurement run: restores the pre-fix
            # "every |move| Wish arms a wish" behaviour so the fix can be shown
            # capable of failing.
            self.wish = (2, healed_hp)
            return True
        if self.wish[0] > 0:
            logger.info(
                "Wish failed: this side already has a wish pending ({})".format(
                    self.wish
                )
            )
            return False
        self.wish = (2, healed_hp)
        return True

    def find_pokemon_in_reserves(self, pkmn_name):
        for reserve_pkmn in self.reserve:
            if reserve_pkmn.name == pkmn_name or reserve_pkmn.base_name == pkmn_name:
                return reserve_pkmn
            if pkmn_name in [
                normalize_name(n)
                for n in pokedex[reserve_pkmn.name].get("otherFormes", [])
            ]:
                return reserve_pkmn

        # PROTOCOL-DERIVED IDENTITY (opponent-side forme-duplicate prevention).
        #
        # The forme-duplicate prune in `Battler._drop_unclaimed_forme_
        # duplicates` is REQUEST-DRIVEN and therefore covers the bot's side
        # only; the opponent has no request, so an opponent mon that re-enters
        # under a forme name none of the three lookups above recognise gets a
        # SECOND object built for it and the party grows to seven.  synth15853:
        # Shaymin enters as `Shaymin-Sky`, is frozen and permanently reverts to
        # `Shaymin` (PS data/conditions.ts:92-93 -> formeChange('Shaymin',
        # effect, /*isPermanent*/ true)), switches out, and re-enters as
        # `Shaymin`.  Neither `name`/`base_name` nor `otherFormes` of the
        # tracked forme matches, so a duplicate Shaymin was created and the
        # binding raised `OverlongPartyError` for the rest of the game
        # (24 raises, 20 turns of coverage lost).
        #
        # `forme_lineage` is the set of species this object has actually been
        # NAMED BY in the protocol -- entry species plus every |detailschange| /
        # |-formechange| / switch-out revert.  It is an observation, not an
        # inference from the pokedex, so matching on it cannot invent an
        # identity the log never asserted.
        #
        # Conservative on purpose (REFUSE-DON'T-GUESS): the match must be
        # UNIQUE.  If two reserve rows have worn the same name the identity is
        # genuinely ambiguous and this returns None, which lets the over-length
        # party reach the binding and be REFUSED loudly.  The refusal is
        # correct and is deliberately left in place -- coverage lost to an
        # honest refusal is recoverable, a silent wrong answer is not.
        if not os.environ.get("FP_CONTROL_NO_FORME_LINEAGE_MATCH"):
            lineage_matches = [
                p
                for p in self.reserve
                if pkmn_name in getattr(p, "forme_lineage", ())
            ]
            if len(lineage_matches) == 1:
                logger.info(
                    "Matched incoming {} to tracked {} by forme lineage {}".format(
                        pkmn_name,
                        lineage_matches[0].name,
                        sorted(lineage_matches[0].forme_lineage),
                    )
                )
                return lineage_matches[0]
        return None

    def find_reserve_pkmn_by_unknown_forme(self, pkmn_name):
        for reserve_pkmn in filter(lambda x: x.unknown_forme, self.reserve):
            pkmn_base_forme = normalize_name(pokedex[pkmn_name].get("changesFrom", ""))
            if pkmn_base_forme == reserve_pkmn.base_name:
                return reserve_pkmn
        return None

    def find_reserve_pokemon_by_nickname(self, pkmn_nickname):
        for reserve_pkmn in self.reserve:
            if pkmn_nickname == reserve_pkmn.nickname:
                return reserve_pkmn
        return None

    def lock_active_pkmn_first_turn_moves(self):
        # disable firstimpression and fakeout if the last_used_move was not a switch
        if self.last_used_move.pokemon_name == self.active.name:
            for m in self.active.moves:
                if m.name in constants.FIRST_TURN_MOVES:
                    m.disabled = True

    def lock_active_pkmn_status_moves_if_active_has_assaultvest(self):
        if self.active.item == "assaultvest":
            for m in self.active.moves:
                if all_move_json[m.name][constants.CATEGORY] == constants.STATUS:
                    m.disabled = True

    def choice_lock_moves(self):
        # if the active pokemon has a choice item and their last used move was by this pokemon -> lock their other moves
        #
        # known bug: the bot tricking a choice item onto the opponent will lock their move when it shouldn't.
        # this will only happen if the bot tricks the opponent after the opponent has already used a move
        if (
            self.active.item in constants.CHOICE_ITEMS
            and self.last_used_move.pokemon_name == self.active.name
            and self.last_used_move.move not in ["trick", "switcheroo"]
        ):
            for m in self.active.moves:
                if (
                    self.last_used_move.move == constants.HIDDEN_POWER
                    and m.name.startswith(constants.HIDDEN_POWER)
                ):
                    pass
                elif m.name != self.last_used_move.move:
                    m.disabled = True

    def taunt_lock_moves(self):
        if constants.TAUNT in self.active.volatile_statuses:
            for m in self.active.moves:
                if all_move_json[m.name][constants.CATEGORY] == constants.STATUS:
                    m.disabled = True

    def locked_move_lock(self):
        if constants.LOCKED_MOVE in self.active.volatile_statuses:
            for m in self.active.moves:
                if m.name != self.last_used_move.move:
                    m.disabled = True

    def lock_moves(self):
        self.choice_lock_moves()
        self.lock_active_pkmn_status_moves_if_active_has_assaultvest()
        self.lock_active_pkmn_first_turn_moves()
        self.taunt_lock_moves()
        self.locked_move_lock()

    def _initialize_user_active_from_request_json(self, request_json):
        self.active.can_mega_evo = request_json[constants.ACTIVE][0].get(
            constants.CAN_MEGA_EVO, False
        )
        self.active.can_ultra_burst = request_json[constants.ACTIVE][0].get(
            constants.CAN_ULTRA_BURST, False
        )
        self.active.can_dynamax = request_json[constants.ACTIVE][0].get(
            constants.CAN_DYNAMAX, False
        )
        self.active.can_terastallize = request_json[constants.ACTIVE][0].get(
            constants.CAN_TERASTALLIZE, False
        )

        # A transformed pkmn (Ditto/Mew) used to early-return here on the
        # premise that "nothing in the request is authoritative while
        # transformed".  THAT PREMISE IS FALSE and the logs falsify it:
        # `getMoveRequestData` -> `getMoves()` iterates `this.moveSlots`
        # (sim/pokemon.ts:993-1046), and `transformInto` REPLACED moveSlots with
        # the copied set at `pp = Math.min(5, move.pp)`, `maxpp = pp` for gen>=5
        # (sim/pokemon.ts:1364-1379).  So the request's ACTIVE block is the
        # copied moveset carrying the copied, live PP -- measured over 20,000
        # corpus logs: 1,338 requests taken while the user's active was
        # transformed, 1,333 with every maxpp == 5 and the 5 exceptions all
        # Revival Blessing at maxpp 1, which is the same `min(5, pp)` rule.
        # (The side.pokemon entry agrees: `getSwitchRequestData` sends
        # `this.moves`, i.e. moveSlots, not baseMoves, whenever `forAlly` is
        # falsy -- sim/pokemon.ts:1172 with the getter at :512.)
        #
        # Skipping the refresh left a move first revealed AFTER the transform
        # carrying its base-species PP object: synth09899's Ditto reached T19
        # with Psyshock at 16-4=12 instead of the request's 1, so the engine
        # never ran Encore's "encored move hit 0 PP" termination and the
        # observed `|-end|p1a: Ditto|Encore` was in no branch.
        #
        # What IS genuinely non-authoritative while transformed is the SIDE
        # entry's `stats`: those are `baseStoredStats` (sim/pokemon.ts:1165-1171),
        # the UNtransformed values.  That guard lives in
        # `update_from_request_json` below and is deliberately left in place.
        if os.environ.get("FP_CONTROL_TRANSFORMED_ACTIVE_SKIP") and (
            constants.TRANSFORM in self.active.volatile_statuses
            or getattr(self.active, "transformed_into", None)
        ):
            # CONTROL ONLY, never set in a measurement run: restores the pre-fix
            # early return so the fix can be shown capable of failing
            # (tests/test_transformed_active_request_refresh.py asserts this
            # path DOES reproduce the stale base-species PP).
            return

        request_moves = request_json[constants.ACTIVE][0][constants.MOVES]

        # A forced/locked request is NOT a description of the pokemon's moveset
        # - it is a description of the single legal action. PS builds it in
        # sim/pokemon.ts:970-990 `getMoves(lockedMove)`, which short-circuits
        # before the normal loop and returns just
        #     [{ move: moveSlot.move, id: moveSlot.id }]
        # with NO pp/maxpp/target/disabled keys (the normal branch at :1043-1050
        # always sends pp+maxpp). getMoveRequestData reaches that branch
        # whenever getLockedMove() is truthy (:1089-1095): Outrage/Thrash/Petal
        # Dance/Uproar/Rollout/Bide mid-lock, the RELEASE turn of every two-turn
        # charge move, and `recharge`.
        #
        # Rebuilding `self.active.moves` from that single entry threw away the
        # other three moves and reset the locked move's PP to the `1` default,
        # so every sampled search world saw the bot with a one-move, 1-PP
        # active for the whole horizon. Keep the tracked moveset (real PP)
        # and express the lock the way the engine already consumes it: only
        # the forced move is left selectable. poke-engine reads exactly that -
        # `add_available_moves` skips `p.disabled` moves
        # (poke-engine src/genx/state.rs:462-463) and the conversion forwards
        # the flag verbatim (fp/search/poke_engine_helpers.py:164).
        #
        # `recharge` / `struggle` / gen1 `fight` are pseudo-moves PS invents for
        # this slot (sim/pokemon.ts:973-977, :1107-1112) - they are never in
        # `moveSlots`, so `get_move` misses and we fall through to the old
        # replace-with-a-single-move behaviour, which is what currently makes
        # the bot answer a recharge turn with `/choose move recharge`.
        if len(request_moves) == 1 and constants.PP not in request_moves[0]:
            forced_move = self.active.get_move(request_moves[0][constants.ID])
            if forced_move is not None:
                logger.info(
                    "Request is locked into {} - keeping {}'s full moveset and "
                    "disabling the rest".format(forced_move.name, self.active.name)
                )
                for m in self.active.moves:
                    m.disabled = m is not forced_move
                    # PS omits canZMove/canMegaEvo/... entirely while locked
                    # (sim/pokemon.ts:1142 `if (!lockedMove)`), and these Move
                    # objects now survive across requests, so a stale gen7
                    # can_z must not linger onto the locked turn
                    m.can_z = False
                return

            # STRUGGLE is not a locked move slot at all: PS SUBSTITUTES this
            # entry for an EMPTY move list (sim/pokemon.ts:1109-1111 `} else if
            # (!moves.length) { moves = [{move: 'Struggle', id: 'struggle',
            # target: 'randomNormal', disabled: false}]; lockedMove =
            # 'struggle' }`) and leaves `moveSlots` -- and `lastMove` --
            # untouched.  `get_move('struggle')` therefore misses and the
            # generic path below used to `clear()` the moveset and rebuild it as
            # a single `struggle` slot at the default 1 PP.  That is not a
            # cosmetic loss: `last_used_move` is a slot INDEX, so it then
            # resolved to STRUGGLE instead of the move actually used, and
            # poke-engine correctly failed Encore against it (data/moves.ts:4745
            # encore onStart -> `move.flags['failencore']`, with struggle
            # carrying that flag at data/moves.ts:617).  synth86252 T8: Mamoswine
            # is Choice-locked into Earthquake and Cursed Body Disables it, the
            # request offers only Struggle, and the observed `|-start|p1a:
            # Mamoswine|Encore` appeared in NO engine branch.
            # Keep the tracked moveset (real PP, real last-used-move index) and
            # express "nothing is selectable" the way the engine already consumes
            # it -- poke-engine pushes MoveChoice::Struggle itself once no move
            # passes `add_available_moves` (src/genx/state.rs:506-510).
            if request_moves[0][constants.ID] == "struggle" and self.active.moves:
                logger.info(
                    "Request offers only Struggle - keeping {}'s moveset and "
                    "disabling every move".format(self.active.name)
                )
                for m in self.active.moves:
                    m.disabled = True
                    m.can_z = False
                return

            # RECHARGE is the OTHER pseudo-move PS invents for this slot
            # (sim/pokemon.ts:973-977) and it has exactly the Struggle defect the
            # branch above fixes.  Rebuilding the moveset as a single `recharge`
            # slot at the default 1 PP threw away the real moves, and
            # `last_used_move` is a slot INDEX - so poke_engine_helpers'
            # "move not found" fallback (:415) resolved it to `move:0`, i.e. that
            # 1-PP pseudo-slot.  The engine then decremented it to 0 and Encore's
            # PS-exact `moveSlot.pp <= 0` guard (data/moves.ts encore onStart)
            # correctly FAILED - synth137727 T15: Slaking is recharging off Giga
            # Impact, Quaquaval Encores it, and the observed `|-start|p1a:
            # Slaking|Encore` appeared in none of the 5 candidate branches.
            # (Verified: the same engine state with only that slot's PP raised
            # 1 -> 8 emits `ApplyVolatileStatus SideOne: ENCORE`.)
            #
            # Keep the tracked moveset (real PP, real last-used-move index) and
            # express the recharge the way the engine already consumes it for the
            # OPPONENT: the `mustrecharge` volatile, which short-circuits
            # `add_available_moves` and leaves `MoveChoice::None` as the side's
            # only legal action (poke-engine src/genx/state.rs:1276-1281), exactly
            # PS's own `trapped` + single-action recharge request.
            if request_moves[0][constants.ID] == "recharge" and self.active.moves:
                logger.info(
                    "Request offers only Recharge - keeping {}'s moveset and "
                    "setting mustrecharge".format(self.active.name)
                )
                for m in self.active.moves:
                    m.disabled = True
                    m.can_z = False
                if "mustrecharge" not in self.active.volatile_statuses:
                    self.active.volatile_statuses.append("mustrecharge")
                return

        # request JSON gives detailed information about the moves
        # available to the active pkmn. Take those as the source of truth
        self.active.moves.clear()
        for index, move in enumerate(request_moves):
            # hidden power's ID is always 'hiddenpower' regardless of the type
            # parse it separately from the 'move' key
            if move[constants.ID] == constants.HIDDEN_POWER:
                self.active.add_move(normalize_name(move["move"]))
            else:
                self.active.add_move(move[constants.ID])
            self.active.moves[-1].disabled = move.get(constants.DISABLED, False)
            self.active.moves[-1].current_pp = move.get(constants.PP, 1)

            # PokemonShowdown disables these moves in the protocol after they are used once,
            # but poke-engine does not expect them to be disabled
            # poke-engine deals with this by not allowing these moves to be selected if they are the last used move
            if self.last_used_move.move in ["gigatonhammer", "bloodmoon"]:
                for m in self.active.moves:
                    if m.name == self.last_used_move.move and m.disabled:
                        m.disabled = False

            try:
                self.active.moves[index].can_z = request_json[constants.ACTIVE][0][
                    constants.CAN_Z_MOVE
                ][index]
            except KeyError:
                pass

    def update_from_request_json(self, request_json):
        """
        Updates the battler's information based on the request JSON
        This should be called with a cloned battle/battler so that the original is not modified
        """
        try:
            # PS sets `trapped` in the move request only for a HARD trap - one
            # the server has confirmed prevents switching (sim/pokemon.ts:1124-1138
            # only writes data.trapped when pokemon.trapped is true, otherwise it
            # writes data.maybeTrapped). A maybeTrapped request leaves the switch
            # LEGAL (sim/side.ts:983-997: the hard case emitChoiceErrors and
            # rejects the switch, the maybe case merely sets choice.cantUndo and
            # the switch still resolves). Folding maybeTrapped into a hard trap
            # sets force_trapped and makes the engine strip every legal root
            # switch, so mirror only the confirmed hard trap here.
            self.trapped = request_json[constants.ACTIVE][0].get(
                constants.TRAPPED, False
            )
        except KeyError:
            self.trapped = False

        # every party row the request accounts for, by object identity; see
        # `_drop_unclaimed_forme_duplicates` below
        claimed_ids = set()

        for index, pkmn_dict in enumerate(
            request_json[constants.SIDE][constants.POKEMON]
        ):
            switch_string_pkmn = Pokemon.from_switch_string(
                pkmn_dict[constants.DETAILS]
            )
            pkmn_name = switch_string_pkmn.name
            pkmn_level = switch_string_pkmn.level
            pkmn_status = switch_string_pkmn.status
            if pkmn_dict[constants.ACTIVE]:
                if self.active.name != pkmn_name and self.active.base_name != pkmn_name:
                    raise ValueError(
                        "Active pokemon mismatch: expected {} or {}, got {}".format(
                            self.active.name, self.active.base_name, pkmn_name
                        )
                    )

                if constants.ACTIVE in request_json:
                    self._initialize_user_active_from_request_json(request_json)

                pkmn = self.active
            else:
                pkmn = self.find_pokemon_in_reserves(pkmn_name)
                for move_name in pkmn_dict[constants.MOVES]:
                    if not pkmn.get_move(move_name):
                        pkmn.add_move(move_name)

            claimed_ids.add(id(pkmn))
            pkmn.index = index + 1
            pkmn.level = pkmn_level
            pkmn.status = pkmn_status
            pkmn.nickname = self.active.extract_nickname_from_pokemonshowdown_string(
                pkmn_dict[constants.IDENT]
            )
            pkmn.reviving = pkmn_dict.get(constants.REVIVING, False)
            hp, max_hp, status = get_pokemon_info_from_condition(
                pkmn_dict[constants.CONDITION]
            )
            pkmn.hp = hp
            # the |request| condition string is exact `hp/maxhp`, so it always
            # (re-)establishes an exact-HP certificate on our own side
            pkmn.hp_exact = True
            pkmn.status = status

            # a fainted condition ("0 fnt") carries no maxhp information;
            # preserve the previously-known max_hp so the pkmn remains
            # revivable by Revival Blessing (maxhp == 0 means an empty slot)
            if max_hp != 0:
                pkmn.max_hp = max_hp
                pkmn.max_hp_exact = True
            pkmn.ability = pkmn_dict[constants.REQUEST_DICT_ABILITY]
            pkmn.item = pkmn_dict[constants.ITEM] if pkmn_dict[constants.ITEM] else None

            # PS request stats are baseStoredStats - the UNtransformed values
            # (sim/pokemon.ts getSwitchRequestData). While this pkmn is
            # transformed (Ditto/Mew), its live stats are the copied target's
            # stats set by the `transform` battle-modifier; overwriting from
            # the request would revert e.g. Ditto-as-Tyranitar's def 217 back
            # to Ditto's own 133 mid-transform. Only the transformed ACTIVE can
            # hit this: transform ends on switch-out.
            if (
                constants.TRANSFORM in pkmn.volatile_statuses
                or getattr(pkmn, "transformed_into", None)
            ):
                continue

            for stat, number in pkmn_dict[constants.STATS].items():
                pkmn.stats[constants.STAT_ABBREVIATION_LOOKUPS[stat]] = number

        self._drop_unclaimed_forme_duplicates(claimed_ids)

    def _drop_unclaimed_forme_duplicates(self, claimed_ids):
        """Drop reserve rows the request's party list does not account for, when
        the row is a provable forme-duplicate of a row it DOES account for.

        `sim/side.ts:355-366 getRequestData` pushes exactly one entry per member
        of `this.pokemon`, so the request's party list IS the server's roster:
        six rows, one physical pokemon each.  Live tracking can nonetheless end
        up holding the same mon twice when it appears under two forme names
        across its lifetime.  The reproduced case is a mon whose ability changes
        its forme on entry: `|switch|p1a: Terapagos|Terapagos, L77, F|265/265`
        followed by `|-activate| ability: Tera Shift` + `|detailschange|
        Terapagos-Terastal`.  Replayed against a state already initialized from
        the (post-detailschange) request, `switch_or_drag`'s
        `find_pokemon_in_reserves("terapagos")` misses -- the Terastal forme is
        the ACTIVE, not a reserve -- so it builds a second object and benches the
        first.  The party then holds SEVEN rows.

        That over-length party was being silently truncated at the engine
        boundary (`PySide::new` indexed `pokemon[0..5]`), which discarded the
        last living reserve and made the engine believe the side was wiped:
        synth29732 T31 lost Arceus-Dark 288/288 and synth41888 T26 lost
        Hydrapple 312/312, and in both the engine then suppressed a correct
        end-of-turn Leftovers residual because it thought the battle was decided.

        Conservative on purpose (REFUSE-DON'T-GUESS): a row is dropped only when
        BOTH (a) no request entry claimed that object, and (b) some row the
        request DID claim shares its forme family.  A reserve row whose family
        the request does not cover is left alone and will reach the binding,
        which now refuses an over-length party loudly instead of truncating it.
        """
        if os.environ.get("FP_CONTROL_KEEP_FORME_DUPLICATES"):
            # CONTROL ONLY, never set in a measurement run: restores the pre-fix
            # behaviour so the fix can be shown capable of failing.
            return
        claimed_families = {
            base_species_name(p.name)
            for p in ([self.active] if self.active is not None else []) + self.reserve
            if id(p) in claimed_ids
        }
        kept = []
        for pkmn in self.reserve:
            if (
                id(pkmn) not in claimed_ids
                and base_species_name(pkmn.name) in claimed_families
            ):
                logger.info(
                    "Dropping stale forme-duplicate reserve row {} ({}/{}): the "
                    "request's party list does not account for it".format(
                        pkmn.name, pkmn.hp, pkmn.max_hp
                    )
                )
                continue
            kept.append(pkmn)
        if len(kept) != len(self.reserve):
            self.reserve[:] = kept

    def re_initialize_active_pokemon_from_request_json(self, request_json):
        """
        Re-initializes the active pokemon based on the last request JSON that was received
        This is useful when the bot's active pkmn has mega-evolved. We need to get the new stats/hp
        """
        pokedex_name = normalize_name(pokedex[self.active.name][constants.NAME])
        request_json_active_pkmn = [
            p
            for p in request_json["side"]["pokemon"]
            if normalize_name(p[constants.DETAILS]).split(",")[0] == pokedex_name
            or normalize_name(p[constants.DETAILS]).split(",")[0]
            == self.active.base_name
        ]
        stale_forme_stats = False
        if pokedex_name == "terapagosstellar" and len(request_json_active_pkmn) == 0:
            request_json_active_pkmn = [
                p
                for p in request_json["side"]["pokemon"]
                if normalize_name(p[constants.DETAILS]).split(",")[0]
                == "terapagosterastal"
                or normalize_name(p[constants.DETAILS]).split(",")[0]
                == self.active.base_name
            ]
            # this request predates the Terapagos-Stellar detailschange: its
            # stats belong to the Terastal forme. PS recalculates storedStats
            # for the Stellar forme at the permanent formeChange
            # (sim/pokemon.ts:1393-1426 setSpecies via formeChange), which
            # forme_change() already mirrored - keep those, take only HP here.
            stale_forme_stats = True
        if len(request_json_active_pkmn) != 1:
            # The request describes a DIFFERENT forme than the one on the field, and
            # neither the live forme name nor `base_name` appears in any `details`.
            # That is not a bug in the log: `details` is a snapshot of whatever forme
            # the pokemon was on when the request was BUILT (sim/pokemon.ts:1162
            # getSwitchRequestData -> `this.details`, refreshed at every permanent
            # formeChange, sim/pokemon.ts:1448-1452), while `update_battle` buffers a
            # turn's protocol and replays it only once the NEXT request has already
            # replaced `battle.request_json` -- so a mid-turn |detailschange| is always
            # matched against the END-of-turn forme.  Two ways that diverges:
            #   * Ice Face oscillating inside one turn (synth20228 T14: switch in as
            #     Eiscue-Noice, Snowscape restores Eiscue, Tail Slap re-busts it; the
            #     restore is matched against a request listing Eiscue-Noice);
            #   * a forme reverted by a same-turn KO (synth33783 T5: Terapagos
            #     terastallizes to Terapagos-Stellar and faints, PS reverts it with
            #     `|detailschange|p1: Terapagos|Terapagos|[silent]`, so the forceSwitch
            #     request lists plain Terapagos).
            # Identify the entry the way PS itself does instead of by forme: `active`
            # is positional (`this.position < this.side.active.length`,
            # sim/pokemon.ts:1164), so in singles exactly one entry carries it and it
            # is the mon on the field whatever its details say; `ident` is
            # `this.fullname` (:1161), the nickname, which no forme change ever
            # rewrites.  The stats in such a request belong to the other forme, so take
            # only HP -- exactly as for the Terapagos case above.  `forme_change()` has
            # already recomputed the live forme's stats, mirroring PS's own
            # setSpecies (sim/pokemon.ts:1404-1418).
            by_active = [
                p
                for p in request_json["side"]["pokemon"]
                if p.get(constants.ACTIVE) is True
            ]
            if len(by_active) != 1:
                by_active = [
                    p
                    for p in request_json["side"]["pokemon"]
                    if normalize_name(p.get(constants.IDENT, "").split(":")[-1])
                    in (pokedex_name, self.active.base_name, self.active.name)
                ]
            if len(by_active) == 1:
                request_json_active_pkmn = by_active
                stale_forme_stats = True
        assert (
            len(request_json_active_pkmn) == 1
        ), f"Didn't find exactly 1 {pokedex_name}, pokemon: {request_json}"
        pkmn_info = request_json_active_pkmn[0]
        if not stale_forme_stats:
            for stat, number in pkmn_info[constants.STATS].items():
                self.active.stats[constants.STAT_ABBREVIATION_LOOKUPS[stat]] = number
        self.active.hp, _, _ = get_pokemon_info_from_condition(
            pkmn_info[constants.CONDITION]
        )
        self.active.hp_exact = True

    def initialize_first_turn_user_from_json(self, request_json):
        """
        Similar to `update_from_request_json`, but meant to be used on the first `request_json` that is seen
        This function differs in that it adds new pokemon to the Side rather than modifying existing ones
        """
        try:
            # only mirror a confirmed HARD trap; a maybeTrapped request still
            # allows the switch (see update_from_request_json for the PS refs).
            self.trapped = request_json[constants.ACTIVE][0].get(
                constants.TRAPPED, False
            )
        except KeyError:
            self.trapped = False

        self.name = request_json[constants.SIDE][constants.ID]
        self.reserve.clear()
        for index, pkmn_dict in enumerate(
            request_json[constants.SIDE][constants.POKEMON]
        ):
            nickname = pkmn_dict[constants.IDENT]
            pkmn_details = pkmn_dict[constants.DETAILS]
            pkmn_item = pkmn_dict[constants.ITEM] if pkmn_dict[constants.ITEM] else None
            pkmn = Pokemon.from_switch_string(pkmn_details, nickname=nickname)

            # For some reason PS sends "zacian" during team preview
            # when you have a zacian with a rusted sword
            if pkmn.name.startswith("zacian") and pkmn_item == "rustedsword":
                pkmn = Pokemon("zaciancrowned", pkmn.level)
                pkmn.nickname = nickname

            pkmn.ability = pkmn_dict[constants.REQUEST_DICT_ABILITY]
            pkmn.index = index + 1
            pkmn.reviving = pkmn_dict.get(constants.REVIVING, False)
            pkmn.hp, pkmn.max_hp, pkmn.status = get_pokemon_info_from_condition(
                pkmn_dict[constants.CONDITION]
            )
            pkmn.hp_exact = True
            pkmn.max_hp_exact = True
            for stat, number in pkmn_dict[constants.STATS].items():
                pkmn.stats[constants.STAT_ABBREVIATION_LOOKUPS[stat]] = number

            pkmn.item = pkmn_item
            if constants.TERA_TYPE in pkmn_dict:
                pkmn.tera_type = normalize_name(pkmn_dict[constants.TERA_TYPE])

            if pkmn_dict[constants.ACTIVE]:
                # the bot's own first-turn |switch| message is dropped
                # (the request JSON sets the lead) so it never goes through
                # `switch_or_drag` - the lead must be marked revealed here
                pkmn.revealed = True
                self.active = pkmn
            else:
                self.reserve.append(pkmn)

            for move_name in pkmn_dict[constants.MOVES]:
                if (
                    pkmn.name.startswith("zacian")
                    and pkmn_item == "rustedsword"
                    and move_name == "ironhead"
                ):
                    logger.info(
                        "Zacian with rusted sword: changing ironhead to behemothblade"
                    )
                    move_name = "behemothblade"
                elif (
                    pkmn.name.startswith("zamazenta")
                    and pkmn_item == "rustedshield"
                    and move_name == "ironhead"
                ):
                    logger.info(
                        "Zamazenta with rusted shield: changing ironhead to behemothbash"
                    )
                    move_name = "behemothbash"
                pkmn.add_move(move_name)

        # if there is an active pokemon, we want to look through it's moves
        if constants.ACTIVE in request_json:
            self._initialize_user_active_from_request_json(request_json)

        # if a team_dict exists, meaning we are playing a format where we selected our own team,
        # set the nature/evs for each pokmeon
        if self.team_dict is not None:
            team_dict_pkmn_names = [p["species"] for p in self.team_dict]
            for pkmn in [self.active] + self.reserve:
                pkmn_other_formes = [
                    normalize_name(n) for n in pokedex[pkmn.name].get("otherFormes", [])
                ]
                if pkmn.name in team_dict_pkmn_names:
                    team_dict_pkmn = next(
                        p for p in self.team_dict if p["species"] == pkmn.name
                    )
                elif pkmn.base_name in team_dict_pkmn_names:
                    team_dict_pkmn = next(
                        p for p in self.team_dict if p["species"] == pkmn.base_name
                    )
                elif any(p in team_dict_pkmn_names for p in pkmn_other_formes):
                    other_forme_in_team = next(
                        p for p in pkmn_other_formes if p in team_dict_pkmn_names
                    )
                    team_dict_pkmn = next(
                        p for p in self.team_dict if p["species"] == other_forme_in_team
                    )
                else:
                    raise ValueError("Could not find {} in team_dict".format(pkmn.name))
                pkmn.nature = team_dict_pkmn["nature"] or "serious"
                pkmn.evs = (
                    int(team_dict_pkmn["evs"]["hp"] or 0),
                    int(team_dict_pkmn["evs"]["atk"] or 0),
                    int(team_dict_pkmn["evs"]["def"] or 0),
                    int(team_dict_pkmn["evs"]["spa"] or 0),
                    int(team_dict_pkmn["evs"]["spd"] or 0),
                    int(team_dict_pkmn["evs"]["spe"] or 0),
                )


class Pokemon:
    def __init__(self, name: str, level: int, nature="serious", evs=None, ivs=None):
        if evs is None:
            evs = random_battles_evs()
        if ivs is None:
            ivs = (31,) * 6

        self.name = normalize_name(name)
        self.nickname = None
        self.base_name = self.name
        self.level = level
        self.nature = nature
        self.evs = evs
        self.ivs = ivs
        self.speed_range = StatRange(min=0, max=float("inf"))
        self.hidden_power_possibilities = possible_hidden_power_types()

        try:
            self.base_stats = pokedex[self.name][constants.BASESTATS]
        except KeyError:
            logger.info("Could not pokedex entry for {}".format(self.name))
            self.name = [k for k in pokedex if self.name.startswith(k)][0]
            logger.info("Using {} instead".format(self.name))
            self.base_stats = pokedex[self.name][constants.BASESTATS]

        self.stats = calculate_stats(
            self.base_stats, self.level, ivs=ivs, nature=nature, evs=evs
        )

        self.max_hp = self.stats.pop(constants.HITPOINTS)
        self.hp = self.max_hp
        # exact-HP certificate state (fp/hp_certificate.py).  A fresh pokemon is
        # at full HP, which is exact by construction -- `hp == max_hp` is an
        # identity, not a percent-derived estimate.
        hp_certificate.init_pokemon(self)
        self.substitute_hit = False
        # This pokemon's Substitute's remaining HP, tracked as a CLOSED INTERVAL
        # [substitute_health_low, substitute_health].  PS never puts the absorbed
        # magnitude on the wire (`-activate ... move: Substitute|[damage]` is
        # magnitude-free, data/moves.ts:18354), so after any absorbed hit the
        # remaining HP is a RANGE -- the hit was one of Showdown's 16 rolls -- and
        # collapsing it to a point is a bug rather than a tuning choice (HANDOFF
        # section 4 rule 10).  `substitute_health` is the interval's UPPER bound,
        # which is also the single number the engine boundary consumes (see
        # battle_to_poke_engine_state) and the only bound HANDOFF section 13
        # allows a state string to be read as.  Both are seeded to floor(maxhp/4)
        # on `-start Substitute` (PS data/moves.ts substitute onStart:
        # effectState.hp = Math.floor(maxhp/4)), where the value is EXACT and the
        # interval a singleton; both are 0 when no substitute is up.
        self.substitute_health = 0
        self.substitute_health_low = 0
        if self.name == "shedinja":
            self.max_hp = 1
            self.hp = 1

        self.ability = None
        self.types = pokedex[self.name][constants.TYPES]
        self.item = constants.UNKNOWN_ITEM
        self.removed_item = None
        # Previous upkeep HP, used to rule out Leftovers/Black Sludge only
        # across a full turn boundary. Reset per Pokemon so a switch-in never
        # inherits another mon's history.
        self._last_upkeep_hp = None
        self.unknown_forme = False

        self.moves_used_since_switch_in = set()
        self.zoroark_disguised_as = None
        self.hp_at_switch_in = self.max_hp
        self.status_at_switch_in = None
        # Item held when this object last switched IN.  Only an item that changed
        # DURING a stay can belong to an Illusion bearer wearing this species' face;
        # anything already established beforehand is this mon's own (illusion_end).
        self.item_at_switch_in = self.item
        self.terastallized = False
        self.tera_type = None
        self.forme_changed = False
        # Every species name this ONE physical pokemon has been observed under
        # in the protocol: its entry species, plus every |detailschange| /
        # |-formechange| target and every switch-out forme revert.  This is the
        # OPPONENT side's substitute for the bot side's request party list: the
        # opponent never sends a request, so `Battler.
        # _drop_unclaimed_forme_duplicates` -- which keys on which objects the
        # request claimed -- structurally cannot run there.  `Side.
        # find_pokemon_in_reserves` consults this lineage so a mon re-entering
        # under a forme name it has previously worn is RECOGNISED rather than
        # built a second time, which is what pushed synth15853's opponent party
        # to seven rows and cost 20 checked turns at the binding refusal.
        self.forme_lineage = {self.name}
        # Set to the species named by a |detailschange| and consumed by the
        # |-formechange| that immediately follows it (see
        # `fp.battle_modifier.form_change`).  PS's PERMANENT formeChange path
        # emits BOTH lines when the source is a Status
        # (sim/pokemon.ts:1449-1476), and the trailing |-formechange| must not
        # be mistaken for the non-permanent path.
        self.permanent_forme_echo_pending = None
        # the species this pkmn has transformed into (Ditto/Mew via Transform or
        # Imposter), or None. The `transform` battle-modifier copies the target's
        # stats/types/ability/moves/boosts directly onto this object but leaves
        # `name` as the base species; `transformed_into` carries the copied
        # species identity (id/weight/base-types) through to engine conversion.
        self.transformed_into = None
        self.original_ability = None
        self.fainted = False
        self.reviving = False

        # whether the opponent has seen this pkmn: set to True when it
        # enters the field or is shown in team preview. Only random battle
        # formats ever leave this False for the bot's never-fielded pkmn
        # and for sampled opponent fill-ins
        self.revealed = False

        # whether this pkmn's Illusion has been broken (zoroark unmasked).
        # only ever set True on an actual zoroark once its disguise drops
        # or it is otherwise discovered
        self.illusion_broken = False
        self.moves = []
        self.status = None
        self.volatile_statuses = []
        self.volatile_status_durations = defaultdict(lambda: 0)
        self.boosts = defaultdict(lambda: 0)
        self.rest_turns = 0
        self.sleep_turns = 0
        self.knocked_off = False
        self.can_mega_evo = False
        self.can_ultra_burst = False
        self.can_dynamax = False
        self.can_terastallize = False
        self.is_mega = False
        self.mega_name = None
        self.can_have_choice_item = True
        self.item_inferred = False
        self.gen_3_consecutive_sleep_talks = 0
        self.impossible_items = set()
        self.impossible_abilities = set()
        # Mechanics signatures disproved by exact damage observations for this
        # one physical pokemon.  It belongs on the object so deepcopy preserves
        # evidence while another pokemon of the same species starts clean.
        self.rejected_set_signatures = set()

        # ENTRY-INTENT evidence: one snapshot of what OUR active looked like
        # each time the opponent CHOSE to bring this pokemon in against it
        # (recorded by battle_modifier.switch_or_drag; consumed by the
        # sampler's entry_intent_multipliers)
        self.entry_contexts = []

        # number of times this pkmn has been directly hit by a damaging move
        # (Rage Fist). Accumulates for the entire battle: it is never reset,
        # persisting through switches and fainting/revival
        self.times_attacked = 0

        # whether this pkmn has already spent its ONCE-PER-BATTLE ability trigger:
        # Intrepid Sword (PS data/abilities.ts:2204-2208 pokemon.swordBoost),
        # Dauntless Shield (:844-848 pokemon.shieldBoost) and Battle Bond
        # (:353-360 source.bondTriggered). A pkmn has exactly one ability, so a
        # single flag serves all three. Persists across switches and is never
        # reset. Forwarded to the engine, which hard-coded it False until the
        # binding-completeness pass and therefore re-armed the trigger at every
        # search root.
        self.once_per_battle_ability_used = False

        # move TYPES whose one-time Stellar-terastallization boost this pkmn has
        # already consumed (PS pokemon.stellarBoostedTypes, sim/pokemon.ts:264;
        # spent in sim/battle-actions.ts:1778-1784). Only ever non-empty for a
        # pkmn terastallized into Stellar. Persists for the whole battle across
        # switches and is NOT copied by Transform. Held as type NAMES because
        # that is what the engine binding accepts.
        self.stellar_boosted_types = set()

        # the Stellar type consumed by this pkmn's most recent move line, kept
        # only until that move resolves: PS spends the boost inside the damage
        # calculation, so a move that MISSES, FAILS or is IMMUNE never spends it.
        # The protocol delivers those verdicts on the line(s) right after |move|,
        # which roll this back. None whenever nothing is pending.
        self.stellar_boost_pending_type = None

        # PS pokemon.activeMoveActions (sim/pokemon.ts:245-255): number of move
        # actions this pkmn has taken since it last switched in. Incremented in
        # runMove BEFORE the move executes (sim/battle-actions.ts:217) - so a
        # fully-paralyzed/flinched/asleep attempt (a |cant| line) still counts -
        # and reset to 0 on every switch-in (sim/battle-actions.ts:138).
        # Consumed by the engine's Fake Out / First Impression gating.
        self.active_move_actions = 0

        # PS pokemon.activeTurns: reset to 0 on switch-in
        # (sim/battle-actions.ts:137) and incremented in nextTurn AFTER the
        # turn's residuals ran, just before the |turn| line is emitted
        # (sim/battle.ts:1762) - so it is still 0 during the residual phase of
        # the turn this pkmn entered on. Mirrored here: zeroed in
        # switch_or_drag, incremented in the |turn| handler. Consumed by the
        # Slow Start upkeep decrement gate (PS data/abilities.ts:4309
        # `if (pokemon.activeTurns && this.effectState.counter)`): a mon that
        # entered mid-turn skips that turn's decrement.
        self.active_turns = 0

        # True once this pkmn's own action line (|move| or |cant|) has been
        # seen this turn; cleared on |turn| and on switch-in. Mirrors PS's
        # `!this.queue.willMove(target)` "already acted" check, which bumps
        # the taunt/encore duration by one at apply time (data/moves.ts
        # taunt/encore condition durationCallback/onStart)
        self.moved_this_turn = False

        # True while this pkmn's current forme came from a NON-permanent
        # |-formechange| (PS formeChange with isPermanent falsy). PS reverts
        # such formes on switch-out: clearVolatile ends with
        # setSpecies(this.baseSpecies) (sim/pokemon.ts:1514-1565), e.g.
        # Meloetta-Pirouette -> Meloetta. A |detailschange| (permanent:
        # Mega/Terapagos-Stellar/Mimikyu-Busted) clears this flag instead
        # and never reverts on switch-out.
        self.reverts_forme_on_switch_out = False

        # True while this pkmn's own self-costing move (Substitute/Belly Drum/
        # ghost-Curse/...) is the most recent |move| line: the next bare
        # '-damage' on it is the move's HP cost, not a hit (see
        # SELF_COST_DAMAGE_MOVES in fp.battle_modifier)
        self.self_cost_damage_expected = False

    def get_species(self):
        pokedex_data = pokedex[self.name]
        return normalize_name(pokedex_data.get("baseSpecies", self.name))

    def get_mega_pkmn_info(self) -> list[tuple[str, str]]:
        # For avoiding Legends ZA megas: omit mega pokemon in the pokedex that have "gen": 9
        # Come back and undo this when Legends ZA megas are available in standard formats
        mega_names = []
        if self.name == "rayquaza":
            return [("rayquaza", "none")]
        elif self.name == "floetteeternal":
            return [("floettemega", "floettite")]

        potential_megas = [
            f"{self.name}mega",
            f"{self.name}megax",
            f"{self.name}megay",
        ]
        for mega_forme in potential_megas:
            if mega_forme in pokedex:
                mega_names.append(
                    (
                        mega_forme,
                        normalize_name(pokedex[mega_forme]["requiredItem"]),
                    )
                )

        return mega_names

    def has_type(self, pkmn_type: str):
        if self.terastallized:
            return pkmn_type == self.tera_type
        else:
            return pkmn_type in self.types

    def forme_change(self, new_forme_switch_string):
        # PS's non-permanent formeChange never touches hp or max hp
        # (sim/pokemon.ts setSpecies assigns hp only when `!this.maxhp`, i.e. at
        # construction; max-HP-raising formes announce the gain separately), and
        # max_hp is not reassigned here either, so hp must carry over untouched.
        # The old `int(hp / max_hp * max_hp)` float round-trip truncated 85 -> 84
        # (synth286069 T5: Cramorant -> Gorging), silently desyncing hp from its
        # own display certificate -- which both made an exactly-lethal Night
        # Shade kill in every branch AND tripped the `hp != hp_display_hp` fast
        # path that disables the B1 band re-evaluation.
        new_pokemon = Pokemon.from_switch_string(new_forme_switch_string)
        self.name = new_pokemon.name
        self.hp_exact = False
        self.base_stats = new_pokemon.base_stats
        # Preserve the set's nature/EVs/IVs across the forme change (Showdown
        # setSpecies recomputes with the actual nature); otherwise a Jolly
        # Palafin-Hero etc. would be recalculated with a neutral nature and lose
        # its ~10% Speed.
        self.stats = calculate_stats(
            self.base_stats,
            self.level,
            ivs=getattr(self, "ivs", (31,) * 6),
            nature=self.nature,
            evs=self.evs,
        )
        self.ability = new_pokemon.ability
        self.types = new_pokemon.types
        self.forme_changed = True
        # protocol-derived identity: this object has now been seen under
        # `new_pokemon.name` too
        self.forme_lineage.add(self.name)

    def is_alive(self):
        return self.hp > 0

    def calculate_boosted_stats(self):
        return {
            constants.ATTACK: boost_multiplier_lookup[self.boosts[constants.ATTACK]]
            * self.stats[constants.ATTACK],
            constants.DEFENSE: boost_multiplier_lookup[self.boosts[constants.DEFENSE]]
            * self.stats[constants.DEFENSE],
            constants.SPECIAL_ATTACK: boost_multiplier_lookup[
                self.boosts[constants.SPECIAL_ATTACK]
            ]
            * self.stats[constants.SPECIAL_ATTACK],
            constants.SPECIAL_DEFENSE: boost_multiplier_lookup[
                self.boosts[constants.SPECIAL_DEFENSE]
            ]
            * self.stats[constants.SPECIAL_DEFENSE],
            constants.SPEED: boost_multiplier_lookup[self.boosts[constants.SPEED]]
            * self.stats[constants.SPEED],
        }

    @classmethod
    def extract_nickname_from_pokemonshowdown_string(cls, ps_string):
        return "".join(ps_string.split(":")[1:]).strip()

    @classmethod
    def from_switch_string(cls, switch_string, nickname=None):
        if nickname is not None:
            nickname = cls.extract_nickname_from_pokemonshowdown_string(nickname)

        details = switch_string.split(",")
        name = details[0]
        try:
            level = int(details[1].replace("L", "").strip())
        except (IndexError, ValueError):
            level = 100
        pkmn = Pokemon(name, level)
        pkmn.nickname = nickname
        return pkmn

    def set_spread(self, nature, evs, ivs=None):
        if isinstance(evs, str):
            evs = [int(e) for e in evs.split(",")]
        if ivs is None:
            ivs = getattr(self, "ivs", (31,) * 6)
        elif isinstance(ivs, str):
            ivs = [int(i) for i in ivs.split(",")]
        hp_percent = self.hp / self.max_hp
        self.stats = calculate_stats(
            self.base_stats, self.level, ivs=ivs, evs=evs, nature=nature
        )
        self.nature = nature
        self.evs = evs
        self.ivs = ivs
        self.max_hp = self.stats.pop(constants.HITPOINTS)
        self.hp = round(self.max_hp * hp_percent)
        # a fraction rescale onto a different max HP is an estimate, whatever the
        # old value was
        self.hp_exact = False

    def add_move(self, move_name: str):
        try:
            new_move = Move(move_name)
            self.moves.append(new_move)
            return new_move
        except KeyError:
            logger.warning("{} is not a known move".format(move_name))
            return None

    def remove_move(self, move_name: str):
        for mv in self.moves:
            if mv.name == move_name:
                self.moves.remove(mv)
                return True
        return False

    def get_move(self, move_name: str):
        for m in self.moves:
            if m.name == normalize_name(move_name):
                return m
            elif m.name.startswith(constants.HIDDEN_POWER) and move_name.startswith(
                constants.HIDDEN_POWER
            ):
                return m
            elif m.name.startswith("return") and move_name.startswith("return"):
                return m
        return None

    @classmethod
    def get_dummy(cls):
        p = Pokemon("pikachu", 100)
        p.hp = 0
        p.name = ""
        p.ability = None
        p.fainted = True
        return p

    def __eq__(self, other):
        return self.name == other.name and self.level == other.level

    def __repr__(self):
        return "{}, level {}".format(self.name, self.level)


class Move:
    def __init__(self, name):
        name = normalize_name(name)
        if (
            constants.HIDDEN_POWER != name
            and constants.HIDDEN_POWER in name
            and not name.endswith(constants.HIDDEN_POWER_ACTIVE_MOVE_BASE_DAMAGE_STRING)
        ):
            name = "{}{}".format(
                name, constants.HIDDEN_POWER_ACTIVE_MOVE_BASE_DAMAGE_STRING
            )
        move_json = all_move_json[name]
        self.name = name

        if move_json[constants.PP] == 1:
            self.max_pp = 1
        elif "champions" in FoulPlayConfig.pokemon_format:
            self.max_pp = int(int(move_json[constants.PP] / 5 + 1) * 4)
        else:
            self.max_pp = int(move_json.get(constants.PP) * 1.6)

        self.disabled = False
        self.can_z = False
        self.current_pp = self.max_pp

    def __eq__(self, other):
        return self.name == other.name

    def __repr__(self):
        return "{}".format(self.name)
