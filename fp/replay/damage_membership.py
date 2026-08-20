"""PS-EXACT damage membership checking for SYNTHETIC replay logs.

Real-ladder replay checking treats damage magnitude as SOFT because the
opponent's exact randbats spread is unknown.  The synthetic corpus
(tools/gen_corpus.js) writes a `<game>.teams.json` sidecar with BOTH full teams
(EVs/IVs/computed stats), which upgrades damage to a HARD assertion: for each
observed direct-move |-damage|, the PS-EXACT 16-value damage set for that exact
attacker/defender state (with the realized crit flag) must CONTAIN the observed
HP delta.  Tolerance is exactly 0 -- membership is set membership, never a band.

Two checks, per the design spec (PS_EXACT_ROLLS_SPEC.md 5):
  (a) observed vs PS-exact.  `derive_ps_damage` reimplements Showdown's damage
      chain (sim/battle-actions.ts getDamage/modifyDamage, sim/battle.ts
      modify/chainModify/randomizer) in pure integer Python, INDEPENDENTLY of
      poke-engine: the roll-independent base is computed once (basePower event
      -> stat events -> tr(tr(tr(tr(2L/5+2)*bp*A)/D)/50) -> +2 ->
      WeatherModifyDamage -> crit) and the truncating tail is run per roll
      r in 0..15 (randomizer -> STAB -> typeMod doublings/floor-halvings ->
      burn -> one chained ModifyDamage -> min-1 -> tr(d,16)).  A failure here is
      a state-RECONSTRUCTION bug (or a PS behaviour this module gets wrong).
  (b) engine vs PS-exact (`engine_vs_ps`).  The same reconstructed state is fed
      to poke_engine.calculate_damage and compared against the PS-exact set --
      independent of what the replay happened to roll, so a failure is an ENGINE
      MODEL bug.  The binding now exports the full 16+16 roll set
      (`calculate_damage_roll_sets` -> poke-engine-py/src/lib.rs
      `calculate_damage_rolls_full`), so this comparison is ELEMENTWISE over all
      16 values (it was max-only while the export was missing).  It counts
      `engine_elementwise_agrees` / `engine_vs_ps_diverged` (+ a signed
      max-delta histogram, where a `+0` delta means the sets differ only away
      from the max roll -- exactly the class of bug the old max-only check could
      not see).  Diverging records carry the engine's own 16 values in
      `DamageRecord.engine_rolls` alongside the PS set in `.rolls`.

The derivation never guesses.  Anything it cannot derive with certainty raises
PsRefusal and the event is excluded under a named `ps_*` bucket: an unknown
ability/item, a damage-relevant ability/item/volatile that is not modelled (the
relevant sets are generated from data/abilities.ts and data/items.ts by
scanning for damage handlers), an unimplemented base-power callback, a Stellar
tera, a handler order that PS resolves by speed (chain() is not associative), an
HP-ratio gate whose outcome is not decided by the /100-reconstructed HP, an
unrecoverable multi-hit index, or a non-neutral nature (the sidecar carries no
`nature`, so fp's neutral default is only exact because gen9 randbats assigns
none -- see `_NEUTRAL_NATURES`).

Scope decision (measured against the actual protocol, not assumed):
  * Protocol HP is EXACT ("199/269") only for the perspective side (p1 =
    synthbot); the opponent side is shown in /100 fractions, so an observed p2
    HP delta maps to a RANGE of true deltas and membership is ill-posed.
    Membership therefore runs on p1-DEFENDER events (p2's move damaging p1's
    active); p1->p2 direct hits are counted as `fraction_limited`.  This is an
    OBSERVATION limit, not a modelling one: it cannot be made exact from the
    protocol alone.
  * A direct move hit is a |-damage| line with NO "[from]" tag, attributed to
    the most recent |move| line whose attacker is the opposite slot and whose
    target (when present) is the damaged slot.  Everything indirect (hazards /
    weather / status / items / recoil / drain / confusion self-hits) carries a
    "[from]" tag in the protocol and is never a candidate.
  * MULTI-HIT is IN SCOPE under exact derivation: PS reruns the whole chain per
    hit, so every hit of a CONSTANT-base-power use must be a member of the SAME
    16-value set.  Variable-BP multi-hit (Triple Kick / Triple Axel: `10|20 *
    move.hit`; Beat Up: one BP per eligible party member, in party order) and
    Parental Bond (hit 2 is modify(d, 0.25)) are ALSO in scope, derived per hit
    from PS's `move.hit`: the hit index is recovered from the |-hitcount| line
    whenever the use produced one -damage line per executed hit, and refuses
    (`ps_multihit_index_ambiguous`) when a Substitute swallowed one of them.
  * FIXED / DERIVED DAMAGE is split by whether the value is decidable here.
    PS returns these before the roll chain (battle-actions.ts:1604-1610), so
    there is no 16-value set -- but the single value is asserted exactly when
    its inputs are protocol facts:
      - ASSERTED: Seismic Toss / Night Shade (`damage: 'level'` -> the
        attacker's level) and Super Fang / Nature's Madness / Ruination
        (`clampIntRange(floor(target.hp / 2), 1)` off the defender's EXACT
        pre-hit HP, which the p1 side always has),
      - EXCLUDED (`fixed_damage`): Final Gambit / Endeavor (read the attacker's
        /100-quantised HP), Pain Split, the counter family (needs the damage
        taken this turn), Bide, and the OHKO moves (damage == target.maxhp, so
        the observation is always just "it died").
  * EXCLUDED and counted separately:
      - hits where the pre-turn state demonstrably no longer matches the
        realized pre-hit state: mid-turn boosts / status changes / item loss /
        ability triggers / weather / terrain / screens set BEFORE the hit
        (`confounded_*`, including Court Change's `-swapsideconditions`), the
        defender or attacker not being the turn-start active (pivot switch-ins,
        replacements), a defender that used Roost/Glaive Rush/Minimize earlier
        in the turn (silent self-volatiles that change incoming damage), and
        attacker-HP-scaled moves after the attacker's HP changed mid-turn,
      - Avalanche / Stomping Tantrum (`scope_ephemeral_state`): base power
        gated on ephemeral damage_dealt / last_move_failed state that is not
        reconstructible from the protocol,
      - an attacker that gained the Charge volatile mid-turn before its hit
        (`confounded_charge`), and a Rage Fist whose user was hit earlier in
        the turn (`confounded_timesattacked`: PS increments timesAttacked per
        damaging hit, including sub-absorbed ones),
      - Zoroark illusion spans (sidecar stats keyed by species are wrong for a
        disguised mon),
      - Substitute-absorbed hits produce no |-damage| line at all (nothing to
        assert); a `-activate ... Substitute [damage]` is counted.  A hit that
        DOES produce a -damage line while a Substitute is up bypassed it and is
        in scope (Substitute does not change the damage value),
      - delayed hits (Future Sight / Doom Desire land without a |move| line).
  * A hit that faints the defender ("0 fnt") or is capped by a survival effect
    (Focus Sash / Sturdy / Endure at exactly 1 HP) observes only a LOWER BOUND
    on the roll, so membership weakens to `some roll >= delta` and the event is
    counted as `lethal`/`capped` rather than feeding the delta histogram.
  * Fickle Beam is tested against BOTH arms, each derived PS's way: PS doubles
    the BASE POWER (onBasePower chainModify(2)), so the doubled arm is a second
    full derivation, NOT `2 * damage` (which is what the engine models -- see
    ficklebeam_membership).

Roll convention: roll arrays are ASCENDING, index j <-> PS's random(16) = 15-j,
so [0] is the 85% roll and [15] the max roll (the same convention the engine's
DamageRollSet uses).
"""

import json
import os
import re
from collections import Counter
from copy import copy, deepcopy
from dataclasses import dataclass, field, replace

import constants
from fp import hp_certificate
from fp.helpers import calculate_stats, normalize_name

try:
    from data import all_move_json, pokedex
except Exception:  # pragma: no cover
    all_move_json = {}
    pokedex = {}

try:
    # foul-play's LIVE-PLAY mirror of PS's generation-time spread
    # post-processing (teams.ts:1536-1589).  Reused rather than re-implemented
    # for the HP-EV shave; see `_randbats_shaved_hp_ev`.
    from data.pkmn_sets import random_battle_ev_iv_spread
except Exception:  # pragma: no cover
    random_battle_ev_iv_spread = None

try:
    # only for `_fill_unrevealed_reserves`, which materialises sidecar-stated
    # party slots the protocol never revealed
    from fp.battle import Pokemon
except Exception:  # pragma: no cover
    Pokemon = None

try:
    from fp.search.poke_engine_helpers import battle_to_poke_engine_state
    from poke_engine import calculate_damage, calculate_damage_roll_sets
except Exception:  # pragma: no cover
    battle_to_poke_engine_state = None
    calculate_damage = None
    calculate_damage_roll_sets = None


# ---------------------------------------------------------------------------
# PS-exact arithmetic primitives (sim/battle.ts, sim/dex.ts)
# ---------------------------------------------------------------------------
#
# Every value below is an integer.  No float emulation survives in this module:
# the only floats are the literal modifier constants PS itself writes as floats
# (1.5, 0.75, ...), and those are converted to their 4096-denominated integer
# form with the same `trunc(x * 4096)` PS uses, once, at table-build time.


def ps_trunc(num, bits: int = 0) -> int:
    """`Battle.trunc` (sim/dex.ts:391-394): `num >>> 0` (truncate toward zero
    into uint32), optionally `% 2**bits`.  Every value this module feeds it is
    non-negative and < 2**32, so the uint32 wrap is never exercised; a negative
    or oversized input is a bug and raises rather than silently wrapping."""
    n = int(num)
    if n < 0 or n >= (1 << 32):
        raise ValueError("ps_trunc out of uint32 range: {}".format(num))
    return n % (1 << bits) if bits else n


def ps_modifier_4096(numerator, denominator=1) -> int:
    """`tr(numerator * 4096 / denominator)` -- the integer modifier PS derives
    from a float/fraction argument (battle.ts:2337).  1.5 -> 6144, 1.3 -> 5324
    (NOT 5325: PS spells the 5325 modifiers as the explicit pair [5325, 4096]),
    0.75 -> 3072, 1.25 -> 5120."""
    return int(numerator * 4096 / denominator)


def ps_apply_modifier(value: int, mod_4096: int) -> int:
    """`modify(value, m)` with the modifier already in 4096ths
    (battle.ts:2329-2340): `tr((tr(value*m) + 2048 - 1) / 4096)`.  The +2047
    makes this round HALF-DOWN at the 4096 boundary."""
    return (value * mod_4096 + 2047) // 4096


def ps_modify(value: int, numerator, denominator=1) -> int:
    """`battle.modify(value, numerator, denominator)`."""
    return ps_apply_modifier(value, ps_modifier_4096(numerator, denominator))


def ps_chain_mods(mods_4096) -> int:
    """`chainModify` accumulation (battle.ts:2315/2326): each modifier folds in
    as `k'' = (k*k' + 0x800) >> 12` -- round HALF-UP, and NOT associative, so
    the iteration order is PS's handler order.  Returns the accumulated 4096ths
    modifier that `finalModify` then applies with a single `modify`."""
    acc = 4096
    for m in mods_4096:
        acc = (acc * m + 2048) >> 12
    return acc


def ps_randomizer(base_damage: int, r: int) -> int:
    """`randomizer` (battle.ts:2388-2391): `tr(tr(b * (100 - random(16))) / 100)`
    with r = random(16) in 0..15.  r=0 is the max roll, r=15 the 85% roll."""
    return (base_damage * (100 - r)) // 100


@dataclass
class PsTailParams:
    """The roll-INDEPENDENT parameters of PS's per-roll tail (spec 1.1
    R1-R9).  One instance describes both the crit and non-crit arms; only the
    ModifyDamage chain differs between them (screens off / Sniper on)."""

    stab_4096: int = 4096
    type_levels: int = 0
    burn: bool = False
    final_4096_noncrit: int = 4096
    final_4096_crit: int = 4096


def ps_roll(base_after_crit: int, r: int, params: PsTailParams, crit: bool) -> int:
    """One PS-exact damage value: the truncating tail of
    BattleActions#modifyDamage (sim/battle-actions.ts:1755-1841) run for
    `random(16) == r` on a base that has already been through
    basePower/stat events, the base formula, +2, WeatherModifyDamage and the
    crit multiply (spec 1.1 P1-P7).

    R1 randomizer -> R2 STAB modify -> R3 typeMod doublings / floor-halvings ->
    R4 burn modify(0.5) -> R6 chained ModifyDamage (one truncation) -> R8 min 1
    -> R9 tr(d, 16) (which may legally return 0 AFTER the min-1 check)."""
    d = ps_randomizer(base_after_crit, r)  # R1
    if params.stab_4096 != 4096:  # R2
        d = ps_apply_modifier(d, params.stab_4096)
    levels = params.type_levels  # R3
    while levels > 0:
        d *= 2
        levels -= 1
    while levels < 0:
        d //= 2  # tr(d/2)
        levels += 1
    if params.burn:  # R4
        d = ps_apply_modifier(d, 2048)
    final = params.final_4096_crit if crit else params.final_4096_noncrit  # R6
    if final != 4096:
        d = ps_apply_modifier(d, final)
    if d == 0:  # R8 (gen != 5)
        return 1
    return d % 65536  # R9


def ps_sixteen_rolls(base_after_crit: int, params: PsTailParams, crit: bool) -> list[int]:
    """The TRUE 16-value PS multiset for a reconstructed state, ASCENDING:
    index j corresponds to PS's `random(16) == 15 - j`, so [0] is the 85% roll
    and [15] the max roll (the convention the engine's DamageRollSet uses)."""
    return [ps_roll(base_after_crit, 15 - j, params, crit) for j in range(16)]


def ficklebeam_membership(
    attacker, defender, ctx: "PsCombatContext", delta: int, lethal: bool, crit: bool
) -> tuple[bool, int, list[int], str | None]:
    """Both-arm exact membership for Fickle Beam.

    PS doubles the BASE POWER (data/moves.ts ficklebeam:
    `onBasePower ... randomChance(3,10) -> chainModify(2)`), which lands
    pre-formula; the engine instead doubles the final damage
    (generate_instructions.rs:5140), and `2 * damage != damage(2 * bp)` because
    the formula truncates in between.  The doubled arm is therefore a SECOND
    full derivation with the doubled-BP chain, never `2 * rolls`.

    Returns (member, nearest_delta, rolls_of_reported_arm, arm)."""
    base_rolls = derive_ps_damage(attacker, defender, "ficklebeam", ctx).rolls(crit)
    doubled_rolls = derive_ps_damage(
        attacker, defender, "ficklebeam", ctx, ficklebeam_doubled=True
    ).rolls(crit)
    member, nearest = check_roll_membership(base_rolls, delta, lethal)
    d_member, d_nearest = check_roll_membership(doubled_rolls, delta, lethal)
    if member and d_member:
        return True, 0, base_rolls, "ambiguous"
    if member:
        return True, 0, base_rolls, "base"
    if d_member:
        return True, 0, doubled_rolls, "doubled"
    if abs(d_nearest) < abs(nearest):
        return False, d_nearest, doubled_rolls, None
    return False, nearest, base_rolls, None


def check_roll_membership(
    rolls: list[int], delta: int, lethal: bool
) -> tuple[bool, int]:
    """Return (member, signed delta to the nearest roll; 0 when member).

    Membership is EXACT set membership: the observed delta must equal one of
    the 16 PS-exact values.  lethal/capped events observe only a lower bound
    (PS clamps damage to the defender's remaining HP), so their membership
    weakens to `some roll >= delta`."""
    if lethal:
        top = max(rolls)
        if top >= delta:
            return True, 0
        return False, delta - top
    if delta in rolls:
        return True, 0
    nearest = min(rolls, key=lambda r: (abs(r - delta), r))
    return False, delta - nearest


# ---------------------------------------------------------------------------
# PS-exact damage derivation from a reconstructed state
# ---------------------------------------------------------------------------
#
# This is an INDEPENDENT implementation of Showdown's damage chain (it does not
# consult the engine at all).  Everything it cannot derive with certainty is
# REFUSED (the event is excluded and counted under a named bucket) rather than
# approximated: an unmodelled damage-relevant ability/item/volatile, a base
# power callback that is not implemented, an unknown ability/item, or a
# chain-order ambiguity all abort the derivation.
#
# The refusal sets are generated from the PS sources themselves: every ability
# and item whose entry in data/abilities.ts / data/items.ts contains any
# damage-relevant handler (onBasePower / onModify{Atk,SpA,Def,SpD} and their
# onSource/onAny/onAlly variants / onModifyDamage / onSourceModifyDamage /
# onAnyModifyDamage / onModifySTAB / onEffectiveness / onModifyType /
# onWeatherModifyDamage) is listed below.  Names in the *_MODELLED sets are
# implemented here; every other listed name refuses.

_ABILITY_RELEVANT = frozenset(
    """adaptability aerilate analytic angershell battery battlearmor battlebond
    beadsofruin berserk blaze darkaura defeatist disguise dragonize dragonsmaw
    dryskin fairyaura filter firemane flareboost flashfire flowergift fluffy
    friendguard furcoat galvanize gluttony gorillatactics grasspelt guts
    hadronengine heatproof hugepower hustle iceface icescales illuminate
    infiltrator ironfist keeneye liquidvoice longreach magicguard marvelscale
    megalauncher megasol merciless mindseye minus moldbreaker mountaineer
    multiscale myceliummight neuroforce normalize orichalcumpulse overgrow
    pixilate plus poisonheal powerspot prismarmor propellertail protosynthesis
    punkrock purepower purifyingsalt quarkdrive reckless refrigerate ripen
    rivalry rockhead rockypayload sandforce scrappy serenegrace shadowshield
    sharpness sheerforce shellarmor skilllink slowstart sniper solarpower
    solidrock stakeout stalwart stancechange steelworker steelyspirit stench
    strongjaw sturdy superluck supremeoverlord swarm swordofruin tabletsofruin
    technician teravolt thickfat tintedlens torrent toughclaws toxicboost
    transistor turboblaze unseenfist vesselofruin waterbubble""".split()
)

# Listed as "relevant" by the source scan but provably unable to change a
# damage VALUE once the crit flag is observed (crit-rate only, doubles-only,
# multi-hit-count only, or a protocol-visible boost/forme trigger that the
# reconstruction already sees).
_ABILITY_NO_DAMAGE_EFFECT = frozenset(
    """angershell battery battlearmor battlebond berserk disguise friendguard
    gluttony illuminate keeneye magicguard merciless minus myceliummight
    plus poisonheal powerspot propellertail ripen rockhead serenegrace
    shellarmor skilllink stalwart stench sturdy superluck unseenfist""".split()
)

_ITEM_RELEVANT = frozenset(
    """adamantcrystal adamantorb assaultvest babiriberry blackbelt blackglasses
    charcoal chartiberry chilanberry choiceband choicescarf choicespecs
    chopleberry cobaberry colburberry cornerstonemask deepseascale deepseatooth
    dracoplate dragonfang dreadplate earthplate eviolite expertbelt fairyfeather
    fistplate flameplate focusband focussash griseouscore griseousorb habanberry
    hardstone hearthflamemask icicleplate insectplate ironball ironplate
    kasibberry kebiaberry kingsrock leek lifeorb lightball loadeddice
    luckypunch lustrousglobe lustrousorb magnet meadowplate metalcoat
    metalpowder metronome mindplate miracleseed muscleband mysticwater
    nevermeltice occaberry oddincense passhoberry payapaberry pinkbow pixieplate
    poisonbarb polkadotbow punchingglove razorclaw razorfang rindoberry
    rockincense roseincense roseliberry scopelens seaincense sharpbeak
    shucaberry silkscarf silverpowder skyplate softsand souldew spelltag
    splashplate spookyplate stick stoneplate tangaberry thickclub toxicplate
    twistedspoon vilevial wacanberry waveincense wellspringmask wiseglasses
    yacheberry zapplate""".split()
)

_ITEM_NO_DAMAGE_EFFECT = frozenset(
    """choicescarf focusband focussash kingsrock leek loadeddice luckypunch
    razorclaw razorfang scopelens stick""".split()
)

# Abilities whose only damage role is to suppress the DEFENDER's ability.
# (data/abilities.ts moldbreaker:2684 / teravolt:5199 / turboblaze:4975 -- each
# is an `onModifyMove(move) { move.ignoreAbility = true; }`.)  Moves that carry
# `ignoreAbility: true` in the dex do the same thing WITHOUT an ability, and are
# read straight off `mv["ignoreAbility"]` (fp's data/moves.json keeps the field):
# Sunsteel Strike, Moongeist Beam, Photon Geyser, Light That Burns The Sky and
# the three G-Max moves.
_ABILITY_IGNORES_ABILITY = frozenset(("moldbreaker", "teravolt", "turboblaze"))

# Abilities that Mold-Breaker-class suppression can actually turn off.
#
# sim/battle.ts:836-841 (runEvent) and :602-603 (singleEvent) skip a handler
# only when
#     effect.effectType === 'Ability' && effect.flags['breakable'] &&
#     this.suppressingAbility(effectHolder)
# so an ability with `flags: {}` is NEVER suppressed, however many
# Mold Breakers are on the field.  That is the whole point of this set: the
# four Ruin abilities, Prism Armor, Shadow Shield, Protosynthesis and Quark
# Drive all have `flags: {}` and must keep applying against a Mold Breaker /
# Photon Geyser / Sunsteel Strike attacker.  Generated by parsing every
# top-level entry of data/abilities.ts (320 entries) for `breakable: 1`.
_ABILITY_BREAKABLE = frozenset(
    """armortail aromaveil aurabreak battlearmor bigpecks bulletproof clearbody
    contrary damp dazzling disguise dryskin eartheater eelevate filter flashfire
    flowergift flowerveil fluffy friendguard furcoat goodasgold grasspelt
    guarddog heatproof heavymetal hypercutter iceface icescales illuminate
    immunity innerfocus insomnia keeneye leafguard levitate lightmetal
    lightningrod limber magicbounce magmaarmor marvelscale mindseye mirrorarmor
    motordrive mountaineer multiscale oblivious overcoat owntempo pastelveil
    punkrock purifyingsalt queenlymajesty rebound sandveil sapsipper shellarmor
    shielddust simple snowcloak solidrock soundproof stickyhold stormdrain
    sturdy suctioncups sweetveil tangledfeet telepathy terashell
    thermalexchange thickfat unaware vitalspirit voltabsorb waterabsorb
    waterbubble waterveil wellbakedbody whitesmoke windrider wonderguard
    wonderskin""".split()
)

# Volatiles that change a damage value.  Anything here that is not modelled
# refuses; anything NOT here is assumed damage-neutral.
_VOLATILE_MODELLED = frozenset(("flashfire", "slowstart", "charge", "glaiverush", "minimize"))
_VOLATILE_RELEVANT = frozenset(
    """flashfire slowstart charge glaiverush minimize protosynthesis quarkdrive
    tarshot powertrick dynamax laserfocus helpinghand magnetrise
    smackdown ingrain autotomize telekinesis dig dive fly bounce phantomforce
    shadowforce skydrop""".split()
)

_PARADOX_VOLATILE_PREFIXES = ("protosynthesis", "quarkdrive")

# EVENT-TIME STATE.  The reconstructed state this module derives from is the
# TURN-START snapshot, but PS resolves the two sides' actions in sequence, so a
# volatile that STARTS during the block is live for every later hit in the same
# block.  The protocol announces those starts (`|-start|<slot>|ability: Flash
# Fire`, `|-start|<slot>|protosynthesisspa`, ...), so they are recoverable
# exactly rather than approximable.
#
# Two disjoint treatments, both keyed on the `-start` line's position relative
# to the -damage line being asserted:
#   FOLDED   -- the volatile is one this module already models, so it is added
#               to the attacker's volatile list for that hit's derivation and
#               the exact boosted set is asserted.  (The ENGINE cross-check is
#               skipped for such a hit: the preview call takes the same stale
#               turn-start state, so it cannot arbitrate -- see
#               `engine_skipped_event_time_state`.)
#   CONFOUND -- damage-relevant but not modelled here; excluded outright, the
#               same treatment a turn-start occurrence of it would get from
#               `_check_modelled`.
# Anything in neither set is damage-neutral and ignored, exactly as at turn
# start.
_FOLDABLE_START_VOLATILES = frozenset(("flashfire", "slowstart"))


def _paradox_stat(pkmn) -> str | None:
    """The stat Protosynthesis / Quark Drive locked in when its volatile
    started.  PS stores it in `effectState.bestStat` and announces it as
    `-start <pkmn> protosynthesisatk`; fp keeps the announced volatile name
    verbatim (battle_modifier.py:1756), so the suffix IS the stat and no
    getBestStat reconstruction (which would need the boosts as of activation)
    is needed."""
    for vol in getattr(pkmn, "volatile_statuses", ()) or ():
        for prefix in _PARADOX_VOLATILE_PREFIXES:
            if vol.startswith(prefix) and len(vol) > len(prefix):
                return vol[len(prefix):]
    return None


_STOMP_CLASS_MOVES = frozenset(
    ("stomp", "steamroller", "bodyslam", "dragonrush", "flyingpress", "heatcrash", "heavyslam", "maliciousmoonsault")
)

_TYPE_BOOST_ITEMS = {
    "charcoal": "fire",
    "mysticwater": "water",
    "magnet": "electric",
    "miracleseed": "grass",
    "nevermeltice": "ice",
    "blackbelt": "fighting",
    "poisonbarb": "poison",
    "softsand": "ground",
    "sharpbeak": "flying",
    "twistedspoon": "psychic",
    "silverpowder": "bug",
    "hardstone": "rock",
    "spelltag": "ghost",
    "dragonfang": "dragon",
    "blackglasses": "dark",
    "metalcoat": "steel",
    "silkscarf": "normal",
    "fairyfeather": "fairy",
    "seaincense": "water",
    "waveincense": "water",
    "roseincense": "grass",
    "rockincense": "rock",
    "oddincense": "psychic",
    "flameplate": "fire",
    "splashplate": "water",
    "zapplate": "electric",
    "meadowplate": "grass",
    "icicleplate": "ice",
    "fistplate": "fighting",
    "toxicplate": "poison",
    "earthplate": "ground",
    "skyplate": "flying",
    "mindplate": "psychic",
    "insectplate": "bug",
    "stoneplate": "rock",
    "spookyplate": "ghost",
    "dracoplate": "dragon",
    "dreadplate": "dark",
    "ironplate": "steel",
    "pixieplate": "fairy",
}

# Items whose onBasePower is gated on the holder's species.
_SPECIES_TYPE_ITEMS = {
    "adamantorb": ("dialga", ("steel", "dragon")),
    "adamantcrystal": ("dialga", ("steel", "dragon")),
    "lustrousorb": ("palkia", ("water", "dragon")),
    "lustrousglobe": ("palkia", ("water", "dragon")),
    "griseousorb": ("giratina", ("ghost", "dragon")),
    "griseouscore": ("giratina", ("ghost", "dragon")),
    "souldew": (("latios", "latias"), ("psychic", "dragon")),
    "cornerstonemask": ("ogerponcornerstone", None),
    "wellspringmask": ("ogerponwellspring", None),
    "hearthflamemask": ("ogerponhearthflame", None),
}

# Moves whose base power is derived by a callback / whose own handlers change
# the chain.  Anything not in _MOVE_MODELLED refuses.
_MOVE_SPECIAL = frozenset(
    """acrobatics assurance avalanche beatup boltbeak crushgrip dragonenergy
    echoedvoice electroball eruption firepledge fishiousrend flail frustration
    furycutter grassknot grasspledge gyroball hardpress heatcrash heavyslam hex
    iceball infernalparade lastrespects lowkick payback pikapapow powertrip
    punishment pursuit ragefist return revenge reversal risingvoltage rollout
    round smellingsalts spitup stompingtantrum storedpower temperflare terablast
    tripleaxel triplekick trumpcard veeveevolley wakeupslap waterpledge
    watershuriken waterspout wringout barbbarrage brine charge collisioncourse
    electrodrift expandingforce facade ficklebeam fusionbolt fusionflare
    gravapple helpinghand knockoff lashout mefirst mistyexplosion mudsport
    psyblade retaliate solarbeam solarblade venoshock watersport aurawheel
    electrify hiddenpower iondeluge ivycudgel judgment multiattack naturalgift
    ragingbull revelationdance technoblast terastarstorm terrainpulse
    weatherball flyingpress freezedry tarshot thousandarrows struggle
    bodypress psyshock psystrike secretsword foulplay
    photongeyser shellsidearm lightthatburnsthesky""".split()
)

_MOVE_MODELLED = frozenset(
    """acrobatics boltbeak fishiousrend electroball gyroball grassknot lowkick
    heavyslam heatcrash hex powertrip storedpower brine facade knockoff
    venoshock collisioncourse electrodrift expandingforce psyblade risingvoltage
    solarbeam solarblade freezedry struggle bodypress psyshock psystrike
    secretsword weatherball eruption waterspout dragonenergy flail reversal
    lastrespects ragefist terablast ficklebeam revelationdance gravapple
    foulplay tripleaxel triplekick beatup aurawheel
    photongeyser shellsidearm lightthatburnsthesky""".split()
)

# Moves whose own `onModifyMove` can flip `move.category` from Special to
# Physical.  PS decides this ONCE, before the damage chain, off
# `getStat(stat, false, true)` -- BOOSTED (unboosted=false) but UNMODIFIED
# (unmodified=true, sim/pokemon.ts:596-638), i.e. the stored stat with only its
# boost stage applied: no ModifyAtk/ModifySpA event, no Choice Band, no Ruin,
# and (unmodified=true) no ModifyBoost event either.
_CATEGORY_SWITCH_MOVES = frozenset(
    ("photongeyser", "lightthatburnsthesky", "terablast", "shellsidearm")
)

# battle-actions.ts:1663-1666 -- the terastallized <60-BP floor is SKIPPED for a
# move whose DEX basePower is exactly 0 or 150 AND that carries a
# basePowerCallback ("Hard move.basePower check for moves like Dragon Energy
# that have variable BP").  Generated by scanning data/moves.ts for
# `basePower: 0|150` together with `basePowerCallback(`; the isNonstandard
# members are included because PS applies the same test to them.
_TERA_FLOOR_BP_CALLBACK_EXEMPT = frozenset(
    """beatup crushgrip dragonenergy electroball eruption flail grassknot
    gyroball hardpress heatcrash heavyslam lowkick reversal spitup waterspout
    frustration pikapapow punishment return trumpcard veeveevolley
    wringout""".split()
)

# `nature` is NOT carried by the synthetic corpus's `<game>.teams.json` sidecar
# (it writes species/level/ability/item/moves/evs/ivs/teraType/stats only), so
# fp's reconstruction keeps its default `serious`.  gen9 randbats sets are all
# neutral-natured (data/random-battles/gen9/teams.ts never assigns a nature),
# which is why every derivation is exact today; the sidecar's computed `stats`
# are also copied verbatim by `apply_exact_team`, so the nature only re-enters
# through `calculate_stats` on a forme change.  Should a non-neutral nature ever
# appear the reconstruction would silently compute the WRONG stats, so refuse
# instead of deriving.  (PS: data/natures.ts -- these five have plus === minus.)
_NEUTRAL_NATURES = frozenset(("serious", "hardy", "docile", "bashful", "quirky"))

# PS basePower values that foul-play's data/moves.json does not carry verbatim
# (verified by diffing every move's basePower/type/category against
# data/moves.ts: these are the only two divergences).
_PS_BASE_POWER_OVERRIDE = {"acrobatics": 55, "return": 0}

# PS moves whose `secondary` is function-valued (an inline onHit), which
# foul-play's data/moves.json exports as `null` -- Sheer Force keys on
# `move.secondaries`, so the raw json would mis-derive every Sheer Force hit
# with one of these.  Verified by diffing data/moves.ts against
# foul-play/data/moves.json: these 15 are the only disagreements, and there are
# none in the other direction.
_PS_HAS_SECONDARY_OVERRIDE = frozenset(
    """alluringvoice anchorshot burningjealousy ceaselessedge direclaw eeriespell
    firefang icefang saltcure spiritshackle stoneaxe throatchop thunderfang
    triattack triplearrows""".split()
)


def _has_secondary(mv: dict, move_id: str) -> bool:
    return bool(mv.get("secondary")) or move_id in _PS_HAS_SECONDARY_OVERRIDE

_UNREMOVABLE_ITEM_SPECIES = {
    "rustedsword": "zacian",
    "rustedshield": "zamazenta",
    "cornerstonemask": "ogerpon",
    "wellspringmask": "ogerpon",
    "hearthflamemask": "ogerpon",
    "adamantcrystal": "dialga",
    "lustrousglobe": "palkia",
    "griseouscore": "giratina",
    # Plates on Arceus (data/items.ts flameplate:2163-2167 and the 16 other
    # plates carry the identical gate):
    #   onTakeItem(item, pokemon, source) {
    #     if ((source && source.baseSpecies.num === 493) ||
    #         pokemon.baseSpecies.num === 493) return false;
    #   }
    # Knock Off's onBasePower (data/moves.ts:9971-9977) fires this exact
    # handler via `singleEvent('TakeItem', item, ...)` before granting the
    # 1.5x, so a plated Arceus forme takes UNBOOSTED Knock Off.  This was the
    # 258-event divergence class in the 50k resweep (every one an
    # arceus<type> + <type>plate defender).
    "dracoplate": "arceus",
    "dreadplate": "arceus",
    "earthplate": "arceus",
    "fistplate": "arceus",
    "flameplate": "arceus",
    "icicleplate": "arceus",
    "insectplate": "arceus",
    "ironplate": "arceus",
    "meadowplate": "arceus",
    "mindplate": "arceus",
    "pixieplate": "arceus",
    "skyplate": "arceus",
    "splashplate": "arceus",
    "spookyplate": "arceus",
    "stoneplate": "arceus",
    "toxicplate": "arceus",
    "zapplate": "arceus",
    # Memories on Silvally (data/items.ts bugmemory:704-709, num 773 gate,
    # identical on all 17):
    "bugmemory": "silvally",
    "darkmemory": "silvally",
    "dragonmemory": "silvally",
    "electricmemory": "silvally",
    "fairymemory": "silvally",
    "fightingmemory": "silvally",
    "firememory": "silvally",
    "flyingmemory": "silvally",
    "ghostmemory": "silvally",
    "grassmemory": "silvally",
    "groundmemory": "silvally",
    "icememory": "silvally",
    "poisonmemory": "silvally",
    "psychicmemory": "silvally",
    "rockmemory": "silvally",
    "steelmemory": "silvally",
    "watermemory": "silvally",
    # Drives on Genesect (data/items.ts burndrive:719-724, num 649 gate):
    "burndrive": "genesect",
    "chilldrive": "genesect",
    "dousedrive": "genesect",
    "shockdrive": "genesect",
    # Orbs (data/items.ts redorb:5173-5174 / blueorb:589-590,
    # baseSpecies.baseSpecies gate):
    "redorb": "groudon",
    "blueorb": "kyogre",
}
# NOTE deliberately absent: Sticky Hold.  Knock Off's boost gate is
# `singleEvent('TakeItem', item, target.itemState, ...)` -- a singleEvent on
# the ITEM's own handler only, so the ability's onTakeItem
# (data/abilities.ts:4615-4622) never runs there: a Sticky Hold target keeps
# its item (pokemon.ts takeItem -> runEvent('TakeItem') does consult it) but
# still takes the BOOSTED hit.

# Booster Energy (data/items.ts:643-646):
#   onTakeItem(item, source) {
#     if (source.baseSpecies.tags.includes("Paradox")) return false;
#   }
# The gate is the HOLDER's pokedex `tags`, and in THIS Showdown checkout
# exactly these 16 species carry `tags: ["Paradox"]` (data/pokedex.ts) --
# gougingfire / ragingbolt / ironboulder / ironcrown have NO tags entry at
# all here, so Booster Energy IS takeable from them (and Knock Off IS
# boosted); the engine's species_is_paradox (poke-engine
# src/genx/items.rs:316-344) deliberately adds those four per live-PS data
# and therefore disagrees with this oracle on that corner.
_PARADOX_TAGGED_SPECIES = frozenset(
    """greattusk screamtail brutebonnet fluttermane slitherwing sandyshocks
    irontreads ironbundle ironhands ironjugulis ironmoth ironthorns
    roaringmoon ironvaliant walkingwake ironleaves""".split()
)

_BOOST_TABLE = (1, 1.5, 2, 2.5, 3, 3.5, 4)

# Moves with `ignoreDefensive: true` in data/moves.ts (sacredsword:15565,
# darkestlariat:3334, chipaway:2431 -- gen5, unobtainable in gen9 but listed
# for completeness).  battle-actions.ts:1691,1697-1700:
#   const ignoreDefensive = !!(move.ignoreDefensive ||
#       (ignorePositiveDefensive && defBoosts > 0));
#   if (ignoreDefensive) { ... defBoosts = 0; }
# i.e. the flag zeroes the defender's (sp)def stage in BOTH directions --
# unlike a crit, which only neutralises POSITIVE defensive stages -- and it
# applies on the non-crit path too.  foul-play's data/moves.json does not
# export the field, hence the explicit list.  (`ignoreEvasion` never enters
# the damage chain; no gen9 move carries `ignoreOffensive`.)
_IGNORE_DEFENSIVE_MOVES = frozenset(("sacredsword", "darkestlariat", "chipaway"))

_SUNS = ("sunnyday", "desolateland")
_RAINS = ("raindance", "primordialsea")

# the exact `case` labels of Weather Ball's onModifyMove switch
# (data/moves.ts:20714-20731).  Deliberately an explicit membership test rather
# than a truthiness test on the weather: there is no `default:` arm, so clear
# weather (protocol `|-weather|none`) and Delta Stream leave base power alone.
_WEATHERBALL_DOUBLING = frozenset(_SUNS + _RAINS + ("sandstorm", "hail", "snowscape"))


class PsRefusal(Exception):
    """Raised when the PS-exact chain cannot be derived with certainty."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass
class PsCombatContext:
    weather: str | None = None
    terrain: str | None = None
    defender_screens: frozenset = frozenset()
    attacker_fainted_allies: int = 0
    attacker_moves_first: bool | None = None
    gravity: bool = False
    # The replay driver reconstructs the OPPONENT's HP from the protocol's /100
    # display, so the attacker's HP is only known to within one displayed
    # percent.  Anything that reads the attacker's HP as a ratio then either
    # refuses (continuous dependence: Eruption's base power) or requires the
    # gate to be decided outside the uncertainty band (threshold dependence:
    # Blaze / Defeatist).
    attacker_hp_exact: bool = True
    # PS's `move.hit`, 1-indexed (battle-actions.ts:889 `move.hit = hit`).  Set
    # only when the observed -damage lines of the use can be indexed
    # unambiguously (their count matches the |-hitcount| PS printed); None
    # otherwise, which makes every hit-index-dependent derivation refuse.
    hit_index: int | None = None
    # Beat Up's per-hit base powers, in PS's order (data/moves.ts beatup:
    # `move.allies = side.pokemon.filter(a => a === user || (!a.fainted &&
    # !a.status))`, then `5 + floor(setSpecies.baseStats.atk / 10)` per shift).
    beatup_bp: tuple = ()
    # This hit is hit 2 of a Parental Bond pair (battle-actions.ts:1738).
    parental_bond_hit2: bool = False
    # PS already ran `runEvent('BeforeMove')` on the DEFENDER this turn, before this
    # hit resolved (the defender moved -- or tried to -- first).  Volatiles whose
    # condition removes itself in an onBeforeMove handler are gone by now; Glaive
    # Rush's drawback is the one such volatile this module models.
    defender_before_move_ran: bool = False


@dataclass
class PsDamage:
    """A fully derived PS damage chain for one attacker/defender/move."""

    base_noncrit: int
    base_crit: int
    params: PsTailParams
    base_power: int
    attack: int
    defense: int

    def rolls(self, crit: bool) -> list[int]:
        base = self.base_crit if crit else self.base_noncrit
        return ps_sixteen_rolls(base, self.params, crit)


_NO_ITEM_VALUES = frozenset(("", "none", "None"))


def _item(pkmn):
    """The holder's item as a normalised id, or None when it holds nothing.

    `battle_to_poke_engine_state` MUTATES `pkmn.item` to the STRING "None" for
    an itemless mon (fp/search/poke_engine_helpers.py:135), and it runs before
    this module's derivation, so a raw truthiness test on `.item` reads an
    itemless mon as holding something -- which silently mis-derived Acrobatics
    (55 BP instead of 110) and Knock Off (1.5x applied against a bare
    target)."""
    item = getattr(pkmn, "item", None)
    if item is None or item in _NO_ITEM_VALUES:
        return None
    return item


def _tie_groups(handlers):
    """`handlers` is a list of (priority, kind, mod).  Yields every ordering
    consistent with PS's priority-descending sort (PS breaks equal-priority
    ties by speed/effect order, which this module does not reconstruct), so a
    caller can detect when the tie-break would change the result."""
    ordered = sorted(handlers, key=lambda h: -h[0])
    groups = []
    for h in ordered:
        if groups and groups[-1][0][0] == h[0]:
            groups[-1].append(h)
        else:
            groups.append([h])
    from itertools import permutations, product

    total = 1
    for g in groups:
        total *= len(g) ** 2  # cheap upper bound on the permutation count
    if total > 64:
        raise PsRefusal("ps_chain_order_ambiguous")
    for combo in product(*[list(permutations(g)) for g in groups]):
        yield [h for grp in combo for h in grp]


def _run_event(value: int, handlers) -> int:
    """One PS `runEvent`: handlers sorted priority-descending; `direct`
    handlers replace the relayVar immediately (`return this.modify(v, x)`),
    `chain` handlers accumulate into event.modifier which `finalModify` applies
    once at the end.  Refuses if an equal-priority tie-break would change the
    result."""
    if not handlers:
        return value
    results = set()
    for order in _tie_groups(handlers):
        v = value
        acc = 4096
        for _, kind, mod in order:
            if kind == "direct":
                v = ps_apply_modifier(v, mod)
            else:
                acc = (acc * mod + 2048) >> 12
        results.add(ps_apply_modifier(v, acc))
        if len(results) > 1:
            raise PsRefusal("ps_chain_order_ambiguous")
    return results.pop()


def _ps_types(pkmn) -> list[str]:
    """`pokemon.getTypes()`: a terastallized mon IS its tera type."""
    if getattr(pkmn, "terastallized", False) and pkmn.tera_type:
        if pkmn.tera_type == "stellar":
            raise PsRefusal("ps_unmodelled_stellar")
        return [pkmn.tera_type]
    return [t for t in pkmn.types if t and t != "typeless"]


def _base_types(pkmn) -> list[str]:
    """`pokemon.getTypes(false, true)` -- types ignoring Terastallization, which
    STAB still consults (battle-actions.ts:1766).

    PS's `preterastallized` types are the LIVE `pokemon.types` array, i.e. the
    types as any earlier type change left them -- NOT the dex species' types.
    fp's `-terastallize` handler (battle_modifier.py:1701-1713) deliberately
    leaves `pkmn.types` alone, so `pkmn.types` already IS
    `getTypes(false, true)` and must be used verbatim.

    Reading the DEX here instead (the previous behaviour) diverges whenever the
    mon changed type before terastallizing: a Protean Meowscarada that used
    Toxic Spikes is `['poison']` in PS, and after a Grass tera PS gives its
    Flower Trick only the 1.5x STAB (`terastallized === type` but
    `getTypes(false, true)` does NOT include Grass) and its Knock Off no STAB at
    all -- while the dex answer ['grass','dark'] wrongly awarded 2.0x and 1.5x
    respectively (5 HARD divergences in the 120-game acceptance sample:
    synth26954 T3/T5/T6, synth25216 T11/T12).

    The dex is consulted only as a last resort, for a mon whose live type list
    is empty (nothing in gen9 singles produces one)."""
    live = [t for t in pkmn.types if t and t != "typeless"]
    if live:
        return live
    entry = pokedex.get(pkmn.name) or {}
    return [normalize_name(t) for t in entry.get("types", ())]


def _grounded(pkmn, ctx: PsCombatContext, ability_suppressed: bool = False) -> bool:
    # `ability_suppressed` mirrors PS sim/pokemon.ts:2161
    #     if (this.hasAbility(['levitate','eelevate']) && !this.battle.suppressingAbility(this))
    #         return null;
    # -- under a Mold-Breaker-class attacker `suppressingAbility` (sim/battle.ts:
    # 365-368) is true for the DEFENDER, so the Levitate branch is skipped and the
    # mon falls through to `item !== 'airballoon'`, i.e. it IS grounded.
    volatiles = set(getattr(pkmn, "volatile_statuses", ()) or ())
    if ctx.gravity or "smackdown" in volatiles or "ingrain" in volatiles:
        return True
    if "magnetrise" in volatiles or "telekinesis" in volatiles:
        return False
    if _item(pkmn) == "airballoon":
        return False
    if pkmn.ability == "levitate" and not ability_suppressed:
        return False
    return "flying" not in _ps_types(pkmn)


def _weight_hg(pkmn) -> int:
    if pkmn.ability in ("heavymetal", "lightmetal") or _item(pkmn) == "floatstone":
        raise PsRefusal("ps_unmodelled_weight_modifier")
    if "autotomize" in set(getattr(pkmn, "volatile_statuses", ()) or ()):
        raise PsRefusal("ps_unmodelled_weight_modifier")
    try:
        kg = pokedex[pkmn.name]["weightkg"]
    except (KeyError, TypeError):
        raise PsRefusal("ps_unknown_weight")
    return max(1, int(kg * 10))


def _hp_ratio_gate(pkmn, numerator: int, denominator: int, exact: bool) -> bool:
    """PS gates like `attacker.hp <= attacker.maxhp / 3` (Blaze) or `/ 2`
    (Defeatist).  With an inexact (percent-derived) HP the gate is only
    trustworthy when the reconstructed HP is further from the boundary than the
    reconstruction's own uncertainty (one displayed percent, plus 1 for the
    rounding); inside that band the derivation refuses."""
    lhs = pkmn.hp * denominator
    rhs = pkmn.max_hp * numerator
    if not exact:
        tolerance = denominator * (pkmn.max_hp // 100 + 1)
        if abs(lhs - rhs) <= tolerance:
            raise PsRefusal("ps_attacker_hp_gate_ambiguous")
    return lhs <= rhs


def _boosted_stat(pkmn, stat_key: str) -> int:
    """`pokemon.getStat(stat, unboosted=false, unmodified=true)`: the stored
    stat with its boost stage applied and nothing else."""
    boost = max(-6, min(6, pkmn.boosts.get(stat_key, 0)))
    value = pkmn.stats[stat_key]
    if boost >= 0:
        return int(value * _BOOST_TABLE[boost])
    return int(value // _BOOST_TABLE[-boost])


def _category_switch(
    move_id: str, attacker, defender, category: str
) -> tuple[str, bool]:
    """PS's `onModifyMove` Special->Physical category switches.

    Returns `(category, coinflip)`.  `coinflip` is True only for a Shell Side
    Arm whose two arms tie, where PS decides with an unobservable `randomChance
    (1, 2)`; the caller derives BOTH arms and refuses only if they differ.

    Run as `singleEvent('ModifyMove', move, ...)` at battle-actions.ts:431 --
    BEFORE the damage chain, so the switched category is what selects the
    Atk/Def stats, the ModifyAtk-vs-ModifySpA event, Reflect-vs-Light Screen,
    the burn halving, Muscle Band vs Wise Glasses, Ice Scales, ... everything
    downstream in this module reads `category`.

    Every arm compares `getStat(stat, false, true)` (sim/pokemon.ts:596-638):
    BOOSTED (unboosted=false) but UNMODIFIED (unmodified=true) -- the stored
    stat with only its boost stage applied, so no ModifyAtk/ModifySpA event, no
    Choice Band, no Ruin ability, and no ModifyBoost event participates.
    """
    if category != "special":
        return category, False
    atk = _boosted_stat(attacker, constants.ATTACK)
    spa = _boosted_stat(attacker, constants.SPECIAL_ATTACK)

    if move_id in ("photongeyser", "lightthatburnsthesky"):
        # data/moves.ts:13348-13350 (photongeyser) / :10369-10371
        # (lightthatburnsthesky):
        #   onModifyMove(move, pokemon) {
        #     if (pokemon.getStat('atk', false, true) >
        #         pokemon.getStat('spa', false, true)) move.category = 'Physical';
        #   }
        # STRICTLY greater: a tie stays Special.  There is no target term, so
        # unlike Shell Side Arm this is never a coin flip.
        return ("physical" if atk > spa else category), False

    if move_id == "terablast":
        # data/moves.ts:19223-19228 -- the same comparison, gated on
        # `pokemon.terastallized`.
        if getattr(attacker, "terastallized", False) and atk > spa:
            return "physical", False
        return category, False

    if move_id == "shellsidearm":
        # data/moves.ts:16223-16234 -- compares the two arms' PRE-modifier base
        # damage at a hard-coded 90 BP, using getStat(...,false,true) on BOTH
        # sides:
        #   const physical = floor(floor(floor(floor(2*L/5+2) * 90 * atk) / def) / 50);
        #   const special  = floor(floor(floor(floor(2*L/5+2) * 90 * spa) / spd) / 50);
        #   if (physical > special || (physical === special && randomChance(1,2)))
        #     { move.category = 'Physical'; move.flags.contact = 1; }
        level = attacker.level

        def _arm(off: int, deff: int) -> int:
            return ps_trunc(
                ps_trunc(ps_trunc(ps_trunc(2 * level / 5 + 2) * 90 * off) / deff) / 50
            )

        physical = _arm(atk, _boosted_stat(defender, constants.DEFENSE))
        special = _arm(spa, _boosted_stat(defender, constants.SPECIAL_DEFENSE))
        if physical == special:
            return category, True
        return ("physical" if physical > special else category), False

    return category, False


def _positive_boosts(pkmn) -> int:
    return sum(v for v in pkmn.boosts.values() if v > 0)


def _effective_weather(ctx: PsCombatContext, attacker, defender) -> str | None:
    """`pokemon.effectiveWeather()`, normalized so "no weather" is falsy.

    PS's `field.weather` is `''` once weather is cleared; the protocol spells
    that state `|-weather|none`.  `battle_modifier.weather()` now maps it to
    None, but normalize defensively here too: every downstream weather test is
    either an equality against a real weather id (harmlessly False for the
    literal "none") or a TRUTHINESS test -- Weather Ball's base-power doubling
    (data/moves.ts:20714-20731) and the Utility Umbrella refusal below -- and
    the truthiness ones must not fire in clear weather."""
    if attacker.ability in ("airlock", "cloudnine") or defender.ability in (
        "airlock",
        "cloudnine",
    ):
        return None
    if not ctx.weather or ctx.weather == "none":
        return None
    if _item(attacker) == "utilityumbrella" or _item(defender) == "utilityumbrella":
        raise PsRefusal("ps_unmodelled_utilityumbrella")
    return ctx.weather


def _check_known(pkmn, side: str) -> None:
    if getattr(pkmn, "transformed_into", None) or "transform" in (
        getattr(pkmn, "volatile_statuses", ()) or ()
    ):
        # Transform/Imposter copies the target's species, types, stats, ability
        # and moves onto the user at copy time, while item/status/HP stay the
        # user's own.  The sidecar describes the BASE set, and fp's copy is
        # taken from its own (possibly inferred) view of the target, so the live
        # set is not reconstructible with certainty.
        raise PsRefusal("ps_transformed_" + side)
    if not pkmn.ability:
        raise PsRefusal("ps_unknown_ability_" + side)
    if getattr(pkmn, "item", None) == constants.UNKNOWN_ITEM:
        raise PsRefusal("ps_unknown_item_" + side)
    # NATURE LATENCY (documented, currently vacuous):  the sidecar carries no
    # `nature` field, so `apply_exact_team` cannot fill one and fp's default
    # `serious` survives.  That is exact for gen9 randbats -- the generator
    # never assigns a nature, so every set is neutral -- and the sidecar's
    # computed `stats` are copied verbatim anyway; the nature only re-enters
    # the numbers through `calculate_stats` on a forme change.  If a
    # non-neutral nature ever reaches this module (another format, a sidecar
    # that starts carrying natures, an fp inference), the reconstructed stats
    # are no longer trustworthy -- refuse rather than derive a wrong set.
    nature = normalize_name(getattr(pkmn, "nature", "") or "serious")
    if nature not in _NEUTRAL_NATURES:
        raise PsRefusal("ps_non_neutral_nature_" + side)
    for vol in set(getattr(pkmn, "volatile_statuses", ()) or ()):
        if vol in _PARADOX_VOLATILE_PREFIXES:
            # a bare 'protosynthesis'/'quarkdrive' volatile carries no boosted
            # stat, and PS's bestStat was fixed when the volatile started
            raise PsRefusal("ps_unmodelled_volatile_" + vol)
        if vol.startswith(_PARADOX_VOLATILE_PREFIXES):
            continue
        if vol in _VOLATILE_RELEVANT and vol not in _VOLATILE_MODELLED:
            raise PsRefusal("ps_unmodelled_volatile_" + vol)


def _check_modelled(pkmn, side: str) -> None:
    ability = pkmn.ability
    if (
        ability in _ABILITY_RELEVANT
        and ability not in _ABILITY_NO_DAMAGE_EFFECT
        and ability not in _ABILITY_MODELLED
    ):
        raise PsRefusal("ps_unmodelled_ability_" + ability)
    item = _item(pkmn)
    if item in _ITEM_RELEVANT and item not in _ITEM_NO_DAMAGE_EFFECT and item not in _ITEM_MODELLED:
        raise PsRefusal("ps_unmodelled_item_" + item)


_ABILITY_MODELLED = frozenset(
    """adaptability aerilate analytic beadsofruin blaze defeatist dragonsmaw
    dryskin filter flareboost flashfire fluffy furcoat galvanize gorillatactics
    grasspelt guts hadronengine heatproof hugepower hustle icescales infiltrator
    ironfist liquidvoice longreach marvelscale megalauncher mindseye moldbreaker
    multiscale neuroforce normalize orichalcumpulse overgrow pixilate prismarmor
    punkrock purepower purifyingsalt reckless refrigerate rockypayload sandforce
    scrappy shadowshield sharpness sheerforce slowstart sniper solarpower
    solidrock stakeout steelworker strongjaw supremeoverlord swarm swordofruin
    tabletsofruin technician teravolt thickfat tintedlens torrent toughclaws
    toxicboost transistor turboblaze vesselofruin waterbubble protosynthesis
    quarkdrive""".split()
)

_ITEM_MODELLED = (
    frozenset(
        """lifeorb expertbelt muscleband wiseglasses assaultvest eviolite
        choiceband choicespecs punchingglove lightball thickclub deepseatooth
        deepseascale metalpowder""".split()
    )
    | frozenset(_TYPE_BOOST_ITEMS)
    | frozenset(_SPECIES_TYPE_ITEMS)
)

_NFE_CACHE: dict = {}


def _is_nfe(name: str) -> bool:
    if name not in _NFE_CACHE:
        entry = pokedex.get(name) or {}
        _NFE_CACHE[name] = bool(entry.get("evos"))
    return _NFE_CACHE[name]


def _move_type(mv: dict, move_id: str, attacker, ctx, weather) -> str:
    """PS ModifyType: type-changing abilities and the handful of modelled
    type-changing moves."""
    mtype = mv["type"]
    if move_id == "struggle":
        return "???"
    # Aura Wheel's static dex type is Electric, but PS rewrites it from the
    # user's current forme (data/moves.ts:801-806).
    if move_id == "aurawheel":
        return (
            "dark"
            if normalize_name(attacker.name) == "morpekohangry"
            else "electric"
        )
    if move_id == "terablast":
        return attacker.tera_type if getattr(attacker, "terastallized", False) else mtype
    if move_id == "weatherball":
        if weather in _SUNS:
            return "fire"
        if weather in _RAINS:
            return "water"
        if weather == "sandstorm":
            return "rock"
        if weather in ("hail", "snowscape"):
            return "ice"
        return mtype
    if move_id == "revelationdance":
        types = _ps_types(attacker)
        return types[0] if types else mtype
    ability = attacker.ability
    ate = {
        "aerilate": "flying",
        "pixilate": "fairy",
        "refrigerate": "ice",
        "galvanize": "electric",
    }
    if ability in ate and mtype == "normal":
        return ate[ability]
    if ability == "normalize":
        return "normal"
    if ability == "liquidvoice" and mv.get("flags", {}).get("sound"):
        return "water"
    return mtype


def _type_changer_boosted(mv: dict, move_id: str, attacker, mtype: str) -> bool:
    # The MOVE's own onModifyMove runs BEFORE the ability ModifyType event
    # (battle-actions.ts:431 vs :438), so Struggle is already '???' by the time
    # -ate abilities look at it: no type change, no 1.2x base-power boost.
    if mtype == "???" or move_id == "struggle":
        return False
    ability = attacker.ability
    if ability in ("aerilate", "pixilate", "refrigerate", "galvanize"):
        return mv["type"] == "normal" and mtype != "normal"
    if ability == "normalize":
        return move_id not in ("hiddenpower", "struggle") and mtype == "normal"
    return False


def _base_power_pre_event(mv, move_id, attacker, defender, ctx, mtype, category, weather):
    """PS `move.basePower` / `basePowerCallback` (battle-actions.ts:1618-1622)."""
    bp = _PS_BASE_POWER_OVERRIDE.get(move_id, mv.get("basePower"))
    if bp is None:
        raise PsRefusal("ps_no_base_power")
    if move_id in _MOVE_SPECIAL and move_id not in _MOVE_MODELLED:
        raise PsRefusal("ps_unmodelled_move_" + move_id)
    if move_id == "acrobatics":
        return bp * 2 if _item(attacker) is None else bp
    if move_id in ("hex", "infernalparade"):
        # data/moves.ts hex:8610-8620 / infernalparade:9546-9553 —
        # `if (target.status || target.hasAbility('comatose'))`. Comatose never
        # writes `pokemon.status` (data/abilities.ts:588 "Permanent sleep status
        # implemented in the relevant sleep-checking effects"), so a status-only
        # test understates the BP by half against a Comatose holder and the
        # reference roll set comes out at half the real damage (g74 synth789767
        # T4: Hex into tera-Ghost Komala observed 140, reference max_roll 80).
        # Comatose is `cantsuppress` and not `breakable`, so no Mold Breaker /
        # Neutralizing Gas qualifier is needed.
        return bp * 2 if (defender.status or defender.ability == "comatose") else bp
    if move_id in ("boltbeak", "fishiousrend"):
        if ctx.attacker_moves_first is None:
            raise PsRefusal("ps_unknown_move_order")
        return bp * 2 if ctx.attacker_moves_first else bp
    if move_id in ("storedpower", "powertrip"):
        return bp + 20 * _positive_boosts(attacker)
    if move_id in ("tripleaxel", "triplekick"):
        # data/moves.ts tripleaxel/triplekick: `return 20 * move.hit` /
        # `10 * move.hit`, with `move.hit` the 1-indexed hit counter
        # (battle-actions.ts:889).  `multiaccuracy` rolls accuracy per hit and
        # BREAKS the loop on the first miss, so the landed hits are always the
        # contiguous prefix 1..n -- which is what makes the observed -damage
        # lines indexable at all.
        if ctx.hit_index is None:
            raise PsRefusal("ps_multihit_index_ambiguous")
        return bp * ctx.hit_index
    if move_id == "beatup":
        # data/moves.ts beatup: one hit per entry of `move.allies`, each hit
        # shifting the next ally off the front, BP = 5 + floor(base Atk / 10).
        if ctx.hit_index is None:
            raise PsRefusal("ps_multihit_index_ambiguous")
        if not ctx.beatup_bp:
            raise PsRefusal("ps_unknown_beatup_party")
        if ctx.hit_index > len(ctx.beatup_bp):
            raise PsRefusal("ps_beatup_party_desync")
        return ctx.beatup_bp[ctx.hit_index - 1]
    if move_id == "electroball":
        ratio = attacker.stats[constants.SPEED] // max(1, defender.stats[constants.SPEED])
        return [40, 60, 80, 120, 150][min(ratio, 4)]
    if move_id == "gyroball":
        power = (25 * defender.stats[constants.SPEED]) // max(1, attacker.stats[constants.SPEED]) + 1
        return min(power, 150)
    if move_id in ("grassknot", "lowkick"):
        w = _weight_hg(defender)
        for threshold, power in ((2000, 120), (1000, 100), (500, 80), (250, 60), (100, 40)):
            if w >= threshold:
                return power
        return 20
    if move_id in ("heavyslam", "heatcrash"):
        tw = _weight_hg(defender)
        pw = _weight_hg(attacker)
        for mult, power in ((5, 120), (4, 100), (3, 80), (2, 60)):
            if pw >= tw * mult:
                return power
        return 40
    if move_id in ("eruption", "waterspout", "dragonenergy"):
        if not ctx.attacker_hp_exact:
            raise PsRefusal("ps_attacker_hp_inexact")
        return max(1, (150 * attacker.hp) // attacker.max_hp)
    if move_id in ("flail", "reversal"):
        if not ctx.attacker_hp_exact:
            raise PsRefusal("ps_attacker_hp_inexact")
        ratio = (attacker.hp * 48) // attacker.max_hp
        for threshold, power in ((33, 20), (17, 40), (10, 80), (5, 100), (2, 150)):
            if ratio > threshold:
                return power
        return 200
    if move_id == "lastrespects":
        return min(bp + 50 * ctx.attacker_fainted_allies, 5050)
    if move_id == "ragefist":
        return min(bp + 50 * getattr(attacker, "times_attacked", 0), 350)
    if move_id == "risingvoltage":
        return bp * 2 if (ctx.terrain == "electricterrain" and _grounded(defender, ctx)) else bp
    if move_id == "terablast":
        return bp
    if move_id == "weatherball":
        # data/moves.ts:20714-20731 -- an explicit switch over
        # `pokemon.effectiveWeather()` with a case per REAL weather; there is no
        # default arm, so clear weather (and the unmodelled-by-this-switch
        # deltastream) leaves base power alone.  `weather` here is already
        # `_effective_weather`, which is None when cleared.
        return bp * 2 if weather in _WEATHERBALL_DOUBLING else bp
    return bp


def _base_power_handlers(
    mv, move_id, attacker, defender, ctx, mtype, category, weather, def_ability, ficklebeam_doubled=False
):
    """Every gen9-singles onBasePower handler, with PS's priorities."""
    flags = mv.get("flags", {}) or {}
    contact = bool(flags.get("contact")) and attacker.ability != "longreach"
    if _item(attacker) == "punchingglove" and flags.get("punch"):
        contact = False
    handlers = []
    a = attacker.ability

    # attacker abilities
    if a in ("aerilate", "pixilate", "refrigerate", "galvanize", "normalize") and _type_changer_boosted(
        mv, move_id, attacker, mtype
    ):
        handlers.append((23, "chain", 4915))
    if a == "ironfist" and flags.get("punch"):
        handlers.append((23, "chain", 4915))
    if a == "reckless" and (mv.get("recoil") or move_id in ("highjumpkick", "jumpkick")):
        handlers.append((23, "chain", 4915))
    if a == "analytic":
        if ctx.attacker_moves_first is None:
            raise PsRefusal("ps_unknown_move_order")
        if not ctx.attacker_moves_first:
            handlers.append((21, "chain", 5325))
    if a == "sandforce" and weather == "sandstorm" and mtype in ("rock", "ground", "steel"):
        handlers.append((21, "chain", 5325))
    if a == "sheerforce" and _has_secondary(mv, move_id):
        handlers.append((21, "chain", 5325))
    if a == "supremeoverlord":
        # PS captures `fallen` ONCE, at switch-in (abilities.ts supremeoverlord
        # onStart) and announces it as `-start <pkmn> fallenN [silent]`; fp
        # keeps that volatile verbatim, so the live fainted count must NOT be
        # used (it moves mid-battle while the ability's boost does not).
        fallen = _fallen_count(attacker)
        if fallen:
            handlers.append((21, "chain", (4096, 4506, 4915, 5325, 5734, 6144)[fallen]))
    if a == "toughclaws" and contact:
        handlers.append((21, "chain", 5325))
    if a == "flareboost" and attacker.status == constants.BURN and category == "special":
        handlers.append((19, "chain", 6144))
    if a == "toxicboost" and attacker.status in (constants.POISON, constants.TOXIC) and category == "physical":
        handlers.append((19, "chain", 6144))
    if a == "megalauncher" and flags.get("pulse"):
        handlers.append((19, "chain", 6144))
    if a == "sharpness" and flags.get("slicing"):
        handlers.append((19, "chain", 6144))
    if a == "strongjaw" and flags.get("bite"):
        handlers.append((19, "chain", 6144))
    if a == "punkrock" and flags.get("sound"):
        handlers.append((7, "chain", 5325))

    # defender ability (onSourceBasePower)
    if def_ability == "dryskin" and mtype == "fire":
        handlers.append((17, "chain", 5120))

    # attacker items
    item = _item(attacker)
    if item == "muscleband" and category == "physical":
        handlers.append((16, "chain", 4505))
    if item == "wiseglasses" and category == "special":
        handlers.append((16, "chain", 4505))
    if item == "punchingglove" and flags.get("punch"):
        handlers.append((23, "chain", 4506))
    if item in _TYPE_BOOST_ITEMS and _TYPE_BOOST_ITEMS[item] == mtype:
        handlers.append((15, "chain", 4915))
    if item in _SPECIES_TYPE_ITEMS:
        species, types = _SPECIES_TYPE_ITEMS[item]
        holders = (species,) if isinstance(species, str) else species
        if any(attacker.name.startswith(h) for h in holders) and (types is None or mtype in types):
            handlers.append((15, "chain", 4915))

    # field
    # `def_ability` above is already Mold-Breaker-resolved (levitate is in
    # _ABILITY_BREAKABLE), so Levitate surviving there means it was NOT suppressed;
    # Levitate present on the mon but gone from `def_ability` is exactly PS's
    # `battle.suppressingAbility(defender)` (sim/battle.ts:365-368) for isGrounded
    # (sim/pokemon.ts:2161).  The attacker-side `_grounded(attacker, ctx)` calls
    # below intentionally stay unsuppressed: gen>=8 suppressingAbility requires
    # `activePokemon !== target`, so a move never breaks its own user's ability.
    def_levitate_suppressed = defender.ability == "levitate" and def_ability != "levitate"
    if ctx.terrain == "grassyterrain":
        if move_id in ("earthquake", "bulldoze", "magnitude") and _grounded(
            defender, ctx, ability_suppressed=def_levitate_suppressed
        ):
            handlers.append((6, "chain", 2048))
        if mtype == "grass" and _grounded(attacker, ctx):
            handlers.append((6, "chain", 5325))
    elif ctx.terrain == "electricterrain":
        if mtype == "electric" and _grounded(attacker, ctx):
            handlers.append((6, "chain", 5325))
    elif ctx.terrain == "psychicterrain":
        if mtype == "psychic" and _grounded(attacker, ctx):
            handlers.append((6, "chain", 5325))
    elif ctx.terrain == "mistyterrain":
        if mtype == "dragon" and _grounded(defender, ctx):
            handlers.append((6, "chain", 2048))

    # attacker volatiles
    volatiles = set(getattr(attacker, "volatile_statuses", ()) or ())
    if "charge" in volatiles and mtype == "electric":
        handlers.append((9, "chain", 8192))

    # the move's own onBasePower (priority 0)
    if move_id == "facade" and attacker.status and attacker.status != constants.SLEEP:
        handlers.append((0, "chain", 8192))
    elif move_id == "brine" and defender.hp * 2 <= defender.max_hp:
        handlers.append((0, "chain", 8192))
    elif move_id == "venoshock" and defender.status in (constants.POISON, constants.TOXIC):
        handlers.append((0, "chain", 8192))
    elif move_id == "knockoff" and _knockable_item(defender):
        handlers.append((0, "chain", 6144))
    elif move_id in ("solarbeam", "solarblade") and weather in ("raindance", "primordialsea", "sandstorm", "hail", "snowscape"):
        handlers.append((0, "chain", 2048))
    elif move_id == "expandingforce" and ctx.terrain == "psychicterrain" and _grounded(attacker, ctx):
        handlers.append((0, "chain", 6144))
    elif move_id == "psyblade" and ctx.terrain == "electricterrain":
        handlers.append((0, "chain", 6144))
    elif move_id == "gravapple" and ctx.gravity:
        handlers.append((0, "chain", 6144))
    elif move_id == "ficklebeam" and ficklebeam_doubled:
        # PS's 30% arm: onBasePower chainModify(2) -- a BASE POWER double
        handlers.append((0, "chain", 8192))
    elif move_id in ("collisioncourse", "electrodrift"):
        # data/moves.ts:2634-2640 (collisioncourse) / :4620-4626 (electrodrift):
        #   onBasePower(bp, source, target, move) {
        #     if (target.runEffectiveness(move) > 0) return this.chainModify([5461, 4096]);
        #   }
        # The gate is the move's TYPE EFFECTIVENESS against the target
        # (`runEffectiveness`, sim/pokemon.ts:2214 -- the same summed typeMod
        # the damage chain later doublings/halvings run on), NOT the observed
        # `-supereffective` line, and it chains 5461/4096 into the BasePower
        # event at the move's own priority 0.
        if _type_levels(mtype, defender, move_id, attacker.ability) > 0:
            handlers.append((0, "chain", 5461))
    return handlers, contact


def _fallen_count(pkmn) -> int:
    for vol in getattr(pkmn, "volatile_statuses", ()) or ():
        if vol.startswith("fallen") and vol[6:].isdigit():
            return min(int(vol[6:]), 5)
    return 0


def _knockable_item(defender) -> bool:
    item = _item(defender)
    if not item or item == constants.UNKNOWN_ITEM:
        return False
    holder = _UNREMOVABLE_ITEM_SPECIES.get(item)
    if holder and defender.name.startswith(holder):
        return False
    if item == "boosterenergy" and defender.name in _PARADOX_TAGGED_SPECIES:
        return False
    return True


def _stat_handlers(which, attacker, defender, mtype, category, ctx, weather, def_ability, atk_ability):
    """onModifyAtk / onModifySpA / onModifyDef / onModifySpD handler sets, with
    PS's priorities.  `which` is the PS stat the event is named for."""
    handlers = []
    if which in ("atk", "spa"):
        a = atk_ability
        offensive_pinch = {"blaze": "fire", "torrent": "water", "overgrow": "grass", "swarm": "bug"}
        # defender's onSourceModify*
        if def_ability == "thickfat" and mtype in ("ice", "fire"):
            handlers.append((6 if which == "atk" else 5, "chain", 2048))
        if def_ability == "heatproof" and mtype == "fire":
            handlers.append((6 if which == "atk" else 5, "chain", 2048))
        if def_ability == "purifyingsalt" and mtype == "ghost":
            handlers.append((6 if which == "atk" else 5, "chain", 2048))
        if def_ability == "waterbubble" and mtype == "fire":
            handlers.append((5, "chain", 2048))
        # attacker abilities
        if (
            a in offensive_pinch
            and mtype == offensive_pinch[a]
            and _hp_ratio_gate(attacker, 1, 3, ctx.attacker_hp_exact)
        ):
            handlers.append((5, "chain", 6144))
        if a == "defeatist" and _hp_ratio_gate(attacker, 1, 2, ctx.attacker_hp_exact):
            handlers.append((5, "chain", 2048))
        if a == "dragonsmaw" and mtype == "dragon":
            handlers.append((5, "chain", 6144))
        if a == "rockypayload" and mtype == "rock":
            handlers.append((5, "chain", 6144))
        if a == "steelworker" and mtype == "steel":
            handlers.append((5, "chain", 6144))
        if a == "transistor" and mtype == "electric":
            handlers.append((5, "chain", 5325))
        if a == "stakeout" and not getattr(defender, "active_turns", 1):
            # PS: `!defender.activeTurns` -- true only for a mon that entered
            # mid-turn.  fp mirrors PS's per-turn increment
            # (battle_modifier.py:3227), and such a defender is out of scope
            # anyway (combatant_not_turnstart), so this is a no-op in practice.
            handlers.append((5, "chain", 8192))
        if a == "waterbubble" and mtype == "water":
            handlers.append((0, "chain", 8192))
        volatiles = set(getattr(attacker, "volatile_statuses", ()) or ())
        if a == "flashfire" and "flashfire" in volatiles and mtype == "fire":
            handlers.append((5, "chain", 6144))
        if a in _PARADOX_VOLATILE_PREFIXES and _paradox_stat(attacker) == which:
            handlers.append((5, "chain", 5325))
        if which == "atk":
            if a == "guts" and attacker.status:
                handlers.append((5, "chain", 6144))
            if a in ("hugepower", "purepower"):
                handlers.append((5, "chain", 8192))
            if a == "hustle":
                handlers.append((5, "direct", 6144))
            if a == "slowstart" and "slowstart" in volatiles:
                handlers.append((5, "chain", 2048))
            if a == "gorillatactics":
                handlers.append((1, "chain", 6144))
            if a == "orichalcumpulse" and weather in _SUNS:
                handlers.append((5, "chain", 5461))
            if _item(attacker) == "choiceband":
                handlers.append((1, "chain", 6144))
            if _item(attacker) == "thickclub" and attacker.name.startswith(("cubone", "marowak")):
                handlers.append((1, "chain", 8192))
            if _item(attacker) == "lightball" and attacker.name.startswith("pikachu"):
                handlers.append((1, "chain", 8192))
            # data/abilities.ts:4864-4881 Tablets of Ruin:
            #   onAnyModifyAtk(atk, source, target, move) {
            #     const abilityHolder = this.effectState.target;
            #     if (source.hasAbility('Tablets of Ruin')) return;
            #     if (!move.ruinedAtk) move.ruinedAtk = abilityHolder;
            #     if (move.ruinedAtk !== abilityHolder) return;
            #     return this.chainModify(0.75);
            #   }
            # `onAny` = every active Pokemon's copy of the handler runs, so the
            # HOLDER is whichever side has the ability; the event's own target
            # (battle-actions.ts:1708 `runEvent('ModifyAtk', source, target,
            # move, attack)`) is the ATTACKER, which the handler names `source`.
            # So: the holder lowers EVERY OTHER mon's Atk, and the
            # `source.hasAbility` line is the "does not stack with itself"
            # clause -- the holder's own Atk is untouched.  In singles that
            # collapses to "defender holds it and attacker does not"; the
            # `move.ruinedAtk` bookkeeping only matters in doubles (one holder
            # applies, not two).
            #
            # Tablets is NOT `breakable` (flags: {}), so `def_ability` here is
            # the un-suppressed value even against Mold Breaker / Photon Geyser.
            # No onAnyModifyAtkPriority is declared -> priority 0.
            if def_ability == "tabletsofruin" and atk_ability != "tabletsofruin":
                handlers.append((0, "chain", 3072))
        else:
            if a == "solarpower" and weather in _SUNS:
                handlers.append((5, "chain", 6144))
            if a == "hadronengine" and ctx.terrain == "electricterrain":
                handlers.append((5, "chain", 5461))
            if _item(attacker) == "choicespecs":
                handlers.append((1, "chain", 6144))
            if _item(attacker) == "deepseatooth" and attacker.name.startswith("clamperl"):
                handlers.append((1, "chain", 8192))
            if _item(attacker) == "lightball" and attacker.name.startswith("pikachu"):
                handlers.append((1, "chain", 8192))
            # data/abilities.ts:5277-5294 Vessel of Ruin -- onAnyModifySpA, the
            # exact mirror of Tablets (holder lowers every OTHER mon's SpA;
            # `source` is the attacker; `flags: {}` so not breakable).  This
            # arm is reached only when `atk_event == "spa"`, i.e. the move's
            # CATEGORY is Special -- Body Press (overrideOffensiveStat 'def')
            # still runs ModifyAtk, and Photon Geyser that flipped to Physical
            # runs ModifyAtk, not this.
            if def_ability == "vesselofruin" and atk_ability != "vesselofruin":
                handlers.append((0, "chain", 3072))
    else:  # def / spd, run on the DEFENDER
        d = def_ability
        if d in _PARADOX_VOLATILE_PREFIXES and _paradox_stat(defender) == which:
            handlers.append((6, "chain", 5325))
        if which == "def":
            if weather == "snowscape" and "ice" in _ps_types(defender):
                handlers.append((10, "direct", 6144))
            if d == "furcoat":
                handlers.append((6, "chain", 8192))
            if d == "marvelscale" and defender.status:
                handlers.append((6, "chain", 6144))
            if d == "grasspelt" and ctx.terrain == "grassyterrain":
                handlers.append((6, "chain", 6144))
            if _item(defender) == "eviolite" and _is_nfe(defender.name):
                handlers.append((2, "chain", 6144))
            if _item(defender) == "metalpowder" and defender.name == "ditto":
                handlers.append((2, "chain", 8192))
            # data/abilities.ts:4811-4828 Sword of Ruin:
            #   onAnyModifyDef(def, target, source, move) { ...
            #     if (target.hasAbility('Sword of Ruin')) return; ... 0.75 }
            # The ModifyDef event's target IS the defender
            # (battle-actions.ts:1709 `runEvent('ModifyDef', target, source,
            # move, defense)`), so here the holder is the ATTACKER and the
            # self-exemption is on the DEFENDER.
            #
            # STAT-OVERRIDE GATING: PS runs `'Modify' + statTable[defenseStat]`
            # where `defenseStat = move.overrideDefensiveStat || ...`
            # (battle-actions.ts:1676, :1709).  Psyshock / Psystrike / Secret
            # Sword override to 'def', so they run ModifyDef -- Sword of Ruin
            # DOES apply to them and Beads of Ruin does NOT.  That is why this
            # branch is keyed on `def_event`, not on the move's category.
            if atk_ability == "swordofruin" and d != "swordofruin":
                handlers.append((0, "chain", 3072))
        else:
            if weather == "sandstorm" and "rock" in _ps_types(defender):
                handlers.append((10, "direct", 6144))
            if _item(defender) == "assaultvest":
                handlers.append((1, "chain", 6144))
            if _item(defender) == "eviolite" and _is_nfe(defender.name):
                handlers.append((2, "chain", 6144))
            if _item(defender) == "deepseascale" and defender.name.startswith("clamperl"):
                handlers.append((2, "chain", 8192))
            # data/abilities.ts:374-391 Beads of Ruin -- onAnyModifySpD, the
            # mirror of Sword.  Reached ONLY when `def_event == "spd"`, so a
            # Psyshock (overrideDefensiveStat 'def') never gets here: PS runs
            # ModifySpD off the OVERRIDDEN stat name, and Beads only listens to
            # ModifySpD.
            if atk_ability == "beadsofruin" and d != "beadsofruin":
                handlers.append((0, "chain", 3072))
    return handlers


def _type_levels(mtype, defender, move_id, atk_ability) -> int:
    if mtype == "???":
        return 0
    from fp.helpers import DAMAGE_MULTIPICATION_ARRAY, POKEMON_TYPE_INDICES

    ignore_ghost = atk_ability in ("scrappy", "mindseye") and mtype in ("normal", "fighting")
    total = 0
    for dtype in _ps_types(defender):
        if move_id == "freezedry" and dtype == "water":
            total += 1
            continue
        mult = DAMAGE_MULTIPICATION_ARRAY[POKEMON_TYPE_INDICES[mtype]][POKEMON_TYPE_INDICES[dtype]]
        if mult == 0:
            if ignore_ghost and dtype == "ghost":
                continue
            raise PsRefusal("ps_immune_but_damage_observed")
        total += {0.5: -1, 1: 0, 2: 1}[mult]
    return max(-6, min(6, total))


def _modify_damage_handlers(
    crit, type_levels, mv, move_id, attacker, defender, ctx, mtype, category, def_ability, contact
):
    """The R6 ModifyDamage event (one chained modifier, one truncation)."""
    handlers = []
    infiltrates = attacker.ability == "infiltrator"
    if not crit and not infiltrates:
        screens = ctx.defender_screens
        if category == "physical" and constants.REFLECT in screens:
            handlers.append((0, "chain", 2048))
        elif category == "special" and constants.LIGHT_SCREEN in screens:
            handlers.append((0, "chain", 2048))
        elif constants.AURORA_VEIL in screens:
            handlers.append((0, "chain", 2048))
    if _item(attacker) == "lifeorb":
        handlers.append((0, "chain", 5324))
    if _item(attacker) == "expertbelt" and type_levels > 0:
        handlers.append((0, "chain", 4915))
    a = attacker.ability
    if a == "sniper" and crit:
        handlers.append((0, "chain", 6144))
    if a == "tintedlens" and type_levels < 0:
        handlers.append((0, "chain", 8192))
    if a == "neuroforce" and type_levels > 0:
        handlers.append((0, "chain", 5120))
    d = def_ability
    if d in ("multiscale", "shadowshield") and defender.hp >= defender.max_hp:
        handlers.append((0, "chain", 2048))
    if d in ("filter", "solidrock", "prismarmor") and type_levels > 0:
        handlers.append((0, "chain", 3072))
    if d == "icescales" and category == "special":
        handlers.append((0, "chain", 2048))
    if d == "fluffy":
        mod = 1.0
        if mtype == "fire":
            mod *= 2
        if contact:
            mod /= 2
        if mod != 1.0:
            handlers.append((0, "chain", ps_modifier_4096(mod)))
    if d == "punkrock" and (mv.get("flags", {}) or {}).get("sound"):
        handlers.append((0, "chain", 2048))
    defender_volatiles = set(getattr(defender, "volatile_statuses", ()) or ())
    # Glaive Rush's drawback is NOT a plain "until you switch" volatile: its condition
    # carries `onBeforeMovePriority: 100` / `onBeforeMove(pokemon) {
    # pokemon.removeVolatile('glaiverush') }` (data/moves.ts:6671-6675), so the holder
    # spends it the next time it ATTEMPTS an action -- ahead of every abort handler
    # (flinch is priority 8, sleep/freeze 10, recharge 11, paralysis 1), which is why
    # even a |cant| turn consumes it.  The reconstructed state handed to this module is
    # the TURN-START snapshot, so it still carries the volatile for a holder that has
    # already moved this turn; doubling off that snapshot is exactly wrong.  The engine
    # models the same expiry on its side (poke-engine generate_instructions.rs consumes
    # GLAIVERUSH when `defending_choice.first_move`), and the observed replay damage
    # sits in the engine's set at half the doubled one.
    if "glaiverush" in defender_volatiles and not ctx.defender_before_move_ran:
        handlers.append((0, "chain", 8192))
    if "minimize" in defender_volatiles and move_id in _STOMP_CLASS_MOVES:
        handlers.append((0, "chain", 8192))
    return handlers


def derive_ps_damage(
    attacker,
    defender,
    move_id: str,
    ctx: PsCombatContext,
    ficklebeam_doubled: bool = False,
    category_override: str | None = None,
) -> PsDamage:
    """Derive the PS-exact damage chain (spec 1.1 P1-P7 + tail parameters).

    Raises PsRefusal with a named reason when any input is unknown or any
    effect in play is not modelled -- never guesses.

    `category_override` forces the post-`onModifyMove` category and is used
    only to derive the two arms of a tied Shell Side Arm."""
    mv = all_move_json.get(move_id)
    if not mv:
        raise PsRefusal("ps_unknown_move")
    _check_known(attacker, "attacker")
    _check_known(defender, "defender")
    _check_modelled(attacker, "attacker")
    _check_modelled(defender, "defender")

    atk_ability = attacker.ability
    def_ability = defender.ability
    # Mold-Breaker-class ability suppression.
    #
    # The gate is NOT "blank the defender's ability": sim/battle.ts:836-841
    # skips a handler only when
    #     effect.effectType === 'Ability' && effect.flags['breakable'] &&
    #     this.suppressingAbility(effectHolder)
    # and `suppressingAbility` (sim/battle.ts:365-368) additionally requires
    #     this.activeMove.ignoreAbility            (ability OR move-flagged)
    #     this.activePokemon !== target (gen >= 8)  -- never the move's own user
    #     !target.hasItem('Ability Shield')
    # Abilities with `flags: {}` -- the four Ruin abilities, Prism Armor,
    # Shadow Shield, Protosynthesis, Quark Drive -- are therefore NOT
    # suppressed.  (The old blanket `def_ability = None` is exactly why a
    # Mold Breaker Haxorus's Outrage into a Tablets of Ruin Wo-Chien derived
    # a 0.75x-too-high attack.)
    #
    # `mv["ignoreAbility"]` is the dex flag (Photon Geyser / Sunsteel Strike /
    # Moongeist Beam / Light That Burns The Sky), which PS treats identically.
    ignores_ability = atk_ability in _ABILITY_IGNORES_ABILITY or bool(
        mv.get("ignoreAbility")
    )
    if (
        ignores_ability
        and def_ability in _ABILITY_BREAKABLE
        and _item(defender) != "abilityshield"
    ):
        def_ability = None

    weather = _effective_weather(ctx, attacker, defender)
    category = mv["category"]
    if category == "status":
        raise PsRefusal("ps_status_move")
    mtype = _move_type(mv, move_id, attacker, ctx, weather)
    if category_override is not None:
        category = category_override
    elif move_id in _CATEGORY_SWITCH_MOVES:
        category, coinflip = _category_switch(move_id, attacker, defender, category)
        if coinflip:
            # Shell Side Arm's two arms tied and PS broke the tie with an
            # unobservable `randomChance(1, 2)`.  Derive BOTH arms: a tie in the
            # PRE-modifier base damage does NOT imply a tie in the final roll
            # set (the Physical arm additionally makes the move contact, and the
            # two arms take different Reflect/Light Screen, Muscle Band/Wise
            # Glasses, Ice Scales, burn and Ruin branches), so the event is only
            # decidable when both arms produce the SAME 16 values crit and
            # non-crit.
            arms = [
                derive_ps_damage(
                    attacker, defender, move_id, ctx, ficklebeam_doubled, cat
                )
                for cat in ("physical", "special")
            ]
            if arms[0].rolls(False) == arms[1].rolls(False) and arms[0].rolls(
                True
            ) == arms[1].rolls(True):
                return arms[0]
            raise PsRefusal("ps_shellsidearm_category_coinflip")
    if move_id == "shellsidearm" and category == "physical":
        # data/moves.ts:16233 -- the Physical arm also sets
        # `move.flags.contact = 1`, which feeds Tough Claws / Fluffy /
        # Punching Glove (and Rocky Helmet, which is not a damage effect)
        # downstream.
        mv = {**mv, "flags": {**(mv.get("flags") or {}), "contact": 1}}

    # ---- P1 basePower -----------------------------------------------------
    bp = _base_power_pre_event(mv, move_id, attacker, defender, ctx, mtype, category, weather)
    bp = max(1, int(bp))
    handlers, contact = _base_power_handlers(
        mv, move_id, attacker, defender, ctx, mtype, category, weather, def_ability, ficklebeam_doubled
    )
    if atk_ability == "technician" and bp <= 60:
        # onBasePowerPriority 30 -- strictly the first handler, so the base
        # power it tests is the raw one (modify(bp, 1) == bp)
        if any(h[0] >= 30 for h in handlers):
            raise PsRefusal("ps_chain_order_ambiguous")
        handlers.append((30, "chain", 6144))
    bp = _run_event(bp, handlers)
    bp = max(1, bp)
    if (
        getattr(attacker, "terastallized", False)
        and attacker.tera_type
        and mtype in _ps_types(attacker)
        and bp < 60
        and (mv.get("priority") or 0) <= 0
        and not mv.get("multihit")
        # battle-actions.ts:1663-1666: "Hard move.basePower check for moves like
        # Dragon Energy that have variable BP" --
        # `!((move.basePower === 0 || move.basePower === 150) &&
        #    move.basePowerCallback)`.  Grass Knot / Low Kick / Electro Ball /
        # Gyro Ball / Flail / Reversal / Eruption / Water Spout / Dragon Energy /
        # Heat Crash / Heavy Slam / Crush Grip / Hard Press / Beat Up are all
        # EXEMPT from the 60 floor, so a tera'd Gyro Ball that computes 25 BP
        # stays at 25.
        and move_id not in _TERA_FLOOR_BP_CALLBACK_EXEMPT
    ):
        bp = 60

    # ---- P2 attack / defense ---------------------------------------------
    # `overrideOffensivePokemon: 'target'` (data/moves.ts foulplay): the STAT
    # (and its boosts) come from the target, while the ModifyAtk event still
    # runs on the move's user (battle-actions.ts:1668/1706-1709).
    stat_source = defender if move_id == "foulplay" else attacker
    if move_id == "bodypress":
        atk_stat, atk_boost_key = constants.DEFENSE, constants.DEFENSE
    elif category == "physical":
        atk_stat, atk_boost_key = constants.ATTACK, constants.ATTACK
    else:
        atk_stat, atk_boost_key = constants.SPECIAL_ATTACK, constants.SPECIAL_ATTACK
    if move_id in ("psyshock", "psystrike", "secretsword"):
        def_stat, def_event = constants.DEFENSE, "def"
    elif category == "physical":
        def_stat, def_event = constants.DEFENSE, "def"
    else:
        def_stat, def_event = constants.SPECIAL_DEFENSE, "spd"
    atk_event = "atk" if category == "physical" else "spa"

    atk_boost = stat_source.boosts.get(atk_boost_key, 0)
    def_boost = defender.boosts.get(def_stat, 0)
    if def_ability == "unaware":
        atk_boost = 0
    if atk_ability == "unaware":
        def_boost = 0
    # move.ignoreDefensive (battle-actions.ts:1691,1697-1700): zeroes the
    # defender's defensive stage in BOTH directions, crit or not.  The crit
    # path below (`_resolve(..., ignore_pos=crit)`) only neutralises POSITIVE
    # stages, so this must be applied here, unconditionally, for Sacred Sword
    # and Darkest Lariat -- the 27-event Sacred Sword divergence class in the
    # 50k resweep was exactly this: the checker honouring defensive stages
    # (both signs) that PS ignores.
    if move_id in _IGNORE_DEFENSIVE_MOVES:
        def_boost = 0

    def _resolve(base_stat, boost, ignore_neg, ignore_pos):
        if ignore_neg and boost < 0:
            boost = 0
        if ignore_pos and boost > 0:
            boost = 0
        boost = max(-6, min(6, boost))
        if boost >= 0:
            return int(base_stat * _BOOST_TABLE[boost])
        return int(base_stat // _BOOST_TABLE[-boost])

    atk_handlers = _stat_handlers(
        atk_event, attacker, defender, mtype, category, ctx, weather, def_ability, atk_ability
    )
    def_handlers = _stat_handlers(
        def_event, attacker, defender, mtype, category, ctx, weather, def_ability, atk_ability
    )

    bases = {}
    for crit in (False, True):
        attack = _resolve(stat_source.stats[atk_stat], atk_boost, crit, False)
        defense = _resolve(defender.stats[def_stat], def_boost, False, crit)
        attack = _run_event(attack, atk_handlers)
        defense = _run_event(defense, def_handlers)
        level = attacker.level
        base = ps_trunc(
            ps_trunc(ps_trunc(ps_trunc(2 * level / 5 + 2) * bp * attack) / defense) / 50
        )  # P3
        base += 2  # P4
        # P5 Parental Bond: battle-actions.ts:1738-1743 -- `modify(baseDamage,
        # 0.25)` on hit 2, applied AFTER the +2 and BEFORE WeatherModifyDamage.
        if ctx.parental_bond_hit2:
            base = ps_modify(base, 0.25)
        # P6 WeatherModifyDamage (a priorityEvent on the active weather
        # condition ONLY, data/conditions.ts) -- spelled out per weather rather
        # than as a sun/rain family, because the four handlers are NOT
        # symmetric:
        #   sunnyday (:556-569) tests hydrosteam FIRST and returns
        #     chainModify(1.5) before ever reaching the Water-suppress branch,
        #     so Hydro Steam is BOOSTED in sun, not halved;
        #   desolateland (:605-611) boosts Fire only (Water moves never get
        #     here -- its onTryMove fails them outright), and does NOT carry
        #     the hydrosteam special case;
        #   raindance (:509-521) boosts Water / suppresses Fire;
        #   primordialsea (:653-659) boosts Water only (Fire moves fail).
        if weather == "sunnyday":
            if move_id == "hydrosteam":
                base = ps_modify(base, 1.5)
            elif mtype == "fire":
                base = ps_modify(base, 1.5)
            elif mtype == "water":
                base = ps_modify(base, 0.5)
        elif weather == "desolateland":
            if mtype == "fire":
                base = ps_modify(base, 1.5)
        elif weather == "raindance":
            if mtype == "water":
                base = ps_modify(base, 1.5)
            elif mtype == "fire":
                base = ps_modify(base, 0.5)
        elif weather == "primordialsea":
            if mtype == "water":
                base = ps_modify(base, 1.5)
        if crit:  # P7
            base = ps_trunc(base * 1.5)
        bases[crit] = (base, attack, defense)

    # ---- tail parameters --------------------------------------------------
    stab_4096 = 4096
    if mtype != "???":
        base_types = _base_types(attacker)
        current_types = _ps_types(attacker)
        is_stab = mtype in current_types or mtype in base_types
        stab = 1.0
        if is_stab:
            stab = 1.5
        if getattr(attacker, "terastallized", False) and attacker.tera_type == mtype and mtype in base_types:
            stab = 2.0
        if atk_ability == "adaptability" and mtype in current_types:
            stab = 2.25 if stab == 2.0 else 2.0
        stab_4096 = ps_modifier_4096(stab)

    type_levels = _type_levels(mtype, defender, move_id, atk_ability)
    burn = (
        attacker.status == constants.BURN
        and category == "physical"
        and atk_ability != "guts"
        and move_id != "facade"
    )
    finals = {}
    for crit in (False, True):
        finals[crit] = ps_chain_mods(
            [
                m
                for _, _, m in _ordered_final(
                    _modify_damage_handlers(
                        crit,
                        type_levels,
                        mv,
                        move_id,
                        attacker,
                        defender,
                        ctx,
                        mtype,
                        category,
                        def_ability,
                        contact,
                    )
                )
            ]
        )

    params = PsTailParams(
        stab_4096=stab_4096,
        type_levels=type_levels,
        burn=burn,
        final_4096_noncrit=finals[False],
        final_4096_crit=finals[True],
    )
    return PsDamage(
        base_noncrit=bases[False][0],
        base_crit=bases[True][0],
        params=params,
        base_power=bp,
        attack=bases[False][1],
        defense=bases[False][2],
    )


# ---------------------------------------------------------------------------
# Fixed / derived damage (no roll set -- a single exact value)
# ---------------------------------------------------------------------------

# `getDamage` returns BEFORE the roll chain for these (battle-actions.ts:
# 1604-1610):
#     if (move.damageCallback) return move.damageCallback(source, target);
#     if (move.damage === 'level')  return source.level;
# so there is no 16-value set -- but the single value IS exact whenever its
# inputs are, and the doctrine says an assertable case must be asserted.
#
#   seismictoss / nightshade  `damage: 'level'`      -> the ATTACKER's level,
#     which the sidecar (and the |switch| details) give exactly;
#   superfang / naturesmadness / ruination
#     `clampIntRange(floor(target.getUndynamaxedHP() / 2), 1)`
#     (clampIntRange itself floors, lib/utils.ts:320-326) -> exact from the
#     defender's EXACT pre-hit HP, which is only known for the p1 side, which
#     is exactly the side this check runs on.
#
# NOT derivable, still excluded: Final Gambit / Endeavor (read the ATTACKER's
# HP, only known to /100), Pain Split, the counter family (needs the damage
# taken this turn), Bide, and the OHKO moves (damage == target.maxhp, always
# lethal, so the observation carries no information).
_LEVEL_DAMAGE_MOVES = frozenset(("seismictoss", "nightshade"))
_HALF_HP_DAMAGE_MOVES = frozenset(("superfang", "naturesmadness", "ruination"))
EXACT_FIXED_DAMAGE_MOVES = _LEVEL_DAMAGE_MOVES | _HALF_HP_DAMAGE_MOVES


def derive_fixed_damage(attacker, defender, move_id: str, defender_hp: int) -> int:
    """The single exact damage value PS returns for a fixed/derived-damage move.

    `defender_hp` is the defender's EXACT HP immediately before the hit (the
    protocol's own running value for the p1 side), which is what PS reads."""
    if move_id in _LEVEL_DAMAGE_MOVES:
        level = getattr(attacker, "level", None)
        if not level:
            raise PsRefusal("ps_unknown_level")
        return int(level)
    if move_id in _HALF_HP_DAMAGE_MOVES:
        if defender_hp is None:
            raise PsRefusal("ps_unknown_defender_hp")
        return max(1, int(defender_hp) // 2)
    raise PsRefusal("ps_unmodelled_fixed_damage_" + move_id)


def _ordered_final(handlers):
    """ModifyDamage handlers all sit at priority 0, where PS breaks ties by
    speed/effect order.  chain() is not associative, so >2 simultaneous
    modifiers whose order changes the product are refused."""
    if len(handlers) <= 2:
        return handlers
    seen = set()
    for order in _tie_groups(handlers):
        seen.add(ps_chain_mods([m for _, _, m in order]))
        if len(seen) > 1:
            raise PsRefusal("ps_chain_order_ambiguous")
    return handlers


# ---------------------------------------------------------------------------
# Engine cross-check scaffold (spec 5 check (b))
# ---------------------------------------------------------------------------


def engine_vs_ps(
    ps: PsDamage, engine_rolls, crit: bool
) -> tuple[bool, int | None, list[int] | None]:
    """Compare the ENGINE's damage model against the PS-exact set for the same
    reconstructed state -- independent of what the replay observed.

    ACTIVE ELEMENTWISE MODE.  The binding now exports the full 16+16 roll set
    (`poke_engine.calculate_damage_roll_sets` ->
    poke-engine-py/src/lib.rs `calculate_damage_rolls_full`), so the comparison
    is a full elementwise equality of all 16 values, not just the max.  Both
    sides use the same ASCENDING convention (index 15 == max roll), so index j
    on either side is PS's `random(16) == 15 - j`.

    `engine_rolls` accepts either a `poke_engine.DamageRollSet` (the new export)
    or the legacy `[max_noncrit, max_crit]` pair; the legacy pair is degenerate
    (it can only fill index 15) and is reported as a max-only comparison so a
    stale wheel can never silently masquerade as a passing elementwise run.

    Returns (agree, engine_max - ps_max, engine_set) with (False, None, None)
    when the engine produced no usable number."""
    ps_set = ps.rolls(crit)

    engine_set = None
    if engine_rolls is None:
        return False, None, None
    if hasattr(engine_rolls, "crit") and hasattr(engine_rolls, "noncrit"):
        engine_set = list(engine_rolls.crit if crit else engine_rolls.noncrit)
        if len(engine_set) != 16:
            return False, None, engine_set
    else:
        # legacy [max_noncrit, max_crit] pair -- max-only, cannot be elementwise
        if not engine_rolls or len(engine_rolls) < 2:
            return False, None, None
        engine_max = engine_rolls[1] if crit else engine_rolls[0]
        return engine_max == ps_set[15], engine_max - ps_set[15], [engine_max]

    return engine_set == ps_set, engine_set[15] - ps_set[15], engine_set


# ---------------------------------------------------------------------------
# Scope tables
# ---------------------------------------------------------------------------

# Fixed/derived damage: the observed delta is not a 16-roll sample.
FIXED_DAMAGE_MOVES = frozenset(
    (
        "seismictoss",
        "nightshade",
        "finalgambit",
        "endeavor",
        "superfang",
        "naturesmadness",
        "ruination",
        "painsplit",
        "counter",
        "mirrorcoat",
        "metalburst",
        "comeuppance",
        "bide",
        # OHKO: damage == remaining HP, no roll set
        "sheercold",
        "fissure",
        "guillotine",
        "horndrill",
    )
)

# Multi-hit moves whose base power is NOT constant across the hits of one use.
# These are now DERIVED per hit (Triple Kick/Axel: `10|20 * move.hit`; Beat Up:
# one BP per party member), which needs the hit INDEX of each observed -damage
# line.  PS emits one line per landed hit in order and then `|-hitcount|`, so
# the index is recoverable exactly when the number of -damage lines the context
# produced equals that hitcount; when it does not (a hit went into a Substitute,
# so it produced an `-activate ... Substitute [damage]` and no -damage line),
# the mapping is genuinely ambiguous and the derivation refuses under
# `ps_multihit_index_ambiguous`.
_MULTIHIT_VARIABLE_BP_MOVES = frozenset(("tripleaxel", "triplekick", "beatup"))

# Defender self-volatiles applied SILENTLY (no protocol line) by its own move
# earlier in the turn, all of which change incoming damage: Roost (drops
# Flying), Glaive Rush (2x damage taken), Minimize (2x from stomp-class moves).
DEFENDER_SILENT_VOLATILE_MOVES = frozenset(("roost", "glaiverush", "minimize"))

# Attacker-HP-scaled base power: the pre-turn state's HP is wrong for these if
# the attacker took damage/healed before moving.
ATTACKER_HP_BP_MOVES = frozenset(
    ("waterspout", "eruption", "dragonenergy", "reversal", "flail")
)

# Excluded by SCOPE (counted under a named bucket, never asserted): base power gated on
# ephemeral state the calculate_damage preview API cannot seed -- Avalanche doubles when
# the user was damaged this turn (engine damage_dealt), Stomping Tantrum when the user's
# previous move failed (engine last_move_failed).
#
# `last_move_failed` IS settable now: it is a serialized side field and
# `_move_failed_sides` harvests it from the previous block, so the state this module
# builds carries it (the categorical gate uses it -- synth19386 T19's doubled Temper
# Flare). Stomping Tantrum stays excluded here only because this module has no PS-exact
# base-power callback for it (`_MOVE_SPECIAL` minus `_MOVE_MODELLED` would refuse it
# anyway); the exclusion is now a MODELLING gap, not a plumbing one. `damage_dealt` is
# still genuinely un-settable, so Avalanche's exclusion stands as written.
SCOPE_EPHEMERAL_MOVES = frozenset(("avalanche", "stompingtantrum"))

# Attacker abilities whose damage contribution is gated on the attacker's OWN
# HP fraction (pinch boosts at <=1/3, Defeatist at <=1/2): a mid-turn HP change
# before the attack flips the gate relative to the pre-turn state.
ATTACKER_HP_GATED_ABILITIES = frozenset(
    ("swarm", "blaze", "torrent", "overgrow", "defeatist")
)

# Defender abilities gated on the defender being at FULL HP.
DEFENDER_FULL_HP_ABILITIES = frozenset(("multiscale", "shadowshield", "terashell"))

_BOOST_ACTIONS = frozenset(
    (
        "-boost",
        "-unboost",
        "-setboost",
        "-swapboost",
        "-copyboost",
        "-invertboost",
        "-clearboost",
        "-clearallboost",
        "-clearnegativeboost",
    )
)

_PHASE_ACTIONS = frozenset(("faint", "cant", "upkeep", "turn"))


# ---------------------------------------------------------------------------
# Sidecar loading / exact-team injection
# ---------------------------------------------------------------------------


def _species_key(name: str | None) -> str | None:
    if name is None:
        return None
    return re.sub(r"[^a-z0-9]", "", name.lower())


# Poison pill appended to a side's reconstructed party order by
# `checker._apply_block_to_side_state` when a |switch|/|drag| details species
# cannot be mapped onto exactly one sidecar party slot.  From that point the
# side's `side.pokemon` permutation is UNKNOWN, and anything reading party order
# (only Beat Up) must REFUSE rather than silently walk a stale order.  The NUL
# prefix cannot collide with a `_species_key` (which is [a-z0-9]*), so it never
# matches a real slot in `resolve_party_slot`.
PARTY_ORDER_UNRESOLVED = "\x00party_order_unresolved"


def resolve_party_slot(party_order, key: str | None) -> str | None:
    """Map a live species key onto the ONE sidecar party slot it belongs to.

    A single PS party slot can surface in the protocol (and in fp) under several
    species names: `side.pokemon[i].set.species` never moves, but a |switch|
    details string carries the CURRENT forme -- `Terapagos-Terastal`,
    `Urshifu-Rapid-Strike`, `Charizard-Mega-X`, `Palafin-Hero`, `Aegislash-Blade`,
    `Minior` (whose set species is `Minior-Meteor`).  Every such pair is a prefix
    relation in either direction once punctuation is stripped, so match by
    prefix, preferring an exact hit when the team carries both a base forme and a
    forme-named sibling.

    Returns None when the key resolves to zero or to more than one slot -- the
    caller must then refuse, never guess."""
    if not key:
        return None
    if key in party_order:
        return key
    matches = [
        k for k in party_order if k and (k.startswith(key) or key.startswith(k))
    ]
    return matches[0] if len(matches) == 1 else None


def sidecar_path_for_log(log_path: str, teams_dir: str, suffix: str = ".teams.json") -> str:
    base = os.path.basename(log_path)
    # `.log.gz` is the farm's archive form and `fp.replay.protocol.iter_chunks` reads it
    # directly, so the sidecar must resolve to the SAME name it would for the
    # uncompressed log -- otherwise sweeping a corpus gzipped silently loses the
    # exact-damage half again.
    if base.endswith(".gz"):
        base = base[:-3]
    if base.endswith(".log"):
        base = base[:-4]
    path = os.path.join(teams_dir, base + suffix)
    # FARM / RL PERSPECTIVE LOGS. The synthetic corpus writes ONE log and ONE
    # sidecar per game (`<tag>_synthopp.log` / `<tag>_synthopp.teams.json`), so the
    # log's basename IS the game name. The farm (truestate/farm/lane.py) instead
    # writes one log PER PERSPECTIVE -- `<gid>.p1.log` and `<gid>.p2.log` -- against
    # a SINGLE per-game sidecar `<gid>.teams.json` carrying both sides, so the
    # perspective suffix has to come off before the lookup. Without this the sidecar
    # never resolved, `exact_teams` stayed None in `run_replay_checks`, and the
    # exact-damage membership half of the sweep reported 0 events on every farm game
    # (DATA_GENERATION.md "Still open -- exact-damage membership reports 0 events on
    # farm games"). The CATEGORICAL half never reads the sidecar, which is why it
    # looked healthy the whole time.
    #
    # Existence-first, so a sidecar named exactly after the log still wins: every
    # `_synthopp` name resolves byte-identically to before (it cannot match the
    # suffix test either way), and the gate corpora are untouched.
    if not os.path.exists(path) and (base.endswith(".p1") or base.endswith(".p2")):
        return os.path.join(teams_dir, base[:-3] + suffix)
    return path


def load_teams_sidecar(log_path: str, teams_dir: str) -> dict | None:
    """Load `<game>.teams.json` and index each side's team by species key.
    Returns {"p1": {species_key: mon_dict}, "p2": {...}} or None."""
    path = sidecar_path_for_log(log_path, teams_dir)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    teams = payload.get("teams") or {}
    out = {}
    for pid in ("p1", "p2"):
        side = teams.get(pid) or {}
        lookup = {}
        for mon in side.get("team", ()):
            key = _species_key(mon.get("species", ""))
            if key:
                lookup[key] = mon
        out[pid] = lookup
    return out if (out.get("p1") or out.get("p2")) else None


def _row_move_keys(rec) -> set:
    """The sidecar row's SET moves as normalized keys (empty for non-rows)."""
    if not isinstance(rec, dict):
        return set()
    return {normalize_name(m) for m in (rec.get("moves") or ()) if m}


def _is_transform_only_row(rec) -> bool:
    """A row whose whole moveset is Transform -- i.e. an Imposter Ditto."""
    return _row_move_keys(rec) == {"transform"}


def _disambiguate_by_moves(candidates: list, revealed: set, drop_decoys=False) -> list:
    """Narrow same-forme-family sidecar rows using the live mon's revealed moves.

    IMPOSTER-DITTO SPECIES COLLISION.  A Ditto that Transforms into a species
    its own side also carries makes the sidecar hold TWO rows in one forme
    family, and a species-keyed lookup then joins the live mon to whichever the
    generator happened to name -- with the Ditto's flat 133 stats, which
    `apply_exact_team` applies verbatim because live key == row key.  Measured,
    synthu6256926: p2's Imposter Ditto was recorded as `Terapagos-Terastal`
    while the REAL Terapagos kept the base-forme name, so the terastallized
    Terapagos derived atk 133 / spa 133 instead of 191 / 206 and both its hits
    (crit Rapid Spin T2, super-effective Earth Power T3) fell outside the
    PS-exact roll set.
    Moves settle it: a Transform-only row cannot be a mon that has used any
    other move, and otherwise the row that CONTAINS every revealed move is the
    set actually being played.
    """
    if not revealed and not drop_decoys:
        return candidates
    non_decoy = [c for c in candidates if not _is_transform_only_row(c)]
    if non_decoy:
        candidates = non_decoy
    if len(candidates) > 1:
        covering = [c for c in candidates if revealed <= _row_move_keys(c)]
        if covering:
            candidates = covering
    return candidates


def _match_exact_mon(lookup: dict, species: str, moves=None) -> dict | None:
    key = _species_key(species)
    if key is None:
        return None
    # Revealed moves of the LIVE mon (`fp.battle.Move` objects or plain names),
    # minus Transform itself -- a real Ditto legitimately shows Transform.
    revealed = {
        normalize_name(getattr(m, "name", m)) for m in (moves or ()) if m is not None
    }
    revealed.discard("transform")
    rec = lookup.get(key)
    # A row whose ENTIRE moveset is Transform is an Imposter Ditto, and the
    # sidecar contract states the immutable SET species -- so a Transform-only
    # row named as anything but a Ditto forme is a generator-written LIVE
    # (Transformed) species: its key is not the identity of any set, and the
    # live mon that hit it is the real bearer of that species.  Prefer any
    # other row of the same forme family; keep `rec` if there is none, so a
    # rejection can never lose a slot.
    decoy = (
        rec is not None
        and _is_transform_only_row(rec)
        and not key.startswith("ditto")
    )
    if rec is not None and not decoy and not (revealed and _is_transform_only_row(rec)):
        return rec
    if not os.environ.get("FP_CONTROL_NO_EXACT_TEAM_FORME_FAMILY"):
        # A permanent detailschange names a different forme of the SAME
        # physical pokemon; PS changes `baseSpecies` with the forme
        # (sim/pokemon.ts:1433-1453). Prefix matching cannot recognise sibling
        # names such as Terapagos-Terastal / Terapagos-Stellar, so the exact
        # sidecar never reached the live form and its base-160 max HP remained
        # reconstructed against a guessed base-95 value.
        def family(value):
            species_key = _species_key(value)
            base = (pokedex.get(species_key) or {}).get("baseSpecies")
            return _species_key(base) if base else species_key

        wanted_family = family(key)
        family_hits = [v for k, v in lookup.items() if family(k) == wanted_family]
        family_hits = _disambiguate_by_moves(family_hits, revealed, drop_decoys=decoy)
        if len(family_hits) == 1:
            return family_hits[0]
    # forme drift (detailschange etc.): fall back to a unique prefix match
    hits = [v for k, v in lookup.items() if k.startswith(key) or key.startswith(k)]
    hits = _disambiguate_by_moves(hits, revealed, drop_decoys=decoy)
    if len(hits) == 1:
        return hits[0]
    return rec


_STAT_ORDER = ("hp", "atk", "def", "spa", "spd", "spe")

# ---------------------------------------------------------------------------
# PS's gen9 random-battle "no Attack-stat move" spread rule
# ---------------------------------------------------------------------------
# data/random-battles/gen9/teams.ts:1565-1584 -- the LAST thing randomSet does
# before returning:
#
#     // Minimize confusion damage
#     const noAttackStatMoves = [...moves].every(m => {
#         const move = this.dex.moves.get(m);
#         if (move.damageCallback || move.damage) return true;
#         if (move.id === 'shellsidearm') return false;
#         // Physical Tera Blast
#         if (move.id === 'terablast' && (species.id === 'porygon2' ||
#             ['Contrary', 'Defiant'].includes(ability) || moves.has('shiftgear') ||
#             species.baseStats.atk > species.baseStats.spa)) return false;
#         return move.category !== 'Physical' || move.id === 'bodypress' ||
#             move.id === 'foulplay';
#     });
#     if (noAttackStatMoves && !moves.has('transform') && ...) {
#         evs.atk = 0;
#         ivs.atk = 0;
#     }
#
# The v7 farm2 `<game>.teams.json` sidecar records the NOMINAL 85-EV spread and
# carries no `ivs` at all (unlike the older synthetic corpus, which carries both
# `ivs` and the computed `stats`), so without re-deriving the rule the
# reconstruction hands the opponent an Attack stat PS never had -- e.g. Blissey
# L85 66 instead of 22, Necrozma L80 217 instead of 176, Yanmega L82 172
# instead of 129.  That corrupts every derivation that reads Attack:
#   * Struggle (physical, 50 BP) -- the whole roll set is inflated;
#   * Photon Geyser / Tera Blast -- `getStat('atk', false, true) > getStat('spa',
#     false, true)` (data/moves.ts:13348-13350, :19223-19228) flips the category
#     to Physical off the inflated stat, so a Draco-Meteor'd / Moonblast'd
#     special attacker derives a physical hit PS resolved as special.
_ATK_STAT_PHYSICAL_EXEMPT = frozenset(("bodypress", "foulplay"))

# PS moves carrying a `damageCallback` (data/moves.ts, gen9 top-level entries).
# `move.damage` is exported by foul-play's data/moves.json; `damageCallback` is
# a function and is not, hence the explicit list.
_DAMAGE_CALLBACK_MOVES = frozenset(
    (
        "comeuppance",
        "counter",
        "endeavor",
        "finalgambit",
        "guardianofalola",
        "metalburst",
        "mirrorcoat",
        "naturesmadness",
        "psywave",
        "ruination",
        "superfang",
    )
)


def _randbats_zeroes_attack(move_ids, species_key, ability, base_stats) -> bool:
    """PS's `noAttackStatMoves && !moves.has('transform')` gate, verbatim.

    Returns False for any move foul-play's move table does not know: an
    unrecognised move cannot be certified as Attack-blind, and the caller must
    then leave the default spread alone."""
    move_ids = [normalize_name(m) for m in move_ids]
    if "transform" in move_ids:
        return False
    for move_id in move_ids:
        mv = all_move_json.get(move_id)
        if mv is None:
            return False
        if move_id in _DAMAGE_CALLBACK_MOVES or mv.get("damage"):
            continue
        if move_id == "shellsidearm":
            return False
        if move_id == "terablast":
            if (
                species_key == "porygon2"
                or normalize_name(ability or "") in ("contrary", "defiant")
                or "shiftgear" in move_ids
                or base_stats[constants.ATTACK] > base_stats[constants.SPECIAL_ATTACK]
            ):
                return False
            continue
        if (
            mv.get("category") == "physical"
            and move_id not in _ATK_STAT_PHYSICAL_EXEMPT
        ):
            return False
    return True


# ---------------------------------------------------------------------------
# PS's gen9 random-battle Gyro Ball / Trick Room speed rule
# ---------------------------------------------------------------------------
# data/random-battles/gen9/teams.ts:1586-1589, the LAST spread adjustment
# randomSet makes, immediately after the Attack rule above:
#
#     if (moves.has('gyroball') || moves.has('trickroom')) {
#         evs.spe = 0;
#         ivs.spe = 0;
#     }
#
# Same sidecar gap as the Attack rule (nominal 85 EVs, no `ivs`), but what it
# corrupts is TURN ORDER rather than a stat.  gen9 randbats' main Revival
# Blessing user is Rabsca, whose set ALWAYS carries Trick Room, so the
# reconstruction stood every opposing Rabsca up at 134 Spe against PS's true 86
# (L91) -- and the engine then ordered Revival Blessing FIRST on turns PS
# resolved it LAST.  Revival Blessing arms `force_switch` and PARKS the other
# side's move for the deferred phase-2 half of the turn (poke-engine
# genx/generate_instructions.rs:19689-19780), so with the order flipped the
# foe's move -- and every boost / weather / hazard / status / item loss it
# carried -- resolved in the wrong half of the turn and no phase-1 branch could
# reproduce it.  That is the whole "Revival Blessing" finding family: 375 hard
# findings over its 316 games, 0 with this rule applied, coverage identical.
_ZERO_SPEED_MOVES = frozenset(("gyroball", "trickroom"))


def _randbats_zeroes_speed(move_ids) -> bool:
    """PS's `moves.has('gyroball') || moves.has('trickroom')` gate, verbatim."""
    return any(normalize_name(m) in _ZERO_SPEED_MOVES for m in move_ids)


def _randbats_set_species_key(species) -> str | None:
    """The species PS's GENERATOR saw, from the species the sidecar recorded.

    `randomSet` derives the spread from the randbats SET's species, so a forme
    the BATTLE produced has to be folded back -- but a forme the GENERATOR chose
    must not be, and `baseSpecies` cannot tell them apart (`Minior-Meteor` and
    `Ogerpon-Hearthflame` both carry one).  `battleOnly` can, and is exactly the
    dex field that means it: Minior-Meteor / Eiscue-Noice / Palafin-Hero /
    Terapagos-Terastal name their set species in it, while Ogerpon-Hearthflame,
    Arceus-Fire and Urshifu-Rapid-Strike -- real randbats species with their own
    types, and therefore their own Stealth-Rock weakness -- do not.
    MEASURED: folding on `baseSpecies` instead shaved Ogerpon-Hearthflame (240
    vs PS's 239) and Arceus-Fire (292 vs 291) off Teal's pure-Grass / Arceus's
    pure-Normal Rock matchup instead of their own.

    A COSMETIC forme -- Minior's colours, and Minior is the one species whose HP
    shave has a branch of its own (teams.ts:1545-1547, `species.id === 'minior'`)
    -- carries NEITHER field in fp's trimmed pokedex.json, so it is folded by
    name, and only when the shorter key exists and is stat- and type-IDENTICAL:
    a genuine forme difference can never be folded away by that arm.
    """
    key = _species_key(species)
    if not key:
        return None
    entry = pokedex.get(key)
    if entry is None:
        return key
    battle_only = entry.get("battleOnly")
    if battle_only:
        if isinstance(battle_only, (list, tuple)):
            battle_only = battle_only[0] if battle_only else None
        if battle_only:
            return _species_key(normalize_name(battle_only))
    if entry.get("baseSpecies"):
        return key  # a generator-chosen forme: PS spread it as itself
    head = _species_key(str(species).split("-")[0])
    sibling = pokedex.get(head) if head and head != key else None
    if (
        sibling is not None
        and sibling.get("baseStats") == entry.get("baseStats")
        and sibling.get("types") == entry.get("types")
    ):
        return head
    return key


def _randbats_shaved_hp_ev(rec, level):
    """PS's HP-EV shave (teams.ts:1536-1564) -> the HP EV randomSet ended on.

    `while (evs.hp > 1)` walks the flat 85 down 4 at a time until the resulting
    HP hits the set's breakpoint: an EVEN HP for a Belly Drum / Fillet Away /
    Shed Tail set holding Sitrus Berry (or Gluttony), an HP divisible by 4 for
    Substitute+Sitrus and for Minior's Shields Down, an HP NOT divisible by 4
    for Substitute+Endeavor, else the Stealth-Rock-switch-in maximiser keyed on
    the mon's Rock weakness (skipped outright when the mon cannot be chipped:
    Magic Guard / Heavy-Duty Boots / Regenerator / Leftovers / Life Orb).

    DELEGATED to foul-play's live-play mirror of the same PS lines
    (data/pkmn_sets.random_battle_ev_iv_spread) rather than re-implemented, and
    the delegation is VERIFIED rather than assumed: recomputing all six stats
    for every mon of the 632 perspective logs behind the 316 Revival-Blessing
    games and comparing against the exact stats PS states in each `|request|`
    gives 3783/3792 mons exact (99.7627%), the whole residue being Minior
    cosmetic formes that `_randbats_set_species_key` folds; the flat 85/31
    spread the sidecar implies scores 3468/3792 on the same mons.  The only PS
    line the mirror omits is `if (isDoubles) break;`, unreachable in
    gen9randombattle(blitz).

    Returns None when the shave cannot be derived (a species fp's pokedex does
    not know, or the champions 11-EV base spread, which PS does not shave).
    """
    if random_battle_ev_iv_spread is None:
        return None
    species_key = _randbats_set_species_key(rec.get("species"))
    if not species_key or species_key not in pokedex:
        return None
    evs, _ivs = random_battle_ev_iv_spread(
        species_key,
        [normalize_name(m) for m in (rec.get("moves") or ())],
        normalize_name(rec.get("ability") or ""),
        normalize_name(rec.get("item") or ""),
        level,
    )
    return evs[0]


def _apply_randbats_hp_shave(pkmn, rec) -> None:
    """Apply PS's HP-EV shave to a v7-sidecar mon (see `_randbats_shaved_hp_ev`).

    WHICH FIELD: fp keeps HP OUT of `Pokemon.stats` -- that dict is
    atk/def/spa/spd/spe, `calculate_stats`' HITPOINTS entry being popped into
    `max_hp` (fp/battle.py set_spread:1353-1371) -- and every max-HP consumer in
    the checker reads `pkmn.max_hp`.  So the shave lands there, and the live
    `hp` is carried across the correction by `_rebase_hp_onto_exact_max`, the
    same helper the sidecar's own `stats.hp` override uses: it honours an
    exact-HP certificate and otherwise re-derives the point estimate from the
    last percent DISPLAY against the corrected max, which is strictly better
    than a fraction rescale (that can land outside the band the protocol
    stated).  `hp` is therefore never "recomputed" -- it stays protocol-tracked,
    exactly as the Attack arm's note requires.

    `max_hp_exact` IS set, on the same terms the sidecar's stated `stats.hp`
    claims it: once all three of randomSet's adjustments are re-derived the
    spread has stopped being a guess, so neither is the max HP that falls out of
    it.  The claim is MEASURED, not asserted -- recomputing every stat of the
    3,516 mon-instances behind the 291 ground-truth games and comparing against
    the exact stats PS states in each `|request|` leaves zero max-HP mismatches
    -- and it is what lets a deferred HP certificate settle.  Withholding it
    while the number is exact keeps the checker guessing on purpose, and costs
    67 soft + 11 hard phantom findings on those games, every one an HP-precision
    artifact (ko-margin boosts, hazard chip, Sitrus gates).

    Returns True when the spread was derived -- nothing is changed when it could
    not be (unknown species, non-flat base spread, the champions 11-EV format).
    """
    if getattr(pkmn, "transformed_into", None) or pkmn.name == "shedinja":
        return False
    if tuple(pkmn.evs)[0] != 85:
        return False  # not the flat base spread randomSet shaves down from
    hp_ev = _randbats_shaved_hp_ev(rec, pkmn.level)
    if hp_ev is None or not pkmn.max_hp:
        return False
    evs = list(pkmn.evs)
    evs[0] = hp_ev
    new_max_hp = calculate_stats(
        pkmn.base_stats,
        pkmn.level,
        ivs=pkmn.ivs,
        evs=evs,
        nature=pkmn.nature,
    )[constants.HITPOINTS]
    hp_frac = pkmn.hp / pkmn.max_hp
    pkmn.evs = tuple(evs)
    pkmn.max_hp = new_max_hp
    pkmn.max_hp_exact = True
    # Unconditional, exactly as the `stats.hp` override below does it: even when
    # the max HP itself did not move, this is the point at which a DEFERRED
    # display-consistency check on an HP certificate becomes decidable.
    _rebase_hp_onto_exact_max(pkmn, hp_frac)
    return True


def _fold_forme_duplicate_rows(battler, lookup: dict) -> None:
    """Collapse tracked rows that are the same PS roster slot.

    `load_teams_sidecar` indexes the team dump one record per party member, and
    `sim/side.ts getRequestData` pushes exactly one entry per member of
    `this.pokemon` -- so the sidecar IS the opponent's roster, exactly as the
    `|request|` party list is the user's.  Two tracked rows that claim the SAME
    record are therefore one physical pokemon, and this is the opponent-side
    counterpart of `Battler._drop_unclaimed_forme_duplicates` (fp/battle.py:769),
    which cannot run here because the opponent has no `|request|` to claim rows.

    The gap this closes is TERA formes.  `battleOnly` marks every forme a battle
    can produce, tera formes included -- `ogerpontealtera -> Ogerpon`,
    `ogerponhearthflametera -> Ogerpon-Hearthflame`, `terapagosstellar ->
    Terapagos` (data/pokedex.ts; PS only ever enters them through
    `formeChange` on terastallization, so none can be a SET species) -- but
    nothing was folding them onto the row they share a slot with, so a
    terastallized Ogerpon held TWO of the six slots.  Measured, fresh100k
    arbiter #1's single hard finding: side p1 reconstructed as HAWLUCHA BISHARP
    COMFEY UXIE OGERPON OGERPONTEALTERA against a true roster of Bisharp,
    Ogerpon, Masquerain, Comfey, Hawlucha, Uxie -- the duplicate consumed the
    slot `_fill_unrevealed_reserves` needed for MASQUERAIN, so when Roar dragged
    Masquerain in, its Intimidate had no mon in the state to come from and no
    branch could produce the observed `-unboost`.

    WHICH ROW SURVIVES.  Never the one still on the field, and never the one
    carrying more of the battle: the live object is kept and the stale one
    dropped, scored on how much of the battle it has actually seen (turns
    active, move actions, revealed moves), with the SET species -- the sidecar's
    own species, which `sim/pokemon.ts` never moves -- as the tie-break.  So a
    Terapagos that detailschanged while active keeps its Terastal row (which
    carries that forme's real max HP), while this game's two never-active,
    identical 259/259 Ogerpon rows collapse onto the canonical `ogerpon`.
    """
    if os.environ.get("FP_CONTROL_KEEP_FORME_DUPLICATES"):
        # CONTROL ONLY (shared with fp/battle.py:800 and
        # poke_engine_helpers.py:395): restores the pre-fix pipeline whole, so
        # the fix can be shown capable of failing.
        return
    groups: dict = {}
    for pkmn in battler.reserve:
        rec = _match_exact_mon(lookup, pkmn.name, pkmn.moves)
        if rec is not None:
            groups.setdefault(id(rec), []).append(pkmn)
    active_rec = (
        _match_exact_mon(lookup, battler.active.name, battler.active.moves)
        if battler.active is not None
        else None
    )
    drop: set = set()
    for rec_id, rows in groups.items():
        if active_rec is not None and id(active_rec) == rec_id:
            # the active already holds this slot, and one physical pokemon cannot
            # also be sitting on the bench: EVERY bench row for it is stale, even
            # a lone one (`|switch|Terapagos` -> `|detailschange|Terapagos-Terastal`
            # benches the pre-change object while the mon never left the field).
            drop.update(id(p) for p in rows)
            continue
        if len(rows) < 2:
            continue
        rec = _match_exact_mon(lookup, rows[0].name, rows[0].moves) or {}
        set_key = _species_key(rec.get("species") or "")

        def _liveness(p):
            return (
                getattr(p, "active_turns", 0) or 0,
                getattr(p, "active_move_actions", 0) or 0,
                len(getattr(p, "moves", ()) or ()),
                _species_key(p.name) == set_key,
            )

        keep = max(rows, key=_liveness)
        drop.update(id(p) for p in rows if p is not keep)
    if drop:
        battler.reserve[:] = [p for p in battler.reserve if id(p) not in drop]


def _fill_unrevealed_reserves(battler, lookup: dict) -> None:
    """Materialise the party slots the protocol never revealed.

    fp tracks only what it has SEEN, so a reserve that never switched in is
    simply ABSENT from `battler.reserve`, and the engine side is padded out to
    six with maxhp-0 placeholders (`_padded_party` ->
    `get_dummy_poke_engine_pkmn`, fp/search/poke_engine_helpers.py:349-353,
    :437-439).  That is right for LIVE play, where those slots really are
    unknown, and wrong here, where the sidecar states the whole team -- and it
    is wrong in a way that anything COUNTING party members reads.  Beat Up
    throws one hit per unfainted, unstatused party member (data/moves.ts beatup
    `onModifyMove`: `move.allies = pokemon.side.pokemon.filter(ally => ally ===
    pokemon || (!ally.fainted && !ally.status))`, then `move.multihit =
    move.allies.length`), so a side still holding two unrevealed mons threw FOUR
    hits in-engine where PS threw SIX.  The PS-exact derivation on this side of
    the checker never had the bug -- `_beatup_base_powers` walks the sidecar's
    `party_order` -- which is exactly why the two disagreed.

    Only the OPPONENT can have unseen slots (the user's six are in every
    `|request|`), and this runs only on the per-turn `deepcopy(battle)` snapshot
    the sidecar path owns (checker.py:4251,:5440), so nothing added here can
    leak back into the protocol reconstruction, into a later turn, or into live
    play.

    A filled mon is at full HP with no status BY CONSTRUCTION: a mon that never
    entered the battle cannot have been damaged, statused or fainted.  That is
    the same reasoning `_beatup_base_powers` already documents for the mons it
    cannot see, and it is what makes the filled slot safe to count.

    The party is capped at PS's six: `_padded_party` refuses a longer one, so
    when fp is already tracking six (a forme duplicate, say) nothing is added.
    """
    if Pokemon is None:
        return
    _fold_forme_duplicate_rows(battler, lookup)
    party = list(battler.reserve)
    if battler.active is not None:
        party.append(battler.active)
    room = 6 - len(party)
    if room <= 0:
        return
    # Identity, not species keys: `_match_exact_mon` also resolves forme
    # families and unique prefixes, so the mon fp is tracking as
    # `Terapagos-Terastal` already claims the sidecar's `Terapagos` slot.
    claimed = set()
    for pkmn in party:
        rec = _match_exact_mon(lookup, pkmn.name, pkmn.moves)
        if rec is not None:
            claimed.add(id(rec))
    for rec in lookup.values():  # sidecar order == PS's initial party order
        if room <= 0:
            break
        if id(rec) in claimed:
            continue
        species = normalize_name(rec.get("species") or "")
        level = rec.get("level")
        if not species or species not in pokedex or not level:
            continue  # never guess a slot the sidecar does not fully state
        battler.reserve.append(Pokemon(species, int(level)))
        room -= 1


def apply_exact_team(battler, lookup: dict, is_user: bool) -> None:
    """Override a battler's reconstructed knowledge with the sidecar's exact
    sets.  Fill-if-unknown for ability/item/moves/tera (protocol tracking of
    mid-battle changes -- Knock Off, Trick, berry eats, Skill Swap -- stays
    authoritative); for the OPPONENT the computed stats/max-HP are overridden
    outright (fixed set properties the reconstruction otherwise infers from
    randbats spread assumptions).  The user side is already exact from
    |request| (stats/hp/ability/item), so only its tera type is filled."""
    if not lookup:
        return
    # BEFORE the knowledge pass, so a slot the protocol never revealed is filled
    # in and then set up by exactly the same arms as every other mon.
    if not is_user and not os.environ.get("FP_CONTROL_NO_ROSTER_FILL"):
        _fill_unrevealed_reserves(battler, lookup)
    mons = list(battler.reserve)
    if battler.active is not None:
        mons.append(battler.active)
    for pkmn in mons:
        rec = _match_exact_mon(lookup, pkmn.name, pkmn.moves)
        if rec is None:
            continue
        tera_type = rec.get("teraType")
        if tera_type and not pkmn.terastallized and not pkmn.tera_type:
            pkmn.tera_type = normalize_name(tera_type)
        if is_user:
            continue
        # The sidecar ability is KNOWLEDGE; fp's ability is either unknown,
        # protocol knowledge (an `|-ability|` / `[from] ability:` reveal), or a
        # GUESS made by an inference heuristic (`ability_inferred` -- today only
        # the "healed while off the field -> regenerator" arm at
        # battle_modifier.py:1242-1259).  A guess must lose to the sidecar, for
        # exactly the reason `item_inferred` does just below: synth1352906 T19
        # stood Klawf up with REGENERATOR instead of ANGER SHELL (the off-field
        # gain belonged to a Zoroark-Hisui that had been wearing its face), so no
        # branch produced the observed Anger Shell +atk/+spa/+spe/-def/-spd.
        # Fill-if-unknown otherwise, so a WITNESSED mid-battle ability (Skill
        # Swap / Trace / Mummy) is still never overwritten.
        ability_is_guess = getattr(pkmn, "ability_inferred", False)
        if (not pkmn.ability or ability_is_guess) and rec.get("ability"):
            pkmn.ability = normalize_name(rec["ability"])
            pkmn.ability_inferred = False
        # The sidecar item is KNOWLEDGE; fp's item is either unknown, protocol
        # knowledge (a reveal / Knock Off / consumption), or a GUESS made by the
        # inference heuristics (`item_inferred`, e.g. "took no Stealth Rock
        # damage -> Heavy-Duty Boots", battle_modifier.py:3814).  A guess must
        # lose to the sidecar: a Komala guessed into Heavy-Duty Boots while it
        # really holds a Choice Band mis-derives every hit it makes by 1.5x.
        # A protocol-established removal (knocked_off / removed_item) is real
        # knowledge and is never overwritten.
        item_is_guess = getattr(pkmn, "item_inferred", False) and not (
            getattr(pkmn, "knocked_off", False) or getattr(pkmn, "removed_item", None)
        )
        # DISGUISE-POLLUTED HOLD.  A `removed_item` the sidecar CONTRADICTS names
        # an item this mon provably never held: the protocol printed that item
        # line under its face while an Illusion bearer wore it
        # (`_undo_disguised_item_misattribution` owns the acquisition half of
        # this; the residue it leaves behind is the removal half).  With
        # `removed_item` set, `item_is_guess` is false by construction, so a
        # WRONG hold that the tracker went on to infer from the bearer's
        # behaviour could never be corrected: synthu6012750 T42 stood the real
        # Assault-Vest Basculegion up in a CHOICE SCARF (its `removed_item` was
        # the disguised Zoroark's Life Orb), which made it outspeed Venusaur,
        # kill it before Leech Seed, and leave the observed `-start` in no
        # branch.  Requires a THREE-way contradiction -- sidecar item, held
        # item and removed item all different, nothing knocked off, something
        # still held -- so every legitimate mid-battle change is excluded: Knock
        # Off, Trick, Bestow and a consumed berry all leave `removed_item`
        # EQUAL to the sidecar's item, and a knocked-off mon holds nothing.
        sidecar_item = normalize_name(rec["item"]) if rec.get("item") else None
        removed_item = getattr(pkmn, "removed_item", None)
        removed_item = normalize_name(removed_item) if removed_item else None
        held = normalize_name(pkmn.item) if pkmn.item else None
        disguise_polluted = (
            sidecar_item is not None
            and removed_item is not None
            and held is not None
            and held != constants.UNKNOWN_ITEM
            and removed_item != sidecar_item
            and held != sidecar_item
            and not getattr(pkmn, "knocked_off", False)
        )
        if (
            pkmn.item == constants.UNKNOWN_ITEM or item_is_guess or disguise_polluted
        ) and "item" in rec:
            # An empty sidecar item ("") is KNOWLEDGE (the set truly holds no
            # item -- Acrobatics doubles, engine Items::NONE), not ignorance:
            # fill None (fp's no-item value, converted to "None" by
            # battle_to_poke_engine_state) instead of skipping the fill.
            pkmn.item = normalize_name(rec["item"]) if rec["item"] else None
            pkmn.item_inferred = False
        for mv in rec.get("moves", ()):
            if len(pkmn.moves) >= 4:
                break
            mv = normalize_name(mv)
            if not any(m.name == mv for m in pkmn.moves):
                pkmn.add_move(mv)
        # EVs/IVs are SET properties: they never change, not even across a
        # forme change or a Transform, so they are applied unconditionally.
        # (The old code gated the whole block on `forme_changed`, which made
        # every post-forme-change turn fall back to fp's default 85-EV
        # assumption -- e.g. Eiscue L88 max_hp 275 instead of the true 274 once
        # Ice Face broke, which silently corrupts Belly Drum / Sitrus gates and
        # every damage derivation from that mon onward.  `forme_changed` is
        # also set when a mon REVERTS to its base forme (fp/battle.py:815) and
        # is only cleared on switch-out (battle_modifier.py:353), so the guard
        # stuck for the rest of the mon's time on the field.)
        try:
            evs = rec.get("evs")
            if evs:
                pkmn.evs = tuple(int(evs[s]) for s in _STAT_ORDER)
            ivs = rec.get("ivs")
            if ivs:
                pkmn.ivs = tuple(int(ivs[s]) for s in _STAT_ORDER)
            else:
                # No `ivs` in the sidecar -> the v7 farm2 dump, which also wrote
                # the NOMINAL 85-EV spread, so ALL THREE of randomSet's
                # generation-time adjustments (teams.ts:1536-1589) have to be
                # re-derived from the (exact) sidecar set.  The arms are
                # independent and one set can hit several -- a Gyro Ball wall
                # zeroes Attack AND Speed -- so each is gated on its own rule
                # and recomputes only the stat that rule touches.  A sidecar
                # that DOES carry `ivs` (and its computed `stats`, applied just
                # below) is authoritative and is left exactly as it was.
                if not os.environ.get(
                    "FP_CONTROL_NO_RANDBATS_ATK_ZERO"
                ) and _randbats_zeroes_attack(
                    rec.get("moves") or (),
                    _species_key(rec.get("species") or pkmn.name),
                    rec.get("ability"),
                    pkmn.base_stats,
                ):
                    # Re-derive PS's Attack zeroing (see
                    # `_randbats_zeroes_attack`) and recompute ONLY the Attack
                    # stat: the rule touches no other stat, and HP must not be
                    # re-derived here (fp's `hp` is protocol-tracked, not a
                    # fraction of a freshly computed max).
                    pkmn.evs = tuple(0 if i == 1 else v for i, v in enumerate(pkmn.evs))
                    pkmn.ivs = tuple(0 if i == 1 else v for i, v in enumerate(pkmn.ivs))
                    pkmn.stats[constants.ATTACK] = calculate_stats(
                        pkmn.base_stats,
                        pkmn.level,
                        ivs=pkmn.ivs,
                        evs=pkmn.evs,
                        nature=pkmn.nature,
                    )[constants.ATTACK]
                if not os.environ.get(
                    "FP_CONTROL_NO_RANDBATS_SPE_ZERO"
                ) and _randbats_zeroes_speed(rec.get("moves") or ()):
                    # teams.ts:1586-1589; recompute ONLY the Speed stat.
                    pkmn.evs = tuple(0 if i == 5 else v for i, v in enumerate(pkmn.evs))
                    pkmn.ivs = tuple(0 if i == 5 else v for i, v in enumerate(pkmn.ivs))
                    pkmn.stats[constants.SPEED] = calculate_stats(
                        pkmn.base_stats,
                        pkmn.level,
                        ivs=pkmn.ivs,
                        evs=pkmn.evs,
                        nature=pkmn.nature,
                    )[constants.SPEED]
                if not os.environ.get("FP_CONTROL_NO_RANDBATS_HP_SHAVE"):
                    # teams.ts:1536-1564; touches `max_hp` only, and carries the
                    # protocol-tracked `hp` across it.
                    _apply_randbats_hp_shave(pkmn, rec)
        except (KeyError, TypeError):
            pass

        # Computed stats are forme-dependent.  On the sidecar's own forme the
        # sidecar's computed stats are authoritative; on a battle forme they
        # are recomputed from THAT forme's base stats with the (now exact)
        # EVs/IVs -- which keeps a forme whose base HP genuinely differs
        # (Terapagos-Stellar, Zygarde-Complete) at its real max HP without any
        # hardcoded species list, because the carve-out falls out of the base
        # stat data.  A Transform copies the target's stats outright and is
        # never overridden.
        stats = rec.get("stats")
        if not stats or getattr(pkmn, "transformed_into", None):
            continue
        try:
            hp_frac = pkmn.hp / pkmn.max_hp if pkmn.max_hp else 0.0
            # The question is "is the LIVE forme the SIDECAR's forme?", which is
            # a species comparison -- NOT `forme_changed`, a live-tracking flag
            # that battle_modifier clears on switch-out (battle_modifier.py:353)
            # while the forme itself persists across that switch.  Shields Down
            # is where the two come apart: PS's randbats set species is `Minior`
            # (Core, base atk 100) and data/abilities.ts:4229-4242 flips it to
            # Minior-Meteor (base atk 60) at switch-in, so the corpus's
            # mid-battle team dump records species `Minior-Meteor` with METEOR's
            # computed stats (setSpecies recomputes storedStats per forme,
            # sim/pokemon.ts:1404-1418).  A Minior that flipped to Core below
            # 1/2 HP, switched OUT (flag cleared; PS's clearVolatile revert
            # target is baseSpecies = Core) and walked back in still Core
            # (abilities.ts:4237-4241 onStart only flips Meteor->Core, so no
            # forme line is emitted) therefore had `forme_changed` False and was
            # handed Meteor's 140 Attack in place of Core's 204.  synth112113
            # T25: Minior L79 Earthquake vs Houndstone L86 -- roll set capped at
            # 43 against PS's true band 52..62, observed 53.
            if _species_key(pkmn.name) != _species_key(rec.get("species") or ""):
                computed = calculate_stats(
                    pkmn.base_stats,
                    pkmn.level,
                    ivs=pkmn.ivs,
                    evs=pkmn.evs,
                    nature=pkmn.nature,
                )
                new_max_hp = computed.pop(constants.HITPOINTS)
                pkmn.stats = computed
            else:
                pkmn.stats = {
                    constants.ATTACK: int(stats["atk"]),
                    constants.DEFENSE: int(stats["def"]),
                    constants.SPECIAL_ATTACK: int(stats["spa"]),
                    constants.SPECIAL_DEFENSE: int(stats["spd"]),
                    constants.SPEED: int(stats["spe"]),
                }
                new_max_hp = int(stats["hp"])
            if pkmn.name != "shedinja":
                pkmn.max_hp = new_max_hp
                # the sidecar's `stats.hp` IS the true max HP -- this is where a
                # deferred certificate consistency check becomes decidable
                pkmn.max_hp_exact = True
                _rebase_hp_onto_exact_max(pkmn, hp_frac)
        except (KeyError, TypeError, ZeroDivisionError):
            continue


def _rebase_hp_onto_exact_max(pkmn, hp_frac) -> None:
    """Carry a reconstructed HP across the sidecar's max-HP correction.

    The sidecar's `stats.hp` is the mon's TRUE max HP, so this is the moment the
    reconstruction stops guessing it.  How the current HP should follow depends
    on what kind of value it is:

    * an EXACT-HP certificate (fp/hp_certificate.py) is either an absolute
      integer stated by a protocol identity (a landed Endeavor, a Pain Split) --
      rescaling that by a ratio of max HPs would destroy exactly the information
      the certificate exists to carry, so it is kept verbatim -- or a stated
      FUNCTION of max HP (`hp == max_hp`, a revive's `max_hp // 2`), which is
      re-evaluated here.  `verify_against_exact_max` does both, and settles the
      deferred display-consistency check.
    * otherwise, if the last percent DISPLAY is still the thing that set this
      HP, re-derive the point estimate from that display against the TRUE max
      HP.  This is strictly better than `round(new_max_hp * hp_frac)`: the
      fraction was computed against a GUESSED max HP, and `round` could (and
      did -- APPROXIMATIONS U3's hard finding: 310 * 0.25 -> 78 for a mon the
      protocol had pinned to [75, 77]) land outside the band the protocol
      stated.
    * with neither, fall back to the fraction rescale.
    """
    if hp_certificate.is_exact(pkmn):
        pkmn.hp = int(pkmn.hp)
        # A certificate taken while max HP was a guess is only decidable now.
        # If it fails, it is dropped (loudly, and counted in
        # hp_certificate.CERTIFICATE_REFUSALS) and the reconstruction falls
        # through to the display estimate below.
        if hp_certificate.verify_against_exact_max(pkmn):
            return
    pct = getattr(pkmn, "hp_display_pct", None)
    if pct is not None and pkmn.hp == getattr(pkmn, "hp_display_hp", None):
        hp_certificate.apply_display(pkmn, pct)
        return
    pkmn.hp = round(pkmn.max_hp * hp_frac)


def _refresh_transform_copied_stats(snap) -> None:
    """Re-copy a Transform / Imposter user's stats from the mon it copied.

    PS `transformInto` copies the target's TRUE `storedStats` (sim/pokemon.ts
    transformInto).  fp's `-transform` handler does the same
    (`side.active.stats = deepcopy(transformed_into.stats)`,
    battle_modifier.transform) -- but it runs DURING the protocol replay, when an
    OPPONENT target still carries fp's randbats-spread GUESS.  `apply_exact_team`
    then corrects the opponent's OWN object and stops there (`if is_user:
    continue`), so the copy keeps the guess for the rest of the battle.

    synth73533 T31: Imposter Ditto copies Lugia and is left on the 85-EV guess
    Atk 174 instead of Lugia's true 136 (that set has atk EV 0).  Struggle then
    rolls 24..29 against Lugia's certified 20..22 HP band instead of 19..23, so
    EVERY roll kills, the engine emits one capped kill arm at every band value,
    and Lugia's `|-heal|p2a: Lugia|51/100` (Recover) is unreachable everywhere.
    """
    for battler, other in ((snap.user, snap.opponent), (snap.opponent, snap.user)):
        active = getattr(battler, "active", None)
        target = getattr(active, "transformed_into", None) if active is not None else None
        if not target:
            continue
        target = normalize_name(target)
        candidates = list(other.reserve)
        if other.active is not None:
            candidates.append(other.active)
        for cand in candidates:
            if normalize_name(cand.name) == target:
                active.stats = deepcopy(cand.stats)
                break


def apply_exact_teams(snap, user_pid: str, exact_teams: dict) -> None:
    opp_pid = "p1" if user_pid == "p2" else "p2"
    apply_exact_team(snap.user, exact_teams.get(user_pid) or {}, is_user=True)
    apply_exact_team(snap.opponent, exact_teams.get(opp_pid) or {}, is_user=False)
    # a transformed mon's copied stats were taken BEFORE the two calls above
    # corrected the original's stats; refresh them (see the helper's docstring)
    _refresh_transform_copied_stats(snap)


# ---------------------------------------------------------------------------
# HP-TRUTH sidecar (`<game>.hptruth.json`): exact per-turn HP for BOTH sides,
# observed by the generator from PS's own battle object at each `|turn|N`
# boundary (see foul-play/tools/conformance/gen_random_corpus.js).  The same
# authoritative-sidecar pattern as the teams sidecar: exact values win over
# /100-quantized display parsing.  Replay-with-sidecar only -- nothing here
# runs in live play (loaded only via check_replays' --teams-dir path).
# ---------------------------------------------------------------------------


def load_hptruth_sidecar(log_path: str, teams_dir: str) -> dict | None:
    """Load `<game>.hptruth.json` -> {"<turn>": {"p1": {species: [hp, maxhp]},
    "p2": {...}}} keyed by species (same start-of-battle naming as the teams
    sidecar, so `_match_exact_mon`'s forme-family resolution applies).
    Returns the turns mapping (JSON string keys) or None."""
    path = sidecar_path_for_log(log_path, teams_dir, suffix=".hptruth.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    turns = payload.get("turns") or {}
    out = {}
    for turn, entry in turns.items():
        sides = {}
        for pid in ("p1", "p2"):
            lookup = {}
            for species, hp_pair in (entry.get(pid) or {}).items():
                key = _species_key(species)
                if key:
                    lookup[key] = hp_pair
            sides[pid] = lookup
        out[str(turn)] = sides
    return out or None


def apply_hptruth(snap, user_pid: str, turn_entry: dict, stats: dict) -> None:
    """Pin every roster mon's pre-turn HP to the generator-observed exact value
    via `hp_certificate.set_exact` (the same call the user side's absolute
    conditions use).  Runs AFTER `apply_exact_teams` (max_hp already exact) and
    BEFORE the protocol-certificate clamps, which treat an exact HP as strictly
    better information.  Misses (a roster mon with no truth entry) are counted,
    not silently skipped -- the canary gate reads them."""
    from fp import hp_certificate

    opp_pid = "p1" if user_pid == "p2" else "p2"
    for pid, battler in ((user_pid, snap.user), (opp_pid, snap.opponent)):
        lookup = turn_entry.get(pid) or {}
        if not lookup:
            continue
        mons = list(battler.reserve)
        if battler.active is not None:
            mons.append(battler.active)
        # AMBIGUOUS-ROSTER GUARD: a reconstructed side can carry two mons with
        # the same species key (lead Impostor Ditto recorded under its
        # transformed species by the teams-sidecar convention).  Pinning either
        # one from a species-keyed lookup would be a coin flip, and a wrong pin
        # manufactures findings (a live mon pinned to a dead twin's HP).  Skip
        # the whole side, keep live-tracked values, and surface the count.
        keys = [k for k in (_species_key(p.name) for p in mons) if k]
        if len(keys) != len(set(keys)):
            stats["hptruth_roster_dupe"] = stats.get("hptruth_roster_dupe", 0) + 1
            continue
        for pkmn in mons:
            # ILLUSION: the truth entries are keyed by the PHYSICAL mon, while a
            # disguised bearer is standing under the shown species' name.
            # `_apply_illusion` (checker.py) has already rebuilt this object as
            # the bearer -- level, types, moveset, ability, max HP -- and stamps
            # the identity it resolved, so honour it rather than re-resolving to
            # the disguise.
            true_species = getattr(pkmn, "illusion_true_species", None)
            rec = _match_exact_mon(lookup, true_species or pkmn.name)
            if rec is None:
                stats["hptruth_misses"] = stats.get("hptruth_misses", 0) + 1
                continue
            try:
                hp = int(rec[0])
                truth_max = int(rec[1])
            except (TypeError, ValueError, IndexError):
                stats["hptruth_misses"] = stats.get("hptruth_misses", 0) + 1
                continue
            # IDENTITY GUARD: the entry's max HP is a property of the physical
            # mon, so it must agree with the reconstruction's own max HP.  When
            # it does not, this entry belongs to a DIFFERENT mon than the object
            # in hand -- an unresolved disguise, a forme the truth was recorded
            # under, a roster desync -- and pinning it would manufacture
            # findings (a live mon on a dead twin's HP).  DECLINE, by name: the
            # live-tracked value stands and the count is visible.
            if pkmn.max_hp and truth_max and int(pkmn.max_hp) != truth_max:
                stats["hptruth_identity_mismatch"] = (
                    stats.get("hptruth_identity_mismatch", 0) + 1
                )
                continue
            hp_certificate.set_exact(pkmn, hp)
            stats["hptruth_applied"] = stats.get("hptruth_applied", 0) + 1


# ---------------------------------------------------------------------------
# Direct-damage event extraction
# ---------------------------------------------------------------------------


@dataclass
class DamageEvent:
    line_index: int
    ctx_id: int
    attacker_slot: str
    defender_slot: str
    move: str
    crit: bool
    prev_hp: int | None
    new_hp: int | None
    shown_max: int | None
    lethal: bool
    capped: bool = False
    raw: str = ""
    exclusion: str | None = None
    # 1-based position of this -damage line among the ones its move context
    # produced (PS emits one per landed hit, in order)
    hit_ordinal: int = 1
    # PS's `move.hit` for this line, filled in the classification pass only when
    # the context's -damage lines can be indexed unambiguously; None otherwise
    hit_index: int | None = None
    # cumulative `side.totalFainted` of the ATTACKER's side at this line
    attacker_side_faints: int = 0
    # The DEFENDER already attempted an action (|move| or |cant|) earlier in this
    # turn's block, i.e. PS already ran `runEvent('BeforeMove')` on it
    # (sim/battle-actions.ts:254) before this hit resolved.  Any volatile whose
    # condition removes itself in an onBeforeMove handler is therefore already gone
    # by the time this damage is computed -- see `PsCombatContext.
    # defender_before_move_ran`.
    defender_acted_before: bool = False
    # Volatiles the ATTACKER gained from a `-start` line EARLIER in this block --
    # event-time state the turn-start snapshot does not carry (see
    # `_FOLDABLE_START_VOLATILES`).  Folded into the attacker for this hit's
    # derivation; non-empty also disables the engine cross-check, whose preview
    # call reads the same stale snapshot.
    attacker_gained_volatiles: frozenset = frozenset()
    # This hit came from a Sleep-Talk-called move (`[from] move: Sleep Talk`): the
    # attacker is STILL asleep at damage time, so status-reading attack modifiers
    # (Guts, Facade, Hex, ...) apply.  Passed to the engine preview so its
    # before_move status-hiding pass -- correct for a DIRECTLY selected move -- is
    # bypassed exactly as it is on the engine's real Sleep Talk path.
    sleep_talk_called: bool = False

    @property
    def delta(self) -> int | None:
        if self.prev_hp is None or self.new_hp is None:
            return None
        return self.prev_hp - self.new_hp


@dataclass
class DamageRecord:
    """One in-scope membership result, fully self-describing for triage."""

    turn: int
    attacker: str
    defender: str
    move: str
    user_move: str
    opp_move: str
    crit: bool
    observed_delta: int
    lethal: bool
    max_roll: int
    rolls: list[int]
    member: bool
    nearest_delta: int
    status: str  # "member" | "lethal_member" | "diverged" | "engine_no_damage"
    raw: str = ""
    game: str = ""
    state_string: str = ""
    # check (b): the engine's own 16 values for this state/crit-flag, and whether
    # they matched the PS-exact set elementwise.  None when the engine produced
    # nothing usable (or the event was not engine-checkable, e.g. Fickle Beam).
    engine_rolls: list[int] | None = None
    engine_agrees: bool | None = None


def _parse_hp_token(token: str) -> tuple[int | None, int | None, bool]:
    """"199/269" -> (199, 269, False); "199/269 brn" same; "0 fnt" ->
    (0, None, True); "100/100" opponent-side fractions parse identically."""
    token = token.strip()
    if not token:
        return None, None, False
    head = token.split(" ")[0].split("|")[0]
    if head == "0" or token.startswith("0 fnt"):
        return 0, None, True
    m = re.match(r"^(\d+)/(\d+)$", head)
    if not m:
        return None, None, False
    return int(m.group(1)), int(m.group(2)), False


def _slot_of(token: str) -> str:
    return token.split(":")[0].strip()


def _species_from_details(details: str) -> str:
    return _species_key(details.split(",")[0].strip()) or ""


def extract_direct_damage_events(
    block_lines: list[str],
    user_pid: str,
    user_species: str | None,
    opp_species: str | None,
    user_hp: int | None,
    user_max_hp: int | None,
) -> tuple[list[DamageEvent], Counter, dict]:
    """Walk one turn's resolution block; return (events, counters, sidedata).

    Events cover EVERY no-[from] |-damage| attributable to a move context; each
    carries exclusion=None (candidate for membership) or a reason.  sidedata
    carries the executed move per slot and the acting order (for the engine
    call's defending-choice / first-move arguments)."""
    opp_pid = "p1" if user_pid == "p2" else "p2"
    user_slot = user_pid + "a"
    opp_slot = opp_pid + "a"

    slot_species = {
        user_slot: _species_key(user_species),
        opp_slot: _species_key(opp_species),
    }
    turnstart_species = dict(slot_species)
    hp_track: dict[str, tuple[int, int]] = {}
    if user_hp is not None and user_max_hp:
        hp_track[user_slot] = (int(user_hp), int(user_max_hp))

    counters: Counter = Counter()
    events: list[DamageEvent] = []
    confounders: list[tuple[int, str]] = []
    executed_move: dict[str, str] = {}
    first_actor: str | None = None
    hp_changed_slots: set[str] = set()
    # slot -> first line index it gained the Charge volatile this turn
    charge_start_first: dict[str, int] = {}
    # (line index, slot, volatile) for every foldable event-time `-start`
    volatile_start_first: list[tuple[int, str, str]] = []
    # slot -> first line index a Substitute absorbed a hit for it (PS still
    # increments timesAttacked for sub-absorbed hits: HIT_SUBSTITUTE is 0,
    # `typeof moveDamage[i] === 'number'` in sim/battle-actions.ts:993-995)
    sub_hit_first: dict[str, int] = {}
    # ctx id -> the |-hitcount| PS printed for that use (hit - 1, i.e. the
    # number of hits that actually executed; sim/battle-actions.ts:977)
    ctx_hitcount: dict[int, int] = {}
    # side -> running count of |faint| lines seen so far in this block.  PS
    # increments `side.totalFainted` in exactly the same place it emits the
    # |faint| line (sim/battle.ts:2549-2551), so this IS totalFainted's delta.
    side_faints: dict[str, int] = {user_pid: 0, opp_pid: 0}

    ctx = None  # {"id", "attacker", "target", "move", "crit", "hits"}
    ctx_seq = 0
    # the move context a later |-hitcount| belongs to; unlike `ctx` it SURVIVES
    # the |faint| that PS prints between the last hit and the hitcount
    hitcount_ctx_id = None

    hp_change_first: dict[str, int] = {}

    def _record_hp(slot: str, cur: int | None, mx: int | None, li: int | None = None) -> None:
        if cur is None:
            return
        # any -damage/-heal/-sethp line marks the slot's HP as touched from
        # that index on (li is None for switch lines: a fresh occupant)
        if li is not None:
            hp_change_first.setdefault(slot, li)
        prev = hp_track.get(slot)
        if mx is None and prev is not None:
            mx = prev[1]
        if mx is not None:
            hp_track[slot] = (cur, mx)

    for li, line in enumerate(block_lines):
        parts = line.split("|")
        if len(parts) < 2:
            continue
        action = parts[1]

        if action == "move" and len(parts) >= 4:
            attacker = _slot_of(parts[2])
            move_id = normalize_name(parts[3])
            target = None
            if len(parts) >= 5:
                tok = parts[4].strip()
                if tok.startswith("p1") or tok.startswith("p2"):
                    target = _slot_of(tok)
            executed_move[attacker] = move_id
            if first_actor is None:
                first_actor = attacker
            ctx_seq += 1
            ctx = {
                "id": ctx_seq,
                "attacker": attacker,
                "target": target,
                "move": move_id,
                "crit": False,
                "hits": 0,
                # PS calls Sleep Talk's inner move with the user STILL asleep
                # (data/moves.ts sleeptalk `sleepUsable: true`: the sleep condition's
                # onBeforeMove returns without curing), so Guts / Facade / Quick Feet /
                # Hex still see the status.  A move the user selected DIRECTLY can only
                # execute after it wakes.  The engine's preview export takes only a move
                # NAME, so it has to be told which of the two this line is.
                "sleep_talk": "[from]" in line and "sleep talk" in line.lower(),
            }
            hitcount_ctx_id = ctx_seq
            continue

        if action in ("switch", "drag", "replace") and len(parts) >= 4:
            slot = _slot_of(parts[2])
            slot_species[slot] = _species_from_details(parts[3])
            if len(parts) >= 5:
                cur, mx, _ = _parse_hp_token(parts[4])
                _record_hp(slot, cur, mx)
            if first_actor is None and action == "switch":
                first_actor = slot
            ctx = None
            hitcount_ctx_id = None
            continue

        if action in _PHASE_ACTIONS:
            if action == "faint" and len(parts) >= 3:
                fainted_pid = _slot_of(parts[2])[:2]
                if fainted_pid in side_faints:
                    side_faints[fainted_pid] += 1
            ctx = None
            # NOT hitcount_ctx_id: PS runs `faintMessages()` immediately BEFORE
            # it prints `|-hitcount|` (sim/battle-actions.ts:975-978), so a
            # multi-hit that KOs emits |faint| between the last -damage and the
            # hitcount -- and the hitcount still belongs to that use.
            if action != "faint":
                hitcount_ctx_id = None
            continue

        if not action.startswith("-"):
            ctx = None
            hitcount_ctx_id = None
            continue

        slot = _slot_of(parts[2]) if len(parts) >= 3 else ""

        # `|-crit|` belongs to ONE hit, not to the whole use.  PS prints it from
        # getDamage (sim/battle-actions.ts, inside the per-hit
        # hitStepMoveHitLoop), so on a multi-hit move each hit gets its own
        # line and the flag must be consumed by the hit that follows it.  A hit
        # can end WITHOUT a |-damage| line -- a Substitute absorbs it and PS
        # prints `-activate ... move: Substitute|[damage]`, or the sub breaks
        # and PS prints `-end ... Substitute` -- and both of those consume the
        # flag too.  Attributing the crit to the whole event instead made the
        # NEXT visible hit inherit a crit it never had (synth19270 T26: the
        # |-crit| sits before the 4th of 5 Bullet Seed hits, which broke the
        # substitute; the 5th hit is the only one with a -damage line and was
        # derived as a crit, so the observed 24 sat at 0.60x of the crit set).
        if action == "-crit":
            if ctx is not None:
                ctx["crit"] = True
            continue

        if action == "-hitcount" and len(parts) >= 4 and hitcount_ctx_id is not None:
            # `|-hitcount|<target>|<n>` -- note the target token loses its
            # position letter once the target has fainted ("p1: Gengar", not
            # "p1a: Gengar"), so it is never parsed as a slot here; the count
            # belongs to the move context by position alone.
            try:
                hitcount = int(parts[3])
            except ValueError:
                pass
            else:
                ctx_hitcount[hitcount_ctx_id] = hitcount
            continue

        if action == "-end" and len(parts) >= 4:
            label = parts[3].strip()
            if "Future Sight" in label or "Doom Desire" in label:
                ctx = None  # delayed hit: next bare -damage is unattributable
                counters["delayed_move"] += 1
            elif label == "Substitute" and ctx is not None and slot != ctx["attacker"]:
                # this hit BROKE the substitute: PS took the damage on the sub
                # (data/moves.ts:18334-18355 onTryPrimaryHit ->
                # `target.removeVolatile('substitute')`) and emitted no
                # |-damage|, so the hit still consumed any |-crit| printed for
                # it.  See the -crit comment below.
                ctx["crit"] = False
                sub_hit_first.setdefault(slot, li)
            continue

        # Libero/Protean/Color Change/Soak: the combatant's types changed
        # mid-turn; the pre-turn state computes with the stale typing
        if action == "-start" and len(parts) >= 4 and "typechange" in parts[3].lower():
            confounders.append((li, "typechange"))
            continue

        # Charge gained mid-turn (Electromorphosis / Wind Power `-start ...
        # Charge`, or the move Charge): the holder's NEXT Electric hit is
        # doubled, which the pre-turn state cannot express -- attacker-scoped
        # confounder for its subsequent hits this turn
        if action == "-start" and len(parts) >= 4 and parts[3].strip() in (
            "Charge",
            "move: Charge",
        ):
            charge_start_first.setdefault(slot, li)
            continue

        # every other `-start`: an event-time volatile.  fp's own reconstruction
        # names it `normalize_name(parts[3].split(":")[-1])`
        # (battle_modifier.start_volatile_status:1866), so the same
        # normalisation is used here and the folded name matches what a
        # turn-start occurrence would have been called.
        if action == "-start" and len(parts) >= 4:
            vol = normalize_name(parts[3].split(":")[-1])
            if vol in _FOLDABLE_START_VOLATILES or vol.startswith(
                _PARADOX_VOLATILE_PREFIXES
            ):
                volatile_start_first.append((li, slot, vol))
            elif vol in _VOLATILE_RELEVANT:
                confounders.append((li, "volatile_start"))
            continue

        if action in ("-damage", "-heal", "-sethp") and len(parts) >= 4:
            cur, mx, fainted = _parse_hp_token(parts[3])
            if action == "-damage" and "[from]" not in line:
                if ctx is None:
                    counters["unattributed"] += 1
                elif slot == ctx["attacker"]:
                    counters["self_damage"] += 1  # Substitute/Belly Drum cost
                elif ctx["target"] is not None and ctx["target"] != slot:
                    counters["target_mismatch"] += 1
                else:
                    prev = hp_track.get(slot)
                    ctx["hits"] += 1
                    events.append(
                        DamageEvent(
                            line_index=li,
                            ctx_id=ctx["id"],
                            attacker_slot=ctx["attacker"],
                            defender_slot=slot,
                            move=ctx["move"],
                            crit=ctx["crit"],
                            prev_hp=prev[0] if prev else None,
                            new_hp=cur,
                            shown_max=mx if mx is not None else (prev[1] if prev else None),
                            lethal=fainted,
                            raw=line,
                            hit_ordinal=ctx["hits"],
                            attacker_side_faints=side_faints.get(
                                ctx["attacker"][:2], 0
                            ),
                            sleep_talk_called=ctx["sleep_talk"],
                        )
                    )
                    ctx["crit"] = False
            _record_hp(slot, cur, mx, li)
            hp_changed_slots.add(slot)
            continue

        if action == "-activate" and len(parts) >= 4:
            label = parts[3].strip()
            if "Substitute" in label and "[damage]" in line:
                counters["substitute"] += 1
                sub_hit_first.setdefault(slot, li)
                # the substitute ate this hit: it produced no |-damage| line but
                # it DID consume the |-crit| printed for it (see -crit above)
                if ctx is not None and slot != ctx["attacker"]:
                    ctx["crit"] = False
                continue
            if label.startswith("ability:") or label.startswith("item:"):
                confounders.append((li, "activate"))
            continue

        if action in _BOOST_ACTIONS:
            confounders.append((li, "boost"))
        elif action in ("-status", "-curestatus"):
            confounders.append((li, "status"))
        elif action in ("-enditem", "-item"):
            confounders.append((li, "item"))
        elif action == "-ability":
            confounders.append((li, "ability"))
        elif action == "-weather" and "[upkeep]" not in line:
            confounders.append((li, "weather"))
        elif action in ("-fieldstart", "-fieldend"):
            confounders.append((li, "terrain"))
        elif action in ("-sidestart", "-sideend"):
            confounders.append((li, "screen"))
        elif action == "-swapsideconditions":
            # Court Change moves the screens (and hazards) to the other side
            # mid-turn: PS updates each condition's owning side
            # (data/moves.ts courtchange onHitField), so a later hit this turn
            # is judged against a screen set the pre-turn state cannot express
            confounders.append((li, "swapsideconditions"))
        elif action in ("-transform", "-formechange"):
            confounders.append((li, "forme"))

    # ---- classification pass -------------------------------------------------
    # index at which each slot's silent self-volatile move resolved
    defender_silent_from: dict[str, int] = {}
    # index of each slot's FIRST action attempt this turn.  PS runs
    # `runEvent('BeforeMove')` inside `runMove` (sim/battle-actions.ts:254) for every
    # action attempt, including Dancer's external copies (:343 passes
    # externalMove: true, and runMove still runs the event) and including attempts that
    # are then aborted -- a |cant| line means BeforeMove ran and one of its handlers
    # returned false, since flinch/sleep/recharge/paralysis are themselves onBeforeMove
    # handlers.  A `[from]`-tagged |move| (Sleep Talk's inner move, Magic Bounce, ...)
    # can only follow its own outer action line, so taking the FIRST index per slot is
    # unaffected by them.
    first_action_from: dict[str, int] = {}
    for li, line in enumerate(block_lines):
        parts = line.split("|")
        if len(parts) >= 4 and parts[1] == "move":
            mv = normalize_name(parts[3])
            if mv in DEFENDER_SILENT_VOLATILE_MOVES:
                defender_silent_from.setdefault(_slot_of(parts[2]), li)
        if len(parts) >= 3 and parts[1] in ("move", "cant"):
            first_action_from.setdefault(_slot_of(parts[2]), li)

    # hits-per-context, used to decide whether a context's -damage lines can be
    # mapped onto PS's `move.hit` indices
    ctx_damage_lines: Counter = Counter(ev.ctx_id for ev in events)

    for ev in events:
        acted = first_action_from.get(ev.defender_slot)
        ev.defender_acted_before = acted is not None and acted < ev.line_index
        ev.attacker_gained_volatiles = frozenset(
            vol
            for idx, vslot, vol in volatile_start_first
            if idx < ev.line_index and vslot == ev.attacker_slot
        )
        # Under EXACT derivation a multi-hit move is not ambiguous by itself:
        # PS runs the whole damage chain independently per hit
        # (sim/battle-actions.ts hitStepMoveHitLoop), so for a CONSTANT-base-
        # power multi-hit every hit's delta must be a member of the SAME
        # 16-value set.  Variable-BP multi-hits (Triple Kick / Triple Axel /
        # Beat Up) and Parental Bond pairs instead need the hit INDEX, which is
        # recoverable exactly when the context produced one -damage line per
        # executed hit -- i.e. when their count equals the |-hitcount| PS
        # printed.  A sub-absorbed hit (an `-activate ... Substitute [damage]`
        # and no -damage line) breaks that equality and the index stays None,
        # which makes the derivation refuse rather than guess.
        hitcount = ctx_hitcount.get(ev.ctx_id)
        if hitcount is None:
            # no |-hitcount| line: a single-hit use, so this is hit 1
            ev.hit_index = 1 if ctx_damage_lines[ev.ctx_id] == 1 else None
        elif ctx_damage_lines[ev.ctx_id] == hitcount:
            ev.hit_index = ev.hit_ordinal
        else:
            ev.hit_index = None
        if ev.move in FIXED_DAMAGE_MOVES and ev.move not in EXACT_FIXED_DAMAGE_MOVES:
            ev.exclusion = "fixed_damage"
            continue
        if ev.move in SCOPE_EPHEMERAL_MOVES:
            # Avalanche / Stomping Tantrum: BP gated on ephemeral
            # damage_dealt / last_move_failed state the preview API cannot
            # seed -- excluded by scope, under a named bucket for visibility
            ev.exclusion = "scope_ephemeral_state"
            continue
        if slot_at_index(block_lines, ev, turnstart_species) is False:
            ev.exclusion = "combatant_not_turnstart"
            continue
        if ev.defender_slot != user_slot:
            ev.exclusion = "fraction_limited"
            continue
        sil = defender_silent_from.get(ev.defender_slot)
        if sil is not None and sil < ev.line_index:
            ev.exclusion = "defender_silent_volatile"
            continue
        chg = charge_start_first.get(ev.attacker_slot)
        if chg is not None and chg < ev.line_index:
            # the attacker gained Charge mid-turn BEFORE this hit: its Electric
            # damage is doubled relative to the pre-turn state
            ev.exclusion = "confounded_charge"
            continue
        if ev.move == "ragefist":
            # Rage Fist BP = 50 + 50*timesAttacked, read at move time (PS
            # data/moves.ts:14583); a same-turn hit landing on the attacker
            # BEFORE it moves (direct move damage, or a sub-absorbed hit --
            # both increment timesAttacked, sim/battle-actions.ts:993-995)
            # raises BP above what the pre-turn state models.
            prior_hit = any(
                e2.line_index < ev.line_index
                and e2.defender_slot == ev.attacker_slot
                for e2 in events
            ) or sub_hit_first.get(ev.attacker_slot, len(block_lines)) < ev.line_index
            if prior_hit:
                ev.exclusion = "confounded_timesattacked"
                continue
        if ev.move in ATTACKER_HP_BP_MOVES and ev.attacker_slot in hp_changed_slots:
            # conservative: any HP touch on the attacker's slot this turn
            ev.exclusion = "attacker_hp_dependent"
            continue
        conf = next(
            (reason for idx, reason in confounders if idx < ev.line_index), None
        )
        if conf is not None:
            ev.exclusion = "confounded_" + conf
            continue
        if ev.prev_hp is None or ev.new_hp is None:
            ev.exclusion = "hp_desync"
            continue
        if (
            ev.shown_max is not None
            and user_max_hp
            and ev.shown_max != int(user_max_hp)
        ):
            ev.exclusion = "hp_desync"
            continue
        if ev.delta is None or ev.delta < 0:
            ev.exclusion = "hp_desync"
            continue
        # survival caps: exactly 1 HP left + a sash/sturdy/endure marker in the
        # same instant means the roll was clamped -- lower-bound semantics
        if ev.new_hp == 1 and _survival_cap_near(block_lines, ev.line_index):
            ev.capped = True

    sidedata = {
        "executed_move": executed_move,
        "first_actor": first_actor,
        "turnstart_species": turnstart_species,
        "hp_change_first": hp_change_first,
    }
    return events, counters, sidedata


def slot_at_index(block_lines, ev: DamageEvent, turnstart_species: dict) -> bool:
    """True when both combatants of `ev` are still the turn-start actives at
    the event line (no pivot switch/replacement happened for either slot)."""
    current = dict(turnstart_species)
    for line in block_lines[: ev.line_index]:
        parts = line.split("|")
        if len(parts) >= 4 and parts[1] in ("switch", "drag", "replace"):
            current[_slot_of(parts[2])] = _species_from_details(parts[3])
    return (
        current.get(ev.attacker_slot) == turnstart_species.get(ev.attacker_slot)
        and current.get(ev.defender_slot) == turnstart_species.get(ev.defender_slot)
        and turnstart_species.get(ev.attacker_slot) is not None
        and turnstart_species.get(ev.defender_slot) is not None
    )


_SURVIVAL_MARKERS = ("Focus Sash", "ability: Sturdy", "Endure")


def _survival_cap_near(block_lines: list[str], idx: int) -> bool:
    """A sash/Sturdy/Endure marker within the same instant as the damage line
    (scan both directions until a phase/action boundary)."""

    def _is_boundary(line: str) -> bool:
        parts = line.split("|")
        return len(parts) >= 2 and not parts[1].startswith("-") and parts[1] != ""

    for line in block_lines[idx + 1 :]:
        if _is_boundary(line):
            break
        if any(m in line for m in _SURVIVAL_MARKERS):
            return True
    for line in reversed(block_lines[:idx]):
        if _is_boundary(line):
            break
        if any(m in line for m in _SURVIVAL_MARKERS):
            return True
    return False


# ---------------------------------------------------------------------------
# Per-turn membership driver
# ---------------------------------------------------------------------------


def _illusion_active(reveals, pid: str, species_key: str | None, turn: int) -> bool:
    """True when this side's active may be (or historically was) an Illusion
    disguise.  The turn window is deliberately NOT applied: once a |replace|
    reveals a disguise species for this player, the reconstruction of BOTH the
    fake and the real bearer of that species is unreliable (the disguise
    accumulates the zoroark's moves/level onto the real species object), so
    every turn showing that species is excluded."""
    if species_key is None:
        return False
    if "zoroark" in species_key:
        return True
    for il in (reveals or {}).get("illusions", ()):
        if il["pid"] == pid and species_key in (il["disguise"], il["true_species"]):
            return True
    return False


def _illusion_unresolvable(reveals, pid: str, turn: int) -> bool:
    """True when this side's active on `turn` is a stay the sidecar could pin
    to neither the bearer nor the genuine mon (checker._infer_illusion_spans).
    Such a mon might be the Illusion bearer wearing its face, so its stats --
    and therefore every roll derived from them -- are unknowable.  Refuse
    instead of asserting against a species that may not be there."""
    for start, end in ((reveals or {}).get("illusion_unresolved") or {}).get(pid, ()):
        if start < turn <= end:
            return True
    return False


def _parental_bond_applies(move_id: str) -> bool:
    """`abilities.ts parentalbond onPrepareHit`: the ability turns the use into
    a 2-hit move UNLESS it is a status move, already multihit, flagged
    noparentalbond / charge / futuremove, a spread hit, a Z-move or a Max move.
    (Spread/Z/Max do not exist in gen9 singles randbats.)"""
    mv = all_move_json.get(move_id) or {}
    if mv.get("category") == "status" or mv.get("multihit"):
        return False
    flags = mv.get("flags") or {}
    return not (
        flags.get("noparentalbond") or flags.get("charge") or flags.get("futuremove")
    )


def _beatup_base_powers(attacker, party, party_order) -> tuple:
    """PS's per-hit base powers for Beat Up (data/moves.ts beatup).

    `onModifyMove` builds
        move.allies = pokemon.side.pokemon.filter(
            ally => ally === pokemon || (!ally.fainted && !ally.status))
    and sets `move.multihit = move.allies.length`; the basePowerCallback then
    shifts one ally per hit and returns `5 + floor(setSpecies.baseStats.atk/10)`.

    `side.pokemon` is PARTY order, and PS permutes it on every switch-in (the
    incoming mon swaps into the active slot, battle-actions.ts:129-131), so the
    order has to come from the reconstructed `party_order`, not from fp's
    reveal-ordered `reserve`.  `party` maps species key -> the reconstructed
    Pokemon (only REVEALED mons are in it; a party member that never switched in
    has by construction never fainted and carries no status, which is exactly
    the filter's `!fainted && !status`)."""
    if not party_order:
        raise PsRefusal("ps_unknown_beatup_party")
    if PARTY_ORDER_UNRESOLVED in party_order:
        # a |switch|/|drag| earlier in this game named a species that did not map
        # onto any sidecar party slot, so the switch-in swap that PS applied to
        # `side.pokemon` could not be replayed.  The order we hold is stale and
        # Beat Up's per-hit base powers are read straight off it -- refuse.
        raise PsRefusal("ps_beatup_party_order_unresolved")

    def _slot_for(key: str) -> str:
        # One PS party slot can appear in fp under SEVERAL species keys: a forme
        # change (Terapagos -> Terapagos-Terastal -> Terapagos-Stellar, Ogerpon
        # masks, Palafin) leaves fp holding one Pokemon object per forme, while
        # PS keeps a single `side.pokemon` entry whose `set.species` never
        # moves.  They collapse back onto the set species by prefix; anything
        # that does not resolve to exactly one slot is a genuine desync.
        slot = resolve_party_slot(party_order, key)
        if slot is None:
            raise PsRefusal("ps_beatup_party_desync")
        return slot

    # every mon the protocol revealed has to land in exactly one party slot, or
    # the fainted/status filter below would read a stale (or missing) mon
    slots: dict = {key: [] for key in party_order}
    for key, pkmn in party.items():
        slots[_slot_for(key)].append(pkmn)
    user_key = _slot_for(_species_key(attacker.name) or "")

    bps = []
    for key in party_order:
        # `ally === pokemon` short-circuits the filter, so the user is always in
        if key != user_key and any(
            getattr(p, "fainted", False) or p.hp <= 0 or p.status for p in slots[key]
        ):
            continue
        # `setSpecies` is the SET's species (data/moves.ts:1155), i.e. the
        # team-list species, never a mid-battle forme.  A BATTLE-ONLY forme can
        # never BE a set species: the gen9 randbats generator writes
        # `species: (typeof species.battleOnly === 'string') ? species.battleOnly
        # : species.name` (data/random-battles/gen9/teams.ts:2552) and the team
        # validator applies the same rewrite (sim/team-validator.ts:1649-1669).
        # `key` is the forme the protocol revealed, so Zacian-Crowned (base Atk
        # 150 -> BP 20) has to be read back as Zacian (base Atk 120 -> BP 17).
        entry = pokedex.get(key) or {}
        battle_only = entry.get("battleOnly")
        if isinstance(battle_only, str):
            entry = pokedex.get(normalize_name(battle_only)) or {}
        try:
            base_atk = int(entry["baseStats"]["attack"])
        except (KeyError, TypeError, ValueError):
            raise PsRefusal("ps_unknown_beatup_party")
        bps.append(5 + base_atk // 10)
    if not bps:
        raise PsRefusal("ps_beatup_party_desync")
    return tuple(bps)


def run_for_turn(
    snap,
    block_lines: list[str],
    user_pid: str,
    turn: int,
    u_action,
    o_action,
    tera_sides: set[str],
    reveals: dict | None,
    tolerance: int = 0,
    prior_faints: dict | None = None,
    party_order: dict | None = None,
    prior_lines: list[str] | None = None,
) -> tuple[list[DamageRecord], Counter, list]:
    """Run exact-damage membership for one reconstructed turn.

    Returns (records, counters, findings).  `snap` must already carry the
    request-refreshed user side and the exact-team override.

    `prior_faints` is {pid: cumulative |faint| count BEFORE this turn's block}
    -- PS's `side.totalFainted`, which Last Respects and Supreme Overlord read
    and which (unlike a live "how many are fainted right now" count) does NOT
    go back down when Revival Blessing revives someone.

    `party_order` is {pid: [species_key, ...]} in PS's `side.pokemon` order at
    the start of this turn (the switch-in swap of battle-actions.ts:129-131
    applied over the sidecar's team order), which is the order Beat Up walks."""
    from fp.replay.comparator import Finding, Severity

    counters: Counter = Counter()
    records: list[DamageRecord] = []
    findings: list[Finding] = []

    if calculate_damage is None or battle_to_poke_engine_state is None:
        return records, counters, findings

    user_active = snap.user.active
    opp_active = snap.opponent.active
    if user_active is None or opp_active is None:
        return records, counters, findings

    events, counters, sidedata = extract_direct_damage_events(
        block_lines,
        user_pid,
        user_active.name,
        opp_active.name,
        user_active.hp,
        user_active.max_hp,
    )
    counters["damage_direct_events"] = len(events)

    # PS splits ONE turn into TWO protocol decision blocks whenever a mid-turn
    # switch request fires -- Revival Blessing carries `selfSwitch: true` purely
    # to raise one (data/moves.ts:15120-15124), and pivot moves do the same.
    # This call then sees only the second block, so a `first_actor` derived from
    # it alone names whoever acted AFTER the split and reports the opponent as
    # having moved first.  Analytic reads exactly that order (`this.queue
    # .willMove(target)`, data/abilities.ts:110-125), so re-seed from the
    # earlier blocks of the SAME turn whenever they already name an actor.
    # synth1000871 T28: Pawmot's Revival Blessing split the turn and Magnezone's
    # Volt Switch was derived unboosted (max_roll 72 vs PS's observed 82).
    for _prior in prior_lines or ():
        _pp = _prior.split("|")
        if len(_pp) >= 4 and _pp[1] in ("move", "switch"):
            sidedata["first_actor"] = _slot_of(_pp[2])
            break

    opp_pid = "p1" if user_pid == "p2" else "p2"
    # Zoroark illusion spans: sidecar stats keyed by species are wrong for a
    # disguised mon, so its turns are excluded outright.
    illusion = _illusion_active(
        reveals, user_pid, _species_key(user_active.name), turn
    ) or _illusion_active(reveals, opp_pid, _species_key(opp_active.name), turn)
    # a side whose Illusion bearer was never pinned to an occupancy: nothing on
    # that side can be trusted to be the species it is shown as
    illusion_unresolved = _illusion_unresolvable(
        reveals, user_pid, turn
    ) or _illusion_unresolvable(reveals, opp_pid, turn)

    # full-HP-gated defender abilities (Multiscale / Shadow Shield / Tera
    # Shell): a mid-turn heal/chip before the hit flips the gate relative to
    # the pre-turn state (e.g. Lugia Recovers to full, THEN is hit -- reality
    # halves, the pre-turn state does not), so the event is unassertable
    hp_gate_ability = (user_active.ability or "") in DEFENDER_FULL_HP_ABILITIES
    attacker_hp_gate = (opp_active.ability or "") in ATTACKER_HP_GATED_ABILITIES
    hp_change_first = sidedata["hp_change_first"]

    parental_bond = (opp_active.ability or "") == "parentalbond"

    in_scope = []
    for ev in events:
        if ev.exclusion is None and parental_bond and _parental_bond_applies(ev.move):
            # PS turns the use into a 2-hit move and gives hit 2 (and only hit 2)
            # `modify(d, 0.25)` (abilities.ts parentalbond onPrepareHit +
            # battle-actions.ts:1738-1743), so the two hits do NOT share one roll
            # set -- they are two separate derivations keyed on `move.hit`.  That
            # is derivable whenever the hit index is, so only an unindexable use
            # still refuses.
            if ev.hit_index is None:
                ev.exclusion = "parental_bond_index_ambiguous"
        if ev.exclusion is None and illusion:
            ev.exclusion = "illusion"
        if ev.exclusion is None and illusion_unresolved:
            ev.exclusion = "illusion_unresolved_bearer"
        if (
            ev.exclusion is None
            and hp_gate_ability
            and ev.prev_hp is not None
            and ev.prev_hp != user_active.hp
        ):
            ev.exclusion = "defender_hp_gate"
        if (
            ev.exclusion is None
            and attacker_hp_gate
            and hp_change_first.get(ev.attacker_slot, len(block_lines)) < ev.line_index
        ):
            # a Swarm/Blaze/... attacker whose HP moved before it attacked:
            # the pinch gate may have flipped relative to the pre-turn state
            ev.exclusion = "attacker_hp_gate"
        if ev.exclusion is None:
            in_scope.append(ev)
        else:
            counters["excluded_" + ev.exclusion] += 1
    if not in_scope:
        return records, counters, findings

    # engine state for this turn; terastallization resolves before any move,
    # so a side that tera'd this turn is pre-applied on a copy
    state_snap = snap
    if tera_sides:
        state_snap = deepcopy(snap)
        if "user" in tera_sides and state_snap.user.active.tera_type:
            state_snap.user.active.terastallized = True
        if "opp" in tera_sides and state_snap.opponent.active.tera_type:
            state_snap.opponent.active.terastallized = True

    try:
        state = battle_to_poke_engine_state(state_snap)
    except BaseException:
        counters["damage_state_errors"] += 1
        return records, counters, findings

    executed = sidedata["executed_move"]
    user_slot = user_pid + "a"
    opp_slot = opp_pid + "a"
    s1_first = sidedata["first_actor"] != opp_slot  # user acted (or switched) first

    # defending-side choice: the move the user actually executed (a called move
    # like Sleep Talk's Ice Beam matters for Sucker Punch / Avalanche handling)
    if u_action is not None and u_action[0] == "switch":
        s1_move = "switch"
    else:
        s1_move = executed.get(user_slot) or (u_action[1] if u_action else "switch")

    # PS's `side.totalFainted` for the ATTACKING (opponent) side: the cumulative
    # number of |faint| lines that side has produced, NOT the number of its mons
    # currently fainted.  data/moves.ts lastrespects reads exactly this, and
    # `totalFainted` never decreases -- a Revival Blessing revive leaves it
    # alone (sim/battle.ts:2551 only ever increments), so a live "count the
    # fainted reserves" reading under-counts after every revive.
    base_faints = int((prior_faints or {}).get(opp_pid, 0))

    ps_ctx = PsCombatContext(
        weather=getattr(state_snap, "weather", None),
        terrain=getattr(state_snap, "field", None),
        defender_screens=frozenset(
            k for k, v in (state_snap.user.side_conditions or {}).items() if v
        ),
        attacker_fainted_allies=base_faints,
        attacker_moves_first=(sidedata["first_actor"] == opp_slot)
        if sidedata["first_actor"]
        else None,
        gravity=bool(getattr(state_snap, "gravity", False)),
        # the attacker here is always the OPPONENT side, whose HP the protocol
        # only shows in /100 fractions.
        #
        # NOT yet threaded to `hp_certificate.is_exact(state_snap.opponent
        # .active)`, deliberately.  Certificates would make Eruption / Water
        # Spout / Flail / Reversal base power and the Blaze/Defeatist pinch
        # gates decidable on certified turns -- real assertion scope the
        # reconstruction now has the information to claim.  But PS reads
        # `attacker.hp` AT HIT TIME while this context is built from the
        # TURN-START snapshot, so it would have to be a per-EVENT flag gated on
        # `hp_change_first[opp_slot] >= ev.line_index` (the same staleness test
        # `attacker_hp_gate` already applies below).  Widening what the
        # membership check asserts has to be measured on the full corpus in its
        # own right, not folded into the certificate work.
        attacker_hp_exact=False,
    )
    # the PS derivation runs on the same state the engine call gets (tera
    # pre-applied when a side terastallized this turn)
    ps_attacker = state_snap.opponent.active
    ps_defender = state_snap.user.active

    opp_party = {}
    for p in list(state_snap.opponent.reserve) + (
        [state_snap.opponent.active] if state_snap.opponent.active else []
    ):
        key = _species_key(p.name)
        if key:
            opp_party[key] = p
    opp_party_order = list((party_order or {}).get(opp_pid) or ())

    for ev in in_scope:
        s2_move = ev.move
        delta = ev.delta
        lethal = ev.lethal or ev.capped

        # per-hit context: `move.hit`, Beat Up's ally list, the Parental Bond
        # arm, and any mid-turn faint that already bumped `side.totalFainted`
        # before this line
        pb_hit = parental_bond and _parental_bond_applies(ev.move)
        try:
            ev_ctx = replace(
                ps_ctx,
                attacker_fainted_allies=base_faints + ev.attacker_side_faints,
                hit_index=ev.hit_index,
                parental_bond_hit2=bool(pb_hit and ev.hit_index == 2),
                defender_before_move_ran=ev.defender_acted_before,
                beatup_bp=(
                    _beatup_base_powers(ps_attacker, opp_party, opp_party_order)
                    if ev.move == "beatup"
                    else ()
                ),
            )
        except PsRefusal as refusal:
            counters["excluded_" + refusal.reason] += 1
            continue

        # EVENT-TIME ATTACKER.  A `-start` earlier in this block gave the
        # attacker a volatile the turn-start snapshot does not carry; the whole
        # derivation (base-power handlers, stat handlers, the modelled/known
        # gates) must see it, so it runs against a shallow view of the attacker
        # carrying the extra volatiles rather than against the snapshot object.
        # The canonical case is Flash Fire: the defender's own Fire move hits the
        # holder FIRST, `|-start|<slot>|ability: Flash Fire` fires, and the
        # holder's Fire move later in the same block is 1.5x (data/abilities.ts
        # flashfire onModifyAtk/onModifySpA, chain 6144) -- synth21362 T2,
        # synth16405 T10 and 11 more, all previously derived unboosted.
        ev_attacker = ps_attacker
        if ev.attacker_gained_volatiles:
            ev_attacker = copy(ps_attacker)
            ev_attacker.volatile_statuses = list(
                dict.fromkeys(
                    list(getattr(ps_attacker, "volatile_statuses", ()) or ())
                    + sorted(ev.attacker_gained_volatiles)
                )
            )

        # ---- fixed / derived damage: one exact value, no roll set ----------
        if ev.move in EXACT_FIXED_DAMAGE_MOVES:
            # deliberately NOT gated on _check_known/_check_modelled: PS returns
            # the fixed value before ANY ability/item/field event runs, so an
            # unknown item or unmodelled ability cannot change it.  Only the
            # attacker's level and the defender's exact HP are needed, and both
            # are protocol facts.
            try:
                expected = derive_fixed_damage(
                    ps_attacker, ps_defender, ev.move, ev.prev_hp
                )
            except PsRefusal as refusal:
                counters["excluded_" + refusal.reason] += 1
                continue
            except BaseException:
                counters["ps_derivation_errors"] += 1
                continue
            counters["damage_in_scope"] += 1
            counters["damage_fixed_exact_asserted"] += 1
            # a KO'd or survival-capped defender only bounds the value below
            member = expected >= delta if lethal else expected == delta
            nearest_delta = delta - expected
            status = ("lethal_member" if lethal else "member") if member else "diverged"
            counters["damage_" + status] += 1
            if lethal:
                counters["damage_lethal_events"] += 1
            rec = DamageRecord(
                turn=turn,
                attacker=opp_active.name,
                defender=user_active.name,
                move=ev.move,
                user_move=s1_move,
                opp_move=ev.move,
                crit=ev.crit,
                observed_delta=delta,
                lethal=lethal,
                max_roll=expected,
                rolls=[expected],
                member=member,
                nearest_delta=nearest_delta,
                status=status,
                raw=ev.raw,
            )
            if not member and abs(nearest_delta) > tolerance:
                findings.append(
                    Finding(
                        turn,
                        Severity.HARD,
                        "damage",
                        "observed damage {} on {} from {} {} is not the PS-exact "
                        "fixed damage {}".format(
                            delta,
                            user_active.name,
                            opp_active.name,
                            ev.move,
                            expected,
                        ),
                        observed=ev.raw,
                        predicted="fixed={}".format(expected),
                    )
                )
            records.append(rec)
            continue

        # ---- check (a): observed damage vs the PS-EXACT 16-value set -------
        # (tests state reconstruction; no engine value participates)
        try:
            if ev.move == "ficklebeam":
                member, nearest_delta, rolls, arm = ficklebeam_membership(
                    ev_attacker, ps_defender, ev_ctx, delta, lethal, ev.crit
                )
                ps_damage = None
                if arm == "ambiguous":
                    counters["fickle_arm_ambiguous"] += 1
                elif arm == "base":
                    counters["fickle_base_arm"] += 1
                elif arm == "doubled":
                    counters["fickle_doubled_arm"] += 1
            else:
                ps_damage = derive_ps_damage(ev_attacker, ps_defender, s2_move, ev_ctx)
                rolls = ps_damage.rolls(ev.crit)
                member, nearest_delta = check_roll_membership(rolls, delta, lethal)
        except PsRefusal as refusal:
            counters["excluded_" + refusal.reason] += 1
            continue
        except BaseException:
            counters["ps_derivation_errors"] += 1
            continue

        counters["damage_in_scope"] += 1
        max_roll = rolls[15]
        status = ("lethal_member" if lethal else "member") if member else "diverged"

        # ---- check (b) ACTIVE: ENGINE 16-roll set vs the PS-exact set -------
        # Full elementwise comparison against the binding's 16+16 export
        # (calculate_damage_roll_sets).  This is a model check, independent of
        # the observation: a divergence here is an ENGINE bug.
        #
        # SCOPE LIMIT (harness, not engine): variable-BP multi-hit moves.  PS
        # derives each hit's base power from `move.hit` (Triple Axel 20/40/60,
        # Triple Kick 10/20/30, Beat Up one BP per party member), and this module
        # asserts the correct PER-HIT set.  The engine models that correctly in
        # its real path (generate_instructions.rs MultiHitMove::TripleAxel scales
        # base_power by 0.5/1.0/1.5), but the PREVIEW export takes no hit index,
        # so it can only ever return the flat-BP set.  Comparing them would
        # report a harness limitation as an engine bug, so these are counted
        # under their own bucket instead of `engine_vs_ps_diverged`.
        engine_set = None
        engine_agrees = None
        if ev.move in _MULTIHIT_VARIABLE_BP_MOVES:
            counters["engine_skipped_variable_bp_multihit"] += 1
        elif ev.attacker_gained_volatiles:
            # SCOPE LIMIT (harness, not engine): this hit's PS set was derived
            # from EVENT-TIME state, but `calculate_damage_roll_sets` takes the
            # turn-start `state` object -- the same stale snapshot the folding
            # exists to correct.  The two sets would differ BY CONSTRUCTION, so
            # the comparison cannot arbitrate anything and is not made.  (This
            # is the trap the freeze gate flagged: a stale-snapshot divergence
            # carries `engine_agrees: true` from a preview call that shares the
            # staleness, which reads as corroboration and is not.)
            counters["engine_skipped_event_time_state"] += 1
        elif (
            ps_damage is not None
            and calculate_damage_roll_sets is not None
            and state is not None
        ):
            try:
                _, s2_set = calculate_damage_roll_sets(
                    state,
                    s1_move,
                    s2_move,
                    s1_first,
                    s2_sleep_talk_move=ev.sleep_talk_called,
                )
            except BaseException:
                counters["damage_calc_errors"] += 1
            else:
                agree, engine_delta, engine_set = engine_vs_ps(
                    ps_damage, s2_set, ev.crit
                )
                engine_agrees = agree
                if engine_delta is None:
                    counters["engine_no_damage"] += 1
                    engine_agrees = None
                elif agree:
                    counters["engine_elementwise_agrees"] += 1
                    counters["engine_max_agrees"] += 1
                else:
                    counters["engine_vs_ps_diverged"] += 1
                    counters["engine_vs_ps_delta_{:+d}".format(engine_delta)] += 1
                    if engine_delta == 0:
                        counters["engine_vs_ps_diverged_nonmax_only"] += 1

        counters["damage_" + status] += 1
        if lethal:
            counters["damage_lethal_events"] += 1

        rec = DamageRecord(
            turn=turn,
            attacker=opp_active.name,
            defender=user_active.name,
            move=ev.move,
            user_move=s1_move,
            opp_move=s2_move,
            crit=ev.crit,
            observed_delta=delta,
            lethal=lethal,
            max_roll=max_roll,
            rolls=rolls,
            member=member,
            nearest_delta=nearest_delta,
            status=status,
            raw=ev.raw,
            engine_rolls=engine_set,
            engine_agrees=engine_agrees,
        )
        observed_diverged = not member and abs(nearest_delta) > tolerance
        # State is dumped for BOTH failure kinds so either is triageable, but the
        # two are never conflated: check (a) is a state-reconstruction failure
        # against the OBSERVED roll, check (b) an ENGINE-model failure that is
        # independent of what the replay rolled.
        if observed_diverged or engine_agrees is False:
            try:
                rec.state_string = state.to_string()
            except Exception:
                rec.state_string = ""
        if observed_diverged:
            findings.append(
                Finding(
                    turn,
                    Severity.HARD,
                    "damage",
                    "observed damage {} on {} from {} {} (crit={}) not in the "
                    "PS-exact roll set (nearest delta {:+d}; max_roll {})".format(
                        delta,
                        user_active.name,
                        opp_active.name,
                        ev.move,
                        ev.crit,
                        nearest_delta,
                        max_roll,
                    ),
                    observed=ev.raw,
                    predicted="rolls={}".format(rolls),
                )
            )
        if engine_agrees is False:
            findings.append(
                Finding(
                    turn,
                    Severity.HARD,
                    "engine_damage",
                    "ENGINE roll set != PS-exact roll set for {} {} -> {} "
                    "(crit={}): engine {} vs ps {}".format(
                        opp_active.name,
                        ev.move,
                        user_active.name,
                        ev.crit,
                        engine_set,
                        rolls,
                    ),
                    observed="ps={}".format(rolls),
                    predicted="engine={}".format(engine_set),
                )
            )
        records.append(rec)

    return records, counters, findings
