"""Faithful transliteration of PS gen9 randombattle ability / item / level selection.

Ground truth: pokemon-showdown/data/random-battles/gen9/teams.ts
    shouldCullAbility  1048-1087
    getAbility         1087-1135
    getPriorityItem    1135-1235
    getItem            1325-1420
    getLevel           1420-1448
getDoublesItem (1235-1325) is deliberately NOT ported -- singles only.
All `isDoubles` branches are hard-coded to False and the dead code is kept
inline (commented) so the ladder still reads line-for-line against teams.ts.

=============================================================================
INTERFACE CONTRACT -- what this module EXPECTS from the other two ports
=============================================================================
Functions I CALL from the team-loop / moveset agents: NONE.  This module is
self-contained; it only consumes values they produce.  They call me as:

    get_ability(types, moves, abilities, counter, team_details, species,
                is_lead, is_doubles, tera_type, role) -> str
    get_priority_item(ability, types, moves, counter, team_details, species,
                      is_lead, tera_type, role, is_doubles) -> str | None
    get_item(ability, types, moves, counter, team_details, species,
             is_lead, tera_type, role) -> str
    get_level(species, set_data) -> int

Argument shapes (mirroring randomSet, teams.ts 1508-1533):
  types        : set[str]   PS-capitalised type names, e.g. {"Bug", "Flying"}
  moves        : set[str]   move IDs, lowercase alnum, e.g. {"uturn", "roost"}
  abilities    : list[str]  set_data["abilities"], PS-capitalised, e.g. ["Swarm"]
  counter      : object with .get(key: str) -> int   (MoveCounter).
                 Keys used here: 'Status', 'Physical', 'Special', 'Grass',
                 'Bug', 'Water', 'setup', 'priority', 'sheerforce',
                 'skilllink', 'recovery', 'recoil', 'hazards',
                 'speedsetup', 'physicalsetup'.
  team_details : dict-like; .get(k) used for 'sun','rain','sand','snow'.
  species      : a PSSpecies (see below).  Build it with get_species(id) --
                 the team loop is welcome to import that helper.
  is_lead      : bool
  is_doubles   : bool  (always False for gen9randombattle)
  tera_type    : str   PS-capitalised single type, e.g. "Ghost"
  role         : str   e.g. "Fast Attacker"
  set_data     : the sets.json entry for the species (needs key "level")

Caller MUST do, exactly as teams.ts 1524-1531:
    item = get_priority_item(...)
    if item is None:
        item = get_item(...)
(get_priority_item returns "" -- an intentional *no item* -- for the
acrobatics branch; that is NOT the same as None.)

RNG: distribution-matching only, not sequence-matching.  Module-level
`RNG` (random.Random).  Call seed(n) to make a run reproducible.  Other
modules may import `sample`, `random_chance`, `RNG`.

DATA SOURCES
  species types / base stats / requiredItems / evos:
      foul-play/data/pokedex.json
  type effectiveness: fp.helpers.DAMAGE_MULTIPICATION_ARRAY (per-type
      multiplier -> PS effectiveness stage; identical to Dex#getEffectiveness)
  moves.json is NOT needed by these three functions (they only test move IDs).
MISSING FIELD: pokedex.json has no `nfe` flag; it is derived as
bool(species.evos), verified equal to PS's Dex nfe for all 509 gen9
randombattle species (PS rule at sim/dex-species.ts:585 only differs when an
evo is isNonstandard, which never happens inside this pool).
"""

import json
import os
import random

from fp.helpers import DAMAGE_MULTIPICATION_ARRAY, POKEMON_TYPE_INDICES

# --------------------------------------------------------------------------
# RNG helpers (base class: pokemon-showdown/data/random-battles/teams.ts)
# --------------------------------------------------------------------------
RNG = random.Random()


def seed(n):
    RNG.seed(n)


def sample(seq):
    """this.sample(list)"""
    return RNG.choice(list(seq))


def random_chance(numerator, denominator):
    """this.randomChance(n, d)"""
    return RNG.randrange(denominator) < numerator


# --------------------------------------------------------------------------
# Constants copied verbatim from teams.ts
# --------------------------------------------------------------------------
# teams.ts 121-123
PIVOT_MOVES = [
    "chillyreception", "flipturn", "partingshot", "shedtail", "teleport",
    "uturn", "voltswitch",
]
# teams.ts 147-149
DEFENSIVE_TERA_BLAST_USERS = [
    "alcremie", "bellossom", "comfey", "fezandipiti", "florges",
]
# getAbility 1126-1128
WEATHER_ABILITIES = [
    "Chlorophyll", "Hydration", "Sand Force", "Sand Rush", "Slush Rush",
    "Solar Power", "Swift Swim",
]


def to_id(text):
    """toID()"""
    return "".join(c for c in text.lower() if c.isalnum())


# --------------------------------------------------------------------------
# Species
# --------------------------------------------------------------------------
_POKEDEX_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data", "pokedex.json",
)
_STAT_KEYS = {
    "hp": "hp", "atk": "attack", "def": "defense",
    "spa": "special-attack", "spd": "special-defense", "spe": "speed",
}
_pokedex = None
_species_cache = {}


class PSSpecies:
    """The subset of PS's Species that ability/item/level selection touches."""

    __slots__ = ("id", "name", "base_species", "types", "base_stats",
                 "required_items", "nfe")

    def __init__(self, sid, entry):
        self.id = sid
        # pokedex.json stores lowercase hyphenated names; PS uses "Arceus-Fire".
        # Only baseSpecies is compared against a literal in the ported code, and
        # that field is already PS-cased in pokedex.json.
        self.name = entry["name"]
        self.base_species = entry.get("baseSpecies") or entry["name"].title()
        self.types = [t.capitalize() for t in entry["types"]]
        self.base_stats = {k: entry["baseStats"][v] for k, v in _STAT_KEYS.items()}
        self.required_items = entry.get("requiredItems")
        self.nfe = bool(entry.get("evos"))


def get_species(sid):
    global _pokedex
    sid = to_id(sid)
    if sid in _species_cache:
        return _species_cache[sid]
    if _pokedex is None:
        with open(_POKEDEX_PATH) as f:
            _pokedex = json.load(f)
    sp = PSSpecies(sid, _pokedex[sid])
    _species_cache[sid] = sp
    return sp


def get_effectiveness(source_type, target):
    """Dex#getEffectiveness (sim/dex.ts 268-290).

    Returns a stage sum: +1 per super-effective defending type, -1 per
    resisted type, 0 for neutral AND for immune (PS handles immunity
    elsewhere).  `target` is a PSSpecies, a list of types, or one type.
    """
    if isinstance(target, PSSpecies):
        types = target.types
    elif isinstance(target, str):
        types = [target]
    else:
        types = list(target)
    total = 0
    src = POKEMON_TYPE_INDICES[source_type.lower()]
    for t in types:
        mult = DAMAGE_MULTIPICATION_ARRAY[src][POKEMON_TYPE_INDICES[t.lower()]]
        if mult == 2:
            total += 1
        elif mult == 0.5:
            total -= 1
    return total


# --------------------------------------------------------------------------
# shouldCullAbility -- teams.ts 1048-1087
# --------------------------------------------------------------------------
def should_cull_ability(ability, types, moves, abilities, counter, team_details,
                        species, role, is_lead, is_doubles):
    # Abilities which are primarily useful for certain moves or with team support
    if ability in ("Chlorophyll", "Solar Power"):
        return not team_details.get("sun")
    if ability == "Defiant":
        return species.id == "thundurus" and bool(counter.get("Status"))
    if ability in ("Hydration", "Swift Swim"):
        return not team_details.get("rain")
    if ability in ("Iron Fist", "Skill Link"):
        return not counter.get(to_id(ability))
    if ability == "Overgrow":
        return not counter.get("Grass")
    if ability == "Prankster":
        return not counter.get("Status")
    if ability in ("Sand Force", "Sand Rush"):
        return not team_details.get("sand")
    if ability == "Slush Rush":
        return not team_details.get("snow")
    if ability == "Swarm":
        return not counter.get("Bug")
    if ability == "Torrent":
        return not counter.get("Water") and "flipturn" not in moves

    return False


# --------------------------------------------------------------------------
# getAbility -- teams.ts 1087-1135
# --------------------------------------------------------------------------
def get_ability(types, moves, abilities, counter, team_details, species,
                is_lead, is_doubles, tera_type, role):
    if len(abilities) <= 1:
        return abilities[0]

    # Hard-code abilities here
    if species.id == "drifblim":
        return "Aftermath" if "defog" in moves else "Unburden"
    if "Flash Fire" in abilities and get_effectiveness("Fire", tera_type) >= 1:
        return "Flash Fire"
    if species.id in ("thundurus", "tornadus") and not counter.get("Physical"):
        return "Prankster"
    if species.id == "swampert" and (counter.get("Water") or "flipturn" in moves):
        return "Torrent"
    if species.id == "toucannon" and counter.get("skilllink"):
        return "Skill Link"
    if "Slush Rush" in abilities and "snowscape" in moves:
        return "Slush Rush"
    if species.id == "golduck" and team_details.get("rain"):
        return "Swift Swim"

    # Obtain a list of abilities that are allowed (not culled)
    ability_allowed = [
        a for a in abilities
        if not should_cull_ability(a, types, moves, abilities, counter,
                                   team_details, species, role, is_lead, is_doubles)
    ]

    # Pick a random allowed ability
    if len(ability_allowed) >= 1:
        return sample(ability_allowed)

    # If all abilities are rejected, prioritize weather abilities
    if not ability_allowed:
        weather_abilities = [a for a in abilities if a in WEATHER_ABILITIES]
        if weather_abilities:
            return sample(weather_abilities)

    # Pick a random ability
    return sample(abilities)


# --------------------------------------------------------------------------
# getPriorityItem -- teams.ts 1135-1235
# Returns None when PS returns undefined (caller must then call get_item).
# Returns "" for the acrobatics branch (deliberately item-less).
# --------------------------------------------------------------------------
def get_priority_item(ability, types, moves, counter, team_details, species,
                      is_lead, tera_type, role, is_doubles):
    if not is_doubles:
        if role == "Fast Bulky Setup" and ability in ("Quark Drive", "Protosynthesis"):
            return "Booster Energy"
        if species.id == "lokix":
            return "Silver Powder" if role == "Fast Attacker" else "Life Orb"
    if species.required_items:
        # Z-Crystals aren't available in Gen 9, so require Plates
        if species.base_species == "Arceus":
            return species.required_items[0]
        return sample(species.required_items)
    if species.id == "pikachu":
        return "Light Ball"
    if role == "AV Pivot":
        return "Assault Vest"
    if species.id == "regieleki":
        return "Magnet"
    if "Normal" in types and "doubleedge" in moves and "fakeout" in moves:
        return "Silk Scarf"
    if (
        species.id == "froslass" or "populationbomb" in moves or
        (ability == "Hustle" and counter.get("setup") and not is_doubles and random_chance(1, 2))
    ):
        return "Wide Lens"
    if species.id == "smeargle":
        return "Focus Sash"
    if "clangoroussoul" in moves or (species.id == "toxtricity" and "shiftgear" in moves):
        return "Throat Spray"
    if (
        (species.base_species == "Magearna" and role == "Tera Blast user") or
        (species.id in ("calyrexice", "necrozmaduskmane") and is_doubles)
    ):
        return "Weakness Policy"
    if any(m in moves for m in ("dragonenergy", "lastrespects", "waterspout")):
        return "Choice Scarf"
    if not is_doubles and (
        ability == "Imposter" or (species.id == "magnezone" and role == "Fast Attacker")
    ):
        return "Choice Scarf"
    if species.id == "rampardos" and (role == "Fast Attacker" or is_doubles):
        return "Choice Scarf"
    if species.id == "palkia" and counter.get("Status"):
        return "Lustrous Orb"
    if (
        "courtchange" in moves or
        (not is_doubles and (
            species.id == "luvdisc" or (species.id == "terapagos" and "rest" not in moves)
        ))
    ):
        return "Heavy-Duty Boots"
    if (
        ability in ("Cheek Pouch", "Cud Chew", "Harvest", "Ripen") or
        "bellydrum" in moves or "filletaway" in moves
    ):
        return "Sitrus Berry"
    if any(m in moves for m in ("healingwish", "switcheroo", "trick")):
        if (
            60 <= species.base_stats["spe"] <= 108 and
            role != "Wallbreaker" and role != "Doubles Wallbreaker" and
            not counter.get("priority")
        ):
            return "Choice Scarf"
        else:
            return "Choice Band" if counter.get("Physical") > counter.get("Special") else "Choice Specs"
    if counter.get("Status") and species.id in ("latias", "latios"):
        return "Soul Dew"
    if species.id == "scyther" and not is_doubles:
        return "Eviolite" if (is_lead and "uturn" not in moves) else "Heavy-Duty Boots"
    if ability == "Poison Heal" or ability == "Quick Feet":
        return "Toxic Orb"
    if species.nfe:
        return "Eviolite"
    if (ability == "Guts" or "facade" in moves) and "sleeptalk" not in moves:
        return "Toxic Orb" if ("Fire" in types or ability == "Toxic Boost") else "Flame Orb"
    if ability == "Magic Guard" or (ability == "Sheer Force" and counter.get("sheerforce")):
        return "Life Orb"
    if ability == "Anger Shell":
        return sample(["Expert Belt", "Lum Berry", "Scope Lens", "Sitrus Berry"])
    if "dragondance" in moves and is_doubles:
        return "Clear Amulet"
    if counter.get("skilllink") and ability != "Skill Link" and species.id != "breloom":
        return "Loaded Dice"
    if ability == "Unburden":
        return "White Herb" if ("closecombat" in moves or "leafstorm" in moves) else "Sitrus Berry"
    if "shellsmash" in moves and ability != "Weak Armor":
        return "White Herb"
    if "meteorbeam" in moves or ("electroshot" in moves and not team_details.get("rain")):
        return "Power Herb"
    if "acrobatics" in moves and ability != "Protosynthesis":
        return ""
    if "auroraveil" in moves or ("lightscreen" in moves and "reflect" in moves):
        return "Light Clay"
    if ability == "Gluttony":
        return "%s Berry" % sample(["Aguav", "Figy", "Iapapa", "Mago", "Wiki"])
    if species.id == "giratina" and not is_doubles and "rest" in moves and "sleeptalk" not in moves:
        return "Leftovers"
    if (
        "rest" in moves and "sleeptalk" not in moves and
        ability != "Natural Cure" and ability != "Shed Skin"
    ):
        return "Chesto Berry"
    if (
        species.id != "yanmega" and
        get_effectiveness("Rock", species) >= 2 and (
            "Flying" not in types or not is_doubles)
    ):
        return "Heavy-Duty Boots"
    return None


# --------------------------------------------------------------------------
# getItem -- teams.ts 1325-1420
# --------------------------------------------------------------------------
def get_item(ability, types, moves, counter, team_details, species, is_lead,
             tera_type, role):
    life_orb_reqs = all(m not in moves for m in ("flamecharge", "nuzzle", "rapidspin"))

    if (
        species.id != "jirachi" and counter.get("Physical") >= len(moves) and
        all(m not in moves for m in (
            "dragontail", "fakeout", "firstimpression", "flamecharge", "rapidspin", "trailblaze"))
    ):
        scarf_reqs = (
            role != "Wallbreaker" and
            (species.base_stats["atk"] >= 100 or ability == "Huge Power" or ability == "Pure Power") and
            60 <= species.base_stats["spe"] <= 108 and
            ability != "Speed Boost" and not counter.get("priority")
        )
        return "Choice Scarf" if (scarf_reqs and random_chance(1, 2)) else "Choice Band"
    if (
        counter.get("Special") >= len(moves) or
        (counter.get("Special") >= len(moves) - 1 and any(m in moves for m in ("flipturn", "uturn")))
    ):
        scarf_reqs = (
            role != "Wallbreaker" and
            species.base_stats["spa"] >= 100 and
            60 <= species.base_stats["spe"] <= 108 and
            ability != "Speed Boost" and ability != "Tinted Lens" and
            "uturn" not in moves and not counter.get("priority")
        )
        return "Choice Scarf" if (scarf_reqs and random_chance(1, 2)) else "Choice Specs"
    if counter.get("speedsetup") and not counter.get("physicalsetup") and role == "Bulky Setup":
        return "Weakness Policy"
    if not counter.get("Status") and role not in ("Fast Attacker", "Wallbreaker", "Tera Blast user"):
        return "Assault Vest"
    if species.id == "golem":
        return "Weakness Policy" if counter.get("speedsetup") else "Custap Berry"
    if "substitute" in moves:
        return "Leftovers"
    if (
        "stickyweb" in moves and is_lead and
        (species.base_stats["hp"] + species.base_stats["def"] + species.base_stats["spd"]) <= 235
    ):
        return "Focus Sash"
    if get_effectiveness("Rock", species) >= 1:
        return "Heavy-Duty Boots"
    if (
        "chillyreception" in moves or (
            role == "Fast Support" and
            any(m in moves for m in PIVOT_MOVES + ["defog", "mortalspin", "rapidspin"]) and
            "Flying" not in types and ability != "Levitate"
        )
    ):
        return "Heavy-Duty Boots"

    # Low Priority
    if "dragondance" in moves and role == "Bulky Setup":
        return "Weakness Policy"
    if (
        ability == "Rough Skin" or (
            ability == "Regenerator" and role in ("Bulky Support", "Bulky Attacker") and
            (species.base_stats["hp"] + species.base_stats["def"]) >= 180 and random_chance(1, 2)
        ) or (
            ability != "Regenerator" and not counter.get("setup") and counter.get("recovery") and
            get_effectiveness("Fighting", species) < 1 and
            (species.base_stats["hp"] + species.base_stats["def"]) > 200 and random_chance(1, 2)
        )
    ):
        return "Rocky Helmet"
    if "outrage" in moves and counter.get("setup"):
        return "Lum Berry"
    if "protect" in moves and ability != "Speed Boost":
        return "Leftovers"
    if (
        role == "Fast Support" and is_lead and not counter.get("recovery") and
        not counter.get("recoil") and
        (counter.get("hazards") or counter.get("setup")) and
        (species.base_stats["hp"] + species.base_stats["def"] + species.base_stats["spd"]) < 258
    ):
        return "Focus Sash"
    if (
        not counter.get("setup") and ability != "Levitate" and
        get_effectiveness("Ground", species) >= 2
    ):
        return "Air Balloon"
    if role in ("Bulky Attacker", "Bulky Support", "Bulky Setup"):
        return "Leftovers"
    if species.id == "pawmot" and "nuzzle" in moves:
        return "Leppa Berry"
    if role == "Fast Support" or role == "Fast Bulky Setup":
        return "Life Orb" if (
            counter.get("Physical") + counter.get("Special") > counter.get("Status") and life_orb_reqs
        ) else "Leftovers"
    if role == "Tera Blast user" and species.id in DEFENSIVE_TERA_BLAST_USERS:
        return "Leftovers"
    if life_orb_reqs and role in ("Fast Attacker", "Setup Sweeper", "Tera Blast user", "Wallbreaker"):
        return "Life Orb"
    return "Leftovers"


# --------------------------------------------------------------------------
# getLevel -- teams.ts 1420-1448
# adjustLevel and the gen-2 tier ladder do not apply to gen9randombattle;
# every one of the 509 sets.json entries carries a "level", so this is
# always the randomSets lookup.
# --------------------------------------------------------------------------
def get_level(species, set_data, is_doubles=False):
    level = set_data.get("level")
    if level:
        return level
    # Default to 80
    return 80
