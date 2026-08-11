import logging
import os

import constants
from data import pokedex
from fp.battle import Battle, Pokemon, Battler, LastUsedMove

from poke_engine import (
    State as PokeEngineState,
    Side as PokeEngineSide,
    SideConditions as PokeEngineSideConditions,
    VolatileStatusDurations as PokeEngineVolatileStatusDurations,
    Pokemon as PokeEnginePokemon,
    Move as PokeEngineMove,
    calculate_damage,
)

try:
    from poke_engine import calculate_damage_rolls_full
except ImportError:  # pre-0.0.54 wheels export only the legacy max-only pair
    calculate_damage_rolls_full = None

logger = logging.getLogger(__name__)

# older poke_engine binaries do not have the `revealed` field and reject
# the constructor kwarg - detect support once at import so foul-play keeps
# working against either build
POKE_ENGINE_SUPPORTS_REVEALED = hasattr(PokeEnginePokemon, "revealed")

# same wheel-compat detection for the `illusion_broken` field
POKE_ENGINE_SUPPORTS_ILLUSION_BROKEN = hasattr(PokeEnginePokemon, "illusion_broken")

# same wheel-compat detection for the `known` field
POKE_ENGINE_SUPPORTS_KNOWN = hasattr(PokeEnginePokemon, "known")

# same wheel-compat detection for the `times_attacked` field (Rage Fist)
POKE_ENGINE_SUPPORTS_TIMES_ATTACKED = hasattr(PokeEnginePokemon, "times_attacked")

# same wheel-compat detection for the `transformed` field (Ditto/Imposter). The
# engine gained a `transformed` flag alongside its in-search Transform machinery;
# older binaries lack it, so it is only passed when the installed wheel supports
# it. The copied species-id/stats/types/weight are what actually make search see
# the copy and work on every wheel regardless of this flag.
POKE_ENGINE_SUPPORTS_TRANSFORMED = hasattr(PokeEnginePokemon, "transformed")

# wheel-compat detection for the two remaining PERSISTENT Pokemon fields the
# binding-completeness pass wired through. Both survive switches and are read on
# LATER turns, which is exactly the class the replay checker cannot see (it
# rebuilds a fresh state from the protocol every turn), so they can only be
# validated by forwarding them:
#   once_per_battle_ability_used - Intrepid Sword / Dauntless Shield / Battle Bond
#     fire at most once per battle; hard-coded False, the engine re-armed all
#     three at every search root.
#   stellar_boosted_types - the move types whose one-time Stellar-tera boost has
#     been spent; hard-coded empty, the engine re-granted the bonus every root.
POKE_ENGINE_SUPPORTS_ONCE_PER_BATTLE_ABILITY_USED = hasattr(
    PokeEnginePokemon, "once_per_battle_ability_used"
)
POKE_ENGINE_SUPPORTS_STELLAR_BOOSTED_TYPES = hasattr(
    PokeEnginePokemon, "stellar_boosted_types"
)

# same wheel-compat detection for the `disable` volatile-status duration field.
# Older binaries lack it, so the disable timer is only forwarded when supported;
# the per-move `disabled` flag (set in the protocol parser) already prevents the
# engine from re-selecting the move on every wheel regardless of this timer.
POKE_ENGINE_SUPPORTS_DISABLE_DURATION = hasattr(
    PokeEngineVolatileStatusDurations, "disable"
)

# same wheel-compat detection for the `partiallytrapped`/`magnetrise` volatile
# duration fields. The engine core already carries, serializes and consumes both
# as an elapsed-EOT count (src/state.rs, src/genx/generate_instructions.rs:6628-6739),
# but older PyVolatileStatusDurations bindings omit them and hard-code the FFI
# conversion to 0, discarding the real elapsed count. Only forward the counts
# when the installed wheel exposes the fields; otherwise the engine keeps
# re-counting a full effect from scratch (its prior behaviour on every wheel).
POKE_ENGINE_SUPPORTS_TRAP_MAGNETRISE_DURATION = hasattr(
    PokeEngineVolatileStatusDurations, "partiallytrapped"
)

# same wheel-compat detection for the `healblock`/`throatchop`/`syrupbomb`
# volatile duration fields (all three shipped together with the engine's EOT
# arms in src/genx/generate_instructions.rs, but check each so a partial
# binding never sends an unsupported kwarg). Without forwarding, the engine
# re-counts each effect from scratch every root - its prior behaviour.
POKE_ENGINE_SUPPORTS_HEALBLOCK_THROATCHOP_SYRUPBOMB_DURATION = all(
    hasattr(PokeEngineVolatileStatusDurations, field)
    for field in ("healblock", "throatchop", "syrupbomb")
)

# same wheel-compat detection for the `cudchew` volatile duration field.  The
# engine's `cudchew_end_of_turn` (genx/generate_instructions.rs:5917) is gated on
# BOTH the CUDCHEW volatile and this two-state counter: 0 -> tick to 1 (the eat
# turn's own end of turn), 1 -> remove the volatile and RE-EAT
# `last_consumed_item`.  Without forwarding it, a state whose active ate a berry
# on the PREVIOUS turn reaches the engine with the counter at its 0 default, so
# the re-eat is pushed a turn out and the observed end-of-turn re-eat (PS emits
# `-activate <slot>|ability: Cud Chew` then `-enditem <berry>|[eat]` and the
# berry's effect) can never be reproduced.
POKE_ENGINE_SUPPORTS_CUDCHEW_DURATION = hasattr(
    PokeEngineVolatileStatusDurations, "cudchew"
)

# same wheel-compat detection for the Side `revival_blessing` field
POKE_ENGINE_SUPPORTS_REVIVAL_BLESSING = hasattr(PokeEngineSide, "revival_blessing")

# same wheel-compat detection for the Side `last_move_failed` field (PS
# `moveLastTurnResult`). The engine gates Stomping Tantrum / Temper Flare's doubling on
# it and used to hard-code it false on every deserialize, so no replay/preview caller
# could ever activate the doubling. Older wheels reject the kwarg; without it those two
# moves simply never double, which is the pre-field behaviour.
POKE_ENGINE_SUPPORTS_LAST_MOVE_FAILED = hasattr(PokeEngineSide, "last_move_failed")

# same wheel-compat detection for the Side `times_revived` field. The engine derives
# PS's cumulative `side.totalFainted` as `num_fainted_pkmn() + times_revived` (the two
# differ by exactly the number of Revival Blessing revives, since PS never decrements
# totalFainted - sim/battle.ts:2551), and Supreme Overlord's `fallen` snapshot reads it
# (genx/abilities.rs:3127). The binding used to hard-code 0 in the python-object ->
# engine conversion, so the engine fix was inert for every foul-play caller. Older
# wheels reject the kwarg; without it the count degrades to currently-fainted-only,
# which is the pre-field behaviour.
POKE_ENGINE_SUPPORTS_TIMES_REVIVED = hasattr(PokeEngineSide, "times_revived")

# same wheel-compat detection for the Side `stats_raised` field (PS
# `statsRaisedThisTurn`). The engine gates Alluring Voice / Burning Jealousy's 100%
# confuse/burn secondary on it and used to hard-code it false in the python-object ->
# engine conversion, on the premise that it is within-turn-only state. PS falsifies that
# on turn 1, where `nextTurn` skips the reset (sim/battle.ts:1673-1675) and a `|start|`
# switch-in ability boost is still live for turn 1's moves. Older wheels reject the
# kwarg; without it those two moves simply never fire the secondary off a pre-turn-1
# boost, which is the pre-field behaviour.
POKE_ENGINE_SUPPORTS_STATS_RAISED = hasattr(PokeEngineSide, "stats_raised")

# same wheel-compat detection for the `last_consumed_item` field. The engine
# gates Harvest/Cud Chew recycling on it (genx/abilities.rs:1314-1315: berry
# AND empty item slot), so it must be seeded at root or a berry eaten before
# this decision can never be recycled in-search.
POKE_ENGINE_SUPPORTS_LAST_CONSUMED_ITEM = hasattr(
    PokeEnginePokemon, "last_consumed_item"
)

# same wheel-compat detection for the `active_move_actions` field (PS
# pokemon.activeMoveActions, sim/pokemon.ts:245-255: move actions taken since
# the last switch-in; Fake Out / First Impression fail when it exceeds 1).
# The binding field ships with wheel 0.0.49 - on 0.0.48 the kwarg is rejected,
# so it is only forwarded when supported and the engine keeps its prior
# "fresh window every search" behavior.
POKE_ENGINE_SUPPORTS_ACTIVE_MOVE_ACTIONS = hasattr(
    PokeEnginePokemon, "active_move_actions"
)


def status_to_string(status):
    if status == constants.SLEEP:
        return "Sleep"
    elif status == constants.BURN:
        return "Burn"
    elif status == constants.FROZEN:
        return "Freeze"
    elif status == constants.PARALYZED:
        return "Paralyze"
    elif status == constants.POISON:
        return "Poison"
    elif status == constants.TOXIC:
        return "Toxic"
    elif status is None:
        return "None"
    raise ValueError(f"Unknown status: {status}")


def engine_move_window(moves, last_used_move=None):
    """The <=4 moves that reach the engine's fixed move slots.

    `pkmn.moves` is in REVEAL order, so the old `moves[:4]` kept the OLDEST
    four and discarded the newest, most decision-relevant evidence - exactly
    backwards in the Zoroark pile-up cases that produce >4 moves. Keep the most
    recent four, and never drop the move the side actually just used: the
    `last_used_move` index lookup falls back to "move:0" when its move is
    outside the window, which Encore/choice-locks the engine onto the wrong
    slot. Both callers use this one helper so they cannot disagree.
    """
    moves = list(moves)
    if len(moves) <= 4:
        return moves

    kept = moves[-4:]
    if last_used_move and all(m.name != last_used_move for m in kept):
        for mv in moves:
            if mv.name == last_used_move:
                kept = [mv] + kept[1:]
                break
    return kept


def pokemon_to_poke_engine_pkmn(
    pkmn: Pokemon, mark_terastallized=False, last_used_move=None
):
    """
    id,level,type0,type1,hp,maxhp,ability,item,atk,def,spa,spd,spe,atkb,defb,spab,spdb,speb,accb,evab,status,subhp,restturns
    nature,volatiles,m0,m1,m2,m3

    mark_terastallized: force the engine-side `terastallized` flag on (used
    only on a FAINTED party slot to keep the engine's side-level
    can_use_tera() false after this side's tera was spent - see _padded_party)
    """

    # Gen 3/4 don't remove items if knocked off
    # but the item is not active, so lets remove it
    if pkmn.knocked_off or pkmn.item == "" or pkmn.item is None:
        pkmn.item = "None"

    # A transformed pokemon (Ditto/Mew via Transform or Imposter) has the
    # target's stats/types/ability/moves/boosts copied onto it by the `transform`
    # battle-modifier, but `name` stays the base species (e.g. 'ditto'). Showdown
    # also copies the target's species-id and weight (pokemon.ts:1276-1386), so
    # resolve the effective species here: id, base_types, and weight_kg follow
    # the transformed-into species. Otherwise the engine would see 4.0kg base
    # Ditto (wrong Heavy Slam/Low Kick BP) and any in-search stat recalc would
    # revert to Ditto's ~48 base stats instead of the copy.
    transformed_into = getattr(pkmn, "transformed_into", None)
    species_id = transformed_into if transformed_into else str(pkmn.name)

    base_types = pokedex[species_id][constants.TYPES]
    if len(base_types) == 1:
        base_types = (base_types[0], "typeless")
    if len(pkmn.types) == 1:
        pkmn.types = (pkmn.types[0], "typeless")
    # NOTE: a LOCAL window - never `pkmn.moves = ...`. The old in-place slice
    # mutated the caller's Pokemon and was safe only because every caller
    # happened to pass a copy.
    kept_moves = engine_move_window(pkmn.moves, last_used_move)
    if len(pkmn.moves) > 4:
        logger.warning(
            "More than 4 moves on pokemon: {} moves: {} - sending {}".format(
                pkmn.name,
                [m.name for m in pkmn.moves],
                [m.name for m in kept_moves],
            )
        )

    pkmn_moves = [
        PokeEngineMove(id=str(m.name), disabled=m.disabled, pp=m.current_pp)
        for m in kept_moves
    ]
    while len(pkmn_moves) < 4:
        pkmn_moves.append(PokeEngineMove(id="none", disabled=True, pp=0))

    # `original_ability` is only set when something CHANGED the ability
    # mid-battle, so for virtually every mon it is None and base_ability went
    # to the engine as "". The engine reverts ability to base_ability on every
    # in-tree switch-out (genx/abilities.rs), so an empty base_ability
    # permanently strips Regenerator / Intimidate / Levitate / ... from BOTH
    # sides after the first simulated pivot - a depth-dependent corruption of
    # the whole search. Fall back to the ability the mon actually has.
    base_ability = str(pkmn.original_ability or pkmn.ability or "")

    extra_kwargs = {}
    if POKE_ENGINE_SUPPORTS_REVEALED:
        # getattr so that pkmn objects lacking the attribute
        # (e.g. older pickles) safely default to revealed
        extra_kwargs["revealed"] = getattr(pkmn, "revealed", True)
    if POKE_ENGINE_SUPPORTS_ILLUSION_BROKEN:
        extra_kwargs["illusion_broken"] = getattr(pkmn, "illusion_broken", False)
    if POKE_ENGINE_SUPPORTS_KNOWN:
        # `known` starts as a snapshot of `revealed` at conversion: they only
        # diverge in-search (the engine may toggle `revealed`; `known` is
        # immutable so the known-alive premium never moves on a reveal)
        extra_kwargs["known"] = getattr(pkmn, "revealed", True)
    if POKE_ENGINE_SUPPORTS_TIMES_ATTACKED:
        # getattr so pkmn objects lacking the attribute (e.g. older pickles)
        # safely default to never having been hit
        extra_kwargs["times_attacked"] = getattr(pkmn, "times_attacked", 0)
    if POKE_ENGINE_SUPPORTS_TRANSFORMED and transformed_into:
        # forward-compat annotation for wheels with the transform machinery; on
        # older wheels the copied id/stats/types/weight above already deliver the
        # copy, so this is purely additive and never load-bearing at root
        extra_kwargs["transformed"] = True
    if POKE_ENGINE_SUPPORTS_ONCE_PER_BATTLE_ABILITY_USED:
        # getattr so older pickles safely default to the trigger still being
        # available - the pre-forwarding behaviour
        extra_kwargs["once_per_battle_ability_used"] = getattr(
            pkmn, "once_per_battle_ability_used", False
        )
    if POKE_ENGINE_SUPPORTS_STELLAR_BOOSTED_TYPES:
        # the binding takes TYPE NAMES (the engine's bit indices are an internal
        # enum ordering foul-play must not encode). Sorted for determinism.
        extra_kwargs["stellar_boosted_types"] = sorted(
            getattr(pkmn, "stellar_boosted_types", None) or []
        )
    if POKE_ENGINE_SUPPORTS_LAST_CONSUMED_ITEM:
        # a knocked-off item was removed, not consumed (PS only sets lastItem
        # on use/eat), so it must not enable Harvest/Cud Chew recycling.
        # getattr so older pickles safely default to nothing consumed
        # PS's `lastItem` is written ONLY by useItem/eatItem (sim/pokemon.ts:1805,
        # :1846) and is overwritten by every later consumption; `takeItem`
        # (:1856-1871) never sets it.  `battle_modifier.remove_item` records exactly
        # those consumptions on `last_consumed_item`, and `set_item` clears it to ""
        # when Harvest restores the berry (mirroring harvest.onResidual's
        # `pokemon.lastItem = ''`), so when the attribute EXISTS it is both newer
        # and more faithful than the `removed_item` latch (a one-shot that also
        # records Trick-donor / steal removals).  `removed_item` stays as the
        # fallback for a mon NEVER seen consuming anything (attribute absent ->
        # None), so those states convert exactly as before -- including the
        # Illusion-bearer carry in fp/replay/checker.py.  A cleared "" must NOT
        # fall back: PS's lastItem is known-empty there.
        last_consumed = getattr(pkmn, "last_consumed_item", None)
        if last_consumed is None:
            removed_item = getattr(pkmn, "removed_item", None)
            if removed_item and not getattr(pkmn, "knocked_off", False):
                last_consumed = removed_item
        if last_consumed:
            extra_kwargs["last_consumed_item"] = str(last_consumed)
    if POKE_ENGINE_SUPPORTS_ACTIVE_MOVE_ACTIONS:
        # PS increments in runMove before the move executes
        # (sim/battle-actions.ts:217) and resets on switch-in (:138); the
        # protocol parser mirrors that on the pkmn. getattr so older pickles
        # safely default to a fresh Fake Out window
        extra_kwargs["active_move_actions"] = getattr(pkmn, "active_move_actions", 0)

    return PokeEnginePokemon(
        id=species_id,
        level=pkmn.level,
        types=tuple(pkmn.types),
        base_types=tuple(base_types),
        hp=int(pkmn.hp),
        maxhp=int(pkmn.max_hp),
        ability=str(pkmn.ability),
        base_ability=base_ability,
        item=str(pkmn.item),
        nature=pkmn.nature,
        evs=tuple(pkmn.evs),
        attack=pkmn.stats[constants.ATTACK],
        defense=pkmn.stats[constants.DEFENSE],
        special_attack=pkmn.stats[constants.SPECIAL_ATTACK],
        special_defense=pkmn.stats[constants.SPECIAL_DEFENSE],
        speed=pkmn.stats[constants.SPEED],
        status=status_to_string(pkmn.status),
        rest_turns=pkmn.rest_turns,
        sleep_turns=pkmn.sleep_turns,
        weight_kg=float(pokedex[species_id][constants.WEIGHT]),
        moves=pkmn_moves,
        tera_type=pkmn.tera_type or "typeless",
        terastallized=pkmn.terastallized or mark_terastallized,
        **extra_kwargs,
    )


def get_dummy_poke_engine_pkmn():
    # maxhp == 0 marks this as an empty party slot (not a fainted pkmn)
    # so the engine never treats it as a Revival Blessing target
    return PokeEnginePokemon(id="pikachu", level=1, hp=0, maxhp=0)


def battler_has_revive_prompt(battler: Battler) -> bool:
    """
    Returns True if this battler is being prompted to revive a fainted pkmn
    (i.e. Revival Blessing was used)

    PokemonShowdown sends this as a normal forceSwitch request where the
    pokemon that used the reviving move has `"reviving": true` on its
    entry in the request JSON
    """
    return any(
        getattr(p, "reviving", False)
        for p in [battler.active] + battler.reserve
        if p is not None
    )


class OverlongPartyError(ValueError):
    """A side was tracked with more than the six pokemon PS gives it."""


def _padded_party(battler: Battler):
    # The engine Side holds a fixed 6-slot party and appending to
    # `side.pokemon` AFTER construction is silently discarded (the getter
    # returns a fresh list), so the party must be padded to 6 with
    # maxhp-0 dummies BEFORE the Side is constructed
    party = [battler.active] + battler.reserve

    # ...and REFUSE an over-length one rather than let it be truncated.
    # `PySide::new` used to index `pokemon[0..5]` and drop the tail in silence,
    # so a party that had grown to seven (one physical mon tracked under two
    # forme names -- Terapagos entering as `Terapagos` and immediately
    # |detailschange|-ing to `Terapagos-Terastal`) lost its LAST living reserve.
    # The engine then read the side as wiped and suppressed correct end-of-turn
    # residuals: synth29732 T31 (Arceus-Dark 288/288 dropped) and synth41888 T26
    # (Hydrapple 312/312 dropped) both lost a Leftovers heal that way.  The
    # binding now refuses too; this mirror makes the refusal effective against a
    # wheel built before that change, and names the party in the message.
    # Coverage lost to an honest refusal is recoverable; a silent wrong answer
    # is not.
    if len(party) > 6 and os.environ.get("FP_CONTROL_KEEP_FORME_DUPLICATES"):
        # CONTROL ONLY, never set in a measurement run: the pre-fix silent
        # truncation, paired with the same flag in
        # `Battler._drop_unclaimed_forme_duplicates`, so the whole pre-fix
        # pipeline can be restored and shown to reproduce the original findings.
        party = party[:6]
    if len(party) > 6:
        raise OverlongPartyError(
            "side {} was tracked with {} pokemon (max 6): {}".format(
                battler.name,
                len(party),
                [(p.name, p.hp, p.max_hp) for p in party],
            )
        )

    # A side whose tera was consumed by a pkmn that later FAINTED has no
    # terastallized pkmn left (the faint handler cleared the flag, mirroring
    # PS sim/battle.ts:2565), but the engine derives its side-level
    # can_use_tera() from "any party pkmn terastallized"
    # (genx/state.rs:980-987). Re-flag one FAINTED slot so search can never
    # offer this side a second tera; a fainted placeholder never affects
    # damage. (If every pkmn is alive again post-Revival-Blessing there is no
    # slot to carry the marker and search may over-offer tera - a strictly
    # smaller error than the pre-fix wrong live typing.)
    tera_marked_index = None
    if getattr(battler, "tera_spent", False) and not any(
        p.terastallized for p in party
    ):
        for i, p in enumerate(party):
            if p.hp <= 0 and p.max_hp > 0:
                tera_marked_index = i
                break

    # party[0] is the active: it is the only slot `last_used_move` can name
    pokemon = [
        pokemon_to_poke_engine_pkmn(
            p,
            mark_terastallized=(i == tera_marked_index),
            last_used_move=(battler.last_used_move.move if i == 0 else None),
        )
        for i, p in enumerate(party)
    ]
    while len(pokemon) < 6:
        pokemon.append(get_dummy_poke_engine_pkmn())
    return pokemon


def battler_to_poke_engine_side(
    battler: Battler, force_switch=False, stayed_in_on_switchout_move=False
):
    last_used_move = "move:none"
    if battler.last_used_move.move.startswith("switch "):
        last_used_move = "switch:0"
    elif battler.last_used_move.move == "struggle":
        # Struggle occupies no moveset slot, so it has no index to serialize. It is not
        # "move:none" either: PS records it as `lastMove` like any other move and Encore
        # FAILS against it (struggle carries `failencore` and owns no moveSlot,
        # data/moves.ts:18205ff / :4725ff). Falling through to the "move:0" default below
        # made the engine Encore-lock a Struggling mon into slot 0 and redirect its next
        # move there (synth30440 T20 Gogoat's Bulk Up, synth46824 T60 Snorlax's Curse).
        last_used_move = "move:struggle"
    elif battler.last_used_move.move:
        # The engine pokemon has at most 4 move slots (indices 0-3). When a
        # pokemon reveals >4 moves (e.g. a Zoroark illusion piling its own moves
        # onto the disguised species), match against the SAME window
        # `pokemon_to_poke_engine_pkmn` sends, so last_used_move can never
        # serialize an index the engine would reject (PokemonMoveIndex::
        # deserialize panics on "4"). The window keeps last_used_move whenever
        # the mon has it, so the "move:0" fallback is now unreachable except
        # for a move the mon does not own at all.
        pkmn_moves = [
            m.name
            for m in engine_move_window(
                battler.active.moves, battler.last_used_move.move
            )
        ]
        for i, move in enumerate(pkmn_moves):
            if move == battler.last_used_move.move:
                last_used_move = "move:{}".format(i)
                break
        else:
            last_used_move = "move:0"

    # The exact remaining substitute HP is not fully knowable (the '[damage]'
    # protocol ping is magnitude-free), but the client tracks a running point
    # estimate on the pokemon: seeded to floor(maxhp/4) on `-start Substitute`
    # (== PS moves.ts substitute onStart and the engine's own creation seed) and
    # whittled by each observed absorbed hit. Fall back to floor(maxhp/4) when a
    # substitute is up but no creation event was observed (reconnect /
    # mid-battle start), which is no worse than the old unhit assumption.
    substitute_health = 0
    if constants.SUBSTITUTE in battler.active.volatile_statuses:
        # int() at the engine boundary: max_hp can be a float (e.g. after the
        # `/= 2` un-Dynamax) and the engine field is an integer
        substitute_health = int(
            getattr(battler.active, "substitute_health", 0)
            or (battler.active.max_hp / 4)
        )

    future_sight_index = 0
    if battler.future_sight[0] > 0:
        if (
            battler.active.name == battler.future_sight[1]
            or battler.active.base_name == battler.future_sight[1]
        ):
            future_sight_index = 0
        else:
            index = 1
            for pkmn in battler.reserve:
                if (
                    pkmn.name == battler.future_sight[1]
                    or pkmn.base_name == battler.future_sight[1]
                ):
                    future_sight_index = index
                    break
                index += 1
            else:
                raise ValueError(
                    "Couldnt find future sight source: {} not in {} + {}".format(
                        battler.future_sight[1],
                        battler.active.name,
                        [p.name for p in battler.reserve],
                    )
                )

    volatile_status_duration_kwargs = dict(
        confusion=battler.active.volatile_status_durations[constants.CONFUSION],
        lockedmove=battler.active.volatile_status_durations[constants.LOCKED_MOVE],
        encore=battler.active.volatile_status_durations["encore"],
        slowstart=battler.active.volatile_status_durations[constants.SLOW_START],
        taunt=battler.active.volatile_status_durations[constants.TAUNT],
        yawn=battler.active.volatile_status_durations[constants.YAWN],
    )
    if POKE_ENGINE_SUPPORTS_DISABLE_DURATION:
        volatile_status_duration_kwargs["disable"] = (
            battler.active.volatile_status_durations[constants.DISABLE]
        )
    if POKE_ENGINE_SUPPORTS_TRAP_MAGNETRISE_DURATION:
        # the engine consumes both as an elapsed-EOT count (chips taken so far /
        # turns levitated so far) and ticks them up in-search until release at 4
        # (generate_instructions.rs:6636 partiallytrapped `>= 4`, :6700 magnetrise
        # match 0..=3 -> tick, 4 -> release, `_` -> panic). Clamp to 4 so a
        # magnetrise seed can never hit that panic arm and so the rare
        # random(5,7) 6-7-turn partial-trap overshoot is bounded to the folded
        # 5-turn model.
        volatile_status_duration_kwargs["partiallytrapped"] = min(
            battler.active.volatile_status_durations[constants.PARTIALLY_TRAPPED], 4
        )
        volatile_status_duration_kwargs["magnetrise"] = min(
            battler.active.volatile_status_durations[constants.MAGNET_RISE], 4
        )
    if POKE_ENGINE_SUPPORTS_HEALBLOCK_THROATCHOP_SYRUPBOMB_DURATION:
        # same elapsed-EOT semantics as partiallytrapped/magnetrise above,
        # reconstructed per real `upkeep`. Clamps come from the engine EOT arms
        # (genx/generate_instructions.rs): healblock releases at 1 and panics
        # above 1; throatchop releases at exactly 1 (a larger seed ticks up
        # forever and never releases); syrupbomb drops Speed at 0|1|2, releases
        # at 3 and panics above 3. Clamping to each release counter turns any
        # stale/overshot root count into "release at the next simulated EOT".
        volatile_status_duration_kwargs["healblock"] = min(
            battler.active.volatile_status_durations[constants.HEAL_BLOCK], 1
        )
        volatile_status_duration_kwargs["throatchop"] = min(
            battler.active.volatile_status_durations[constants.THROAT_CHOP], 1
        )
        volatile_status_duration_kwargs["syrupbomb"] = min(
            battler.active.volatile_status_durations[constants.SYRUP_BOMB], 3
        )
    if POKE_ENGINE_SUPPORTS_CUDCHEW_DURATION:
        # two-state counter, NOT an elapsed-turn count: the engine's EOT arm
        # accepts only 0 (tick to 1) and 1 (re-eat), and panics on anything else
        # (generate_instructions.rs:6033). Clamped to that domain so a stale or
        # overshot seed degrades to "re-eat at the next simulated end of turn"
        # instead of tripping the panic. Only meaningful together with the
        # CUDCHEW volatile, which the engine checks first.
        volatile_status_duration_kwargs["cudchew"] = min(
            battler.active.volatile_status_durations["cudchew"], 1
        )

    extra_side_kwargs = {}
    if POKE_ENGINE_SUPPORTS_REVIVAL_BLESSING:
        # wheel-compat guarded like every other newer field: older binaries
        # reject the kwarg. Without it a revive prompt degrades to the plain
        # force_switch the older engine understood anyway.
        extra_side_kwargs["revival_blessing"] = battler_has_revive_prompt(battler)
    if POKE_ENGINE_SUPPORTS_LAST_MOVE_FAILED:
        extra_side_kwargs["last_move_failed"] = bool(
            getattr(battler, "last_move_failed", False)
        )
    if POKE_ENGINE_SUPPORTS_TIMES_REVIVED:
        # Clamped to the engine's i8 domain and to PS's own `totalFainted < 100` cap
        # (sim/battle.ts:2551); a single side can realistically revive at most 5 times,
        # so the clamp only guards against a malformed count reaching the binding.
        extra_side_kwargs["times_revived"] = max(
            0, min(int(getattr(battler, "times_revived", 0)), 100)
        )
    if POKE_ENGINE_SUPPORTS_STATS_RAISED:
        extra_side_kwargs["stats_raised"] = bool(
            getattr(battler, "stats_raised_this_turn", False)
        )

    side = PokeEngineSide(
        active_index="0",
        baton_passing=battler.baton_passing,
        shed_tailing=battler.shed_tailing,
        pokemon=_padded_party(battler),
        side_conditions=PokeEngineSideConditions(
            aurora_veil=battler.side_conditions[constants.AURORA_VEIL],
            crafty_shield=battler.side_conditions["craftyshield"],
            healing_wish=battler.side_conditions[constants.HEALING_WISH],
            light_screen=battler.side_conditions[constants.LIGHT_SCREEN],
            lucky_chant=battler.side_conditions["luckychant"],
            lunar_dance=battler.side_conditions["lunardance"],
            mat_block=battler.side_conditions["matblock"],
            mist=battler.side_conditions["mist"],
            protect=battler.side_conditions[constants.PROTECT],
            quick_guard=battler.side_conditions["quickguard"],
            reflect=battler.side_conditions[constants.REFLECT],
            safeguard=battler.side_conditions[constants.SAFEGUARD],
            spikes=battler.side_conditions[constants.SPIKES],
            stealth_rock=battler.side_conditions[constants.STEALTH_ROCK],
            sticky_web=battler.side_conditions[constants.STICKY_WEB],
            tailwind=battler.side_conditions[constants.TAILWIND],
            toxic_count=battler.side_conditions[constants.TOXIC_COUNT],
            toxic_spikes=battler.side_conditions[constants.TOXIC_SPIKES],
            wide_guard=battler.side_conditions["wideguard"],
        ),
        wish=(int(battler.wish[0]), int(battler.wish[1])),
        future_sight=(battler.future_sight[0], str(future_sight_index)),
        force_switch=force_switch,
        force_trapped=battler.trapped,
        slow_uturn_move=stayed_in_on_switchout_move,
        volatile_statuses=set(battler.active.volatile_statuses),
        volatile_status_durations=PokeEngineVolatileStatusDurations(
            **volatile_status_duration_kwargs
        ),
        substitute_health=substitute_health,
        attack_boost=battler.active.boosts[constants.ATTACK],
        defense_boost=battler.active.boosts[constants.DEFENSE],
        special_attack_boost=battler.active.boosts[constants.SPECIAL_ATTACK],
        special_defense_boost=battler.active.boosts[constants.SPECIAL_DEFENSE],
        speed_boost=battler.active.boosts[constants.SPEED],
        accuracy_boost=battler.active.boosts[constants.ACCURACY],
        evasion_boost=battler.active.boosts[constants.EVASION],
        last_used_move=last_used_move,
        switch_out_move_second_saved_move="NONE",  # always none because we can't know this
        **extra_side_kwargs,
    )

    return side


def get_weather_string(weather):
    if weather == constants.RAIN:
        return "rain"
    elif weather == constants.SUN:
        return "sun"
    elif weather == constants.SAND:
        return "sand"
    elif weather == constants.HAIL:
        return "hail"
    elif weather == constants.SNOW:
        return "snow"
    elif weather == constants.DESOLATE_LAND:
        return "harshsun"
    elif weather == constants.HEAVY_RAIN:
        return "heavyrain"
    elif weather is None:
        return "none"
    elif weather == "none":
        return "none"
    else:
        raise ValueError(f"Unknown weather {weather}")


def get_terrain_string(terrain):
    if terrain == constants.ELECTRIC_TERRAIN:
        return "electricterrain"
    elif terrain == constants.GRASSY_TERRAIN:
        return "grassyterrain"
    elif terrain == constants.MISTY_TERRAIN:
        return "mistyterrain"
    elif terrain == constants.PSYCHIC_TERRAIN:
        return "psychicterrain"
    elif terrain is None:
        return "none"
    elif terrain == "none":
        return "none"
    else:
        raise ValueError(f"Unknown terrain {terrain}")


def replace_hidden_power_last_used_move(battler: Battler):
    for mv in battler.active.moves:
        if mv.name.startswith(constants.HIDDEN_POWER):
            battler.last_used_move = LastUsedMove(
                pokemon_name=battler.last_used_move.pokemon_name,
                move=mv.name,
                turn=battler.last_used_move.turn,
            )
            break
    else:
        logger.warning("Could not replace hiddenpower")
        battler.last_used_move = LastUsedMove(
            pokemon_name=battler.last_used_move.pokemon_name,
            move="switch {}".format(battler.active.name),
            turn=battler.last_used_move.turn,
        )


def replace_return_last_used_move(battler: Battler):
    for mv in battler.active.moves:
        if mv.name.startswith("return"):
            battler.last_used_move = LastUsedMove(
                pokemon_name=battler.last_used_move.pokemon_name,
                move=mv.name,
                turn=battler.last_used_move.turn,
            )
            break
    else:
        logger.warning("Could not replace return")
        battler.last_used_move = LastUsedMove(
            pokemon_name=battler.last_used_move.pokemon_name,
            move="switch {}".format(battler.active.name),
            turn=battler.last_used_move.turn,
        )


def battle_to_poke_engine_state(battle: Battle, swap=False):
    # Boolean that represents if we have used a switch-out move first (i.e. fast uturn)
    # this is toggled to True if we did, and signifies to the engine that the opponent has
    # selected a move and that should be accounted for in the search
    opponent_switchout_move_stayed_in = False
    bot_lum = battle.user.last_used_move
    opp_lum = battle.opponent.last_used_move
    if bot_lum.move in constants.SWITCH_OUT_MOVES and opp_lum.turn != bot_lum.turn:
        opponent_switchout_move_stayed_in = True

    if battle.opponent.last_used_move.move == constants.HIDDEN_POWER:
        replace_hidden_power_last_used_move(battle.opponent)
    elif battle.opponent.last_used_move.move == "return":
        replace_return_last_used_move(battle.opponent)

    if battle.user.last_used_move.move == constants.HIDDEN_POWER:
        replace_hidden_power_last_used_move(battle.user)
    if battle.user.last_used_move.move == "return":
        replace_return_last_used_move(battle.user)

    side_one = battler_to_poke_engine_side(
        battle.user, force_switch=battle.force_switch
    )
    side_two = battler_to_poke_engine_side(
        battle.opponent, stayed_in_on_switchout_move=opponent_switchout_move_stayed_in
    )

    if swap:
        side_one, side_two = side_two, side_one

    state = PokeEngineState(
        side_one=side_one,
        side_two=side_two,
        weather=get_weather_string(battle.weather),
        weather_turns_remaining=battle.weather_turns_remaining,
        terrain=get_terrain_string(battle.field),
        terrain_turns_remaining=battle.field_turns_remaining,
        trick_room=battle.trick_room,
        trick_room_turns_remaining=battle.trick_room_turns_remaining,
        team_preview=battle.team_preview,
    )

    return state


def poke_engine_get_damage_rolls(
    battle: Battle, side_one_move, side_two_move, side_one_went_first
):
    if side_one_move.startswith("switch"):
        side_one_move = "switch"
    if side_two_move.startswith("switch"):
        side_two_move = "switch"

    state = battle_to_poke_engine_state(battle)

    logger.debug(
        "Calling calculate damage with state: {}, m1: {}, m2: {}, s1_went_first: {}".format(
            state.to_string(),
            side_one_move,
            side_two_move,
            side_one_went_first,
        )
    )

    s1_rolls, s2_rolls = calculate_damage(
        state,
        side_one_move,
        side_two_move,
        side_one_went_first,
    )

    logger.debug(
        "Got Rolls s1_rolls: {}, s2_rolls: {}".format(
            s1_rolls,
            s2_rolls,
        )
    )

    return s1_rolls, s2_rolls


def poke_engine_get_damage_roll_sets(
    battle: Battle, side_one_move, side_two_move, side_one_went_first
):
    """The FULL 16 non-crit + 16 crit Showdown-exact values for both sides.

    Returns ``((s1_noncrit, s1_crit) | None, (s2_noncrit, s2_crit) | None)``,
    each list ASCENDING (index 15 is PS's ``random(16) == 0`` max roll).  This
    is the interval-preserving sibling of `poke_engine_get_damage_rolls`, whose
    ``[max_noncrit, max_crit]`` pair throws away the very range a caller needs
    in order to avoid collapsing a 16-roll distribution to a point estimate
    (HANDOFF section 4 rule 10).

    Returns ``(None, None)`` on a wheel that predates the full export, so a
    caller can REFUSE rather than silently fall back to a point.
    """
    if calculate_damage_rolls_full is None:
        return None, None

    if side_one_move.startswith("switch"):
        side_one_move = "switch"
    if side_two_move.startswith("switch"):
        side_two_move = "switch"

    state = battle_to_poke_engine_state(battle)
    return calculate_damage_rolls_full(
        state,
        side_one_move,
        side_two_move,
        side_one_went_first,
    )
