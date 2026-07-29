"""Post-game replay fidelity checker.

Replays a saved foul-play battle log through the bot's OWN parser to reconstruct
the Battle at the start of every turn, then for each turn feeds the true
(revealed) pre-turn state and both sides' actually-chosen actions into the
poke-engine's `generate_instructions`, and flags any observed protocol outcome
that no predicted branch can reproduce.

Scope: gen9 random battles.  Because the opponent's exact randbats spread is
unknown, damage magnitude is NOT asserted here -- the checker verifies
CATEGORICAL, stat-independent mechanics (status/immunity/boost/volatile/weather/
terrain/hazard/item-loss), which is exactly the class the interaction sweep
targets, now grounded in real games.

Limitations (turns are skipped, never falsely flagged):
  * a move used for the FIRST time by the opponent is not yet in the pre-turn
    state, so `from_string` can't parse it -> skipped;
  * turns where either side could not cleanly act (fainted before moving,
    forced replacement) -> skipped via the last_used_move turn guard.

DEFERRED (two-phase) turns are replayed in TWO engine calls.  When a turn
contains a mid-turn decision request (a pivot switch-in after U-turn / Volt
Switch / Flip Turn / Parting Shot / Shed Tail / Baton Pass, or a Revival
Blessing revive-target choice), the engine's phase-1 branch only arms
`force_switch` and PARKS the second mover's move; the whole deferred second
phase (the parked move and its boosts/status/hazards, the revive, the entire
end-of-turn block) is absent from those instructions.  The checker therefore
continues each deferring branch with a second `generate_instructions` call
down the OBSERVED continuation and matches the turn against the union of the
two phases -- see `_replay_deferred_phase` for the mechanics and for why a
pivot the protocol never performed is deliberately NOT continued.
"""

import json
import os
import re
from copy import deepcopy

import constants
from constants import BattleType
from fp.battle import Battle, LastUsedMove, Pokemon, SharedByReference
from fp.helpers import normalize_name
import fp.battle_modifier as _battle_modifier
from fp import hp_certificate
from fp.battle_modifier import update_battle

# update_dataset_possibilities narrows the opponent's possible randbats sets from
# observed damage by round-tripping through the engine. It is a live-play
# opponent-modeling heuristic the checker never consumes (it converts the
# directly-reconstructed revealed state), and it panics (Rust: "Invalid
# PokemonMoveIndex") on mons the protocol has revealed >4 moves for. Neutralize it.
_battle_modifier.update_dataset_possibilities = lambda *a, **k: None

# check_speed_ranges / check_choicescarf are live-play heuristics that infer the
# opponent's speed/scarf from the bot's OWN last_selected_move -- a private choice
# absent from the protocol. Both early-return when that move starts with "switch ",
# so seeding a switch-shaped placeholder makes them skip cleanly instead of raising
# KeyError('') on the empty default; the inference is meaningless offline anyway.
_REPLAY_PLACEHOLDER_MOVE = LastUsedMove(None, "switch _replay_placeholder_", 0)
from fp.replay.comparator import (
    Finding,
    Severity,
    TurnContext,
    compare_turn,
    parse_branches,
    _norm,
)

try:
    from data import pokedex
except Exception:  # pragma: no cover
    pokedex = {}
from fp.replay.protocol import _PHASE_ACTIONS, extract_observed_events, iter_chunks

try:
    from data.pkmn_sets import RandomBattleTeamDatasets
except Exception:  # pragma: no cover
    RandomBattleTeamDatasets = None

try:
    from fp.search.poke_engine_helpers import battle_to_poke_engine_state
    from poke_engine import generate_instructions
except Exception:  # pragma: no cover
    battle_to_poke_engine_state = None
    generate_instructions = None


SWITCH_PREFIX = "switch "


def _format_from_tag(tag: str) -> str:
    # "battle-gen9randombattleblitz-2654342126" -> "gen9randombattleblitz"
    parts = tag.split("-")
    return parts[1] if len(parts) >= 3 else "gen9randombattle"


def _tag_from_chunks(chunks: list[str], fallback: str) -> str:
    for chunk in chunks:
        first = chunk.split("\n", 1)[0].strip()
        if first.startswith(">battle-"):
            return first[1:]
    return fallback


def _find_first_request(chunks: list[str]):
    for chunk in chunks:
        for line in chunk.split("\n"):
            sp = line.split("|")
            if len(sp) >= 3 and sp[1].strip() == "request" and sp[2].strip():
                try:
                    payload = json.loads(sp[2].strip().strip("'"))
                except json.JSONDecodeError:
                    continue
                if payload.get("side"):
                    return payload
    return None


def _detect_tera_sides(block_lines: list[str], user_pid: str) -> set[str]:
    sides: set[str] = set()
    for line in block_lines:
        sp = line.split("|")
        if len(sp) >= 3 and sp[1] == "-terastallize":
            tag = sp[2].split(":")[0].strip()  # "p2a"
            pid = tag[:2]
            sides.add("user" if pid == user_pid else "opp")
    return sides


def _extract_side_action(block_lines: list[str], slot: str):
    """Determine a side's real turn DECISION from the resolution block, keyed on
    its active slot ("p1a"/"p2a").  Returns ("move", move_id) | ("switch",
    species) | None (skip: the side fainted or was prevented before acting, or a
    forced replacement is all that's visible).  Reading the block's FIRST action
    for the slot avoids last_used_move being clobbered by a mid-turn forced
    faint-replacement switch (which carries the same turn number)."""
    from fp.helpers import normalize_name

    for line in block_lines:
        sp = line.split("|")
        if len(sp) < 3:
            continue
        act = sp[1]
        tag = sp[2].split(":")[0].strip()
        if tag != slot:
            continue
        if act == "move" and len(sp) >= 4:
            if "[from]" in line and "ability: Dancer" in line:
                # a Dancer copy is an externalMove PS attributes to the copier
                # (`|move|...|[from] ability: Dancer`), NOT the side's chosen
                # action -- it can precede the real decision when the opponent
                # danced first (synth02764 T2: Oricorio's copied Revelation
                # Dance before its chosen Quiver Dance).  Keep scanning for the
                # first non-Dancer move line.
                continue
            return ("move", normalize_name(sp[3]))
        if act == "switch" and len(sp) >= 4:
            species = normalize_name(sp[3].split(",")[0].strip())
            return ("switch", species)
        if act == "drag":
            # |drag| is a forced phaze (Roar / Whirlwind / Dragon Tail): the
            # dragged-in mon is NOT the side's chosen switch, and reaching a
            # drag before any move line means the side's own decision never
            # visibly resolved -> not a clean 1-action turn, skip it.
            return None
        if act in ("faint", "cant"):
            return None  # fainted / prevented before a clean action
    return None


# ---------------------------------------------------------------------------
# SKIP ATTRIBUTION (bookkeeping only -- changes no checker decision).
#
# Every `turns_skipped` increment carries a named reason and, where the reason
# is itself a family, a sub-bucket.  Keys are flat ints so check_replays'
# Counter aggregation sums them; the `skipped_` prefix marks a top-level reason
# (these sum to turns_skipped) and `skipsub_` marks a sub-classification of one
# of those reasons (these sum to their parent).  Rule 20: a refusal that is only
# counted in aggregate is unattributable, and an unattributable refusal cannot
# be fixed.
# ---------------------------------------------------------------------------

# bounded free-text samples behind the two residual buckets, so "what is in the
# residual" is answerable rather than merely countable
SKIP_RESIDUAL_SAMPLES: list = []
_SKIP_SAMPLE_CAP = 6  # PER KIND -- a global cap would be filled by the commonest
_SKIP_SAMPLE_SEEN: dict = {}


def _skip_sample(kind: str, detail: str) -> None:
    n = _SKIP_SAMPLE_SEEN.get(kind, 0)
    if n < _SKIP_SAMPLE_CAP:
        _SKIP_SAMPLE_SEEN[kind] = n + 1
        SKIP_RESIDUAL_SAMPLES.append((kind, detail))


def _stat_token(s: str) -> str:
    """A stats-key-safe token: lowercase, non-alphanumerics collapsed to '_'."""
    out = []
    prev_us = False
    for c in (s or "").strip().lower():
        if c.isalnum():
            out.append(c)
            prev_us = False
        elif not prev_us:
            out.append("_")
            prev_us = True
    return "".join(out).strip("_") or "unspecified"


def _bump(stats: dict, key: str) -> None:
    stats[key] = stats.get(key, 0) + 1


# reasons for which the block's FRAGMENT kind is part of the identity: the side
# emitted no action line, and whether the block is a whole turn or half of a
# split one is then the difference between "it was prevented" and "the protocol
# put its decision in the other half"
_NO_ACTION_LINE_FAMILY = (
    "noaction_lines",
    "noaction_absent",
    "prevented_confusion",
    "empty_block",
)


def _turn_fragment_kind(snap, block_lines: list[str]) -> str:
    """Whether this resolution block is a WHOLE turn or one FRAGMENT of a turn
    the protocol split in half.

    Blocks are cut at every USER request (`check_log`'s `if not decision`
    loop), and a mid-turn forced replacement -- a faint, or a self-switch pivot
    like U-turn -- issues one.  A single game turn therefore arrives as TWO
    blocks, and each half is missing one side's chosen action by construction:
    the first half ends before the other side has acted, the second half
    contains only the replacement switch and the end-of-turn residue.  That is
    a PROTOCOL-SEGMENTATION fact, not a Pokemon-was-prevented fact, and the two
    must not share a bucket."""
    armed_force = False
    req = getattr(snap, "request_json", None)
    if isinstance(req, dict):
        armed_force = bool(req.get("forceSwitch"))
    elif req:
        armed_force = '"forceSwitch"' in str(req)
    ends_force = any(
        line.startswith("|request|") and '"forceSwitch"' in line
        for line in block_lines
    )
    if armed_force and ends_force:
        return "split_middle"
    if armed_force:
        return "split_2nd_half"
    if ends_force:
        return "split_1st_half"
    return "whole_block"


def _unnamable_action_reason(block_lines: list[str], slot: str) -> str:
    """WHY `_extract_side_action` could not name this slot's chosen action.

    Mirrors that function's scan line-for-line and is only ever called after it
    has already returned None for the same slot, so the `named_*` returns are
    unreachable in practice -- they exist so a divergence between the two scans
    would show up as its own bucket instead of being absorbed into a residual."""
    seen_slot = False
    for line in block_lines:
        sp = line.split("|")
        if len(sp) < 3:
            continue
        act = sp[1]
        tag = sp[2].split(":")[0].strip()
        if tag != slot:
            continue
        seen_slot = True
        if act == "move" and len(sp) >= 4:
            if "[from]" in line and "ability: Dancer" in line:
                continue
            return "named_move"
        if act == "switch" and len(sp) >= 4:
            return "named_switch"
        if act == "drag":
            return "dragged"
        if act == "faint":
            return "fainted"
        if act == "cant":
            why = sp[3] if len(sp) >= 4 else ""
            return "cant_" + _stat_token(why)
    # --- no action line for the slot.  Two further real causes, both of which
    # would otherwise sit in an uninterpretable residual: ---
    # (a) CONFUSION SELF-HIT.  PS emits `-activate|SLOT|confusion` followed by
    #     `-damage|SLOT|...|[from] confusion` and NO `|cant|` and NO `|move|`
    #     (sim/pokemon.ts confusion onBeforeMove).  It is a prevention exactly
    #     like par/slp, but it carries no `|cant|` marker to read it off.
    for line in block_lines:
        sp = line.split("|")
        if (
            len(sp) >= 4
            and sp[1] == "-activate"
            and sp[2].split(":")[0].strip() == slot
            and _stat_token(sp[3]) == "confusion"
        ):
            return "prevented_confusion"
    # (b) a block with no protocol content at all -- only the bookkeeping lines
    #     and the re-issued request.  Neither side acted because nothing
    #     happened in it.
    if not any(
        line.split("|")[1:2] not in ([], [""], ["t:"], ["request"], ["upkeep"], ["turn"])
        for line in block_lines
    ):
        return "empty_block"
    # residual: the slot is absent from the block entirely, or appears only in
    # effect lines with no cause named above
    return "noaction_lines" if seen_slot else "noaction_absent"


def _action_to_move_string(action, side: str, tera_sides: set[str]) -> str | None:
    if action is None:
        return None
    kind, val = action
    if kind == "switch":
        return val  # bare species id for from_string
    if side in tera_sides:
        return val + "-tera"
    return val


def _active_species(battler) -> str | None:
    if battler.active is None:
        return None
    return battler.active.name


def _populate_opponent_knowledge(battler) -> None:
    """Fill each revealed opponent mon's ability/item from randbats when it is
    UNAMBIGUOUS across all of that species' possible sets. The live bot's search
    samples these; the checker's un-sampled state otherwise leaves them unknown,
    so ability/item-driven effects (Contrary self-boosts, Sitrus heals, ...) look
    like engine defects. Ambiguous mons are left unknown (a residual known limit)."""
    if RandomBattleTeamDatasets is None:
        return
    mons = list(battler.reserve)
    if battler.active is not None:
        mons.append(battler.active)
    for pkmn in mons:
        try:
            sets = RandomBattleTeamDatasets.get_pkmn_sets_from_pkmn_name(pkmn)
        except Exception:
            continue
        if not sets:
            continue
        if not pkmn.ability:
            abilities = {s.pkmn_set.ability for s in sets if s.pkmn_set.ability}
            if len(abilities) == 1:
                pkmn.ability = next(iter(abilities))
        if pkmn.item == constants.UNKNOWN_ITEM:
            items = {s.pkmn_set.item for s in sets if s.pkmn_set.item}
            if len(items) == 1:
                pkmn.item = next(iter(items))


_OF_SLOT = re.compile(r"\[of\] (p[12][ab]?): ?([^|]*)")
_FROM_ABILITY = re.compile(r"\[from\] ability: ([^|]+)")
_FROM_ITEM = re.compile(r"\[from\] item: ([^|]+)")
# `|-item|` effects that MOVE the item off the `[of]` pokemon.  `ability: Frisk` is
# deliberately NOT here: it reveals the foe's item without transferring it, and PS
# formats it identically (synth33779 line 344 is a Frisk, lines 356-357 a real Trick).
# Bestow belongs here too: PS names the GIVER in the `[of]` tag
# (data/moves.ts:1257 `this.add('-item', target, myItem, '[from] move: Bestow',
# `[of] ${source}`)`).
_ITEM_TRANSFER_LINE = re.compile(
    r"\[from\] (?:ability: (?:Magician|Pickpocket)|move: (?:Thief|Covet|Bestow))", re.I
)
# Trick / Switcheroo are a SWAP, and PS gives each side its own line with NO `[of]`
# (data/moves.ts:19888-19899), so the line's own subject is the receiver and the line
# says nothing about the donor.  Both this and `_ITEM_TRANSFER_LINE` mark the subject
# as having ACQUIRED the item mid-battle rather than held it from turn 0.
_ITEM_SWAP_LINE = re.compile(r"\[from\] move: (?:Trick|Switcheroo)", re.I)

# actions whose "[of] pXa:" tag names the ability HOLDER. "-heal" is excluded:
# for absorb abilities (Volt/Water Absorb, ...) "[of]" names the ATTACKER while
# the ability belongs to the healed subject.
# "-fail" is included on PS's authority: the unboost-blocking abilities all emit
# `-fail <target>|unboost|...|[from] ability: <X>|[of] <target>` -- the "[of]"
# repeats the TARGET, i.e. the holder (data/abilities.ts:525 Clear Body, :2150
# Inner Focus, :3023 Oblivious, :3152 Own Tempo, :4081 Scrappy).
_OF_OWNS_ABILITY = frozenset(
    (
        "-weather",
        "-fieldstart",
        "-status",
        "-damage",
        "-item",
        "-enditem",
        "-start",
        "-fail",
    )
)

# line kinds that REASSIGN slot occupancy: the slot->species map is mid-update
# while they are read, so a "[from]" attribution keyed on it would name the
# wrong mon.  Excluded from the generic "[from] ability:" harvest below.
_SLOT_REASSIGNING = frozenset(("switch", "drag", "replace", "detailschange"))


def _harvest_reveals(chunks: list[str]) -> dict:
    """Pre-pass over the WHOLE log: harvest per-(player, species) knowledge the
    protocol reveals at ANY point.  Abilities are immutable for the battle, so a
    reveal back-fills every pre-turn snapshot; an item revealed anywhere existed
    from battle start until its -enditem line, recorded as (item, removal_turn or
    None).  |-ability| lines carrying "[from]" (Trace/Skill Swap products) are
    skipped -- they do not name the mon's own set ability.  Abilities are ALSO
    harvested from "[from] ability: X" annotations on any other line kind
    (|move| for a Magic Bounce reflection, |-fail| for Clear Body, ...), which
    for several abilities is the only evidence the protocol ever emits."""
    abilities: dict[tuple[str, str], str] = {}
    items: dict[tuple[str, str], tuple[str, int | None]] = {}
    # Items ACQUIRED mid-battle (Trick / Switcheroo / Bestow / Thief / Covet /
    # Magician / Pickpocket): (pid, species) -> [[gain_turn, item, end_turn|None]].
    # These are deliberately NOT folded into `items`, whose `(item, None)` means
    # "held from battle start" -- back-filling an acquisition into every earlier
    # snapshot is exactly the wrong direction.  Kept as their own timeline so that
    # (a) a later `-enditem` of an ACQUIRED item is not mistaken for evidence about
    # what the mon started with, and (b) snapshots after the acquisition can be
    # filled with what the mon actually holds.
    item_gains: dict[tuple[str, str], list] = {}
    # (pid, species) -> {move id: FIRST turn the mon selected it}.  A dict, not a
    # set, for two reasons.  (a) Insertion order is first-use order, so the
    # traversal in `_backfill_revealed_knowledge` is deterministic; a set of str
    # iterates in HASH order, and under the hard 4-slot cap there that made which
    # moves survived -- and therefore the checker's own turns-checked/skipped
    # split -- PYTHONHASHSEED-dependent (gate-7 blocker G6, reproduced on
    # synth44042: 18/16 at seeds 0,1,3 and 19/15 at seeds 2,4).  (b) The
    # first-use turn is what lets that cap keep the moves the snapshot being
    # reconstructed actually needs.
    moves: dict[tuple[str, str], dict[str, int]] = {}
    roster: dict[str, dict[str, str]] = {}  # pid -> {species: switch-details}
    tera: dict[tuple[str, str], str] = {}  # (pid, species) -> tera type
    illusions: list[dict] = []  # Zoroark disguise spans revealed by |replace|
    # (pid, species) -> turns on which it ATE a berry ("-enditem ... [eat]"), the
    # event that arms Cud Chew's next-turn re-eat.  A Cud Chew RE-eat emits the
    # same line and must not re-arm, so it is excluded via the same same-instant
    # `-activate ... ability: Cud Chew` test protocol._is_cudchew_reeat uses.
    berry_eats: dict[tuple[str, str], set] = {}
    # (pid, species) -> FIRST turn that mon fainted.  Consumed by
    # `_mark_protocol_illusion_ambiguity`: a fainted Illusion bearer cannot
    # re-disguise, so occupancies after its faint are genuine.
    faint_turns: dict[tuple[str, str], int] = {}
    cudchew_activated: set[str] = set()  # slots with a live Cud Chew activation
    slot_species: dict[str, str] = {}
    slot_entry_turn: dict[str, int] = {}  # slot -> turn its CURRENT occupant entered
    # One entry per SLOT OCCUPANCY (switch/drag in -> next switch/drag), carrying
    # the moves that occupant selected itself.  A disguised Zoroark the log never
    # |replace|s is invisible to `illusions` above; `_infer_illusion_spans` proves
    # those occupancies from the sidecar movesets instead.
    occupancies: list[dict] = []
    open_occupancy: dict[str, dict] = {}
    # pid -> sorted turns on which the protocol printed a `|-item|` line for that
    # SIDE, plus the Symbiosis `-activate` that hands an item over without one
    # (data/abilities.ts:4842 adds `-activate ... ability: Symbiosis` and no
    # `-item`).  Every OTHER route that puts a new item on a mon goes through
    # `setItem` with an `-item` announcement -- Trick/Switcheroo
    # (data/moves.ts:19890/19896, 18668/18674), Thief/Covet/Bestow
    # (`_ITEM_TRANSFER_LINE`), Magician (data/abilities.ts:2481), Pickpocket,
    # Recycle (data/moves.ts:14829), Harvest (data/abilities.ts:1798), Pickup
    # (data/abilities.ts:3264) -- so "no `-item` line for this side yet" is a
    # sound proof that NOBODY on the side has acquired anything.  Consumed by
    # `_no_prior_acquisition`, which is the precondition of the item-evidence
    # Illusion discriminator in `_infer_illusion_spans`.  Deliberately counts a
    # PLAIN reveal (Frisk, Air Balloon) too even though it moves nothing: the
    # cost is refusing a little more often, which is the safe direction.
    item_line_turns: dict[str, list[int]] = {}
    # The SAME ledger keyed by LOG ORDINAL as well as turn: (turn, line_idx).
    # A turn counter is too coarse to found the "at most one line on the
    # observation turn" allowance on -- that allowance ASSUMES the single
    # permitted `-item` line is the receiving half of the very transfer being
    # read, and nothing verifies the pairing.  A same-turn Knock Off of a
    # JUST-RECEIVED item is an observation landing AFTER the acquisition, and
    # reading it as a fixed set property fabricates a disguise on a provably
    # genuine mon.  `this.add(...)` appends to the log buffer in strict
    # resolution order, so a monotone per-line ordinal answers "did any
    # acquisition on this side resolve BEFORE this observation?" exactly.
    item_line_events: dict[str, list[tuple[int, int]]] = {}
    line_idx = 0
    # the most recent _PHASE_ACTIONS line.  A move hit's `-damage` is exactly a
    # `-damage` with no `[from]` tag under a `move` phase; every residual /
    # hazard / item / confusion `-damage` carries a `[from]`.  Feeds
    # `survived_damaging_hit`, the PS-impossibility tripwire on the bearer arm.
    last_phase: str | None = None
    # slot -> the slot it is currently swapping items with, from Trick's own
    # `-activate <source>|move: Trick|[of] <target>` (data/moves.ts:19887; note
    # Switcheroo reuses the string 'move: Trick' at :18665).  PS then prints one
    # `-item` per side with NO `[of]`, so the line's subject is the RECEIVER and
    # the donor is only recoverable through this pairing.
    swap_partner: dict[str, str] = {}
    # PER-SLOT sleep-attempt counter (PS `statusState.time`: `sleep` counts DOWN once per
    # attempted action, conditions.ts slp onBeforeMove).  The reconstruction counts it
    # per-POKEMON, which Illusion breaks: the disguised sleeper and the genuine mon of the
    # disguise species are ONE object there, so the real Skeledirge switching in wiped the
    # two attempts the Zoroark wearing its face had already served (synth25500 T45/T46),
    # and the revealed Zoroark reached T53 with a zero counter -- unable to wake in any
    # branch, so its Bitter Malice and that move's `-unboost` were unreachable.  The slot
    # is physical, so counting there survives the swap.  Snapshotted at every |turn| line,
    # i.e. the value at the START of that turn.
    slot_sleep_attempts: dict[str, int] = {}
    sleep_attempts_by_turn: dict[tuple[str, int], int] = {}
    turn = 0

    def _open_occupancy(slot: str, species: str, entry_turn: int, details: str) -> None:
        prev = open_occupancy.get(slot)
        if prev is not None:
            prev["end_turn"] = entry_turn
        # `getFullDetails` appends ", tera:<Type>" iff the pokemon that is
        # ACTUALLY entering is terastallized (sim/pokemon.ts:553), so this is the
        # entrant's own tera state even when the species half is a disguise.
        entry_tera = None
        for piece in details.split(","):
            piece = piece.strip()
            if piece.startswith("tera:"):
                entry_tera = normalize_name(piece[len("tera:") :])
        occ = {
            "pid": slot[:2],
            "slot": slot,
            "species": species,
            "start_turn": entry_turn,
            "end_turn": entry_turn,
            "moves": set(),
            # (turn, item) the PHYSICAL occupant of this slot was PROVEN to be
            # holding.  Slot-keyed, so unlike `reveals["items"]` / `item_gains`
            # it survives Illusion (PS renders the per-mon lines through the
            # disguise's name, sim/pokemon.ts:531, but the SLOT is the real mon).
            "held_items": [],
            # PS breaks Illusion on any DAMAGING MOVE HIT the disguise survives
            # (data/abilities.ts:2061-2064 `onDamagingHit` -> singleEvent('End')
            # -> :2069 `this.add('replace', ...)`), and clears it silently only
            # on faint (:2078 `onFaint`).  So an occupancy that ate a damaging
            # hit, LIVED, and produced no |replace| cannot have been a disguised
            # bearer -- it is the strongest disconfirming evidence PS offers.
            "survived_damaging_hit": False,
            "transformed": False,
            "entry_tera": entry_tera,
            "tera_during": None,
            # the turn during whose RESOLUTION the |-terastallize| landed.  That
            # turn's PRE-state is still un-tera'd, so consumers gate on
            # `tera_during_turn < turn`, exactly as they gate on `start_turn`.
            "tera_during_turn": None,
        }
        open_occupancy[slot] = occ
        occupancies.append(occ)

    def _occupant(slot: str, name_token: str) -> str:
        # randbats mons are unnicknamed, so the tag name is the species when a
        # switch line has not yet populated the slot map
        return slot_species.get(slot) or _species_key(name_token)

    def _end_gain(key, item: str, turn: int) -> bool:
        """Close the open ACQUISITION of `item` on `key` at `turn`.

        Returns True when one was found, which is also the signal that this loss
        says NOTHING about what the mon held from battle start -- the item it just
        lost is one it picked up mid-battle, so it must not be written into `items`
        as a start-of-battle holding.
        """
        for rec in reversed(item_gains.get(key, ())):
            if rec[1] == item and rec[2] is None and rec[0] <= turn:
                rec[2] = turn
                return True
        return False

    def _subject(parts) -> tuple[str, str] | None:
        tag = parts[2]
        pid = _tag_player_pid(tag)
        if pid is None:
            return None
        slot = tag.split(":")[0].strip()
        return pid, _occupant(slot, tag.split(":", 1)[1].strip())

    def _from_ability(line, parts, action) -> None:
        """Record the ability named by this line's `[from] ability: X` tag
        against the mon that OWNS it: the `[of]` slot for the actions where PS
        puts the holder there, the annotated subject otherwise."""
        m = _FROM_ABILITY.search(line)
        ability = normalize_name(m.group(1)) if m else ""
        if not ability:
            return
        of = _OF_SLOT.search(line)
        if of is None:
            sub = _subject(parts)
            if sub is not None:
                abilities.setdefault(sub, ability)
        elif action in _OF_OWNS_ABILITY:
            slot = of.group(1)
            abilities.setdefault(
                (slot[:2], _occupant(slot, of.group(2).strip())), ability
            )
        elif action == "-heal":
            sub = _subject(parts)
            if sub is not None:
                abilities.setdefault(sub, ability)
        # any other "[of]" semantics: ambiguous owner -> skip

    for chunk in chunks:
        for line in chunk.split("\n"):
            if not line.startswith("|"):
                continue
            parts = line.split("|")
            if len(parts) < 3:
                continue
            action = parts[1]
            line_idx += 1
            if action in _PHASE_ACTIONS:
                last_phase = action
            elif action == "-damage" and len(parts) >= 4 and last_phase == "move":
                # A move hit's damage line carries no `[from]`: confusion
                # (`|-damage|...|[from] confusion`), hazards, weather, residuals,
                # Rocky Helmet / Life Orb and recoil all tag their source.  A hit
                # the mon does NOT survive (`0 fnt`) is excluded -- Illusion's
                # `onFaint` clears the disguise with no |replace|.
                if "[from]" not in line and not parts[3].strip().startswith("0 "):
                    docc = open_occupancy.get(parts[2].split(":")[0].strip())
                    if docc is not None:
                        docc["survived_damaging_hit"] = True

            if action == "turn":
                try:
                    turn = int(parts[2])
                except ValueError:
                    pass
                else:
                    for slot, attempts in slot_sleep_attempts.items():
                        sleep_attempts_by_turn[(slot, turn)] = attempts
                cudchew_activated.clear()
                continue

            if len(parts) >= 4 and parts[3].strip() == "slp":
                slot = parts[2].split(":")[0].strip()
                if action == "-status":
                    slot_sleep_attempts[slot] = 0
                elif action == "-curestatus":
                    slot_sleep_attempts.pop(slot, None)
                elif action == "cant":
                    slot_sleep_attempts[slot] = slot_sleep_attempts.get(slot, 0) + 1

            # Cud Chew berry-eat tracking (see `berry_eats`).  Kept ahead of the
            # dispatch below so it sees every line: the activation flag lives
            # only until the next protocol "instant" boundary, mirroring
            # protocol._is_cudchew_reeat's back-scan stop condition.
            if action in _PHASE_ACTIONS:
                cudchew_activated.clear()
            if (
                action == "-activate"
                and len(parts) >= 4
                and normalize_name(parts[3].strip().split(":")[-1]) == "cudchew"
            ):
                cudchew_activated.add(parts[2].split(":")[0].strip())
            elif action == "-enditem" and len(parts) >= 4 and "[eat]" in line:
                slot = parts[2].split(":")[0].strip()
                sub = _subject(parts)
                if sub is not None and slot not in cudchew_activated:
                    berry_eats.setdefault(sub, set()).add(turn)

            # "[from] ability: X" annotations are NOT confined to the "-"-prefixed
            # effect lines the dispatch below reaches: PS defaults a called move's
            # sourceEffect to the ability that called it
            # (sim/battle-actions.ts:387 `if (!sourceEffect && this.battle.effect.id)
            # sourceEffect = this.battle.effect`) and appends
            # "|[from] <fullname>" to the |move| line it prints (:452).  A Magic
            # Bounce reflection therefore reveals the bouncer's ability ONLY on its
            # bounced |move| line --
            #   |move|p2a: Hatterene|Stealth Rock|p1a: Donphan|[from] ability: Magic Bounce
            # -- with no |-ability| line anywhere (data/abilities.ts:2427 magicbounce
            # onTryHit just re-uses the move).  Harvested up here, before the |move| /
            # |-terastallize| / non-"-" arms below route those lines away.
            if "[from] ability:" in line and action not in _SLOT_REASSIGNING:
                _from_ability(line, parts, action)

            if action in _SLOT_REASSIGNING:
                if len(parts) >= 4:
                    slot = parts[2].split(":")[0].strip()
                    head = parts[3].split(",")[0].strip()
                    new_species = _species_key(head)
                    pid = slot[:2]
                    # |replace| reveals that the mon standing in this slot was a
                    # disguised Zoroark (Illusion).  The span it covers is exactly
                    # the CURRENT occupancy: from the switch/drag that put this
                    # occupant on the field up to the reveal turn.  It must NOT be
                    # widened to the disguise species' first-ever appearance -- the
                    # REAL mon of that species is on the same team (Illusion copies
                    # the last party member, so both exist) and its own earlier
                    # stays would then be given Zoroark's Dark typing, corrupting
                    # every type-dependent check and damage roll on those turns
                    # (synth00633: the real Mienshao's T19 Knock Off gains a bogus
                    # Dark STAB, inflating it into a KO that erases the Justified /
                    # Calm Mind boosts).  A Zoroark that switches out and back in
                    # re-applies Illusion and is caught by its own later |replace|.
                    # _apply_illusion further gates on the disguise being the active.
                    if action == "replace":
                        disguise = slot_species.get(slot)
                        if disguise and disguise != new_species:
                            illusions.append(
                                {
                                    "pid": pid,
                                    "disguise": disguise,
                                    "true_species": new_species,
                                    "start_turn": slot_entry_turn.get(slot, turn),
                                    "end_turn": turn,
                                }
                            )
                        occ = open_occupancy.get(slot)
                        if occ is not None:
                            occ["revealed_true_species"] = new_species
                    elif action in ("switch", "drag"):
                        # |detailschange| (forme change) is deliberately excluded:
                        # it re-labels the SAME occupant and must not restart the
                        # occupancy clock.
                        slot_entry_turn[slot] = turn
                        _open_occupancy(slot, new_species, turn, parts[3])
                    slot_species[slot] = new_species
                    # roster: species -> its first switch-in details ("Dondozo, L79")
                    roster.setdefault(pid, {}).setdefault(
                        new_species, parts[3].strip()
                    )
                continue

            # a move a mon SELECTS is in its fixed randbats moveset from turn 1,
            # so harvest it to back-fill earlier pre-turn states (a first-use move
            # otherwise can't be parsed). Exclude called moves (Sleep Talk /
            # Copycat / Dancer, tagged [from]...), but keep a continuing lockedmove.
            if action == "move" and len(parts) >= 4:
                if "[from]" not in line or "[from]lockedmove" in line:
                    sub = _subject(parts)
                    if sub is not None:
                        moves.setdefault(sub, {}).setdefault(
                            normalize_name(parts[3]), turn
                        )
                    occ = open_occupancy.get(parts[2].split(":")[0].strip())
                    if occ is not None:
                        occ["moves"].add(normalize_name(parts[3]))
                continue

            # tera type is revealed only when a mon terastallizes, but it is a
            # fixed set property; back-fill it so a "<move>-tera" action on an
            # earlier pre-turn state teras to the correct type (else the engine
            # teras to typeless and misses a tera type-immunity).
            if action == "-terastallize" and len(parts) >= 4:
                sub = _subject(parts)
                if sub is not None:
                    tera.setdefault(sub, normalize_name(parts[3]))
                occ = open_occupancy.get(parts[2].split(":")[0].strip())
                if occ is not None:
                    occ["tera_during"] = normalize_name(parts[3])
                    occ["tera_during_turn"] = turn
                continue

            if action == "faint":
                sub = _subject(parts)
                if sub is not None:
                    faint_turns.setdefault(sub, turn)
                continue

            if not action.startswith("-"):
                continue

            if action == "-ability" and len(parts) >= 4 and "[from]" not in line:
                sub = _subject(parts)
                if sub is not None:
                    abilities.setdefault(sub, normalize_name(parts[3]))

            elif (
                action == "-activate"
                and len(parts) >= 4
                and parts[3].startswith("ability: ")
            ):
                sub = _subject(parts)
                if sub is not None:
                    abilities.setdefault(
                        sub, normalize_name(parts[3][len("ability: ") :])
                    )

            # ("[from] ability:" on any other "-" line was already attributed by
            # the hoisted _from_ability call at the top of the loop.)

            if action == "-activate" and len(parts) >= 4:
                activate_head = parts[3].strip()
                if activate_head in ("move: Trick", "move: Switcheroo"):
                    # `this.add('-activate', source, 'move: Trick', '[of] target')`
                    # (data/moves.ts:19887; Switcheroo emits the SAME string at
                    # :18665).  It is the only line that names BOTH ends of the
                    # swap -- the two `-item` lines that follow carry no `[of]`.
                    swap_of = _OF_SLOT.search(line)
                    swap_a = parts[2].split(":")[0].strip()
                    swap_partner.clear()
                    if swap_of is not None:
                        swap_b = swap_of.group(1)
                        swap_partner[swap_a] = swap_b
                        swap_partner[swap_b] = swap_a
                elif normalize_name(activate_head.split(":")[-1]) == "symbiosis":
                    # Symbiosis hands an item over with NO `-item` line
                    # (data/abilities.ts:4842), so it would be invisible to the
                    # `item_line_turns` acquisition proof.  Record it there.
                    swap_of = _OF_SLOT.search(line)
                    if swap_of is not None:
                        item_line_turns.setdefault(
                            swap_of.group(1)[:2], []
                        ).append(turn)
                        item_line_events.setdefault(
                            swap_of.group(1)[:2], []
                        ).append((turn, line_idx))

            # `-item` announces the mon that NOW HOLDS the item.  Three shapes, and
            # they must be told apart:
            #   * plain reveal (no `[from]`, or `[from] ability: Frisk`) -- the mon
            #     held it from battle start;
            #   * a STEAL / Bestow (`_ITEM_TRANSFER_LINE`) -- the subject ACQUIRED it
            #     this turn and the `[of]` mon lost it;
            #   * a Trick / Switcheroo half (`_ITEM_SWAP_LINE`) -- the subject
            #     ACQUIRED it this turn, and nothing is said about the donor (PS gives
            #     the other side its own line, data/moves.ts:19888-19899).
            # This arm used to be gated on `"[from] move:" not in line`, which dropped
            # the whole Trick/Switcheroo/Bestow family AND made the `move: Thief|Covet`
            # alternation inside `_ITEM_TRANSFER_LINE` unreachable, so the Thief/Covet
            # victim never got a removal turn either.
            if action == "-item" and len(parts) >= 4:
                sub = _subject(parts)
                gained = normalize_name(parts[3])
                is_steal = _ITEM_TRANSFER_LINE.search(line) is not None
                is_swap = _ITEM_SWAP_LINE.search(line) is not None
                subject_slot = parts[2].split(":")[0].strip()
                item_line_turns.setdefault(subject_slot[:2], []).append(turn)
                item_line_events.setdefault(subject_slot[:2], []).append(
                    (turn, line_idx)
                )
                if is_swap:
                    # The line's subject RECEIVED `gained`; under Trick the item
                    # it received is the item the OTHER end was holding
                    # (`source.takeItem()` -> `target.setItem(myItem)`,
                    # data/moves.ts:19872-19890).  So the PARTNER slot is the one
                    # this line proves a holding for.
                    partner = swap_partner.get(subject_slot)
                    pocc = open_occupancy.get(partner) if partner else None
                    if pocc is not None:
                        pocc["held_items"].append((turn, gained, line_idx))
                if sub is not None and not is_steal and not is_swap:
                    # `(item, None)` means "held from battle start", so it is only
                    # honest for a plain reveal.
                    items.setdefault(sub, (gained, None))
                elif sub is not None:
                    # An ACQUISITION.  Recorded on its own timeline (see `item_gains`)
                    # rather than in `items`, because the receiver demonstrably did NOT
                    # start the battle with it.
                    item_gains.setdefault(sub, []).append([turn, gained, None])
                # ...and the `[of]` victim STOPS holding it here (same shape as the
                # -enditem arm below). Frisk is excluded by _ITEM_TRANSFER_LINE: it
                # reveals the foe's item without moving it (synth33779 line 344).
                if is_steal:
                    of = _OF_SLOT.search(line)
                    if of is not None:
                        slot = of.group(1)
                        # ...which also PROVES the `[of]` slot was holding it: PS
                        # names the donor there for both directions of transfer
                        # (Magician/Thief take from `[of]`, data/abilities.ts:2481;
                        # Bestow gives from `[of]`, data/moves.ts:1257).
                        vocc = open_occupancy.get(slot)
                        if vocc is not None:
                            vocc["held_items"].append((turn, gained, line_idx))
                        victim = (slot[:2], _occupant(slot, of.group(2).strip()))
                        if not _end_gain(victim, gained, turn):
                            prior = items.get(victim)
                            if prior is None:
                                items[victim] = (gained, turn)
                            elif prior[0] == gained and prior[1] is None:
                                items[victim] = (gained, turn)

            # Flame Orb / Toxic Orb self-inflict burn/poison at end of turn via a
            # "-status <slot>|<status>|[from] item: <Item>" line (conditions.ts brn/
            # tox onStart). That line is the ONLY protocol evidence the orb exists:
            # an orb is never |-item|'d or |-enditem|'d (it is not consumed, Fling'd
            # or Knocked Off in the reproduced turn), so without harvesting it here
            # the checker never learns the orb and the engine's EOT self-burn/-poison
            # can't be reproduced (Heracross Flame Orb, battle-2651908107 T1). The
            # orb holder is the -status subject (self-inflicted, no [of] tag); a
            # defensive [of] arm attributes to the named holder if one ever appears.
            elif (
                action == "-status"
                and len(parts) >= 5
                and "[from] item:" in line
            ):
                m = _FROM_ITEM.search(line)
                item = normalize_name(m.group(1)) if m else ""
                if item:
                    of = _OF_SLOT.search(line)
                    if of is not None:
                        slot = of.group(1)
                        owner = (slot[:2], _occupant(slot, of.group(2).strip()))
                    else:
                        owner = _subject(parts)
                    if owner is not None:
                        # removal_turn None == present from battle start, so every
                        # pre-turn snapshot back-fills the orb
                        items.setdefault(owner, (item, None))

            # Residual `-heal`/`-damage` lines tagged `[from] item: X` are for
            # several items the ONLY protocol evidence the item exists at all:
            # Leftovers / Black Sludge emit nothing but the end-of-turn
            # `-heal ... [from] item: Leftovers` (data/items.ts leftovers
            # onResidual `this.heal(pokemon.baseMaxhp / 16)`; the heal itself is
            # gated at sim/battle.ts:2272 `if (target.hp >= target.maxhp) return
            # false;`), and Life Orb recoil / Black Sludge / Sticky Barb chip is
            # the `-damage` twin.  Without harvesting them, the FIRST such line
            # of a game is unreproducible in its own turn's pre-state -- the
            # live tracker learns the item only after the line arrives, so the
            # snapshot still holds UNKNOWNITEM and the engine has no residual to
            # emit.  That was the dominant real-ladder soft class: 33 of 46
            # rows, every one a first-reveal `heal [from] item: Leftovers` turn.
            # Ownership matches the orb arm: an `[of]` slot names the holder
            # when the item belongs to the OTHER mon (Rocky Helmet's `-damage
            # ... [of] <holder>`), otherwise the subject holds it.  A berry's
            # `-heal ... [from] item: Sitrus Berry` follows its `-enditem ...
            # [eat]` within the same instant, so `items` already carries the
            # (item, removal_turn) record and setdefault keeps it.  An item the
            # mon ACQUIRED mid-battle (Trick et al.) must not be written back as
            # a start-of-battle holding: its `-item` line precedes any residual
            # it produces, so an open acquisition of the same item at this turn
            # suppresses the start-item claim exactly like `_end_gain` does for
            # removals.
            elif (
                action in ("-heal", "-damage")
                and len(parts) >= 5
                and "[from] item:" in line
            ):
                m = _FROM_ITEM.search(line)
                item = normalize_name(m.group(1)) if m else ""
                if item:
                    of = _OF_SLOT.search(line)
                    if of is not None:
                        slot = of.group(1)
                        owner = (slot[:2], _occupant(slot, of.group(2).strip()))
                    else:
                        owner = _subject(parts)
                    acquired = owner is not None and any(
                        rec[1] == item
                        and rec[0] <= turn
                        and (rec[2] is None or turn <= rec[2])
                        for rec in item_gains.get(owner, ())
                    )
                    if owner is not None and not acquired:
                        items.setdefault(owner, (item, None))

            elif action == "-transform" and len(parts) >= 3:
                # a Transform/Imposter copy uses the TARGET's moves, so its move
                # list is no longer evidence about its own set
                occ = open_occupancy.get(parts[2].split(":")[0].strip())
                if occ is not None:
                    occ["transformed"] = True

            elif action == "-enditem" and len(parts) >= 4:
                sub = _subject(parts)
                if sub is not None:
                    item = normalize_name(parts[3])
                    # `-enditem` always names the mon that WAS holding the item --
                    # PS emits it from `Pokemon#takeItem`/`eatItem`/`useItem` on
                    # the holder itself, for every route (berry eat, Knock Off's
                    # `[of]` is the ATTACKER, Fling, Incinerate, and Trick's
                    # `[silent]` half at data/moves.ts:19892/19898).  Slot-keyed,
                    # so it is evidence about the PHYSICAL occupant.
                    eocc = open_occupancy.get(parts[2].split(":")[0].strip())
                    if eocc is not None:
                        eocc["held_items"].append((turn, item, line_idx))
                    # An `-enditem` of an item this mon ACQUIRED mid-battle closes
                    # that acquisition and is not evidence about what it started
                    # with.  Without this the losing line back-dates the acquired
                    # item to turn 0 -- a Cobalion that started on Leftovers, was
                    # Tricked Choice Specs on T30 and had them Knocked Off on T40
                    # would read as "held Choice Specs from battle start until T40".
                    if not _end_gain(sub, item, turn):
                        prior = items.get(sub)
                        if prior is None:
                            items[sub] = (item, turn)
                        elif prior[0] == item and prior[1] is None:
                            items[sub] = (item, turn)

    for occ in open_occupancy.values():
        occ["end_turn"] = turn

    return {
        "abilities": abilities,
        "items": items,
        "item_gains": item_gains,
        "item_line_turns": {p: sorted(t) for p, t in item_line_turns.items()},
        "item_line_events": {p: sorted(e) for p, e in item_line_events.items()},
        "moves": moves,
        "occupancies": occupancies,
        "roster": roster,
        "tera": tera,
        "illusions": illusions,
        "berry_eats": berry_eats,
        "faint_turns": faint_turns,
        "sleep_attempts_by_turn": sleep_attempts_by_turn,
    }


def _tag_player_pid(token: str) -> str | None:
    m = re.match(r"^\s*(p[12])[ab]?:", token)
    return m.group(1) if m else None


def _backfill_roster(battler, pid, reveals) -> None:
    """Add any opponent species revealed anywhere in the log but not yet in this
    snapshot's party, so a switch INTO a not-yet-seen mon parses.  An unrevealed
    mon has not been in play, so full HP (from its first switch details) is the
    correct pre-turn state.

    ORDERING (two constraints, both load-bearing):
    * it must run BEFORE `_backfill_revealed_knowledge`'s fill loop, so these
      mons also receive their ability/item/moves there (a switched-in mon's own
      ability -- Intimidate, Drought -- fires on entry and must be modeled);
      `_backfill_revealed_knowledge` therefore still calls it itself (the
      function is idempotent -- the `present` set makes a second call a no-op).
    * it must ALSO run before `apply_exact_teams` / `_populate_opponent_knowledge`
      on the full-knowledge corpus.  Those two only touch mons already on the
      roster, so a mon making its FIRST appearance during the modeled turn used
      to miss both passes entirely and was handed to the engine with NONE
      ability, UNKNOWNITEM and randbats default-85-EV stats (synth01954 T14:
      Amoonguss debuts as ability=None/item=unknownitem while the sidecar says
      Regenerator / Rocky Helmet).  `_fire_turn` calls it up front for that
      reason."""
    present = {_species_key(p.name) for p in battler.reserve}
    if battler.active is not None:
        present.add(_species_key(battler.active.name))
    party_size = len(battler.reserve) + (1 if battler.active is not None else 0)
    for species, details in reveals.get("roster", {}).get(pid, {}).items():
        if species in present or party_size >= 6:
            continue
        try:
            newp = Pokemon.from_switch_string(details)
        except Exception:
            continue
        battler.reserve.append(newp)
        present.add(species)
        party_size += 1


def _backfill_move_order(first_use: dict | None, snapshot_turn: int) -> list:
    """Order the revealed-move backfill candidates for a snapshot, deterministically.

    The engine has exactly 4 move slots and panics past them, so the backfill is
    capped -- which means the ORDER decides which revealed moves the
    reconstructed state gets.  Two properties are required of that order:

    1. DETERMINISM.  It must not depend on Python hash randomisation.  The old
       code iterated a `set` of move ids, so the surviving 4 (and hence whether
       the turn's real move parsed at all, and hence the checker's own
       turns-checked/skipped totals) moved between runs of an unchanged tree.
       A gate cannot certify a number it cannot reproduce.

    2. RELEVANCE.  The backfill exists so a FIRST-USE move parses; a move first
       used at or after `snapshot_turn` is the one this snapshot may need, and
       the nearest such turn is the likeliest.  Moves first used strictly BEFORE
       the snapshot were already revealed to the live reconstruction, so they
       are usually present anyway and are ranked last (most-recent first).

    Both keys are total on the input, so the result is a strict order with no
    hash-order tie-break left in it."""
    if not first_use:
        return []
    if os.environ.get("FP_CONTROL_UNSORTED_BACKFILL"):
        # CONTROL ONLY, never set in a measurement run: restores the pre-fix
        # hash-order traversal so the determinism test can prove itself capable
        # of failing (a probe whose setup silently no-ops is a false PASS).
        # tests/test_replay_determinism.py asserts this path DOES diverge across
        # seeds; if that assertion ever fails, the reproducer went stale and a
        # new one is owed -- it does not mean the ordering became unnecessary.
        return list(set(first_use))
    return sorted(
        first_use,
        key=lambda mv: (
            (0, first_use[mv], mv)
            if first_use[mv] >= snapshot_turn
            else (1, -first_use[mv], mv)
        ),
    )


def _control(name):
    """Negative-control switch, read per call so a probe can flip it in-process.
    Each flag gates exactly ONE mechanism."""
    return os.environ.get(name, "") not in ("", "0", "false", "False")


def _bounded_item_gains(gains):
    """`item_gains` with each record's window closed at the NEXT acquisition.

    A record carries `end_turn = None` when `_harvest_reveals` never saw a
    matching loss line -- which is the NORMAL case for an item Tricked away
    again, because PS reports the giver's loss as the RECEIVER's `|-item|` and
    never as an `|-enditem|` on the giver.  Read literally, such a record claims
    the item is held for the rest of the battle.  It is not: acquiring the NEXT
    item proves the previous one is gone, since every acquisition route either
    requires the receiver to be holding nothing (Thief / Covet / Magician /
    Pickpocket check `source.item`, Bestow checks `target.item`) or hands the
    receiver's own item away in the same breath (Trick / Switcheroo)."""
    records = sorted(gains or (), key=lambda g: g[0])
    out = []
    for i, (gain_turn, gain_item, end_turn) in enumerate(records):
        next_gain = records[i + 1][0] if i + 1 < len(records) else None
        if next_gain is not None and (end_turn is None or end_turn > next_gain):
            end_turn = next_gain
        out.append((gain_turn, gain_item, end_turn))
    return out


def _species_keyed_event_is_reliable(reveals, pid, species, turn) -> bool:
    """False when an event the protocol printed against `(pid, species)` during
    `turn` may physically belong to a DIFFERENT pokemon.

    Illusion is the one gen9 mechanism that makes PS attribute an event to the
    wrong species: the |move| / |-item| lines are printed against `pokemon`,
    whose id renders through the illusion's name (sim/pokemon.ts:531).  So a
    `(pid, species)` reveal is only an identity while no Illusion span had the
    bearer wearing that species' face, and while the side's occupancy at that
    turn is not one `_infer_illusion_spans` refused to decide.

    Deliberately NARROW: it asks about ONE species at ONE turn, not "does this
    side have a Zoroark".  The side-wide question would have withdrawn the
    synth15565 item fix, whose Hoopa timeline is nowhere near that game's
    Illusion spans -- and coverage given up for a question nobody asked is
    coverage given up for nothing."""
    if illusion_unresolved_turn(reveals, pid, turn):
        return False
    for il in reveals.get("illusions", ()):
        if il.get("pid") != pid or il.get("disguise") != species:
            continue
        # inclusive at BOTH ends: unlike a pre-turn state, an event resolves
        # DURING its turn, so the bearer is on the field for its entry turn too
        if il["start_turn"] <= turn <= il["end_turn"]:
            return False
    return True


def _backfill_revealed_knowledge(battler, pid, reveals, snapshot_turn) -> None:
    """Fill still-unknown opponent ability/item fields from full-log reveals.
    Abilities apply to every snapshot; an item is only filled into snapshots
    taken before it left play (pre-turn state of the removal turn still holds
    it -- consumption happens mid-turn; the live reconstruction already tracks
    the removal for later turns)."""
    # roster FIRST -- see _backfill_roster's docstring for why.
    _backfill_roster(battler, pid, reveals)

    mons = list(battler.reserve)
    if battler.active is not None:
        mons.append(battler.active)
    for pkmn in mons:
        key = (pid, _species_key(pkmn.name))
        ability = reveals["abilities"].get(key)
        if ability and not pkmn.ability:
            pkmn.ability = ability
        gains = reveals.get("item_gains", {}).get(key) or ()
        # An item ACQUIRED mid-battle (Trick / Switcheroo / Bestow / Thief /
        # Covet / Magician / Pickpocket) is held for every snapshot STRICTLY
        # AFTER the turn it was acquired on -- the acquisition resolves mid-turn,
        # so that turn's own pre-state still shows the old item -- and up to and
        # including the turn it left again (same mid-turn convention).
        #
        # THIS RUNS UNCONDITIONALLY, not only when the item is still unknown.
        # An acquisition record is an OBSERVATION off the log's own `|-item|`
        # line; a set list (the full-knowledge sidecar `apply_exact_teams`
        # applies a few lines earlier in `_fire_turn`, or the randbats
        # inference) is ground truth only at TURN 0.  Gating the timeline on
        # `item == UNKNOWN_ITEM` let the weaker, older source suppress the
        # stronger, newer one: synth15565's Hoopa-Unbound Tricked its Choice
        # Band to Greedent on T7 and received a Sitrus Berry, and every snapshot
        # from T8 on was still reconstructed holding CHOICEBAND -- so the
        # `|-enditem|p2a: Hoopa|Sitrus Berry|[eat]` on T26 was unreproducible in
        # every branch.  The two sources now COMPOSE: the sidecar fills turn 0
        # and anything the protocol never spoke about, the timeline owns every
        # window it actually witnessed.
        #
        # TWO NARROWINGS, both learned from the affected-population measurement
        # (HANDOFF section 4 rule 19 -- the 16-game acceptance set said this
        # composition opened nothing; the 6,102-game population where it can
        # actually fire said it opened 31 rows, 5 of them HARD):
        #
        #  (1) A record whose `end_turn` is None does NOT mean "held for the rest
        #      of the battle".  `_harvest_reveals._end_gain` can only close a
        #      record on a LOSS line for that mon, and Trick reports the GIVER's
        #      loss as the RECEIVER's `|-item|` -- so a Tricked-away item leaves
        #      its acquisition record open forever.  synth08772: Roaring Moon
        #      gained a Choice Band on T1 and traded it back on T2, and the open
        #      T1 record put a Choice Band on it for the rest of the game (T6
        #      Iron Head, observed 41, engine max roll 62 = 41 x 1.5 -- a HARD
        #      damage-membership miss).  The NEXT acquisition bounds the previous
        #      one exactly as it already bounds the battle-start item a few lines
        #      below, and for the identical reason: every acquisition route
        #      requires the receiver to be holding nothing or hands its own item
        #      away in the same breath.
        #  (2) `item_gains` is keyed by (pid, SPECIES), and Illusion is precisely
        #      the mechanism that makes the protocol attribute an event to the
        #      wrong species (PS prints |move|/|-item| against the illusion's
        #      name, sim/pokemon.ts:531).  synth47388: a Zoroark wearing
        #      Pincurchin's face Tricked on T1, and the record put the Loaded
        #      Dice it received onto the REAL Pincurchin that switched in on T2 --
        #      six turns of Leftovers residual unreproducible.  On a side that
        #      has an Illusion bearer at all, the species key is not an identity,
        #      so the timeline may fill an UNKNOWN but must never OVERRIDE:
        #      REFUSE-DON'T-GUESS rather than assert the wrong mon's item.
        gains = _bounded_item_gains(gains)
        # NEGATIVE CONTROL: the pre-wave gate, under which any earlier source
        # (the sidecar's turn-0 item, the randbats inference) SUPPRESSED the
        # protocol timeline outright.  Gates this one mechanism and nothing else
        # (HANDOFF section 4 rule 18).
        may_override = not _control("FP_CONTROL_ITEM_TIMELINE_UNKNOWN_ONLY")
        timeline_item = None
        for gain_turn, gain_item, end_turn in gains:
            if snapshot_turn <= gain_turn or (
                end_turn is not None and snapshot_turn > end_turn
            ):
                continue
            if pkmn.item != constants.UNKNOWN_ITEM and not (
                may_override
                and _species_keyed_event_is_reliable(
                    reveals, pid, _species_key(pkmn.name), gain_turn
                )
            ):
                continue
            timeline_item = gain_item
        if timeline_item is not None:
            pkmn.item = timeline_item
            pkmn.item_inferred = False
        # NOT DONE HERE, deliberately: the mirror case where the timeline proves
        # the start item is GONE (removed_turn / first_gain passed) with nothing
        # acquired in its place.  Live tracking already owns that -- it sets
        # `removed_item`/`knocked_off`, which `apply_exact_teams` refuses to
        # overwrite -- and asserting a NEGATIVE here would move far more turns
        # than the one row this composition fixes.  REVISIT TRIGGER: a finding
        # whose reconstruction holds an item the protocol removed and never
        # replaced.
        if pkmn.item == constants.UNKNOWN_ITEM:
            rec = reveals["items"].get(key)
            if rec is not None and pkmn.item == constants.UNKNOWN_ITEM:
                item, removed_turn = rec
                # The start-of-battle item is also gone once the mon ACQUIRES one:
                # every acquisition route requires the receiver to be holding
                # nothing (Thief/Covet/Magician/Pickpocket check `source.item`,
                # Bestow checks `target.item`) or hands its own item away in the
                # same breath (Trick/Switcheroo), so a gain turn bounds the start
                # item exactly like a removal turn does.
                first_gain = min((g[0] for g in gains), default=None)
                if (removed_turn is None or snapshot_turn <= removed_turn) and (
                    first_gain is None or snapshot_turn <= first_gain
                ):
                    pkmn.item = item
        # tera type: only relevant when this mon teras (via a -tera action); set
        # it if unknown so the tera resolves to the right type. Do NOT set
        # terastallized -- the pre-turn state has not tera'd yet.
        tt = reveals.get("tera", {}).get(key)
        if tt and not pkmn.terastallized and not pkmn.tera_type:
            pkmn.tera_type = tt
        # moves: a move used later is in the fixed moveset now; back-fill it so a
        # first-use move parses. Cap at 4 (the engine has 4 move slots; exceeding
        # it panics).
        for mv in _backfill_move_order(
            reveals.get("moves", {}).get(key), snapshot_turn
        ):
            if len(pkmn.moves) >= 4:
                break
            if not any(m.name == mv for m in pkmn.moves):
                pkmn.add_move(mv)


def _backfill_user_tera(battler, pid, reveals) -> None:
    """Apply the full-log tera-type reveal to the PERSPECTIVE player's own mons.
    In replay reconstruction the user's request json carries no `teraType`, so a
    mon's tera_type stays unset and a `<move>-tera` action would tera to typeless,
    missing a tera type-immunity (Leavanny->Ghost is immune to a Normal Tera
    Starstorm; a Blaziken->Dark is immune to a Prankster Encore).  The tera type is
    a fixed set property revealed when the mon terastallizes anywhere in the log.
    Guarded exactly like the opponent backfill: set only when still unknown and the
    mon has not yet terastallized in this pre-turn state."""
    tera = reveals.get("tera", {})
    if not tera:
        return
    mons = list(battler.reserve)
    if battler.active is not None:
        mons.append(battler.active)
    for pkmn in mons:
        tt = tera.get((pid, _species_key(pkmn.name)))
        if tt and not pkmn.terastallized and not pkmn.tera_type:
            pkmn.tera_type = tt


_ILLUSION_ABILITY = "illusion"
# Struggle is not in any moveset, so it can never witness a disguise
_NEVER_OWN_MOVE = frozenset(("struggle",))


def _sidecar_moves(mon: dict | None) -> set:
    return {normalize_name(m) for m in (mon or {}).get("moves", ()) if m}


def _sidecar_item(mon: dict | None) -> str:
    return normalize_name((mon or {}).get("item", "") or "")


def _no_prior_acquisition(reveals, pid: str, turn: int, obs_idx: int | None = None) -> bool:
    """THE PRECONDITION of the item-evidence discriminator: proof that whatever
    this side's slot was holding at `turn` is still its SIDECAR item.

    An observed holding only identifies a species while the mon has not picked
    anything up.  Every gen9 route that puts a NEW item on a mon runs through
    `Pokemon#setItem` with an `-item` announcement -- Trick/Switcheroo
    (data/moves.ts:19890 / :19896, :18668 / :18674), Thief/Covet/Bestow
    (data/moves.ts:1257 for Bestow), Magician (data/abilities.ts:2481),
    Pickpocket, Recycle (data/moves.ts:14829), Harvest (data/abilities.ts:1798)
    and Pickup (data/abilities.ts:3264) -- and the one exception, Symbiosis
    (data/abilities.ts:4842, which announces only `-activate`), is folded into
    the same counter at harvest time.  So "this side has printed no such line
    yet" is a PROOF that nobody on the side has acquired anything, and neither
    candidate mon can be wearing an item that is not its own.

    Two counts, not one.  Turns strictly BEFORE must be empty, and the
    observation turn itself may carry AT MOST ONE such line -- the receiving
    half of the very transfer being read.  A second line on the same turn would
    mean some other acquisition also resolved there, so it REFUSES.

    ...and the counts ALONE are not enough, which is the repair here.  The
    same-turn allowance ASSUMES the one permitted `-item` line is the receiving
    half of the transfer being read, and a turn number cannot verify that
    pairing.  Every line of this composition is PS-legal:

        T1  Knock Off empties the GENUINE mon's hand -- an `-enditem` only, so
            the `-item` ledger records nothing at all (defect (i): the ledger is
            blind to `-enditem` LOSSES);
        T5  empty-handed, it Tricks: `takeItem()` on an empty hand returns
            `undefined`, NOT false (sim/pokemon.ts:1856-1859), so trick's
            `myItem === false` failure guard does not fire (data/moves.ts
            :19873-19877) and the swap proceeds ONE-SIDED --
            `|-enditem|<target>|...|[silent]` then `|-item|<source>|<item>`
            (data/moves.ts:19887-19899).  ONE `-item` line for the side;
        T5  the foe Knock Offs the just-received item: a second `-enditem`, on
            the SAME turn, AFTER the acquisition.

    `before == 0 and at <= 1` passes, the T5 `-enditem` is read as a fixed set
    property, and the arm PROVES a Zoroark disguise on a mon the log shows is
    genuine.  (It is not even ambiguous: a disguised Zoroark would still hold
    its own Choice Specs, so its Trick would have been TWO-sided.)

    The fix is LOG ORDER, not a bigger budget.  `this.add(...)` appends to the
    battle log in strict resolution order, so requiring the observation's line
    ordinal to PRECEDE every acquisition ordinal on the side proves that at the
    instant of the observation NO acquisition on that side had resolved -- and
    since every route that puts a foreign item on a mon announces itself in this
    ledger, the observed holding is necessarily the occupant's own set item.
    This subsumes the turn counts (it is strictly stronger on the observation
    turn) and needs no model of WHICH transfer paired with which.  In
    synth15565 the observation is the partner half of a two-sided Trick,
    `|-item|<target>|Choice Band|[from] move: Trick`, emitted at :19890 BEFORE
    the source's own `-item` at :19896 -- so it still passes.

    `obs_idx=None` (or a `reveals` with no `item_line_events`, i.e. a
    hand-built fixture) falls back to the counts alone; the real harvest path
    always carries ordinals.

    This is the whole fix.  Comparing a handed-over item against two candidate
    sidecars WITHOUT this proof is exactly the defect APPROXIMATIONS.md 16.3(b)
    had to repair from the other side (a Zoroark wearing Pincurchin's face put a
    Tricked item onto the real Pincurchin): an item that changed hands earlier is
    no longer a fixed set property, and reading it as one silently misattributes
    the occupancy."""
    turns = ((reveals or {}).get("item_line_turns") or {}).get(pid, ())
    before = 0
    at = 0
    for t in turns:
        if t < turn:
            before += 1
        elif t == turn:
            at += 1
    if before != 0 or at > 1:
        return False
    if obs_idx is None:
        return True
    events = (reveals or {}).get("item_line_events")
    if events is None:
        return True
    return all(idx > obs_idx for _t, idx in (events.get(pid) or ()))


def _item_evidence_occupant(reveals, occ: dict, shown_item: str, bearer_item: str):
    """"shown" / "bearer" when a PROVEN holding settles who stood in this slot,
    else None.

    The sidecar item is a fixed set property exactly like the sidecar moveset the
    arms above use: Illusion changes what the slot is CALLED, never what it
    carries.  So a holding proven against the SLOT (see `held_items`) that
    matches only one of the two candidates' set items names the occupant.

    Refusals, all deliberate:
      * the two candidates share the set item -> no discrimination exists;
      * `_no_prior_acquisition` cannot be proven at that turn -> the holding is
        no longer a set property;
      * the holding matches NEITHER candidate -> the premises are contradicted
        (some acquisition route this function does not model must have fired),
        so every observation on this occupancy is discarded, not just this one;
      * two qualifying observations disagree -> same."""
    if not shown_item or not bearer_item or shown_item == bearer_item:
        return None
    verdict = None
    for obs in occ.get("held_items", ()):
        # (turn, item) from the harvest before ordinals existed / from a
        # hand-built fixture; (turn, item, line_idx) from the real harvest.
        turn, item = obs[0], obs[1]
        obs_idx = obs[2] if len(obs) > 2 else None
        if not _no_prior_acquisition(reveals, occ["pid"], turn, obs_idx):
            continue
        if item == shown_item:
            seen = "shown"
        elif item == bearer_item:
            seen = "bearer"
        else:
            return None
        if verdict is not None and verdict != seen:
            return None
        verdict = seen
    return verdict


def _bearer_tera_from(occ: dict) -> int:
    """The turn from which the bearer's tera state holds, in the same half-open
    convention as an illusion span's `start_turn` (the state is tera'd for every
    turn STRICTLY GREATER than this).

    A `tera:` suffix on the entry line means the entrant was already
    terastallized when it walked in, so it holds for the whole occupancy and the
    entry turn is the bound.  A |-terastallize| DURING the occupancy lands
    mid-resolution, so the pre-state of that turn is still un-tera'd and the
    bound is the tera turn itself.  Getting this wrong back-dates the tera over
    the whole span: synth45461's Zoroark-Hisui teras Normal on T5 but the span
    starts at T0, so T1-T4 were reconstructed as pure-Normal and the T3
    `|-immune|` to Double-Edge (Zoroark-Hisui is Normal/GHOST until it teras)
    was contradicted by every branch."""
    if occ.get("tera_during") is not None:
        during = occ.get("tera_during_turn")
        return occ["start_turn"] if during is None else during
    return occ["start_turn"]


def _infer_illusion_spans(reveals: dict, exact_teams) -> None:
    """Resolve the Illusion spans the PROTOCOL alone cannot, using the sidecar.

    `illusions` (built above) only holds spans a |replace| announced, and PS only
    emits |replace| when the disguise BREAKS -- `illusion.onDamagingHit` ->
    `singleEvent('End', ...)` -> `this.add('replace', ...)`
    (data/abilities.ts:2061-2071).  A disguised Zoroark that pivots out (or wins)
    without ever taking a damaging hit is therefore never announced at all, and
    every turn it was on the field is reconstructed as the DISGUISE: wrong types,
    wrong stats, wrong level, wrong moveset.  That is the single largest residue
    class in the corpus (a "Dusknoir" that U-turns, a "Vaporeon" that Focus
    Blasts).

    With the exact-teams sidecar the roster is ground truth, so the disguise can
    be PROVEN rather than guessed: movesets are fixed for the whole battle, so an
    occupant that selects a move which is not in the sidecar moveset of the
    species it is shown as, but IS in the sidecar moveset of that side's Illusion
    bearer, cannot be the shown species.  Illusion is the only mechanism in gen9
    randbats that makes the protocol attribute a move to the wrong species (PS
    prints the |move| line against `pokemon`, whose id is rendered through
    `toString()` -> the illusion's name, sim/pokemon.ts:531), so the occupant is
    the bearer.

    Only self-selected moves count as evidence.  A move CALLED by another effect
    (Sleep Talk / Copycat / Dancer / Magic Bounce / Metronome / Instruct) is
    printed with a `[from]` tag and is excluded at harvest time, and an occupant
    that Transformed is copying its target's moves and is excluded here.

    The proven span is the whole OCCUPANCY (its switch-in -> its switch-out): the
    physical mon standing in the slot does not change between those events, so
    proving it once proves it throughout.  Spans a |replace| already announced are
    not duplicated.  An occupancy that stays unexplained is left alone --
    `illusion_unresolved_turn` refuses on it rather than guessing which stay was
    the disguise."""
    reveals.setdefault("illusion_unresolved", {})
    if not exact_teams:
        return
    illusions = reveals.setdefault("illusions", [])
    known = {}
    for il in illusions:
        known.setdefault((il["pid"], il["start_turn"], il["disguise"]), il)
    bearers = {}
    for pid in ("p1", "p2"):
        for key, mon in (exact_teams.get(pid) or {}).items():
            if normalize_name(mon.get("ability", "") or "") == _ILLUSION_ABILITY:
                bearers[pid] = (key, _sidecar_moves(mon))
                break
    reveals["illusion_bearers"] = {pid: key for pid, (key, _) in bearers.items()}
    if not bearers:
        return
    unresolved = reveals["illusion_unresolved"]
    for occ in reveals.get("occupancies", ()):
        bearer = bearers.get(occ["pid"])
        if bearer is None or occ["transformed"]:
            continue
        bearer_key, bearer_moves = bearer
        if occ["species"] == bearer_key:
            continue  # already standing as itself
        announced = known.get((occ["pid"], occ["start_turn"], occ["species"]))
        if announced is not None and occ.get("revealed_true_species") != announced.get(
            "true_species"
        ):
            # (pid, start_turn, species) is not an identity: when the revealed Zoroark
            # FAINTS, its replacement enters on the SAME turn, and if that replacement is
            # the genuine mon of the disguise species the two occupancies collide.
            # Extending the span with the replacement's window overlaid Zoroark's
            # Normal/Ghost typing onto the real mon for the rest of the game --
            # synth28469 T17 (a "Piloswine" with types NORMAL,GHOST two turns after the
            # Zoroark died, so Supercell Slam hit it instead of being Ground-immune) and
            # synth33481 T27 (Goodra-Hisui, Poison Jab). The |replace| stamps
            # `revealed_true_species` on the DISGUISED occupancy only, so it is the
            # identity test.
            announced = None
        if announced is not None:
            # |replace| ends the span at the REVEAL turn, but the physical mon
            # stays in the slot until it switches out.  On the opponent side
            # `battle_modifier.illusion_end` swaps the reconstructed active over
            # to the real Zoroark at the reveal, so the extra turns simply never
            # match here; on the USER side that handler is a no-op
            # (`is_opponent` gate), the active goes on standing as the disguise,
            # and every post-reveal turn needs the same type substitution.
            announced["end_turn"] = max(announced["end_turn"], occ["end_turn"])
            announced["bearer_tera"] = occ["tera_during"] or occ["entry_tera"]
            announced["bearer_tera_from"] = _bearer_tera_from(occ)
            continue
        shown = _sidecar_moves((exact_teams.get(occ["pid"]) or {}).get(occ["species"]))
        if not shown:
            continue
        selected = occ["moves"] - _NEVER_OWN_MOVE
        witnesses = (selected - shown) & bearer_moves
        if witnesses:
            illusions.append(
                {
                    "pid": occ["pid"],
                    "disguise": occ["species"],
                    "true_species": occ.get("revealed_true_species") or bearer_key,
                    "start_turn": occ["start_turn"],
                    "end_turn": occ["end_turn"],
                    "bearer_tera": occ["tera_during"] or occ["entry_tera"],
                    "bearer_tera_from": _bearer_tera_from(occ),
                    "inferred_from": sorted(witnesses),
                }
            )
            continue
        # the mirror-image proof: a move only the SHOWN species has settles the
        # occupancy as the genuine article, because the bearer could not have
        # selected it
        if selected & (shown - bearer_moves):
            continue
        # THE MOVE EVIDENCE IS A COIN FLIP -- try the ITEM.
        #
        # The sidecar item is a fixed set property of exactly the same kind as
        # the moveset the two arms above use, and Illusion touches neither: PS
        # renders the slot's NAME through the disguise (sim/pokemon.ts:531) but
        # the pokemon object keeps its own `item`.  So a holding proven against
        # the SLOT identifies the occupant whenever the two candidates' set items
        # differ.
        #
        # APPROXIMATIONS.md 16.3(c) / synth15565: the T7 occupancy shows
        # Hoopa-Unbound, and `trick` is in BOTH Hoopa-Unbound's and Zoroark's
        # sidecar movesets, so `witnesses` and the mirror proof are both empty.
        # But the Trick handed a CHOICE BAND over -- Hoopa-Unbound's set item,
        # where Zoroark's is Choice Specs -- and nothing had been acquired yet,
        # so the Tricker was the genuine Hoopa.
        #
        # The whole soundness of this rests on `_no_prior_acquisition`, which is
        # checked per observation inside `_item_evidence_occupant` and REFUSES
        # rather than guessing.  NEGATIVE CONTROL: with
        # FP_CONTROL_ILLUSION_ITEM_EVIDENCE_OFF the arm is skipped entirely and
        # the occupancy falls through to the refusal below, exactly as before
        # this fix.  Gates this one mechanism and nothing else (HANDOFF sec 4
        # rule 18).
        if not _control("FP_CONTROL_ILLUSION_ITEM_EVIDENCE_OFF"):
            side = exact_teams.get(occ["pid"]) or {}
            bearer_item = _sidecar_item(side.get(bearer_key))
            settled = _item_evidence_occupant(
                reveals, occ, _sidecar_item(side.get(occ["species"])), bearer_item
            )
            if settled == "shown":
                continue  # proven the genuine article
            if (
                settled == "bearer"
                and occ.get("survived_damaging_hit")
                and not occ.get("revealed_true_species")
            ):
                # THE PS-IMPOSSIBILITY TRIPWIRE.  Illusion ends on any damaging
                # move hit -- `onDamagingHit` -> `singleEvent('End', ...)` ->
                # `this.add('replace', ...)` (data/abilities.ts:2061-2071) --
                # and is cleared SILENTLY only by fainting (:2078 `onFaint`).
                # So an occupancy that took a damaging hit, SURVIVED it, and
                # never produced a |replace| is PS-IMPOSSIBLE as a disguise,
                # whatever the item says.  This is the strongest disconfirming
                # evidence the simulator offers and the bearer arm otherwise
                # never consults it.  Defence in depth exactly where the
                # evidence is thinnest: every one of the 71 corpus resolutions
                # of this arm came out "shown", so "bearer" has zero corpus
                # support and is the direction a premise violation fabricates
                # through.  A tripwire hit is a REFUSAL, never a "shown".
                settled = None
            if settled == "bearer":
                illusions.append(
                    {
                        "pid": occ["pid"],
                        "disguise": occ["species"],
                        "true_species": occ.get("revealed_true_species") or bearer_key,
                        "start_turn": occ["start_turn"],
                        "end_turn": occ["end_turn"],
                        "bearer_tera": occ["tera_during"] or occ["entry_tera"],
                        "bearer_tera_from": _bearer_tera_from(occ),
                        "inferred_from": ["item:" + bearer_item],
                    }
                )
                continue
        # neither proof fired: this stay is a coin-flip between the real mon and
        # the bearer wearing its face, and nothing in the protocol or the sidecar
        # decides it.  Record the window so both the categorical gate and the
        # damage check refuse on it instead of asserting against a species that
        # may not be there.
        unresolved.setdefault(occ["pid"], []).append(
            (occ["start_turn"], occ["end_turn"])
        )


def _mark_protocol_illusion_ambiguity(reveals) -> None:
    """No-sidecar twin of `_infer_illusion_spans`'s refusal arm.

    A `|replace|` proves this side's roster carries an Illusion bearer -- PS
    emits it only when the disguise BREAKS on a damaging hit
    (data/abilities.ts illusion `onDamagingHit` -> `singleEvent('End', ...)` ->
    `this.add('replace', ...)`) -- and Illusion is re-applied at every
    switch-in, so any EARLIER occupancy showing the SAME species may equally
    have been the bearer wearing that face.  The protocol alone cannot decide
    it, and asserting the shown species there asserts types/stats/moves nothing
    certified.  Witness (real-ladder Sassyflygon2 T13): a shown "Zacian" that
    is immune to a Psychic-type Expanding Force, carries Dark Pulse and Life
    Orb -- none of which Zacian can produce -- and whose 91/100 exit HP is the
    entry HP of the occupancy the later `|replace|` unmasked as Zoroark, L83.

    Occupancies starting at or after the bearer's FAINT are genuine (a fainted
    bearer cannot re-disguise; in the witness log the real Zacian enters one
    line after `|faint|p1a: Zoroark` and announces Intrepid Sword) and stay
    asserted.  Occupancies wearing a DIFFERENT face than any announced disguise
    are left alone -- marking every stay on the side would delete decidable
    coverage wholesale; the narrower rule is deliberately evidence-bounded.

    Sidecar-backed logs must never reach this: there `_infer_illusion_spans`
    PROVES or refuses each occupancy from the exact movesets, and re-marking
    proven spans here would delete decidable coverage.  The caller gates on
    `exact_teams is None`."""
    unresolved = reveals.setdefault("illusion_unresolved", {})
    faints = reveals.get("faint_turns") or {}
    for il in reveals.get("illusions", ()):
        pid = il["pid"]
        bearer_faint = faints.get((pid, il.get("true_species")))
        for occ in reveals.get("occupancies", ()):
            if occ["pid"] != pid or occ.get("transformed"):
                continue
            if occ["species"] != il["disguise"]:
                continue
            if occ["start_turn"] == il["start_turn"]:
                continue  # the announced span itself; _apply_illusion covers it
            if bearer_faint is not None and occ["start_turn"] >= bearer_faint:
                continue
            span = (occ["start_turn"], occ["end_turn"])
            if span not in unresolved.get(pid, []):
                unresolved.setdefault(pid, []).append(span)


def _occupancy_covering(reveals, pid: str, turn: int):
    """The slot occupancy that physically holds this side's active in `turn`'s PRE-state.

    Same half-open window every other span consumer uses: `start_turn` is the turn during
    whose RESOLUTION the mon walked in, so it is not there yet in that turn's pre-state.
    The LAST match wins (a slot can be re-entered)."""
    found = None
    for occ in (reveals or {}).get("occupancies", ()):
        if occ["pid"] != pid:
            continue
        if occ["start_turn"] < turn <= occ["end_turn"]:
            found = occ
    return found


def _apply_slot_tera(battler, pid, reveals, turn) -> None:
    """Resolve the ACTIVE's terastallization from the SLOT it stands in, not from a
    species-name lookup.

    `|-terastallize|p1a: Volbeat|Fighting` names the SLOT and renders the occupant through
    `toString()` -- which is the ILLUSION's name (sim/pokemon.ts:531).  Every consumer that
    binds the tera to that name credits it to the disguise species' real owner, which is a
    different pokemon sitting in the same party:

      * synth22893 / synth27340: a disguised Zoroark-Hisui tera'd, and the REAL Rayquaza /
        Weavile came out terastallized -- pure Fighting / Normal instead of Dragon/Flying
        and Dark/Ice -- so their `|-immune|` to Earthquake / Psyshock was contradicted by
        every branch.
      * synth24996: T4's tera was visible in T3's pre-state.
      * synth42893 / synth34663: no illusion at all, just a forme rename -- the protocol
        says "Minior" while the reconstruction tracks `miniormeteor`, so the name lookup
        missed and the tera-Flying STAB upgrade (1.5x -> 2.0x on a base-Flying type) never
        applied.

    The occupancy record is slot-based and immune to all three: `entry_tera` is the
    `tera:` suffix `getFullDetails` derives from the pokemon ACTUALLY entering
    (sim/pokemon.ts:544-553), and `tera_during` / `tera_during_turn` are the
    `|-terastallize|` seen while this occupant held the slot.

    CLEARING a tera the reconstruction wrongly set is gated on this side having an
    Illusion bearer: Illusion is the only gen9-randbats mechanism that makes the protocol
    attribute a per-mon line to the wrong species, so without a bearer there is nothing to
    correct and the live tracking (and, for the user, the request json) stays
    authoritative."""
    active = getattr(battler, "active", None)
    if active is None:
        return
    occ = _occupancy_covering(reveals, pid, turn)
    if occ is None:
        return

    if occ.get("entry_tera"):
        # already terastallized when it walked in, so it holds for the whole occupancy
        active.tera_type = occ["entry_tera"]
        active.terastallized = True
        return

    tera_during = occ.get("tera_during")
    if tera_during:
        during = occ.get("tera_during_turn")
        # the tera lands mid-resolution, so the pre-state of that turn is NOT tera'd yet
        # -- but the type must be known for the mid-turn application to resolve.
        #
        # EXCEPT when the snapshot itself is MID-turn: a pivot (U-turn) splits the
        # turn's resolution into several |t:| chunks with their own requests, and a
        # chunk armed AFTER the |-terastallize| of the SAME turn carries a live
        # tracking state that is already (correctly) tera'd.  Blanket-clearing here
        # un-tera'd Sneasler for the post-pivot chunk of synth49327 T7: the engine
        # then read it as Fighting/Poison, and Synchronize's reflected psn -- real,
        # because tera-Dark had removed the Poison typing -- was immune in every
        # branch.  `during == turn and active.terastallized` is exactly "the live
        # tracking saw this turn's tera before this snapshot", so keep it.
        active.tera_type = tera_during
        active.terastallized = (during is not None and during < turn) or (
            during == turn and bool(getattr(active, "terastallized", False))
        )
        return

    # this occupant never terastallized
    if (reveals or {}).get("illusion_bearers", {}).get(pid):
        active.terastallized = False


def illusion_unresolved_turn(reveals, pid: str, turn: int) -> bool:
    """True when this side's active on `turn` is a stay `_infer_illusion_spans`
    could prove neither a disguise nor genuine.  Same half-open window as the
    spans: the pre-state of the ENTRY turn still holds the previous occupant."""
    for start, end in ((reveals or {}).get("illusion_unresolved") or {}).get(pid, ()):
        if start < turn <= end:
            return True
    return False


def _illusion_switch_target(reveals, pid: str, turn: int, species: str | None):
    """The species a switch performed DURING `turn` really brought in.

    An Illusion span's `start_turn` is the turn during whose resolution the disguised mon
    walked in, so a span starting exactly here means this turn's switch-in was the bearer
    wearing the shown species' face.  `_apply_illusion` deliberately excludes that turn
    (the PRE-state still holds the previous occupant), so the correction has to happen on
    the ACTION instead: switch in the real mon.

    Without it the engine switched in the genuine disguise species and the incoming mon's
    typing decided the turn -- a "Primarina" that Draco Meteor could not touch
    (synth29327 T4) and an "Alcremie" likewise (synth40054 T10), so the attacker's own
    self -2 SpA (which PS only applies on a landed hit) was unreachable in every branch.
    The corpus is full-knowledge and the |replace| lands in the same turn, so this is a
    reveal the checker is entitled to use."""
    if not species:
        return species
    for il in (reveals or {}).get("illusions", ()):
        if (
            il["pid"] == pid
            and il["start_turn"] == turn
            and il["disguise"] == species
            and il.get("true_species")
        ):
            return il["true_species"]
    return species


def _apply_illusion(battler, pid, reveals, turn) -> None:
    """Substitute a disguised Zoroark's real types onto a side's active.
    When a |replace| (or the sidecar inference above) reveals the current active
    was a disguised Zoroark this
    turn, the reconstructed active still carries the DISGUISE's (wrong) types, so a
    type-based check misfires (a Dark Zoroark shown as a Fairy 'Zacian' is wrongly
    damaged by a Psychic Expanding Force instead of being immune).  Overriding the
    active's `types` to Zoroark's real (Dark) types resolves those checks; stats
    are irrelevant to the categorical type checks the comparator makes.

    Applied to BOTH sides.  The user's own team is authoritative from the request
    JSON in general, but not here: `getSwitchRequestData` reports the TRUE species
    (sim/pokemon.ts:1159-1162 uses `this.details`) while the |switch| line the
    reconstruction is built from reports the disguise for every viewer including
    the owner (`getFullDetails`, :544-552, substitutes `this.illusion`'s details).
    The two disagree, `update_from_request_json` raises on the mismatch, and the
    user's active is left standing as the disguise -- so a user-side Zoroark is
    exactly as mis-typed as an opponent-side one.

    The span is per-OCCUPANCY (the disguised mon's own switch-in -> its
    |replace|), never the disguise species' whole history: the REAL mon of that
    species is on the same team (Illusion copies the last party member) and its
    own stays must keep their true types.

    The ENTRY turn is EXCLUDED (`start_turn < turn`, strictly).  `start_turn` is
    the turn during whose RESOLUTION the disguised mon switched in, so it is not
    yet on the field in that turn's PRE-state -- the mon standing there is
    whoever the switch replaced, very often the REAL mon of the disguise species
    (synth00421: the genuine Forretress switches in during T22, the Zoroark
    pivots in during T23 and is |replace|d in T24; with an inclusive start the
    T23 pre-state's real Forretress was typed Normal/Ghost, making Gurdurr's
    Drain Punch read as immune).  `end_turn` stays INCLUSIVE: the |replace|
    happens mid-turn, so that turn's pre-state still holds the disguise.

    TERASTALLIZATION is carried by the span too, for the same object-identity
    reason: `terastallized` on the reconstructed disguise belongs to the REAL mon
    of that species (synth31657: the genuine Garganacl teras Dragon on turn 1, so
    the Zoroark-Hisui wearing its face still read as a Dragon type and Close
    Combat was not immune).  `bearer_tera` is the entrant's own state, read off
    the switch line's `tera:` suffix -- which `getFullDetails` derives from the
    entering pokemon, not from the illusion (sim/pokemon.ts:544-553) -- plus any
    `|-terastallize|` during the span."""
    if battler.active is None:
        return
    active_key = _species_key(battler.active.name)
    for il in reveals.get("illusions", ()):
        # The span covers ONE physical occupancy, and the reconstruction can be
        # standing it under either name: as the DISGUISE (every turn before the
        # |replace|, and every turn after it on the user side, where
        # `battle_modifier.illusion_end`'s swap is gated to the opponent), or as
        # the TRUE species (after the swap on the opponent side).  Both are the
        # same mon, so both need the bearer's own state -- the tera in
        # particular, which `illusion_end` does not carry across the swap: it
        # was applied to the disguise object by the |-terastallize| handler and
        # the revealed Zoroark is left un-tera'd, which put a Normal/Ghost
        # Zoroark-Hisui where PS had a pure-Fighting one (synth25867 T3) and a
        # pure-Normal one (synth39515 T8), making Close Combat read as immune
        # and its self-`unboost` unreachable in every branch.
        if il["pid"] != pid or active_key not in (
            il["disguise"],
            il.get("true_species"),
        ):
            continue
        if not (il["start_turn"] < turn <= il["end_turn"]):
            continue
        standing_as_disguise = active_key == il["disguise"]
        # the bearer's tera holds only from the turn it actually terastallized
        # (`_bearer_tera_from`), not across the whole span
        bearer_tera = il.get("bearer_tera")
        if bearer_tera is not None and turn <= il.get(
            "bearer_tera_from", il["start_turn"]
        ):
            bearer_tera = None
        if "bearer_tera" in il:
            battler.active.terastallized = bearer_tera is not None
            if bearer_tera is not None:
                battler.active.tera_type = bearer_tera
        true_types = pokedex.get(il["true_species"], {}).get(constants.TYPES)
        if bearer_tera:
            battler.active.types = [bearer_tera]
        elif standing_as_disguise and true_types:
            # only the disguise carries the WRONG species' types; once the
            # reconstruction stands the mon under its true name its types are
            # already right and must not be re-derived from the pokedex (a
            # Zoroark that Soaked / Reflect Typed would be clobbered)
            battler.active.types = list(true_types)
        if standing_as_disguise:
            # ...and the ABILITY is the bearer's too. The physical mon is a Zoroark, so
            # its ability is Illusion (which does nothing once the disguise is up); the
            # disguise species' ability is not in play at all. Leaving it set gave a
            # "Jolteon" its VOLT ABSORB, which absorbed the Thunder Wave PS actually
            # paralysed the Zoroark with (synth24996 T3). Illusion is a fixed randbats
            # set property of both Zoroark formes, so it needs no per-game lookup.
            battler.active.ability = _ILLUSION_ABILITY
        return


def _apply_illusion_sleep_counter(battler, pid, reveals, turn) -> None:
    """Give a disguised / just-revealed Illusion bearer the sleep attempts it actually
    served, counted on the SLOT (see `slot_sleep_attempts` in _harvest_reveals).

    Scoped to the bearer: it is the only pokemon whose per-mon counter the reconstruction
    can lose, because it is the only one sharing an object with a different physical mon.
    Raise-only, so a correctly-tracked counter is never lowered."""
    active = getattr(battler, "active", None)
    if active is None or active.status != constants.SLEEP or active.rest_turns:
        return
    bearer = (reveals or {}).get("illusion_bearers", {}).get(pid)
    if not bearer:
        return
    key = _species_key(active.name)
    if key != bearer and not any(
        il["pid"] == pid
        and il["start_turn"] < turn <= il["end_turn"]
        and key in (il["disguise"], il.get("true_species"))
        for il in reveals.get("illusions", ())
    ):
        return
    attempts = reveals.get("sleep_attempts_by_turn", {}).get((pid + "a", turn))
    if attempts is not None and attempts > active.sleep_turns:
        active.sleep_turns = attempts


def _arm_cudchew(battler, pid, reveals, turn) -> None:
    """Give a Cud Chew holder that ate a berry on the PREVIOUS turn the engine
    state its end-of-turn re-eat is gated on.

    PS: `cudchew` (data/abilities.ts) reacts to onEatItem by scheduling a re-eat
    of `lastItem` at the end of the FOLLOWING turn.  poke-engine models that with
    a CUDCHEW volatile applied at the eat (items.rs:525 `maybe_start_cudchew`)
    plus a two-state duration that its end-of-turn arm advances 0 -> 1 on the eat
    turn and consumes at 1 (generate_instructions.rs:5917).  Nothing in the
    replay reconstruction sets either: `update_battle` has no Cud Chew handling,
    so the pre-turn state of the re-eat turn carries neither the volatile nor the
    counter and the engine produces no re-eat at all -- the observed
    `-activate|ability: Cud Chew` / `-enditem <berry>|[eat]` / berry effect is
    then unreproducible in every branch.

    The berry itself reaches the engine as `last_consumed_item` (converted from
    `removed_item` in fp.search.poke_engine_helpers), so only the arming is
    missing.  Armed for the ACTIVE only, and only when the eat was last turn --
    a mon that switched out in between is no longer the active and PS drops the
    volatile on switch-out anyway."""
    active = getattr(battler, "active", None)
    if active is None or normalize_name(active.ability or "") != "cudchew":
        return
    eats = reveals.get("berry_eats", {}).get((pid, _species_key(active.name)))
    if not eats or (turn - 1) not in eats:
        return
    if "cudchew" not in active.volatile_statuses:
        active.volatile_statuses.append("cudchew")
    # 1 == "the eat turn's end of turn already ticked": re-eat at THIS turn's end
    active.volatile_status_durations["cudchew"] = 1


# Terapagos' forme-LINKED abilities.  PS's Tera Shift formeChanges base Terapagos
# into Terapagos-Terastal the instant it switches in and the Terastal forme's
# ability is Tera Shell (data/abilities.ts:4956 `terashift.onSwitchIn` ->
# formeChange('Terapagos-Terastal'); :4949 terashell); terastallizing changes it
# again to Terapagos-Stellar with Teraform Zero (sim/battle-actions.ts:1956-1958).
# poke-engine performs both rewrites itself, but ONLY when the transition happens
# inside the simulated turn (src/genx/abilities.rs:2176-2201 for the Tera Shift
# switch-in, genx/generate_instructions.rs:9184 for the tera).  The replay
# reconstruction meets these mons ALREADY in the changed forme and every knowledge
# source it has -- randbats sets and the corpus sidecar alike -- reports the BASE
# forme's Tera Shift, so nothing ever rewrites it: the state builder must.  Left
# as TERASHIFT, Tera Shell's at-full-HP 0.5x (abilities.rs:3369, MULTISCALE-style
# hp==maxhp gate) never applies -- synth01373 T27: Close Combat KOs the full-HP
# Terapagos in every branch, so its own Rapid Spin (and the speed boost) is
# unreachable.
_FORME_ABILITY = {
    "terapagosterastal": "terashell",
    "terapagosstellar": "teraformzero",
}
# only a stale ability from the SAME forme chain is rewritten -- a Skill Swap /
# Gastro Acid product or any genuinely different ability is left alone
_FORME_ABILITY_STALE = frozenset(("terashift", "terashell", "teraformzero", ""))


def _apply_forme_abilities(battler) -> None:
    """Set the ability a mon's CURRENT forme implies where the reconstruction
    cannot observe the forme-change that would have set it (see _FORME_ABILITY)."""
    mons = list(battler.reserve)
    if battler.active is not None:
        mons.append(battler.active)
    for pkmn in mons:
        want = _FORME_ABILITY.get(_species_key(pkmn.name))
        if want is None or pkmn.ability == want:
            continue
        if (pkmn.ability or "") in _FORME_ABILITY_STALE:
            pkmn.ability = want


# A `-start ... Encore` on a slot BEFORE that slot's own |move| line means the
# executed move was Encore-overridden, so the side's CHOSEN move is unknowable.
_ENCORE_START = ("Encore", "move: Encore")


def _encore_overridden_side(block_lines, user_pid) -> str | None:
    """Return the side ("user"/"opp") whose move this block shows was overridden
    by an Encore applied EARLIER IN THE SAME BLOCK, else None.

    PS's Encore rewrites the target's action to its last-used move
    (data/moves.ts encore `onOverrideAction`), so the |move| line that follows
    the `-start` names the LOCKED move, not the move the side selected.  Feeding
    that locked move to `generate_instructions` as the side's decision hands the
    engine the wrong priority and the wrong turn order -- synth00543 T31: Hypno
    is Encored into Protect, and replaying "protect" as its choice gives it
    Protect's +4 priority so it moves FIRST and blocks the very Encore that is
    observed landing.  The chosen move is not recoverable from the protocol, so
    the turn is skipped rather than checked against a fabricated decision."""
    encore_at: dict[str, int] = {}
    move_at: dict[str, int] = {}
    for i, line in enumerate(block_lines):
        sp = line.split("|")
        if len(sp) < 3:
            continue
        slot = sp[2].split(":")[0].strip()
        if not re.match(r"^p[12][ab]$", slot):
            continue
        if sp[1] == "move":
            move_at.setdefault(slot, i)
        elif sp[1] == "-start" and len(sp) >= 4 and sp[3].strip() in _ENCORE_START:
            encore_at.setdefault(slot, i)
    for slot, enc_i in encore_at.items():
        move_i = move_at.get(slot)
        if move_i is not None and enc_i < move_i:
            return "user" if slot[:2] == user_pid else "opp"
    return None


# ---------------------------------------------------------------------------
# KO-boundary margin: findings the engine's FOLDED damage rolls cannot decide
# ---------------------------------------------------------------------------
# The installed poke-engine 0.0.49 python binding exposes
#     generate_instructions(py_state, side_one_move, side_two_move)
# (poke_engine/poke_engine.pyi:509) -- three parameters, no damage-branching
# knob -- so the checker CANNOT request DamageBranching::Branch kill-roll fans
# from python; it gets whatever the wheel was compiled with, and that wheel
# folds a hit to a single 0.925 * max_damage roll (a crit to 0.925 *
# max_crit_damage, a multi-hit to the same fold per hit) instead of enumerating
# the 16 rolls.  Rebuilding the wheel is off the table (a live sweep has it
# mmap'd), so this residual is CLASSIFIED rather than eliminated -- the fallback
# the fix brief specifies.
#
# The failure mode: when the real roll landed within a hair of the defender's
# HP, the folded roll sits on the OTHER side of the KO threshold, and every
# faint-gated companion effect becomes unreachable in EVERY branch:
#   * protocol KOs / engine survives -> the effect never fires.  Moxie and
#     Chilling Neigh attack boosts: synth02943 T3 Triple Axel folds to
#     45+88+130 = 263 against Lurantis' 264 HP (1 short); synth01616 T22
#     19+39+58 = 116 against Lumineon's 118 (2 short); synth00611 T4 the crit
#     High Horsepower folds to 199 against Gurdurr's 202 (3 short).
#   * protocol survives / engine KOs -> the effect is SUPPRESSED by the phantom
#     faint.  synth02538 T33: the folded Surf (168) plus Gulp Missile (64) take
#     Heatran to exactly 0, so the missile's Defense drop is skipped, while the
#     real Surf rolled 7 low and left it alive at 3 HP.
# Neither is an engine fidelity breach, so those findings are reported SOFT.
#
# The gate stays narrow deliberately: it fires only when NO branch reproduces
# the protocol's faint outcome AND the closest branch misses the KO threshold by
# at most _KO_MARGIN_HP.  A turn where the engine and the protocol disagree
# about a faint by more than a roll's worth of HP is a real mismatch and stays
# HARD -- as does a turn where they AGREE about the faint, which is why
# synth02878 T3 keeps its HARD status finding (the engine faints that Cramorant
# in every branch and still drops Gulp Missile's paralysis: a genuine defect).
_KO_MARGIN_HP = 4


def _protocol_faints(block_lines, user_pid) -> dict:
    """{"user": bool, "opp": bool}: did the mon the engine simulated for that
    side faint in this block?  Scanning stops at the first switch-family line
    that FOLLOWS a faint, mirroring extract_observed_events' truncation -- past
    that point the slot holds a replacement the engine never simulated."""
    out = {"user": False, "opp": False}
    faint_seen = False
    for line in block_lines:
        sp = line.split("|")
        if len(sp) < 3:
            continue
        if faint_seen and sp[1] in ("switch", "drag", "replace"):
            break
        if sp[1] != "faint":
            continue
        pid = sp[2].split(":")[0].strip()[:2]
        out["user" if pid == user_pid else "opp"] = True
        faint_seen = True
    return out


def _simulated_pokemon(battler, action):
    """The mon this side's branch damage lands on: the switch TARGET when the
    side's decision was a switch, else the turn-start active."""
    pkmn = battler.active
    if action is not None and action[0] == "switch":
        want = _species_key(action[1])
        for cand in list(battler.reserve) + (
            [battler.active] if battler.active is not None else []
        ):
            if _species_key(cand.name) == want:
                pkmn = cand
                break
    return pkmn


def _simulated_hp(battler, action) -> int | None:
    """Pre-turn HP -- rounded exactly as battle_to_poke_engine_state rounds it --
    of the mon this side's branch damage lands on (see _simulated_pokemon)."""
    pkmn = _simulated_pokemon(battler, action)
    if pkmn is None:
        return None
    return int(pkmn.hp)


def _branch_hp_after(branch, side_key, hp) -> int:
    """Remaining HP of side_key's simulated mon at the end of one branch.
    `Damage SideX` is damage dealt TO SideX; a NEGATIVE `Heal SideX` is
    self-inflicted damage (Life Orb), so both are netted here."""
    remaining = hp
    for instr in branch:
        if instr.side != side_key:
            continue
        if instr.kind == "Damage":
            remaining -= instr.amount() or 0
        elif instr.kind == "Heal":
            remaining += instr.amount() or 0
    return remaining


def _ko_margin_sides(parsed, snap, u_action, o_action, block_lines, user_pid) -> list:
    """Sides whose faint outcome NO branch reproduces, and which the closest
    branch misses by at most _KO_MARGIN_HP -- i.e. the disagreement is inside
    the damage spread the folded roll threw away (see _KO_MARGIN_HP above)."""
    if not parsed:
        return []
    faints = _protocol_faints(block_lines, user_pid)
    out = []
    for side, battler, action, side_key in (
        ("user", snap.user, u_action, "s1"),
        ("opp", snap.opponent, o_action, "s2"),
    ):
        hp = _simulated_hp(battler, action)
        if hp is None or hp <= 0:
            continue
        outcomes = set()
        margin = None
        for branch in parsed:
            remaining = _branch_hp_after(branch, side_key, hp)
            outcomes.add(remaining <= 0)
            margin = abs(remaining) if margin is None else min(margin, abs(remaining))
        if faints[side] in outcomes:
            continue  # some branch already reproduces the observed KO status
        if margin is not None and margin <= _KO_MARGIN_HP:
            out.append(side)
    return out


def _demote_ko_margin_findings(
    turn_findings, parsed, snap, u_action, o_action, block_lines, user_pid, stats
) -> None:
    """Downgrade this turn's HARD findings to SOFT when a KO-boundary margin
    (see _KO_MARGIN_HP) explains them.  The finding is kept, not suppressed: it
    is tagged so triage can still cluster and count the class."""
    sides = _ko_margin_sides(parsed, snap, u_action, o_action, block_lines, user_pid)
    if not sides:
        return
    reason = (
        "engine/protocol faint disagreement on {} within {} HP of the KO "
        "threshold; the folded damage roll (the installed binding exposes no "
        "kill-roll branching) cannot decide this turn"
    ).format("+".join(sides), _KO_MARGIN_HP)
    for f in turn_findings:
        if f.severity is not Severity.HARD:
            continue
        f.severity = Severity.SOFT
        f.message += " [ko-margin]"
        f.predicted = (f.predicted + "; " if f.predicted else "") + reason
        stats["ko_margin_demotions"] = stats.get("ko_margin_demotions", 0) + 1


# Abilities that ABSORB a move outright. PS's handlers return `null`, which makes the
# attacker's `moveThisTurnResult` false exactly like a miss (sim/battle-actions.ts), and
# they announce themselves with a `[from] ability: X|[of] <attacker>` annotation rather
# than an `|-immune|` line -- synth19386 T18's Waterfall into Water Absorb is only
# `|-heal|p1a: Poliwrath|302/302|[from] ability: Water Absorb|[of] p2a: Gyarados`.
_ABSORB_ABILITIES = frozenset(
    (
        "voltabsorb",
        "waterabsorb",
        "dryskin",
        "flashfire",
        "lightningrod",
        "stormdrain",
        "motordrive",
        "sapsipper",
        "wellbakedbody",
        "eartheater",
        "windrider",
        "magicbounce",
    )
)
# Actions that mark the in-flight move as FAILED for its user.
_MOVE_FAILED_ACTIONS = frozenset(("-fail", "-miss", "-immune", "-notarget"))


def _move_failed_sides(block_lines) -> dict:
    """Which sides' moves FAILED in this resolution block (PS `moveThisTurnResult`
    false), read straight off the protocol.

    Consumed as the NEXT turn's `moveLastTurnResult`, which is what Stomping Tantrum /
    Temper Flare double on (data/moves.ts temperflare basePowerCallback
    `source.moveLastTurnResult === false`).

    A gen9 Protect-class block is deliberately NOT a failure: PS's protect handlers do not
    clear the flag in this generation (the same carve-out the engine documents at
    genx/choice_effects.rs:404-410). A side that SWITCHED after its failed move is also
    not carried: the engine's flag is per-SIDE while PS's is per-POKEMON, so it is only
    honest while the same mon is still standing."""
    failed = {"p1": False, "p2": False}
    switched_after: dict = {}
    attacker_slot = None
    for line in block_lines:
        sp = line.split("|")
        if len(sp) < 3:
            continue
        action = sp[1]
        slot = sp[2].split(":")[0].strip()
        if action in ("switch", "drag"):
            switched_after[slot[:2]] = True
            attacker_slot = None
            continue
        if action == "move":
            attacker_slot = slot
            switched_after.pop(slot[:2], None)
            continue
        if attacker_slot is None:
            continue
        if action in _MOVE_FAILED_ACTIONS:
            failed[attacker_slot[:2]] = True
            continue
        m = _FROM_ABILITY.search(line)
        if m is not None and normalize_name(m.group(1)) in _ABSORB_ABILITIES:
            of = _OF_SLOT.search(line)
            if of is not None and of.group(1)[:2] == attacker_slot[:2]:
                failed[attacker_slot[:2]] = True
    for pid in ("p1", "p2"):
        if switched_after.get(pid):
            failed[pid] = False
    return failed


def _clamp_used_move_pp(block_lines, snap, user_pid) -> None:
    """A move this block shows a mon USING necessarily had >=1 PP at turn start;
    the reconstruction can under-count (Leppa restoration, PP-tracking drift),
    which wrongly fails pp-gated mechanics (e.g. the Encore pp<=0 gate)."""
    for line in block_lines:
        sp = line.split("|")
        if len(sp) < 4 or sp[1] != "move":
            continue
        pid = sp[2].split(":")[0].strip()[:2]
        battler = snap.user if pid == user_pid else snap.opponent
        if battler.active is None:
            continue
        name = normalize_name(sp[3])
        for m in battler.active.moves:
            if m.name == name and m.current_pp < 1:
                m.current_pp = 1


# Protocol lines that CERTIFY an HP inequality the percent-HP reconstruction cannot
# express.  `|-fail|SLOT|move: Substitute|[weak]` is PS's substitute onTry bailing on
# `source.hp <= source.maxhp / 4` (data/moves.ts substitute onTry), so the user's HP was
# at most floor(maxhp/4) at that instant.  The reconstruction only ever sees the opponent
# as a PERCENT (`|-damage|p2a: X|25/100`), and the round-trip to absolute HP rounds UP
# across that exact boundary: synth02704's Articuno came out at 74/295 = 25.08%, one HP
# over the line, so the engine's Substitute SUCCEEDED and then -- correctly, per PS
# data/moves.ts:3456-3459 -- blocked Defog's evasion drop, making the observed
# `|-unboost|...|evasion|1` unreachable in every branch.
_SUBSTITUTE_WEAK_FAIL = "|move: Substitute|[weak]"
# lines that change a mon's HP; a certificate is only honored when none of these touched
# the certifying slot earlier in the same block (otherwise the bound is on the mid-turn
# HP, not the pre-turn HP the state is built from)
_HP_CHANGING_ACTIONS = frozenset(("-damage", "-heal", "-sethp"))


def _clamp_hp_from_protocol_certificates(block_lines, snap, user_pid) -> None:
    """Honor HP inequalities the protocol states outright (see _SUBSTITUTE_WEAK_FAIL).

    Only applied when nothing has moved that slot's HP earlier in the block, so the
    certified bound really is a bound on the PRE-TURN hp this state is built from."""
    hp_touched: set = set()
    for line in block_lines:
        sp = line.split("|")
        if len(sp) < 3:
            continue
        action = sp[1]
        slot = sp[2].split(":")[0].strip()
        if action in _HP_CHANGING_ACTIONS:
            hp_touched.add(slot)
            continue
        if action != "-fail" or _SUBSTITUTE_WEAK_FAIL not in line:
            continue
        if slot in hp_touched:
            continue
        battler = snap.user if slot[:2] == user_pid else snap.opponent
        active = getattr(battler, "active", None)
        if active is None or not active.max_hp:
            continue
        bound = int(active.max_hp) // 4
        # An exact-HP certificate (fp/hp_certificate.py) is strictly better
        # information than an inequality -- a value the protocol stated
        # outright, not a bound on a percent-derived estimate -- so a
        # certificate that already SATISFIES the bound is left alone rather than
        # clamped down to it.  A certificate that VIOLATES it is a contradiction
        # between two things the protocol said, i.e. a broken certification
        # chain, and is refused loudly (and counted) instead of either side
        # silently winning.
        if hp_certificate.is_exact(active):
            if active.hp <= bound:
                continue
            hp_certificate.refuse_against_bound(
                active, bound, "substitute [weak] fail (hp <= max_hp // 4)"
            )
        if active.hp > bound:
            active.hp = bound


# ---------------------------------------------------------------------------
# PHASE-2 DEFERRED-TURN REPLAY
# ---------------------------------------------------------------------------
# A pivot (U-turn / Volt Switch / Flip Turn / Parting Shot / Shed Tail / Baton
# Pass) and Revival Blessing split their turn in two at a mid-turn decision
# request.  The engine models that split explicitly: the phase-1 branch arms the
# mover's `force_switch` (`ToggleSideOneForceSwitch` / `ToggleSideTwoForceSwitch`,
# plus `ToggleRevivalBlessing` for the revive case) and PARKS the other side's
# move in `switch_out_move_second_saved_move`
# (`SideXMoveSecondSwitchOutMove: NONE -> <move>`), then bails out of that side's
# move (genx/generate_instructions.rs:4459-4469 `!choice.first_move &&
# other_side.force_switch` -> save and return) and skips end-of-turn entirely
# (:6103).  Everything that belongs to the deferred phase -- the slower side's
# move and its boosts/status/hazards, the revived reserve's heal, the whole
# end-of-turn block -- is therefore absent from the phase-1 instruction set, and
# a single `generate_instructions` call can never reproduce it.
#
# The fix is a SECOND call: apply the phase-1 branch to a state copy (the engine
# state is then exactly what the search would see at the mid-turn decision node,
# `get_all_options` genx/state.rs:1192-1235), feed the switching side the
# OBSERVED continuation (the pivot switch-in, or the revived reserve -- both are
# `MoveChoice::Switch`, and a fainted reserve is a legal target while
# `revival_blessing` is set, cf. `add_fainted_switches` genx/state.rs:1004) and
# the other side the parked move, then UNION the two instruction sets per branch
# (phase-1 list ++ phase-2 list) so a turn's observed events may be satisfied by
# either phase while every combined branch stays internally consistent.
#
# Deliberately conservative: phase 2 runs ONLY along the OBSERVED continuation.
# When the engine armed a pivot the protocol never performed (e.g. PS deletes
# `selfSwitch` when Parting Shot's boost is fully blocked -- data/moves.ts
# partingshot onHit:13177-13182 -- while the engine arms it anyway) there is no
# observed switch to continue with, phase 2 is skipped with a counted reason, and
# the resulting findings correctly stay HARD instead of being papered over by a
# fabricated switch-in.
_DEFERRED_MARKERS = frozenset(
    (
        "ToggleSideOneForceSwitch",
        "ToggleSideTwoForceSwitch",
        "ToggleRevivalBlessing",
        "SideOneMoveSecondSwitchOutMove",
        "SideTwoMoveSecondSwitchOutMove",
    )
)

# Upper bound on phase-2 engine calls per turn (one per deferring phase-1
# branch).  Deferred turns are rare and their phase-1 fan-out is small (the
# pivot move's hit/miss/secondary branching), but the sweep runs over 50k games
# so the cost stays explicitly bounded.
_PHASE2_MAX_CALLS = 12


def _index_int(pokemon_index) -> int:
    """PyState exposes indices as `PokemonIndex` reprs ('P0'..'P5')."""
    digits = re.sub(r"\D", "", str(pokemon_index))
    return int(digits) if digits else 0


def _observed_pivot_switch(block_lines, pid) -> str | None:
    """Species key of the mon `pid` brought in AFTER its own |move| this block --
    i.e. the pivot switch-in, not a turn-start switch decision and not a
    post-faint replacement (scanning stops at that side's own |faint|)."""
    acted = False
    for line in block_lines:
        sp = line.split("|")
        if len(sp) < 3:
            continue
        slot = sp[2].split(":")[0].strip()
        if not slot.startswith(pid):
            continue
        if sp[1] == "move":
            acted = True
        elif sp[1] == "faint":
            return None
        elif sp[1] in ("switch", "drag") and acted and len(sp) >= 4:
            return _species_key(sp[3].split(",")[0].strip())
    return None


def _observed_revive_target(block_lines, pid) -> str | None:
    """Species key of the reserve `pid` revived this block: Revival Blessing's
    restore is announced as `|-heal|p2: Vigoroth|50/100|[from] move: Revival
    Blessing` (a bench mon, so the tag carries no a/b slot letter)."""
    for line in block_lines:
        sp = line.split("|")
        if len(sp) < 4 or sp[1] != "-heal" or "Revival Blessing" not in line:
            continue
        m = re.match(r"^(p[12])[ab]?:\s*(.+)$", sp[2].strip())
        if m is not None and m.group(1) == pid:
            return _species_key(m.group(2))
    return None


def _reserve_move_string(side, want: str | None, fainted: bool) -> str | None:
    """The engine move string (the BARE lowercase pokemon id -- `MoveChoice::
    from_string` genx/state.rs:115-121 resolves a bare id to `Switch(index)`)
    for the reserve whose species the protocol named.

    The protocol reports the BASE species for cosmetic formes ('p2: Squawkabilly'
    for SQUAWKABILLYBLUE), so an exact match is tried first and a unique prefix
    match second; anything ambiguous returns None rather than guessing."""
    if not want:
        return None
    active = _index_int(side.active_index)
    cands = []
    for idx, pkmn in enumerate(side.pokemon):
        if idx == active:
            continue
        if fainted:
            if pkmn.hp > 0 or pkmn.maxhp <= 0:
                continue
        elif pkmn.hp <= 0:
            continue
        cands.append((str(pkmn.id).lower(), _species_key(str(pkmn.id))))
    exact = [c for c in cands if c[1] == want]
    if len(exact) == 1:
        return exact[0][0]
    prefixed = [c for c in cands if c[1].startswith(want) or want.startswith(c[1])]
    if len(prefixed) == 1:
        return prefixed[0][0]
    return None


def _eot_skipped_by_faint_replacement(snap, u_action, o_action) -> bool:
    """True when this engine call is one the engine deliberately runs WITHOUT an
    end-of-turn phase, because a side's chosen action is the replacement of a
    fainted active.

    PS runs a faint turn's residuals BEFORE the replacement enters (the faint
    request is queued after `residualEvent`), so poke-engine sets
    `skip_end_of_turn = s1_replacing_fainted_pkmn || s2_replacing_fainted_pkmn`
    (genx/generate_instructions.rs:9504) and emits no residual instructions at
    all.  The checker can still see residual lines in the same protocol block:
    a U-turn that KOs its target splits the turn into (move) / (pivot switch-in
    + RESIDUALS) / (opponent's faint replacement), the mid-turn request makes the
    checker fire the last two as one pseudo-turn, and the residuals in between
    then belong to a phase the engine is right not to model.  Asserting them
    reports a weather expiry / Leftovers tick / status chip the engine can never
    produce (synth04576 T12: `|-weather|none`, RAIN with 1 turn left)."""
    for battler, action in ((snap.user, u_action), (snap.opponent, o_action)):
        if action is None or action[0] != "switch":
            continue
        active = getattr(battler, "active", None)
        if active is not None and active.hp <= 0:
            return True
    return False


def _residual_phase_start(block_lines) -> int | None:
    """Index of the line opening the block's END-OF-TURN phase: PS prints a bare
    `|` separator before the residuals and `|upkeep` after them, so it is the
    last bare `|` preceding `|upkeep`.  None when the block has no upkeep."""
    up = None
    for i, ln in enumerate(block_lines):
        if ln.strip() == "|upkeep":
            up = i
            break
    if up is None:
        return None
    for i in range(up - 1, -1, -1):
        if block_lines[i].strip() == "|":
            return i
    return None


def _second_t_boundary(block_lines) -> int | None:
    """Index of the SECOND `|t:|` line in the block -- the protocol boundary
    between a deferred turn's two phases (the mid-turn decision request sits
    between them, and PS timestamps the sub-block that follows it).  None when
    the block has fewer than two, i.e. nothing to attribute."""
    seen = 0
    for i, ln in enumerate(block_lines):
        if ln.startswith("|t:|"):
            seen += 1
            if seen == 2:
                return i
    return None


def _replay_deferred_phase(state, branches, parsed, block_lines, user_pid, stats):
    """Return `(branch_list, phase2_ran, phase_split)` for this turn.

    Non-deferred turns (the overwhelming majority) return `parsed` UNCHANGED
    after a single membership scan over the already-parsed instructions -- no
    extra engine call, no state copy.  Deferred turns get one phase-2
    `generate_instructions` call per deferring phase-1 branch, and each phase-1
    branch is replaced by its (phase-1 ++ phase-2) continuations.

    `phase_split` is per-combined-branch the number of PHASE-1 instructions (the
    rest are phase 2), or None when nothing was continued.  The comparator needs
    it to keep the two phases' events and instructions from satisfying each
    other -- see TurnContext.phase_split.

    Any failure degrades to phase-1-only matching for that branch with a counted
    reason; nothing here may raise."""
    if not any(i.kind in _DEFERRED_MARKERS for b in parsed for i in b):
        return parsed, False, None

    opp_pid = "p1" if user_pid == "p2" else "p2"
    combined: list = []
    splits: list[int] = []
    ran = False
    calls_left = _PHASE2_MAX_CALLS
    for b_obj, b_parsed in zip(branches, parsed):
        if calls_left <= 0 or not any(i.kind in _DEFERRED_MARKERS for i in b_parsed):
            combined.append(b_parsed)
            splits.append(len(b_parsed))
            continue
        try:
            mid = state.apply_instructions(b_obj)
        except BaseException:
            stats["phase2_apply_errors"] = stats.get("phase2_apply_errors", 0) + 1
            combined.append(b_parsed)
            splits.append(len(b_parsed))
            continue

        # the branch's OWN outcome decides which side (if either) actually
        # deferred: a marker can be present on some branches only (a U-turn that
        # missed arms nothing), so this is read off the applied state.
        if mid.side_one.force_switch:
            sw_side, other_side, sw_pid = mid.side_one, mid.side_two, user_pid
            sw_is_user = True
        elif mid.side_two.force_switch:
            sw_side, other_side, sw_pid = mid.side_two, mid.side_one, opp_pid
            sw_is_user = False
        else:
            combined.append(b_parsed)
            splits.append(len(b_parsed))
            continue

        revival = bool(sw_side.revival_blessing)
        want = (
            _observed_revive_target(block_lines, sw_pid)
            if revival
            else _observed_pivot_switch(block_lines, sw_pid)
        )
        target = _reserve_move_string(sw_side, want, fainted=revival)
        if target is None:
            # the protocol never performed the continuation the engine armed (or
            # named a reserve we cannot resolve): stay with phase 1 so a genuine
            # spurious-pivot defect keeps its finding.
            stats["phase2_no_observed_switch"] = (
                stats.get("phase2_no_observed_switch", 0) + 1
            )
            combined.append(b_parsed)
            splits.append(len(b_parsed))
            continue

        deferred = str(other_side.switch_out_move_second_saved_move or "none").lower()
        if not deferred or deferred == "none":
            deferred = "none"
        else:
            try:
                if other_side.pokemon[_index_int(other_side.active_index)].hp <= 0:
                    deferred = "none"  # parked mover fainted in phase 1
            except BaseException:
                deferred = "none"
        s1_move, s2_move = (
            (target, deferred) if sw_is_user else (deferred, target)
        )

        calls_left -= 1
        try:
            phase2 = generate_instructions(mid, s1_move, s2_move)
        except BaseException:
            stats["phase2_call_errors"] = stats.get("phase2_call_errors", 0) + 1
            combined.append(b_parsed)
            splits.append(len(b_parsed))
            continue
        if not phase2:
            combined.append(b_parsed)
            splits.append(len(b_parsed))
            continue
        ran = True
        for b2 in parse_branches(phase2):
            combined.append(b_parsed + b2)
            splits.append(len(b_parsed))

    if not ran:
        return parsed, False, None
    stats["phase2_turns"] = stats.get("phase2_turns", 0) + 1
    return combined, True, splits


def check_log(
    log_path: str,
    teams_dir: str | None = None,
    damage_collector: list | None = None,
    damage_tolerance: int = 0,
) -> tuple[list[Finding], dict]:
    """teams_dir: directory holding `<game>.teams.json` full-knowledge sidecars
    (synthetic corpus).  When given AND a sidecar exists for this log, both
    sides' reconstructed knowledge is overridden with the exact sets and the
    exact-damage membership check runs (see fp.replay.damage_membership);
    in-scope DamageRecords are appended to damage_collector when provided.
    Default behavior (teams_dir=None) is unchanged."""
    findings: list[Finding] = []
    stats = {
        "turns_checked": 0,
        "turns_skipped": 0,
        "parse_errors": 0,
        "reconstruct_errors": 0,
    }
    # exact-HP certificates whose display-consistency check FAILED (see
    # fp/hp_certificate.py).  A non-zero total means a certification chain is
    # broken somewhere, so it is REPORTED next to reconstruct-errors rather than
    # logged and forgotten -- and each refusal record carries the game/turn it
    # happened in (hp_certificate.CONTEXT), because gate 5's B5 showed a bare
    # count is invisible without re-instrumentation.
    hp_certificate.reset_refusals()
    hp_certificate.set_context(game=os.path.basename(log_path))
    # named refusal buckets raised while reconstructing Substitute absorptions
    # (fp.battle_modifier).  Reset per log and reported in `stats` so a refusal
    # is a NUMBER the gate can see rather than a warning nobody reads.
    _battle_modifier.reset_substitute_absorb_refusals()
    if generate_instructions is None:
        stats["reconstruct_errors"] = 1
        return findings, stats

    exact_teams = None
    if teams_dir is not None:
        from fp.replay import damage_membership

        exact_teams = damage_membership.load_teams_sidecar(log_path, teams_dir)

    chunks = list(iter_chunks(log_path))
    tag = _tag_from_chunks(
        chunks, os.path.basename(log_path).split("_")[0].replace(".log", "")
    )
    fmt = _format_from_tag(tag)
    first_req = _find_first_request(chunks)
    if first_req is None:
        return findings, stats

    user_pid = first_req["side"]["id"]
    opp_pid = "p1" if user_pid == "p2" else "p2"

    battle = Battle(tag)
    battle.pokemon_format = fmt
    battle.generation = fmt[:4]
    battle.battle_type = BattleType.RANDOM_BATTLE
    # the sidecar IS the roster: switch off battle_modifier's live-play
    # "invent a Zoroark to explain this" inference (see
    # fp.battle_modifier.zoroark_inference_allowed) and resolve Illusion from
    # the sidecar instead (_infer_illusion_spans)
    battle.exact_roster_known = exact_teams is not None
    # ...and the sidecar is also what the LIVE parse must derive with wherever it
    # derives anything at all.  battle_modifier reconstructs a Substitute's
    # absorbed hits during the live walk, long before `_fire_turn` gets to apply
    # the exact sets to a snapshot, so a derivation made from live-tracking
    # GUESSES about the opponent's item/spread contradicts the very state the
    # rest of the checker replays with (synth45492 T27).  Attaching the sidecar
    # here makes the two agree by construction.
    # wrapped so the per-turn `deepcopy(battle)` snapshots ALIAS the sidecar
    # instead of copying it once per armed turn
    battle.exact_teams = (
        SharedByReference(exact_teams) if exact_teams is not None else None
    )
    if RandomBattleTeamDatasets is not None:
        try:
            RandomBattleTeamDatasets.initialize(fmt)
        except Exception:
            pass
    battle.user.name = user_pid
    battle.opponent.name = opp_pid
    battle.request_json = first_req
    try:
        battle.user.initialize_first_turn_user_from_json(first_req)
    except Exception:
        stats["reconstruct_errors"] += 1
        return findings, stats
    battle.rqid = first_req.get("rqid")
    battle.user.last_selected_move = _REPLAY_PLACEHOLDER_MOVE

    # full-log pre-pass: immutable ability / timelined item reveals, back-filled
    # into every pre-turn opponent snapshot so ability/item-gated effects (Anger
    # Shell, Flame Body, Focus Sash, ...) are reproducible for turns where the
    # randbats set is otherwise ambiguous.
    try:
        reveals = _harvest_reveals(chunks)
    except Exception:
        reveals = {"abilities": {}, "items": {}}
    # promote the sidecar-provable disguises into the same span list |replace|
    # feeds, then record which sides are still ambiguous so the damage check can
    # refuse there instead of asserting against a possibly-wrong species
    try:
        _infer_illusion_spans(reveals, exact_teams)
    except Exception:
        reveals.setdefault("illusions", [])
        reveals["illusion_unresolved"] = {}
    if exact_teams is None:
        # real-ladder logs have no sidecar to prove Illusion spans with; mark
        # the protocol-undecidable ones so the turn evaluator refuses instead
        # of asserting a species that may not be there
        try:
            _mark_protocol_illusion_ambiguity(reveals)
        except Exception:
            pass

    armed = None  # {"turn": int, "snapshot": Battle, "faints", "party_order"}
    block_lines: list[str] = []
    first_request_seen = False
    # PS `side.totalFainted` (sim/battle.ts:2551) and `side.pokemon` order
    # (permuted on every switch-in, sim/battle-actions.ts:129-131).  Both are
    # cumulative across the whole log, so they are tracked here rather than
    # rebuilt per turn; `_apply_block_to_side_state` folds one resolution block
    # in and the result is frozen into the NEXT armed turn.
    faints = {"p1": 0, "p2": 0}
    party_order = _initial_party_order(exact_teams)

    for chunk in chunks:
        for ln in chunk.split("\n"):
            if ln.startswith("|"):
                block_lines.append(ln)
        try:
            decision = update_battle(battle, chunk)
        except BaseException:
            # includes pyo3 PanicException (a BaseException): a Rust panic can
            # poison state, so abort this log's remaining reconstruction rather
            # than press on with a corrupt battle.
            stats["reconstruct_errors"] += 1
            break
        hp_certificate.set_context(turn=battle.turn)

        if not decision:
            continue

        if not first_request_seen:
            first_request_seen = True
            _apply_block_to_side_state(block_lines, faints, party_order)
            armed = {
                "turn": battle.turn,
                "snapshot": deepcopy(battle),
                "faints": dict(faints),
                "party_order": {k: list(v) for k, v in party_order.items()},
                "move_failed": _move_failed_sides(block_lines),
            }
            block_lines = []
            continue

        _fire_turn(
            armed,
            block_lines,
            battle,
            user_pid,
            findings,
            stats,
            reveals,
            exact_teams=exact_teams,
            damage_collector=damage_collector,
            damage_tolerance=damage_tolerance,
        )
        _apply_block_to_side_state(block_lines, faints, party_order)
        armed = {
            "turn": battle.turn,
            "snapshot": deepcopy(battle),
            "faints": dict(faints),
            "party_order": {k: list(v) for k, v in party_order.items()},
            # PS `moveLastTurnResult` for the turn now being armed: whether each side's
            # move FAILED in the block just consumed
            "move_failed": _move_failed_sides(block_lines),
        }
        block_lines = []

    stats["hp_certificate_refusals"] = len(hp_certificate.CERTIFICATE_REFUSALS)
    # one counter per NAMED bucket, plus the total: an absorbed-hit derivation
    # the reconstruction declined to make widens a Substitute's HP interval, and
    # a gate must be able to see how often and why.
    sub_refusals = _battle_modifier.SUBSTITUTE_ABSORB_REFUSALS
    if sub_refusals:
        stats["substitute_absorb_refusals"] = sum(sub_refusals.values())
        for reason, count in sub_refusals.items():
            stats["substitute_absorb_refused_" + reason] = count
    return findings, stats


def _initial_party_order(exact_teams) -> dict:
    """PS's `side.pokemon` at battle start: the team list, in order.

    `load_teams_sidecar` indexes each side's team into a dict in the sidecar's
    own array order, and python dicts preserve insertion order, so the keys ARE
    the party order.  Without a sidecar there is no party order to speak of and
    everything that needs one refuses."""
    if not exact_teams:
        return {"p1": [], "p2": []}
    return {pid: list((exact_teams.get(pid) or {}).keys()) for pid in ("p1", "p2")}


def _apply_block_to_side_state(block_lines, faints: dict, party_order: dict) -> None:
    """Fold one resolution block into the cumulative side state.

    faints: one |faint| line == one `side.totalFainted++` (sim/battle.ts:
    2549-2551 emits the line and increments in the same branch), and PS never
    decrements it -- a Revival Blessing revive leaves totalFainted alone.

    party_order: `switchIn` swaps the incoming mon into the active slot and
    sends the outgoing one to the incoming mon's old index
    (sim/battle-actions.ts:129-131 `side.pokemon[pokemon.position] = pokemon;
    side.pokemon[oldActive.position] = oldActive`), i.e. a plain two-element
    swap of the party array.  A |replace| is Illusion revealing itself, not a
    switch, and must NOT swap.

    A |switch| details string carries the mon's CURRENT forme
    (`Terapagos-Terastal`, `Palafin-Hero`, `Charizard-Mega-X`), while the sidecar
    is keyed by the SET species, so the two are matched through
    `resolve_party_slot`.  When even that cannot pin the details onto exactly one
    slot, the swap PS performed is unknown and the order we hold from here on is
    stale: poison the side's order with PARTY_ORDER_UNRESOLVED so Beat Up refuses
    (`ps_beatup_party_order_unresolved`) instead of silently walking a wrong
    party order and inventing per-hit base powers."""
    from fp.replay.damage_membership import (
        PARTY_ORDER_UNRESOLVED,
        _species_key,
        resolve_party_slot,
    )

    for line in block_lines:
        parts = line.split("|")
        if len(parts) < 3:
            continue
        action = parts[1]
        if action == "faint":
            pid = parts[2].split(":")[0].strip()[:2]
            if pid in faints:
                faints[pid] += 1
        elif action in ("switch", "drag") and len(parts) >= 4:
            pid = parts[2].split(":")[0].strip()[:2]
            order = party_order.get(pid)
            if not order:
                continue
            if PARTY_ORDER_UNRESOLVED in order:
                continue  # already unknown; nothing to keep in sync
            key = _species_key(parts[3].split(",")[0].strip())
            slot = resolve_party_slot(order, key)
            if slot is None:
                order.append(PARTY_ORDER_UNRESOLVED)
                continue
            idx = order.index(slot)
            order[0], order[idx] = order[idx], order[0]


# ---------------------------------------------------------------------------
# TURN EVALUATION -- engine call(s) + comparator, re-runnable (B1/B2)
# ---------------------------------------------------------------------------
# The reconstruction hands the engine ONE state, but two kinds of knowledge are
# honestly SETS of states:
#
#  * (B1) an opponent HP read off a `pct/100` display is an INTERVAL
#    (hp_certificate.display_bounds), and gate 5 proved that collapsing it to
#    ANY point estimate manufactures hard findings on whichever side of a
#    threshold the constant lands (band-top: 3, band-midpoint: 6).  So the
#    assertions are made INTERVAL-AWARE instead: the turn is first evaluated at
#    the estimator's value, and any finding that produces is then re-evaluated
#    at the OTHER values in the certified band -- it is reported only if EVERY
#    value in the band fails to reproduce the observation.  This is strictly
#    more correct and strictly more coverage than any point estimate: no
#    tolerance, no demotion, and a certified-exact HP (hp_certificate) collapses
#    the band to one value and takes the fast path untouched.
#    COST: zero on clean turns (the overwhelming majority -- one evaluation,
#    exactly as before); on a finding turn with an inexact opponent HP, at most
#    band-width - 1 extra evaluations, and the band is ~max_hp/100 wide
#    (2-4 typical, <= 8 for the biggest gen9 randbats HP stat).  Measured by
#    the `band_eval_*` counters check_replays prints.
#
#  * (B2) an opponent carrying the `unburden` volatile (its item was observed
#    leaving) whose ability NO knowledge source can resolve -- a real-ladder
#    customgame has no randbats dataset, and PS never announces Unburden -- is
#    ambiguous between a mon whose speed doubled and one whose speed did not.
#    The engine gates the doubling on ability == UNBURDEN, so the baseline
#    state alone asserts the un-doubled order as fact.  The turn is evaluated
#    under BOTH hypotheses and the branch sets unioned: an observed outcome
#    only either resolution can produce is not a fidelity breach.  (Gate-5 B2,
#    battle-gen9customgame-2651860374_beatmesilly T4: Hitmonlee's White Herb
#    was consumed on T3, its Unburden-doubled 394 outruns Pawmot's 214, and its
#    Close Combat unboosts were unreachable in the baseline's single branch.
#    That game was clean at gate 4 only because the pre-wave checker misread
#    the customgame's exact-HP displays as percents and inflated the opponent
#    to 501/224 -- a false clean this wave's interval clamp correctly removed.)


def _unburden_ability_ambiguous(battler) -> bool:
    """True when this side's active tracked an item loss into the `unburden`
    volatile (battle_modifier.remove_item does so only for species that CAN
    have Unburden) but its actual ability is still unknown after every
    knowledge fill -- the engine will only double speed when the ability says
    UNBURDEN, so the reconstruction is ambiguous between the two orders."""
    active = getattr(battler, "active", None)
    return (
        active is not None
        and not getattr(active, "ability", None)
        and "unburden" in (getattr(active, "volatile_statuses", None) or ())
    )


def _generate_turn_branches(snap, u_move, o_move, block_lines, user_pid, stats):
    """All engine branches for one turn evaluation: the baseline reconstruction
    plus (B2) the UNBURDEN-ability hypothesis when it is live, each continued
    through the deferred phase-2 replay.

    Returns `(parsed, phase2_ran, phase_split, baseline_state)`, or None when
    the baseline call cannot be built (unparseable first-use move etc.) -- the
    caller skips the turn, exactly as before."""
    hypotheses: list = [None]
    if _unburden_ability_ambiguous(snap.opponent):
        hypotheses.append("unburden")
    all_parsed: list = []
    all_splits: list[int] = []
    ran_any = False
    baseline_state = None
    for hyp in hypotheses:
        active = snap.opponent.active
        prior_ability = getattr(active, "ability", None) if active is not None else None
        if hyp is not None:
            if active is None:
                continue
            active.ability = hyp
        stage = "state_build"
        try:
            state = battle_to_poke_engine_state(snap)
            stage = "engine_call"
            branches = generate_instructions(state, u_move, o_move)
        except Exception as exc:
            # unparseable move (e.g. opponent's first-seen move not yet in
            # state) or an unsupported action shape: skip rather than false-flag
            if hyp is None:
                # bookkeeping only -- `stage` names WHICH of the two calls
                # raised, which the single shared try otherwise hides
                # the message HEAD (first three words) separates the distinct
                # engine refusals inside one exception type; the tail carries
                # the move/species name and is deliberately dropped so the key
                # space stays bounded (the full text is in the samples below)
                _bump(
                    stats,
                    "skipsub_build_%s_%s_%s"
                    % (
                        stage,
                        _stat_token(type(exc).__name__),
                        _stat_token(" ".join(str(exc).split()[:3])),
                    ),
                )
                _skip_sample(
                    "build/" + stage,
                    "%s: %s" % (type(exc).__name__, str(exc)[:160]),
                )
                return None
            continue
        finally:
            if hyp is not None and active is not None:
                active.ability = prior_ability
        if baseline_state is None:
            baseline_state = state
        parsed = parse_branches(branches)
        ran = False
        split = None
        # the ALTERNATE hypothesis' phase-2 bookkeeping goes to a scratch dict
        # so the phase2_* coverage counters keep meaning "per checked turn"
        hyp_stats = stats if hyp is None else {}
        try:
            parsed, ran, split = _replay_deferred_phase(
                state, branches, parsed, block_lines, user_pid, hyp_stats
            )
        except BaseException:
            hyp_stats["phase2_errors"] = hyp_stats.get("phase2_errors", 0) + 1
        if hyp is not None:
            stats["unburden_hypothesis_turns"] = (
                stats.get("unburden_hypothesis_turns", 0) + 1
            )
        all_parsed.extend(parsed)
        all_splits.extend(split if split is not None else [len(b) for b in parsed])
        ran_any = ran_any or ran
    return all_parsed, ran_any, (all_splits if ran_any else None), baseline_state


def _evaluate_turn(
    snap,
    turn,
    u_move,
    o_move,
    u_action,
    o_action,
    observed,
    block_lines,
    user_pid,
    stats,
):
    """One full evaluation of the turn from `snap`'s current state: engine
    branches, comparator, KO-margin demotion.  Returns `(turn_findings, parsed,
    baseline_state)` or None when the engine call cannot be built.

    Deliberately re-runnable: the B1 band re-evaluation calls it again with the
    opponent's HP moved to another in-band value."""
    generated = _generate_turn_branches(
        snap, u_move, o_move, block_lines, user_pid, stats
    )
    if generated is None:
        return None
    parsed, phase2_ran, phase_split, state = generated
    # protocol-side counterpart of `phase_split`: everything from the block's
    # second `|t:|` on belongs to phase 2.  Only meaningful when phase 2
    # actually ran; without it the comparator matches the whole union as before.
    phase2_line_index = _second_t_boundary(block_lines) if phase2_ran else None
    # volatiles each side ALREADY holds at turn start: a protocol `-start` for
    # one of these is a PS onRestart re-emission with no engine state change, so
    # the comparator must not demand an ApplyVolatileStatus for it (SideOne==user).
    pre_volatiles: dict = {}
    try:
        pre_volatiles = {
            "s1": {_norm(v) for v in state.side_one.volatile_statuses},
            "s2": {_norm(v) for v in state.side_two.volatile_statuses},
        }
    except Exception:
        pre_volatiles = {}
    # turn-start max HP of each side's active: lets the immunity check size an
    # end-of-turn burn/poison chip out of the damage profile
    side_maxhp: dict = {}
    side_hp: dict = {}
    try:
        side_maxhp = {
            "s1": state.side_one.pokemon[_index_int(state.side_one.active_index)].maxhp,
            "s2": state.side_two.pokemon[_index_int(state.side_two.active_index)].maxhp,
        }
        side_hp = {
            "s1": state.side_one.pokemon[_index_int(state.side_one.active_index)].hp,
            "s2": state.side_two.pokemon[_index_int(state.side_two.active_index)].hp,
        }
    except Exception:
        side_maxhp = {}
        side_hp = {}
    ctx = TurnContext(
        turn=turn,
        branches=parsed,
        observed=observed,
        user_move=u_move,
        opp_move=o_move,
        pre_volatiles=pre_volatiles,
        side_maxhp=side_maxhp,
        side_hp=side_hp,
        phase_split=phase_split,
        phase2_line_index=phase2_line_index,
    )
    turn_findings = compare_turn(ctx, user_is_side_one=True)
    if turn_findings:
        # a marginal KO-boundary disagreement makes every faint-gated companion
        # event on this turn undecidable (see _KO_MARGIN_HP): report SOFT
        try:
            _demote_ko_margin_findings(
                turn_findings,
                parsed,
                snap,
                u_action,
                o_action,
                block_lines,
                user_pid,
                stats,
            )
        except Exception:
            pass
    return turn_findings, parsed, state


# Bands are enumerated exhaustively up to this width; a `pct/100` band is
# ~max_hp/100 wide, so every gen9 randbats mon (max HP <= ~720) enumerates
# fully.  The fallback below is defensive only.
_BAND_ENUM_MAX = 16


def _band_candidate_values(pkmn, parsed) -> list[int]:
    """The OTHER pre-turn HP values inside the protocol-certified display band
    of the opponent mon this turn's branch damage lands on (B1).

    Fast paths to [] -- no re-evaluation at all -- when the HP is certified
    exact (the band is one value), when there is no display to derive a band
    from, or when something other than the display set the current HP (a
    Substitute-[weak] clamp, say): the band then no longer describes the value
    and re-deriving one would assert HPs nothing certified."""
    if pkmn is None or hp_certificate.is_exact(pkmn):
        return []
    pct = getattr(pkmn, "hp_display_pct", None)
    disp_hp = getattr(pkmn, "hp_display_hp", None)
    if pct is None or disp_hp is None or int(pkmn.hp) != int(disp_hp):
        return []
    max_hp = int(getattr(pkmn, "max_hp", 0) or 0)
    lo, hi = hp_certificate.display_bounds(pct, max_hp)
    cur = int(pkmn.hp)
    if hi <= lo or not (lo <= cur <= hi):
        return []
    if hi - lo + 1 <= _BAND_ENUM_MAX:
        return [v for v in range(lo, hi + 1) if v != cur]
    # Defensive fallback for a pathologically wide band (max HP > ~1600 --
    # impossible in gen9 randbats): the endpoints, plus every in-band value
    # where a categorical outcome can flip -- damage-vs-HP lethality (each
    # branch's running damage total against this mon, and the value one above
    # it), and the berry / Substitute fractional-HP gates.  Endeavor's >=
    # comparison is subsumed: a landed Endeavor's Damage is itself one of the
    # running totals.
    cands = {lo, hi, (lo + hi) // 2}
    for gate in (max_hp // 4, max_hp // 2):
        cands.update((gate, gate + 1))
    for b in parsed:
        total = 0
        for i in b:
            if i.side != "s2":
                continue
            amt = i.amount() or 0
            if i.kind == "Damage" and amt > 0:
                total += amt
                cands.update((total, total + 1))
            elif i.kind == "Heal":
                total -= amt
    return sorted(v for v in cands if lo <= v <= hi and v != cur)


def _substitute_band_values(pkmn, parsed, side_key) -> list[int]:
    """The OTHER Substitute HPs inside `pkmn`'s tracked interval (B3).

    A Substitute's remaining HP is protocol-invisible once anything has hit it
    (`-activate ... move: Substitute|[damage]` carries no magnitude), so
    fp.battle_modifier tracks it as [substitute_health_low, substitute_health]
    and the assertions have to be made over the whole interval for exactly the
    reason B1's HP band exists: the single number the state hands the engine is
    one draw from a 16-roll distribution, and whichever side of a break
    threshold it lands on decides the turn's outcome (synth45492 T27: a
    Rapid Spin that deals 12 or 18 breaks a 8-19 HP substitute at some values
    and not at others, and the observed `|-end|...|Substitute` is only
    unreproducible at the top of the interval).

    Fast path to [] when the interval is a singleton -- a freshly created
    substitute, or a mon without one -- so clean turns pay nothing."""
    if pkmn is None or _control("FP_CONTROL_NO_SUBSTITUTE_BAND"):
        # NEGATIVE CONTROL: assert against the single tracked value only, the
        # way the checker did before this axis existed.  Gates the B3 axis and
        # nothing else -- the B1 HP band is untouched.
        return []
    hi = int(getattr(pkmn, "substitute_health", 0) or 0)
    if hi <= 0 or constants.SUBSTITUTE not in pkmn.volatile_statuses:
        return []
    lo = int(getattr(pkmn, "substitute_health_low", 0) or 0)
    if lo <= 0 or lo > hi:
        return []
    if hi - lo + 1 <= _BAND_ENUM_MAX:
        return [v for v in range(lo, hi + 1) if v != hi]
    # Defensive fallback for a wide interval (a REFUSED derivation widens it to
    # [1, prior]): the endpoints plus every value where the categorical outcome
    # can flip -- the running `DamageSubstitute` total against this side in each
    # branch, and the value one above it, which is the break threshold.
    cands = {lo, hi, (lo + hi) // 2}
    for b in parsed:
        total = 0
        for i in b:
            if i.side != side_key or i.kind != "DamageSubstitute":
                continue
            total += i.amount() or 0
            cands.update((total, total + 1))
    return sorted(v for v in cands if lo <= v <= hi and v != hi)


# A finding turn re-evaluates the product of its open bands.  Both are small in
# practice (an HP display band is ~max_hp/100 wide; a substitute interval is one
# damage roll spread), but the product is capped so a pathological pair cannot
# turn one finding turn into hundreds of engine calls.  Beyond the cap the axes
# are walked one at a time, which is strictly less coverage -- it can leave a
# finding in that some OFF-AXIS combination would have reproduced -- and never
# less honest.
_BAND_PRODUCT_MAX = 96


def _band_assignments(axes) -> list[tuple]:
    """Every alternative assignment to re-evaluate, as a tuple of
    (obj, attr, value) triples.  `axes` is a list of (obj, attr, cur, values)."""
    axes = [a for a in axes if a[3]]
    if not axes:
        return []
    product = 1
    for _obj, _attr, _cur, values in axes:
        product *= len(values) + 1
    out: list[tuple] = []
    if product - 1 <= _BAND_PRODUCT_MAX:
        combos = [()]
        for obj, attr, cur, values in axes:
            combos = [
                c + ((obj, attr, v),) for c in combos for v in ([cur] + list(values))
            ]
        cur_point = tuple((obj, attr, cur) for obj, attr, cur, _v in axes)
        out = [c for c in combos if c != cur_point]
    else:
        for obj, attr, _cur, values in axes:
            out.extend(((obj, attr, v),) for v in values)
    return out


def _finding_key(f) -> tuple:
    """Identity of a finding across band re-evaluations.  The KO-margin
    demotion appends to the message, so severity decorations are stripped --
    the same defect must compare equal whether or not a given evaluation
    demoted it."""
    return (f.category, f.observed, f.message.replace(" [ko-margin]", ""))


# ---------------------------------------------------------------------------
# EXISTENTIAL MEMBERSHIP ASSERT over an unnamable side's LEGAL ACTION SET
# ---------------------------------------------------------------------------
# `generate_instructions(state, s1_move, s2_move)` needs BOTH arguments.  When a
# side produced no EXECUTED action -- it was PREVENTED (|cant| slp/par/flinch/
# recharge/truant, or a confusion self-hit which carries no |cant| at all), KO'd
# before acting, phazed, or it already acted in the OTHER half of a turn the
# protocol split at a forced replacement -- its argument cannot be named, and the
# checker used to refuse the whole turn.  That refusal was 97.35% of every skip.
#
# It is replaced by the same discipline this codebase already uses for damage
# rolls (HANDOFF rule 10 -- assert MEMBERSHIP of a set, never a point estimate):
#
#   enumerate the unnamable side's legal action set, replay ONE engine call per
#   candidate, and
#     * at least one candidate reproduces the observation  -> the turn PASSES
#     * NO candidate reproduces it                          -> HARD finding
#
# "Reproduces the observation" == that candidate's engine branch set, run through
# the ordinary comparator against the ordinary `observed` event list, yields zero
# HARD findings.  Nothing about the assert is new; only the argument it is run
# with is quantified.
#
# THE SET IS DELIBERATELY OVER-APPROXIMATED.  Every move in the reconstructed
# moveset is a candidate regardless of PP or `disabled`, because an
# UNDER-approximation that omitted the true action would make "no candidate
# reproduces" fire on a correct engine -- a FALSE HARD finding, the one outcome
# that must not happen.  A superset only weakens the assert, and the weakening is
# measured and reported per turn (see `_membership_class`).
#
# STRENGTH IS PART OF THE RESULT, NOT A FOOTNOTE.  Membership over a large set
# has less power to detect a defect: with 81 candidates some candidate may
# reproduce the observation by coincidence.  Every membership turn therefore
# records (candidates that reproduce)/(candidates tried) and is classified:
#   point   -- the set has exactly ONE member; this is a point assert, not a
#              membership one (the split-half case, where the other side's
#              action is not unknown but already observed in the previous block)
#   full    -- >1 tried, exactly 1 reproduces: as sharp as a point assert
#   partial -- >1 reproduce but not all
#   vacuous -- EVERY candidate reproduces: the check decided nothing this turn
# A vacuous check is NOT coverage and is never folded into a coverage headline.
_MEMBERSHIP_PAIR_CAP = 200  # engine calls per turn; bounded turns are COUNTED

# Every turn where NO legal action reproduced the observation, recorded by
# identity.  A `fail` that is only COUNTED is unadjudicable, and the B1/B3 band
# re-evaluation downstream can legitimately drop its findings -- which would
# otherwise make the fail invisible in the finding list (HANDOFF rule 20).
MEMBERSHIP_FAIL_SAMPLES: list = []


def _membership_off() -> bool:
    """DEFAULT OFF as of 2026-07-28 (owner decision).  Opt in with
    `FP_MEMBERSHIP_REPLAY=1`; `FP_CONTROL_NO_MEMBERSHIP_REPLAY=1` still forces it
    off and still gates exactly this one mechanism (rule 18).

    WHY IT IS OFF.  Measured on 400 corpus games, the existential assert took
    skipped turns 4,477 -> 33, which reads as coverage 73.4% -> 99.8%.  That
    headline is hollow: **92.73% of the recovered turns are `vacuous`** -- every
    candidate reproduces, so the turn decides nothing.  Proven by perturbation
    with a working positive control: a `|-boost|` PS never emitted raises 1 HARD
    finding on a normal two-move turn and **0** on a membership-recovered turn,
    on a fainting mon and on a live paralysed one alike.

    The pre-wave code SKIPPED those turns, so leaving this on does not lose
    correctness -- it converts an honest refusal into a vacuous pass, and a
    skipped turn is visibly unchecked while a vacuously-passed one is not.  That
    is the whole reason for the default.

    NOT DIAGNOSED, and required before this is turned back on: whether the
    blindness is the candidate set being rich enough to explain any observation
    (fundamental to the approach) or the comparison never examining the unnamable
    side's events (a plain bug, and fixable).  Probes: scratchpad/perturbed*.log.

    IF YOU OPT IN: `turns checked` in the summary WILL include vacuous turns.
    Read the membership strength table underneath it, never the headline alone.
    """
    if os.environ.get("FP_CONTROL_NO_MEMBERSHIP_REPLAY"):
        return True
    return not os.environ.get("FP_MEMBERSHIP_REPLAY")


def _legal_action_strings(state, side_key: str, tera: bool) -> list[str]:
    """OVER-approximated legal action set for one side, as `MoveChoice::from_string`
    strings, read off the very engine state the replay will use.

    Derivation: every move id in the active's reconstructed 4-slot moveset, plus
    every non-active party member with hp > 0 (a switch is named by bare species
    id -- genx/state.rs:118-125).  Struggle is added only when the moveset is
    empty, which is exactly when PS offers it (sim/pokemon.ts:1109-1112).

    Deliberately NOT filtered by PP or `disabled`: those are reconstruction
    products, and dropping a candidate the reconstruction got wrong could make
    "no candidate reproduces" fire on a correct engine.  Over-approximation is
    sound; under-approximation manufactures hard findings.

    When the block shows this side TERASTALLIZING, every candidate carries the
    `-tera` suffix and switches are dropped -- a side cannot both tera and switch,
    and the tera is directly observed, so this narrows the set on EVIDENCE rather
    than assumption."""
    side = state.side_one if side_key == "s1" else state.side_two
    active_i = _index_int(side.active_index)
    out: list[str] = []
    try:
        active = side.pokemon[active_i]
    except Exception:
        return []
    for mv in active.moves:
        mid = str(mv.id).lower()
        if not mid or mid == "none":
            continue
        out.append(mid + "-tera" if tera else mid)
    if not out:
        out.append("struggle")
    if not tera:
        for i, p in enumerate(side.pokemon):
            if i == active_i:
                continue
            try:
                if p.hp > 0:
                    out.append(str(p.id).lower())
            except Exception:
                continue
    return sorted(set(out))


def _membership_bound(u_cands: list, o_cands: list, cap: int):
    """Bound the candidate PRODUCT to `cap` engine calls.  Returns
    `(u_cands, o_cands, bounded)`.  The smaller axis is kept whole and the larger
    is subsampled on an EVEN STRIDE (never a prefix, which would bias toward one
    end of the sorted id space).  `bounded` is returned so the caller can COUNT
    these turns separately -- a silent cap reads as "covered everything" when it
    did not (HANDOFF rule 19)."""
    if len(u_cands) * len(o_cands) <= cap:
        return u_cands, o_cands, False
    small, large = (u_cands, o_cands) if len(u_cands) <= len(o_cands) else (o_cands, u_cands)
    keep = max(1, cap // max(1, len(small)))
    if keep < len(large):
        if keep == 1:
            idx = [0]
        else:
            span = (len(large) - 1) / float(keep - 1)
            idx = sorted({min(len(large) - 1, int(round(i * span)))
                          for i in range(keep)})
        large = [large[i] for i in idx]
    if len(u_cands) <= len(o_cands):
        return small, large, True
    return large, small, True


def _membership_class(reproducing: int, tried: int, any_observed: bool = True) -> str:
    """`any_observed` False means the block carried NO categorical event at all,
    so no candidate could have failed and the pass is worth nothing -- vacuous,
    whatever the candidate ratio says.  Calling such a turn `point` because its
    legal set had one member would dress an empty assert as the sharpest kind."""
    if not any_observed:
        return "vacuous"
    if reproducing == 0:
        return "fail"
    if tried <= 1:
        return "point"
    if reproducing == 1:
        return "full"
    if reproducing >= tried:
        return "vacuous"
    return "partial"


def _membership_evaluate(
    snap,
    turn,
    u_move,
    o_move,
    u_action,
    o_action,
    observed,
    block_lines,
    user_pid,
    stats,
    u_cand_spec,
    o_cand_spec,
    class_key,
    authoritative=True,
):
    """Run the existential membership assert for one turn.

    `*_cand_spec` is None for a side whose action IS named (its single candidate
    is the named move), the literal `["none"]` for a side the protocol shows has
    ALREADY ACTED in the other half of a split turn (`MoveChoice::None`,
    genx/state.rs:110-112 -- a real engine option, so this is an exact point
    assert, not an approximation), or the string "legal" to enumerate.

    `authoritative` is False when at least one ENUMERATED side's legal set is
    only an under-approximation (an opponent moveset on a log with no
    full-knowledge sidecar).  A `fail` there is then reported SOFT, because it
    may be the set that is incomplete rather than the model that is wrong.

    Returns `(evaluated, chosen_u, chosen_o)` where `evaluated` is exactly what
    `_evaluate_turn` returns for the CHOSEN candidate pair, with its finding list
    already reduced to the findings that survive membership; or None when no
    candidate could even be built (the caller then refuses, named)."""
    try:
        enum_state = battle_to_poke_engine_state(snap)
    except Exception as exc:
        _bump(stats, "skipsub_membership_enum_%s" % _stat_token(type(exc).__name__))
        return None
    tera_sides = _detect_tera_sides(block_lines, user_pid)

    # The engine runs NO end-of-turn phase when either side is force-switching:
    # `run_end_of_turn = !skip_end_of_turn && !battle_decided && !(force_switch
    # || force_switch)` (genx/generate_instructions.rs:11778-11780).  PS, by
    # contrast, runs the game turn's residual phase and -- when the request that
    # cut the block came mid-turn -- prints it in THIS block.  Asserting those
    # residuals against a call the engine deliberately runs without a residual
    # phase reports events no branch can ever produce.  `_eot_skipped_by_faint_
    # replacement` already drops them for the case it can see (a side whose
    # NAMED action is a switch and whose active is fainted); it cannot see this
    # one, because on a membership turn the side has no named action at all.
    # VALIDATED BY EXECUTION, not by reading: synth00304 T10's reconstructed
    # state returns `[[]]` -- a single branch with an empty instruction list --
    # from `generate_instructions(state, "none", "none")`, while the same call on
    # a state with force_switch clear returns the end-of-turn chip.
    # Scoped to membership turns so no previously-checked turn changes verdict.
    try:
        eot_suppressed = bool(
            enum_state.side_one.force_switch or enum_state.side_two.force_switch
        )
    except Exception:
        eot_suppressed = False
    if eot_suppressed:
        cut = _residual_phase_start(block_lines)
        if cut is not None:
            kept = [
                e for e in observed if e.line_index is None or e.line_index < cut
            ]
            if len(kept) != len(observed):
                stats["membership_eot_dropped_force_switch"] = (
                    stats.get("membership_eot_dropped_force_switch", 0)
                    + (len(observed) - len(kept))
                )
                observed = kept

    def _cands(spec, side_key, side_name, named):
        if spec is None:
            return [named]
        if spec == "none":
            return ["none"]
        return _legal_action_strings(enum_state, side_key, side_name in tera_sides)

    u_cands = _cands(u_cand_spec, "s1", "user", u_move)
    o_cands = _cands(o_cand_spec, "s2", "opp", o_move)
    if not u_cands or not o_cands:
        _bump(stats, "skipsub_membership_empty_legal_set")
        return None
    u_cands, o_cands, bounded = _membership_bound(
        u_cands, o_cands, _MEMBERSHIP_PAIR_CAP
    )
    if bounded:
        _bump(stats, "membership_bounded_turns")

    reproducing: list = []
    best = None
    tried = 0
    unbuildable = 0
    any_instructions = False
    for a in u_cands:
        for b in o_cands:
            ev = _evaluate_turn(
                snap, turn, a, b, u_action, o_action, observed, block_lines,
                user_pid, {},
            )
            if ev is None:
                unbuildable += 1
                continue
            tried += 1
            if any(len(br) for br in (ev[1] or [])):
                any_instructions = True
            f = ev[0]
            n_hard = sum(1 for x in f if x.severity is Severity.HARD)
            if n_hard == 0:
                reproducing.append((a, b, ev))
            rank = (n_hard, len(f))
            if best is None or rank < best[0]:
                best = (rank, a, b, ev)
    if tried == 0:
        _bump(stats, "skipsub_membership_all_unbuildable")
        return None
    if observed and not any_instructions:
        # DEGENERATE CALL -- refuse rather than assert.  The engine short-circuits
        # a turn in which a side it believes must switch (`force_switch`) is
        # handed `MoveChoice::None`: PROVEN BY EXECUTION, not by reading --
        # `generate_instructions(state, "none", "rest")` on the reconstructed
        # state of battle-...-2653919147 T23 (side_one.force_switch true) returns
        # ONE branch with an EMPTY instruction list, so the opponent's Rest is
        # never simulated and its `|-status|slp` + heal look like engine defects.
        # A block whose every candidate call produces no instruction at all
        # cannot reproduce ANY observed event, so a "fail" there measures the
        # MODEL, not the engine.  Refuse it under its own name (rule 4).
        # Deliberately keyed on the executed result, not on a predicate about
        # force_switch: whatever makes the call empty, the conclusion is the same.
        _bump(stats, "skipsub_membership_degenerate_" + class_key)
        return "degenerate"
    stats["membership_candidates_tried"] = (
        stats.get("membership_candidates_tried", 0) + tried
    )
    if unbuildable:
        stats["membership_candidates_unbuildable"] = (
            stats.get("membership_candidates_unbuildable", 0) + unbuildable
        )
    cls = _membership_class(len(reproducing), tried, bool(observed))
    if not observed:
        # nothing categorical was observed this block, so no candidate could have
        # failed and the pass carries no information at all.  Distinguished from
        # a vacuum caused by candidates being indistinguishable ON a real
        # observation -- lumping the two would overstate what the check saw.
        _bump(stats, "membership_zero_observed")
    _bump(stats, "membership_class_" + cls)
    _bump(stats, "membershipby_%s_%s" % (cls, class_key))
    stats["membership_turns"] = stats.get("membership_turns", 0) + 1

    if not reproducing:
        # NO legal action explains what happened: the model cannot reproduce the
        # observation under ANY decision the side could have made.  Report the
        # least-bad candidate's findings, HARD, tagged with the strength so the
        # row is adjudicable without re-running.
        _rank, a, b, ev = best
        tag = " [membership 0/%d legal actions reproduce]" % tried
        if not authoritative:
            # THE LEGAL SET IS AN UNDER-APPROXIMATION HERE, so "no candidate
            # reproduces" may simply mean the true action was never a candidate.
            # An opponent's moveset on a real-ladder log is only what the
            # protocol has revealed so far -- battle-...-2651908107 T14's
            # Toucannon armed Beak Blast (`|-singleturn|... move: Beak Blast`)
            # and fainted before its |move| line, so the move it actually chose
            # was not in the 4 reconstructed slots at all.  Under-approximation
            # is the one failure mode that manufactures HARD findings, so a fail
            # over a non-authoritative set is reported SOFT and named.  The
            # synthetic corpus is unaffected: its sidecar makes both sides' sets
            # exact, which is why the gate keeps its sharpness.
            tag += " [legal-set NOT authoritative: under-approximated]"
            _bump(stats, "membership_fail_nonauthoritative_set")
            for f in ev[0]:
                if f.severity is Severity.HARD:
                    f.severity = Severity.SOFT
        for f in ev[0]:
            f.message += tag
        MEMBERSHIP_FAIL_SAMPLES.append(
            (
                hp_certificate.CONTEXT.get("game") or "?",
                turn,
                class_key,
                tried,
                "%s|%s" % (a, b),
                "; ".join(sorted({f.category for f in ev[0]})) or "(no finding)",
            )
        )
        return ev, a, b

    # PASS.  A finding is reported only if EVERY reproducing candidate produces
    # it -- the same rule the B1/B3 band re-evaluation uses (rule 10): a finding
    # absent under some candidate the protocol admits is not decidable.
    a, b, ev = reproducing[0]
    keys = None
    for _a, _b, alt in reproducing:
        k = {_finding_key(f) for f in alt[0]}
        keys = k if keys is None else (keys & k)
        if not keys:
            break
    tag = " [membership %d/%d %s]" % (len(reproducing), tried, cls)
    kept = []
    for f in ev[0]:
        if _finding_key(f) in (keys or set()):
            f.message += tag
            kept.append(f)
    return (kept, ev[1], ev[2]), a, b


def _fire_turn(
    armed,
    block_lines,
    battle,
    user_pid,
    findings,
    stats,
    reveals=None,
    exact_teams=None,
    damage_collector=None,
    damage_tolerance=0,
):
    turn = armed["turn"]
    snap = armed["snapshot"]
    opp_pid = "p1" if user_pid == "p2" else "p2"
    # refusals raised while re-deriving THIS turn (the deferred certificate
    # check inside apply_exact_teams) belong to this turn, not to wherever the
    # live parse currently is
    hp_certificate.set_context(turn=turn)

    if snap.user.active is None or snap.opponent.active is None:
        stats["turns_skipped"] += 1
        _bump(stats, "skipped_no_active")
        if snap.user.active is None and snap.opponent.active is None:
            _bump(stats, "skipsub_no_active_both")
        elif snap.user.active is None:
            _bump(stats, "skipsub_no_active_user")
        else:
            _bump(stats, "skipsub_no_active_opp")
        return

    # actions read from the block itself (robust to forced-replacement clobber)
    u_action = _extract_side_action(block_lines, user_pid + "a")
    o_action = _extract_side_action(block_lines, opp_pid + "a")
    tera_sides = _detect_tera_sides(block_lines, user_pid)
    u_move = _action_to_move_string(u_action, "user", tera_sides)
    o_move = _action_to_move_string(o_action, "opp", tera_sides)
    # a switch-in the log later (or in this same turn) |replace|s was the Illusion bearer
    # wearing that species' face -- switch in the real mon, not the disguise
    if reveals is not None:
        if u_action is not None and u_action[0] == "switch":
            u_move = _illusion_switch_target(reveals, user_pid, turn, u_move)
        if o_action is not None and o_action[0] == "switch":
            o_move = _illusion_switch_target(reveals, opp_pid, turn, o_move)
    # Phase 2: a side whose action the protocol never names is asserted by
    # MEMBERSHIP of its legal action set instead of refusing the turn.  `plan`
    # carries, per side, how that side's candidate list is built (see
    # _membership_evaluate); None means every action was named and the turn runs
    # exactly as before.
    plan = None
    if u_move is None or o_move is None:
        reasons, slots = [], []
        if u_move is None:
            slots.append(user_pid + "a")
            reasons.append(_unnamable_action_reason(block_lines, slots[-1]))
        if o_move is None:
            slots.append(opp_pid + "a")
            reasons.append(_unnamable_action_reason(block_lines, slots[-1]))
        # one key per turn (reasons sorted so a two-sided turn lands in a single
        # bucket regardless of which side is which) -- these sum to the parent
        # a two-sided turn is exactly a key containing "+", so the number of
        # both-sides-unnamable turns stays readable off the same buckets.
        # A "noaction" reason is qualified by the block's fragment kind: without
        # it the protocol-segmentation cases and the genuinely-actionless ones
        # collapse into one uninterpretable residual.
        key = "+".join(sorted(reasons))
        frag = _turn_fragment_kind(snap, block_lines)
        if any(r in _NO_ACTION_LINE_FAMILY for r in reasons):
            key += "@" + frag
        for r, slot in zip(reasons, slots):
            if r.startswith("noaction") or r.startswith("named_"):
                _skip_sample(
                    "unnamable/" + r + "@" + frag,
                    "T%s slot=%s | %s"
                    % (
                        turn,
                        slot,
                        " ".join(x.strip() for x in block_lines[:5])[:220],
                    ),
                )
        if _membership_off():
            # NEGATIVE CONTROL: the pre-Phase-2 blanket refusal, and only that.
            stats["turns_skipped"] += 1
            _bump(stats, "skipped_unnamable_action")
            _bump(stats, "skipsub_unnamable_" + key)
            return
        # A side whose reason is "no action line" in a block the protocol cut at
        # a forced replacement did not fail to act -- it acted in the OTHER half,
        # and that action is already observed there.  Its candidate set is the
        # single engine option `none`, which makes the turn an exact POINT
        # assert.  Enumerating that side's moves here would be enumerating over an
        # action already seen: a wide, low-discrimination membership check where
        # a sharp one is available.
        def _spec(reason: str):
            if reason.startswith("noaction") and frag in (
                "split_2nd_half",
                "split_middle",
            ):
                return "none"
            return "legal"

        plan = {"class": key, "u": None, "o": None}
        i = 0
        if u_move is None:
            plan["u"] = _spec(reasons[i])
            i += 1
        if o_move is None:
            plan["o"] = _spec(reasons[i])

    # refresh the USER's team (exact moves/hp/stats) from the request that gated
    # this turn -- exactly what async_pick_move does live before searching. Without
    # it the active carries the moveset from its switch-in line (no moves), and our
    # own move strings fail to parse.
    if snap.request_json:
        try:
            snap.user.update_from_request_json(snap.request_json)
        except Exception:
            pass
        # the request's 6-mon party list is the SERVER's authoritative roster for
        # the user side; live tracking can leave a stale duplicate behind when one
        # mon appears under two forme names across its lifetime (synth26245:
        # Terapagos was stored as `terapagosterastal` 273/273 on switch-out and
        # re-created as `terapagos` from a later switch-in, so the fainted mon's
        # side still counted one phantom ALIVE reserve -- which armed Parting
        # Shot's self-switch in the engine while the real, benchless side stayed
        # in and Curse resolved the same turn).  Drop reserve rows the request
        # does not list whose base-species family the request DOES cover.
        try:
            _prune_stale_forme_duplicates(snap.user, snap.request_json)
        except Exception:
            pass
    # roster fill FIRST: a mon making its first appearance during this turn is
    # not on the snapshot's roster yet, and both the exact-team application and
    # the randbats inference below only touch mons already there -- without this
    # the debut mon reaches the engine with no ability, no item and default-EV
    # stats on a FULL-KNOWLEDGE corpus (see _backfill_roster).  Idempotent, and
    # _backfill_revealed_knowledge still calls it so its own ordering invariant
    # holds regardless of this call.
    if reveals is not None:
        try:
            _backfill_roster(snap.opponent, opp_pid, reveals)
        except Exception:
            pass
    # exact full-knowledge sets (synthetic corpus sidecar) BEFORE the randbats
    # inference heuristics: ground truth fills unknowns, heuristics only touch
    # whatever remains unknown
    if exact_teams is not None:
        from fp.replay import damage_membership

        try:
            damage_membership.apply_exact_teams(snap, user_pid, exact_teams)
        except Exception:
            pass
    try:
        _populate_opponent_knowledge(snap.opponent)
    except Exception:
        pass

    # authoritative full-log reveals override the ambiguous-set inference above
    # (immutable ability; item respecting its removal timeline)
    if reveals is not None:
        try:
            _backfill_revealed_knowledge(snap.opponent, opp_pid, reveals, turn)
        except Exception:
            pass
        # the tera-type reveal is a fixed set property but update_from_request_json
        # does not carry it for the user's own mons, so apply it here (narrow to
        # tera only -- the user's ability/item/roster are authoritative already)
        try:
            _backfill_user_tera(snap.user, user_pid, reveals)
        except Exception:
            pass
        # `|-terastallize|` names a SLOT, and the occupant is rendered under the
        # ILLUSION's name (or a stale forme name), so both backfills above can credit
        # the tera to the wrong pokemon. Re-resolve it from the slot occupancy, before
        # _apply_illusion so the illusion pass (which derives the types from the same
        # bearer_tera) has the last word.
        for _bt, _pid in ((snap.user, user_pid), (snap.opponent, opp_pid)):
            try:
                _apply_slot_tera(_bt, _pid, reveals, turn)
            except Exception:
                pass
        # a disguised Zoroark is reconstructed with the disguise's types on EITHER
        # side (the |switch| line shows the disguise even to its own owner);
        # substitute Zoroark's real types so type checks resolve
        for _bt, _pid in ((snap.user, user_pid), (snap.opponent, opp_pid)):
            try:
                _apply_illusion(_bt, _pid, reveals, turn)
            except Exception:
                pass
        # sleep attempts the bearer served under someone else's name (slot-counted)
        for _bt, _pid in ((snap.user, user_pid), (snap.opponent, opp_pid)):
            try:
                _apply_illusion_sleep_counter(_bt, _pid, reveals, turn)
            except Exception:
                pass
        # Cud Chew's next-turn re-eat is armed by a berry eaten LAST turn -- a
        # cross-turn fact only the full-log harvest carries.  Both sides: the
        # user's own request json reports no volatiles either.
        for _bt, _pid in ((snap.user, user_pid), (snap.opponent, opp_pid)):
            try:
                _arm_cudchew(_bt, _pid, reveals, turn)
            except Exception:
                pass

    # forme-linked abilities no knowledge source reports (Terapagos-Terastal's
    # Tera Shell); runs after every fill so it wins over the base-forme ability,
    # and before the damage-membership call so both consumers see the same mon.
    for _battler in (snap.user, snap.opponent):
        try:
            _apply_forme_abilities(_battler)
        except Exception:
            pass

    # a move the block shows being USED had >=1 PP at turn start (Leppa / drift)
    try:
        _clamp_used_move_pp(block_lines, snap, user_pid)
    except Exception:
        pass

    # HP inequalities the protocol certifies outright (Substitute's [weak] fail) are
    # free constraints on the quantized percent-HP reconstruction
    try:
        _clamp_hp_from_protocol_certificates(block_lines, snap, user_pid)
    except Exception:
        pass

    # PS `moveLastTurnResult`: harvested from the PREVIOUS block (see
    # _move_failed_sides).  Nothing else can set it -- the engine only ever raises the
    # flag from a move failing inside a turn it simulates, which a one-turn preview call
    # never sees, so Stomping Tantrum / Temper Flare could not double in ANY replayed
    # turn (synth19386 T19: the doubled Temper Flare that KO'd Arbok 246 and fired Moxie).
    move_failed = armed.get("move_failed") or {}
    snap.user.last_move_failed = bool(move_failed.get(user_pid))
    snap.opponent.last_move_failed = bool(move_failed.get(opp_pid))

    # exact-damage membership (synthetic corpus only): runs BEFORE the
    # categorical generate_instructions gate so first-use-move parse skips do
    # not cost damage coverage.  calculate_damage builds its Choice from the
    # move id directly and needs no moveset parse.
    # NOT run on action-membership turns: `run_for_turn` consumes u_action /
    # o_action, and on these turns at least one of them is None.  Keeping it off
    # holds every damage_* counter byte-identical to the pre-Phase-2 run, so the
    # whole coverage delta is attributable to the categorical membership assert
    # and nothing else.  Extending damage membership to these turns is a stated
    # deferred item, not a silent omission.
    if exact_teams is not None and plan is None:
        from fp.replay import damage_membership

        try:
            d_records, d_counters, d_findings = damage_membership.run_for_turn(
                snap,
                block_lines,
                user_pid,
                turn,
                u_action,
                o_action,
                tera_sides,
                reveals,
                tolerance=damage_tolerance,
                prior_faints=armed.get("faints"),
                party_order=armed.get("party_order"),
            )
        except BaseException:
            stats["damage_turn_errors"] = stats.get("damage_turn_errors", 0) + 1
        else:
            for k, v in d_counters.items():
                key = k if k.startswith("damage_") else "damage_" + k
                stats[key] = stats.get(key, 0) + v
            findings.extend(d_findings)
            if damage_collector is not None:
                damage_collector.extend(d_records)

    # An Encore that landed before its target's own |move| line makes that side's
    # CHOSEN action unknowable -- the shown move is the Encore-locked one -- so the
    # turn cannot be replayed as a 1-decision-each engine call.  Gated HERE, after
    # the damage-membership run: that check keys off the EXECUTED move and the
    # observed actor order (damage_membership.run_for_turn: `executed_move` /
    # `first_actor`), neither of which the override corrupts, so damage coverage
    # is kept.  Counted under its own reason so coverage stats stay honest.
    # Phase 2: the EXECUTED move is known but the CHOSEN action is not, which is
    # precisely the membership case -- enumerate that side's legal choices and
    # assert existentially, instead of refusing.
    _enc = _encore_overridden_side(block_lines, user_pid)
    if _enc is not None:
        if _membership_off() or plan is not None:
            # control arm, or a turn already carrying an unnamable side: the
            # Encore side's OWN move is still named, so widening it here would
            # compound two candidate axes on a turn the first axis already covers
            stats["turns_skipped"] += 1
            stats["skipped_encore_override"] = (
                stats.get("skipped_encore_override", 0) + 1
            )
            return
        plan = {"class": "encore_override", "u": None, "o": None}
        plan["u" if _enc == "user" else "o"] = "legal"

    # An Illusion bearer whose stay on the field the sidecar could pin to neither
    # a disguise nor the genuine mon leaves the species standing there undecided,
    # and with it every type/ability/item the categorical checks read.
    #
    # This is NOT an action-axis case and the membership apparatus above cannot
    # take it: the hypothesis is a SPECIES substitution that sits upstream of the
    # whole knowledge-fill chain (roster backfill, exact-team application,
    # randbats inference, tera/illusion/Cud-Chew passes), all of which mutate
    # `snap` in place and are not re-runnable per hypothesis.  Rather than leave
    # it refused, the turn is asserted under the reconstruction as-is with its
    # findings CAPPED TO SOFT: the second hypothesis was not evaluated, so no
    # finding on such a turn is HARD-decidable.  Sound (it can never manufacture
    # a hard finding) and strictly more coverage than a refusal, but it is a
    # PARTIAL handling of the class and is counted as one.
    illusion_capped = reveals is not None and (
        illusion_unresolved_turn(reveals, user_pid, turn)
        or illusion_unresolved_turn(reveals, opp_pid, turn)
    )
    if illusion_capped and (_membership_off() or plan is not None):
        stats["turns_skipped"] += 1
        stats["skipped_illusion_unresolved"] = (
            stats.get("skipped_illusion_unresolved", 0) + 1
        )
        return
    if illusion_capped:
        stats["illusion_soft_capped_turns"] = (
            stats.get("illusion_soft_capped_turns", 0) + 1
        )

    restrict = {
        "user": _species_key(_active_species(snap.user)),
        "opp": _species_key(_active_species(snap.opponent)),
    }
    observed = extract_observed_events(block_lines, user_pid, restrict)
    if plan is None and not observed:
        # The same emptiness the membership block reports for ITS turns, counted
        # for the ordinary ones too.  Without it the membership strength table
        # would look worse than the pre-existing coverage purely because only one
        # of the two is honest about how many of its turns had nothing to assert.
        stats["nonmembership_zero_observed"] = (
            stats.get("nonmembership_zero_observed", 0) + 1
        )
    # a faint-replacement call has NO end-of-turn phase in the engine by design;
    # drop the block's residual-phase events rather than assert them (see
    # _eot_skipped_by_faint_replacement)
    if _eot_skipped_by_faint_replacement(snap, u_action, o_action):
        cut = _residual_phase_start(block_lines)
        if cut is not None:
            kept = [
                e
                for e in observed
                if e.line_index is None or e.line_index < cut
            ]
            if len(kept) != len(observed):
                stats["eot_dropped_faint_replacement"] = stats.get(
                    "eot_dropped_faint_replacement", 0
                ) + (len(observed) - len(kept))
            observed = kept

    if plan is not None and plan["u"] == "none" and plan["o"] == "none":
        # A block where NEITHER side takes an action this sub-turn (both halves
        # of the split have already spent their decisions) can still carry the
        # PREVIOUS half's move residue: PS emits Revival Blessing's revive heal
        # AFTER the forceSwitch request, so `|-heal|p1: Miraidon|119/238|[from]
        # move: Revival Blessing` lands in the replacement block (synth00254 T28,
        # log lines 429-437).  No engine call for a sub-turn in which no move is
        # used can produce a move-sourced event, so asserting one is a guaranteed
        # false positive.  Drop those events and COUNT them -- the alternative is
        # refusing the whole block and losing its other coverage.  Narrow by
        # construction: it fires only when both actions are `none`, and only on
        # events whose own protocol line names a move as their source.
        n = 0
        kept = []
        for e in observed:
            src = (
                block_lines[e.line_index]
                if e.line_index is not None and e.line_index < len(block_lines)
                else ""
            )
            if "[from] move:" in src:
                n += 1
                continue
            kept.append(e)
        if n:
            observed = kept
            stats["membership_noop_move_residue_dropped"] = (
                stats.get("membership_noop_move_residue_dropped", 0) + n
            )

    if plan is None:
        evaluated = _evaluate_turn(
            snap,
            turn,
            u_move,
            o_move,
            u_action,
            o_action,
            observed,
            block_lines,
            user_pid,
            stats,
        )
    else:
        # EXISTENTIAL MEMBERSHIP.  The chosen candidate pair replaces the
        # unnamable argument(s) for everything downstream (the B1/B3 band
        # re-evaluation re-runs the turn and needs a namable pair, and the
        # finding context must report the pair the verdict was reached on).
        m = _membership_evaluate(
            snap,
            turn,
            u_move,
            o_move,
            u_action,
            o_action,
            observed,
            block_lines,
            user_pid,
            stats,
            plan["u"],
            plan["o"],
            plan["class"],
            # The USER's legal set is authoritative: `update_from_request_json`
            # above rewrites its active from the server's own request, which
            # lists the exact moveset.  The OPPONENT's is authoritative only when
            # a full-knowledge sidecar was applied; otherwise it is whatever the
            # protocol has revealed so far -- an UNDER-approximation, and the one
            # case where "no candidate reproduces" can be the set's fault.
            authoritative=(plan["o"] != "legal" or exact_teams is not None),
        )
        if m is None:
            stats["turns_skipped"] += 1
            _bump(stats, "skipped_membership_unbuildable")
            return
        if m == "degenerate":
            stats["turns_skipped"] += 1
            _bump(stats, "skipped_degenerate_engine_call")
            return
        evaluated, u_move, o_move = m
    if evaluated is None:
        # _evaluate_turn has exactly ONE None-return path: _generate_turn_branches
        # failed to build the baseline engine call.  Its sub-classification
        # (which call raised, with what exception) was recorded there.
        stats["turns_skipped"] += 1
        _bump(stats, "skipped_engine_build_failed")
        return
    turn_findings, parsed, state = evaluated
    if illusion_capped and turn_findings:
        # the un-evaluated species hypothesis (see the gate above) makes every
        # finding on this turn undecidable at HARD
        n = 0
        for f in turn_findings:
            if f.severity is Severity.HARD:
                f.severity = Severity.SOFT
                f.message += " [illusion-undecidable]"
                n += 1
        if n:
            stats["illusion_hard_capped_findings"] = (
                stats.get("illusion_hard_capped_findings", 0) + n
            )

    # B1/B3 INTERVAL-AWARE RE-EVALUATION (see the block comment above
    # _unburden_ability_ambiguous): a finding produced at the reconstruction's
    # own values is only reported if it also produces at EVERY other value the
    # protocol leaves open -- the opponent mon's certified display band (B1) and
    # either side's tracked Substitute HP interval (B3).  Clean turns never
    # enter this block.
    if turn_findings:
        band_pkmn = _simulated_pokemon(snap.opponent, o_action)
        axes = [
            (
                band_pkmn,
                "hp",
                int(band_pkmn.hp) if band_pkmn is not None else 0,
                _band_candidate_values(band_pkmn, parsed),
            )
        ]
        # a Substitute cannot survive its owner leaving the field, so the only
        # mon whose sub matters is the turn-start active on each side
        for _battler, _side_key in ((snap.user, "s1"), (snap.opponent, "s2")):
            _sub_pkmn = _battler.active
            if _sub_pkmn is None:
                continue
            axes.append(
                (
                    _sub_pkmn,
                    "substitute_health",
                    int(getattr(_sub_pkmn, "substitute_health", 0) or 0),
                    _substitute_band_values(_sub_pkmn, parsed, _side_key),
                )
            )
        axes = [a for a in axes if a[0] is not None and a[3]]
        assignments = _band_assignments(axes)
        if assignments:
            stats["band_eval_turns"] = stats.get("band_eval_turns", 0) + 1
            before = len(turn_findings)
            originals = [(obj, attr, cur) for obj, attr, cur, _v in axes]
            alive = {_finding_key(f) for f in turn_findings}
            soften: set = set()
            try:
                for assignment in assignments:
                    if not alive:
                        break
                    # reset FIRST: an assignment from the axis-aligned fallback
                    # names only ONE axis, and without this the others would
                    # still carry the previous assignment's values -- an
                    # accidental product walk that is sound (every combination is
                    # a state the protocol admits) but is not the walk the
                    # fallback claims to be doing, and is order-dependent.
                    for obj, attr, cur in originals:
                        setattr(obj, attr, cur)
                    for obj, attr, v in assignment:
                        setattr(obj, attr, v)
                    # scratch stats: a re-evaluation of the SAME turn must not
                    # inflate the per-turn coverage counters
                    alt = _evaluate_turn(
                        snap,
                        turn,
                        u_move,
                        o_move,
                        u_action,
                        o_action,
                        observed,
                        block_lines,
                        user_pid,
                        {},
                    )
                    if alt is None:
                        continue  # unbuildable at this value: no evidence
                    stats["band_eval_engine_calls"] = (
                        stats.get("band_eval_engine_calls", 0) + 1
                    )
                    alt_sev: dict = {}
                    for f in alt[0]:
                        k = _finding_key(f)
                        if f.severity is Severity.SOFT or k not in alt_sev:
                            alt_sev[k] = f.severity
                    # a finding ABSENT at this in-band assignment means the
                    # assignment reproduces the observation: the assertion is not
                    # decidable at reconstruction precision and must not be
                    # reported
                    alive &= set(alt_sev)
                    # ...and one the KO-margin demotion softened at this value
                    # is not HARD-decidable across the band either
                    soften |= {
                        k for k in alive if alt_sev.get(k) is Severity.SOFT
                    }
            finally:
                for obj, attr, cur in originals:
                    setattr(obj, attr, cur)
            kept = []
            for f in turn_findings:
                k = _finding_key(f)
                if k not in alive:
                    continue
                if k in soften and f.severity is Severity.HARD:
                    f.severity = Severity.SOFT
                    f.message += " [band-undecidable]"
                kept.append(f)
            turn_findings = kept
            if before != len(kept):
                stats["band_findings_dropped"] = stats.get(
                    "band_findings_dropped", 0
                ) + (before - len(kept))

    if turn_findings:
        # attach context so downstream triage can classify real-bug vs
        # false-positive without re-running the reconstruction.  `parsed` is
        # the SAME branch set the matcher used (phase-2 continuations and any
        # B2 ability-hypothesis branches included); `Instr.raw` is the
        # engine instruction's repr verbatim.
        branch_reprs = [[i.raw for i in b] for b in parsed][:10]
        block = [ln for ln in block_lines if not ln.startswith("|request|")]
        user_species = _active_species(snap.user)
        opp_species = _active_species(snap.opponent)
        try:
            state_string = state.to_string()
        except Exception:
            state_string = ""
        for f in turn_findings:
            f.user_move = u_move
            f.opp_move = o_move
            f.user_active = user_species
            f.opp_active = opp_species
            f.branches = branch_reprs
            f.block = block
            f.state_string = state_string
        findings.extend(turn_findings)
    stats["turns_checked"] += 1


def _species_key(name: str | None) -> str | None:
    if name is None:
        return None
    import re

    return re.sub(r"[^a-z0-9]", "", name.lower())


def _base_species_key(name: str | None) -> str | None:
    """Forme-family key: the pokedex baseSpecies when the entry names one
    (terapagosterastal -> terapagos, cramorantgulping -> cramorant), else the
    species itself."""
    key = _species_key(name)
    if key is None:
        return None
    try:
        from data import pokedex

        base = pokedex.get(key, {}).get("baseSpecies")
    except Exception:
        base = None
    return _species_key(normalize_name(base)) if base else key


def _prune_stale_forme_duplicates(battler, request_json) -> None:
    """Drop USER-side reserve rows that are stale forme-duplicates of a mon the
    request already accounts for.

    The request's 6-entry party list is the server's authoritative roster
    (sim/side.ts getRequestData renders every party member exactly once, under
    its CURRENT details).  Live tracking can end up with the same real mon under
    two forme names -- stored under one name on switch-out and re-created under
    another by a later switch-in -- leaving a phantom row whose hp/status are
    frozen at the earlier occupancy (synth26245: `terapagosterastal` 273/273
    survived next to the real, fainted `terapagos` 0/373 and kept counting as an
    ALIVE reserve, which wrongly armed Parting Shot's self-switch).

    A reserve row is dropped iff BOTH: (a) its exact species does not appear in
    the request list, and (b) its base-species family IS covered by another row
    (active or reserve) whose exact species the request lists.  Rows whose
    family the request does not cover are left untouched -- this prunes only
    provable duplicates, never merely-unrecognized mons."""
    try:
        req_entries = request_json["side"]["pokemon"]
    except Exception:
        return
    req_names = set()
    for p in req_entries:
        try:
            req_names.add(_species_key(normalize_name(p["details"].split(",")[0])))
        except Exception:
            continue
    if not req_names:
        return

    rows = list(battler.reserve)
    if battler.active is not None:
        rows.append(battler.active)
    covered_families = {
        _base_species_key(mon.name)
        for mon in rows
        if mon is not None and _species_key(mon.name) in req_names
    }
    kept = []
    for mon in battler.reserve:
        name_key = _species_key(mon.name)
        if name_key not in req_names and _base_species_key(mon.name) in covered_families:
            continue  # stale forme duplicate of a request-listed mon
        kept.append(mon)
    if len(kept) != len(battler.reserve):
        battler.reserve[:] = kept
