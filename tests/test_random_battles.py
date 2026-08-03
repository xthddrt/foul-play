import os
import unittest
from unittest import mock

import constants
from constants import BattleType
from data.pkmn_sets import RandomBattleTeamDatasets
from fp.battle import Battle, Pokemon
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

        with (
            mock.patch(
                "fp.search.random_battles.random.shuffle",
                side_effect=lambda _population: None,
            ),
            mock.patch(
                "fp.search.random_battles.random.choices",
                side_effect=lambda population, weights=None: [population[0]],
            ),
        ):
            sampled = prepare_random_battles(battle, 1)

        self.assertEqual("eviolite", sampled[0][0].opponent.active.item)


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
