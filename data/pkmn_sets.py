from __future__ import annotations

import math
import ntpath
from dataclasses import dataclass

import requests
from dateutil import relativedelta
from datetime import datetime
import os
import json
import logging
import time
import typing
from typing import Tuple
from typing import Optional


import constants
from data import all_move_json, pokedex
from fp.helpers import calculate_stats, random_battles_evs
from fp.helpers import normalize_name
from fp.helpers import type_effectiveness_modifier

PWD = os.path.dirname(os.path.abspath(__file__))
SMOGON_CACHE_DIR = os.path.join(PWD, "smogon_stats_cache")
os.makedirs(SMOGON_CACHE_DIR, exist_ok=True)
PKMN_SETS_CACHE_DIR = os.path.join(PWD, "pkmn_sets_cache")
os.makedirs(PKMN_SETS_CACHE_DIR, exist_ok=True)

PKMN_SETS_REMOTE_BASE_URL = "https://data.foulplay.cc/{}/{}"
PS_SETS_REMOTE_BASE_URL = "https://play.pokemonshowdown.com/data/sets/{}"
PKMN_RANDBATS_REMOTE_BASE_URL = "https://pkmn.github.io/randbats/data/full/{}.json"

OTHER_STRING = "other"
MOVES_STRING = "moves"
ITEM_STRING = "items"
SPREADS_STRING = "spreads"
ABILITY_STRING = "abilities"
TERA_TYPE_STRING = "tera_types"
EFFECTIVENESS = "effectiveness"
TEAMMATES = "teammates"
RAW_COUNT = "raw_count"

if typing.TYPE_CHECKING:
    from fp.battle import Pokemon

logger = logging.getLogger(__name__)

GRADED_EVIDENCE_RELAXATION_FLAG = "FP_GRADED_EVIDENCE_RELAXATION"
# Graded relaxation is ON by default; this disables it, restoring the legacy
# all-at-once fallback.  Named to match the established FP_CONTROL_NO_* negative
# control convention used elsewhere in the tree.
GRADED_EVIDENCE_RELAXATION_CONTROL_OFF = "FP_CONTROL_NO_GRADED_EVIDENCE_RELAXATION"


# 24h: pkmn/randbats regenerates whenever Showdown's generator changes
# (~monthly, typically at dex rotations - exactly when sets shift most). A
# 7-day TTL risked sampling week-stale sets through a meta change; the file
# is ~466KB so a daily re-download is free. On download failure the stale
# cache is kept (see get_sets_file), so a tight TTL cannot brick the bot.
SETS_CACHE_TTL_SECONDS = 24 * 60 * 60


def _build_cosmetic_forme_to_base() -> dict[str, str]:
    """Maps every cosmetic forme id to its base species id.

    PS lists cosmetic formes on the BASE entry (`cosmeticFormes`) and gives the
    forme itself no `baseSpecies`, so a forme like `sawsbucksummer` cannot be
    resolved to `sawsbuck` by the normal baseSpecies path.  Cosmetic formes are
    identical for set generation -- only appearance differs -- so they must share
    the base species' sets.
    """
    # Two passes: cosmetic forme ENTRIES carry a copy of `cosmeticFormes`, so a
    # single pass lets one forme overwrite another's base (sawsbucksummer ->
    # sawsbuckwinter).  Collect the forme ids first, then only accept a base
    # that is not itself a cosmetic forme.
    all_formes = set()
    for entry in pokedex.values():
        for forme in (entry or {}).get("cosmeticFormes") or []:
            all_formes.add(normalize_name(forme))

    mapping = {}
    for base_id, entry in pokedex.items():
        if base_id in all_formes:
            continue
        for forme in (entry or {}).get("cosmeticFormes") or []:
            mapping[normalize_name(forme)] = base_id
    return mapping


COSMETIC_FORME_TO_BASE = _build_cosmetic_forme_to_base()


def _read_sets_cache_file(cache_path: str) -> Optional[dict]:
    """Returns the cached sets, or None if the cache is missing, empty, or unreadable"""
    if not os.path.exists(cache_path):
        return None
    try:
        with open(cache_path, "r") as f:
            sets = json.load(f)
    except (OSError, ValueError):
        logger.warning(f"Could not read cache file: {cache_path}")
        return None

    # an empty cache file is a previously-cached failure: treat it as missing
    if not sets:
        return None

    return sets


def get_sets_file(cache_path: str, remote_url: str) -> dict:
    cached_sets = _read_sets_cache_file(cache_path)
    if cached_sets is not None:
        cache_age_seconds = time.time() - os.path.getmtime(cache_path)
        if cache_age_seconds <= SETS_CACHE_TTL_SECONDS:
            logger.info(f"Loaded from cache: {cache_path}")
            return cached_sets
        logger.info(f"Cache is older than {SETS_CACHE_TTL_SECONDS}s: {cache_path}")

    sets = {}
    try:
        r = requests.get(remote_url)
        if r.status_code == 200:
            sets = r.json()
        else:
            logger.warning(
                f"Could not retrieve from remote: {remote_url} "
                f"(status code {r.status_code})"
            )
    except (requests.RequestException, ValueError) as e:
        logger.warning(f"Could not retrieve from remote: {remote_url} ({e})")

    # only successful non-empty responses are cached so that failures
    # are retried on the next call rather than persisted to disk
    if sets:
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        # atomic write: parallel workers racing this cache tore the file and
        # poisoned every subsequent read (1046 warnings in one cloud sweep)
        tmp_path = f"{cache_path}.tmp.{os.getpid()}"
        with open(tmp_path, "w") as f:
            json.dump(sets, f)
        os.replace(tmp_path, cache_path)
        logger.info(f"Downloaded and cached from remote: {remote_url}")
        return sets

    if cached_sets is not None:
        logger.warning(f"Falling back to stale cache: {cache_path}")
        return cached_sets

    return sets


USER_SETS_INGESTED_KEY = "_ingested"


def _strip_blitz(pkmn_mode: str) -> str:
    if pkmn_mode.endswith("blitz"):
        return pkmn_mode[:-5]
    return pkmn_mode


def user_sets_cache_path(pkmn_mode: str) -> str:
    return os.path.join(PKMN_SETS_CACHE_DIR, f"{_strip_blitz(pkmn_mode)}_user.json")


def make_user_set_key(level, item, ability, moves, tera_type=None) -> str:
    """
    Builds a set key in the same format as the randombattle sets file:
    'level,item,ability,move1,...,moveN[,teraType]' with normalized ids
    and moves sorted alphabetically
    """
    parts = [str(level), normalize_name(item or ""), normalize_name(ability or "")]
    parts.extend(sorted(normalize_name(m) for m in moves))
    if tera_type:
        parts.append(normalize_name(tera_type))
    return ",".join(parts)


def load_user_observed_sets(pkmn_mode: str) -> dict:
    path = user_sets_cache_path(pkmn_mode)
    if not os.path.exists(path):
        return {}
    try:
        with open(path, "r") as f:
            return json.load(f)
    except (OSError, ValueError):
        logger.warning(f"Could not read user observed sets: {path}")
        return {}


def record_user_set_observations(
    pkmn_mode: str,
    observations: typing.Iterable[Tuple[str, str]],
    ingested_marker: Optional[str] = None,
):
    """
    Increments the count of each observed (pkmn_name, set_key) in the
    user-observation overlay file for this mode

    The overlay lives next to the downloaded cache but is a separate
    file: the downloaded cache file is never modified

    `ingested_marker` optionally records a source identifier (e.g. a
    file name) under '_ingested' so backfills can be idempotent
    """
    overlay = load_user_observed_sets(pkmn_mode)
    for pkmn_name, set_key in observations:
        pkmn_observations = overlay.setdefault(pkmn_name, {})
        pkmn_observations[set_key] = pkmn_observations.get(set_key, 0) + 1

    if ingested_marker is not None:
        ingested = overlay.setdefault(USER_SETS_INGESTED_KEY, [])
        if ingested_marker not in ingested:
            ingested.append(ingested_marker)

    path = user_sets_cache_path(pkmn_mode)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(overlay, f)


def get_ps_sets_file(pkmn_mode: str) -> dict:
    def add_new_set(d: dict, p: str, s: str):
        if p not in d:
            d[p] = {}
        if s not in d[p]:
            d[p][s] = 0
        d[p][s] += 1

    def get_pkmn_data(n: dict, s: dict):
        for pkmn_name, pkmn_sets in s.items():
            pkmn_name = normalize_name(pkmn_name)
            for pkmn_set in pkmn_sets.values():
                moves = "|".join(sorted([normalize_name(m) for m in pkmn_set["moves"]]))
                ability = normalize_name(pkmn_set.get("ability", "noability"))
                item = normalize_name(pkmn_set.get("item", "none"))
                nature = normalize_name(pkmn_set.get("nature", "serious"))
                tera_type = normalize_name(pkmn_set.get("teraType", ""))

                # this EV handling looks stupid, but it is to deal with all generations
                # if evs dict isn't present we are in gen1 or gen2: set all to 252
                evs = pkmn_set.get(
                    "evs",
                    {
                        "hp": 252,
                        "atk": 252,
                        "def": 252,
                        "spa": 252,
                        "spd": 252,
                        "spe": 252,
                    },
                )
                # missing individual ev keys means we didn't trigger the default in the above line (we are in gen3+)
                # missing ev keys means it is 0
                evs = (
                    evs.get("hp", 0),
                    evs.get("atk", 0),
                    evs.get("def", 0),
                    evs.get("spa", 0),
                    evs.get("spd", 0),
                    evs.get("spe", 0),
                )
                evs = ",".join(str(v) for v in evs)
                set_string = f"{tera_type}|{ability}|{item}|{nature}|{evs}|{moves}"
                add_new_set(n, pkmn_name, set_string)

    cache_path = os.path.join(PKMN_SETS_CACHE_DIR, pkmn_mode, "showdown_sets.json")
    remote_url = PS_SETS_REMOTE_BASE_URL.format(f"{pkmn_mode}.json")
    sets_dict = get_sets_file(cache_path, remote_url)
    new_sets = {}
    get_pkmn_data(new_sets, sets_dict.get("dex", {}))
    get_pkmn_data(new_sets, sets_dict.get("stats", {}))
    return new_sets


def get_pkmn_sets_file(pkmn_mode: str, file_name: str) -> dict:
    cache_path = os.path.join(PKMN_SETS_CACHE_DIR, pkmn_mode, file_name)
    remote_url = PKMN_SETS_REMOTE_BASE_URL.format(pkmn_mode, file_name)
    return get_sets_file(cache_path, remote_url)


def get_randbats_sets_file(pkmn_randbats_mode: str) -> dict:
    cache_path = os.path.join(PKMN_SETS_CACHE_DIR, f"{pkmn_randbats_mode}.json")
    remote_url = PKMN_RANDBATS_REMOTE_BASE_URL.format(pkmn_randbats_mode)
    return get_sets_file(cache_path, remote_url)


# moves whose damage is computed by a callback and so never read the attacker's
# Attack stat (pokemon-showdown data/moves.ts, `damageCallback`). foul-play's
# moves.json does not carry the `damageCallback` flag, so they are listed here.
_DAMAGE_CALLBACK_MOVES = {
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
}


def _set_uses_physical_attack_stat(moves, species_id, ability, base_stats) -> bool:
    """
    Inverse of pokemon-showdown teams.ts randomSet's ``noAttackStatMoves``
    (data/random-battles/gen9/teams.ts:1567-1584): returns True when the set has
    at least one move that reads the Attack stat, i.e. a move that is NOT purely
    special/status/fixed-damage. Used to decide whether the flat Atk EV/IV should
    be zeroed.
    """
    for mv in moves:
        move_data = all_move_json.get(mv, {})
        # fixed-damage moves (Seismic Toss / Night Shade / Sonic Boom / Dragon
        # Rage) and damage-callback moves (Super Fang / Counter / ...) don't use
        # the Attack stat
        if move_data.get("damage") or mv in _DAMAGE_CALLBACK_MOVES:
            continue
        if mv == "shellsidearm":
            return True
        # physical Tera Blast
        if mv == "terablast" and (
            species_id == "porygon2"
            or ability in ("contrary", "defiant")
            or "shiftgear" in moves
            or base_stats[constants.ATTACK] > base_stats[constants.SPECIAL_ATTACK]
        ):
            return True
        if (
            move_data.get(constants.CATEGORY) == constants.PHYSICAL
            and mv not in ("bodypress", "foulplay")
        ):
            return True
    return False


def random_battle_ev_iv_spread(species_id, moves, ability, item, level):
    """
    Reproduce the deterministic EV/IV post-processing pokemon-showdown applies in
    teams.ts randomSet (data/random-battles/gen9/teams.ts:1541-1589), derived from
    a set's already-known moves/item/ability. Starts from the generator's flat
    85 EV / 31 IV spread and applies the three singles adjustments:
      (a) zero Atk EV+IV when the set has no physical attacking move
      (b) zero Spe EV+IV for Gyro Ball / Trick Room sets
      (c) shave HP EVs to hit Stealth-Rock-survival / berry breakpoints

    Returns ``(evs, ivs)`` as 6-tuples ordered (hp, atk, def, spa, spd, spe).
    """
    evs = list(random_battles_evs())
    ivs = [31] * 6

    # only the standard 85-EV base is post-processed here; champions and any
    # future non-standard base are left as-is
    if evs != [85, 85, 85, 85, 85, 85]:
        return tuple(evs), tuple(ivs)

    entry = pokedex.get(species_id)
    if entry is None:
        return tuple(evs), tuple(ivs)
    base_stats = entry[constants.BASESTATS]
    types = entry[constants.TYPES]
    moves = set(moves)

    # (c) shave HP EVs to the optimal breakpoint (teams.ts:1541-1564)
    sr_immunity = ability == "magicguard" or item == "heavydutyboots"
    if sr_immunity:
        sr_weakness = 0
    else:
        rock_mult = type_effectiveness_modifier("rock", types)
        sr_weakness = 0 if rock_mult == 0 else round(math.log2(rock_mult))
    # crash-damage move users want an odd HP to survive two misses
    if moves & {"axekick", "highjumpkick", "jumpkick", "supercellslam"}:
        sr_weakness = 2
    while evs[0] > 1:
        hp = math.floor(
            math.floor(
                2 * base_stats[constants.HITPOINTS]
                + ivs[0]
                + math.floor(evs[0] / 4)
                + 100
            )
            * level
            / 100
            + 10
        )
        if ("substitute" in moves and item == "sitrusberry") or species_id == "minior":
            # Two Substitutes should activate Sitrus Berry (or Minior Shields Down)
            if hp % 4 == 0:
                break
        elif (moves & {"bellydrum", "filletaway", "shedtail"}) and (
            item == "sitrusberry" or ability == "gluttony"
        ):
            # Belly Drum should activate Sitrus Berry
            if hp % 2 == 0:
                break
        elif "substitute" in moves and "endeavor" in moves:
            if hp % 4 > 0:
                break
        else:
            # maximize the number of Stealth Rock switch-ins in singles
            if (
                sr_weakness <= 0
                or ability == "regenerator"
                or item in ("leftovers", "lifeorb")
            ):
                break
            if item != "sitrusberry" and hp % (4 // sr_weakness) > 0:
                break
            if item == "sitrusberry" and hp % (4 // sr_weakness) == 0:
                break
        evs[0] -= 4

    # (a) zero Atk EV+IV for sets with no physical attacking move (teams.ts:1567-1584)
    if (
        not _set_uses_physical_attack_stat(moves, species_id, ability, base_stats)
        and "transform" not in moves
    ):
        evs[1] = 0
        ivs[1] = 0

    # (b) zero Spe EV+IV for Gyro Ball / Trick Room (teams.ts:1586-1589)
    if "gyroball" in moves or "trickroom" in moves:
        evs[5] = 0
        ivs[5] = 0

    return tuple(evs), tuple(ivs)


def spreads_are_alike(s1, s2):
    if s1[0] != s2[0]:
        return False

    s1 = [int(v) for v in s1[1].split(",")]
    s2 = [int(v) for v in s2[1].split(",")]

    diff = [abs(i - j) for i, j in zip(s1, s2)]

    # 24 is arbitrarily chosen as the threshold for EVs to be "alike"
    return all(v <= 48 for v in diff)


@dataclass
class PredictedPokemonSet:
    pkmn_set: PokemonSet
    pkmn_moveset: PokemonMoveset

    def mechanics_signature(self):
        """Canonical identity for mechanics-equivalent dataset rows."""
        return (
            normalize_name(self.pkmn_set.ability or ""),
            normalize_name(self.pkmn_set.item or ""),
            normalize_name(self.pkmn_set.nature or ""),
            tuple(int(value) for value in self.pkmn_set.evs),
            tuple(int(value) for value in self.pkmn_set.ivs),
            (
                None
                if self.pkmn_set.level is None
                else int(self.pkmn_set.level)
            ),
            normalize_name(self.pkmn_set.tera_type or ""),
            tuple(sorted(normalize_name(move) for move in self.pkmn_moveset.moves)),
        )

    def full_set_pkmn_can_have_set(
        self,
        pkmn: Pokemon,
        match_ability=True,
        match_item=True,
        speed_check=True,
        level_check=False,
        tera_check=True,
    ) -> bool:
        if self.mechanics_signature() in getattr(
            pkmn, "rejected_set_signatures", set()
        ):
            return False
        return self.pkmn_set.set_makes_sense(
            pkmn,
            match_ability=match_ability,
            match_item=match_item,
            speed_check=speed_check,
            level_check=level_check,
            match_tera=tera_check,
        ) and self.pkmn_moveset.full_set_pkmn_can_have_moves(pkmn)


@dataclass
class PokemonSet:
    ability: str
    item: str
    nature: str
    evs: tuple[int, ...] | list[int]
    count: int
    ivs: tuple[int, ...] | list[int] = (31, 31, 31, 31, 31, 31)
    level: Optional[int] = 100
    tera_type: Optional[str] = None

    def speed_check(self, pkmn: Pokemon):
        """
        The only non-observable speed modifier that should allow a
        Pokemon's speed_range to be set is choicescarf
        """
        stats = calculate_stats(
            pkmn.base_stats,
            pkmn.level,
            ivs=self.ivs,
            evs=self.evs,
            nature=self.nature,
        )
        speed = stats[constants.SPEED]
        if self.item == "choicescarf":
            speed = int(speed * 1.5)

        return pkmn.speed_range.min <= speed <= pkmn.speed_range.max

    def item_check(self, pkmn: Pokemon) -> bool:
        if pkmn.item == self.item and pkmn.removed_item is None:
            return True
        elif pkmn.removed_item == self.item:
            return True
        elif pkmn.item is None and pkmn.removed_item is None:
            return False
        if self.item in pkmn.impossible_items:
            return False
        elif self.item in constants.CHOICE_ITEMS and not pkmn.can_have_choice_item:
            return False
        else:
            return pkmn.item == constants.UNKNOWN_ITEM

    def ability_check(self, pkmn: Pokemon) -> bool:
        if self.ability == pkmn.ability:
            return True
        elif self.ability in pkmn.impossible_abilities:
            return False
        else:
            return pkmn.ability is None

    def set_makes_sense(
        self,
        pkmn: Pokemon,
        match_ability=True,
        match_item=True,
        speed_check=True,
        level_check=False,
        match_tera=True,
    ):
        ability_check = not match_ability or self.ability_check(pkmn)
        item_check = not match_item or self.item_check(pkmn)
        level_check = not level_check or pkmn.level == self.level
        speed_check = not speed_check or self.speed_check(pkmn)
        tera_check = True
        if (
            match_tera
            and self.tera_type is not None
            and pkmn.terastallized
            and self.tera_type != pkmn.tera_type
        ):
            tera_check = False

        return (
            ability_check and item_check and speed_check and level_check and tera_check
        )


@dataclass
class PokemonMoveset:
    moves: Tuple[str, ...] | list[str]
    count: int = 1

    def __post_init__(self):
        new_moves = []
        for mv in self.moves:
            if mv.startswith(constants.HIDDEN_POWER) and not mv.endswith("0"):
                new_moves.append(
                    f"{mv}{constants.HIDDEN_POWER_ACTIVE_MOVE_BASE_DAMAGE_STRING}"
                )
            else:
                new_moves.append(mv)

        self.moves = tuple(new_moves)

    def full_set_pkmn_can_have_moves(self, pkmn: Pokemon) -> bool:
        for mv in pkmn.moves:
            if mv.name == constants.HIDDEN_POWER:
                hidden_power_possibilities = [
                    f"{constants.HIDDEN_POWER}{p}{constants.HIDDEN_POWER_ACTIVE_MOVE_BASE_DAMAGE_STRING}"
                    for p in pkmn.hidden_power_possibilities
                ]
                hidden_power_in_this_pkmn_set = [
                    m for m in self.moves if m.startswith(constants.HIDDEN_POWER)
                ]
                if (
                    len(hidden_power_in_this_pkmn_set) == 1
                    and hidden_power_in_this_pkmn_set[0] in hidden_power_possibilities
                ):
                    pass
                else:
                    return False
            elif mv.name not in self.moves:
                return False
        return True

    def add_move(self, mv: str):
        self.moves += (mv,)

    def remove_move(self, mv: str):
        self.moves = tuple(m for m in self.moves if m != mv)

    def __iter__(self):
        yield from self.moves

    def __len__(self):
        return len(self.moves)


class PokemonSets:
    raw_pkmn_sets: dict[str, list]
    pkmn_sets: dict[str, list]
    pkmn_mode: str

    def initialize(self, pkmn_mode: str, pkmn_names: set[str]): ...

    def predict_set(self, pkmn: Pokemon) -> Optional[PredictedPokemonSet]: ...

    @staticmethod
    def get_key_in_dict_from_pkmn_name(
        pkmn_name: str, pkmn_base_name: str, pkmn_mega_name: str | None, d: dict
    ):
        if pkmn_mega_name in d:
            return d[pkmn_mega_name]
        elif pkmn_name in d:
            # A COSMETIC forme (Sawsbuck-Summer, Gastrodon-East, ...) shares its
            # base species' sets in PS's generator: pokedex records them under
            # `cosmeticFormes` on the BASE entry and gives the forme itself no
            # `baseSpecies`, so the baseSpecies fallback below never fires for
            # them.  `_merge_user_observed_sets` writes an observed set under the
            # narrow cosmetic key, and this early return then SHADOWS the entire
            # base family -- measured at 600 support gaps where the opponent's
            # true set existed in the dataset but the lookup never offered it
            # (SPEC-B4).  Merge instead of shadowing; dedupe so a set observed
            # under both keys is not double-weighted in the count-weighted draw.
            base_species = COSMETIC_FORME_TO_BASE.get(pkmn_name)
            if base_species is not None and base_species in d:
                merged = list(d[base_species])
                seen = {
                    s.mechanics_signature()
                    for s in merged
                    if hasattr(s, "mechanics_signature")
                }
                for s in d[pkmn_name]:
                    if (
                        not hasattr(s, "mechanics_signature")
                        or s.mechanics_signature() not in seen
                    ):
                        merged.append(s)
                return merged
            return d[pkmn_name]
        elif pkmn_base_name in d:
            return d[pkmn_base_name]

        if pkmn_name in pokedex and "baseSpecies" in pokedex[pkmn_name]:
            pkmn_base_species = normalize_name(pokedex[pkmn_name]["baseSpecies"])
            if pkmn_base_species in d:
                return d[pkmn_base_species]

        if pkmn_name in pokedex and "name" in pokedex[pkmn_name]:
            pkmn_non_cosmetic_name = normalize_name(pokedex[pkmn_name]["name"])
            if pkmn_non_cosmetic_name in d:
                return d[pkmn_non_cosmetic_name]

        return []

    def get_pkmn_sets_from_pkmn_name(self, pkmn: Pokemon):
        ret = []
        ret += self.get_key_in_dict_from_pkmn_name(
            pkmn.name, pkmn.base_name, pkmn.mega_name, self.pkmn_sets
        )

        pkmn_mega_info = pkmn.get_mega_pkmn_info()
        for pkmn_mega_name, _ in pkmn_mega_info:
            ret += self.get_key_in_dict_from_pkmn_name(
                pkmn_mega_name, pkmn_mega_name, None, self.pkmn_sets
            )

        return ret

    def get_raw_pkmn_sets_from_pkmn_name(self, pkmn_name: str, pkmn_base_name: str):
        if pkmn_name in self.raw_pkmn_sets:
            return self.raw_pkmn_sets[pkmn_name]
        elif pkmn_base_name in self.raw_pkmn_sets:
            return self.raw_pkmn_sets[pkmn_base_name]

        if pkmn_name in pokedex and "baseSpecies" in pokedex[pkmn_name]:
            pkmn_base_species = normalize_name(pokedex[pkmn_name]["baseSpecies"])
            if pkmn_base_species in self.raw_pkmn_sets:
                return self.raw_pkmn_sets[pkmn_base_species]

        if pkmn_name in pokedex and "name" in pokedex[pkmn_name]:
            pkmn_non_cosmetic_name = normalize_name(pokedex[pkmn_name]["name"])
            if pkmn_non_cosmetic_name in self.raw_pkmn_sets:
                return self.raw_pkmn_sets[pkmn_non_cosmetic_name]

        return {}


class _RandomBattleSets(PokemonSets):
    def __init__(self):
        self.raw_pkmn_sets = {}
        self.pkmn_sets = {}
        self.pkmn_mode = "uninitialized"
        self.species_sample_names = []
        self.species_sample_weights = []

    def _load_raw_sets(self, pokemon_battle_mode):
        pokemon_battle_mode = _strip_blitz(pokemon_battle_mode)
        self.raw_pkmn_sets = get_randbats_sets_file(pokemon_battle_mode)
        self._merge_user_observed_sets(pokemon_battle_mode)

    def _merge_user_observed_sets(self, pokemon_battle_mode):
        # sets observed on the bot's own teams supplement the downloaded
        # data in-memory; the downloaded cache file is never modified
        overlay = load_user_observed_sets(pokemon_battle_mode)
        for pkmn_name, observed_sets in overlay.items():
            if pkmn_name.startswith("_") or not isinstance(observed_sets, dict):
                continue
            existing_sets = self.raw_pkmn_sets.setdefault(pkmn_name, {})
            for set_key, count in observed_sets.items():
                existing_sets[set_key] = existing_sets.get(set_key, 0) + count

    def _initialize_pkmn_sets(self):
        # gen9 full-set keys are `level,item,ability,<1-4 moves>,teraType`: the
        # tera type is always the trailing token and the move count is variable
        # (e.g. Ditto is `87,choicescarf,imposter,transform,ghost`). Earlier gens
        # have no tera token and always carry four moves.
        is_gen9 = "gen9" in self.pkmn_mode
        for pkmn, sets in self.raw_pkmn_sets.items():
            self.pkmn_sets[pkmn] = []
            for set_, count in sets.items():
                set_split = set_.split(",")
                level = int(set_split[0])
                item = set_split[1]
                ability = set_split[2]
                if is_gen9:
                    tera_type = set_split[-1]
                    moves = set_split[3:-1]
                    evs, ivs = random_battle_ev_iv_spread(
                        pkmn, moves, ability, item, level
                    )
                else:
                    moves = set_split[3:7]
                    tera_type = set_split[7] if len(set_split) > 7 else None
                    evs, ivs = random_battles_evs(), (31,) * 6
                self.pkmn_sets[pkmn].append(
                    PredictedPokemonSet(
                        pkmn_set=PokemonSet(
                            ability=ability,
                            item=item,
                            nature="serious",
                            evs=evs,
                            ivs=ivs,
                            count=count,
                            tera_type=tera_type,
                            level=level,
                        ),
                        pkmn_moveset=PokemonMoveset(moves=moves),
                    )
                )
            self.pkmn_sets[pkmn].sort(key=lambda x: x.pkmn_set.count, reverse=True)

        # precompute per-species weights for sampling unrevealed pkmn:
        # each species is weighted by the total count of its sets
        self.species_sample_names = list(self.pkmn_sets.keys())
        self.species_sample_weights = [
            sum(s.pkmn_set.count for s in self.pkmn_sets[name])
            for name in self.species_sample_names
        ]

    def initialize(self, pkmn_mode: str, _pkmn_names=None):
        # pkmn_names unused here since randombattles don't have team preview
        # always load entire JSON into memory
        self.raw_pkmn_sets = {}
        self.pkmn_sets = {}
        self.pkmn_mode = pkmn_mode
        self._load_raw_sets(pkmn_mode)
        self._initialize_pkmn_sets()

    def predict_set(
        self, pkmn: Pokemon, match_traits=True
    ) -> Optional[PredictedPokemonSet]:
        if not self.pkmn_sets:
            logger.warning("Called `predict_set` when pkmn_sets was empty")

        for pkmn_set in self.get_pkmn_sets_from_pkmn_name(pkmn):
            if pkmn_set.full_set_pkmn_can_have_set(
                pkmn,
                match_ability=match_traits,
                match_item=match_traits,
                speed_check=False,
                level_check=False,
                tera_check=match_traits,
            ):
                return pkmn_set

        return None

    def get_all_remaining_sets(self, pkmn: Pokemon) -> list[PredictedPokemonSet]:
        if not self.pkmn_sets:
            logger.warning("Called `predict_set` when pkmn_sets was empty")
            return []

        pkmn_sets = self.get_pkmn_sets_from_pkmn_name(pkmn)

        # A TRANSFORMED mon (Ditto/Imposter) carries COPIED moves/ability that
        # match no real set of its base species, so the normal filters return
        # nothing and the true item is never sampled (forensic: transformed
        # Ditto's Choice Scarf missing from every world -> the engine priced a
        # lost priority mirror as a 50/50 speed tie and burned tera on it).
        # Match the base species' sets on item knowledge only.
        if getattr(pkmn, "transformed_into", None):
            remaining_sets = []
            for pkmn_set in pkmn_sets:
                if pkmn_set.mechanics_signature() in getattr(
                    pkmn, "rejected_set_signatures", set()
                ):
                    continue
                set_item = pkmn_set.pkmn_set.item
                # A transformed mon that has LOST its item carries `item == ""`
                # (falsy), not UNKNOWN_ITEM. The old guard treated empty as
                # "known to hold nothing" and required the set's item to be
                # empty too, excluding the true set. `removed_item` records what
                # it actually held -- 100% valid information, and the only
                # evidence available once the item is gone.
                #
                # Measured: this was the dominant support-gap cause (SPEC-B4).
                # Worked example -- transformed Ditto, item "", removed_item
                # choicescarf, true set Choice Scarf, candidate list EMPTY.
                known_item = pkmn.item if pkmn.item else None
                removed_item = getattr(pkmn, "removed_item", None) or None
                if known_item is not None and known_item != constants.UNKNOWN_ITEM:
                    if set_item != known_item:
                        continue
                elif removed_item is not None:
                    # It held `removed_item` before losing it: only sets with
                    # that item are possible. This NARROWS, it does not widen.
                    if set_item != removed_item:
                        continue
                # An item proven impossible stays impossible -- unless it is the
                # very item we watched this mon hold and lose, which is direct
                # observation and outranks any inference.
                if set_item in getattr(pkmn, "impossible_items", set()) and (
                    removed_item is None or set_item != removed_item
                ):
                    continue
                remaining_sets.append(pkmn_set)
            return remaining_sets

        def matching_sets(**checks):
            return [
                pkmn_set
                for pkmn_set in pkmn_sets
                if pkmn_set.full_set_pkmn_can_have_set(pkmn, **checks)
            ]

        remaining_sets = matching_sets(
            match_ability=True,
            match_item=True,
            speed_check=True,
            level_check=True,
            tera_check=True,
        )
        if remaining_sets:
            return remaining_sets

        if os.environ.get(GRADED_EVIDENCE_RELAXATION_CONTROL_OFF):
            # Legacy all-at-once fallback, retained ONLY as the negative control.
            # Graded relaxation is the default because it is strictly better on
            # every measured axis (SPEC-B2): recovery 402/402 with 0 regressions
            # - it never returns empty where all-at-once returned candidates -
            # while mean candidate count falls 20.54 -> 16.30 and mean retained
            # match flags rise 0 -> 2.16.  The path fires on 17.53% of games
            # (27/154), so shipping it dormant would have been a fix nobody runs.
            return matching_sets(
                match_ability=False,
                match_item=False,
                speed_check=False,
                level_check=False,
                tera_check=False,
            )

        # Speed is the weakest inferred evidence. Recorded positions showed
        # that level or tera never recovered first, so keep ability until the
        # final rung and relax the middle group together.
        relaxation_steps = (
            (
                1,
                "speed",
                dict(
                    match_ability=True,
                    match_item=True,
                    speed_check=False,
                    level_check=True,
                    tera_check=True,
                ),
            ),
            (
                2,
                "speed,level,tera,item",
                dict(
                    match_ability=True,
                    match_item=False,
                    speed_check=False,
                    level_check=False,
                    tera_check=False,
                ),
            ),
            (
                3,
                "speed,level,tera,item,ability",
                dict(
                    match_ability=False,
                    match_item=False,
                    speed_check=False,
                    level_check=False,
                    tera_check=False,
                ),
            ),
        )
        for depth, relaxed_constraints, checks in relaxation_steps:
            remaining_sets = matching_sets(**checks)
            if remaining_sets:
                logger.info(
                    "Evidence relaxation for %s recovered %d candidates "
                    "at depth=%d after relaxing=%s",
                    pkmn.name,
                    len(remaining_sets),
                    depth,
                    relaxed_constraints,
                )
                return remaining_sets

        # FINAL RUNG. `mechanics_signature() in rejected_set_signatures` is
        # checked FIRST and UNCONDITIONALLY in `full_set_pkmn_can_have_set`, so
        # the ladder above can never relax it: one wrong damage verdict would
        # otherwise be permanent. Damage evidence must never leave us worse off
        # than not having recorded it at all, so if everything is gone, drop
        # the ledger and try the whole ladder once more.
        rejected = getattr(pkmn, "rejected_set_signatures", None)
        if rejected:
            logger.warning(
                "Evidence relaxation for %s exhausted with %d rejected-set "
                "signatures recorded - clearing them and retrying",
                pkmn.name,
                len(rejected),
            )
            rejected.clear()
            return self.get_all_remaining_sets(pkmn)

        logger.info(
            "Evidence relaxation for %s exhausted at depth=3 after "
            "relaxing=speed,level,tera,item,ability with no candidates",
            pkmn.name,
        )
        return []

    def get_all_possible_moves(self, pkmn: Pokemon):
        if not self.pkmn_sets:
            logger.warning("Called `predict_set` when pkmn_sets was empty")
            return []

        possible_moves = set()
        for pkmn_set in self.get_pkmn_sets_from_pkmn_name(pkmn):
            for mv in pkmn_set.pkmn_moveset.moves:
                possible_moves.add(mv)

        return list(possible_moves)


class _TeamDatasets(PokemonSets):
    def __init__(self):
        self.raw_pkmn_sets = {}
        self.raw_pkmn_moves = {}
        self.pkmn_sets = {}
        self.pkmn_mode = "uninitialized"

    def _get_sets_dict(self):
        ps_sets = get_ps_sets_file(self.pkmn_mode)
        full_sets = get_pkmn_sets_file(self.pkmn_mode, "pokemon_full_sets.json")
        for pkmn, sets in ps_sets.items():
            if pkmn not in full_sets:
                full_sets[pkmn] = sets
            else:
                for set_, count in sets.items():
                    if set_ not in full_sets[pkmn]:
                        full_sets[pkmn][set_] = count
                    else:
                        full_sets[pkmn][set_] += count
        return full_sets

    def _get_moves_dict(self):
        return get_pkmn_sets_file(self.pkmn_mode, "replay_moves.json")

    def _get_battle_factory_sets_dict(self, tier_name):
        return get_pkmn_sets_file(self.pkmn_mode, "factory-sets.json")[tier_name]

    def _load_battle_factory_team_datasets(self, pkmn_names: set[str], tier_name: str):
        sets_dict = self._get_battle_factory_sets_dict(tier_name)
        for pkmn in pkmn_names:
            try:
                self.raw_pkmn_sets[pkmn] = sets_dict[pkmn]
            except KeyError:
                logger.warning("No pokemon sets for {}".format(pkmn))

    def _load_team_datasets(self, pkmn_names: set[str], get_all_pkmn: bool):
        sets_dict = self._get_sets_dict()
        all_pkmn_moves = self._get_moves_dict()
        iter_list = all_pkmn_moves.keys() if get_all_pkmn else pkmn_names
        for pkmn in iter_list:
            if pkmn not in sets_dict:
                sets_dict[pkmn] = {}
            self.raw_pkmn_sets[pkmn] = sets_dict[pkmn]
            self.raw_pkmn_moves[pkmn] = []
            for moves_str, count in all_pkmn_moves.get(pkmn, {}).items():
                moves = moves_str.split("|")
                self.raw_pkmn_moves[pkmn].append(
                    PokemonMoveset(moves=tuple(moves), count=count)
                )

    def _add_to_pkmn_sets(self, raw_sets: dict[str, list]):
        for pkmn, sets in raw_sets.items():
            self.pkmn_sets[pkmn] = []
            for set_, count in sets.items():
                set_split = set_.split("|")
                tera_type = set_split[0] or "typeless"
                ability = set_split[1]
                item = set_split[2]
                nature = set_split[3]
                evs = tuple(int(i) for i in set_split[4].split(","))
                moves = set_split[5:]

                self.pkmn_sets[pkmn].append(
                    PredictedPokemonSet(
                        pkmn_set=PokemonSet(
                            ability=ability,
                            item=item,
                            nature=nature,
                            evs=evs,
                            count=count,
                            tera_type=tera_type,
                        ),
                        pkmn_moveset=PokemonMoveset(moves=moves),
                    )
                )
            self.pkmn_sets[pkmn].sort(key=lambda x: x.pkmn_set.count, reverse=True)

    def initialize(
        self, pkmn_mode: str, pkmn_names: set[str], battle_factory_tier_name=None
    ):
        self.raw_pkmn_sets = {}
        self.pkmn_sets = {}
        self.pkmn_mode = pkmn_mode
        get_all_pkmn = any(
            g in pkmn_mode
            for g in [
                "gen1",
                "gen2",
                "gen3",
                "gen4",
            ]
        )
        if battle_factory_tier_name:
            self._load_battle_factory_team_datasets(
                pkmn_names, battle_factory_tier_name
            )
        else:
            self._load_team_datasets(pkmn_names, get_all_pkmn)
        self._add_to_pkmn_sets(self.raw_pkmn_sets)

    def add_new_pokemon(self, pkmn_name: str):
        sets_dict = self._get_sets_dict()
        all_pkmn_moves = self._get_moves_dict()
        if pkmn_name not in sets_dict:
            return
        self.raw_pkmn_moves[pkmn_name] = []
        for moves_str, count in all_pkmn_moves.get(pkmn_name, {}).items():
            moves = moves_str.split("|")
            self.raw_pkmn_moves[pkmn_name].append(
                PokemonMoveset(moves=tuple(moves), count=count)
            )
        self._add_to_pkmn_sets({pkmn_name: sets_dict[pkmn_name]})

    def get_all_remaining_sets(self, pkmn: Pokemon) -> list[PredictedPokemonSet]:
        if not self.pkmn_sets:
            return []

        remaining_sets = []
        for pkmn_set in self.get_pkmn_sets_from_pkmn_name(pkmn):
            if pkmn_set.full_set_pkmn_can_have_set(
                pkmn,
                match_ability=True,
                match_item=True,
                speed_check=True,
                tera_check=True,
            ):
                remaining_sets.append(pkmn_set)

        # do not do this extra check for TeamDatasets unless in battlefactory mode
        if not remaining_sets and self.pkmn_mode.endswith("battlefactory"):
            for pkmn_set in self.get_pkmn_sets_from_pkmn_name(pkmn):
                if pkmn_set.full_set_pkmn_can_have_set(
                    pkmn,
                    match_ability=False,
                    match_item=False,
                    speed_check=False,
                    tera_check=False,
                ):
                    remaining_sets.append(pkmn_set)

        return remaining_sets

    def get_all_possible_move_combinations(self, pkmn: Pokemon, pkmn_set: PokemonSet):
        valid_movesets = []
        for pkmn_moveset in self.get_key_in_dict_from_pkmn_name(
            pkmn.name, pkmn.base_name, pkmn.mega_name, self.raw_pkmn_moves
        ):
            if PredictedPokemonSet(
                pkmn_set=pkmn_set, pkmn_moveset=pkmn_moveset
            ).full_set_pkmn_can_have_set(pkmn):
                valid_movesets.append(pkmn_moveset)

        return valid_movesets

    def get_all_possible_moves(self, pkmn: Pokemon):
        if not self.pkmn_sets:
            logger.warning("Called `predict_set` when pkmn_sets was empty")
            return []

        possible_moves = set()
        for pkmn_set in self.get_pkmn_sets_from_pkmn_name(pkmn):
            for mv in pkmn_set.pkmn_moveset.moves:
                possible_moves.add(mv)

        return list(possible_moves)

    def predict_set(
        self, pkmn: Pokemon, match_traits=True
    ) -> Optional[PredictedPokemonSet]:
        for pkmn_set in self.get_pkmn_sets_from_pkmn_name(pkmn):
            if pkmn_set.full_set_pkmn_can_have_set(
                pkmn,
                match_ability=match_traits,
                match_item=match_traits,
                speed_check=True,
                tera_check=match_traits,
            ):
                return pkmn_set

        return None


class _SmogonSets(PokemonSets):
    def __init__(self):
        self.current_pkmn_sets_url = ""
        self.raw_pkmn_sets = {}
        self.all_pkmn_counts = {}
        self.pkmn_sets = {}
        self.pkmn_mode = "uninitialized"

    def _smogon_predicted_move_set_makes_sense(
        self, predicted_set: PredictedPokemonSet
    ):
        has_hiddenpower = False
        for mv in predicted_set.pkmn_moveset.moves:
            # only 1 hiddenpower in a moveset
            if mv.startswith(constants.HIDDEN_POWER) and has_hiddenpower:
                return False
            elif mv.startswith(constants.HIDDEN_POWER):
                has_hiddenpower = True

            # dont pick certain moves with choice items
            if predicted_set.pkmn_set.item in constants.CHOICE_ITEMS:
                if all_move_json[mv][
                    constants.CATEGORY
                ] not in constants.DAMAGING_CATEGORIES and mv not in [
                    "trick",
                    "switcheroo",
                ]:
                    return False
        return True

    def _pokemon_is_similar(self, normalized_name, list_of_pkmn_names):
        return any(normalized_name.startswith(n) for n in list_of_pkmn_names) or any(
            n.startswith(normalized_name) for n in list_of_pkmn_names
        )

    def _get_smogon_stats_json(self, smogon_stats_url):
        cache_file_name = ntpath.basename(smogon_stats_url)
        cache_file = os.path.join(SMOGON_CACHE_DIR, cache_file_name)
        if os.path.exists(cache_file):
            with open(cache_file, "r") as f:
                infos = json.load(f)
        else:
            r = requests.get(smogon_stats_url)
            if r.status_code == 404:
                r = requests.get(
                    self._get_smogon_stats_file_name(
                        ntpath.basename(smogon_stats_url.replace("-0.json", "")),
                        month_delta=2,
                    )
                )
            infos = r.json()["data"]
            with open(cache_file, "w") as f:
                json.dump(infos, f)

        return infos

    def _get_pokemon_information(self, smogon_stats_url, pkmn_names) -> dict:
        infos = self._get_smogon_stats_json(smogon_stats_url)
        self.all_pkmn_counts.clear()

        final_infos = {}
        for pkmn_name, pkmn_information in infos.items():
            normalized_name = normalize_name(pkmn_name)
            self.all_pkmn_counts[normalized_name] = {}
            self.all_pkmn_counts[normalized_name][RAW_COUNT] = pkmn_information[
                "Raw count"
            ]
            self.all_pkmn_counts[normalized_name][TEAMMATES] = {}
            for teammate_name, teammate_count in pkmn_information["Teammates"].items():
                self.all_pkmn_counts[normalized_name][TEAMMATES][
                    normalize_name(teammate_name)
                ] = teammate_count

            # if `pkmn_names` is provided, only find data on pkmn in that list
            if (
                pkmn_names
                and normalized_name not in pkmn_names
                and not self._pokemon_is_similar(normalized_name, pkmn_names)
            ):
                continue
            else:
                logger.debug(
                    "Adding {} to sets lookup for this battle".format(normalized_name)
                )

            spreads = []
            items = []
            moves = []
            abilities = []
            tera_types = []
            matchup_effectiveness = {}
            total_count = pkmn_information["Raw count"]
            final_infos[normalized_name] = {}

            for counter_name, counter_information in pkmn_information[
                "Checks and Counters"
            ].items():
                counter_name = normalize_name(counter_name)
                if counter_name in pkmn_names:
                    matchup_effectiveness[counter_name] = round(
                        1 - counter_information["p"], 2
                    )

            for spread, count in sorted(
                pkmn_information["Spreads"].items(), key=lambda x: x[1], reverse=True
            ):
                percentage = count / total_count
                if percentage > 0:
                    nature, evs = [normalize_name(i) for i in spread.split(":")]
                    evs = evs.replace("/", ",")
                    for sp in spreads:
                        if spreads_are_alike(sp, (nature, evs)):
                            sp[2] += percentage
                            break
                    else:
                        spreads.append([nature, evs, percentage])

            for item, count in pkmn_information["Items"].items():
                if count > 0:
                    items.append((item, count / total_count))

            for move, count in pkmn_information["Moves"].items():
                if count > 0 and move and move.lower() != "nothing":
                    if move.startswith(constants.HIDDEN_POWER):
                        move = f"{move}{constants.HIDDEN_POWER_ACTIVE_MOVE_BASE_DAMAGE_STRING}"
                    moves.append((move, count / total_count))

            for ability, count in pkmn_information["Abilities"].items():
                if count > 0:
                    abilities.append((ability, count / total_count))

            for tera_type, count in pkmn_information["Tera Types"].items():
                if tera_type == "nothing":
                    tera_type = "typeless"
                if count > 0:
                    tera_types.append((tera_type, count / total_count))

            final_infos[normalized_name][SPREADS_STRING] = sorted(
                spreads, key=lambda x: x[2], reverse=True
            )[:20]
            final_infos[normalized_name][ITEM_STRING] = sorted(
                items, key=lambda x: x[1], reverse=True
            )[:10]
            final_infos[normalized_name][MOVES_STRING] = sorted(
                moves, key=lambda x: x[1], reverse=True
            )[:100]
            final_infos[normalized_name][ABILITY_STRING] = sorted(
                abilities, key=lambda x: x[1], reverse=True
            )
            final_infos[normalized_name][TERA_TYPE_STRING] = sorted(
                tera_types, key=lambda x: x[1], reverse=True
            )[:6]
            final_infos[normalized_name][EFFECTIVENESS] = matchup_effectiveness

        return final_infos

    def _get_smogon_stats_file_name(self, game_mode, month_delta=1):
        """
        Gets the smogon stats url based on the game mode
        Uses the previous-month's statistics
        """

        if game_mode.endswith("blitz"):
            game_mode = game_mode[:-5]

        # always use the `-0` file - the higher ladder is for noobs
        smogon_url = "https://www.smogon.com/stats/{}-{}/chaos/{}-0.json"

        previous_month = datetime.now() - relativedelta.relativedelta(
            months=month_delta
        )
        year = previous_month.year
        month = "{:02d}".format(previous_month.month)

        return smogon_url.format(year, month, game_mode)

    def _pokemon_set_makes_sense(self, pkmn_set: PokemonSet):
        # Without a large amount in the supporting stat choice items don't make sense
        if pkmn_set.item == "choiceband" and pkmn_set.evs[1] < 204:
            return False
        if pkmn_set.item == "choicespecs" and pkmn_set.evs[3] < 204:
            return False
        if pkmn_set.item == "choicescarf" and pkmn_set.evs[5] < 204:
            return False

        # without a large amount in an offensive stat life orb and expert belt don't make sense
        if pkmn_set.item in ["lifeorb", "expertbelt"] and (
            pkmn_set.evs[1] < 200 and pkmn_set.evs[3] < 200
        ):
            return False

        return True

    def _initialize(self, raw_pkmn_sets: dict):
        for pkmn, sets in raw_pkmn_sets.items():
            self.pkmn_sets[pkmn] = []
            for spread in sets[SPREADS_STRING]:
                for ability in sets[ABILITY_STRING]:
                    for item in sets[ITEM_STRING]:
                        for tera_type in sets[TERA_TYPE_STRING]:
                            pkmn_set = PokemonSet(
                                ability=ability[0],
                                item=item[0],
                                nature=spread[0],
                                evs=tuple(int(i) for i in spread[1].split(",")),
                                tera_type=tera_type[0],
                                count=(ability[1] * item[1] * spread[2] * tera_type[1]),
                            )
                            if self._pokemon_set_makes_sense(pkmn_set):
                                self.pkmn_sets[pkmn].append(pkmn_set)
            self.pkmn_sets[pkmn].sort(key=lambda x: x.count, reverse=True)

    def initialize(self, pkmn_mode: str, pkmn_names: set[str]):
        self.pkmn_mode = pkmn_mode
        smogon_stats_url = self._get_smogon_stats_file_name(pkmn_mode)
        if self.current_pkmn_sets_url != smogon_stats_url:
            self.raw_pkmn_sets = self._get_pokemon_information(
                smogon_stats_url, pkmn_names
            )
            self.current_pkmn_sets_url = smogon_stats_url
        else:
            new_pkmn_names = [p for p in pkmn_names if p not in self.raw_pkmn_sets]
            if new_pkmn_names:
                self.raw_pkmn_sets = self._get_pokemon_information(
                    smogon_stats_url, pkmn_names
                )

        self._initialize(self.raw_pkmn_sets)

    def add_new_pokemon(self, pkmn_name: str):
        pkmn_information = self._get_pokemon_information(
            self.current_pkmn_sets_url, {pkmn_name}
        )
        self.raw_pkmn_sets.update(pkmn_information)
        self._initialize(pkmn_information)

    def get_all_remaining_sets(self, pkmn: Pokemon) -> list[PredictedPokemonSet]:
        if not self.pkmn_sets:
            logger.warning("Called `predict_set` when pkmn_sets was empty")
            return []

        remaining_sets = []
        for pkmn_set in self.get_pkmn_sets_from_pkmn_name(pkmn):
            if pkmn_set.set_makes_sense(
                pkmn,
            ):
                remaining_sets.append(pkmn_set)

        if not remaining_sets:
            for pkmn_set in self.get_pkmn_sets_from_pkmn_name(pkmn):
                if pkmn_set.set_makes_sense(
                    pkmn,
                    match_ability=False,
                    match_item=False,
                    match_tera=False,
                    speed_check=False,
                ):
                    remaining_sets.append(pkmn_set)

        return remaining_sets

    def predict_set(
        self, pkmn: Pokemon, num_predicted_moves=4, match_traits=True
    ) -> Optional[PredictedPokemonSet]:
        if not self.pkmn_sets:
            logger.warning("Called `predict_set` when pkmn_sets was empty")

        pokemon_set = None
        for pkmn_set in self.get_pkmn_sets_from_pkmn_name(pkmn):
            if pkmn_set.set_makes_sense(pkmn, match_traits):
                pokemon_set = pkmn_set
                break

        if pokemon_set is None:
            return None

        predicted_pokemon_set = PredictedPokemonSet(
            pkmn_set=pokemon_set,
            pkmn_moveset=PokemonMoveset(moves=tuple(m.name for m in pkmn.moves)),
        )

        if pkmn.get_move(constants.HIDDEN_POWER) is not None:
            hidden_power_possibilities = [
                f"{constants.HIDDEN_POWER}{p}{constants.HIDDEN_POWER_ACTIVE_MOVE_BASE_DAMAGE_STRING}"
                for p in pkmn.hidden_power_possibilities
            ]
            for mv, _count in self.get_raw_pkmn_sets_from_pkmn_name(
                pkmn.name, pkmn.base_name
            )[MOVES_STRING]:
                if mv in hidden_power_possibilities:
                    predicted_pokemon_set.pkmn_moveset.remove_move("hiddenpower")
                    predicted_pokemon_set.pkmn_moveset.add_move(mv)
                    break

        for mv, _count in self.get_raw_pkmn_sets_from_pkmn_name(
            pkmn.name, pkmn.base_name
        )[MOVES_STRING]:
            if len(predicted_pokemon_set.pkmn_moveset.moves) >= num_predicted_moves:
                break

            if mv in predicted_pokemon_set.pkmn_moveset.moves:
                continue

            predicted_pokemon_set.pkmn_moveset.add_move(mv)
            if not self._smogon_predicted_move_set_makes_sense(predicted_pokemon_set):
                predicted_pokemon_set.pkmn_moveset.remove_move(mv)

        return predicted_pokemon_set


TeamDatasets = _TeamDatasets()
RandomBattleTeamDatasets = _RandomBattleSets()
SmogonSets = _SmogonSets()
