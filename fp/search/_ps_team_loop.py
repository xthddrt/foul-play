"""
PORT GROUP 1 -- the sequential team loop of Pokemon Showdown's gen9randombattle
team generator.  Transliterated from (READ-ONLY ground truth):

    pokemon-showdown/data/random-battles/gen9/teams.ts
        sampleIfArray            276-282
        random / sample          270-286
        fastPop / sampleNoReplace 290-315
        getForme                 1448-1471
        getPokemonPool           1614-1646
        getPokemonCompatibility  1652-1722
        randomTeam               1728-1935

SINGLES ONLY (gen9randombattle).  Every `isDoubles` branch is kept in the port
for line-for-line fidelity but is dead code at is_doubles=False.

--------------------------------------------------------------------------
CONTRACT WITH THE OTHER PORT GROUPS  (we all merge into fp/search/ps_teams.py)
--------------------------------------------------------------------------
THIS FILE **CALLS** these two, which Groups 2/3 must define in the merged
module with exactly these names and signatures:

    def random_set(species: Species,
                   team_details: dict,
                   is_lead: bool = False,
                   is_doubles: bool = False) -> dict
        # teams.ts:1471-1611.  Returns a dict with AT LEAST these keys, which
        # the team loop reads:
        #   'speciesId': str   PS id of the *base* species (species.id)
        #   'species'  : str   the forme name from get_forme()  (display name)
        #   'name'     : str   species.baseSpecies
        #   'level'    : int
        #   'ability'  : str   DISPLAY name, e.g. 'Drizzle', 'Sand Stream'
        #   'moves'    : list[str]  move IDs, e.g. 'stealthrock', 'raindance'
        #   'role'     : str   e.g. 'Tera Blast user', 'Bulky Support'
        # (plus item / teraType / evs / ivs, which the loop does not read)
        #
        # NOTE for Group 2: the teamDetails.teraBlast check at teams.ts:1492
        # lives inside YOUR function; the loop only supplies `team_details`.
        # The complementary species-level rejection (teams.ts:1765) is ported
        # here.

    def get_level(species: Species, is_doubles: bool = False) -> int
        # teams.ts:1420-1446.  Called by the loop for the level-100 cap
        # (teams.ts:1827) BEFORE random_set runs, exactly as PS does.

THIS FILE **PROVIDES** (Groups 2/3 should use these rather than redefining):

    to_id(text) -> str                       PS toID()
    get_species(name_or_id) -> Species       PS this.dex.species.get()
    get_effectiveness(type_name, species)    PS this.dex.getEffectiveness()
    get_forme(species) -> str                teams.ts:1448
    seed(n)                                  seed the module RNG
    random_int(m=None, n=None) -> int        PS this.random()
    random_chance(num, den) -> bool          PS this.randomChance()
    sample(items)                            PS this.sample()
    sample_if_array(item)                    PS this.sampleIfArray()
    sample_no_replace(lst)                   PS this.sampleNoReplace()  (mutates)
    fast_pop(lst, i)                         PS this.fastPop()          (mutates)
    shuffle(lst)                             PS prng.shuffle()          (mutates)
    RANDOM_SETS                              parsed gen9 sets.json
    TYPE_NAMES                               PS this.dex.types.names() (see note)
    random_team() -> list[dict]              teams.ts:1728

ASSUMPTIONS / TRADEOFFS (stated explicitly):
  * RNG sequence deliberately does NOT match PS's PRNG -- only the
    distribution is claimed identical.  `random.Random` is used.
  * gen9randombattle rule table is hardcoded: maxTeamSize=6, adjustLevel=None,
    forceMonotype=None, no sametypeclause / pickedteamsize / teampreview /
    terastalclause, so leadsRemaining starts at 1.  PotD is in the ruleset but
    global.Config.potd is unset on a stock server, so potd is None.
  * Species data comes from foul-play/data/pokedex.json (types, abilities,
    baseSpecies, otherFormes, cosmeticFormes, battleOnly).  `tier` is NOT in
    that file; gen9 get_level does not need it.
  * Type effectiveness is computed from foul-play/fp/helpers.py's
    DAMAGE_MULTIPICATION_ARRAY, converted back to PS's log-scale +1/-1 sum
    per defending type (2x -> +1, 0.5x -> -1, 1x and 0x -> 0).  Immunity
    contributing 0 -- not -inf -- is exactly what PS does (damageTaken code 3),
    which is why e.g. Gliscor counts as Electric-weak.
  * PS's dex.types.names() includes 'Stellar'; its damageTaken row is all 0s so
    it contributes 0 to every getEffectiveness call.  It is omitted from
    TYPE_NAMES; the weakness loops are unaffected.
  * sets.json is read from the read-only pokemon-showdown checkout (nothing is
    written there).  Override with the PS_SETS_JSON env var.
"""

from __future__ import annotations

import json
import os
import random as _pyrandom
import re

from fp.helpers import DAMAGE_MULTIPICATION_ARRAY, POKEMON_TYPE_INDICES

# --------------------------------------------------------------------------
# data paths
# --------------------------------------------------------------------------

_HERE = os.path.dirname(os.path.abspath(__file__))
_FP_ROOT = os.path.dirname(os.path.dirname(_HERE))  # .../foul-play

# Resolution order: explicit env var; the live pokemon-showdown checkout
# (ground truth, tracks PS updates); the copy VENDORED into this repo so a
# bare foul-play+poke-engine clone still runs the PS-exact sampler instead of
# silently falling back to the marginal one. The vendored copy is byte-equal
# to the checkout as of 2026-08-15 (sets.json unchanged since 2026-08-06).
_PS_SETS_CANDIDATES = (
    os.path.join(
        os.path.dirname(_FP_ROOT),
        "pokemon-showdown",
        "data",
        "random-battles",
        "gen9",
        "sets.json",
    ),
    os.path.join(_FP_ROOT, "data", "ps", "gen9randombattle_sets.json"),
)
PS_SETS_JSON = os.environ.get("PS_SETS_JSON") or next(
    (p for p in _PS_SETS_CANDIDATES if os.path.isfile(p)),
    _PS_SETS_CANDIDATES[0],
)
PS_POKEDEX_JSON = os.path.join(_FP_ROOT, "data", "pokedex.json")

with open(PS_SETS_JSON) as _f:
    RANDOM_SETS = json.load(_f)

with open(PS_POKEDEX_JSON) as _f:
    _POKEDEX = json.load(_f)


# --------------------------------------------------------------------------
# PS toID  (sim/dex-data.ts)
# --------------------------------------------------------------------------

_NON_ID = re.compile(r"[^a-z0-9]+")


def to_id(text) -> str:
    if text is None:
        return ""
    sid = getattr(text, "id", None)
    if sid is not None:
        return sid
    if isinstance(text, dict):
        text = text.get("name", "")
    text = str(text)
    # memoized: called ~150k times per team build on a small recurring set of
    # strings; the regex is pure so this cannot change any result
    cached = _ID_MEMO.get(text)
    if cached is None:
        cached = _NON_ID.sub("", text.lower())
        _ID_MEMO[text] = cached
    return cached


_ID_MEMO: dict[str, str] = {}


# --------------------------------------------------------------------------
# Species  (a thin stand-in for PS's Species object)
# --------------------------------------------------------------------------

# foul-play's pokedex.json lowercases the `name` field, but every *other* field
# that names a species (baseSpecies, otherFormes, cosmeticFormes, formeOrder,
# prevo, evos, changesFrom, battleOnly) keeps PS's display casing.  Harvest
# those to recover display names; fall back to title-casing.
_DISPLAY_NAMES: dict[str, str] = {}
for _entry in _POKEDEX.values():
    for _field in ("baseSpecies", "battleOnly", "changesFrom", "prevo"):
        _v = _entry.get(_field)
        if isinstance(_v, str):
            _DISPLAY_NAMES.setdefault(to_id(_v), _v)
    for _field in ("otherFormes", "cosmeticFormes", "formeOrder", "evos"):
        for _v in _entry.get(_field) or []:
            _DISPLAY_NAMES.setdefault(to_id(_v), _v)


def _display_name(species_id: str, raw_name: str) -> str:
    if species_id in _DISPLAY_NAMES:
        return _DISPLAY_NAMES[species_id]
    # e.g. "ho-oh" -> "Ho-Oh", "mr. mime" -> "Mr. Mime", "type: null" -> "Type: Null"
    return re.sub(r"[A-Za-z0-9']+", lambda m: m.group(0).capitalize(), raw_name)


class Species:
    """Subset of PS's Species that the random-battle generator touches."""

    __slots__ = (
        "id",
        "name",
        "baseSpecies",
        "forme",
        "types",
        "abilities",
        "baseStats",
        "battleOnly",
        "cosmeticFormes",
        "otherFormes",
        "requiredItem",
        "gender",
        "genderRatio",
        "tier",
        "exists",
        "raw",
    )

    def __init__(self, species_id: str, entry: dict):
        self.id = species_id
        self.raw = entry
        self.exists = bool(entry)
        raw_name = entry.get("name", species_id)
        self.name = _display_name(species_id, raw_name)
        base = entry.get("baseSpecies")
        self.baseSpecies = base if base else self.name
        self.forme = entry.get("forme", "")
        # PS type names are capitalised ('Grass'); foul-play stores 'grass'.
        self.types = [t.capitalize() for t in entry.get("types", [])]
        self.abilities = entry.get("abilities", {})
        self.baseStats = entry.get("baseStats", {})
        self.battleOnly = entry.get("battleOnly")
        self.cosmeticFormes = entry.get("cosmeticFormes")
        self.otherFormes = entry.get("otherFormes")
        self.requiredItem = entry.get("requiredItem")
        self.gender = entry.get("gender")
        self.genderRatio = entry.get("genderRatio")
        self.tier = entry.get("tier", "")

    def __repr__(self):
        return f"<Species {self.name}>"


_SPECIES_CACHE: dict[str, Species] = {}


def get_species(name_or_id) -> Species:
    """PS this.dex.species.get()."""
    if isinstance(name_or_id, Species):
        return name_or_id
    sid = to_id(name_or_id)
    sp = _SPECIES_CACHE.get(sid)
    if sp is None:
        sp = Species(sid, _POKEDEX.get(sid, {}))
        _SPECIES_CACHE[sid] = sp
    return sp


# --------------------------------------------------------------------------
# type effectiveness  (PS this.dex.getEffectiveness)
# --------------------------------------------------------------------------

TYPE_NAMES = [
    "Normal", "Fire", "Water", "Electric", "Grass", "Ice", "Fighting",
    "Poison", "Ground", "Flying", "Psychic", "Bug", "Rock", "Ghost",
    "Dragon", "Dark", "Steel", "Fairy",
]


def get_effectiveness(source_type: str, species) -> int:
    """PS dex.getEffectiveness(type, species): log-scale sum over defending types.

    Per defending type: super effective -> +1, resisted -> -1, neutral OR
    immune -> 0 (PS damageTaken codes 1 / 2 / 0 and 3).
    """
    if isinstance(species, Species):
        types = species.types
    elif isinstance(species, (list, tuple)):
        types = species
    else:
        types = get_species(species).types
    ai = POKEMON_TYPE_INDICES[source_type.lower()]
    total = 0
    for t in types:
        mult = DAMAGE_MULTIPICATION_ARRAY[ai][POKEMON_TYPE_INDICES[t.lower()]]
        if mult == 2:
            total += 1
        elif mult == 0.5:
            total -= 1
    return total


# --------------------------------------------------------------------------
# RNG helpers  (sim/prng.ts + teams.ts:270-315)
# The sequence deliberately differs from PS; the distribution does not.
# --------------------------------------------------------------------------

_PRNG = _pyrandom.Random()


def seed(n) -> None:
    _PRNG.seed(n)


def random_int(m=None, n=None) -> int:
    """PS prng.random(m, n): [0,m) with one arg, [m,n) with two, [0,1) float-free."""
    if m is None:
        return _PRNG.random()
    if n is None:
        return _PRNG.randrange(m)
    return _PRNG.randrange(m, n)


def random_chance(numerator: int, denominator: int) -> bool:
    return random_int(denominator) < numerator


def sample(items):
    if len(items) == 0:
        raise ValueError("Cannot sample an empty array")
    return items[random_int(len(items))]


def sample_if_array(item):
    if isinstance(item, list):
        return sample(item)
    return item


def fast_pop(lst, index):
    length = len(lst)
    if index < 0 or index >= length:
        raise IndexError(f"Index {index} out of bounds for given array")
    element = lst[index]
    lst[index] = lst[length - 1]
    lst.pop()
    return element


def sample_no_replace(lst):
    length = len(lst)
    if length == 0:
        return None
    index = random_int(length)
    return fast_pop(lst, index)


def shuffle(lst):
    _PRNG.shuffle(lst)
    return lst


# --------------------------------------------------------------------------
# teams.ts:1448-1471  getForme
# --------------------------------------------------------------------------

def get_forme(species: Species) -> str:
    if isinstance(species.battleOnly, str):
        # Only change the forme. The species has custom moves, and may have
        # different typing and requirements.
        return species.battleOnly
    if species.cosmeticFormes:
        return sample([species.name] + list(species.cosmeticFormes))
    if species.name.endswith("-Gmax"):
        return species.name[:-5]

    # Consolidate mostly-cosmetic formes, at least for Random Battles
    if species.baseSpecies in ("Dudunsparce", "Maushold", "Polteageist", "Sinistcha", "Zarude"):
        return sample([species.name] + list(species.otherFormes or []))
    if species.baseSpecies == "Basculin":
        return "Basculin" + sample(["", "-Blue-Striped"])
    if species.baseSpecies == "Magearna":
        return "Magearna" + sample(["", "-Original"])
    # Keldeo branch is gen <= 7 only; gen 9 skips it.
    if species.baseSpecies == "Pikachu":  # gen >= 8, non-champions mod
        return "Pikachu" + sample(
            ["", "-Original", "-Hoenn", "-Sinnoh", "-Unova", "-Kalos", "-Alola",
             "-Partner", "-World"]
        )
    return species.name


# --------------------------------------------------------------------------
# teams.ts:1614-1646  getPokemonPool
# --------------------------------------------------------------------------

# (forme_id, baseSpecies) pairs per pokemon_list, resolved once: the
# get_species/to_id sweep over all 509 pool entries was ~1/3 of a team build
# and is invariant across calls. Pure lookup, so caching cannot change output.
_POOL_PAIRS_CACHE: dict[tuple, list] = {}


def _pool_pairs(pokemon_list):
    key = tuple(pokemon_list)
    pairs = _POOL_PAIRS_CACHE.get(key)
    if pairs is None:
        pairs = [(p, get_species(p).baseSpecies) for p in pokemon_list]
        _POOL_PAIRS_CACHE[key] = pairs
    return pairs


def get_pokemon_pool(type_name, pokemon_to_exclude=None, is_monotype=False, pokemon_list=None):
    pokemon_to_exclude = pokemon_to_exclude or []
    exclude = {to_id(p["species"]) for p in pokemon_to_exclude}
    pokemon_pool: dict[str, list[str]] = {}
    base_species_pool: list[str] = []
    if not is_monotype:
        # fast path off the cached pairs -- same iteration order, same output
        for pokemon, base_species in _pool_pairs(pokemon_list):
            if pokemon in exclude:
                continue
            if base_species in pokemon_pool:
                pokemon_pool[base_species].append(pokemon)
            else:
                pokemon_pool[base_species] = [pokemon]
        return _weight_base_species(pokemon_pool)
    for pokemon in pokemon_list:
        species = get_species(pokemon)
        if species.id in exclude:
            continue
        if is_monotype:
            if type_name not in species.types:
                continue
            if isinstance(species.battleOnly, str):
                species = get_species(species.battleOnly)
                if type_name not in species.types:
                    continue

        if species.baseSpecies in pokemon_pool:
            pokemon_pool[species.baseSpecies].append(pokemon)
        else:
            pokemon_pool[species.baseSpecies] = [pokemon]

    return _weight_base_species(pokemon_pool)


def _weight_base_species(pokemon_pool):
    base_species_pool: list[str] = []
    # Include base species 1x if 1-3 formes, 2x if 4-6 formes, 3x if 7+ formes
    for base_species in pokemon_pool:
        # Squawkabilly has 4 formes but only 2 functionally different ones -> 1x
        weight = 1 if base_species == "Squawkabilly" else min(
            -(-len(pokemon_pool[base_species]) // 3), 3
        )
        for _ in range(weight):
            base_species_pool.append(base_species)
    return pokemon_pool, base_species_pool


# --------------------------------------------------------------------------
# teams.ts:1652-1722  getPokemonCompatibility
# --------------------------------------------------------------------------

_WEB_SETTERS = [
    "ariados", "smeargle", "masquerain", "kricketune", "leavanny", "galvantula",
    "vikavolt", "ribombee", "araquanid", "spidops",
]
_SCREEN_SETTERS = ["meowstic", "grimmsnarl", "ninetalesalola", "abomasnow"]

_DOUBLES_WEB_SETTERS = [
    "ariados", "kricketune", "leavanny", "galvantula", "vikavolt", "araquanid", "spidops",
]
_DOUBLES_SCREEN_SETTERS = ["meowstic", "klefki", "grimmsnarl", "ninetalesalola", "abomasnow"]

_SUN_SETTERS = ["ninetales", "torkoal", "groudon", "koraidon"]
_RAIN_SETTERS = ["politoed", "pelipper", "kyogre"]
_SAND_SETTERS = ["tyranitar", "hippowdon"]
_SNOW_SETTERS = ["ninetalesalola", "abomasnow"]

_INCOMPATIBLE_POKEMON = [
    # These Pokemon with support roles are considered too similar to each other.
    ("blissey", "chansey"),
    ("illumise", "volbeat"),
    # These combinations are prevented to avoid double webs or screens.
    (_WEB_SETTERS, _WEB_SETTERS),
    (_SCREEN_SETTERS, _SCREEN_SETTERS),
    # Prevent Dry Skin + sun setting ability
    ("toxicroak", _SUN_SETTERS),
]

_DOUBLES_INCOMPATIBLE_POKEMON = [
    ("illumise", "volbeat"),
    (["minun", "plusle", "pachirisu", "raichu"], ["minun", "plusle", "pachirisu", "raichu"]),
    (_DOUBLES_WEB_SETTERS, _DOUBLES_WEB_SETTERS),
    (_DOUBLES_SCREEN_SETTERS, _DOUBLES_SCREEN_SETTERS),
    ("toxicroak", _SUN_SETTERS),
    (_SUN_SETTERS, _RAIN_SETTERS + _SAND_SETTERS + _SNOW_SETTERS),
    (_RAIN_SETTERS, _SAND_SETTERS + _SNOW_SETTERS),
    (_SAND_SETTERS, _SNOW_SETTERS),
    (["pincurchin", "miraidon"], ["indeedee", "indeedeef", "rillaboom", "arboliva"]),
    (["rillaboom", "arboliva"], ["indeedee", "indeedeef"]),
]


def get_pokemon_compatibility(species: Species, pokemon, is_doubles=False) -> bool:
    """Checks if the new species is compatible with the other mons on the team."""
    incompatibility_list = _DOUBLES_INCOMPATIBLE_POKEMON if is_doubles else _INCOMPATIBLE_POKEMON
    for pair in incompatibility_list:
        mons_a = pair[0] if isinstance(pair[0], list) else [pair[0]]
        mons_b = pair[1] if isinstance(pair[1], list) else [pair[1]]
        if species.id in mons_b:
            if any(m["speciesId"] in mons_a for m in pokemon):
                return False
        if species.id in mons_a:
            if any(m["speciesId"] in mons_b for m in pokemon):
                return False
    return True


# --------------------------------------------------------------------------
# teams.ts:140-142
# --------------------------------------------------------------------------

NO_LEAD_POKEMON = ["Zacian", "Zamazenta"]
DOUBLES_NO_LEAD_POKEMON = [
    "Basculegion", "Houndstone", "Iron Bundle", "Roaring Moon", "Zacian", "Zamazenta",
]


# --------------------------------------------------------------------------
# teams.ts:1728-1935  randomTeam
# --------------------------------------------------------------------------

MAX_TEAM_SIZE = 6
ADJUST_LEVEL = None
FORCE_MONOTYPE = None


def _team_building_species(species_id: str) -> "Species":
    """The species PS's randomTeam would have DRAWN to produce this mon.

    Revealed opponent mons can be in a battle-only forme (Zacian-Crowned,
    Terapagos-Terastal) that never appears in the team loop; the counters must
    be seeded from the species the generator actually drew, exactly as
    teams.ts reads species.types of the drawn species."""
    sp = get_species(species_id)
    battle_only = sp.battleOnly
    if isinstance(battle_only, str):
        return get_species(battle_only)
    if isinstance(battle_only, list) and battle_only:
        return get_species(battle_only[0])
    return sp


def _seed_from_existing(existing, base_formes, type_count, type_combo_count,
                        type_weaknesses, type_double_weaknesses, team_details):
    """Replay randomTeam's post-accept counter block for already-revealed mons.

    Mirrors the "Now that our Pokemon has passed all checks" block below,
    line for line, so a continuation build obeys exactly the constraints a
    from-scratch build would. `existing` entries carry speciesId / ability /
    moves / level; ability may be a display name or a normalized id (compared
    through to_id), and role is unknown for a revealed mon, so the Tera Blast
    team detail falls back to the move itself.

    Returns num_max_level_pokemon."""
    num_max_level = 0
    for e in existing:
        species = _team_building_species(e["speciesId"])
        if not species.exists:
            continue
        base_formes[species.baseSpecies] = 1
        for t in species.types:
            type_count[t] = type_count.get(t, 0) + 1
        type_combo = ",".join(sorted(species.types))
        type_combo_count[type_combo] = type_combo_count.get(type_combo, 0) + 1
        for t in TYPE_NAMES:
            if get_effectiveness(t, species) > 0:
                type_weaknesses[t] = type_weaknesses.get(t, 0) + 1
            if get_effectiveness(t, species) > 1:
                type_double_weaknesses[t] = type_double_weaknesses.get(t, 0) + 1
        ability = to_id(e.get("ability") or "")
        if ability in ("dryskin", "fluffy") and get_effectiveness("Fire", species) == 0:
            type_weaknesses["Fire"] = type_weaknesses.get("Fire", 0) + 1
        if (get_effectiveness("Ice", species) > 0 or
                (get_effectiveness("Ice", species) > -2 and "Water" in species.types)):
            type_weaknesses["Freeze-Dry"] = type_weaknesses.get("Freeze-Dry", 0) + 1
        if e.get("level") == 100:
            num_max_level += 1

        moves = set(e.get("moves") or ())
        if ability == "drizzle" or "raindance" in moves:
            team_details["rain"] = 1
        if ability in ("drought", "orichalcumpulse") or "sunnyday" in moves:
            team_details["sun"] = 1
        if ability == "sandstream":
            team_details["sand"] = 1
        if ability == "snowwarning" or "snowscape" in moves or "chillyreception" in moves:
            team_details["snow"] = 1
        if "healbell" in moves:
            team_details["statusCure"] = 1
        if "spikes" in moves or "ceaselessedge" in moves:
            team_details["spikes"] = team_details.get("spikes", 0) + 1
        if "toxicspikes" in moves or ability == "toxicdebris":
            team_details["toxicSpikes"] = 1
        if "stealthrock" in moves or "stoneaxe" in moves:
            team_details["stealthRock"] = 1
        if "stickyweb" in moves:
            team_details["stickyWeb"] = 1
        if "defog" in moves:
            team_details["defog"] = 1
        if "rapidspin" in moves or "mortalspin" in moves:
            team_details["rapidSpin"] = 1
        if "auroraveil" in moves or ("reflect" in moves and "lightscreen" in moves):
            team_details["screens"] = 1
        if "terablast" in moves or species.id in ("ogerpon", "ogerponhearthflame", "terapagos"):
            team_details["teraBlast"] = 1
    return num_max_level


def random_team(max_team_size=MAX_TEAM_SIZE, is_monotype=False, is_doubles=False,
                existing=None):
    """gen9randombattle randomTeam.  Returns the list of set dicts, lead first.

    `existing` (CONTINUATION MODE): already-revealed opponent mons as dicts
    carrying speciesId / species / ability / moves / level. The loop state is
    seeded from them and only the remaining slots are generated and returned.
    The lead is never among the fill-ins: in a random battle the lead is
    revealed at battle start, so any state with revealed mons has already
    consumed leads_remaining."""
    pokemon: list[dict] = list(existing) if existing else []

    # For Monotype
    is_monotype = bool(FORCE_MONOTYPE) or is_monotype
    type_pool = TYPE_NAMES  # PS filters out "Stellar"; TYPE_NAMES already excludes it
    type_name = FORCE_MONOTYPE or sample(type_pool)

    # PotD: ruleTable has 'potd' for gen9randombattle, but global.Config.potd is
    # unset on a stock server, so potd is null.
    potd = None

    base_formes: dict[str, int] = {}

    type_count: dict[str, int] = {}
    type_combo_count: dict[str, int] = {}
    type_weaknesses: dict[str, int] = {}
    type_double_weaknesses: dict[str, int] = {}
    team_details: dict = {}
    num_max_level_pokemon = 0

    if existing:
        num_max_level_pokemon = _seed_from_existing(
            existing, base_formes, type_count, type_combo_count,
            type_weaknesses, type_double_weaknesses, team_details
        )

    pokemon_list = list(RANDOM_SETS.keys())
    pokemon_pool, base_species_pool = get_pokemon_pool(
        type_name, pokemon, is_monotype, pokemon_list
    )

    leads_remaining = 0 if existing else (2 if is_doubles else 1)
    # gen9randombattle has neither 'pickedteamsize' nor 'teampreview'.
    while base_species_pool and len(pokemon) < max_team_size:
        base_species = sample_no_replace(base_species_pool)
        species = get_species(sample(pokemon_pool[base_species]))
        if not species.exists:
            continue

        # Limit to one of each species (Species Clause)
        if base_formes.get(species.baseSpecies):
            continue

        # Treat Ogerpon formes and Terapagos like the Tera Blast user role;
        # reject if team has one already  (teams.ts:1765)
        if species.id in ("ogerpon", "ogerponhearthflame", "terapagos") and team_details.get("teraBlast"):
            continue

        # Illusion shouldn't be on the last slot
        if species.baseSpecies == "Zoroark" and len(pokemon) >= (max_team_size - 1):
            continue

        types = species.types
        type_combo = ",".join(sorted(types))
        weak_to_freeze_dry = (
            get_effectiveness("Ice", species) > 0 or
            (get_effectiveness("Ice", species) > -2 and "Water" in types)
        )
        # Dynamically scale limits for different team sizes. Minimum value is 1.
        limit_factor = round(max_team_size / 6) or 1

        if not is_monotype and not FORCE_MONOTYPE:
            skip = False

            # Limit two of any type
            for t in types:
                if type_count.get(t, 0) >= 2 * limit_factor:
                    skip = True
                    break
            if skip:
                continue

            # Limit three weak to any type, and one double weak to any type
            for t in TYPE_NAMES:
                if get_effectiveness(t, species) > 0:
                    if not type_weaknesses.get(t):
                        type_weaknesses[t] = 0
                    if type_weaknesses[t] >= 3 * limit_factor:
                        skip = True
                        break
                if get_effectiveness(t, species) > 1:
                    if not type_double_weaknesses.get(t):
                        type_double_weaknesses[t] = 0
                    if type_double_weaknesses[t] >= limit_factor:
                        skip = True
                        break
            if skip:
                continue

            # Count Dry Skin/Fluffy as Fire weaknesses
            if get_effectiveness("Fire", species) == 0 and any(
                a in ("Dry Skin", "Fluffy") for a in species.abilities.values()
            ):
                if not type_weaknesses.get("Fire"):
                    type_weaknesses["Fire"] = 0
                if type_weaknesses["Fire"] >= 3 * limit_factor:
                    continue

            # Limit four weak to Freeze-Dry
            if weak_to_freeze_dry:
                if not type_weaknesses.get("Freeze-Dry"):
                    type_weaknesses["Freeze-Dry"] = 0
                if type_weaknesses["Freeze-Dry"] >= 4 * limit_factor:
                    continue

            # Limit one level 100 Pokemon
            if (not ADJUST_LEVEL and get_level(species, is_doubles) == 100 and
                    num_max_level_pokemon >= limit_factor):
                continue

            # Check compatibility with team
            if not get_pokemon_compatibility(species, pokemon, is_doubles):
                continue

        # Limit three of any type combination in Monotype
        if not FORCE_MONOTYPE and is_monotype and type_combo_count.get(type_combo, 0) >= 3 * limit_factor:
            continue

        # The Pokemon of the Day
        if potd is not None and (len(pokemon) == 1 or max_team_size == 1):
            species = potd

        if leads_remaining:
            no_lead = (DOUBLES_NO_LEAD_POKEMON if is_doubles else NO_LEAD_POKEMON)
            if species.baseSpecies in no_lead:
                if len(pokemon) + leads_remaining == max_team_size:
                    continue
                s = random_set(species, team_details, False, is_doubles)
                pokemon.append(s)
            else:
                s = random_set(species, team_details, True, is_doubles)
                pokemon.insert(0, s)
                leads_remaining -= 1
        else:
            s = random_set(species, team_details, False, is_doubles)
            pokemon.append(s)

        # Don't bother tracking details for the last Pokemon
        if len(pokemon) == max_team_size:
            break

        # Now that our Pokemon has passed all checks, we can increment counters
        base_formes[species.baseSpecies] = 1

        # Increment type counters
        for t in types:
            type_count[t] = type_count.get(t, 0) + 1
        type_combo_count[type_combo] = type_combo_count.get(type_combo, 0) + 1

        # Increment weakness counter
        for t in TYPE_NAMES:
            if get_effectiveness(t, species) > 0:
                type_weaknesses[t] = type_weaknesses.get(t, 0) + 1
            if get_effectiveness(t, species) > 1:
                type_double_weaknesses[t] = type_double_weaknesses.get(t, 0) + 1
        # Count Dry Skin/Fluffy as Fire weaknesses
        if s["ability"] in ("Dry Skin", "Fluffy") and get_effectiveness("Fire", species) == 0:
            type_weaknesses["Fire"] = type_weaknesses.get("Fire", 0) + 1
        if weak_to_freeze_dry:
            type_weaknesses["Freeze-Dry"] = type_weaknesses.get("Freeze-Dry", 0) + 1

        # Increment level 100 counter
        if s["level"] == 100:
            num_max_level_pokemon += 1

        # Track what the team has
        moves = s["moves"]
        ability = s["ability"]
        if ability == "Drizzle" or "raindance" in moves:
            team_details["rain"] = 1
        if ability == "Drought" or ability == "Orichalcum Pulse" or "sunnyday" in moves:
            team_details["sun"] = 1
        if ability == "Sand Stream":
            team_details["sand"] = 1
        if ability == "Snow Warning" or "snowscape" in moves or "chillyreception" in moves:
            team_details["snow"] = 1
        if "healbell" in moves:
            team_details["statusCure"] = 1
        if "spikes" in moves or "ceaselessedge" in moves:
            team_details["spikes"] = team_details.get("spikes", 0) + 1
        if "toxicspikes" in moves or ability == "Toxic Debris":
            team_details["toxicSpikes"] = 1
        if "stealthrock" in moves or "stoneaxe" in moves:
            team_details["stealthRock"] = 1
        if "stickyweb" in moves:
            team_details["stickyWeb"] = 1
        if "defog" in moves:
            team_details["defog"] = 1
        if "rapidspin" in moves or "mortalspin" in moves:
            team_details["rapidSpin"] = 1
        if "auroraveil" in moves or ("reflect" in moves and "lightscreen" in moves):
            team_details["screens"] = 1
        if s["role"] == "Tera Blast user" or species.id in ("ogerpon", "ogerponhearthflame", "terapagos"):
            team_details["teraBlast"] = 1

    if len(pokemon) < max_team_size and len(pokemon) < 12:
        # large teams sometimes cannot be built
        raise RuntimeError("Could not build a random team for gen9randombattle")

    return pokemon
