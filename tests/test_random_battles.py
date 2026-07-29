import unittest
from unittest import mock

import constants
from constants import BattleType
from data.pkmn_sets import RandomBattleTeamDatasets
from fp.battle import Battle, Pokemon
from fp.search.random_battles import (
    prepare_random_battles,
    sample_randombattle_pokemon,
)

PIKACHU_SET_KEY = "85,lightball,static,irontail,surf,thunderbolt,voltswitch,electric"
PIKACHU_OTHER_SET_KEY = (
    "85,lightball,lightningrod,irontail,surf,thunderbolt,voltswitch,water"
)
EEVEE_SET_KEY = "88,eviolite,adaptability,doubleedge,protect,quickattack,wish,normal"


class TestSampleRandombattlePokemon(unittest.TestCase):
    def setUp(self):
        RandomBattleTeamDatasets.initialize("gen9randombattle")

    def test_sampled_fill_in_pokemon_is_not_revealed(self):
        pkmn = sample_randombattle_pokemon([])

        self.assertFalse(pkmn.revealed)


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
