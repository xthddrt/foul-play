"""gen9randombattle team generator -- a faithful port of Pokemon Showdown's
sequential team builder.

Ground truth (READ-ONLY): pokemon-showdown/data/random-battles/gen9/teams.ts
and its base class one directory up.  The port is split across three private
modules, merged here into one working generator:

    _ps_team_loop.py          randomTeam / getPokemonPool /
                              getPokemonCompatibility / getForme / RNG helpers
    _ps_sets_moves.py         randomSet / randomMoveset / cullMovePool /
                              addMove / getMoveType / queryMoves
    _ps_ability_item_level.py shouldCullAbility / getAbility /
                              getPriorityItem / getItem / getLevel

PUBLIC API
    seed(n)                     -> seed the (single, shared) RNG
    random_team()               -> list[dict], six sets, lead first
    generate_team()             -> alias of random_team()
    Species / get_species(id)   -> unified species record

Each returned set is a dict:
    speciesId  PS id of the base species            e.g. 'ogerponhearthflame'
    species    forme/display name from getForme     e.g. 'Pikachu-Kalos'
    name       species.baseSpecies                  e.g. 'Pikachu'
    level      int
    ability    display name                         e.g. 'Sand Stream'
    item       display name, '' means no item       e.g. 'Heavy-Duty Boots'
    moves      list of move IDs                     e.g. ['stealthrock', ...]
    teraType   display name                         e.g. 'Ghost'
    role       PS role string                       e.g. 'Bulky Support'
    evs / ivs  dicts keyed hp/atk/def/spa/spd/spe
    gender     'M' / 'F' / ''

DELIBERATELY SKIPPED (scope, not oversight): doubles (every isDoubles branch is
ported but dead; getDoublesItem is not ported at all), PS's PRNG sequence (we
match the DISTRIBUTION only, via random.Random), shiny / happiness / nickname.

INTEGRATION NOTES -- what this file actually does
    The three ports were written in parallel and each grew its own species
    record with a different attribute-naming convention (baseStats vs
    base_stats, requiredItem vs required_items) and a different notion of
    species.name.  Rather than rewrite three files, this module defines ONE
    Species carrying every attribute under both spellings and rebinds it into
    all three modules.  Two real signature mismatches are reconciled here:
      * get_level: group 3 wrote get_level(species, set_data, is_doubles);
        groups 1 and 2 call get_level(species, is_doubles).  The wrapper below
        supplies set_data from RANDOM_SETS, exactly as teams.ts:1425 does.
      * the RNG: three modules each held a private random.Random.  All three
        are rebound to one shared instance so a single seed() is reproducible.
"""

from __future__ import annotations

import random as _random

from fp.search import _ps_ability_item_level as _abil
from fp.search import _ps_sets_moves as _sets
from fp.search import _ps_team_loop as _loop

RANDOM_SETS = _loop.RANDOM_SETS
to_id = _loop.to_id


# ---------------------------------------------------------------------------
# One species record for all three ports.
# ---------------------------------------------------------------------------

class Species:
    """Superset of the three ports' species records.

    Attributes are exposed under BOTH conventions (camelCase for groups 1/2,
    snake_case for group 3) so no ported line needs editing.
    """

    __slots__ = (
        "id", "name", "baseSpecies", "base_species", "forme", "types",
        "abilities", "baseStats", "base_stats", "battleOnly", "cosmeticFormes",
        "otherFormes", "requiredItem", "requiredItems", "required_items",
        "requiredMove", "gender", "genderRatio", "tier", "evos", "nfe",
        "exists", "raw",
    )

    def __init__(self, species_id: str, entry: dict):
        self.id = species_id
        self.raw = entry
        self.exists = bool(entry)
        # PS display name, recovered by _ps_team_loop._display_name because
        # foul-play's pokedex.json lowercases the `name` field.
        self.name = _loop._display_name(species_id, entry.get("name", species_id))
        base = entry.get("baseSpecies")
        self.baseSpecies = base if base else self.name
        self.base_species = self.baseSpecies
        self.forme = entry.get("forme", "")
        self.types = [t.capitalize() for t in entry.get("types", [])]
        self.abilities = entry.get("abilities", {})
        bs = entry.get("baseStats", {})
        # PS stat keys; foul-play spells them out.
        self.baseStats = {
            "hp": bs.get("hp", 0),
            "atk": bs.get("attack", 0),
            "def": bs.get("defense", 0),
            "spa": bs.get("special-attack", 0),
            "spd": bs.get("special-defense", 0),
            "spe": bs.get("speed", 0),
        }
        self.base_stats = self.baseStats
        self.battleOnly = entry.get("battleOnly")
        self.cosmeticFormes = entry.get("cosmeticFormes")
        self.otherFormes = entry.get("otherFormes")
        self.requiredItem = entry.get("requiredItem")
        # PS's Species always exposes requiredItems as an ARRAY (dex-species.ts
        # fills it from the singular requiredItem).  foul-play's pokedex.json
        # keeps PS's raw fields, so Zacian-Crowned / the Ogerpon masks / the
        # origin orbs only have the singular one.  Without this fallback the
        # port emits no item at all for those nine species.
        self.requiredItems = entry.get("requiredItems") or (
            [entry["requiredItem"]] if entry.get("requiredItem") else None)
        self.required_items = self.requiredItems
        self.requiredMove = entry.get("requiredMove")
        self.gender = entry.get("gender")
        self.genderRatio = entry.get("genderRatio")
        self.tier = entry.get("tier", "")
        self.evos = entry.get("evos")
        # pokedex.json has no `nfe` flag; group 3 verified bool(evos) matches
        # PS's dex-species.ts:585 rule for all 509 gen9randombattle species.
        self.nfe = bool(self.evos)

    def __repr__(self):
        return f"<Species {self.name}>"


_POKEDEX = _loop._POKEDEX
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


# Rebind into all three ports (isinstance checks live in each of them).
_loop.Species = Species
_loop.get_species = get_species
_sets.Species = Species
_sets.get_species = get_species
_abil.PSSpecies = Species
_abil.get_species = get_species


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------

class PSRandomTeams(_sets.PSSetsMoves):
    """gen9randombattle RandomTeams: group 2's mixin + groups 1 and 3."""

    def __init__(self, rng=None):
        self.prng = rng if rng is not None else _random.Random()
        self.random_sets = RANDOM_SETS
        self.random_doubles_sets = None  # singles only

    # -- group 1 ----------------------------------------------------------
    def get_forme(self, species):
        return _loop.get_forme(species)

    # -- group 3 ----------------------------------------------------------
    def get_ability(self, types, moves, abilities, counter, team_details,
                    species, is_lead, is_doubles, tera_type, role):
        return _abil.get_ability(types, moves, abilities, counter, team_details,
                                 species, is_lead, is_doubles, tera_type, role)

    def get_priority_item(self, ability, types, moves, counter, team_details,
                          species, is_lead, tera_type, role, is_doubles):
        return _abil.get_priority_item(ability, types, moves, counter,
                                       team_details, species, is_lead,
                                       tera_type, role, is_doubles)

    def get_item(self, ability, types, moves, counter, team_details, species,
                 is_lead, tera_type, role):
        return _abil.get_item(ability, types, moves, counter, team_details,
                              species, is_lead, tera_type, role)

    def get_level(self, species, is_doubles=False):
        # teams.ts:1420-1448.  Group 3 wrote (species, set_data, is_doubles);
        # groups 1/2 call (species, is_doubles).  Supply set_data here.
        return _abil.get_level(species, RANDOM_SETS[species.id], is_doubles)

    # -- group 1's team loop ----------------------------------------------
    def random_team(self, max_team_size=6, is_monotype=False, is_doubles=False):
        return _loop.random_team(max_team_size, is_monotype, is_doubles)


# One shared generator and one shared RNG.  The team loop and the ability/item
# module both reach for module-level globals, so bind ours into them.
_GEN = PSRandomTeams()
_loop._PRNG = _GEN.prng
_abil.RNG = _GEN.prng
_loop.random_set = _GEN.random_set
_loop.get_level = _GEN.get_level


def seed(n) -> None:
    """Seed the single shared RNG (distribution-faithful, not PS-sequence)."""
    _GEN.prng.seed(n)


def random_team():
    """Generate one gen9randombattle team: six sets, lead first."""
    return _GEN.random_team()


generate_team = random_team


if __name__ == "__main__":
    import json
    import sys

    seed(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
    for mon in random_team():
        print(json.dumps(mon))
