"""Info-state row emission (v1 row schema).

One JSONL row per decision of ONE side, serialized straight out of the bot's own
battle tracker (`fp.battle.Battle`, maintained by `fp.battle_modifier`).  There is
no second protocol parser here and there must never be one: every field below is
read off an attribute the tracker already maintains, and where the tracker
genuinely does not carry the information the field is emitted as `null` and the
gap is written down in `truestate/SCHEMA.md`.

`win` and `opp_action` are always null - the harvester fills them once both
sides' rows for a game exist.

Nothing in this module may break a game: `emit_row` swallows everything.
"""

import json
import logging
import os

import constants
from data.pkmn_sets import RandomBattleTeamDatasets, TeamDatasets
from fp.helpers import calculate_stats

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# action space
# ---------------------------------------------------------------------------
# 15 slots:
#   0-3   moves, indexed by the tracked active moveset (`battle.user.active.moves`)
#   4-7   the same move with terastallization
#   8-12  switches, by ABSOLUTE party index.  PS keeps the active at
#         `side.pokemon[0]` (sim/battle-actions.ts:130-133 swaps the incoming
#         pokemon into the outgoing one's position), so the request's party list
#         is [active, r1, r2, r3, r4, r5] and party index i (1-based) maps to
#         slot 6+i for i in 2..6.  The active's own slot is masked.
#   13    Struggle   (PS substitutes it for an empty move list, sim/pokemon.ts:1109-1111)
#   14    Recharge   (PS invents it for the locked slot, sim/pokemon.ts:973-977)
N_SLOTS = 15
STRUGGLE_SLOT = 13
RECHARGE_SLOT = 14

BOOST_KEYS = (
    ("atk", constants.ATTACK),
    ("def", constants.DEFENSE),
    ("spa", constants.SPECIAL_ATTACK),
    ("spd", constants.SPECIAL_DEFENSE),
    ("spe", constants.SPEED),
    ("accuracy", constants.ACCURACY),
    ("evasion", constants.EVASION),
)
STAT_KEYS = BOOST_KEYS[:5]

HAZARDS = (
    ("stealthrock", constants.STEALTH_ROCK),
    ("spikes", constants.SPIKES),
    ("toxicspikes", constants.TOXIC_SPIKES),
    ("stickyweb", constants.STICKY_WEB),
)
SCREENS = (
    ("reflect", constants.REFLECT),
    ("lightscreen", constants.LIGHT_SCREEN),
    ("auroraveil", constants.AURORA_VEIL),
    ("tailwind", constants.TAILWIND),
)

ENCORE = "encore"


def _pseudo_move_only(request_moves, move_id):
    """PS's locked-slot pseudo-move shape: a single entry with no `pp` key.

    `getMoves(lockedMove)` short-circuits and returns just `{move, id}` with no
    pp/maxpp (sim/pokemon.ts:970-990); Struggle and Recharge reach that slot the
    same way (:973-977, :1109-1111).  Mirrors fp/battle.py:562-615.
    """
    return (
        len(request_moves) == 1
        and constants.PP not in request_moves[0]
        and request_moves[0].get(constants.ID) == move_id
    )


def request_type(battle):
    request = battle.request_json or {}
    if request.get(constants.FORCE_SWITCH):
        party = (request.get(constants.SIDE) or {}).get(constants.POKEMON) or []
        # PS sets `reviving` on the active entry while a Revival Blessing slot
        # condition is pending (sim/pokemon.ts:1189)
        if any(p.get(constants.REVIVING) for p in party):
            return "revival"
        return "forceswitch"
    return "normal"


def build_legal_mask(battle):
    """The 15-slot legality mask plus the slot maps used to place the action.

    Everything is derived from the request JSON (the server's own statement of
    what is legal) and from the tracked active's `disabled` flags, which
    `_initialize_user_active_from_request_json` copies out of that same request.
    """
    request = battle.request_json or {}
    active_block = (request.get(constants.ACTIVE) or [{}])[0] or {}
    party = (request.get(constants.SIDE) or {}).get(constants.POKEMON) or []
    req_type = request_type(battle)

    request_moves = active_block.get(constants.MOVES) or []
    struggle_only = _pseudo_move_only(request_moves, "struggle")
    recharge_only = _pseudo_move_only(request_moves, "recharge")
    can_tera = bool(active_block.get(constants.CAN_TERASTALLIZE))
    trapped = bool(active_block.get(constants.TRAPPED))

    mask = [False] * N_SLOTS
    move_slots = {}

    active_pkmn = battle.user.active
    tracked_moves = list(active_pkmn.moves)[:4] if active_pkmn is not None else []
    for i, mv in enumerate(tracked_moves):
        move_slots[mv.name] = i
        if req_type == "normal" and not struggle_only and not recharge_only:
            mask[i] = not mv.disabled
            mask[4 + i] = mask[i] and can_tera

    if req_type == "normal":
        mask[STRUGGLE_SLOT] = struggle_only
        mask[RECHARGE_SLOT] = recharge_only

    # switches: absolute party index -> slot, active masked
    active_index = None
    for i, entry in enumerate(party):
        if entry.get(constants.ACTIVE):
            active_index = i + 1
            break
    switch_slots = {}
    other_indexes = [i + 1 for i in range(len(party)) if i + 1 != active_index]
    for k, party_index in enumerate(sorted(other_indexes)[:5]):
        switch_slots[party_index] = 8 + k

    for party_index, slot in switch_slots.items():
        entry = party[party_index - 1]
        hp = _condition_hp(entry.get(constants.CONDITION, ""))
        if req_type == "revival":
            # sim/side.ts:965-968: under Revival Blessing the target MUST be fainted
            mask[slot] = hp == 0
        elif req_type == "forceswitch":
            mask[slot] = hp > 0
        else:
            mask[slot] = hp > 0 and not trapped

    return {
        "mask": mask,
        "move_slots": move_slots,
        "switch_slots": switch_slots,
        "active_index": active_index,
        "party_size": len(party),
    }


def _condition_hp(condition):
    """`hp/maxhp status` or `0 fnt` -> current hp int."""
    try:
        return int(str(condition).split()[0].split("/")[0])
    except (ValueError, IndexError):
        return 0


def action_to_slot(battle, chosen_action, legal_mask_info):
    """Map the search's decision string onto (slot, canonical name)."""
    if chosen_action is None:
        return None, None
    name = str(chosen_action).strip()

    # the engine's recharge arm serializes as "No Move" (see run_battle.format_decision)
    if name.lower().replace(" ", "") in ("none", "nomove", "recharge"):
        return RECHARGE_SLOT, "recharge"

    if name.startswith(constants.SWITCH_STRING + " "):
        target = name.split("switch ", 1)[-1].strip()
        for pkmn in battle.user.reserve:
            if pkmn.name == target or pkmn.base_name == target:
                slot = legal_mask_info["switch_slots"].get(getattr(pkmn, "index", None))
                return slot, "switch {}".format(pkmn.name)
        return None, name

    tera = name.endswith("-tera")
    move_name = name.removesuffix("-tera").removesuffix("-mega")
    if move_name == "struggle":
        return STRUGGLE_SLOT, "struggle"
    slot = legal_mask_info["move_slots"].get(move_name)
    if slot is None:
        return None, name
    return (slot + 4 if tera else slot), name


# ---------------------------------------------------------------------------
# per-pokemon serialization
# ---------------------------------------------------------------------------
def _hp_pct(pkmn):
    if not pkmn.max_hp:
        return 0.0
    return round(pkmn.hp / pkmn.max_hp, 6)


def _boosts(pkmn):
    return {short: int(pkmn.boosts[key]) for short, key in BOOST_KEYS}


def _duration(pkmn, volatile, key=None):
    if volatile not in pkmn.volatile_statuses:
        return None
    return int(pkmn.volatile_status_durations[key or volatile])


def _us_pokemon(battler, pkmn, is_active):
    moves = []
    revealed_moves = []
    for mv in list(pkmn.moves)[:4]:
        moves.append(
            {
                "name": mv.name,
                "pp": int(mv.current_pp),
                "disabled": bool(mv.disabled),
            }
        )
        # PP only ever leaves a move by using it (or by Spite, which is equally
        # public), so `current_pp < max_pp` IS "the opponent has seen this move".
        revealed_moves.append(bool(mv.current_pp < mv.max_pp))
    while len(moves) < 4:
        moves.append(None)
        revealed_moves.append(False)

    locked_move = None
    if constants.LOCKED_MOVE in pkmn.volatile_statuses:
        locked_move = battler.last_used_move.move or None

    return {
        "species": pkmn.name,
        "level": int(pkmn.level),
        "hp_pct": _hp_pct(pkmn),
        "status": pkmn.status,
        "sleep_turns": int(pkmn.sleep_turns),
        # foul-play keeps the Toxic counter on the SIDE (it is reset on
        # switch-out exactly as PS resets `statusState.stage`), so it belongs to
        # whichever mon is currently active.
        # explicit active flag: consumers must NOT infer activeness from list
        # position (the party-order invariant holds today, but the tensorizer
        # reading position 0 as active was exactly the integration bug this
        # flag closes)
        "active": bool(is_active),
        "toxic_count": (
            int(battler.side_conditions[constants.TOXIC_COUNT]) if is_active else 0
        ),
        "stats": {short: int(pkmn.stats.get(key, 0)) for short, key in STAT_KEYS},
        "boosts": _boosts(pkmn),
        "moves": moves,
        "item": None if pkmn.item == constants.UNKNOWN_ITEM else pkmn.item,
        "ability": pkmn.ability,
        "tera_type": pkmn.tera_type,
        "tera_used": bool(pkmn.terastallized),
        "volatiles": sorted(pkmn.volatile_statuses),
        "sub_hp": int(pkmn.substitute_health) or None,
        "encore_turns": _duration(pkmn, ENCORE),
        "taunt_turns": _duration(pkmn, constants.TAUNT),
        "locked_move": locked_move,
        "rage_fist_hits": int(pkmn.times_attacked),
        "slow_start_turns": _duration(pkmn, constants.SLOW_START),
        "revealed": {
            "moves": revealed_moves,
            # not tracked for our own side - see SCHEMA.md gap list
            "item": None,
            "ability": None,
            # terastallization is always announced on the wire
            "tera": bool(pkmn.terastallized),
        },
    }


def _dataset_for(battle):
    if battle.battle_type == constants.BattleType.RANDOM_BATTLE:
        return RandomBattleTeamDatasets
    return TeamDatasets


def _candidates_and_bands(battle, pkmn, top_n=5):
    """Surviving-set count, top-N by prior weight, and implied stat bands.

    The candidate list is `get_all_remaining_sets`, i.e. the dataset's sets minus
    everything `battle_modifier`'s elimination lists (impossible_items /
    impossible_abilities / speed_range / rejected_set_signatures / revealed
    moves) have already ruled out.  Weights are the dataset's raw usage counts
    renormalized over the SURVIVORS - a prior restricted to the feasible set, not
    a damage-likelihood posterior (see SCHEMA.md).
    """
    dataset = _dataset_for(battle)
    remaining = dataset.get_all_remaining_sets(pkmn)
    if not remaining:
        return None, None

    total = sum(max(s.pkmn_set.count, 0) for s in remaining) or 1
    ranked = sorted(remaining, key=lambda s: s.pkmn_set.count, reverse=True)
    top = []
    for s in ranked[:top_n]:
        top.append(
            {
                "sig": "{}|{}|{}|{}".format(
                    s.pkmn_set.ability or "",
                    s.pkmn_set.item or "",
                    s.pkmn_set.tera_type or "",
                    ",".join(sorted(s.pkmn_moveset.moves)),
                ),
                "w": round(s.pkmn_set.count / total, 6),
            }
        )

    hp_lo = atk_lo = spe_lo = None
    hp_hi = atk_hi = spe_hi = None
    for s in remaining:
        stats = calculate_stats(
            pkmn.base_stats,
            s.pkmn_set.level or pkmn.level,
            ivs=s.pkmn_set.ivs,
            evs=s.pkmn_set.evs,
            nature=s.pkmn_set.nature,
        )
        spe = stats[constants.SPEED]
        if s.pkmn_set.item == "choicescarf":
            spe = int(spe * 1.5)
        for value, lo_hi in (
            (stats[constants.HITPOINTS], "hp"),
            (stats[constants.ATTACK], "atk"),
            (spe, "spe"),
        ):
            if lo_hi == "hp":
                hp_lo = value if hp_lo is None else min(hp_lo, value)
                hp_hi = value if hp_hi is None else max(hp_hi, value)
            elif lo_hi == "atk":
                atk_lo = value if atk_lo is None else min(atk_lo, value)
                atk_hi = value if atk_hi is None else max(atk_hi, value)
            else:
                spe_lo = value if spe_lo is None else min(spe_lo, value)
                spe_hi = value if spe_hi is None else max(spe_hi, value)

    bands = {
        "hp": [int(hp_lo), int(hp_hi)],
        "atk": [int(atk_lo), int(atk_hi)],
        "spe": [int(spe_lo), int(spe_hi)],
    }
    return {"n": len(remaining), "top": top}, bands


def _them_pokemon(battle, pkmn):
    if pkmn.item == constants.UNKNOWN_ITEM:
        item = "UNKNOWN"
    elif pkmn.item is None:
        item = "KNOWN_ABSENT"
    else:
        item = pkmn.item

    try:
        candidates, bands = _candidates_and_bands(battle, pkmn)
    except Exception:
        logger.debug("infostate: candidate derivation failed", exc_info=True)
        candidates, bands = None, None

    return {
        "species": pkmn.name,
        "level": int(pkmn.level),
        # not tracked: `Pokemon.from_switch_string` parses species+level only
        "gender": None,
        "hp_pct": _hp_pct(pkmn),
        "status": pkmn.status,
        "boosts": _boosts(pkmn),
        "revealed_moves": [
            {"name": mv.name, "pp_min_used": int(max(mv.max_pp - mv.current_pp, 0))}
            for mv in pkmn.moves
        ],
        "item": item,
        "ability": pkmn.ability or "UNKNOWN",
        "tera_type": pkmn.tera_type,
        "volatiles": sorted(pkmn.volatile_statuses),
        "sub_up": bool(pkmn.substitute_health),
        "stat_bands": bands,
        "candidates": candidates,
    }


def _tera_available(battler):
    if battler.tera_spent:
        return False
    party = [battler.active] + list(battler.reserve)
    return not any(p is not None and p.terastallized for p in party)


def _side_field(battler):
    return {
        "hazards": {name: int(battler.side_conditions[key]) for name, key in HAZARDS},
        "screens": {name: int(battler.side_conditions[key]) for name, key in SCREENS},
    }


def _last_actions(battle):
    out = []
    for battler in (battle.user, battle.opponent):
        lum = battler.last_used_move
        if lum and lum.move:
            out.append((lum.turn, {"side": battler.name, "name": lum.move}))
    out.sort(key=lambda t: t[0], reverse=True)
    return [entry for _, entry in out[:3]]


# ---------------------------------------------------------------------------
# the row
# ---------------------------------------------------------------------------
def serialize_row(battle, request_type_str, chosen_action, legal_mask_info):
    slot, action_name = action_to_slot(battle, chosen_action, legal_mask_info)

    party_by_index = {}
    for pkmn in [battle.user.active] + list(battle.user.reserve):
        if pkmn is not None and getattr(pkmn, "index", None):
            party_by_index.setdefault(pkmn.index, pkmn)
    active_index = legal_mask_info.get("active_index")

    us = []
    for party_index in sorted(party_by_index):
        pkmn = party_by_index[party_index]
        us.append(_us_pokemon(battle.user, pkmn, party_index == active_index))

    them_pkmn = [
        p
        for p in [battle.opponent.active] + list(battle.opponent.reserve)
        if p is not None and p.name and getattr(p, "revealed", True)
    ]
    them = [_them_pokemon(battle, p) for p in them_pkmn]

    user_field = _side_field(battle.user)
    opp_field = _side_field(battle.opponent)

    return {
        "game_id": battle.battle_tag,
        "turn": int(battle.turn or 0),
        "side": battle.user.name,
        "request_type": request_type_str,
        "action": {"slot": slot, "name": action_name},
        "legal_mask": legal_mask_info["mask"],
        "win": None,
        "opp_action": None,
        "us": us,
        "them": them,
        "them_unrevealed_count": max(0, 6 - len(them)),
        "field": {
            "hazards_us": user_field["hazards"],
            "hazards_them": opp_field["hazards"],
            "screens_us": user_field["screens"],
            "screens_them": opp_field["screens"],
            "weather": battle.weather,
            "weather_turns": (
                int(battle.weather_turns_remaining) if battle.weather else None
            ),
            "terrain": battle.field,
            "terrain_turns": (
                int(battle.field_turns_remaining) if battle.field else None
            ),
            "trick_room_turns": (
                int(battle.trick_room_turns_remaining) if battle.trick_room else None
            ),
            "gravity": bool(battle.gravity),
        },
        "pending": {
            "wish_us": (
                [int(battle.user.wish[0]), int(battle.user.wish[1])]
                if battle.user.wish[0]
                else None
            ),
            "wish_them": (
                [int(battle.opponent.wish[0]), int(battle.opponent.wish[1])]
                if battle.opponent.wish[0]
                else None
            ),
            "futuresight_us": (
                int(battle.user.future_sight[0]) if battle.user.future_sight[0] else None
            ),
            "futuresight_them": (
                int(battle.opponent.future_sight[0])
                if battle.opponent.future_sight[0]
                else None
            ),
        },
        "tera_available_us": _tera_available(battle.user),
        "tera_available_them": _tera_available(battle.opponent),
        "last_actions": _last_actions(battle),
        "elo_bucket": None,
    }


def emit_row(battle, chosen_action, out_dir):
    """Append one info-state row for this decision.  Never raises."""
    if not out_dir:
        return None
    try:
        if battle.team_preview or battle.request_json is None:
            return None
        legal_mask_info = build_legal_mask(battle)
        row = serialize_row(
            battle, request_type(battle), chosen_action, legal_mask_info
        )
        os.makedirs(out_dir, exist_ok=True)
        path = os.path.join(
            out_dir, "{}_{}.rows.jsonl".format(battle.battle_tag, battle.user.name)
        )
        with open(path, "a") as f:
            f.write(json.dumps(row, separators=(",", ":")) + "\n")
        return path
    except Exception:
        logger.warning(
            "infostate row emission failed (turn=%s) - skipping row",
            getattr(battle, "turn", None),
            exc_info=True,
        )
        return None
