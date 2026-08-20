import os
import random
import unittest
from unittest import mock

import constants
from constants import BattleType
from data.pkmn_sets import RandomBattleTeamDatasets
from fp.battle import Battle, Move, Pokemon
from fp.helpers import normalize_name
from fp.search import random_battles
from fp.search.random_battles import (
    prepare_random_battles,
    sample_randombattle_pokemon,
)

PIKACHU_SET_KEY = "85,lightball,static,irontail,surf,thunderbolt,voltswitch,electric"
PIKACHU_OTHER_SET_KEY = (
    "85,lightball,lightningrod,irontail,surf,thunderbolt,voltswitch,water"
)
EEVEE_SET_KEY = "88,eviolite,adaptability,doubleedge,protect,quickattack,wish,normal"
ALOMOMOLA_SET_KEY = "87,heavydutyboots,regenerator,flipturn,protect,scald,wish,water"


class TestSampleRandombattlePokemon(unittest.TestCase):
    def setUp(self):
        RandomBattleTeamDatasets.initialize("gen9randombattle")

    def test_sampled_fill_in_pokemon_is_not_revealed(self):
        pkmn = sample_randombattle_pokemon([])

        self.assertFalse(pkmn.revealed)


class TestShowdownTeamConstraints(unittest.TestCase):
    @staticmethod
    def _pokemon(name, level=80, ability=None):
        pkmn = Pokemon(name, level)
        pkmn.ability = ability
        return pkmn

    def test_freeze_dry_cap_and_water_ice_exception(self):
        freeze_dry_weak = [
            self._pokemon(name)
            for name in ["alomomola", "pelipper", "quagsire", "vaporeon", "blastoise"]
        ]

        self.assertFalse(
            random_battles._more_than_4_pokemon_weak_to_freeze_dry(freeze_dry_weak[:4])
        )
        self.assertTrue(
            random_battles._more_than_4_pokemon_weak_to_freeze_dry(freeze_dry_weak)
        )
        self.assertFalse(
            random_battles._more_than_4_pokemon_weak_to_freeze_dry(
                freeze_dry_weak[:4] + [self._pokemon("lapras")]
            ),
            "Water/Ice double-resists Ice before Freeze-Dry's Water adjustment",
        )

    def test_fire_cap_counts_dry_skin_and_fluffy_only_when_fire_is_neutral(self):
        fire_weak = [
            self._pokemon(name) for name in ["scizor", "abomasnow", "leavanny"]
        ]
        toxicroak = self._pokemon("toxicroak", ability="dryskin")

        self.assertTrue(
            random_battles._more_than_3_pokemon_weak_to_fire_including_abilities(
                fire_weak + [toxicroak]
            )
        )
        toxicroak.ability = "poisontouch"
        self.assertFalse(
            random_battles._more_than_3_pokemon_weak_to_fire_including_abilities(
                fire_weak + [toxicroak]
            )
        )
        bewear = self._pokemon("bewear", ability="fluffy")
        self.assertTrue(
            random_battles._more_than_3_pokemon_weak_to_fire_including_abilities(
                fire_weak + [bewear]
            )
        )

    def test_level_100_cap(self):
        self.assertFalse(
            random_battles._more_than_1_level_100_pokemon(
                [self._pokemon("sunflora", 100), self._pokemon("pikachu", 99)]
            )
        )
        self.assertTrue(
            random_battles._more_than_1_level_100_pokemon(
                [self._pokemon("sunflora", 100), self._pokemon("luvdisc", 100)]
            )
        )

    def test_singles_incompatibility_groups(self):
        incompatible_pairs = [
            ("blissey", "chansey"),
            ("illumise", "volbeat"),
            ("galvantula", "spidops"),
            ("grimmsnarl", "ninetalesalola"),
            ("toxicroak", "torkoal"),
        ]
        for left, right in incompatible_pairs:
            with self.subTest(left=left, right=right):
                self.assertTrue(
                    random_battles._has_incompatible_pokemon(
                        [self._pokemon(left), self._pokemon(right)]
                    )
                )
        self.assertFalse(
            random_battles._has_incompatible_pokemon(
                [self._pokemon("blissey"), self._pokemon("volbeat")]
            )
        )

    def test_caps_use_team_entry_types_not_battle_time_types(self):
        greninja = self._pokemon("greninja")
        greninja.types = ["ice"]
        water_team = [
            greninja,
            self._pokemon("empoleon"),
            self._pokemon("poliwrath"),
        ]
        self.assertTrue(random_battles._more_than_2_pokemon_of_any_type(water_team))
        self.assertFalse(
            random_battles._more_than_2_pokemon_of_any_type(
                water_team, use_team_building_types=False
            )
        )

        pawmot = self._pokemon("pawmot")
        pawmot.types = ["???", "fighting"]
        ground_weak_team = [
            pawmot,
            self._pokemon("pikachu"),
            self._pokemon("toxicroak"),
            self._pokemon("coalossal"),
        ]
        self.assertTrue(
            random_battles._more_than_3_pokemon_weak_to_a_given_typing(ground_weak_team)
        )
        self.assertFalse(
            random_battles._more_than_3_pokemon_weak_to_a_given_typing(
                ground_weak_team, use_team_building_types=False
            )
        )

        coalossal = self._pokemon("coalossal")
        magcargo = self._pokemon("magcargo")
        coalossal.types = ["normal"]
        magcargo.types = ["normal"]
        self.assertTrue(
            random_battles._more_than_1_pokemon_with_4x_weakness([coalossal, magcargo])
        )
        self.assertFalse(
            random_battles._more_than_1_pokemon_with_4x_weakness(
                [coalossal, magcargo], use_team_building_types=False
            )
        )


class TestShowdownConstraintSamplingControl(unittest.TestCase):
    def setUp(self):
        RandomBattleTeamDatasets.__init__()
        RandomBattleTeamDatasets.pkmn_mode = "gen9randombattle"
        RandomBattleTeamDatasets.raw_pkmn_sets = {
            "alomomola": {ALOMOMOLA_SET_KEY: 100},
            "pikachu": {PIKACHU_SET_KEY: 100},
        }
        RandomBattleTeamDatasets._initialize_pkmn_sets()
        self.addCleanup(RandomBattleTeamDatasets.__init__)

    @staticmethod
    def _sample_with_species_draws(species_draws, control_value=""):
        draws = iter(species_draws)
        species_calls = 0

        def fake_choices(population, weights=None):
            nonlocal species_calls
            if population is RandomBattleTeamDatasets.species_sample_names:
                species_calls += 1
                return [next(draws)]
            return [population[0]]

        existing = [Pokemon("vaporeon", 80), Pokemon("blastoise", 80)]
        with (
            mock.patch.dict(
                os.environ,
                {random_battles.SHOWDOWN_TEAM_CONSTRAINTS_CONTROL_OFF: (control_value)},
            ),
            mock.patch(
                "fp.search.random_battles.random.choices",
                side_effect=fake_choices,
            ),
        ):
            sampled = sample_randombattle_pokemon(existing)
        return sampled, species_calls

    def test_negative_control_restores_tenth_draw_give_up(self):
        draws = ["alomomola"] * 10 + ["pikachu"]

        fixed, fixed_calls = self._sample_with_species_draws(draws)
        legacy, legacy_calls = self._sample_with_species_draws(draws, control_value="1")

        self.assertEqual(("pikachu", 11), (fixed.name, fixed_calls))
        self.assertEqual(("alomomola", 10), (legacy.name, legacy_calls))

    def test_exhaustive_fallback_terminates_with_valid_support(self):
        with mock.patch.object(random_battles, "MAX_RANDOM_SAMPLING_ATTEMPTS", 3):
            sampled, species_calls = self._sample_with_species_draws(["alomomola"] * 3)

        self.assertEqual(("pikachu", 3), (sampled.name, species_calls))


class TestWeightedSampling(unittest.TestCase):
    def setUp(self):
        RandomBattleTeamDatasets.__init__()
        RandomBattleTeamDatasets.raw_pkmn_sets = {
            "pikachu": {
                PIKACHU_SET_KEY: 90,
                PIKACHU_OTHER_SET_KEY: 10,
            },
            "eevee": {EEVEE_SET_KEY: 300},
        }
        RandomBattleTeamDatasets._initialize_pkmn_sets()
        self.addCleanup(RandomBattleTeamDatasets.__init__)

    def test_species_sampling_weights_are_total_set_counts(self):
        species_weights = dict(
            zip(
                RandomBattleTeamDatasets.species_sample_names,
                RandomBattleTeamDatasets.species_sample_weights,
            )
        )

        self.assertEqual({"pikachu": 100, "eevee": 300}, species_weights)

    def test_sample_draws_species_and_set_weighted_by_count(self):
        captured_calls = []

        def fake_choices(population, weights=None):
            captured_calls.append((list(population), list(weights)))
            return [population[0]]

        with mock.patch(
            "fp.search.random_battles.random.choices", side_effect=fake_choices
        ):
            pkmn = sample_randombattle_pokemon([])

        self.assertEqual(2, len(captured_calls))

        species_population, species_weights = captured_calls[0]
        self.assertEqual(["pikachu", "eevee"], species_population)
        self.assertEqual([100, 300], species_weights)

        set_population, set_weights = captured_calls[1]
        self.assertEqual(RandomBattleTeamDatasets.pkmn_sets["pikachu"], set_population)
        # sets are sorted by count descending during initialization
        self.assertEqual([90, 10], set_weights)

        self.assertEqual("pikachu", pkmn.name)
        # the drawn set is the first of the population (count=90 set)
        self.assertEqual("static", pkmn.ability)
        self.assertEqual("lightball", pkmn.item)


class TestTransformedMonSetSampling(unittest.TestCase):
    """A transformed mon (Ditto/Imposter) carries COPIED moves/ability that
    match no real set of its base species: set prediction must fall back to
    item-knowledge-only matching on the base species, and world preparation
    must apply ONLY the item (never the set's moves/spread). Forensic: a
    transformed Ditto's Choice Scarf was absent from every sampled world, so
    the engine priced a lost priority mirror as a 50/50 speed tie."""

    def setUp(self):
        RandomBattleTeamDatasets.__init__()
        RandomBattleTeamDatasets.raw_pkmn_sets = {
            "ditto": {"87,choicescarf,imposter,transform,ghost": 100},
        }
        RandomBattleTeamDatasets._initialize_pkmn_sets()
        self.addCleanup(RandomBattleTeamDatasets.__init__)

    def _transformed_ditto(self):
        pkmn = Pokemon("ditto", 87)
        pkmn.transformed_into = "banette"
        pkmn.item = constants.UNKNOWN_ITEM
        pkmn.moves = []
        for mv in ["shadowsneak", "gunkshot", "thunderwave", "poltergeist"]:
            pkmn.add_move(mv)
        return pkmn

    def test_transformed_mon_matches_base_species_sets_on_item_only(self):
        pkmn = self._transformed_ditto()
        remaining = RandomBattleTeamDatasets.get_all_remaining_sets(pkmn)
        self.assertEqual(1, len(remaining))
        self.assertEqual("choicescarf", remaining[0].pkmn_set.item)

    def test_transformed_mon_with_impossible_item_excludes_that_set(self):
        pkmn = self._transformed_ditto()
        pkmn.impossible_items.add("choicescarf")
        remaining = RandomBattleTeamDatasets.get_all_remaining_sets(pkmn)
        self.assertEqual([], remaining)

    def test_transformed_item_only_fallback_enforces_rejected_signature(self):
        pkmn = self._transformed_ditto()
        signature = RandomBattleTeamDatasets.pkmn_sets["ditto"][0].mechanics_signature()
        pkmn.rejected_set_signatures.add(signature)

        remaining = RandomBattleTeamDatasets.get_all_remaining_sets(pkmn)

        self.assertEqual([], remaining)

    def test_world_preparation_applies_item_but_keeps_copied_moves(self):
        battle = Battle("battle-tag")
        battle.battle_type = BattleType.RANDOM_BATTLE
        battle.opponent.active = self._transformed_ditto()
        battle.user.active = Pokemon("banette", 93)
        for i in range(5):
            filler = Pokemon("pikachu", 80)
            filler.hp = 0
            battle.opponent.reserve.append(filler)
            battle.user.reserve.append(filler)

        sampled = prepare_random_battles(battle, 1)
        world_active = sampled[0][0].opponent.active
        self.assertEqual("choicescarf", world_active.item)
        self.assertEqual(
            ["shadowsneak", "gunkshot", "thunderwave", "poltergeist"],
            [m.name for m in world_active.moves],
            "copied moves must not be replaced by the base species' set moves",
        )



class TestPersistentRejectedSetSampling(unittest.TestCase):
    def setUp(self):
        RandomBattleTeamDatasets.__init__()
        RandomBattleTeamDatasets.raw_pkmn_sets = {
            "eevee": {
                EEVEE_SET_KEY: 90,
                (
                    "88,choicescarf,runaway,doubleedge,protect,"
                    "quickattack,wish,ghost"
                ): 10,
            }
        }
        RandomBattleTeamDatasets._initialize_pkmn_sets()
        self.addCleanup(RandomBattleTeamDatasets.__init__)

    @staticmethod
    def _battle():
        battle = Battle("battle-tag")
        battle.battle_type = BattleType.RANDOM_BATTLE
        battle.opponent.active = Pokemon("eevee", 88)
        battle.user.active = Pokemon("pikachu", 80)
        for _ in range(5):
            filler = Pokemon("caterpie", 80)
            filler.hp = 0
            battle.opponent.reserve.append(filler)
        return battle

    def test_repeated_world_sampling_never_reintroduces_rejected_set(self):
        battle = self._battle()
        rejected = RandomBattleTeamDatasets.pkmn_sets["eevee"][0]
        self.assertEqual("eviolite", rejected.pkmn_set.item)
        battle.opponent.active.rejected_set_signatures.add(
            rejected.mechanics_signature()
        )

        sampled = prepare_random_battles(battle, 20)

        self.assertEqual(
            {"choicescarf"},
            {world.opponent.active.item for world, _weight in sampled},
        )

    def test_disabling_ledger_reproduces_reintroduction_negative_control(self):
        battle = self._battle()
        rejected = RandomBattleTeamDatasets.pkmn_sets["eevee"][0]
        battle.opponent.active.rejected_set_signatures.add(
            rejected.mechanics_signature()
        )
        battle.opponent.active.rejected_set_signatures.clear()

        # 20 worlds rather than 1 forced pick (Sally 2026-08-20).
        # `_stratified_active_plan` now draws a random systematic start, so the
        # single-world case is a draw over the candidates instead of a fixed
        # selection, and mocking shuffle+choices no longer pins it. The claim
        # this control actually makes is that clearing the ledger puts the
        # rejected set back within REACH -- the exact thing the mirror test
        # above asserts is impossible while the signature is recorded.
        random.seed(0)
        sampled = prepare_random_battles(battle, 20)

        self.assertIn(
            "eviolite",
            {world.opponent.active.item for world, _weight in sampled},
        )


class TestEntryIntentReweighting(unittest.TestCase):
    """SPEC entry-intent: sets that justify a CHOSEN entry (recorded in
    Pokemon.entry_contexts) get upweighted in the sampling draw."""

    def _pikachu_with_context(self):
        from data.pkmn_sets import PokemonMoveset, PokemonSet, PredictedPokemonSet

        pkmn = Pokemon("pikachukalos", 93)
        # our low-HP mon that OUTSPEEDS pikachu: only a priority move can
        # revenge it before it acts, so Fake Out is the discriminating signal
        pkmn.entry_contexts.append(
            {
                "vs_hp": 20,
                "vs_maxhp": 246,
                "vs_types": ["psychic"],
                "vs_defense": 149,
                "vs_special_defense": 208,
                "vs_speed": 250,
            }
        )
        fakeout_set = PredictedPokemonSet(
            pkmn_set=PokemonSet(
                nature="serious",
                item="lightball",
                ability="lightningrod",
                evs=[85] * 6,
                count=10,
            ),
            pkmn_moveset=PokemonMoveset(
                moves=["fakeout", "knockoff", "surf", "volttackle"],
            ),
        )
        no_fakeout_set = PredictedPokemonSet(
            pkmn_set=PokemonSet(
                nature="serious",
                item="lightball",
                ability="lightningrod",
                evs=[85] * 6,
                count=10,
            ),
            pkmn_moveset=PokemonMoveset(
                moves=["playrough", "knockoff", "surf", "voltswitch"],
            ),
        )
        return pkmn, [fakeout_set, no_fakeout_set]

    def test_priority_ko_set_is_upweighted_on_revenge_entry(self):
        pkmn, sets = self._pikachu_with_context()
        multipliers = random_battles.entry_intent_multipliers(pkmn, sets)
        self.assertGreater(
            multipliers[0],
            multipliers[1] * 2,
            "the Fake Out set must be sharply likelier after a revenge entry",
        )
        weights = random_battles.entry_weighted_counts(pkmn, sets)
        self.assertGreater(weights[0], weights[1] * 2)

    def test_no_entry_context_leaves_weights_untouched(self):
        pkmn, sets = self._pikachu_with_context()
        pkmn.entry_contexts = []
        self.assertEqual(
            [1.0, 1.0], random_battles.entry_intent_multipliers(pkmn, sets)
        )

    def test_negative_control_restores_count_prior(self):
        pkmn, sets = self._pikachu_with_context()
        with mock.patch.dict(
            os.environ, {random_battles.ENTRY_INTENT_CONTROL_OFF: "1"}
        ):
            self.assertEqual(
                [1.0, 1.0], random_battles.entry_intent_multipliers(pkmn, sets)
            )


class TestRevengeCertainSets(unittest.TestCase):
    """CERTAIN-REVENGE: a chosen entry with an open revenge window samples
    only sets carrying a priority KO on our current active, locked to it."""

    def _setup(self):
        from data.pkmn_sets import PokemonMoveset, PokemonSet, PredictedPokemonSet
        from fp.battle import Move

        espeon = Pokemon("espeon", 84)
        espeon.hp = 20
        # our active's known retaliation makes slower kills costly: without
        # it every kill would look free and nothing would be unique
        espeon.moves = [Move("psyshock")]
        pikachu = Pokemon("pikachukalos", 93)
        pikachu.active_move_actions = 0
        pikachu.entry_contexts.append({"vs_name": "espeon", "vs_hp": 20})
        fakeout_set = PredictedPokemonSet(
            pkmn_set=PokemonSet(
                nature="serious",
                item="lightball",
                ability="lightningrod",
                evs=[85] * 6,
                count=10,
            ),
            pkmn_moveset=PokemonMoveset(
                moves=["fakeout", "knockoff", "surf", "volttackle"],
            ),
        )
        other_set = PredictedPokemonSet(
            pkmn_set=PokemonSet(
                nature="serious",
                item="lightball",
                ability="lightningrod",
                evs=[85] * 6,
                count=10,
            ),
            pkmn_moveset=PokemonMoveset(
                moves=["playrough", "knockoff", "surf", "voltswitch"],
            ),
        )
        return pikachu, espeon, [fakeout_set, other_set]

    def test_priority_ko_sets_become_exclusive(self):
        pikachu, espeon, sets = self._setup()
        certain, moves = random_battles.revenge_certain_sets(pikachu, espeon, sets)
        self.assertEqual(1, len(certain))
        self.assertIn("fakeout", certain[0].pkmn_moveset.moves)
        self.assertEqual({"fakeout"}, moves)

    def test_closed_revenge_window_disables_certainty(self):
        pikachu, espeon, sets = self._setup()
        pikachu.active_move_actions = 1
        certain, moves = random_battles.revenge_certain_sets(pikachu, espeon, sets)
        self.assertIsNone(certain)

    def test_entry_against_a_different_mon_disables_certainty(self):
        pikachu, espeon, sets = self._setup()
        pikachu.entry_contexts[-1]["vs_name"] = "gothitelle"
        certain, moves = random_battles.revenge_certain_sets(pikachu, espeon, sets)
        self.assertIsNone(certain)

    def test_homogeneous_pool_disables_certainty(self):
        # every candidate set has the priority kill: the entry choice reveals
        # nothing about the set, so no lock
        pikachu, espeon, sets = self._setup()
        both_fakeout = [sets[0], sets[0]]
        certain, moves = random_battles.revenge_certain_sets(
            pikachu, espeon, both_fakeout
        )
        self.assertIsNone(certain)

    def test_faster_mon_with_second_killing_move_is_ambiguous(self):
        # opponent outspeeds AND holds another killing move: the priority
        # move is no longer the unique best line, so no lock
        pikachu, espeon, sets = self._setup()
        espeon.stats[constants.SPEED] = 150
        certain, moves = random_battles.revenge_certain_sets(pikachu, espeon, sets)
        self.assertIsNone(certain)


class TestEmptyCandidateSetFallback(unittest.TestCase):
    """A mon whose evidence eliminated every set must still be populated."""

    def setUp(self):
        RandomBattleTeamDatasets.__init__()
        RandomBattleTeamDatasets.raw_pkmn_sets = {
            "eevee": {EEVEE_SET_KEY: 90},
        }
        RandomBattleTeamDatasets._initialize_pkmn_sets()
        self.addCleanup(RandomBattleTeamDatasets.__init__)

        self.battle = Battle("battle-tag")
        self.battle.battle_type = BattleType.RANDOM_BATTLE
        self.battle.opponent.active = Pokemon("eevee", 88)
        self.battle.user.active = Pokemon("pikachu", 80)
        for _ in range(5):
            filler = Pokemon("caterpie", 80)
            filler.hp = 0
            self.battle.opponent.reserve.append(filler)

    def test_mon_matching_no_set_is_populated_from_the_unfiltered_list(self):
        # a move no eevee set has (illusion residue / dataset drift) empties
        # the candidate list via full_set_pkmn_can_have_moves
        active = self.battle.opponent.active
        active.add_move("willowisp")
        self.assertEqual(
            [], RandomBattleTeamDatasets.get_all_remaining_sets(active)
        )

        sampled = prepare_random_battles(self.battle, 1)
        world_active = sampled[0][0].opponent.active

        # the revealed move survives, and it comes first
        self.assertEqual("willowisp", world_active.moves[0].name)
        # the mon is no longer a blank: item / ability / moveset are filled
        self.assertNotEqual(constants.UNKNOWN_ITEM, world_active.item)
        self.assertIsNotNone(world_active.ability)
        self.assertEqual(4, len(world_active.moves))

    def test_unknown_species_leaves_todays_behaviour(self):
        active = self.battle.opponent.active = Pokemon("mrmime", 88)
        active.add_move("willowisp")

        sampled = prepare_random_battles(self.battle, 1)
        world_active = sampled[0][0].opponent.active

        self.assertEqual([("willowisp")], [m.name for m in world_active.moves])
        self.assertEqual(constants.UNKNOWN_ITEM, world_active.item)


class TestCertainRevengeLockRetired(unittest.TestCase):
    """REGRESSION (Sally 2026-08-20). `revenge_certain_sets` still COMPUTES a
    lock -- its own tests above still pass -- but `prepare_random_battles` no
    longer APPLIES it. A sampled world must carry the whole set: a truncated
    moveset hands every move it drops a probability of exactly zero, which the
    search then cannot price at all.

    Ladder game 2667910469 turn 5: the lock sampled Volcarona with
    ['fireblast'] only, so Quiver Dance -- present in 38/38 of its candidate
    sets, i.e. certain -- was unsearchable in all 8 worlds. The opponent
    clicked Quiver Dance. Re-running the decision with complete sets moved the
    argmax off `switch chimecho` entirely (95.0% -> 0.4%).
    """

    def setUp(self):
        RandomBattleTeamDatasets.__init__()
        RandomBattleTeamDatasets.raw_pkmn_sets = {
            "pikachukalos": {
                "93,lightball,lightningrod,"
                "fakeout,knockoff,surf,volttackle,ghost": 10,
                "93,lightball,lightningrod,"
                "playrough,knockoff,surf,voltswitch,ghost": 10,
            }
        }
        RandomBattleTeamDatasets._initialize_pkmn_sets()
        self.addCleanup(RandomBattleTeamDatasets.__init__)

    def test_sampled_worlds_keep_the_whole_moveset(self):
        from fp.battle import Move

        espeon = Pokemon("espeon", 84)
        espeon.hp = 20
        espeon.moves = [Move("psyshock")]
        pikachu = Pokemon("pikachukalos", 93)
        pikachu.active_move_actions = 0
        # FULL context shape, matching what battle_modifier.py:1352 records in
        # a real game -- with the lock gone the graded entry-intent weights now
        # run on entries that used to short-circuit into the lock, and they
        # read every one of these fields.
        ctx = dict(random_battles._fresh_entry_context(espeon))
        ctx["vs_name"] = "espeon"
        pikachu.entry_contexts.append(ctx)

        # the fixture must still SATISFY the lock's premise, or this test
        # would pass for the wrong reason
        _certain, moves = random_battles.revenge_certain_sets(
            pikachu, espeon, RandomBattleTeamDatasets.pkmn_sets["pikachukalos"]
        )
        self.assertEqual({"fakeout"}, moves, "fixture no longer trips the lock")

        battle = Battle("battle-tag")
        battle.battle_type = BattleType.RANDOM_BATTLE
        battle.user.active = espeon
        battle.opponent.active = pikachu
        for _ in range(5):
            filler = Pokemon("caterpie", 80)
            filler.hp = 0
            battle.opponent.reserve.append(filler)
            battle.user.reserve.append(filler)

        sampled = prepare_random_battles(battle, 8)
        self.assertEqual(8, len(sampled))
        for world, _chance in sampled:
            self.assertEqual(
                4,
                len(world.opponent.active.moves),
                "moveset truncated -- the retired certain-revenge lock is back",
            )


class TestBattleOnlyFormeKeepsItsIdentity(unittest.TestCase):
    """REGRESSION (Sally 2026-08-20, finding #15).

    PS's getForme returns `species.battleOnly` for a battle-only forme, so the
    set dict for a generated Zacian-Crowned carries species "Zacian". Naming
    the sampled mon from that display name put the CROWNED set -- Rusted Sword,
    Behemoth Blade, level 64 -- onto base Zacian's mono-Fairy 120-Atk body.
    Caught in live validation run 20260820-141359 at 2/1680 sampled instances;
    the level is the tell, since L64 exists only in the Crowned pool.
    """

    def _ps_set(self, display, species_id, level, moves, item, ability):
        return {
            "species": display, "speciesId": species_id, "level": level,
            "moves": moves, "ability": ability, "item": item,
            "teraType": "Fighting",
            "evs": {k: 85 for k in ("hp", "atk", "def", "spa", "spd", "spe")},
            "ivs": {k: 31 for k in ("hp", "atk", "def", "spa", "spd", "spe")},
        }

    def test_zacian_crowned_is_not_collapsed_to_base(self):
        pkmn = random_battles._pokemon_from_ps_set(self._ps_set(
            "Zacian", "zaciancrowned", 64,
            ["behemothblade", "closecombat", "playrough", "swordsdance"],
            "Rusted Sword", "Intrepid Sword"))
        self.assertEqual("zaciancrowned", pkmn.name)
        self.assertIn("steel", pkmn.types, "lost the Crowned Steel typing")
        self.assertEqual(64, pkmn.level)

    def test_zamazenta_crowned_is_not_collapsed_to_base(self):
        pkmn = random_battles._pokemon_from_ps_set(self._ps_set(
            "Zamazenta", "zamazentacrowned", 68,
            ["bodypress", "heavyslam", "irondefense", "stoneedge"],
            "Rusted Shield", "Dauntless Shield"))
        self.assertEqual("zamazentacrowned", pkmn.name)
        self.assertIn("steel", pkmn.types)
        self.assertEqual(68, pkmn.level)

    def test_ordinary_species_still_uses_the_display_name(self):
        # the display name is CORRECT for everything that is not battle-only,
        # including cosmetic formes -- the fix must not hijack those
        pkmn = random_battles._pokemon_from_ps_set(self._ps_set(
            "Krookodile", "krookodile", 79,
            ["stoneedge", "stealthrock", "earthquake", "knockoff"],
            "Life Orb", "Intimidate"))
        self.assertEqual("krookodile", pkmn.name)


class TestTeamAwareRevealedDraws(unittest.TestCase):
    """REGRESSION (Sally 2026-08-20, finding #13).

    Each revealed opponent mon used to draw its set from `random.choices` with
    no cross-mon state, so nothing stopped two of them rolling Stealth Rock --
    a team Showdown can never build. PS culls stealthrock from every later
    member once teamDetails.stealthRock is set (teams.ts:516), and 0 of 100,000
    generated teams carry two. Validation runs vg_t2 and vg_f2 had 14 and 13
    such worlds; with the shipped counts, P(SR|azelf)=0.150 and
    P(SR|camerupt)=0.885 predict ~13% of worlds.
    """

    def setUp(self):
        # real dataset: this test needs azelf/camerupt's actual set counts, and
        # initialize() also sets pkmn_mode, without which _ps_sampler_enabled()
        # is False and the fill path drops to the marginal fallback sampler
        RandomBattleTeamDatasets.initialize("gen9randombattleblitz", set())
        self.addCleanup(RandomBattleTeamDatasets.__init__)

    @staticmethod
    def _battle():
        battle = Battle("battle-tag")
        battle.battle_type = BattleType.RANDOM_BATTLE
        battle.opponent.active = Pokemon("tsareena", 87)
        for name, level in (("azelf", 82), ("camerupt", 91), ("vaporeon", 86)):
            p = Pokemon(name, level)
            p.item = constants.UNKNOWN_ITEM
            battle.opponent.reserve.append(p)
        battle.user.active = Pokemon("gallade", 80)
        for _ in range(5):
            f = Pokemon("caterpie", 80)
            f.hp = 0
            battle.user.reserve.append(f)
        return battle

    def test_no_world_gets_two_stealth_rock_users(self):
        random.seed(5)
        worlds = 0
        for _ in range(25):
            for world, _chance in prepare_random_battles(self._battle(), 8):
                worlds += 1
                team = [world.opponent.active] + list(world.opponent.reserve)
                users = [
                    p.name for p in team
                    if any(m.name == "stealthrock" for m in p.moves)
                ]
                self.assertLessEqual(
                    len(users), 1,
                    "two stealth rock users in one world: %s" % users,
                )
        self.assertEqual(200, worlds)

    def test_a_confirmed_move_is_never_filtered_away(self):
        # a mon that has genuinely been SEEN using the locked move must still be
        # samplable -- the filter falls back to the unconstrained draw when no
        # candidate avoids it, which is PS's own behaviour
        random.seed(6)
        battle = self._battle()
        camerupt = battle.opponent.reserve[1]
        camerupt.moves = [Move("stealthrock")]
        for world, _chance in prepare_random_battles(battle, 8):
            got = next(p for p in world.opponent.reserve if p.name == "camerupt")
            self.assertIn("stealthrock", [m.name for m in got.moves])


class TestTransformedMonKeepsATeraType(unittest.TestCase):
    """REGRESSION (finding #7).

    A transformed mon had only its ITEM sampled, so `tera_type` stayed None and
    the serializer defaulted it: poke_engine_helpers.py:384
    `tera_type=pkmn.tera_type or "typeless"`. The engine scores the typeless
    column 1.0 against everything, so the search priced the mon's tera arm as
    all-neutral AND STAB-less while still offering it. Transform does not copy
    tera type in PS, so the correct value is DITTO's own -- and it was already
    present on the base-species set being sampled for the item.

    Measured across all 1,520 archived ladder games: 2,810 serializations
    carried tera_type=TYPELESS, every one a transformed mon. Reproduced live in
    validation batch 20260820-155152 game g78 (Ditto copying Slowbro-Galar,
    TYPELESS in 24 worlds).

    Uses the REAL dataset: the stubbed raw key in TestTransformedMonSetSampling
    is parsed by `_initialize_pkmn_sets` with the tera field absorbed as a move,
    so it cannot express this property.
    """

    def setUp(self):
        RandomBattleTeamDatasets.initialize("gen9randombattleblitz", set())
        self.addCleanup(RandomBattleTeamDatasets.__init__)

    def test_transformed_active_is_never_serialized_typeless(self):
        battle = Battle("battle-tag")
        battle.battle_type = BattleType.RANDOM_BATTLE
        ditto = Pokemon("ditto", 87)
        ditto.transformed_into = "banette"
        ditto.item = constants.UNKNOWN_ITEM
        ditto.moves = []
        for mv in ["shadowsneak", "gunkshot", "thunderwave", "poltergeist"]:
            ditto.add_move(mv)
        battle.opponent.active = ditto
        battle.user.active = Pokemon("banette", 93)
        for _ in range(5):
            filler = Pokemon("pikachu", 80)
            filler.hp = 0
            battle.opponent.reserve.append(filler)
            battle.user.reserve.append(filler)

        seen = set()
        for world, _chance in prepare_random_battles(battle, 8):
            active = world.opponent.active
            self.assertIsNotNone(
                active.tera_type, "transformed mon would serialize as TYPELESS"
            )
            self.assertNotEqual("typeless", active.tera_type)
            seen.add(active.tera_type)
            # the copied moves must still survive the tera fill
            self.assertEqual(
                ["shadowsneak", "gunkshot", "thunderwave", "poltergeist"],
                [m.name for m in active.moves],
            )
        # ditto's own two tera types, not the copy target's
        self.assertTrue(seen <= {"ghost", "steel"}, seen)


class TestSpeciesClauseResolvesCosmeticFormes(unittest.TestCase):
    """REGRESSION (finding #6).

    Species Clause is tested on `species.baseSpecies` (_ps_team_loop.py:551
    seeds the table, :650 enforces it), but foul-play's pokedex.json gives
    COSMETIC formes no `baseSpecies` -- PS lists them on the base entry's
    `cosmeticFormes` -- so `ps_teams.Species` fell back to the display name and
    every colour keyed as a separate species. A revealed Florges-Blue therefore
    did not block an invented Florges-White.

    Measured before the fix: florgesblue 94/8000 fill sets contained a second
    Florges, sawsbucksummer 55/8000, alcremierubycream 95/8000, miniorblue
    47/8000 -- against 0/8000 for the same run keyed on the base id. Seen live
    in validation game g13 (two Florges in 3 sampled worlds). PS holds 0 of
    100,000 generated teams with two of a species.
    """

    def test_cosmetic_formes_share_their_base_species_key(self):
        from fp.search import ps_teams

        for forme, base in (
            ("florgesblue", "Florges"), ("florgeswhite", "Florges"),
            ("sawsbucksummer", "Sawsbuck"), ("alcremierubycream", "Alcremie"),
            ("miniorblue", "Minior"), ("gastrodoneast", "Gastrodon"),
        ):
            with self.subTest(forme=forme):
                self.assertEqual(base, ps_teams.get_species(forme).baseSpecies)
        # the BASE species must produce the same key, or nothing collides
        self.assertEqual("Florges", ps_teams.get_species("florges").baseSpecies)

    def test_battle_formes_are_not_collapsed(self):
        # battle formes differ in stats and are a separate pool entry; only
        # COSMETIC ones may share a key
        from fp.search import ps_teams

        self.assertEqual("Zacian", ps_teams.get_species("zaciancrowned").baseSpecies)
        self.assertEqual("Zacian", ps_teams.get_species("zacian").baseSpecies)

    def test_fill_ins_never_duplicate_a_revealed_cosmetic_forme(self):
        from fp.search import ps_teams
        from data.pkmn_sets import COSMETIC_FORME_TO_BASE

        ps_teams.seed(4242)
        for revealed in ("florgesblue", "sawsbucksummer"):
            base = COSMETIC_FORME_TO_BASE[revealed]
            for _ in range(150):
                fills = ps_teams.complete_team([{
                    "speciesId": revealed, "ability": "", "moves": [],
                    "level": 80}])
                for f in fills:
                    fid = normalize_name(f["speciesId"])
                    self.assertNotEqual(
                        base, COSMETIC_FORME_TO_BASE.get(fid, fid),
                        "%s fill duplicated the revealed %s" % (fid, revealed),
                    )


class TestZoroarkReachableAsAFillIn(unittest.TestCase):
    """REGRESSION (finding #14, the reachability half).

    teams.ts:1767-1768 says Illusion may not be the LAST MON GENERATED:
        if (species.baseSpecies === 'Zoroark' && pokemon.length >= (this.maxTeamSize - 1)) continue;
    `pokemon.length` counts what PS has BUILT. Ported with a raw len(pokemon)
    it became a team-MEMBERSHIP rule, and in continuation mode `pokemon` is
    pre-seeded with the revealed mons -- so with 5 of 6 revealed the single
    fill was judged at len(pokemon)==5 >= 5 and every Zoroark forme was
    skipped unconditionally.

    Measured over 60,000 trials before the fix (build a real team, hide one
    random slot, complete the other five): the hidden mon was a Zoroark forme
    144 times and complete_team returned a Zoroark fill ZERO times. Real PS
    runs them at 0.200% of slots / ~1.2% of teams.

    This makes Zoroark SAMPLABLE. It does not model Illusion -- the bot still
    attributes a disguised Zoroark's moves to the mon it is impersonating.
    """

    def test_a_single_fill_slot_can_be_zoroark(self):
        from fp.search import ps_teams

        ps_teams.seed(31337)
        seen = 0
        for _ in range(4000):
            fills = ps_teams.complete_team([
                {"speciesId": s, "ability": "", "moves": [], "level": 80}
                for s in ("pikachu", "gastrodon", "corviknight",
                          "heatran", "dragapult")
            ])
            self.assertEqual(1, len(fills))
            if normalize_name(fills[0]["speciesId"]).startswith("zoroark"):
                seen += 1
        self.assertGreater(seen, 0, "Zoroark is still unreachable in the last slot")

    def test_zoroark_is_still_never_the_last_mon_generated(self):
        # the BUILD-ORDER rule must survive: PS never generates it 6th
        from fp.search import ps_teams

        ps_teams.seed(99)
        for _ in range(3000):
            ids = [normalize_name(m["speciesId"]) for m in ps_teams.random_team()]
            self.assertFalse(
                ids[-1].startswith("zoroark"),
                "Zoroark generated into the last slot from scratch",
            )


class TestFallbackRespectsHardEvidence(unittest.TestCase):
    """REGRESSION (finding #5).

    `populate_pkmn_from_fallback_set` drew from
    `get_pkmn_sets_from_pkmn_name`, the RAW species list, which never runs
    `full_set_pkmn_can_have_set` -- so impossible_items, can_have_choice_item
    and impossible_abilities were all skipped and the fallback could write an
    item or ability we had positively DISPROVED. That evidence is direct
    observation: battle_modifier.py adds every ability in
    ABILITIES_REVEALED_ON_SWITCH_IN to impossible_abilities when a switch-in
    announced none of them, and can_have_choice_item is cleared once the mon
    used two different moves.

    The fallback must still never leave a live threat un-populated, so it falls
    through to the unfiltered draw when nothing survives.
    """

    def setUp(self):
        RandomBattleTeamDatasets.initialize("gen9randombattleblitz", set())
        self.addCleanup(RandomBattleTeamDatasets.__init__)
        battle = Battle("t")
        battle.battle_type = BattleType.RANDOM_BATTLE
        self.datasets = random_battles._datasets_for(battle)

    def _draw(self, setup, n=200):
        items, abilities = set(), set()
        for _ in range(n):
            pkmn = Pokemon("bombirdier", 80)
            pkmn.item = constants.UNKNOWN_ITEM
            pkmn.moves = []
            setup(pkmn)
            random_battles.populate_pkmn_from_fallback_set(pkmn, self.datasets)
            items.add(pkmn.item)
            abilities.add(pkmn.ability)
        return items, abilities

    def test_baseline_spans_the_species_options(self):
        items, abilities = self._draw(lambda p: None)
        self.assertTrue(items & set(constants.CHOICE_ITEMS), items)
        self.assertIn("bigpecks", abilities)

    def test_disproved_item_and_ability_are_never_written(self):
        def evidence(pkmn):
            pkmn.can_have_choice_item = False
            pkmn.impossible_abilities.add("bigpecks")

        items, abilities = self._draw(evidence)
        self.assertFalse(items & set(constants.CHOICE_ITEMS), items)
        self.assertNotIn("bigpecks", abilities)

    def test_still_populates_when_nothing_survives(self):
        # gliscor has a single set; disproving its ability must NOT leave the
        # mon blank -- an un-populated live threat is the worse failure
        pkmn = Pokemon("gliscor", 82)
        pkmn.item = constants.UNKNOWN_ITEM
        pkmn.moves = []
        pkmn.impossible_abilities.add("poisonheal")
        self.assertTrue(
            random_battles.populate_pkmn_from_fallback_set(pkmn, self.datasets)
        )
        self.assertNotEqual(constants.UNKNOWN_ITEM, pkmn.item)
        self.assertIsNotNone(pkmn.ability)


class TestMegaLookupDoesNotLeakSiblingFormeSets(unittest.TestCase):
    """REGRESSION (finding #9).

    `get_pkmn_sets_from_pkmn_name` unions a second lookup keyed on the mega
    forme, and `get_key_in_dict_from_pkmn_name` falls back through baseSpecies.
    fp/battle.py builds that name as f"{name}mega", and 'meowsticfmega' IS in
    pokedex.json with baseSpecies 'Meowstic' -- so Meowstic-F was handed the
    BASE Meowstic's sets. They are different mons: Meowstic runs Prankster with
    screens, Meowstic-F runs Competitive with Nasty Plot, and Prankster is not
    among Meowstic-F's abilities at all. Live proof in ladder game 2662062714:
    405 sampled world states gave Meowstic-F a moveset no Meowstic-F set has.

    gen9 randbats has no megas, so the lookup must be inert for every species.
    """

    def setUp(self):
        RandomBattleTeamDatasets.initialize("gen9randombattleblitz", set())
        self.addCleanup(RandomBattleTeamDatasets.__init__)

    def test_meowstic_f_never_gets_prankster(self):
        sets = RandomBattleTeamDatasets.get_pkmn_sets_from_pkmn_name(
            Pokemon("meowsticf", 89)
        )
        self.assertTrue(sets)
        self.assertEqual(
            {"competitive"}, {s.pkmn_set.ability for s in sets},
            "Meowstic-F was given the base Meowstic's Prankster sets",
        )

    def test_base_meowstic_keeps_its_own(self):
        sets = RandomBattleTeamDatasets.get_pkmn_sets_from_pkmn_name(
            Pokemon("meowstic", 89)
        )
        self.assertEqual({"prankster"}, {s.pkmn_set.ability for s in sets})

    def test_no_species_gains_or_loses_sets_to_the_mega_lookup(self):
        # also pins the Rayquaza case: its mega info is ('rayquaza', 'none'),
        # so an unguarded lookup returned its own list twice and doubled every
        # Rayquaza set's weight in the count-weighted draw
        for name, own in RandomBattleTeamDatasets.pkmn_sets.items():
            got = RandomBattleTeamDatasets.get_pkmn_sets_from_pkmn_name(
                Pokemon(name, 80)
            )
            self.assertEqual(len(own), len(got), name)
