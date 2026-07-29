import unittest

from data.pkmn_sets import (
    TeamDatasets,
    SmogonSets,
    PredictedPokemonSet,
    PokemonSet,
    PokemonMoveset,
    RandomBattleTeamDatasets,
    random_battle_ev_iv_spread,
)
from fp.battle import Pokemon, Move
from fp.search.helpers import populate_pkmn_from_set


class TestTeamDatasets(unittest.TestCase):
    def setUp(self):
        TeamDatasets.__init__()

    def test_team_datasets_initialize_gen5(self):
        TeamDatasets.initialize(
            "gen5ou",
            {"azelf", "heatran", "rotomwash", "scizor", "tyranitar", "volcarona"},
        )
        self.assertEqual("gen5ou", TeamDatasets.pkmn_mode)
        self.assertEqual(6, len(TeamDatasets.pkmn_sets))

    def test_team_datasets_add_new_pokemon(self):
        TeamDatasets.initialize("gen5ou", {"dragonite"})
        self.assertNotIn("azelf", TeamDatasets.pkmn_sets)
        TeamDatasets.add_new_pokemon("azelf")
        self.assertIn("azelf", TeamDatasets.pkmn_sets)

    def test_pokemon_not_in_team_datasets_does_not_error(self):
        TeamDatasets.initialize("gen5ou", {"dragonite"})
        self.assertNotIn("azelf", TeamDatasets.pkmn_sets)
        TeamDatasets.add_new_pokemon("not_in_team_datasets")
        self.assertNotIn("not_in_team_datasets", TeamDatasets.pkmn_sets)

    def test_smogon_datasets_add_new_pokemon_with_cosmetic_forme(self):
        TeamDatasets.initialize("gen5ou", {"dragonite"})
        self.assertNotIn("gastrodon", TeamDatasets.pkmn_sets)
        self.assertNotIn("gastrodoneast", TeamDatasets.pkmn_sets)
        TeamDatasets.add_new_pokemon("gastrodoneast")
        self.assertIn("gastrodoneast", TeamDatasets.pkmn_sets)
        self.assertNotIn("gastrodon", TeamDatasets.pkmn_sets)

    def test_removing_initial_set_does_not_change_existing_pokemon_sets(self):
        TeamDatasets.initialize("gen5ou", {"dragonite"})
        initial_len = len(TeamDatasets.pkmn_sets["dragonite"])
        TeamDatasets.pkmn_sets["dragonite"].pop(-1)
        len_after_pop = len(TeamDatasets.pkmn_sets["dragonite"])
        self.assertNotEqual(initial_len, len_after_pop)
        TeamDatasets.add_new_pokemon("azelf")
        self.assertEqual(len_after_pop, len(TeamDatasets.pkmn_sets["dragonite"]))


class TestSmogonDatasets(unittest.TestCase):
    def setUp(self):
        SmogonSets.__init__()

    def test_smogon_datasets_initialize_gen5(self):
        SmogonSets.initialize(
            "gen5ou",
            {"azelf", "heatran", "scizor", "tyranitar", "volcarona"},
        )
        self.assertEqual("gen5ou", SmogonSets.pkmn_mode)
        self.assertEqual(5, len(SmogonSets.pkmn_sets))

    def test_smogon_datasets_initialize_gen4(self):
        SmogonSets.initialize(
            "gen4ou",
            {"azelf", "heatran", "scizor", "tyranitar", "dragonite"},
        )
        self.assertEqual("gen4ou", SmogonSets.pkmn_mode)
        self.assertEqual(5, len(SmogonSets.pkmn_sets))

    def test_smogon_datasets_add_new_pokemon(self):
        SmogonSets.initialize("gen4ou", {"dragonite"})
        self.assertNotIn("azelf", SmogonSets.pkmn_sets)
        SmogonSets.add_new_pokemon("azelf")
        self.assertIn("azelf", SmogonSets.pkmn_sets)

    def test_smogon_datasets_add_new_pokemon_with_cosmetic_forme(self):
        SmogonSets.initialize("gen4ou", {"dragonite"})
        self.assertNotIn("gastrodon", SmogonSets.pkmn_sets)
        self.assertNotIn("gastrodoneast", SmogonSets.pkmn_sets)
        SmogonSets.add_new_pokemon("gastrodoneast")
        self.assertNotIn("gastrodoneast", SmogonSets.pkmn_sets)
        self.assertIn("gastrodon", SmogonSets.pkmn_sets)

    def test_removing_initial_set_does_not_change_existing_pokemon_sets(self):
        SmogonSets.initialize("gen4ou", {"dragonite"})
        initial_len = len(SmogonSets.pkmn_sets["dragonite"])
        SmogonSets.pkmn_sets["dragonite"].pop(-1)
        len_after_pop = len(SmogonSets.pkmn_sets["dragonite"])
        self.assertNotEqual(initial_len, len_after_pop)
        SmogonSets.add_new_pokemon("azelf")
        self.assertEqual(len_after_pop, len(SmogonSets.pkmn_sets["dragonite"]))


class TestPredictSet(unittest.TestCase):
    def setUp(self):
        TeamDatasets.__init__()

    def test_omits_impossible_ability_when_predicting_set(self):
        TeamDatasets.initialize(
            "gen9battlefactory", {"krookodile"}, battle_factory_tier_name="ru"
        )

        pkmn = Pokemon("krookodile", 100)
        pkmn.ability = None

        all_sets = TeamDatasets.get_all_remaining_sets(pkmn)
        any_set_has_intimidate = any(
            set_.pkmn_set.ability == "intimidate" for set_ in all_sets
        )
        self.assertTrue(
            any_set_has_intimidate
        )  # Intimidate is possible before adding it to impossible_abilities

        pkmn.impossible_abilities.add("intimidate")

        all_sets = TeamDatasets.get_all_remaining_sets(pkmn)
        any_set_has_intimidate = any(
            set_.pkmn_set.ability == "intimidate" for set_ in all_sets
        )
        self.assertFalse(any_set_has_intimidate)

    def test_allows_impossible_ability_when_predicting_set_if_ability_is_explicitly_set(
        self,
    ):
        TeamDatasets.initialize(
            "gen9battlefactory", {"krookodile"}, battle_factory_tier_name="ru"
        )

        pkmn = Pokemon("krookodile", 100)
        pkmn.ability = None

        all_sets = TeamDatasets.get_all_remaining_sets(pkmn)
        any_set_has_intimidate = any(
            set_.pkmn_set.ability == "intimidate" for set_ in all_sets
        )
        self.assertTrue(
            any_set_has_intimidate
        )  # Intimidate is possible before adding it to impossible_abilities

        # this doesn't matter because the pkmn's ability is intimidate
        pkmn.impossible_abilities.add("intimidate")
        pkmn.ability = "intimidate"

        all_sets = TeamDatasets.get_all_remaining_sets(pkmn)
        any_set_has_intimidate = any(
            set_.pkmn_set.ability == "intimidate" for set_ in all_sets
        )
        self.assertTrue(
            any_set_has_intimidate
        )  # this is True because intimidate is the ability

    def test_uses_removed_item_when_predicting_set(self):
        TeamDatasets.initialize(
            "gen9battlefactory", {"gholdengo"}, battle_factory_tier_name="ou"
        )

        pkmn = Pokemon("gholdengo", 100)

        all_sets = TeamDatasets.get_all_remaining_sets(pkmn)
        all_sets_have_airballoon = all(
            set_.pkmn_set.item == "airballoon" for set_ in all_sets
        )
        self.assertFalse(all_sets_have_airballoon)

        pkmn.item = None
        pkmn.removed_item = "airballoon"

        sets_after_removed_item = TeamDatasets.get_all_remaining_sets(pkmn)

        all_sets_have_airballoon = all(
            set_.pkmn_set.item == "airballoon" for set_ in sets_after_removed_item
        )
        self.assertTrue(all_sets_have_airballoon)

    def test_predicts_set_when_there_is_no_removed_item(
        self,
    ):
        TeamDatasets.initialize(
            "gen9battlefactory", {"gholdengo"}, battle_factory_tier_name="ou"
        )

        pkmn = Pokemon("gholdengo", 100)
        pkmn.item = None

        sets_after_removed_item = TeamDatasets.get_all_remaining_sets(pkmn)
        self.assertNotEqual(0, len(sets_after_removed_item))

    def test_removed_item_is_used_when_another_item_was_tricked(
        self,
    ):
        TeamDatasets.initialize("gen5ou", {"starmie"})
        TeamDatasets.raw_pkmn_sets = {
            "starmie": {
                "|analytic|choicespecs|timid|0,0,0,252,4,252|trick|rapidspin|thunder|surf",
            }
        }
        TeamDatasets.pkmn_sets = {
            "starmie": [
                PredictedPokemonSet(
                    pkmn_set=PokemonSet(
                        nature="timid",
                        item="choicespecs",
                        ability="analytic",
                        evs=[0, 0, 0, 252, 4, 252],
                        count=1,
                    ),
                    pkmn_moveset=PokemonMoveset(
                        moves=["trick", "rapidspin", "thunder", "surf"],
                    ),
                )
            ]
        }

        pkmn = Pokemon("starmie", 100)
        pkmn.moves = [
            Move("trick"),
            Move("rapidspin"),
            Move("thunder"),
        ]
        pkmn.item = "leftovers"
        pkmn.removed_item = "choicespecs"

        sets_after_removed_item = TeamDatasets.get_all_remaining_sets(pkmn)
        self.assertNotEqual(0, len(sets_after_removed_item))


# Real gen9randombattle set keys (level,item,ability,<moves>,teraType)
RABSCA_TRICK_ROOM_SET = (
    "91,heavydutyboots,synchronize,bugbuzz,psychic,revivalblessing,trickroom,steel"
)
DITTO_SET = "87,choicescarf,imposter,transform,ghost"
# Abomasnow is 2x weak to Stealth Rock, holds Light Clay (not Boots/Leftovers/
# Life Orb) and has a physical move, so only its HP EVs are shaved.
ABOMASNOW_SET = (
    "84,lightclay,snowwarning,auroraveil,blizzard,earthquake,woodhammer,water"
)


class TestRandomBattleEvIvSpread(unittest.TestCase):
    def setUp(self):
        RandomBattleTeamDatasets.__init__()
        RandomBattleTeamDatasets.pkmn_mode = "gen9randombattle"
        RandomBattleTeamDatasets.raw_pkmn_sets = {
            "rabsca": {RABSCA_TRICK_ROOM_SET: 444},
            "ditto": {DITTO_SET: 741},
            "abomasnow": {ABOMASNOW_SET: 100},
        }
        RandomBattleTeamDatasets._initialize_pkmn_sets()
        self.addCleanup(RandomBattleTeamDatasets.__init__)

    def _only_set(self, name):
        sets = RandomBattleTeamDatasets.pkmn_sets[name]
        self.assertEqual(1, len(sets))
        return sets[0]

    def test_special_attacker_gets_zero_attack_ev_and_iv(self):
        # rabsca's Trick Room set has no physical attacking move
        pkmn_set = self._only_set("rabsca").pkmn_set
        self.assertEqual(0, pkmn_set.evs[1])
        self.assertEqual(0, pkmn_set.ivs[1])

    def test_trick_room_set_gets_zero_speed_ev_and_iv(self):
        pkmn_set = self._only_set("rabsca").pkmn_set
        self.assertEqual(0, pkmn_set.evs[5])
        self.assertEqual(0, pkmn_set.ivs[5])

    def test_zeroed_attack_and_speed_lower_the_computed_stats(self):
        # threading the zeroed EV/IVs through determinization yields the
        # Showdown-accurate Attack/Speed for the L91 Trick Room rabsca set
        pkmn = Pokemon("rabsca", 91)
        populate_pkmn_from_set(pkmn, self._only_set("rabsca"))
        self.assertEqual(96, pkmn.stats["attack"])
        self.assertEqual(86, pkmn.stats["speed"])

    def test_stealth_rock_weak_mon_has_hp_evs_shaved(self):
        pkmn_set = self._only_set("abomasnow").pkmn_set
        # HP EVs shaved below the flat 85 to the survival breakpoint
        self.assertEqual(77, pkmn_set.evs[0])
        # attack is untouched (physical moves present) and speed too
        self.assertEqual(85, pkmn_set.evs[1])
        self.assertEqual(85, pkmn_set.evs[5])

        pkmn = Pokemon("abomasnow", 84)
        populate_pkmn_from_set(pkmn, self._only_set("abomasnow"))
        self.assertEqual(287, pkmn.max_hp)
        # breakpoint invariant: HP stat is not divisible by 4 for a 2x-weak mon
        self.assertNotEqual(0, pkmn.max_hp % 4)

    def test_boots_special_attacker_keeps_full_hp_evs(self):
        # rabsca holds Heavy-Duty Boots, so it is SR-immune and HP is not shaved
        pkmn_set = self._only_set("rabsca").pkmn_set
        self.assertEqual(85, pkmn_set.evs[0])

    def test_spread_function_returns_flat_default_for_unknown_species(self):
        evs, ivs = random_battle_ev_iv_spread(
            "not_a_real_pokemon", ["tackle"], "noability", "noitem", 100
        )
        self.assertEqual((85,) * 6, evs)
        self.assertEqual((31,) * 6, ivs)


class TestRandomBattleEvIvSpreadBreakpoints(unittest.TestCase):
    """Branch-by-branch pins of the teams.ts EV/IV post-processing
    (pokemon-showdown data/random-battles/gen9/teams.ts:1536-1589).
    Expected EV values were derived with an independent implementation of
    the teams.ts HP formula and cross-checked against the production code."""

    @staticmethod
    def _spread(species, moves, ability, item, level):
        return random_battle_ev_iv_spread(species, moves, ability, item, level)

    # ---- HP shave breakpoints (teams.ts:1541-1564) ----

    def test_substitute_sitrus_shaves_to_hp_divisible_by_4(self):
        # teams.ts:1543-1545 - two Substitutes must activate Sitrus Berry.
        # L88 azumarill: 85evs->319hp ... shaved to 69evs->316hp (316 % 4 == 0;
        # the intermediate 318 is even but 318 % 4 == 2, so this branch shaves
        # PAST the belly-drum breakpoint below)
        evs, _ = self._spread(
            "azumarill",
            ["substitute", "aquajet", "playrough"],
            "hugepower",
            "sitrusberry",
            88,
        )
        self.assertEqual(69, evs[0])

    def test_minior_shaves_to_hp_divisible_by_4_regardless_of_item(self):
        # teams.ts:1543-1545 - two SR switch-ins must activate Shields Down
        evs, _ = self._spread(
            "minior",
            ["powergem", "shellsmash", "acrobatics"],
            "shieldsdown",
            "whiteherb",
            80,
        )
        self.assertEqual(69, evs[0])

    def test_bellydrum_sitrus_shaves_to_even_hp(self):
        # teams.ts:1546-1551 - Belly Drum (half HP) must activate Sitrus.
        # L88 azumarill: 85evs->319hp (odd), 81evs->318hp (even) - stops at
        # the first EVEN hp, unlike the substitute branch above
        evs, _ = self._spread(
            "azumarill",
            ["bellydrum", "aquajet", "playrough"],
            "hugepower",
            "sitrusberry",
            88,
        )
        self.assertEqual(81, evs[0])

    def test_bellydrum_gluttony_shaves_to_even_hp_without_sitrus(self):
        # teams.ts:1548 - ability Gluttony qualifies without the berry.
        # L82 snorlax: 85evs->397hp (odd) -> 81evs->396hp
        evs, _ = self._spread(
            "snorlax", ["bellydrum", "bodyslam", "rest"], "gluttony", "chestoberry", 82
        )
        self.assertEqual(81, evs[0])

    def test_substitute_endeavor_shaves_to_hp_not_divisible_by_4(self):
        # teams.ts:1552-1554 - Luvdisc subs down to minimal HP for Endeavor.
        # L100 luvdisc: 85evs->248hp (248 % 4 == 0) -> 81evs->247hp
        evs, _ = self._spread(
            "luvdisc", ["substitute", "endeavor", "surf"], "swiftswim", "focussash", 100
        )
        self.assertEqual(81, evs[0])

    def test_crash_damage_move_forces_odd_hp(self):
        # teams.ts:1540 - High Jump Kick users want odd HP to survive two
        # crash-damage misses (srWeakness forced to 2 -> break on hp % 2 > 0).
        # L86 hitmonlee: 85evs->226hp (even) -> 81evs->225hp
        evs, _ = self._spread(
            "hitmonlee",
            ["highjumpkick", "knockoff", "stoneedge"],
            "unburden",
            "whiteherb",
            86,
        )
        self.assertEqual(81, evs[0])

    def test_leftovers_holder_is_never_shaved(self):
        # teams.ts:1558 - Leftovers/Life Orb/Regenerator break immediately
        # even though SR-neutral garchomp would otherwise qualify
        evs, _ = self._spread(
            "garchomp",
            ["earthquake", "outrage", "swordsdance"],
            "roughskin",
            "leftovers",
            76,
        )
        self.assertEqual(85, evs[0])

    def test_sr_weak_sitrus_holder_shaves_to_activation_breakpoint(self):
        # teams.ts:1560-1561 - the Sitrus condition is INVERTED: shave until
        # hp % (4/srWeakness) == 0 so two 4x-SR switch-ins activate the berry.
        # L85 charizard: 85evs->271hp (odd) -> 81evs->270hp (270 % 2 == 0)
        evs, _ = self._spread(
            "charizard",
            ["flamethrower", "airslash", "roost"],
            "blaze",
            "sitrusberry",
            85,
        )
        self.assertEqual(81, evs[0])

    def test_sr_resisting_mon_is_never_shaved(self):
        # teams.ts:1558 - srWeakness <= 0 (lucario resists rock 4x) breaks
        evs, _ = self._spread(
            "lucario",
            ["closecombat", "meteormash", "swordsdance"],
            "justified",
            "focussash",
            80,
        )
        self.assertEqual(85, evs[0])

    # ---- 0-Atk gating (teams.ts:1567-1584) ----

    def test_bodypress_only_physical_set_zeroes_attack(self):
        # teams.ts:1576 - Body Press reads Defense, so it does not count as
        # an Attack-stat move
        evs, ivs = self._spread(
            "corviknight",
            ["bodypress", "roost", "defog", "irondefense"],
            "pressure",
            "heavydutyboots",
            79,
        )
        self.assertEqual((0, 0), (evs[1], ivs[1]))

    def test_foulplay_only_physical_set_zeroes_attack(self):
        # teams.ts:1576 - Foul Play uses the TARGET's Attack
        evs, ivs = self._spread(
            "umbreon", ["foulplay", "protect", "wish", "toxic"], "synchronize",
            "leftovers", 84,
        )
        self.assertEqual((0, 0), (evs[1], ivs[1]))

    def test_seismictoss_fixed_damage_set_zeroes_attack(self):
        # teams.ts:1569 - damageCallback moves don't read Attack
        evs, ivs = self._spread(
            "blissey",
            ["seismictoss", "softboiled", "calmmind", "flamethrower"],
            "naturalcure",
            "heavydutyboots",
            86,
        )
        self.assertEqual((0, 0), (evs[1], ivs[1]))

    def test_shellsidearm_keeps_attack(self):
        # teams.ts:1570 - Shell Side Arm can go physical
        evs, ivs = self._spread(
            "slowbrogalar",
            ["shellsidearm", "slackoff", "calmmind", "psychic"],
            "regenerator",
            "heavydutyboots",
            85,
        )
        self.assertEqual((85, 31), (evs[1], ivs[1]))

    def test_physical_terablast_keeps_attack(self):
        # teams.ts:1571-1575 - Tera Blast goes physical when base atk > spa
        # (dragonite 134 > 100)
        evs, ivs = self._spread(
            "dragonite",
            ["terablast", "roost", "dragondance", "earthquake"],
            "multiscale",
            "heavydutyboots",
            76,
        )
        self.assertEqual((85, 31), (evs[1], ivs[1]))

    def test_special_terablast_zeroes_attack(self):
        # espeon (65 atk < 130 spa, no shift gear/contrary/defiant): special
        evs, ivs = self._spread(
            "espeon",
            ["terablast", "calmmind", "morningsun", "psychic"],
            "magicbounce",
            "heavydutyboots",
            82,
        )
        self.assertEqual((0, 0), (evs[1], ivs[1]))

    def test_transform_keeps_attack(self):
        # teams.ts:1578 - Ditto keeps Atk investment for the copied moves
        evs, ivs = self._spread(
            "ditto", ["transform"], "imposter", "choicescarf", 88
        )
        self.assertEqual((85, 31), (evs[1], ivs[1]))

    # ---- 0-Spe gating (teams.ts:1586-1589) ----

    def test_gyroball_zeroes_speed(self):
        evs, ivs = self._spread(
            "ferrothorn",
            ["gyroball", "powerwhip", "spikes", "leechseed"],
            "ironbarbs",
            "leftovers",
            82,
        )
        self.assertEqual((0, 0), (evs[5], ivs[5]))
        # attack investment stays: powerwhip is a physical attack
        self.assertEqual((85, 31), (evs[1], ivs[1]))


class TestRandomBattleSetParsing(unittest.TestCase):
    def setUp(self):
        RandomBattleTeamDatasets.__init__()
        RandomBattleTeamDatasets.pkmn_mode = "gen9randombattle"
        self.addCleanup(RandomBattleTeamDatasets.__init__)

    def _parse(self, name, set_key):
        RandomBattleTeamDatasets.raw_pkmn_sets = {name: {set_key: 1}}
        RandomBattleTeamDatasets._initialize_pkmn_sets()
        return RandomBattleTeamDatasets.pkmn_sets[name][0]

    def test_ditto_sub_four_move_set_parses_moves_and_tera(self):
        # '87,choicescarf,imposter,transform,ghost' has a single move + tera type;
        # the trailing token must be parsed as tera, not as a bogus move
        predicted = self._parse("ditto", DITTO_SET)
        self.assertEqual(("transform",), predicted.pkmn_moveset.moves)
        self.assertEqual("ghost", predicted.pkmn_set.tera_type)

    def test_ditto_steel_tera_variant(self):
        predicted = self._parse("ditto", "87,choicescarf,imposter,transform,steel")
        self.assertEqual(("transform",), predicted.pkmn_moveset.moves)
        self.assertEqual("steel", predicted.pkmn_set.tera_type)

    def test_standard_four_move_set_still_parses(self):
        predicted = self._parse("rabsca", RABSCA_TRICK_ROOM_SET)
        self.assertEqual(
            ("bugbuzz", "psychic", "revivalblessing", "trickroom"),
            predicted.pkmn_moveset.moves,
        )
        self.assertEqual("steel", predicted.pkmn_set.tera_type)
