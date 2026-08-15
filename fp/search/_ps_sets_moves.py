"""
PORT GROUP 2 -- set and move selection, transliterated from
pokemon-showdown/data/random-battles/gen9/teams.ts (READ-ONLY ground truth).

Ported spans:
    queryMoves          teams.ts 340-462   (needed by everything here)
    cullMovePool        teams.ts 463-655
    incompatibleMoves   teams.ts 656-676
    addMove             teams.ts 676-697
    getMoveType         teams.ts 697-724
    randomMoveset       teams.ts 724-1048
    randomSet           teams.ts 1471-1614
    plus base helpers fastPop / sample / sampleNoReplace / random / randomChance
    and the moveEnforcementCheckers table (teams.ts 199-247).

SINGLES ONLY.  Every `isDoubles` branch is kept in the code for fidelity but is
never taken (is_doubles defaults to False everywhere); getDoublesItem and the
doubles enforcement blocks are marked and unused.

The PRNG is NOT PS's PRNG.  We reproduce the DISTRIBUTION, not the sequence.

=============================================================================
CONTRACT -- exact signatures this module CALLS but does NOT define.
Mix `PSSetsMoves` into the final RandomTeams class alongside the other groups.

  FROM GROUP 1 (randomTeam / getPokemonPool / getForme):
      self.get_forme(species) -> str                      # teams.ts 1448-1471

  FROM GROUP 3 (ability / item / level):
      self.get_ability(types, moves, abilities, counter, team_details,
                       species, is_lead, is_doubles, tera_type, role) -> str
      self.get_priority_item(ability, types, moves, counter, team_details,
                             species, is_lead, tera_type, role,
                             is_doubles) -> str | None
      self.get_item(ability, types, moves, counter, team_details, species,
                    is_lead, tera_type, role) -> str
      self.get_level(species, is_doubles=False) -> int

  SHARED STATE the mixin expects on `self`:
      self.random_sets : dict          # parsed gen9 sets.json
      self.prng        : random.Random

Argument conventions:
    types        : OrderedSet[str]   capitalized type names, e.g. "Water"
    moves        : OrderedSet[str]   move ids, INSERTION ORDERED (order is
                                     load-bearing in incompatibleMoves)
    abilities    : list[str]         ability names from the chosen set
    counter      : MoveCounter
    team_details : dict              PS TeamDetails, plain dict with the PS
                                     camelCase keys: 'screens', 'stickyWeb',
                                     'stealthRock', 'defog', 'rapidSpin',
                                     'toxicSpikes', 'spikes', 'statusCure',
                                     'teraBlast', 'illusion', weather keys...
    species      : Species           (defined in this file, see get_species)
    tera_type    : str               capitalized
    role         : str               e.g. "Bulky Support"

This module also EXPORTS, for the other groups to reuse:
    OrderedSet, MoveCounter, Species, Move, get_species, get_move, to_id,
    ROCK_EFFECTIVENESS, and the move-category constants (SETUP, HAZARDS, ...).

=============================================================================
DATA SOURCES (stated per the task):
  * move type / category / basePower / accuracy / priority / flags / secondary
    / multihit / recoil / drain / damage
        -> foul-play/data/moves.json   (886 moves; `type` and `category` are
           lowercase there and are re-capitalized here to match PS)
  * basePowerCallback / damageCallback / hasCrashDamage / hasSheerForceBoost
        -> NOT present in moves.json (they are JS functions / flags in
           pokemon-showdown/data/moves.ts).  The id lists below were extracted
           verbatim from pokemon-showdown/data/moves.ts.
  * species types / baseStats / gender / baseSpecies / requiredMove
        -> foul-play/data/pokedex.json
  * getEffectiveness('Rock', species)
        -> Rock column of pokemon-showdown/data/typechart.ts, inlined below.

FIELDS I COULD NOT FIND: none that are used.  `move.secondaries` (plural) is
absent from moves.json; PS's `move.secondary` is null for those moves anyway,
so the `counter.add('sheerforce')` test is unaffected.
"""

from __future__ import annotations

import json
import os
import random
import re

# --------------------------------------------------------------------------
# Data loading
# --------------------------------------------------------------------------

_DATA_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data")


def to_id(text) -> str:
    """sim/dex-data.ts toID()"""
    if text is None:
        return ""
    return re.sub(r"[^a-z0-9]+", "", str(text).lower())


# Extracted from pokemon-showdown/data/moves.ts (functions, not in moves.json)
_BASE_POWER_CALLBACK_MOVES = frozenset([
    'acrobatics', 'assurance', 'avalanche', 'beatup', 'boltbeak', 'crushgrip',
    'dragonenergy', 'echoedvoice', 'electroball', 'eruption', 'firepledge',
    'fishiousrend', 'flail', 'frustration', 'furycutter', 'grassknot',
    'grasspledge', 'gyroball', 'hardpress', 'heatcrash', 'heavyslam', 'hex',
    'iceball', 'infernalparade', 'lastrespects', 'lowkick', 'payback',
    'pikapapow', 'powertrip', 'punishment', 'pursuit', 'ragefist', 'return',
    'revenge', 'reversal', 'risingvoltage', 'rollout', 'round', 'smellingsalts',
    'spitup', 'stompingtantrum', 'storedpower', 'temperflare', 'terablast',
    'tripleaxel', 'triplekick', 'trumpcard', 'veeveevolley', 'wakeupslap',
    'waterpledge', 'watershuriken', 'waterspout', 'wringout',
])
_DAMAGE_CALLBACK_MOVES = frozenset([
    'comeuppance', 'counter', 'endeavor', 'finalgambit', 'guardianofalola',
    'metalburst', 'mirrorcoat', 'naturesmadness', 'psywave', 'ruination',
    'superfang',
])
_HAS_CRASH_DAMAGE = frozenset(['axekick', 'highjumpkick', 'jumpkick', 'supercellslam'])
_HAS_SHEER_FORCE_BOOST = frozenset(['electroshot', 'orderup'])

# Rock damageTaken column of pokemon-showdown/data/typechart.ts
# 1 = weak (+1), 2 = resist (-1), 3 = immune (0), 0 = neutral
_ROCK_DAMAGE_TAKEN = {
    'Bug': 1, 'Dark': 0, 'Dragon': 0, 'Electric': 0, 'Fairy': 0, 'Fighting': 2,
    'Fire': 1, 'Flying': 1, 'Ghost': 0, 'Grass': 0, 'Ground': 2, 'Ice': 1,
    'Normal': 0, 'Poison': 0, 'Psychic': 0, 'Rock': 0, 'Steel': 2,
    'Stellar': 0, 'Water': 0,
}


def rock_effectiveness(types) -> int:
    """this.dex.getEffectiveness('Rock', species)"""
    total = 0
    for t in types:
        taken = _ROCK_DAMAGE_TAKEN.get(t, 0)
        if taken == 1:
            total += 1
        elif taken == 2:
            total -= 1
    return total


ROCK_EFFECTIVENESS = rock_effectiveness


class Move:
    __slots__ = ('id', 'name', 'type', 'category', 'basePower', 'accuracy',
                 'priority', 'flags', 'secondary', 'multihit', 'recoil',
                 'drain', 'damage', 'basePowerCallback', 'damageCallback',
                 'hasCrashDamage', 'hasSheerForceBoost')

    def __init__(self, d):
        self.id = d['id']
        self.name = d.get('name', d['id'])
        self.type = str(d.get('type', 'normal')).capitalize()
        self.category = str(d.get('category', 'status')).capitalize()
        self.basePower = d.get('basePower', 0) or 0
        self.accuracy = d.get('accuracy', True)
        self.priority = d.get('priority', 0) or 0
        self.flags = d.get('flags') or {}
        self.secondary = d.get('secondary')
        self.multihit = d.get('multihit')
        self.recoil = d.get('recoil')
        self.drain = d.get('drain')
        self.damage = d.get('damage')
        self.basePowerCallback = self.id in _BASE_POWER_CALLBACK_MOVES
        self.damageCallback = self.id in _DAMAGE_CALLBACK_MOVES
        self.hasCrashDamage = self.id in _HAS_CRASH_DAMAGE
        self.hasSheerForceBoost = self.id in _HAS_SHEER_FORCE_BOOST

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        return isinstance(other, Move) and other.id == self.id

    def __repr__(self):
        return f"<Move {self.id}>"


class Species:
    __slots__ = ('id', 'name', 'baseSpecies', 'types', 'baseStats', 'gender',
                 'requiredMove', 'abilities')

    def __init__(self, key, d):
        self.id = key
        # pokedex.json 'name' is lowercase-dashed, e.g. "tauros-paldea-combat".
        # PS's species.name is "Tauros-Paldea-Combat"; all comparisons on
        # species.name in this file are done case-insensitively.
        self.name = d.get('name', key)
        base = d.get('baseSpecies')
        self.baseSpecies = base if base else _title(self.name)
        self.types = [t.capitalize() for t in d.get('types', [])]
        bs = d.get('baseStats', {})
        self.baseStats = {
            'hp': bs.get('hp', 0),
            'atk': bs.get('attack', 0),
            'def': bs.get('defense', 0),
            'spa': bs.get('special-attack', 0),
            'spd': bs.get('special-defense', 0),
            'spe': bs.get('speed', 0),
        }
        self.gender = d.get('gender')
        self.requiredMove = d.get('requiredMove')
        self.abilities = d.get('abilities', {})

    def __repr__(self):
        return f"<Species {self.id}>"


def _title(name: str) -> str:
    return "-".join(p.capitalize() for p in str(name).split("-"))


_MOVES = None
_DEX = None
_STATUS_MOVES = None


def _load():
    global _MOVES, _DEX, _STATUS_MOVES
    if _MOVES is not None:
        return
    with open(os.path.join(_DATA_DIR, "moves.json")) as f:
        raw_moves = json.load(f)
    _MOVES = {k: Move(v) for k, v in raw_moves.items()}
    with open(os.path.join(_DATA_DIR, "pokedex.json")) as f:
        raw_dex = json.load(f)
    _DEX = {k: Species(k, v) for k, v in raw_dex.items()}
    # this.cachedStatusMoves = dex.moves.all().filter(category === 'Status')
    _STATUS_MOVES = [m.id for m in _MOVES.values() if m.category == 'Status']


def get_move(name) -> Move:
    """this.dex.moves.get(name)"""
    _load()
    mid = to_id(name)
    m = _MOVES.get(mid)
    if m is None:
        raise KeyError(f"unknown move {name!r} ({mid})")
    return m


def get_species(name) -> Species:
    """this.dex.species.get(name)"""
    _load()
    sid = to_id(name)
    s = _DEX.get(sid)
    if s is None:
        raise KeyError(f"unknown species {name!r} ({sid})")
    return s


def status_moves():
    _load()
    return _STATUS_MOVES


# --------------------------------------------------------------------------
# Ordered containers (JS Set / Multiset semantics)
# --------------------------------------------------------------------------

class OrderedSet:
    """JS `Set` -- insertion ordered.  The order IS load-bearing (see
    incompatibleMoves, which iterates `moves` and pops the first hit)."""
    __slots__ = ('_d',)

    def __init__(self, items=()):
        self._d = dict.fromkeys(items)

    def add(self, x):
        self._d[x] = None

    def has(self, x):
        return x in self._d

    def __contains__(self, x):
        return x in self._d

    def __iter__(self):
        return iter(self._d)

    def __len__(self):
        return len(self._d)

    @property
    def size(self):
        return len(self._d)

    def __repr__(self):
        return f"OrderedSet({list(self._d)!r})"


class MoveCounter:
    """teams.ts:172 -- Utils.Multiset<string> plus two ordered Move sets."""
    __slots__ = ('_c', 'damagingMoves', 'basePowerMoves')

    def __init__(self):
        self._c = {}
        self.damagingMoves = {}   # ordered set of Move
        self.basePowerMoves = {}  # ordered set of Move

    def add(self, key, n=1):
        self._c[key] = self._c.get(key, 0) + n

    def get(self, key):
        return self._c.get(key, 0)

    def set(self, key, value):
        self._c[key] = value


# --------------------------------------------------------------------------
# Move category constants -- teams.ts 188-268 (verbatim)
# --------------------------------------------------------------------------

RECOVERY_MOVES = [
    'healorder', 'milkdrink', 'moonlight', 'morningsun', 'recover', 'roost',
    'shoreup', 'slackoff', 'softboiled', 'strengthsap', 'synthesis',
]
CONTRARY_MOVES = [
    'armorcannon', 'closecombat', 'leafstorm', 'makeitrain', 'overheat',
    'spinout', 'superpower', 'vcreate',
]
PHYSICAL_SETUP = [
    'bellydrum', 'bulkup', 'coil', 'curse', 'dragondance', 'honeclaws', 'howl',
    'meditate', 'poweruppunch', 'swordsdance', 'tidyup', 'victorydance',
]
SPECIAL_SETUP = [
    'calmmind', 'chargebeam', 'geomancy', 'nastyplot', 'quiverdance',
    'tailglow', 'takeheart', 'torchsong',
]
MIXED_SETUP = [
    'clangoroussoul', 'growth', 'happyhour', 'holdhands', 'noretreat',
    'shellsmash', 'workup',
]
SPEED_SETUP = [
    'agility', 'autotomize', 'flamecharge', 'rockpolish', 'snowscape', 'trailblaze',
]
SETUP = [
    'acidarmor', 'agility', 'autotomize', 'bellydrum', 'bulkup', 'calmmind',
    'clangoroussoul', 'coil', 'cosmicpower', 'curse', 'dragondance',
    'flamecharge', 'growth', 'honeclaws', 'howl', 'irondefense', 'meditate',
    'nastyplot', 'noretreat', 'poweruppunch', 'quiverdance', 'rockpolish',
    'shellsmash', 'shelter', 'shiftgear', 'swordsdance', 'tailglow',
    'takeheart', 'tidyup', 'trailblaze', 'workup', 'victorydance',
]
SPEED_CONTROL = [
    'electroweb', 'glare', 'icywind', 'lowsweep', 'nuzzle', 'quash', 'tailwind',
    'thunderwave', 'trickroom',
]
NO_STAB = [
    'acidspray', 'accelerock', 'aquajet', 'bounce', 'breakingswipe',
    'bulletpunch', 'chatter', 'chloroblast', 'clearsmog', 'covet',
    'dragontail', 'doomdesire', 'electroweb', 'eruption', 'explosion',
    'fakeout', 'feint', 'flamecharge', 'flipturn', 'futuresight',
    'grassyglide', 'iceshard', 'icywind', 'incinerate', 'infestation',
    'machpunch', 'meteorbeam', 'mortalspin', 'nuzzle', 'pluck', 'pursuit',
    'quickattack', 'rapidspin', 'reversal', 'selfdestruct', 'shadowsneak',
    'skydrop', 'snarl', 'strugglebug', 'suckerpunch', 'trailblaze', 'uturn',
    'vacuumwave', 'voltswitch', 'watershuriken', 'waterspout',
]
HAZARDS = ['spikes', 'stealthrock', 'stickyweb', 'toxicspikes']
PROTECT_MOVES = ['banefulbunker', 'burningbulwark', 'protect', 'silktrap', 'spikyshield']
PIVOT_MOVES = [
    'chillyreception', 'flipturn', 'partingshot', 'shedtail', 'teleport',
    'uturn', 'voltswitch',
]
MOVE_PAIRS = [
    ['lightscreen', 'reflect'],
    ['sleeptalk', 'rest'],
    ['protect', 'wish'],
    ['leechseed', 'protect'],
    ['leechseed', 'substitute'],
]
PRIORITY_POKEMON = [
    'breloom', 'brutebonnet', 'cacturne', 'honchkrow', 'mimikyu', 'ragingbolt', 'scizor',
]
DEFENSIVE_TERA_BLAST_USERS = [
    'alcremie', 'bellossom', 'comfey', 'fezandipiti', 'florges',
]


# --------------------------------------------------------------------------
# The mixin
# --------------------------------------------------------------------------

class PSSetsMoves:
    """Transliteration of the set/move half of gen9 RandomTeams.

    Requires the CONTRACT members documented at the top of this file.
    """

    max_move_count = 4          # ruleTable.maxMoveCount for gen9randombattle
    no_stab = NO_STAB
    priority_pokemon = PRIORITY_POKEMON
    force_tera_type = None      # ruleTable valueRule 'forceteratype' -- unset
    tera_stal_clause = False    # ruleTable.has('terastalclause') -- unset
    force_of_the_fallen_mod = False
    format_mod = 'gen9'         # this.format.mod !== 'partnersincrime'

    # -- base helpers (teams.ts 264-330) ----------------------------------

    def random_chance(self, numerator, denominator):
        return self.prng.random() < (numerator / denominator)

    def sample(self, items):
        return items[self.prng.randrange(len(items))]

    def sample_if_array(self, item):
        if isinstance(item, list):
            return self.sample(item)
        return item

    def random(self, m=None, n=None):
        if m is None:
            return self.prng.random()
        if n is None:
            return self.prng.randrange(m)
        return m + self.prng.randrange(n - m)

    def fast_pop(self, lst, index):
        length = len(lst)
        if index < 0 or index >= length:
            raise IndexError(f"Index {index} out of bounds for given array")
        element = lst[index]
        lst[index] = lst[length - 1]
        lst.pop()
        return element

    def sample_no_replace(self, lst):
        length = len(lst)
        if length == 0:
            return None
        index = self.random(length)
        return self.fast_pop(lst, index)

    # -- queryMoves (teams.ts 340-462) -------------------------------------

    def query_moves(self, moves, species, tera_type, abilities) -> MoveCounter:
        counter = MoveCounter()
        types = OrderedSet(species.types)
        if not moves or not len(moves):
            return counter

        categories = {'Physical': 0, 'Special': 0, 'Status': 0}

        for moveid in moves:
            move = get_move(moveid)
            # gen 9: naturepower calls triattack
            if moveid == 'naturepower':
                move = get_move('triattack')

            move_type = self.get_move_type(move, species, abilities, tera_type)
            if move.damage or move.damageCallback:
                counter.add('damage')
                counter.damagingMoves[move] = None
            else:
                categories[move.category] += 1

            if moveid == 'lowkick' or (move.basePower and move.basePower <= 60 and
                                       moveid not in ('nuzzle', 'rapidspin')):
                counter.add('technician')

            if move.multihit and isinstance(move.multihit, list) and move.multihit[1] == 5:
                counter.add('skilllink')
            if move.recoil or move.hasCrashDamage:
                counter.add('recoil')
            if move.drain:
                counter.add('drain')

            if move.basePower or move.basePowerCallback:
                counter.basePowerMoves[move] = None
                if (moveid not in self.no_stab or
                        (species.id in self.priority_pokemon and move.priority > 0)):
                    counter.add(move_type)
                    if types.has(move_type):
                        counter.add('stab')
                    if tera_type == move_type:
                        counter.add('stabtera')
                    counter.damagingMoves[move] = None
                if move.flags.get('bite'):
                    counter.add('strongjaw')
                if move.flags.get('punch'):
                    counter.add('ironfist')
                if move.flags.get('sound'):
                    counter.add('sound')
                if move.priority > 0 or (moveid == 'grassyglide' and 'Grassy Surge' in abilities):
                    counter.add('priority')

            if move.secondary or move.hasSheerForceBoost:
                counter.add('sheerforce')

            if move.accuracy and move.accuracy is not True and move.accuracy < 90:
                counter.add('inaccurate')

            if moveid in RECOVERY_MOVES:
                counter.add('recovery')
            if moveid in CONTRARY_MOVES:
                counter.add('contrary')
            if moveid in PHYSICAL_SETUP:
                counter.add('physicalsetup')
            if moveid in SPECIAL_SETUP:
                counter.add('specialsetup')
            if moveid in MIXED_SETUP:
                counter.add('mixedsetup')
            if moveid in SPEED_SETUP:
                counter.add('speedsetup')
            if moveid in SPEED_CONTROL:
                counter.add('speedcontrol')
            if moveid in SETUP:
                counter.add('setup')
            if moveid in HAZARDS:
                counter.add('hazards')

        counter.set('Physical', int(categories['Physical']))
        counter.set('Special', int(categories['Special']))
        counter.set('Status', categories['Status'])
        return counter

    # -- cullMovePool (teams.ts 463-655) -----------------------------------

    def cull_move_pool(self, types, moves, abilities, counter, move_pool,
                       team_details, species, is_lead, tera_type, role,
                       is_doubles=False):
        if moves.size + len(move_pool) <= self.max_move_count:
            return

        # If we have two unfilled moves and only one unpaired move, cull the
        # unpaired move.
        if moves.size == self.max_move_count - 2:
            unpaired_moves = list(move_pool)
            for pair in MOVE_PAIRS:
                if pair[0] in move_pool and pair[1] in move_pool:
                    self.fast_pop(unpaired_moves, unpaired_moves.index(pair[0]))
                    self.fast_pop(unpaired_moves, unpaired_moves.index(pair[1]))
            if len(unpaired_moves) == 1:
                self.fast_pop(move_pool, move_pool.index(unpaired_moves[0]))

        # These moves are paired, and shouldn't appear if there is not room for
        # them both.
        if moves.size == self.max_move_count - 1:
            for pair in MOVE_PAIRS:
                if pair[0] in move_pool and pair[1] in move_pool:
                    self.fast_pop(move_pool, move_pool.index(pair[0]))
                    self.fast_pop(move_pool, move_pool.index(pair[1]))

        status_move_list = status_moves()

        # ---- Team-based move culls (teams.ts 505-536) -- THE POINT ----
        if team_details.get('screens'):
            if 'auroraveil' in move_pool and not is_doubles:
                self.fast_pop(move_pool, move_pool.index('auroraveil'))
            if len(move_pool) >= self.max_move_count + 2:
                if 'reflect' in move_pool:
                    self.fast_pop(move_pool, move_pool.index('reflect'))
                if 'lightscreen' in move_pool:
                    self.fast_pop(move_pool, move_pool.index('lightscreen'))
        if team_details.get('stickyWeb'):
            if 'stickyweb' in move_pool:
                self.fast_pop(move_pool, move_pool.index('stickyweb'))
            if moves.size + len(move_pool) <= self.max_move_count:
                return
        if team_details.get('stealthRock'):
            if 'stealthrock' in move_pool:
                self.fast_pop(move_pool, move_pool.index('stealthrock'))
            if moves.size + len(move_pool) <= self.max_move_count:
                return
        if team_details.get('defog') or team_details.get('rapidSpin'):
            if 'defog' in move_pool:
                self.fast_pop(move_pool, move_pool.index('defog'))
            if 'rapidspin' in move_pool:
                self.fast_pop(move_pool, move_pool.index('rapidspin'))
            if moves.size + len(move_pool) <= self.max_move_count:
                return
        if team_details.get('toxicSpikes'):
            if 'toxicspikes' in move_pool:
                self.fast_pop(move_pool, move_pool.index('toxicspikes'))
            if moves.size + len(move_pool) <= self.max_move_count:
                return
        if team_details.get('spikes') and team_details.get('spikes') >= 2:
            if 'spikes' in move_pool:
                self.fast_pop(move_pool, move_pool.index('spikes'))
            if moves.size + len(move_pool) <= self.max_move_count:
                return
        if team_details.get('statusCure'):
            if 'healbell' in move_pool:
                self.fast_pop(move_pool, move_pool.index('healbell'))
            if moves.size + len(move_pool) <= self.max_move_count:
                return

        if is_doubles:
            # SINGLES ONLY -- never taken.  Kept for fidelity.
            doubles_incompatible_pairs = [
                [SPEED_CONTROL, SPEED_CONTROL],
                [HAZARDS, HAZARDS],
                ['rockslide', 'stoneedge'],
                [SETUP, ['fakeout', 'helpinghand']],
                [PROTECT_MOVES, 'wideguard'],
                [['fierydance', 'fireblast'], 'heatwave'],
                ['dazzlinggleam', ['fleurcannon', 'moonblast']],
                ['poisongas', ['toxicspikes', 'willowisp']],
                [RECOVERY_MOVES, ['healpulse', 'lifedew']],
                ['healpulse', 'lifedew'],
                ['haze', 'icywind'],
                [['hydropump', 'muddywater'], ['muddywater', 'scald']],
                ['disable', 'encore'],
                ['freezedry', 'icebeam'],
                ['energyball', 'leafstorm'],
                ['earthpower', 'sandsearstorm'],
                ['coaching', ['helpinghand', 'howl']],
            ]
            for pair in doubles_incompatible_pairs:
                self.incompatible_moves(moves, move_pool, pair[0], pair[1])
            if role != 'Offensive Protect':
                self.incompatible_moves(moves, move_pool, PROTECT_MOVES, ['flipturn', 'uturn'])

        # General incompatibilities
        incompatible_pairs = [
            # These moves don't mesh well with other aspects of the set
            [status_move_list, ['healingwish', 'switcheroo', 'trick']],
            [SETUP, PIVOT_MOVES],
            [SETUP, HAZARDS],
            [SETUP, ['defog', 'nuzzle', 'toxic', 'yawn', 'haze']],
            [PHYSICAL_SETUP, PHYSICAL_SETUP],
            ['substitute', PIVOT_MOVES],
            [SPEED_SETUP, ['aquajet', 'rest', 'trickroom']],
            ['curse', ['irondefense', 'rapidspin']],
            ['dragondance', 'dracometeor'],
            ['yawn', 'roar'],
            ['trick', 'uturn'],

            # These attacks are redundant with each other
            [['psychic', 'psychicnoise'], ['psyshock', 'psychicnoise']],
            ['surf', 'hydropump'],
            ['liquidation', 'wavecrash'],
            ['aquajet', 'flipturn'],
            ['gigadrain', 'leafstorm'],
            ['powerwhip', 'hornleech'],
            ['airslash', 'hurricane'],
            ['knockoff', 'foulplay'],
            ['throatchop', ['crunch', 'lashout']],
            ['doubleedge', ['bodyslam', 'headbutt']],
            [['fireblast', 'magmastorm'], ['fierydance', 'flamethrower', 'lavaplume']],
            ['thunderpunch', 'wildcharge'],
            ['thunderbolt', 'discharge'],
            ['gunkshot', ['direclaw', 'poisonjab', 'sludgebomb']],
            ['aurasphere', 'focusblast'],
            ['closecombat', 'drainpunch'],
            [['dragonpulse', 'spacialrend'], 'dracometeor'],
            ['dragonclaw', 'outrage'],
            ['heavyslam', 'flashcannon'],
            ['alluringvoice', 'dazzlinggleam'],

            # These status moves are redundant with each other
            ['taunt', 'disable'],
            [['thunderwave', 'toxic'], ['thunderwave', 'willowisp']],
            [['thunderwave', 'toxic', 'willowisp'], 'toxicspikes'],

            # Assorted hardcodes
            # Landorus and Thundurus
            ['nastyplot', ['rockslide', 'knockoff']],
            # Persian
            ['switcheroo', 'fakeout'],
            # Amoonguss
            ['toxic', 'clearsmog'],
            # Chansey and Blissey
            ['healbell', 'stealthrock'],
            # Araquanid and Magnezone
            ['mirrorcoat', ['hydropump', 'bodypress']],
        ]

        for pair in incompatible_pairs:
            self.incompatible_moves(moves, move_pool, pair[0], pair[1])

        if not types.has('Ice'):
            self.incompatible_moves(moves, move_pool, 'icebeam', 'icywind')

        if not is_doubles:
            self.incompatible_moves(moves, move_pool, 'taunt', 'encore')

        if not types.has('Dark') and tera_type != 'Dark':
            self.incompatible_moves(moves, move_pool, 'knockoff', 'suckerpunch')

        if 'Prankster' not in abilities:
            self.incompatible_moves(moves, move_pool, 'thunderwave', 'yawn')

        # Assorted hardcodes
        if species.id == 'barraskewda':
            self.incompatible_moves(moves, move_pool,
                                    ['psychicfangs', 'throatchop'],
                                    ['poisonjab', 'throatchop'])
        if species.id == 'quagsire':
            self.incompatible_moves(moves, move_pool, 'spikes', 'icebeam')
        if species.id == 'cyclizar':
            self.incompatible_moves(moves, move_pool, 'taunt', 'knockoff')
        if species.id == 'camerupt':
            self.incompatible_moves(moves, move_pool, 'roar', 'willowisp')
        if species.id == 'coalossal':
            self.incompatible_moves(moves, move_pool, 'flamethrower', 'overheat')

    # -- incompatibleMoves (teams.ts 656-676) ------------------------------

    def incompatible_moves(self, moves, move_pool, moves_a, moves_b):
        move_array_a = moves_a if isinstance(moves_a, list) else [moves_a]
        move_array_b = moves_b if isinstance(moves_b, list) else [moves_b]
        if moves.size + len(move_pool) <= self.max_move_count:
            return
        for moveid1 in moves:
            if moveid1 in move_array_b:
                for moveid2 in move_array_a:
                    if moveid1 != moveid2 and moveid2 in move_pool:
                        self.fast_pop(move_pool, move_pool.index(moveid2))
                        if moves.size + len(move_pool) <= self.max_move_count:
                            return
            if moveid1 in move_array_a:
                for moveid2 in move_array_b:
                    if moveid1 != moveid2 and moveid2 in move_pool:
                        self.fast_pop(move_pool, move_pool.index(moveid2))
                        if moves.size + len(move_pool) <= self.max_move_count:
                            return

    # -- addMove (teams.ts 676-697) ----------------------------------------

    def add_move(self, move, moves, types, abilities, team_details, species,
                 is_lead, move_pool, tera_type, role, is_doubles=False):
        moves.add(move)
        self.fast_pop(move_pool, move_pool.index(move))
        counter = self.query_moves(moves, species, tera_type, abilities)
        self.cull_move_pool(types, moves, abilities, counter, move_pool,
                            team_details, species, is_lead, tera_type, role,
                            is_doubles)
        return counter

    # -- getMoveType (teams.ts 697-724) ------------------------------------

    def get_move_type(self, move, species, abilities, tera_type) -> str:
        if move.id == 'terablast':
            return tera_type
        if move.id in ('judgment', 'multiattack', 'revelationdance'):
            return species.types[0]

        sname = species.name.lower()
        if move.name == "Raging Bull" and sname.startswith("tauros-paldea"):
            if sname.endswith("combat"):
                return "Fighting"
            if sname.endswith("blaze"):
                return "Fire"
            if sname.endswith("aqua"):
                return "Water"

        if move.name == "Ivy Cudgel" and sname.startswith("ogerpon"):
            if sname.endswith("wellspring"):
                return "Water"
            if sname.endswith("hearthflame"):
                return "Fire"
            if sname.endswith("cornerstone"):
                return "Rock"

        move_type = move.type
        if move_type == 'Normal':
            if 'Aerilate' in abilities:
                return 'Flying'
            if 'Galvanize' in abilities:
                return 'Electric'
            if 'Pixilate' in abilities:
                return 'Fairy'
            if 'Refrigerate' in abilities:
                return 'Ice'
        return move_type

    # -- moveEnforcementCheckers (teams.ts 199-247) ------------------------

    def _run_enforcement_checker(self, checker_name, move_pool, moves, abilities,
                                 types, counter, species, team_details, is_lead,
                                 is_doubles, tera_type, role):
        if checker_name == 'Bug':
            return (any(m in move_pool for m in ('megahorn', 'xscissor')) or
                    (not counter.get('Bug') and (types.has('Electric') or types.has('Psychic'))))
        if checker_name == 'Dark':
            if counter.get('Dark') < 2 and species.id in self.priority_pokemon and role == 'Wallbreaker':
                return True
            return not counter.get('Dark')
        if checker_name == 'Dragon':
            return not counter.get('Dragon')
        if checker_name == 'Electric':
            return not counter.get('Electric')
        if checker_name == 'Fairy':
            return not counter.get('Fairy')
        if checker_name == 'Fighting':
            return not counter.get('Fighting')
        if checker_name == 'Fire':
            return not counter.get('Fire')
        if checker_name == 'Flying':
            return not counter.get('Flying')
        if checker_name == 'Ghost':
            return not counter.get('Ghost')
        if checker_name == 'Grass':
            return (not counter.get('Grass') and (
                'leafstorm' in move_pool or species.baseStats['atk'] >= 100 or
                types.has('Electric') or 'Seed Sower' in abilities))
        if checker_name == 'Ground':
            return not counter.get('Ground')
        if checker_name == 'Ice':
            return ('freezedry' in move_pool or 'blizzard' in move_pool or
                    not counter.get('Ice'))
        if checker_name == 'Normal':
            # NB: PS's Normal checker has a shifted parameter list
            # `(movePool, moves, types, counter)` but only reads movePool.
            return 'boomburst' in move_pool or 'hypervoice' in move_pool
        if checker_name == 'Poison':
            if types.has('Ground'):
                return False
            return not counter.get('Poison')
        if checker_name == 'Psychic':
            if (is_doubles or species.id == 'bruxish') and 'psychicfangs' in move_pool:
                return True
            if species.id == 'hoopaunbound' and 'psychic' in move_pool:
                return True
            if any(types.has(m) for m in ('Dark', 'Steel', 'Water')):
                return False
            return not counter.get('Psychic')
        if checker_name == 'Rock':
            return not counter.get('Rock') and species.baseStats['atk'] >= 80
        if checker_name == 'Steel':
            return (not counter.get('Steel') and
                    (is_doubles or species.baseStats['atk'] >= 90 or
                     'gigatonhammer' in move_pool or 'makeitrain' in move_pool))
        if checker_name == 'Water':
            return not counter.get('Water') and (not types.has('Ground') or is_doubles)
        # No checker for this type (Stellar, etc.)
        return False

    # -- randomMoveset (teams.ts 724-1048) ---------------------------------

    def random_moveset(self, types, abilities, team_details, species, is_lead,
                       move_pool, tera_type, role, is_doubles=False):
        moves = OrderedSet()
        counter = self.query_moves(moves, species, tera_type, abilities)
        self.cull_move_pool(types, moves, abilities, counter, move_pool,
                            team_details, species, is_lead, tera_type, role,
                            is_doubles)

        # If there are only four moves, add all moves and return early
        if len(move_pool) <= self.max_move_count:
            for moveid in move_pool:
                moves.add(moveid)
            return moves

        def run_enforcement_checker(checker_name):
            return self._run_enforcement_checker(
                checker_name, move_pool, moves, abilities, types, counter,
                species, team_details, is_lead, is_doubles, tera_type, role)

        def add(move):
            return self.add_move(move, moves, types, abilities, team_details,
                                 species, is_lead, move_pool, tera_type, role,
                                 is_doubles)

        if role == 'Tera Blast user':
            counter = add('terablast')

        # Add required move (e.g. Relic Song for Meloetta-P)
        if species.requiredMove:
            move = get_move(species.requiredMove).id
            counter = add(move)

        # Enforce Facade if Guts is a possible ability
        if 'facade' in move_pool and 'Guts' in abilities:
            counter = add('facade')

        # Enforce Night Shade, Revelation Dance, Revival Blessing, Sticky Web
        for moveid in ('nightshade', 'revelationdance', 'revivalblessing', 'stickyweb'):
            if moveid in move_pool:
                counter = add(moveid)

        # Enforce Trick Room on Doubles Wallbreaker
        if 'trickroom' in move_pool and role == 'Doubles Wallbreaker':
            counter = add('trickroom')

        # Enforce hazard removal on Bulky Support if the team doesn't have it
        # (teams.ts 788-797)
        if role == 'Bulky Support' and not team_details.get('defog') and not team_details.get('rapidSpin'):
            if 'rapidspin' in move_pool:
                counter = add('rapidspin')
            if 'defog' in move_pool:
                counter = add('defog')

        # Enforce Aurora Veil if the team doesn't already have screens
        # (teams.ts 800)
        if not team_details.get('screens') and 'auroraveil' in move_pool:
            counter = add('auroraveil')

        # Enforce Knock Off on pure Normal- and Fighting-types in singles
        if not is_doubles and types.size == 1 and (types.has('Normal') or types.has('Fighting')):
            if 'knockoff' in move_pool:
                counter = add('knockoff')

        # Enforce Spore on Smeargle
        if species.id == 'smeargle':
            if 'spore' in move_pool:
                counter = add('spore')

        if is_doubles:
            # SINGLES ONLY -- never taken.
            for moveid in ('mortalspin', 'spore'):
                if moveid in move_pool:
                    counter = add(moveid)
            if 'fakeout' in move_pool and species.baseStats['spe'] <= 50:
                counter = add('fakeout')
            if 'tailwind' in move_pool and ('Prankster' in abilities or 'Gale Wings' in abilities):
                counter = add('tailwind')

        # Enforce STAB priority
        if (role in ('Bulky Attacker', 'Bulky Setup', 'Wallbreaker', 'Doubles Wallbreaker') or
                species.id in self.priority_pokemon):
            priority_moves = []
            for moveid in move_pool:
                move = get_move(moveid)
                move_type = self.get_move_type(move, species, abilities, tera_type)
                if (types.has(move_type) and
                        (move.priority > 0 or
                         (moveid == 'grassyglide' and 'Grassy Surge' in abilities)) and
                        (move.basePower or move.basePowerCallback)):
                    priority_moves.append(moveid)
            if priority_moves:
                moveid = self.sample(priority_moves)
                counter = add(moveid)

        # Enforce STAB
        for type_ in list(types):
            stab_moves = []
            for moveid in move_pool:
                move = get_move(moveid)
                move_type = self.get_move_type(move, species, abilities, tera_type)
                if (moveid not in self.no_stab and
                        (move.basePower or move.basePowerCallback) and
                        type_ == move_type):
                    stab_moves.append(moveid)
            while run_enforcement_checker(type_):
                if not stab_moves:
                    break
                moveid = self.sample_no_replace(stab_moves)
                counter = add(moveid)

        # Enforce Tera STAB
        if not counter.get('stabtera') and role not in ('Bulky Support', 'Doubles Support'):
            stab_moves = []
            for moveid in move_pool:
                move = get_move(moveid)
                move_type = self.get_move_type(move, species, abilities, tera_type)
                if (moveid not in self.no_stab and
                        (move.basePower or move.basePowerCallback) and
                        tera_type == move_type):
                    stab_moves.append(moveid)
            if stab_moves:
                moveid = self.sample(stab_moves)
                counter = add(moveid)

        # If no STAB move was added, add a STAB move
        if not counter.get('stab'):
            stab_moves = []
            for moveid in move_pool:
                move = get_move(moveid)
                move_type = self.get_move_type(move, species, abilities, tera_type)
                if (moveid not in self.no_stab and
                        (move.basePower or move.basePowerCallback) and
                        types.has(move_type)):
                    stab_moves.append(moveid)
            if stab_moves:
                moveid = self.sample(stab_moves)
                counter = add(moveid)

        # Enforce recovery
        if role in ('Bulky Support', 'Bulky Attacker', 'Bulky Setup'):
            recovery_moves = [m for m in move_pool if m in RECOVERY_MOVES]
            if recovery_moves:
                moveid = self.sample(recovery_moves)
                counter = add(moveid)

        # Enforce pivoting moves on AV Pivot
        if role == 'AV Pivot':
            pivot_moves = [m for m in move_pool if m in ('uturn', 'voltswitch')]
            if pivot_moves:
                moveid = self.sample(pivot_moves)
                counter = add(moveid)

        # Enforce setup
        if 'Setup' in role or role == 'Tera Blast user':
            non_speed_setup_moves = [m for m in move_pool
                                     if m in SETUP and m not in SPEED_SETUP]
            if non_speed_setup_moves:
                moveid = self.sample(non_speed_setup_moves)
                counter = add(moveid)
            else:
                setup_moves = [m for m in move_pool if m in SETUP]
                if setup_moves:
                    moveid = self.sample(setup_moves)
                    counter = add(moveid)

        # Enforce redirecting moves and Fake Out on Doubles Support
        if role == 'Doubles Support':
            # SINGLES ONLY -- never taken.
            for moveid in ('fakeout', 'followme', 'ragepowder'):
                if moveid in move_pool:
                    counter = add(moveid)
            speed_control = [m for m in move_pool if m in SPEED_CONTROL]
            if speed_control:
                moveid = self.sample(speed_control)
                counter = add(moveid)

        # Enforce Protect
        if 'Protect' in role:
            protect_moves = [m for m in move_pool if m in PROTECT_MOVES]
            if protect_moves:
                moveid = self.sample(protect_moves)
                counter = add(moveid)

        # Enforce a move not on the noSTAB list
        if not len(counter.damagingMoves):
            attacking_moves = []
            for moveid in move_pool:
                move = get_move(moveid)
                if moveid not in self.no_stab and move.category != 'Status':
                    attacking_moves.append(moveid)
            if attacking_moves:
                moveid = self.sample(attacking_moves)
                counter = add(moveid)

        # Enforce coverage move
        if role not in ('AV Pivot', 'Fast Support', 'Bulky Support',
                        'Bulky Protect', 'Doubles Support'):
            if len(counter.damagingMoves) == 1:
                # NB: PS reads `.type` here, NOT getMoveType
                current_attack_type = next(iter(counter.damagingMoves)).type
                coverage_moves = []
                for moveid in move_pool:
                    move = get_move(moveid)
                    move_type = self.get_move_type(move, species, abilities, tera_type)
                    if moveid not in self.no_stab and (move.basePower or move.basePowerCallback):
                        if current_attack_type != move_type:
                            coverage_moves.append(moveid)
                if coverage_moves:
                    moveid = self.sample(coverage_moves)
                    counter = add(moveid)

        # Choose remaining moves randomly from movepool
        while moves.size < self.max_move_count and move_pool:
            if moves.size + len(move_pool) <= self.max_move_count:
                for moveid in move_pool:
                    moves.add(moveid)
                break
            moveid = self.sample(move_pool)
            counter = add(moveid)
            for pair in MOVE_PAIRS:
                if moveid == pair[0] and pair[1] in move_pool:
                    counter = add(pair[1])
                if moveid == pair[1] and pair[0] in move_pool:
                    counter = add(pair[0])
        return moves

    # -- randomSet (teams.ts 1471-1614) ------------------------------------

    def random_set(self, s, team_details=None, is_lead=False, is_doubles=False):
        if team_details is None:
            team_details = {}
        species = s if isinstance(s, Species) else get_species(s)
        forme = self.get_forme(species)
        sets = (self.random_doubles_sets if is_doubles else self.random_sets)[species.id]["sets"]
        possible_sets = []

        for set_ in sets:
            # Prevent Fast Bulky Setup on lead Paradox Pokemon, since it
            # generates Booster Energy.
            abilities = set_['abilities']
            if (is_lead and ('Protosynthesis' in abilities or 'Quark Drive' in abilities) and
                    set_['role'] == 'Fast Bulky Setup'):
                continue
            # Prevent Tera Blast user if the team already has one, or if
            # Terastallization is prevented.
            if ((team_details.get('teraBlast') or self.tera_stal_clause) and
                    set_['role'] == 'Tera Blast user'):
                continue
            possible_sets.append(set_)

        set_ = self.sample_if_array(possible_sets)
        role = set_['role']
        move_pool = []
        for movename in set_['movepool']:
            move_pool.append(get_move(movename).id)
        tera_types = set_['teraTypes']
        tera_type = self.sample_if_array(tera_types)

        evs = {'hp': 85, 'atk': 85, 'def': 85, 'spa': 85, 'spd': 85, 'spe': 85}
        ivs = {'hp': 31, 'atk': 31, 'def': 31, 'spa': 31, 'spd': 31, 'spe': 31}

        types = OrderedSet(species.types)
        abilities = set_['abilities']

        # Get moves
        moves = self.random_moveset(types, abilities, team_details, species,
                                    is_lead, move_pool, tera_type, role, is_doubles)
        counter = self.query_moves(moves, species, tera_type, abilities)

        # Get ability  [GROUP 3]
        ability = self.get_ability(types, moves, abilities, counter, team_details,
                                   species, is_lead, is_doubles, tera_type, role)

        # Get items  [GROUP 3]
        item = self.get_priority_item(ability, types, moves, counter, team_details,
                                      species, is_lead, tera_type, role, is_doubles)
        if item is None:
            if is_doubles:
                item = self.get_doubles_item(ability, types, moves, counter,
                                             team_details, species, is_lead,
                                             tera_type, role)
            else:
                item = self.get_item(ability, types, moves, counter, team_details,
                                     species, is_lead, tera_type, role)

        # Get level  [GROUP 3]
        level = self.get_level(species, is_doubles)

        # Prepare optimal HP
        sr_immunity = ability == 'Magic Guard' or item == 'Heavy-Duty Boots'
        sr_weakness = 0 if sr_immunity else rock_effectiveness(species.types)
        # Crash damage move users want an odd HP to survive two misses
        if any(moves.has(m) for m in ('axekick', 'highjumpkick', 'jumpkick', 'supercellslam')):
            sr_weakness = 2
        while evs['hp'] > 1:
            hp = int((2 * species.baseStats['hp'] + ivs['hp'] + evs['hp'] // 4 + 100)
                     * level // 100 + 10)
            if (moves.has('substitute') and item in ('Sitrus Berry',)) or species.id == 'minior':
                if hp % 4 == 0:
                    break
            elif ((moves.has('bellydrum') or moves.has('filletaway') or moves.has('shedtail')) and
                  (item == 'Sitrus Berry' or ability == 'Gluttony')):
                if hp % 2 == 0:
                    break
            elif moves.has('substitute') and moves.has('endeavor'):
                if hp % 4 > 0:
                    break
            else:
                if is_doubles:
                    break
                if sr_weakness <= 0 or ability == 'Regenerator' or item in ('Leftovers', 'Life Orb'):
                    break
                if item != 'Sitrus Berry' and hp % (4 / sr_weakness) > 0:
                    break
                if item == 'Sitrus Berry' and hp % (4 / sr_weakness) == 0:
                    break
            evs['hp'] -= 4

        # Minimize confusion damage
        def _no_attack_stat(m):
            move = get_move(m)
            if move.damageCallback or move.damage:
                return True
            if move.id == 'shellsidearm':
                return False
            if (move.id == 'terablast' and
                    (species.id == 'porygon2' or ability in ('Contrary', 'Defiant') or
                     moves.has('shiftgear') or
                     species.baseStats['atk'] > species.baseStats['spa'])):
                return False
            return move.category != 'Physical' or move.id in ('bodypress', 'foulplay')

        no_attack_stat_moves = all(_no_attack_stat(m) for m in moves)
        if (no_attack_stat_moves and not moves.has('transform') and
                self.format_mod != 'partnersincrime' and not self.force_of_the_fallen_mod):
            evs['atk'] = 0
            ivs['atk'] = 0

        if moves.has('gyroball') or moves.has('trickroom'):
            evs['spe'] = 0
            ivs['spe'] = 0

        # Enforce Tera Type after all set generation is done
        if self.force_tera_type:
            tera_type = self.force_tera_type

        # shuffle moves to add more randomness to camomons
        shuffled_moves = list(moves)
        self.prng.shuffle(shuffled_moves)
        return {
            'name': species.baseSpecies,
            'species': forme,
            'speciesId': species.id,
            # gender kept (it is free); shiny/happiness/nickname skipped -- they
            # cannot affect engine state.
            'gender': ('M' if species.baseSpecies == 'Greninja'
                       else (species.gender or ('F' if self.random(2) else 'M'))),
            'level': level,
            'moves': shuffled_moves,
            'ability': ability,
            'evs': evs,
            'ivs': ivs,
            'item': item,
            'teraType': tera_type,
            'role': role,
        }
