import unittest
import json
from collections import defaultdict
from unittest import mock

import constants
from constants import BattleType
from data.pkmn_sets import (
    TeamDatasets,
    RandomBattleTeamDatasets,
    PredictedPokemonSet,
    PokemonSet,
    PokemonMoveset,
)
from fp.helpers import calculate_stats

from fp.battle import Battle
from fp.battle import Pokemon
from fp.battle import Move
from fp.battle import LastUsedMove
from fp.battle import DamageDealt
from fp.battle import boost_multiplier_lookup

from fp.battle_modifier import (
    request,
    fieldstart,
    fieldend,
    illusion_end,
    _switch_active_with_zoroark_from_reserves,
    drag,
    switch,
    clearboost,
    remove_item,
    set_item,
    ITEMS_REVEALED_ON_SWITCH_IN,
    sidestart,
    sideend,
    check_opponent_custapberry,
    check_opponent_reactive_items,
)
from fp.battle_modifier import fail
from fp.battle_modifier import terastallize
from fp.battle_modifier import activate
from fp.battle_modifier import anim
from fp.battle_modifier import prepare
from fp.battle_modifier import switch_or_drag
from fp.battle_modifier import clearallboost
from fp.battle_modifier import heal_or_damage
from fp.battle_modifier import swapsideconditions
from fp.battle_modifier import move
from fp.battle_modifier import cant
from fp.battle_modifier import boost
from fp.battle_modifier import setboost
from fp.battle_modifier import unboost
from fp.battle_modifier import status
from fp.battle_modifier import weather
from fp.battle_modifier import curestatus
from fp.battle_modifier import start_volatile_status
from fp.battle_modifier import end_volatile_status
from fp.battle_modifier import immune
from fp.battle_modifier import miss
from fp.battle_modifier import update_ability
from fp.battle_modifier import form_change
from fp.battle_modifier import zpower
from fp.battle_modifier import clearnegativeboost
from fp.battle_modifier import check_speed_ranges
from fp.battle_modifier import check_choicescarf
from fp.battle_modifier import check_heavydutyboots
from fp.battle_modifier import get_damage_dealt
from fp.battle_modifier import singleturn
from fp.battle_modifier import transform
from fp.battle_modifier import process_battle_updates
from fp.battle_modifier import upkeep
from fp.battle_modifier import turn
from fp.battle_modifier import inactive


class TestRequestMessage(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.active = Pokemon("pikachu", 100)
        self.request_json = {
            "active": [
                {
                    "moves": [
                        {
                            "move": "Storm Throw",
                            "id": "stormthrow",
                            "pp": 16,
                            "maxpp": 16,
                            "target": "normal",
                            "disabled": False,
                        },
                        {
                            "move": "Ice Punch",
                            "id": "icepunch",
                            "pp": 24,
                            "maxpp": 24,
                            "target": "normal",
                            "disabled": False,
                        },
                        {
                            "move": "Bulk Up",
                            "id": "bulkup",
                            "pp": 32,
                            "maxpp": 32,
                            "target": "self",
                            "disabled": False,
                        },
                        {
                            "move": "Knock Off",
                            "id": "knockoff",
                            "pp": 32,
                            "maxpp": 32,
                            "target": "normal",
                            "disabled": False,
                        },
                    ]
                }
            ],
            "side": {
                "name": "NiceNameNerd",
                "id": "p1",
                "pokemon": [
                    {
                        "ident": "p1: Throh",
                        "details": "Throh, L83, M",
                        "condition": "335/335",
                        "active": True,
                        "stats": {
                            "atk": 214,
                            "def": 189,
                            "spa": 97,
                            "spd": 189,
                            "spe": 122,
                        },
                        "moves": ["stormthrow", "icepunch", "bulkup", "knockoff"],
                        "baseAbility": "moldbreaker",
                        "item": "leftovers",
                        "pokeball": "pokeball",
                        "ability": "moldbreaker",
                    },
                    {
                        "ident": "p1: Empoleon",
                        "details": "Empoleon, L77, F",
                        "condition": "256/256",
                        "active": False,
                        "stats": {
                            "atk": 137,
                            "def": 180,
                            "spa": 215,
                            "spd": 200,
                            "spe": 137,
                        },
                        "moves": ["icebeam", "grassknot", "scald", "flashcannon"],
                        "baseAbility": "torrent",
                        "item": "choicespecs",
                        "pokeball": "pokeball",
                        "ability": "torrent",
                    },
                    {
                        "ident": "p1: Emboar",
                        "details": "Emboar, L79, M",
                        "condition": "303/303",
                        "active": False,
                        "stats": {
                            "atk": 240,
                            "def": 148,
                            "spa": 204,
                            "spd": 148,
                            "spe": 148,
                        },
                        "moves": ["headsmash", "superpower", "flareblitz", "grassknot"],
                        "baseAbility": "reckless",
                        "item": "assaultvest",
                        "pokeball": "pokeball",
                        "ability": "reckless",
                    },
                    {
                        "ident": "p1: Zoroark",
                        "details": "Zoroark, L77, M",
                        "condition": "219/219",
                        "active": False,
                        "stats": {
                            "atk": 166,
                            "def": 137,
                            "spa": 229,
                            "spd": 137,
                            "spe": 206,
                        },
                        "moves": [
                            "sludgebomb",
                            "darkpulse",
                            "flamethrower",
                            "focusblast",
                        ],
                        "baseAbility": "illusion",
                        "item": "choicespecs",
                        "pokeball": "pokeball",
                        "ability": "illusion",
                    },
                    {
                        "ident": "p1: Reuniclus",
                        "details": "Reuniclus, L78, M",
                        "condition": "300/300",
                        "active": False,
                        "stats": {
                            "atk": 106,
                            "def": 162,
                            "spa": 240,
                            "spd": 178,
                            "spe": 92,
                        },
                        "moves": ["calmmind", "shadowball", "psyshock", "recover"],
                        "baseAbility": "magicguard",
                        "item": "lifeorb",
                        "pokeball": "pokeball",
                        "ability": "magicguard",
                    },
                    {
                        "ident": "p1: Moltres",
                        "details": "Moltres, L77",
                        "condition": "265/265",
                        "active": False,
                        "stats": {
                            "atk": 159,
                            "def": 183,
                            "spa": 237,
                            "spd": 175,
                            "spe": 183,
                        },
                        "moves": ["fireblast", "toxic", "hurricane", "roost"],
                        "baseAbility": "flamebody",
                        "item": "leftovers",
                        "pokeball": "pokeball",
                        "ability": "flamebody",
                    },
                ],
            },
            "rqid": 2,
        }

    def test_request_sets_force_switch_to_false(self):
        split_request_message = ["", "request", json.dumps(self.request_json)]
        request(self.battle, split_request_message)
        self.assertEqual(False, self.battle.force_switch)

    def test_force_switch_properly_sets_the_force_switch_flag(self):
        self.request_json.pop("active")
        self.request_json[constants.FORCE_SWITCH] = [True]
        split_request_message = ["", "request", json.dumps(self.request_json)]
        request(self.battle, split_request_message)
        self.assertEqual(True, self.battle.force_switch)

    def test_wait_properly_sets_wait_flag(self):
        self.request_json.pop("active")
        self.request_json[constants.WAIT] = [True]
        split_request_message = ["", "request", json.dumps(self.request_json)]
        request(self.battle, split_request_message)
        self.assertEqual(True, self.battle.wait)

    def test_wait_does_not_initialize_pokemon(self):
        self.request_json.pop("active")
        self.request_json[constants.WAIT] = [True]
        split_request_message = ["", "request", json.dumps(self.request_json)]
        request(self.battle, split_request_message)
        self.assertEqual(0, len(self.battle.user.reserve))


class TestSwitchOrDrag(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.user.active = Pokemon("pikachu", 100)

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active
        self.battle.opponent.reserve = []

    def test_50_100_g_message(self):
        split_msg = ["", "switch", "p1a: pikachu", "Pikachu, L100, M", "50/100g"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual("pikachu", self.battle.user.active.name)
        self.assertEqual(
            0.5, self.battle.user.active.hp / self.battle.user.active.max_hp
        )

    def test_user_pokemon_switching_in_is_marked_revealed(self):
        weedle = Pokemon("weedle", 100)
        self.battle.user.reserve = [weedle]
        self.assertFalse(weedle.revealed)

        split_msg = ["", "switch", "p1a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertIs(weedle, self.battle.user.active)
        self.assertTrue(weedle.revealed)

    def test_user_reserve_pokemon_that_did_not_enter_the_field_stays_unrevealed(self):
        weedle = Pokemon("weedle", 100)
        metapod = Pokemon("metapod", 100)
        self.battle.user.reserve = [weedle, metapod]

        split_msg = ["", "switch", "p1a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertTrue(weedle.revealed)
        self.assertFalse(metapod.revealed)

    def test_user_pokemon_dragged_in_is_marked_revealed(self):
        weedle = Pokemon("weedle", 100)
        self.battle.user.reserve = [weedle]

        split_msg = ["", "drag", "p1a: weedle", "Weedle, L100, M", "100/100"]
        drag(self.battle, split_msg)

        self.assertIs(weedle, self.battle.user.active)
        self.assertTrue(weedle.revealed)

    def test_opponent_pokemon_switching_in_is_marked_revealed(self):
        # a brand-new opponent pkmn is created when it first enters the field
        split_msg = ["", "switch", "p2a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual("weedle", self.battle.opponent.active.name)
        self.assertTrue(self.battle.opponent.active.revealed)

    def test_adds_intimidate_to_impossible_abilities_when_switching_in(self):
        split_msg = ["", "switch", "p2a: caterpie", "Caterpie, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual("caterpie", self.battle.opponent.active.name)
        self.assertIn("intimidate", self.battle.opponent.active.impossible_abilities)

    def test_does_not_add_sandstream_to_impossible_abilities_if_sand_active(self):
        split_msg = ["", "switch", "p2a: caterpie", "Caterpie, L100, M", "100/100"]
        self.battle.weather = constants.SAND
        switch_or_drag(self.battle, split_msg)

        self.assertEqual("caterpie", self.battle.opponent.active.name)
        self.assertNotIn("sandstream", self.battle.opponent.active.impossible_abilities)

    def test_switch_out_clears_disabled_moves_and_disable_duration(self):
        # a Disabled/Cursed-Body-disabled move is restored on switch-out: PS
        # rebuilds moveSlots from baseMoveSlots and drops the volatile WITHOUT
        # emitting |-end|Disable (sim/pokemon.ts:1514-1541 clearVolatile), so
        # the outgoing pkmn must not keep a phantom mv.disabled=True
        self.opponent_active.add_move("thunderbolt")
        self.opponent_active.add_move("dracometeor")
        self.opponent_active.get_move("thunderbolt").disabled = True
        self.opponent_active.volatile_statuses.append(constants.DISABLE)
        self.opponent_active.volatile_status_durations[constants.DISABLE] = 3

        split_msg = ["", "switch", "p2a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertFalse(
            any(m.disabled for m in self.opponent_active.moves),
            [(m.name, m.disabled) for m in self.opponent_active.moves],
        )
        self.assertEqual(
            0, self.opponent_active.volatile_status_durations[constants.DISABLE]
        )
        self.assertNotIn(constants.DISABLE, self.opponent_active.volatile_statuses)

    def test_user_switch_out_clears_disabled_moves(self):
        self.battle.user.active.add_move("thunderbolt")
        self.battle.user.active.get_move("thunderbolt").disabled = True
        pikachu = self.battle.user.active
        self.battle.user.reserve = [Pokemon("weedle", 100)]

        split_msg = ["", "switch", "p1a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertFalse(pikachu.get_move("thunderbolt").disabled)

    def test_does_not_add_sandstream_to_impossible_abilities_if_heavy_rain_is_active(
        self,
    ):
        split_msg = ["", "switch", "p2a: caterpie", "Caterpie, L100, M", "100/100"]
        self.battle.weather = constants.HEAVY_RAIN
        switch_or_drag(self.battle, split_msg)

        self.assertEqual("caterpie", self.battle.opponent.active.name)
        self.assertNotIn("sandstream", self.battle.opponent.active.impossible_abilities)

    def test_does_not_add_pressure_to_impossible_abilities_gen3(self):
        self.battle.generation = "gen3"
        split_msg = ["", "switch", "p2a: caterpie", "Caterpie, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual("caterpie", self.battle.opponent.active.name)
        self.assertNotIn("pressure", self.battle.opponent.active.impossible_abilities)

    def test_does_not_add_impossible_ability_if_other_side_has_neutralizinggas(self):
        self.battle.user.active.ability = "neutralizinggas"
        split_msg = ["", "switch", "p2a: caterpie", "Caterpie, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual("caterpie", self.battle.opponent.active.name)
        self.assertNotIn("intimidate", self.battle.opponent.active.impossible_abilities)

    def test_adds_impossible_items_when_switching_in(self):
        split_msg = ["", "switch", "p2a: caterpie", "Caterpie, L100, M", "100/100"]

        for item in ITEMS_REVEALED_ON_SWITCH_IN:
            self.assertNotIn(item, self.battle.opponent.active.impossible_items)

        switch_or_drag(self.battle, split_msg)

        for item in ITEMS_REVEALED_ON_SWITCH_IN:
            self.assertIn(item, self.battle.opponent.active.impossible_items)

    def test_cramorantgulping_reverts_to_cramorant_in_switchout(self):
        self.battle.opponent.active.name = "cramorantgulping"
        split_msg = ["", "switch", "p2a: caterpie", "Caterpie, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual("caterpie", self.battle.opponent.active.name)
        self.assertIn("cramorant", [p.name for p in self.battle.opponent.reserve])
        self.assertNotIn(
            "cramorantgulping", [p.name for p in self.battle.opponent.reserve]
        )

    def test_user_switching_in_zaciancrowned_properly_re_initializes_stats(self):
        self.battle.request_json = {
            "active": [],
            "side": {
                "pokemon": [
                    {
                        "ident": "p1: Zacian",
                        "details": "Zacian-Crowned",
                        "condition": "211/325",
                        "active": True,
                        "stats": {
                            "atk": 399,
                            "def": 267,
                            "spa": 176,
                            "spd": 266,
                            "spe": 434,
                        },
                        "moves": [
                            "behemothblade",
                            "swordsdance",
                            "wildcharge",
                            "closecombat",
                        ],
                        "baseAbility": "intrepidsword",
                        "item": "rustedsword",
                        "pokeball": "pokeball",
                        "ability": "intrepidsword",
                        "commanding": False,
                        "reviving": False,
                        "teraType": "Flying",
                        "terastallized": "",
                    }
                ]
            },
        }
        self.battle.user.active = Pokemon("weedle", 100)
        zacian_crowned_reserve = Pokemon("zaciancrowned", 100)
        zacian_crowned_reserve.stats = {
            constants.ATTACK: 359,  # should be replaced with 399
            constants.DEFENSE: 267,
            constants.SPECIAL_ATTACK: 176,
            constants.SPECIAL_DEFENSE: 266,
            constants.SPEED: 434,
        }
        self.battle.user.reserve = [zacian_crowned_reserve]
        split_msg = ["", "switch", "p1a: Zacian", "Zacian-Crowned", "211/325"]
        switch_or_drag(self.battle, split_msg)
        self.assertEqual(399, self.battle.user.active.stats[constants.ATTACK])

    def test_switch_properly_switches_zoroark_for_user_when_last_selected_move_was_zoroark(
        self,
    ):
        self.battle.request_json = {
            "active": [],
            "side": {
                "pokemon": [
                    {
                        "ident": "p1: Zoroark",
                        "details": "Zoroark, L100, M",
                        "active": True,
                    },
                    {
                        "ident": "p1: Weedle",
                        "details": "Weedle, L100, M",
                        "active": False,
                    },
                ]
            },
        }
        self.battle.reserve = [
            Pokemon("zoroark", 100),
            Pokemon("weedle", 100),
        ]
        self.battle.user.last_selected_move = LastUsedMove(
            "caterpie", "switch zoroark", 0
        )
        split_msg = ["", "switch", "p1a: Weedle", "Weedle, L100, M", "100/100"]
        switch(self.battle, split_msg)

        self.assertEqual("zoroark", self.battle.user.active.name)

    def test_being_dragged_into_zoroark_properly_sets_zoroark(self):
        self.battle.request_json = {
            "active": [],
            "side": {
                "pokemon": [
                    {
                        "ident": "p1: Zoroark",
                        "details": "Zoroark, L100, M",
                        "active": True,
                    },
                    {
                        "ident": "p1: Weedle",
                        "details": "Weedle, L100, M",
                        "active": False,
                    },
                ]
            },
        }
        self.battle.reserve = [
            Pokemon("zoroark", 100),
            Pokemon("weedle", 100),
        ]
        split_msg = ["", "drag", "p1a: Weedle", "Weedle, L100, M", "100/100"]
        drag(self.battle, split_msg)

        self.assertEqual("zoroark", self.battle.user.active.name)

    def test_being_dragged_into_not_zoroark_properly_sets_not_zoroark(self):
        self.battle.request_json = {
            "active": [],
            "side": {
                "pokemon": [
                    {
                        "ident": "p1: Zoroark",
                        "details": "Zoroark, L100, M",
                        "active": False,
                    },
                    {
                        "ident": "p1: Weedle",
                        "details": "Weedle, L100, M",
                        "active": True,
                    },
                ]
            },
        }
        self.battle.reserve = [
            Pokemon("zoroark", 100),
            Pokemon("weedle", 100),
        ]
        split_msg = ["", "drag", "p1a: Weedle", "Weedle, L100, M", "100/100"]
        drag(self.battle, split_msg)

        self.assertEqual("weedle", self.battle.user.active.name)

    def test_switch_properly_resets_types_when_pkmn_was_typechanged(self):
        self.battle.opponent.active.volatile_statuses.append(constants.TYPECHANGE)
        self.battle.opponent.active.types = ["fire"]
        active = self.battle.opponent.active
        split_msg = ["", "switch", "p2a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual(["bug"], active.types)

    def test_switch_properly_resets_ability_when_pkmn_had_ability_changed(self):
        self.battle.opponent.active.ability = "lingeringarmoa"
        self.battle.opponent.active.original_ability = "intimidate"
        active = self.battle.opponent.active
        split_msg = ["", "switch", "p2a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual("intimidate", active.ability)

    def test_increments_rest_turns_by_consequtive_sleeptalks(self):
        self.battle.generation = "gen3"
        active = self.battle.opponent.active
        active.gen_3_consecutive_sleep_talks = 1
        active.rest_turns = 1
        active.status = constants.SLEEP
        split_msg = ["", "switch", "p2a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual(0, active.gen_3_consecutive_sleep_talks)
        self.assertEqual(2, active.rest_turns)

    def test_decrements_sleep_turns_by_consequtive_sleeptalks(self):
        self.battle.generation = "gen3"
        active = self.battle.opponent.active
        active.gen_3_consecutive_sleep_talks = 1
        active.sleep_turns = 1
        active.status = constants.SLEEP
        split_msg = ["", "switch", "p2a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual(0, active.gen_3_consecutive_sleep_talks)
        self.assertEqual(0, active.rest_turns)

    def test_switch_properly_resets_rest_turns_to_2_in_gen5(self):
        self.battle.generation = "gen5"
        active = self.battle.opponent.active
        active.rest_turns = 1
        active.status = constants.SLEEP
        split_msg = ["", "switch", "p2a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual(3, active.rest_turns)

    def test_switch_properly_resets_sleep_turns_to_0_in_gen5(self):
        self.battle.opponent.active.volatile_statuses.append(constants.TYPECHANGE)
        self.battle.generation = "gen5"
        active = self.battle.opponent.active
        active.sleep_turns = 1
        active.status = constants.SLEEP
        split_msg = ["", "switch", "p2a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual(0, active.sleep_turns)

    def test_switch_does_not_reset_sleep_turns_to_0_in_gen4(self):
        self.battle.opponent.active.volatile_statuses.append(constants.TYPECHANGE)
        self.battle.generation = "gen4"
        active = self.battle.opponent.active
        active.sleep_turns = 1
        active.status = constants.SLEEP
        split_msg = ["", "switch", "p2a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual(1, active.sleep_turns)

    def test_switch_opponents_pokemon_successfully_creates_new_pokemon_for_active(self):
        new_pkmn = Pokemon("weedle", 100)
        split_msg = ["", "switch", "p2a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual(new_pkmn, self.battle.opponent.active)

    def test_bot_switching_properly_heals_pokemon_if_it_had_regenerator(self):
        current_active = self.battle.user.active
        self.battle.user.active.ability = "regenerator"
        self.battle.user.active.hp = 1
        self.battle.user.active.max_hp = 300
        split_msg = ["", "switch", "p1a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual(101, current_active.hp)  # 100 hp from regenerator heal

    def test_bot_switching_with_regenerator_does_not_overheal(self):
        current_active = self.battle.user.active
        self.battle.user.active.ability = "regenerator"
        self.battle.user.active.hp = 250
        self.battle.user.active.max_hp = 300
        split_msg = ["", "switch", "p1a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual(300, current_active.hp)  # 50 hp from regenerator heal

    def test_fainted_pokemon_switching_does_not_heal(self):
        current_active = self.battle.user.active
        self.battle.user.active.ability = "regenerator"
        self.battle.user.active.hp = 0
        self.battle.user.active.fainted = True
        self.battle.user.active.max_hp = 300
        split_msg = ["", "switch", "p1a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual(
            0, current_active.hp
        )  # no regenerator heal when you are fainted

    def test_nickname_attribute_is_set_when_switching(self):
        # |switch|p2a: Sus|Amoonguss, F|100/100
        split_msg = ["", "switch", "p2a: Sus", "Amoonguss, F", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual(self.battle.opponent.active.name, "amoonguss")
        self.assertEqual(self.battle.opponent.active.nickname, "Sus")

    def test_switch_resets_toxic_count_for_opponent(self):
        self.battle.opponent.side_conditions[constants.TOXIC_COUNT] = 1
        split_msg = ["", "switch", "p2a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual(0, self.battle.opponent.side_conditions[constants.TOXIC_COUNT])

    def test_switch_resets_toxic_count_for_opponent_when_there_is_no_toxic_count(self):
        split_msg = ["", "switch", "p2a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual(0, self.battle.opponent.side_conditions[constants.TOXIC_COUNT])

    def test_switch_resets_toxic_count_for_user(self):
        self.battle.user.side_conditions[constants.TOXIC_COUNT] = 1
        split_msg = ["", "switch", "p1a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual(0, self.battle.user.side_conditions[constants.TOXIC_COUNT])

    def test_switch_opponents_pokemon_successfully_places_previous_active_pokemon_in_reserve(
        self,
    ):
        split_msg = ["", "switch", "p2a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertIn(self.opponent_active, self.battle.opponent.reserve)

    def test_switch_opponents_pokemon_creates_reserve_of_length_1_when_reserve_was_previously_empty(
        self,
    ):
        split_msg = ["", "switch", "p2a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual(1, len(self.battle.opponent.reserve))

    def test_switch_into_already_seen_pokemon_does_not_create_a_new_pokemon(self):
        already_seen_pokemon = Pokemon("weedle", 100)
        self.battle.opponent.reserve.append(already_seen_pokemon)
        split_msg = ["", "switch", "p2a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual(1, len(self.battle.opponent.reserve))

    def test_user_switching_causes_pokemon_to_switch(self):
        already_seen_pokemon = Pokemon("weedle", 100)
        self.battle.user.reserve.append(already_seen_pokemon)
        split_msg = ["", "switch", "p1a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual(Pokemon("weedle", 100), self.battle.user.active)

    def test_user_switching_causes_active_pokemon_to_be_placed_in_reserve(self):
        already_seen_pokemon = Pokemon("weedle", 100)
        self.battle.user.reserve.append(already_seen_pokemon)
        split_msg = ["", "switch", "p1a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual(Pokemon("pikachu", 100), self.battle.user.reserve[0])

    def test_user_switching_removes_volatile_statuses(self):
        user_active = self.battle.user.active
        already_seen_pokemon = Pokemon("weedle", 100)
        self.battle.user.reserve.append(already_seen_pokemon)
        user_active.volatile_statuses = ["flashfire", "encore", "taunt"]
        user_active.volatile_status_durations["encore"] = 1
        user_active.volatile_status_durations["taunt"] = 2
        split_msg = ["", "switch", "p1a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual([], user_active.volatile_statuses)
        self.assertEqual(0, user_active.volatile_status_durations["encore"])
        self.assertEqual(0, user_active.volatile_status_durations["taunt"])

    def test_already_seen_pokemon_is_the_same_object_as_the_one_in_the_reserve(self):
        already_seen_pokemon = Pokemon("weedle", 100)
        self.battle.opponent.reserve.append(already_seen_pokemon)
        split_msg = ["", "switch", "p2a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertIs(already_seen_pokemon, self.battle.opponent.active)

    def test_silvally_steel_replaces_silvally(self):
        already_seen_pokemon = Pokemon("silvally", 100)
        self.battle.opponent.reserve.append(already_seen_pokemon)
        split_msg = [
            "",
            "switch",
            "p2a: silvally",
            "Silvally-Steel, L100, M",
            "100/100",
        ]
        switch_or_drag(self.battle, split_msg)

        expected_pokemon = Pokemon("silvallysteel", 100)

        self.assertEqual(expected_pokemon, self.battle.opponent.active)

    def test_silvally_steel_with_nickname_replaces_silvally(self):
        already_seen_pokemon = Pokemon("silvally", 100)
        self.battle.opponent.reserve.append(already_seen_pokemon)
        split_msg = [
            "",
            "switch",
            "p2a: notsilvally",
            "Silvally-Steel, L100, M",
            "100/100",
        ]
        switch_or_drag(self.battle, split_msg)

        expected_pokemon = Pokemon("silvallysteel", 100)

        self.assertEqual(expected_pokemon, self.battle.opponent.active)

    def test_silvally_replaces_reserve_silvally_with_different_name(self):
        already_seen_pokemon = Pokemon("silvally", 100)
        already_seen_pokemon.unknown_forme = True
        self.battle.opponent.reserve.append(already_seen_pokemon)
        split_msg = [
            "",
            "switch",
            "p2a: notsilvally",
            "Silvally-Steel, L100, M",
            "100/100",
        ]
        switch_or_drag(self.battle, split_msg)

        expected_pokemon = Pokemon("silvallysteel", 100)

        self.assertEqual(expected_pokemon, self.battle.opponent.active)
        self.assertNotIn(already_seen_pokemon, self.battle.opponent.reserve)

    def test_silvally_switching_in_preserves_previous_hp(self):
        already_seen_pokemon = Pokemon("silvallysteel", 100)
        already_seen_pokemon.hp = already_seen_pokemon.max_hp / 2
        self.battle.opponent.reserve.append(already_seen_pokemon)
        split_msg = [
            "",
            "switch",
            "p2a: notsilvally",
            "Silvally-Steel, L100, M",
            "50/100",
        ]
        switch_or_drag(self.battle, split_msg)

        # `50/100` on a 352 max-HP mon means hp in [173, 176]; the midpoint of
        # that band is 174 (see fp/hp_certificate.py -- max_hp / 2 == 176 is the
        # band's top edge, not its centre)
        self.assertEqual(174, self.battle.opponent.active.hp)

    def test_arceus_ghost_switching_in(self):
        already_seen_pokemon = Pokemon("arceus", 100)
        self.battle.opponent.reserve.append(already_seen_pokemon)
        split_msg = ["", "switch", "p2a: Arceus", "Arceus-Ghost", "100/100"]
        switch_or_drag(self.battle, split_msg)

        expected_pokemon = Pokemon("arceus-ghost", 100)

        self.assertEqual(expected_pokemon, self.battle.opponent.active)

    def test_existing_boosts_on_opponents_active_pokemon_are_cleared_when_switching(
        self,
    ):
        self.opponent_active.boosts[constants.ATTACK] = 1
        self.opponent_active.boosts[constants.SPEED] = 1
        split_msg = ["", "switch", "p2a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual({}, self.opponent_active.boosts)

    def test_existing_boosts_on_bots_active_pokemon_are_cleared_when_switching(self):
        pkmn = self.battle.user.active
        pkmn.boosts[constants.ATTACK] = 1
        pkmn.boosts[constants.SPEED] = 1
        split_msg = ["", "switch", "p1a: pidgey", "Pidgey, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual({}, pkmn.boosts)

    def test_switching_into_the_same_pokemon_does_not_put_that_pokemon_in_the_reserves(
        self,
    ):
        # this is specifically for Zororak
        split_msg = ["", "switch", "p2a: caterpie", "Caterpie, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertFalse(self.battle.opponent.reserve)

    def test_switching_sets_last_move_to_none(self):
        split_msg = ["", "switch", "p2a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        expected_last_move = LastUsedMove(None, "switch weedle", 0)

        self.assertEqual(expected_last_move, self.battle.opponent.last_used_move)

    def test_ditto_switching_sets_ability_to_imposter_via_original_ability(self):
        ditto = Pokemon("ditto", 100)
        ditto.ability = "some_ability"
        ditto.original_ability = "imposter"
        ditto.volatile_statuses.append(constants.TRANSFORM)
        self.battle.opponent.active = ditto
        split_msg = ["", "switch", "p2a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        if self.battle.opponent.reserve[0] != ditto:
            self.fail("Ditto was not moved to reserves")

        self.assertEqual("imposter", ditto.ability)

    def test_ditto_switching_sets_moves_to_empty_list(self):
        ditto = Pokemon("ditto", 100)
        ditto.moves = [Move("tackle"), Move("stringshot")]
        ditto.volatile_statuses.append(constants.TRANSFORM)
        self.battle.opponent.active = ditto

        split_msg = ["", "switch", "p2a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        if self.battle.opponent.reserve[0] != ditto:
            self.fail("Ditto was not moved to reserves")

        self.assertEqual([], ditto.moves)

    def test_ditto_switching_sets_moves_to_empty_list_for_user(self):
        ditto = Pokemon("ditto", 100)
        ditto.moves = [Move("tackle"), Move("stringshot")]
        ditto.volatile_statuses.append(constants.TRANSFORM)
        self.battle.user.active = ditto

        split_msg = ["", "switch", "p1a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        if self.battle.user.reserve[0] != ditto:
            self.fail("Ditto was not moved to reserves")

        self.assertEqual([], ditto.moves)

    def test_ditto_switching_resets_stats(self):
        ditto = Pokemon("ditto", 100)
        ditto.stats = {
            constants.ATTACK: 1,
            constants.DEFENSE: 2,
            constants.SPECIAL_ATTACK: 3,
            constants.SPECIAL_DEFENSE: 4,
            constants.SPEED: 5,
        }
        ditto.volatile_statuses.append(constants.TRANSFORM)
        self.battle.opponent.active = ditto

        split_msg = ["", "switch", "p2a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        if self.battle.opponent.reserve[0] != ditto:
            self.fail("Ditto was not moved to reserves")

        expected_stats = calculate_stats(ditto.base_stats, ditto.level)

        self.assertEqual(expected_stats, ditto.stats)

    def test_ditto_switching_resets_boosts(self):
        ditto = Pokemon("ditto", 100)
        ditto.boosts = {
            constants.ATTACK: 1,
            constants.DEFENSE: 2,
            constants.SPECIAL_ATTACK: 3,
            constants.SPECIAL_DEFENSE: 4,
            constants.SPEED: 5,
        }
        ditto.volatile_statuses.append(constants.TRANSFORM)
        self.battle.opponent.active = ditto

        split_msg = ["", "switch", "p2a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        if self.battle.opponent.reserve[0] != ditto:
            self.fail("Ditto was not moved to reserves")

        self.assertEqual({}, ditto.boosts)

    def test_ditto_switching_resets_types(self):
        ditto = Pokemon("ditto", 100)
        ditto.types = ["fairy", "flying"]
        ditto.volatile_statuses.append(constants.TRANSFORM)
        self.battle.opponent.active = ditto

        split_msg = ["", "switch", "p2a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        if self.battle.opponent.reserve[0] != ditto:
            self.fail("Ditto was not moved to reserves")

        self.assertEqual(["normal"], ditto.types)

    def test_ditto_switching_resets_transformed_into(self):
        ditto = Pokemon("ditto", 100)
        ditto.transformed_into = "weedle"
        ditto.volatile_statuses.append(constants.TRANSFORM)
        self.battle.opponent.active = ditto

        split_msg = ["", "switch", "p2a: weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        if self.battle.opponent.reserve[0] != ditto:
            self.fail("Ditto was not moved to reserves")

        self.assertIsNone(ditto.transformed_into)

    def test_shed_tail_switching_in_gets_shed_tailing_flag_set_to_false(self):
        self.battle.user.shed_tailing = True

        split_msg = [
            "",
            "switch",
            "p1a: Pikachu",
            "Pikachu, L100, M",
            "100/100",
            "[from] Shed Tail",
        ]
        switch_or_drag(self.battle, split_msg)

        self.assertFalse(self.battle.user.shed_tailing)

    def test_shed_tail_switching_in_only_keeps_substitute(self):
        self.battle.user.active.volatile_statuses = [
            constants.SUBSTITUTE,
            constants.LEECH_SEED,
        ]
        self.battle.user.active.boosts[constants.SPEED] = 1
        self.battle.user.active.boosts[constants.ATTACK] = -2

        split_msg = [
            "",
            "switch",
            "p1a: Pikachu",
            "Pikachu, L100, M",
            "100/100",
            "[from] Shed Tail",
        ]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual(0, self.battle.user.active.boosts[constants.SPEED])
        self.assertEqual(0, self.battle.user.active.boosts[constants.ATTACK])
        self.assertEqual(
            [constants.SUBSTITUTE], self.battle.user.active.volatile_statuses
        )


class TestHealOrDamage(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.user_active = Pokemon("caterpie", 100)
        self.opponent_active = Pokemon("caterpie", 100)

        # manually set hp to 200 for testing purposes
        self.opponent_active.max_hp = 200
        self.opponent_active.hp = 200

        self.battle.opponent.active = self.opponent_active
        self.battle.user.active = self.user_active

    def test_50_100_g_message(self):
        split_msg = [
            "",
            "-heal",
            "p1a: Pikachu",
            "50/100g",
        ]
        heal_or_damage(self.battle, split_msg)
        self.assertEqual(
            0.5, self.battle.user.active.hp / self.battle.user.active.max_hp
        )

    def test_heal_from_healing_wish_clears_side_condition(self):
        # |-heal|p1a: Caterpie|100/100|[from] move: Healing Wish
        self.battle.opponent.side_conditions[constants.HEALING_WISH] = 1
        split_msg = [
            "",
            "-heal",
            "p2a: Caterpie",
            "100/100",
            "[from] move: Healing Wish",
        ]
        heal_or_damage(self.battle, split_msg)
        self.assertEqual(
            0, self.battle.opponent.side_conditions[constants.HEALING_WISH]
        )

    def test_heal_from_healing_wish_clears_status(self):
        # PS's Healing Wish slot condition heals to full AND calls
        # `target.clearStatus()` (data/moves.ts healingwish `onSwap`), but
        # announces only the -heal line -- there is NO -curestatus to drive the
        # normal cure path.  synth03334 T27:
        #   |switch|p2a: Ursaluna|Ursaluna, L79, M|72/100 brn
        #   |-heal|p2a: Ursaluna|100/100|[from] move: Healing Wish
        self.battle.opponent.side_conditions[constants.HEALING_WISH] = 1
        self.battle.opponent.active.status = constants.BURN
        self.battle.opponent.active.hp = 72
        self.battle.opponent.active.max_hp = 100
        split_msg = [
            "",
            "-heal",
            "p2a: Caterpie",
            "100/100",
            "[from] move: Healing Wish",
        ]
        heal_or_damage(self.battle, split_msg)
        self.assertIsNone(self.battle.opponent.active.status)
        self.assertEqual(
            self.battle.opponent.active.max_hp, self.battle.opponent.active.hp
        )

    def test_heal_from_healing_wish_resets_toxic_count(self):
        self.battle.opponent.side_conditions[constants.HEALING_WISH] = 1
        self.battle.opponent.side_conditions[constants.TOXIC_COUNT] = 4
        self.battle.opponent.active.status = constants.TOXIC
        split_msg = [
            "",
            "-heal",
            "p2a: Caterpie",
            "100/100",
            "[from] move: Healing Wish",
        ]
        heal_or_damage(self.battle, split_msg)
        self.assertIsNone(self.battle.opponent.active.status)
        self.assertEqual(0, self.battle.opponent.side_conditions[constants.TOXIC_COUNT])

    def test_heal_from_lunar_dance_clears_status(self):
        # data/moves.ts lunardance does the same heal + clearStatus (plus PP)
        self.battle.opponent.active.status = constants.PARALYZED
        split_msg = [
            "",
            "-heal",
            "p2a: Caterpie",
            "100/100",
            "[from] move: Lunar Dance",
        ]
        heal_or_damage(self.battle, split_msg)
        self.assertIsNone(self.battle.opponent.active.status)

    def test_ordinary_heal_does_not_clear_status(self):
        self.battle.opponent.active.status = constants.BURN
        split_msg = [
            "",
            "-heal",
            "p2a: Caterpie",
            "100/100",
            "[from] move: Wish",
        ]
        heal_or_damage(self.battle, split_msg)
        self.assertEqual(constants.BURN, self.battle.opponent.active.status)

    def test_sets_ability_when_the_information_is_present(self):
        split_msg = [
            "",
            "-heal",
            "p2a: Quagsire",
            "68/100",
            "[from] ability: Water Absorb",
            "[of] p1a: Genesect",
        ]
        heal_or_damage(self.battle, split_msg)
        self.assertEqual("waterabsorb", self.battle.opponent.active.ability)

    def test_sets_ability_when_the_bot_is_damaged_from_opponents_ability(self):
        split_msg = [
            "",
            "-damage",
            "p1a: Lamdorus",
            "167/319",
            "[from] ability: Iron Barbs",
            "[of] p2a: Ferrothorn",
        ]
        heal_or_damage(self.battle, split_msg)
        self.assertEqual("ironbarbs", self.battle.opponent.active.ability)

    def test_sets_ability_when_the_opponent_is_damaged_from_bots_ability(self):
        split_msg = [
            "",
            "-damage",
            "p2a: Lamdorus",
            "167/319",
            "[from] ability: Iron Barbs",
            "[of] p1a: Ferrothorn",
        ]
        heal_or_damage(self.battle, split_msg)
        self.assertEqual("ironbarbs", self.battle.user.active.ability)

    def test_sets_item_when_it_causes_the_bot_damage(self):
        split_msg = [
            "",
            "-damage",
            "p1a: Kartana",
            "167/319",
            "[from] item: Rocky Helmet",
            "[of] p2a: Ferrothorn",
        ]
        heal_or_damage(self.battle, split_msg)
        self.assertEqual("rockyhelmet", self.battle.opponent.active.item)

    def test_sets_item_when_it_causes_the_opponent_damage(self):
        split_msg = [
            "",
            "-damage",
            "p2a: Kartana",
            "167/319",
            "[from] item: Rocky Helmet",
            "[of] p1a: Ferrothorn",
        ]
        heal_or_damage(self.battle, split_msg)
        self.assertEqual("rockyhelmet", self.battle.user.active.item)

    def test_does_not_set_item_when_item_is_none(self):
        # |-heal|p2a: Drifblim|37/100|[from] item: Sitrus Berry
        split_msg = [
            "",
            "-heal",
            "p2a: Drifblim",
            "37/100",
            "[from] item: Sitrus Berry",
        ]
        self.battle.opponent.active.item = None
        heal_or_damage(self.battle, split_msg)
        self.assertEqual(None, self.battle.opponent.active.item)

    def test_damage_sets_opponents_active_pokemon_to_correct_hp(self):
        # `80/100` on a 200 max-HP mon means `ceil(100*hp/200) == 80`, i.e.
        # hp in [159, 160]; the point estimate is the MIDPOINT of the band the
        # protocol stated, not `max_hp * pct` -- that was the one-sided
        # estimator whose checker-side `round` put APPROXIMATIONS U3's hard
        # finding an HP OUTSIDE the band.  See fp/hp_certificate.py.
        split_msg = ["", "-damage", "p2a: Caterpie", "80/100"]
        heal_or_damage(self.battle, split_msg)
        self.assertEqual(159, self.battle.opponent.active.hp)
        self.assertFalse(self.battle.opponent.active.hp_exact)

    def test_damage_sets_bots_active_pokemon_to_correct_hp(self):
        split_msg = ["", "-damage", "p1a: Caterpie", "150/250"]
        heal_or_damage(self.battle, split_msg)
        self.assertEqual(150, self.battle.user.active.hp)

    def test_damage_sets_bots_active_pokemon_to_correct_maxhp(self):
        split_msg = ["", "-damage", "p1a: Caterpie", "150/250"]
        heal_or_damage(self.battle, split_msg)
        self.assertEqual(250, self.battle.user.active.max_hp)

    def test_damage_sets_bots_active_pokemon_to_zero_hp(self):
        split_msg = ["", "-damage", "p1a: Caterpie", "0 fnt"]
        heal_or_damage(self.battle, split_msg)
        self.assertEqual(0, self.battle.user.active.hp)

    def test_fainted_message_properly_faints_opponents_pokemon(self):
        split_msg = ["", "-damage", "p2a: Caterpie", "0 fnt"]
        heal_or_damage(self.battle, split_msg)
        self.assertEqual(0, self.battle.opponent.active.hp)

    def test_damage_caused_by_an_item_properly_sets_opponents_item(self):
        split_msg = ["", "-damage", "p2a: Caterpie", "100/100", "[from] item: Life Orb"]
        heal_or_damage(self.battle, split_msg)
        self.assertEqual("lifeorb", self.battle.opponent.active.item)

    def test_damage_caused_by_toxic_increases_side_condition_toxic_counter_for_opponent(
        self,
    ):
        split_msg = ["", "-damage", "p2a: Caterpie", "94/100 tox", "[from] psn"]
        heal_or_damage(self.battle, split_msg)
        self.assertEqual(1, self.battle.opponent.side_conditions[constants.TOXIC_COUNT])

    def test_damage_caused_by_toxic_increases_side_condition_toxic_counter_for_user(
        self,
    ):
        split_msg = ["", "-damage", "p1a: Caterpie", "94/100 tox", "[from] psn"]
        heal_or_damage(self.battle, split_msg)
        self.assertEqual(1, self.battle.user.side_conditions[constants.TOXIC_COUNT])

    def test_toxic_count_increases_to_2(self):
        self.battle.opponent.side_conditions[constants.TOXIC_COUNT] = 1
        split_msg = ["", "-damage", "p2a: Caterpie", "94/100 tox", "[from] psn"]
        heal_or_damage(self.battle, split_msg)
        self.assertEqual(2, self.battle.opponent.side_conditions[constants.TOXIC_COUNT])

    def test_damage_caused_by_non_toxic_damage_does_not_increase_toxic_count(self):
        split_msg = [
            "",
            "-damage",
            "p2a: Caterpie",
            "50/100 tox",
            "[from] item: Life Orb",
        ]
        heal_or_damage(self.battle, split_msg)
        self.assertEqual(0, self.battle.opponent.side_conditions[constants.TOXIC_COUNT])

    def test_healing_from_ability_sets_ability_to_opponent(self):
        split_msg = [
            "",
            "-heal",
            "p2a: Caterpie",
            "50/100",
            "[from] ability: Volt Absorb",
            "[of] p1a: Caterpie",
        ]
        heal_or_damage(self.battle, split_msg)
        self.assertEqual("voltabsorb", self.battle.opponent.active.ability)

    def test_healing_from_ability_does_not_set_bots_ability(self):
        self.battle.user.active.ability = None
        split_msg = [
            "",
            "-heal",
            "p2a: Caterpie",
            "50/100",
            "[from] ability: Volt Absorb",
            "[of] p1a: Caterpie",
        ]
        heal_or_damage(self.battle, split_msg)
        self.assertIsNone(self.battle.user.active.ability)

    def test_healing_from_revivalblessing_for_opponent_pkmn(self):
        amoongus_reserve = Pokemon("amoonguss", 100)
        amoongus_reserve.nickname = "Sus"
        amoongus_reserve.hp = 0
        amoongus_reserve.fainted = True
        self.battle.opponent.reserve = [amoongus_reserve]

        # |-heal|p1: Amoonguss|50/100|[from] move: Revival Blessing
        split_msg = ["", "-heal", "p2a: Sus", "50/100", "[from] move: Revival Blessing"]
        heal_or_damage(self.battle, split_msg)
        self.assertEqual(amoongus_reserve.hp, int(amoongus_reserve.max_hp / 2))

    def test_healing_from_revivalblessing_for_bot_pkmn(self):
        amoongus_reserve = Pokemon("amoonguss", 100)
        amoongus_reserve.nickname = "Sus"
        amoongus_reserve.hp = 0
        amoongus_reserve.fainted = True
        self.battle.user.reserve = [amoongus_reserve]

        split_msg = [
            "",
            "-heal",
            "p1a: Sus",
            "150/301",
            "[from] move: Revival Blessing",
        ]
        heal_or_damage(self.battle, split_msg)
        self.assertEqual(amoongus_reserve.hp, int(amoongus_reserve.max_hp / 2))

    def test_revivalblessing_increments_times_revived_for_opponent(self):
        # PS's side.totalFainted is cumulative and a revive never decrements it
        # (sim/battle.ts:2551), so the engine reconstructs it as
        # num_fainted_pkmn() + times_revived. One `-heal ... [from] move: Revival
        # Blessing` line is emitted per revive (sim/battle.ts:2793).
        amoongus_reserve = Pokemon("amoonguss", 100)
        amoongus_reserve.nickname = "Sus"
        amoongus_reserve.hp = 0
        amoongus_reserve.fainted = True
        self.battle.opponent.reserve = [amoongus_reserve]

        self.assertEqual(0, self.battle.opponent.times_revived)

        split_msg = ["", "-heal", "p2a: Sus", "50/100", "[from] move: Revival Blessing"]
        heal_or_damage(self.battle, split_msg)

        self.assertEqual(1, self.battle.opponent.times_revived)
        # the counter is per-side: the bot's side must be untouched
        self.assertEqual(0, self.battle.user.times_revived)

    def test_revivalblessing_increments_times_revived_for_bot(self):
        amoongus_reserve = Pokemon("amoonguss", 100)
        amoongus_reserve.nickname = "Sus"
        amoongus_reserve.hp = 0
        amoongus_reserve.fainted = True
        self.battle.user.reserve = [amoongus_reserve]

        split_msg = [
            "",
            "-heal",
            "p1a: Sus",
            "150/301",
            "[from] move: Revival Blessing",
        ]
        heal_or_damage(self.battle, split_msg)

        self.assertEqual(1, self.battle.user.times_revived)
        self.assertEqual(0, self.battle.opponent.times_revived)

    def test_times_revived_accumulates_and_is_never_decremented(self):
        # totalFainted is CUMULATIVE: a second revive on the same side adds to the
        # count, and nothing walks it back.
        first = Pokemon("amoonguss", 100)
        first.nickname = "Sus"
        first.hp = 0
        first.fainted = True
        second = Pokemon("kingambit", 100)
        second.nickname = "Gambit"
        second.hp = 0
        second.fainted = True
        self.battle.opponent.reserve = [first, second]

        heal_or_damage(
            self.battle,
            ["", "-heal", "p2a: Sus", "50/100", "[from] move: Revival Blessing"],
        )
        heal_or_damage(
            self.battle,
            ["", "-heal", "p2a: Gambit", "50/100", "[from] move: Revival Blessing"],
        )
        self.assertEqual(2, self.battle.opponent.times_revived)

        # an ordinary (non-Revival-Blessing) heal must not touch the counter
        heal_or_damage(self.battle, ["", "-heal", "p2a: Sus", "80/100"])
        self.assertEqual(2, self.battle.opponent.times_revived)

    def test_gen1_pkmn_trapping_foe_releases_target_after_hitting_self_in_confusion(
        self,
    ):
        # |-damage|p1a: Rhydon|376/413|[from] confusion
        self.battle.generation = "gen1"
        self.battle.user.active.volatile_statuses.append(constants.CONFUSION)
        self.battle.opponent.active.volatile_statuses.append(
            constants.PARTIALLY_TRAPPED
        )
        self.battle.opponent.active.volatile_status_durations[
            constants.PARTIALLY_TRAPPED
        ] = 1
        split_msg = ["", "-damage", "p1a: Rhydon", "376/413", "[from] confusion"]
        heal_or_damage(self.battle, split_msg)
        self.assertNotIn(
            constants.PARTIALLY_TRAPPED, self.battle.opponent.active.volatile_statuses
        )
        self.assertEqual(
            0,
            self.battle.opponent.active.volatile_status_durations[
                constants.PARTIALLY_TRAPPED
            ],
        )

    def test_direct_move_damage_increments_times_attacked_for_user(self):
        # a '-damage' line with no '[from]' clause is a direct move hit
        split_msg = ["", "-damage", "p1a: Caterpie", "150/250"]
        heal_or_damage(self.battle, split_msg)
        self.assertEqual(1, self.battle.user.active.times_attacked)
        self.assertEqual(0, self.battle.opponent.active.times_attacked)

    def test_direct_move_damage_increments_times_attacked_for_opponent(self):
        split_msg = ["", "-damage", "p2a: Caterpie", "80/100"]
        heal_or_damage(self.battle, split_msg)
        self.assertEqual(1, self.battle.opponent.active.times_attacked)
        self.assertEqual(0, self.battle.user.active.times_attacked)

    def test_direct_move_damage_accumulates_times_attacked(self):
        split_msg = ["", "-damage", "p1a: Caterpie", "150/250"]
        heal_or_damage(self.battle, split_msg)
        heal_or_damage(self.battle, split_msg)
        self.assertEqual(2, self.battle.user.active.times_attacked)

    def test_direct_move_damage_causing_faint_increments_times_attacked(self):
        split_msg = ["", "-damage", "p1a: Caterpie", "0 fnt"]
        heal_or_damage(self.battle, split_msg)
        self.assertEqual(1, self.battle.user.active.times_attacked)

    def test_chip_damage_with_from_clause_does_not_increment_times_attacked(self):
        for chip_msg in [
            ["", "-damage", "p1a: Caterpie", "94/100 tox", "[from] psn"],
            ["", "-damage", "p1a: Caterpie", "88/100", "[from] Stealth Rock"],
            ["", "-damage", "p1a: Caterpie", "94/100", "[from] Sandstorm"],
            ["", "-damage", "p1a: Caterpie", "90/100", "[from] item: Life Orb"],
            ["", "-damage", "p1a: Caterpie", "80/100", "[from] Recoil"],
            ["", "-damage", "p1a: Caterpie", "376/413", "[from] confusion"],
            ["", "-damage", "p1a: Caterpie", "50/100", "[from] High Jump Kick"],
            [
                "",
                "-damage",
                "p1a: Caterpie",
                "167/319",
                "[from] ability: Iron Barbs",
                "[of] p2a: Ferrothorn",
            ],
            [
                "",
                "-damage",
                "p1a: Caterpie",
                "167/319",
                "[from] item: Rocky Helmet",
                "[of] p2a: Ferrothorn",
            ],
        ]:
            heal_or_damage(self.battle, chip_msg)
        self.assertEqual(0, self.battle.user.active.times_attacked)

    def test_heal_does_not_increment_times_attacked(self):
        split_msg = ["", "-heal", "p1a: Caterpie", "150/250"]
        heal_or_damage(self.battle, split_msg)
        self.assertEqual(0, self.battle.user.active.times_attacked)


class TestTimesAttackedSelfCostFilter(unittest.TestCase):
    """The HP cost of Substitute/Belly Drum/ghost-Curse/Fillet Away/Shed Tail/
    Clangorous Soul is a bare '-damage' line on the user, but PS only counts
    timesAttacked for hits from ANOTHER pokemon's move (sim/battle-actions.ts
    :990-996 `pokemon !== target` guards the increment), so those lines must
    not count."""

    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.user_active = Pokemon("caterpie", 100)
        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active
        self.battle.user.active = self.user_active

    def test_substitute_self_cost_damage_is_not_counted(self):
        # |move|p1a: Caterpie|Substitute -> |-start|...|Substitute -> |-damage|
        move(self.battle, ["", "move", "p1a: Caterpie", "Substitute"])
        heal_or_damage(self.battle, ["", "-damage", "p1a: Caterpie", "75/100"])
        self.assertEqual(0, self.battle.user.active.times_attacked)

    def test_each_self_cost_move_is_filtered(self):
        for mv in [
            "Belly Drum",
            "Fillet Away",
            "Shed Tail",
            "Clangorous Soul",
        ]:
            with self.subTest(move=mv):
                self.setUp()
                move(self.battle, ["", "move", "p1a: Caterpie", mv])
                heal_or_damage(
                    self.battle, ["", "-damage", "p1a: Caterpie", "50/100"]
                )
                self.assertEqual(0, self.battle.user.active.times_attacked)

    def test_opponent_self_cost_damage_is_not_counted(self):
        move(self.battle, ["", "move", "p2a: Caterpie", "Belly Drum"])
        heal_or_damage(self.battle, ["", "-damage", "p2a: Caterpie", "50/100"])
        self.assertEqual(0, self.battle.opponent.active.times_attacked)

    def test_real_hit_after_self_cost_still_counts(self):
        # p1 belly drums (cost swallowed), then p2's move connects: the next
        # |move| line invalidates the expectation so the hit counts
        move(self.battle, ["", "move", "p1a: Caterpie", "Belly Drum"])
        heal_or_damage(self.battle, ["", "-damage", "p1a: Caterpie", "50/100"])
        move(self.battle, ["", "move", "p2a: Caterpie", "Tackle"])
        heal_or_damage(self.battle, ["", "-damage", "p1a: Caterpie", "40/100"])
        self.assertEqual(1, self.battle.user.active.times_attacked)

    def test_ghost_curse_self_cost_is_not_counted(self):
        # Curse only costs HP for a Ghost-type user (bare '-damage' on it)
        self.battle.user.active = Pokemon("gengar", 100)
        move(self.battle, ["", "move", "p1a: Gengar", "Curse"])
        heal_or_damage(self.battle, ["", "-damage", "p1a: Gengar", "50/100"])
        self.assertEqual(0, self.battle.user.active.times_attacked)

    def test_non_ghost_curse_sets_no_expectation(self):
        # a non-ghost Curse emits boost lines and never a cost '-damage'; a
        # later bare '-damage' (e.g. Future Sight impact) must still count
        move(self.battle, ["", "move", "p1a: Caterpie", "Curse"])
        heal_or_damage(self.battle, ["", "-damage", "p1a: Caterpie", "60/100"])
        self.assertEqual(1, self.battle.user.active.times_attacked)

    def test_failed_self_cost_move_clears_the_expectation(self):
        # Substitute below 25% HP: |-fail| arrives instead of the cost
        # '-damage', so the expectation is dropped and a later bare
        # '-damage' on this pokemon counts as a real hit
        move(self.battle, ["", "move", "p1a: Caterpie", "Substitute"])
        fail(self.battle, ["", "-fail", "p1a: Caterpie", "move: Substitute"])
        heal_or_damage(self.battle, ["", "-damage", "p1a: Caterpie", "60/100"])
        self.assertEqual(1, self.battle.user.active.times_attacked)


class TestActivate(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.user_active = Pokemon("caterpie", 100)
        self.opponent_active = Pokemon("caterpie", 100)

        # manually set hp to 200 for testing purposes
        self.opponent_active.max_hp = 200
        self.opponent_active.hp = 200

        self.battle.opponent.active = self.opponent_active
        self.battle.user.active = self.user_active

    def test_lingeringarmoa_activating_to_change_abilities(self):
        self.battle.user.active.ability = "intimidate"
        self.battle.opponent.active.ability = (
            None  # lets say ability is unknown beforehand
        )
        split_msg = [
            "",
            "-activate",
            "p2a: Caterpie",
            "ability: Lingering Aroma",
            "Intimidate",
            "[of] p1a: Caterpie",
        ]
        activate(self.battle, split_msg)

        # sets ability but retains original ability
        self.assertEqual("lingeringaroma", self.battle.user.active.ability)
        self.assertEqual("intimidate", self.battle.user.active.original_ability)

        # sets ability on the other pkmn
        self.assertEqual("lingeringaroma", self.battle.opponent.active.ability)

    def test_activating_partially_trapped_whirlpool(self):
        split_msg = [
            "",
            "-activate",
            "p2a: Caterpie",
            "move: Whirlpool",
            "[of] p1a: Luvdisc",
        ]
        activate(self.battle, split_msg)
        self.assertIn(
            constants.PARTIALLY_TRAPPED, self.battle.opponent.active.volatile_statuses
        )

    def test_activating_partially_trapped_magmastorm(self):
        split_msg = [
            "",
            "-activate",
            "p2a: Caterpie",
            "move: Magma Storm",
            "[of] p1a: Luvdisc",
        ]
        activate(self.battle, split_msg)
        self.assertIn(
            constants.PARTIALLY_TRAPPED, self.battle.opponent.active.volatile_statuses
        )

    def test_does_not_activate_partiallytrapped_when_not_a_partiallytrapping_move(self):
        # this isn't something that would cause an `-activate`, but just to make sure the logic is correct
        split_msg = [
            "",
            "-activate",
            "p2a: Caterpie",
            "move: Tackle",
            "[of] p1a: Luvdisc",
        ]
        activate(self.battle, split_msg)
        self.assertNotIn(
            constants.PARTIALLY_TRAPPED, self.battle.opponent.active.volatile_statuses
        )

    def test_does_not_set_consumed_item(self):
        split_msg = [
            "",
            "-activate",
            "p2a: Caterpie",
            "item: Custap Berry",
            "[consumed]",
        ]
        self.battle.opponent.active.item = None
        activate(self.battle, split_msg)
        self.assertIsNone(self.battle.opponent.active.item)

    def test_sets_item_when_poltergeist_activates(self):
        split_msg = [
            "",
            "-activate",
            "p2a: Mandibuzz",
            "Move: Poltergeist",
            "Leftovers",
        ]
        activate(self.battle, split_msg)
        self.assertEqual("leftovers", self.battle.opponent.active.item)

    def test_sets_item_when_poltergeist_activates_and_move_is_lowercase(self):
        split_msg = [
            "",
            "-activate",
            "p2a: Mandibuzz",
            "move: Poltergeist",
            "Leftovers",
        ]
        activate(self.battle, split_msg)
        self.assertEqual("leftovers", self.battle.opponent.active.item)

    def test_sets_item_from_activate(self):
        split_msg = [
            "",
            "-activate",
            "p2a: Mandibuzz",
            "item: Safety Goggles",
            "Stun Spore",
        ]
        activate(self.battle, split_msg)
        self.assertEqual("safetygoggles", self.battle.opponent.active.item)

    def test_sets_ability_from_activate(self):
        split_msg = ["", "-activate", "p2a: Ferrothorn", "ability: Iron Barbs"]
        activate(self.battle, split_msg)
        self.assertEqual("ironbarbs", self.battle.opponent.active.ability)

    def test_sets_substitute_hit_from_activate(self):
        split_msg = ["", "-activate", "p2a: Heatran", "Substitute", "[damage]"]
        activate(self.battle, split_msg)
        self.assertTrue(self.battle.opponent.active.substitute_hit)

    def test_substitute_absorbed_hit_does_not_increment_times_attacked(self):
        # PS does not count a hit absorbed by the substitute toward Rage Fist
        # (Substitute's onTryPrimaryHit returns HIT_SUBSTITUTE before the
        # timesAttacked counter runs); this surviving-sub '-activate' line still
        # sets substitute_hit but must not increment the counter
        split_msg = ["", "-activate", "p2a: Heatran", "Substitute", "[damage]"]
        activate(self.battle, split_msg)
        self.assertEqual(0, self.battle.opponent.active.times_attacked)
        self.assertEqual(0, self.battle.user.active.times_attacked)

    def test_non_substitute_activate_does_not_increment_times_attacked(self):
        split_msg = ["", "-activate", "p2a: Ferrothorn", "ability: Iron Barbs"]
        activate(self.battle, split_msg)
        self.assertEqual(0, self.battle.opponent.active.times_attacked)


class TestPrepare(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.user_active = Pokemon("caterpie", 100)
        self.opponent_active = Pokemon("caterpie", 100)

        # manually set hp to 200 for testing purposes
        self.opponent_active.max_hp = 200
        self.opponent_active.hp = 200

        self.battle.opponent.active = self.opponent_active
        self.battle.user.active = self.user_active

    def test_prepare_sets_volatile_status_on_pokemon(self):
        # |-prepare|p1a: Dragapult|Phantom Force
        split_msg = ["", "-prepare", "p2a: Caterpie", "Phantom Force"]
        prepare(self.battle, split_msg)
        self.assertIn("phantomforce", self.battle.opponent.active.volatile_statuses)


class TestClearAllBoosts(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.user_active = Pokemon("caterpie", 100)
        self.opponent_active = Pokemon("caterpie", 100)

        # manually set hp to 200 for testing purposes
        self.opponent_active.max_hp = 200
        self.opponent_active.hp = 200

        self.battle.opponent.active = self.opponent_active
        self.battle.user.active = self.user_active

    def test_clears_bots_boosts(self):
        split_msg = ["", "-clearallboost"]
        self.battle.user.active.boosts = {constants.ATTACK: 1, constants.DEFENSE: 1}
        clearallboost(self.battle, split_msg)
        self.assertEqual(0, self.battle.user.active.boosts[constants.ATTACK])
        self.assertEqual(0, self.battle.user.active.boosts[constants.DEFENSE])

    def test_clears_opponents_boosts(self):
        split_msg = ["", "-clearallboost"]
        self.battle.opponent.active.boosts = {constants.ATTACK: 1, constants.DEFENSE: 1}
        clearallboost(self.battle, split_msg)
        self.assertEqual(0, self.battle.opponent.active.boosts[constants.ATTACK])
        self.assertEqual(0, self.battle.opponent.active.boosts[constants.DEFENSE])

    def test_clears_opponents_and_botsboosts(self):
        split_msg = ["", "-clearallboost"]
        self.battle.user.active.boosts = {constants.ATTACK: 1, constants.DEFENSE: 1}
        self.battle.opponent.active.boosts = {constants.ATTACK: 1, constants.DEFENSE: 1}
        clearallboost(self.battle, split_msg)
        self.assertEqual(0, self.battle.user.active.boosts[constants.ATTACK])
        self.assertEqual(0, self.battle.user.active.boosts[constants.DEFENSE])
        self.assertEqual(0, self.battle.opponent.active.boosts[constants.ATTACK])
        self.assertEqual(0, self.battle.opponent.active.boosts[constants.DEFENSE])


class TestMove(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active

        self.battle.user.active = Pokemon("clefable", 100)

        TeamDatasets.pkmn_sets = {}

    def test_infer_zoroark_from_move_not_possible_on_pkmn_battle_factory(self):
        self.battle.battle_type = BattleType.BATTLE_FACTORY
        self.battle.generation = "gen9"
        TeamDatasets.initialize(
            "gen9battlefactory", ["zoroarkhisui", "gyarados"], "ru"
        )  # gen9 RU should always have these pokemon
        TeamDatasets.pkmn_sets["zoroarkhisui"] = [
            PredictedPokemonSet(
                pkmn_set=PokemonSet(
                    ability="illusion",
                    item="lifeorb",
                    nature="timid",
                    evs=(0, 0, 0, 252, 4, 252),
                    tera_type="ghost",
                    count=1,
                ),
                pkmn_moveset=PokemonMoveset(
                    moves=["nastyplot", "terablast", "shadowball", "protect"],
                ),
            )
        ]

        self.battle.opponent.reserve = [Pokemon("zoroarkhisui", 100)]
        self.battle.opponent.reserve[0].add_move("nastyplot")

        self.battle.opponent.active = Pokemon("gyarados", 100)
        self.battle.opponent.active.add_move("terablast")
        self.battle.opponent.active.moves_used_since_switch_in.add("terablast")
        self.battle.opponent.active.boosts[constants.SPECIAL_ATTACK] = 2

        split_msg = [
            "",
            "move",
            "p2a: Gyarados",
            "Shadow Ball",
        ]  # Gyarados does not get shadowball in gen9 battle factory
        move(self.battle, split_msg)

        self.assertEqual("zoroarkhisui", self.battle.opponent.active.name)

        # nastyplot was previously revealed on zoroarkhisui
        # terablast was used by gyarados since switching in, but should be re-associated with zoroarkhisui
        # shadowball is the move used to deduce it was a zoroarkhisui and should be added to zoroarkhisui's moves
        self.assertEqual(
            [Move("nastyplot"), Move("terablast"), Move("shadowball")],
            self.battle.opponent.active.moves,
        )
        # the boosts that existed on gyarados should be on the active zoroarkhisui now
        self.assertEqual(
            {constants.SPECIAL_ATTACK: 2}, dict(self.battle.opponent.active.boosts)
        )

        self.assertEqual("gyarados", self.battle.opponent.reserve[0].name)
        # terablast was used by gyarados since switching in so it should be dis-associated with gyarados
        self.assertEqual([], self.battle.opponent.reserve[0].moves)
        self.assertEqual({}, dict(self.battle.opponent.reserve[0].boosts))

    def test_infer_zoroarkhisui_from_move_not_possible_on_pkmn_randbats_when_zoroark_unrevealed(
        self,
    ):
        self.battle.battle_type = BattleType.RANDOM_BATTLE
        self.battle.generation = "gen9"
        RandomBattleTeamDatasets.initialize("gen9randombattle")

        self.battle.opponent.active = Pokemon("gyarados", 79)
        self.battle.opponent.active.add_move("terablast")
        self.battle.opponent.active.moves_used_since_switch_in.add("terablast")
        self.battle.opponent.active.boosts[constants.SPECIAL_ATTACK] = 2

        split_msg = [
            "",
            "move",
            "p2a: Gyarados",
            "Poltergeist",
        ]  # Gyarados does not get Poltergeist in gen9randbats
        move(self.battle, split_msg)

        self.assertEqual("zoroarkhisui", self.battle.opponent.active.name)

        # since this is randbats, just make sure we aren't setting level to 100
        # dont want to assert a specific level because the levels may change
        self.assertNotEqual(100, self.battle.opponent.active.level)

        # terablast was used by gyarados since switching in, but should be re-associated with zoroarkhisui
        # poltergeist is the move used to deduce it was a zoroarkhisui and should be added to zoroarkhisui's moves
        self.assertEqual(
            [Move("terablast"), Move("poltergeist")],
            self.battle.opponent.active.moves,
        )
        # the boosts that existed on gyarados should be on the active zoroarkhisui now
        self.assertEqual(
            {constants.SPECIAL_ATTACK: 2}, dict(self.battle.opponent.active.boosts)
        )

        self.assertEqual("gyarados", self.battle.opponent.reserve[0].name)
        # terablast was used by gyarados since switching in so it should be dis-associated with gyarados
        self.assertEqual([], self.battle.opponent.reserve[0].moves)
        self.assertEqual({}, dict(self.battle.opponent.reserve[0].boosts))

    def test_infer_zoroark_regular_from_move_not_possible_on_pkmn_randbats_when_zoroark_unrevealed(
        self,
    ):
        self.battle.battle_type = BattleType.RANDOM_BATTLE
        self.battle.generation = "gen9"
        RandomBattleTeamDatasets.initialize("gen9randombattle")

        self.battle.opponent.active = Pokemon("gyarados", 79)
        self.battle.opponent.active.add_move("terablast")
        self.battle.opponent.active.moves_used_since_switch_in.add("terablast")
        self.battle.opponent.active.boosts[constants.SPECIAL_ATTACK] = 2

        split_msg = [
            "",
            "move",
            "p2a: Gyarados",
            "Dark Pulse",
        ]
        move(self.battle, split_msg)

        self.assertEqual("zoroark", self.battle.opponent.active.name)

        # since this is randbats, just make sure we aren't setting level to 100
        # dont want to assert a specific level because the levels may change
        self.assertNotEqual(100, self.battle.opponent.active.level)

        self.assertEqual(
            [Move("terablast"), Move("darkpulse")],
            self.battle.opponent.active.moves,
        )
        self.assertEqual(
            {constants.SPECIAL_ATTACK: 2}, dict(self.battle.opponent.active.boosts)
        )

        self.assertEqual("gyarados", self.battle.opponent.reserve[0].name)
        self.assertEqual([], self.battle.opponent.reserve[0].moves)
        self.assertEqual({}, dict(self.battle.opponent.reserve[0].boosts))

    def test_does_not_infer_zoroark_if_move_can_be_on_active_pkmn(
        self,
    ):
        self.battle.battle_type = BattleType.RANDOM_BATTLE
        self.battle.generation = "gen9"
        RandomBattleTeamDatasets.initialize("gen9randombattle")

        self.battle.opponent.active = Pokemon("tornadustherian", 79)
        self.battle.opponent.active.add_move("terablast")
        self.battle.opponent.active.moves_used_since_switch_in.add("terablast")
        self.battle.opponent.active.boosts[constants.SPECIAL_ATTACK] = 2

        split_msg = [
            "",
            "move",
            "p2a: Tornadus Therian",
            "Nasty Plot",
        ]  # Tornadus Therian gets nastyplot so no inferring zoroark
        move(self.battle, split_msg)

        self.assertEqual("tornadustherian", self.battle.opponent.active.name)
        self.assertEqual([], self.battle.opponent.reserve)

    def test_does_not_infer_zoroark_when_struggle(
        self,
    ):
        self.battle.battle_type = BattleType.RANDOM_BATTLE
        self.battle.generation = "gen9"
        RandomBattleTeamDatasets.initialize("gen9randombattle")

        self.battle.opponent.active = Pokemon("tornadustherian", 79)
        self.battle.opponent.active.add_move("terablast")
        self.battle.opponent.active.moves_used_since_switch_in.add("terablast")
        self.battle.opponent.active.boosts[constants.SPECIAL_ATTACK] = 2

        split_msg = [
            "",
            "move",
            "p2a: Tornadus Therian",
            "Struggle",
        ]
        move(self.battle, split_msg)

        self.assertEqual("tornadustherian", self.battle.opponent.active.name)
        self.assertEqual([], self.battle.opponent.reserve)

    def test_does_not_infer_from_struggle(self):
        self.battle.battle_type = BattleType.BATTLE_FACTORY
        self.battle.generation = "gen9"
        TeamDatasets.initialize(
            "gen9battlefactory", ["zoroarkhisui", "gyarados"], "ru"
        )  # gen9 RU should always have these pokemon

        self.battle.opponent.reserve = [Pokemon("zoroarkhisui", 100)]
        self.battle.opponent.active = Pokemon("gyarados", 100)

        split_msg = [
            "",
            "move",
            "p2a: Gyarados",
            "Struggle",
        ]
        move(self.battle, split_msg)

        # nothing changes
        self.assertEqual("gyarados", self.battle.opponent.active.name)
        self.assertEqual("zoroarkhisui", self.battle.opponent.reserve[0].name)

    def test_sets_healing_wish_side_condition_when_healing_wish_is_used(self):
        split_msg = ["", "move", "p2a: Caterpie", "Healing Wish", "p2a: Caterpie"]
        move(self.battle, split_msg)
        self.assertEqual(
            1, self.battle.opponent.side_conditions[constants.HEALING_WISH]
        )

    def test_swordsdance_sets_burn_nullify_volatile_when_burned(self):
        self.battle.generation = "gen1"
        split_msg = ["", "move", "p2a: Caterpie", "Swords Dance"]
        self.battle.opponent.active.status = constants.BURN

        move(self.battle, split_msg)

        self.assertIn("gen1burnnullify", self.battle.opponent.active.volatile_statuses)

    def test_meditate_sets_burn_nullify_volatile_when_burned(self):
        self.battle.generation = "gen1"
        split_msg = ["", "move", "p2a: Caterpie", "Meditate"]
        self.battle.opponent.active.status = constants.BURN

        move(self.battle, split_msg)

        self.assertIn("gen1burnnullify", self.battle.opponent.active.volatile_statuses)

    def test_agility_sets_paralysis_nullify_when_paralyzed(self):
        self.battle.generation = "gen1"
        split_msg = ["", "move", "p2a: Caterpie", "Agility"]
        self.battle.opponent.active.status = constants.PARALYZED

        move(self.battle, split_msg)

        self.assertIn(
            "gen1paralysisnullify", self.battle.opponent.active.volatile_statuses
        )

    def test_adds_move_to_opponent(self):
        split_msg = ["", "move", "p2a: Caterpie", "String Shot"]

        move(self.battle, split_msg)
        m = Move("String Shot")

        self.assertIn(m, self.battle.opponent.active.moves)

    def test_adds_truant_when_truant_pkmn(self):
        self.battle.opponent.active.ability = "truant"
        split_msg = ["", "move", "p2a: Slaking", "Earthquake"]
        move(self.battle, split_msg)
        self.assertIn("truant", self.battle.opponent.active.volatile_statuses)

    def test_adds_truant_when_slaking_pkmn(self):
        self.battle.opponent.active.name = "slaking"
        split_msg = ["", "move", "p2a: Slaking", "Earthquake"]
        move(self.battle, split_msg)
        self.assertIn("truant", self.battle.opponent.active.volatile_statuses)

    def test_does_not_set_move_for_magicbounce(self):
        split_msg = [
            "",
            "move",
            "p2a: Caterpie",
            "String Shot",
            "[from] ability: Magic Bounce",
        ]

        move(self.battle, split_msg)
        m = Move("String Shot")

        self.assertNotIn(m, self.battle.opponent.active.moves)
        self.assertEqual("magicbounce", self.battle.opponent.active.ability)

    def test_does_not_set_move_for_magicbounce_when_still(self):
        # |move|p2a: Espeon|Leech Seed||[from] ability: Magic Bounce|[still]
        split_msg = [
            "",
            "move",
            "p2a: Caterpie",
            "String Shot",
            "[from]Magic Bounce",
            "[still]",
        ]

        move(self.battle, split_msg)
        m = Move("String Shot")

        self.assertNotIn(m, self.battle.opponent.active.moves)

    def test_new_move_has_one_pp_less_than_max(self):
        split_msg = ["", "move", "p2a: Caterpie", "String Shot"]

        move(self.battle, split_msg)
        m = self.battle.opponent.active.get_move("String Shot")
        expected_pp = m.max_pp - 1

        self.assertEqual(expected_pp, m.current_pp)

    def test_new_move_has_two_pp_less_than_max_if_against_pressure(self):
        split_msg = ["", "move", "p2a: Caterpie", "String Shot"]
        self.battle.user.active.ability = "pressure"

        move(self.battle, split_msg)
        m = self.battle.opponent.active.get_move("String Shot")
        expected_pp = m.max_pp - 2

        self.assertEqual(expected_pp, m.current_pp)

    def test_unknown_move_does_not_try_to_decrement(self):
        split_msg = ["", "move", "p2a: Caterpie", "some-random-unknown-move"]

        move(self.battle, split_msg)

    def test_add_revealed_move_does_not_add_move_twice(self):
        split_msg = ["", "move", "p2a: Caterpie", "String Shot"]

        self.battle.opponent.active.moves.append(Move("String Shot"))
        move(self.battle, split_msg)

        self.assertEqual(1, len(self.battle.opponent.active.moves))

    def test_increments_gen3_consecutive_sleeptalk_turns_when_using_sleeptalk(self):
        split_msg = ["", "move", "p2a: Caterpie", "Earthquake", "[from]Sleep Talk"]
        self.battle.opponent.active.status = constants.SLEEP
        self.battle.generation = "gen3"

        move(self.battle, split_msg)

        self.assertEqual(1, self.battle.opponent.active.gen_3_consecutive_sleep_talks)

    def test_does_not_reset_consecutive_sleeptalk_turns_in_gen3_when_move_is_sleeptalk(
        self,
    ):
        split_msg = ["", "move", "p2a: Caterpie", "Sleep Talk"]
        self.battle.opponent.active.status = constants.SLEEP
        self.battle.opponent.active.gen_3_consecutive_sleep_talks = 1
        self.battle.generation = "gen3"

        move(self.battle, split_msg)

        self.assertEqual(1, self.battle.opponent.active.gen_3_consecutive_sleep_talks)

    def test_resets_consecutive_sleeptalk_turns_in_gen3_when_move_is_non_sleeptalk(
        self,
    ):
        split_msg = ["", "move", "p2a: Caterpie", "Earthquake"]
        self.battle.opponent.active.status = constants.SLEEP
        self.battle.opponent.active.gen_3_consecutive_sleep_talks = 1
        self.battle.generation = "gen3"

        move(self.battle, split_msg)

        self.assertEqual(0, self.battle.opponent.active.gen_3_consecutive_sleep_talks)

    def test_does_not_decrement_pp_if_move_is_called_by_sleeptalk(self):
        split_msg = ["", "move", "p2a: Caterpie", "String Shot", "[from]Sleep Talk"]
        m = Move("String Shot")
        m.current_pp = 5
        self.battle.opponent.active.moves.append(m)
        move(self.battle, split_msg)

        self.assertEqual(5, m.current_pp)

    def test_sets_move_if_doesnt_exist_from_sleeptalk(self):
        split_msg = ["", "move", "p2a: Caterpie", "String Shot", "[from]Sleep Talk"]
        move(self.battle, split_msg)

        self.assertIn(Move("stringshot"), self.battle.opponent.active.moves)
        self.assertEqual(
            self.battle.opponent.active.moves[0].current_pp,
            self.battle.opponent.active.moves[0].max_pp,
        )

    def test_sets_move_if_doesnt_exist_from_move_sleeptalk(self):
        split_msg = [
            "",
            "move",
            "p2a: Caterpie",
            "String Shot",
            "[from]move: Sleep Talk",
        ]
        move(self.battle, split_msg)

        self.assertIn(Move("stringshot"), self.battle.opponent.active.moves)
        self.assertEqual(
            self.battle.opponent.active.moves[0].current_pp,
            self.battle.opponent.active.moves[0].max_pp,
        )

    def test_does_not_decrement_pp_if_move_is_called_by_move_sleeptalk(self):
        split_msg = [
            "",
            "move",
            "p2a: Caterpie",
            "String Shot",
            "[from]move: Sleep Talk",
        ]
        m = Move("String Shot")
        m.current_pp = 5
        self.battle.opponent.active.moves.append(m)
        move(self.battle, split_msg)

        self.assertEqual(5, m.current_pp)

    def test_decrements_seen_move_pp_if_seen_again(self):
        split_msg = ["", "move", "p2a: Caterpie", "String Shot"]
        m = Move("String Shot")
        m.current_pp = 5
        self.battle.opponent.active.moves.append(m)
        move(self.battle, split_msg)

        self.assertEqual(4, m.current_pp)

    def test_decrements_seen_move_pp_by_two_if_opponent_has_pressure(self):
        split_msg = ["", "move", "p2a: Caterpie", "String Shot"]
        m = Move("String Shot")
        m.current_pp = 5
        self.battle.user.active.ability = "pressure"
        self.battle.opponent.active.moves.append(m)
        move(self.battle, split_msg)

        self.assertEqual(3, m.current_pp)

    def test_properly_sets_last_used_move(self):
        split_msg = ["", "move", "p2a: Caterpie", "String Shot"]

        move(self.battle, split_msg)

        expected_last_used_move = LastUsedMove(
            pokemon_name="caterpie", move="stringshot", turn=0
        )

        self.assertEqual(expected_last_used_move, self.battle.opponent.last_used_move)

    def test_using_status_move_makes_assaultvest_impossible(self):
        split_msg = ["", "move", "p2a: Caterpie", "String Shot"]
        self.battle.opponent.last_used_move = LastUsedMove("caterpie", "tackle", 0)

        move(self.battle, split_msg)

        self.assertIn(
            constants.ASSAULT_VEST, self.battle.opponent.active.impossible_items
        )

    def test_using_nonstatus_move_does_not_make_assultvest_impossible(self):
        split_msg = ["", "move", "p2a: Caterpie", "Tackle"]
        self.battle.opponent.last_used_move = LastUsedMove("caterpie", "tackle", 0)

        move(self.battle, split_msg)

        self.assertNotIn(
            constants.ASSAULT_VEST, self.battle.opponent.active.impossible_items
        )

    def test_removes_volatilestatus_if_pkmn_has_it_when_using_move(self):
        self.battle.opponent.active.volatile_statuses = ["phantomforce"]
        split_msg = ["", "move", "p2a: Caterpie", "Phantom Force", "[from] lockedmove"]

        move(self.battle, split_msg)

        self.assertEqual([], self.battle.opponent.active.volatile_statuses)

    def test_does_not_increment_encore_duration_on_move_gen5plus(self):
        # PS ticks encore at end-of-turn (data/moves.ts encore onResidualOrder
        # 16), whether or not the encored mon moved; the engine's gen5+
        # end-of-turn arm consumes the counter the same way
        # (genx/generate_instructions.rs:6752-6805). Move-use must not tick.
        self.battle.generation = "gen9"
        self.battle.opponent.active.volatile_statuses = ["encore"]
        self.battle.opponent.active.volatile_status_durations["encore"] = 0
        split_msg = ["", "move", "p2a: Caterpie", "Tackle"]
        move(self.battle, split_msg)
        self.assertEqual(
            0, self.battle.opponent.active.volatile_status_durations["encore"]
        )

    def test_increments_encore_duration_on_move_pre_gen5(self):
        # legacy pre-gen5 modeling keeps the move-use tick
        self.battle.generation = "gen4"
        self.battle.opponent.active.volatile_statuses = ["encore"]
        self.battle.opponent.active.volatile_status_durations["encore"] = 0
        split_msg = ["", "move", "p2a: Caterpie", "Tackle"]
        move(self.battle, split_msg)
        self.assertEqual(
            1, self.battle.opponent.active.volatile_status_durations["encore"]
        )

    def test_does_not_increment_taunt_duration_on_move_gen5plus(self):
        # PS ticks taunt at end-of-turn (data/moves.ts taunt onResidualOrder
        # 15); gen5+ move-use must not tick (the end-of-turn tick lives in
        # upkeep, matching genx/generate_instructions.rs:6687-6746)
        self.battle.generation = "gen9"
        self.battle.opponent.active.volatile_statuses = [constants.TAUNT]
        self.battle.opponent.active.volatile_status_durations[constants.TAUNT] = 0
        split_msg = ["", "move", "p2a: Caterpie", "Tackle"]
        move(self.battle, split_msg)
        self.assertEqual(
            0, self.battle.opponent.active.volatile_status_durations[constants.TAUNT]
        )

    def test_removes_destinybond_if_it_exists_in_volatiles_when_using_destinybond(self):
        self.battle.opponent.active.volatile_statuses = ["destinybond"]
        split_msg = ["", "move", "p2a: Caterpie", "Destiny Bond"]

        move(self.battle, split_msg)

        self.assertEqual([], self.battle.opponent.active.volatile_statuses)

    def test_removes_destinybond_if_it_exists_in_volatiles_when_not_using_destinybond(
        self,
    ):
        self.battle.opponent.active.volatile_statuses = ["destinybond"]
        split_msg = ["", "move", "p2a: Caterpie", "Tackle"]

        move(self.battle, split_msg)

        self.assertEqual([], self.battle.opponent.active.volatile_statuses)

    def test_sets_can_have_choice_item_to_false_if_two_different_moves_are_used_when_the_pkmn_has_an_unknown_item(
        self,
    ):
        self.battle.opponent.active.can_have_choice_item = True
        split_msg = ["", "move", "p2a: Caterpie", "String Shot"]
        self.battle.opponent.last_used_move = LastUsedMove("caterpie", "tackle", 0)

        move(self.battle, split_msg)

        self.assertFalse(self.battle.opponent.active.can_have_choice_item)

    def test_using_a_boosting_status_move_sets_can_have_choice_item_to_false(self):
        self.battle.opponent.active.can_have_choice_item = True
        split_msg = ["", "move", "p2a: Caterpie", "Dragon Dance"]

        move(self.battle, split_msg)

        self.assertFalse(self.battle.opponent.active.can_have_choice_item)

    def test_using_a_boosting_physical_move_does_not_set_can_have_choice_item_to_false(
        self,
    ):
        self.battle.opponent.active.can_have_choice_item = True
        split_msg = ["", "move", "p2a: Caterpie", "Scale Shot"]

        move(self.battle, split_msg)

        self.assertTrue(self.battle.opponent.active.can_have_choice_item)

    def test_using_a_boosting_special_move_does_not_set_can_have_choice_item_to_false(
        self,
    ):
        self.battle.opponent.active.can_have_choice_item = True
        split_msg = ["", "move", "p2a: Caterpie", "Scale Shot"]

        move(self.battle, split_msg)

        self.assertTrue(self.battle.opponent.active.can_have_choice_item)

    def test_sets_item_to_unknown_if_the_pokemon_choice_item_was_inferred_but_two_different_moves_are_used(
        self,
    ):
        self.battle.opponent.active.can_have_choice_item = True
        self.battle.opponent.active.item = "choiceband"
        self.battle.opponent.active.item_inferred = True
        split_msg = ["", "move", "p2a: Caterpie", "String Shot"]
        self.battle.opponent.last_used_move = LastUsedMove("caterpie", "tackle", 0)
        # the rule is keyed on the CURRENT STAY now, because a Choice lock
        # resets on switch-out -- so the earlier move has to be in
        # moves_used_since_switch_in, not merely in last_used_move
        self.battle.opponent.active.moves_used_since_switch_in.add("tackle")

        move(self.battle, split_msg)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_does_not_set_item_to_unknow_if_choice_item_was_not_inferred_and_two_different_moves_were_used(
        self,
    ):
        self.battle.opponent.active.can_have_choice_item = True
        self.battle.opponent.active.item = "choiceband"
        self.battle.opponent.active.item_inferred = False
        split_msg = ["", "move", "p2a: Caterpie", "String Shot"]
        self.battle.opponent.last_used_move = LastUsedMove("caterpie", "tackle", 0)
        # the rule is keyed on the CURRENT STAY now, because a Choice lock
        # resets on switch-out -- so the earlier move has to be in
        # moves_used_since_switch_in, not merely in last_used_move
        self.battle.opponent.active.moves_used_since_switch_in.add("tackle")

        move(self.battle, split_msg)

        self.assertEqual(constants.CHOICE_BAND, self.battle.opponent.active.item)

    def test_does_not_set_item_to_unknown_if_the_known_item_is_not_a_choice_item_and_two_different_moves_are_used(
        self,
    ):
        self.battle.opponent.active.can_have_choice_item = True
        self.battle.opponent.active.item = "leftovers"
        split_msg = ["", "move", "p2a: Caterpie", "String Shot"]
        self.battle.opponent.last_used_move = LastUsedMove("caterpie", "tackle", 0)

        move(self.battle, split_msg)

        self.assertEqual("leftovers", self.battle.opponent.active.item)

    def test_does_not_set_can_have_choice_item_to_false_if_the_same_move_is_used_when_the_pkmn_has_an_unknown_item(
        self,
    ):
        self.battle.opponent.active.can_have_choice_item = True
        split_msg = ["", "move", "p2a: Caterpie", "Tackle"]
        self.battle.opponent.last_used_move = LastUsedMove("caterpie", "tackle", 0)

        move(self.battle, split_msg)

        self.assertTrue(self.battle.opponent.active.can_have_choice_item)

    def test_sets_can_have_choice_item_to_false_even_if_item_is_known(self):
        # if the item is known - this flag doesn't matter anyways
        self.battle.opponent.active.can_have_choice_item = True
        self.battle.opponent.active.item = "leftovers"
        split_msg = ["", "move", "p2a: Caterpie", "String Shot"]
        self.battle.opponent.last_used_move = LastUsedMove("caterpie", "tackle", 0)

        move(self.battle, split_msg)

        self.assertFalse(self.battle.opponent.active.can_have_choice_item)

    def test_sets_life_orb_as_impossible_if_damaging_move_is_used(self):
        # if a damaging move is used, we no longer want to guess lifeorb as an item
        split_msg = ["", "move", "p2a: Caterpie", "Tackle"]

        move(self.battle, split_msg)

        self.assertIn(constants.LIFE_ORB, self.battle.opponent.active.impossible_items)

    def test_does_not_set_can_life_orb_to_impossible_if_pokemon_could_have_sheerforce(
        self,
    ):
        # mawile could have sheerforce
        # we shouldn't set the lifeorb flag to False because sheerforce doesn't reveal lifeorb when a damaging move is used
        self.battle.opponent.active.name = "mawile"
        split_msg = ["", "move", "p2a: Mawile", "Tackle"]

        move(self.battle, split_msg)

        self.assertNotIn(
            constants.LIFE_ORB, self.battle.opponent.active.impossible_items
        )

    def test_does_not_set_life_orb_to_impossible_if_pokemon_could_have_magic_guard(
        self,
    ):
        # clefable could have magic guard
        # we shouldn't set the lifeorb flag to False because magic guard doesn't reveal lifeorb when a damaging move is used
        self.battle.opponent.active.name = "clefable"
        split_msg = ["", "move", "p2a: Clefable", "Tackle"]

        move(self.battle, split_msg)

        self.assertNotIn(
            constants.LIFE_ORB, self.battle.opponent.active.impossible_items
        )

    def test_adds_normal_gem_to_impossible_items(self):
        split_msg = ["", "move", "p2a: Clefable", "Tackle"]

        move(self.battle, split_msg)
        self.assertIn("normalgem", self.battle.opponent.active.impossible_items)

    def test_adds_flying_gem_to_impossible_items(self):
        split_msg = ["", "move", "p2a: Clefable", "Acrobatics"]

        move(self.battle, split_msg)
        self.assertIn("flyinggem", self.battle.opponent.active.impossible_items)

    def test_does_not_add_gem_if_non_damaging_move(self):
        split_msg = ["", "move", "p2a: Clefable", "Protect"]

        move(self.battle, split_msg)
        self.assertNotIn("normalgem", self.battle.opponent.active.impossible_items)

    def test_wish_sets_battler_wish(self):
        split_msg = ["", "move", "p1a: Clefable", "Wish", "p1a: Clefable"]

        move(self.battle, split_msg)

        expected_wish = (2, self.battle.user.active.max_hp / 2)

        self.assertEqual(expected_wish, self.battle.user.wish)

    def test_failed_wish_does_not_set_wish(self):
        self.battle.user.wish = (1, 100)
        split_msg = ["", "move", "p1a: Clefable", "Wish", "[still]"]

        move(self.battle, split_msg)

        expected_wish = (1, 100)

        self.assertEqual(expected_wish, self.battle.user.wish)

    def test_activating_partially_trapped_gen1(self):
        self.battle.generation = "gen1"
        split_msg = [
            "",
            "move",
            "p1a: Caterpie",
            "Wrap",
            "p2a: Weedle",
        ]
        move(self.battle, split_msg)
        self.assertEqual(
            1,
            self.battle.opponent.active.volatile_status_durations[
                constants.PARTIALLY_TRAPPED
            ],
        )

    def test_does_not_activate_partiallytrapped_on_miss(self):
        self.battle.generation = "gen1"
        split_msg = [
            "",
            "move",
            "p1a: Caterpie",
            "Wrap",
            "p2a: Weedle",
            "[miss]",
        ]
        move(self.battle, split_msg)
        self.assertEqual(
            0,
            self.battle.opponent.active.volatile_status_durations[
                constants.PARTIALLY_TRAPPED
            ],
        )

    def test_removes_existing_partiallytrapped_volatile_after_successfully_using_a_move(
        self,
    ):
        self.battle.generation = "gen1"
        self.battle.opponent.active.volatile_statuses = [
            constants.PARTIALLY_TRAPPED,
        ]
        self.battle.opponent.active.volatile_status_durations[
            constants.PARTIALLY_TRAPPED
        ] = 2
        split_msg = [
            "",
            "move",
            "p2a: Caterpie",
            "Tackle",
            "p1a: Weedle",
        ]
        move(self.battle, split_msg)
        self.assertNotIn(
            constants.PARTIALLY_TRAPPED, self.battle.opponent.active.volatile_statuses
        )
        self.assertEqual(
            0,
            self.battle.opponent.active.volatile_status_durations[
                constants.PARTIALLY_TRAPPED
            ],
        )


class TestTrickRoom(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active

    def test_starts_trickroom_properly(self):
        split_msg = [
            "",
            "-fieldstart",
            "move: Trick Room",
            "p1a: Bronzong",
        ]

        fieldstart(self.battle, split_msg)

        self.assertEqual(True, self.battle.trick_room)
        self.assertEqual(5, self.battle.trick_room_turns_remaining)

    def test_removes_trickroom_properly(self):
        split_msg = [
            "",
            "-fieldend",
            "move: Trick Room",
        ]

        fieldend(self.battle, split_msg)

        self.assertEqual(False, self.battle.trick_room)
        self.assertEqual(0, self.battle.trick_room_turns_remaining)


class TestWeather(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active
        self.user_active = Pokemon("caterpie", 100)
        self.battle.user.active = self.user_active

    def test_starts_weather_properly(self):
        split_msg = [
            "",
            "-weather",
            "RainDance",
            "[from] ability: Drizzle",
            "[of] p2a: Caterpie",
        ]

        weather(self.battle, split_msg)

        self.assertEqual("raindance", self.battle.weather)
        self.assertEqual("opponent:caterpie", self.battle.weather_source)

    def test_sets_weather_turns_remaining_from_ability_gen4(self):
        self.battle.generation = "gen4"
        split_msg = [
            "",
            "-weather",
            "RainDance",
            "[from] ability: Drizzle",
            "[of] p2a: Caterpie",
        ]

        weather(self.battle, split_msg)

        self.assertEqual("raindance", self.battle.weather)
        self.assertEqual(-1, self.battle.weather_turns_remaining)
        self.assertEqual("opponent:caterpie", self.battle.weather_source)

    def test_sets_weather_turns_remaining_from_ability_gen6(self):
        self.battle.generation = "gen6"
        split_msg = [
            "",
            "-weather",
            "RainDance",
            "[from] ability: Drizzle",
            "[of] p2a: Caterpie",
        ]

        weather(self.battle, split_msg)

        self.assertEqual("raindance", self.battle.weather)
        self.assertEqual(5, self.battle.weather_turns_remaining)
        self.assertEqual("opponent:caterpie", self.battle.weather_source)

    def test_sets_weather_turns_remaining_from_user_ability(self):
        self.battle.generation = "gen6"
        split_msg = [
            "",
            "-weather",
            "RainDance",
            "[from] ability: Drizzle",
            "[of] p1a: Weedle",
        ]

        weather(self.battle, split_msg)

        self.assertEqual("raindance", self.battle.weather)
        self.assertEqual(5, self.battle.weather_turns_remaining)
        self.assertEqual("user:caterpie", self.battle.weather_source)

    def test_sets_rain_to_8_turns_from_ability_gen6_with_extension_item(self):
        self.battle.generation = "gen6"
        self.battle.opponent.active.item = "damprock"
        split_msg = [
            "",
            "-weather",
            "RainDance",
            "[from] ability: Drizzle",
            "[of] p2a: Caterpie",
        ]

        weather(self.battle, split_msg)

        self.assertEqual("raindance", self.battle.weather)
        self.assertEqual(8, self.battle.weather_turns_remaining)
        self.assertEqual("opponent:caterpie", self.battle.weather_source)

    def test_sets_sun_to_8_turns_from_ability_gen6_with_extension_item(self):
        self.battle.generation = "gen6"
        self.battle.opponent.active.item = "heatrock"
        split_msg = [
            "",
            "-weather",
            "SunnyDay",
            "[from] ability: Drought",
            "[of] p2a: Caterpie",
        ]

        weather(self.battle, split_msg)

        self.assertEqual("sunnyday", self.battle.weather)
        self.assertEqual(8, self.battle.weather_turns_remaining)
        self.assertEqual("opponent:caterpie", self.battle.weather_source)

    def test_sets_sand_to_8_turns_from_ability_gen6_with_extension_item(self):
        self.battle.generation = "gen6"
        self.battle.opponent.active.item = "smoothrock"
        split_msg = [
            "",
            "-weather",
            "SandStorm",
            "[from] ability: Sand Stream",
            "[of] p2a: Caterpie",
        ]

        weather(self.battle, split_msg)

        self.assertEqual("sandstorm", self.battle.weather)
        self.assertEqual(8, self.battle.weather_turns_remaining)
        self.assertEqual("opponent:caterpie", self.battle.weather_source)

    def test_sets_hail_to_8_turns_from_ability_gen6_with_extension_item(self):
        self.battle.generation = "gen6"
        self.battle.opponent.active.item = "icyrock"
        split_msg = [
            "",
            "-weather",
            "Hail",
            "[from] ability: Snow Warning",
            "[of] p2a: Caterpie",
        ]

        weather(self.battle, split_msg)

        self.assertEqual("hail", self.battle.weather)
        self.assertEqual("opponent:caterpie", self.battle.weather_source)
        self.assertEqual(8, self.battle.weather_turns_remaining)

    def test_sets_weather_turns_remaining_from_move_gen4(self):
        self.battle.generation = "gen4"
        split_msg = [
            "",
            "-weather",
            "RainDance",
        ]

        weather(self.battle, split_msg)

        self.assertEqual("raindance", self.battle.weather)
        self.assertEqual(5, self.battle.weather_turns_remaining)

    def test_decrements_weather(self):
        self.battle.generation = "gen4"
        self.battle.weather = constants.RAIN
        self.battle.weather_turns_remaining = 5
        split_msg = [
            "",
            "-weather",
            "RainDance",
            "[upkeep]",
        ]

        weather(self.battle, split_msg)

        self.assertEqual("raindance", self.battle.weather)
        self.assertEqual(4, self.battle.weather_turns_remaining)

    def test_does_not_decrement_weather_if_set_to_negative_1(self):
        self.battle.generation = "gen4"
        self.battle.weather = constants.RAIN
        self.battle.weather_turns_remaining = -1
        split_msg = [
            "",
            "-weather",
            "RainDance",
            "[upkeep]",
        ]

        weather(self.battle, split_msg)

        self.assertEqual("raindance", self.battle.weather)
        self.assertEqual(-1, self.battle.weather_turns_remaining)

    def test_sets_weather_to_3_when_expecting_0_and_sets_appropriate_extension_item(
        self,
    ):
        self.battle.generation = "gen6"
        self.battle.weather = constants.RAIN
        self.battle.weather_source = "opponent:caterpie"
        self.battle.opponent.active.item = constants.UNKNOWN_ITEM
        self.battle.weather_turns_remaining = 1
        split_msg = [
            "",
            "-weather",
            "RainDance",
            "[upkeep]",
        ]

        weather(self.battle, split_msg)

        self.assertEqual("damprock", self.battle.opponent.active.item)
        self.assertEqual("raindance", self.battle.weather)
        self.assertEqual(3, self.battle.weather_turns_remaining)

    def test_sets_sun_to_3_when_expecting_0_and_sets_appropriate_extension_item(
        self,
    ):
        self.battle.generation = "gen6"
        self.battle.weather = constants.SUN
        self.battle.weather_source = "opponent:caterpie"
        self.battle.opponent.active.item = constants.UNKNOWN_ITEM
        self.battle.weather_turns_remaining = 1
        split_msg = [
            "",
            "-weather",
            "SunnyDay",
            "[upkeep]",
        ]

        weather(self.battle, split_msg)

        self.assertEqual("heatrock", self.battle.opponent.active.item)
        self.assertEqual("sunnyday", self.battle.weather)
        self.assertEqual(3, self.battle.weather_turns_remaining)

    def test_weather_none_clears_weather_to_None_not_the_string_none(self):
        # `|-weather|none` is CLEARED weather.  PS's `field.weather` is `''`
        # there and `effectiveWeather()` returns `''`, so every truthiness test
        # on the weather (Weather Ball's onModifyMove switch,
        # data/moves.ts:20714-20731) must see a falsy value.  Storing the
        # literal "none" made `battle.weather` truthy in clear weather.
        self.battle.weather = constants.RAIN
        self.battle.weather_source = "opponent:caterpie"

        weather(self.battle, ["", "-weather", "none"])

        self.assertIsNone(self.battle.weather)
        self.assertFalse(self.battle.weather)
        self.assertIsNone(self.battle.weather_source)

    def test_weather_none_still_maps_to_the_engine_none_string(self):
        from fp.search.poke_engine_helpers import get_weather_string

        weather(self.battle, ["", "-weather", "none"])

        self.assertEqual("none", get_weather_string(self.battle.weather))

    def test_sets_weather_ability_on_opponent_when_it_is_present(self):
        split_msg = [
            "",
            "-weather",
            "RainDance",
            "[from] ability: Drizzle",
            "[of] p2a: Pelipper",
        ]

        weather(self.battle, split_msg)

        self.assertEqual("drizzle", self.battle.opponent.active.ability)

    def test_sets_weather_ability_on_user_when_it_is_present(self):
        split_msg = [
            "",
            "-weather",
            "RainDance",
            "[from] ability: Drizzle",
            "[of] p1a: Pelipper",
        ]

        weather(self.battle, split_msg)

        self.assertEqual("drizzle", self.battle.user.active.ability)


# |-setboost|p2a: Linoone|atk|6|[from] move: Belly Drum
class TestSetBoost(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.user_active = Pokemon("weedle", 100)
        self.battle.user.active = self.user_active

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active

    def test_set_boost_to_6_from_bellydrum(self):
        split_msg = [
            "",
            "-setboost",
            "p2a: Linoone",
            "atk",
            "6",
            "[from] move: Belly Drum",
        ]
        setboost(self.battle, split_msg)

        expected_boosts = {constants.ATTACK: 6}

        self.assertEqual(expected_boosts, self.battle.opponent.active.boosts)

    def test_set_boost_to_6_even_when_at_negative_from_bellydrum(self):
        self.battle.opponent.active.boosts[constants.ATTACK] = -3
        split_msg = [
            "",
            "-setboost",
            "p2a: Linoone",
            "atk",
            "6",
            "[from] move: Belly Drum",
        ]
        setboost(self.battle, split_msg)

        expected_boosts = {constants.ATTACK: 6}

        self.assertEqual(expected_boosts, self.battle.opponent.active.boosts)


class TestBoostAndUnboost(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.user_active = Pokemon("weedle", 100)
        self.battle.user.active = self.user_active

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active

    def test_opponent_boost_properly_updates_opponent_pokemons_boosts(self):
        split_msg = ["", "boost", "p2a: Weedle", "atk", "1"]
        boost(self.battle, split_msg)

        expected_boosts = {constants.ATTACK: 1}

        self.assertEqual(expected_boosts, self.battle.opponent.active.boosts)

    def test_unboost_works_properly_on_opponent(self):
        split_msg = ["", "boost", "p2a: Weedle", "atk", "1"]
        unboost(self.battle, split_msg)

        expected_boosts = {constants.ATTACK: -1}

        self.assertEqual(expected_boosts, self.battle.opponent.active.boosts)

    def test_unboost_does_not_lower_below_negative_6(self):
        self.battle.opponent.active.boosts[constants.ATTACK] = -6
        split_msg = ["", "unboost", "p2a: Weedle", "atk", "2"]
        unboost(self.battle, split_msg)

        expected_boosts = {constants.ATTACK: -6}

        self.assertEqual(expected_boosts, dict(self.battle.opponent.active.boosts))

    def test_unboost_lowers_one_when_it_hits_the_limit(self):
        self.battle.opponent.active.boosts[constants.ATTACK] = -5
        split_msg = ["", "unboost", "p2a: Weedle", "atk", "2"]
        unboost(self.battle, split_msg)

        expected_boosts = {constants.ATTACK: -6}

        self.assertEqual(expected_boosts, dict(self.battle.opponent.active.boosts))

    def test_boost_does_not_lower_below_negative_6(self):
        self.battle.opponent.active.boosts[constants.ATTACK] = 6
        split_msg = ["", "boost", "p2a: Weedle", "atk", "2"]
        boost(self.battle, split_msg)

        expected_boosts = {constants.ATTACK: 6}

        self.assertEqual(expected_boosts, dict(self.battle.opponent.active.boosts))

    def test_boost_lowers_one_when_it_hits_the_limit(self):
        self.battle.opponent.active.boosts[constants.ATTACK] = 5
        split_msg = ["", "boost", "p2a: Weedle", "atk", "2"]
        boost(self.battle, split_msg)

        expected_boosts = {constants.ATTACK: 6}

        self.assertEqual(expected_boosts, dict(self.battle.opponent.active.boosts))

    def test_unboost_works_properly_on_user(self):
        split_msg = ["", "boost", "p1a: Caterpie", "atk", "1"]
        unboost(self.battle, split_msg)

        expected_boosts = {constants.ATTACK: -1}

        self.assertEqual(expected_boosts, self.battle.user.active.boosts)

    def test_user_boosts_updates_properly(self):
        split_msg = ["", "boost", "p1a: Caterpie", "atk", "1"]
        boost(self.battle, split_msg)

        expected_boosts = {constants.ATTACK: 1}

        self.assertEqual(expected_boosts, self.battle.user.active.boosts)

    def test_multiple_boost_properly_updates(self):
        split_msg = ["", "boost", "p2a: Weedle", "atk", "1"]
        boost(self.battle, split_msg)
        boost(self.battle, split_msg)

        expected_boosts = {constants.ATTACK: 2}

        self.assertEqual(expected_boosts, self.battle.opponent.active.boosts)


class TestStatus(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.battle.opponent.active = Pokemon("caterpie", 100)
        self.battle.user.active = Pokemon("caterpie", 100)

    def test_sets_ability_when_status_comes_from_flamebody(self):
        split_msg = [
            "",
            "-status",
            "p1a: Caterpie",
            "brn",
            "[from] ability: Flame Body",
            "[of] p2a: Caterpie",
        ]
        status(self.battle, split_msg)
        self.assertEqual("flamebody", self.battle.opponent.active.ability)

    def test_sets_ability_when_status_comes_from_effectspore(self):
        split_msg = [
            "",
            "-status",
            "p1a: Caterpie",
            "brn",
            "[from] ability: Effect Spore",
            "[of] p2a: Caterpie",
        ]
        status(self.battle, split_msg)
        self.assertEqual("effectspore", self.battle.opponent.active.ability)

    def test_opponents_active_pokemon_has_status_properly_set(self):
        split_msg = ["", "-status", "p2a: Caterpie", "brn"]
        status(self.battle, split_msg)

        self.assertEqual(self.battle.opponent.active.status, constants.BURN)

    def test_getting_status_causes_lumberry_to_be_an_impossible_item(self):
        split_msg = ["", "-status", "p2a: Caterpie", "brn"]
        status(self.battle, split_msg)

        self.assertIn("lumberry", self.battle.opponent.active.impossible_items)

    def test_rest_turns_set_to_3_on_rest(self):
        split_msg = ["", "-status", "p2a: Caterpie", "slp", "[from] move: Rest"]
        status(self.battle, split_msg)

        self.assertEqual(self.battle.opponent.active.status, constants.SLEEP)
        self.assertEqual(self.battle.opponent.active.rest_turns, 3)

    def test_rest_turns_at_0_and_sleep_turns_at_0_from_nonrest_sleep(self):
        split_msg = ["", "-status", "p2a: Caterpie", "slp", "[from] move: Sleep powder"]
        status(self.battle, split_msg)

        self.assertEqual(self.battle.opponent.active.status, constants.SLEEP)
        self.assertEqual(self.battle.opponent.active.rest_turns, 0)
        self.assertEqual(self.battle.opponent.active.sleep_turns, 0)

    def test_bots_active_pokemon_has_status_properly_set(self):
        split_msg = ["", "-status", "p1a: Caterpie", "brn"]
        status(self.battle, split_msg)

        self.assertEqual(self.battle.user.active.status, constants.BURN)

    def test_status_from_item_properly_sets_that_item(self):
        split_msg = ["", "-status", "p2a: Caterpie", "brn", "[from] item: Flame Orb"]
        status(self.battle, split_msg)

        self.assertEqual(self.battle.opponent.active.item, "flameorb")


class TestCureStatus(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active

        self.opponent_reserve = Pokemon("pikachu", 100)
        self.battle.opponent.reserve = [self.opponent_active, self.opponent_reserve]

        self.battle.user.active = Pokemon("weedle", 100)

    def test_curestatus_resets_toxic_count(self):
        self.battle.opponent.active.status = constants.TOXIC
        self.battle.opponent.side_conditions[constants.TOXIC_COUNT] = 3
        split_msg = ["", "-curestatus", "p2: Caterpie", "tox", "[msg]"]
        curestatus(self.battle, split_msg)

        self.assertEqual(None, self.battle.opponent.active.status)
        self.assertEqual(0, self.battle.opponent.side_conditions[constants.TOXIC_COUNT])

    def test_curestatus_works_on_active_pokemon(self):
        self.opponent_active.status = constants.BURN
        split_msg = ["", "-curestatus", "p2: Caterpie", "brn", "[msg]"]
        curestatus(self.battle, split_msg)

        self.assertEqual(None, self.opponent_active.status)

    def test_curestatus_works_on_active_pokemon_for_bot(self):
        self.battle.user.active.status = constants.BURN
        split_msg = ["", "-curestatus", "p1: Weedle", "brn", "[msg]"]
        curestatus(self.battle, split_msg)

        self.assertEqual(None, self.battle.user.active.status)

    def test_curestatus_works_on_reserve_pokemon(self):
        self.opponent_reserve.status = constants.BURN
        split_msg = ["", "-curestatus", "p2: Pikachu", "brn", "[msg]"]
        curestatus(self.battle, split_msg)

        self.assertEqual(None, self.opponent_reserve.status)

    def test_curestatus_sets_sleep_and_rest_turns_to_0(self):
        self.opponent_reserve.status = constants.SLEEP
        self.opponent_reserve.sleep_turns = 1
        self.opponent_reserve.rest_turns = 1
        split_msg = ["", "-curestatus", "p2: Pikachu", "slp", "[msg]"]
        curestatus(self.battle, split_msg)

        self.assertEqual(0, self.opponent_reserve.sleep_turns)
        self.assertEqual(0, self.opponent_reserve.rest_turns)


class TestStartFutureSight(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active

        self.user_active = Pokemon("weedle", 100)
        self.battle.user.active = self.user_active

    def test_sets_futuresight_on_side_that_used_the_move(self):
        split_msg = ["", "-start", "p2a: Caterpie", "Future Sight"]
        start_volatile_status(self.battle, split_msg)

        self.assertEqual(self.battle.opponent.future_sight, (3, "caterpie"))

    def test_does_not_set_futuresight_as_a_volatilestatus(self):
        split_msg = ["", "-start", "p2a: Caterpie", "Future Sight"]
        self.battle.opponent.active.volatile_statuses = []
        start_volatile_status(self.battle, split_msg)

        self.assertEqual([], self.battle.opponent.active.volatile_statuses)


class TestSetItem(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active

        self.user_active = Pokemon("weedle", 100)
        self.battle.user.active = self.user_active

    def test_sets_remove_item_when_tricked(self):
        split_msg = ["", "-item", "p2a: Caterpie", "Leftovers", "[from] move: Trick"]
        self.battle.opponent.active.item = "choicescarf"
        set_item(self.battle, split_msg)

        self.assertEqual("leftovers", self.battle.opponent.active.item)
        self.assertEqual("choicescarf", self.battle.opponent.active.removed_item)

    def test_does_not_set_removed_item_when_removed_item_already_exists(self):
        split_msg = ["", "-item", "p2a: Caterpie", "Choice Scarf", "[from] move: Trick"]
        self.battle.opponent.active.item = "leftovers"
        self.battle.opponent.active.removed_item = (
            "choicescarf"  # should not be overwritten with leftovers
        )
        set_item(self.battle, split_msg)

        self.assertEqual("choicescarf", self.battle.opponent.active.item)
        self.assertEqual("choicescarf", self.battle.opponent.active.removed_item)

    def test_does_not_set_removed_item_if_unknown(self):
        split_msg = ["", "-item", "p2a: Caterpie", "Choice Scarf", "[from] move: Trick"]
        self.battle.opponent.active.item = constants.UNKNOWN_ITEM
        self.battle.opponent.active.removed_item = None
        set_item(self.battle, split_msg)

        self.assertEqual("choicescarf", self.battle.opponent.active.item)
        self.assertEqual(None, self.battle.opponent.active.removed_item)

    def test_two_trick_protocol_messages_properly_sets_opponents_removed_item(self):
        split_msg_1 = ["", "-item", "p2a: Caterpie", "Leftovers", "[from] move: Trick"]
        split_msg_2 = ["", "-item", "p1a: Weedle", "Choice Specs", "[from] move: Trick"]
        self.battle.opponent.active.item = constants.UNKNOWN_ITEM
        self.battle.opponent.active.removed_item = None
        self.battle.user.active.item = "leftovers"
        self.battle.user.active.removed_item = None
        set_item(self.battle, split_msg_1)
        set_item(self.battle, split_msg_2)

        self.assertEqual("leftovers", self.battle.opponent.active.item)
        self.assertEqual("choicespecs", self.battle.user.active.item)

        self.assertEqual("choicespecs", self.battle.opponent.active.removed_item)

    def test_two_trick_protocol_messages_does_not_overwrite_removed_item_for_opponent(
        self,
    ):
        # trick had already previously swapped items so removed_item is already set for the opponent
        split_msg_1 = [
            "",
            "-item",
            "p2a: Caterpie",
            "Choice Specs",
            "[from] move: Trick",
        ]
        split_msg_2 = ["", "-item", "p1a: Weedle", "Leftovers", "[from] move: Trick"]
        self.battle.opponent.active.item = "leftovers"
        self.battle.opponent.active.removed_item = "choicespecs"
        self.battle.user.active.item = "choicespecs"
        self.battle.user.active.removed_item = None
        set_item(self.battle, split_msg_1)
        set_item(self.battle, split_msg_2)

        self.assertEqual("choicespecs", self.battle.opponent.active.item)
        self.assertEqual("leftovers", self.battle.user.active.item)

        # unchanged because removed_item was already set
        self.assertEqual("choicespecs", self.battle.opponent.active.removed_item)


class TestStartVolatileStatus(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active

        self.user_active = Pokemon("weedle", 100)
        self.battle.user.active = self.user_active

    def test_sets_slowstart_duration_when_slowstart_activates(self):
        # PS slowstart onStart: effectState.counter = 5 (data/abilities.ts),
        # decremented at each residual and ending at 0 - the engine consumes
        # the seed as turns-remaining with the same counter=5 semantics
        split_msg = ["", "-start", "p2a: Caterpie", "Slow Start"]
        start_volatile_status(self.battle, split_msg)

        self.assertEqual(
            5,
            self.battle.opponent.active.volatile_status_durations[constants.SLOW_START],
        )

    def test_volatile_status_is_set_on_opponent_pokemon(self):
        split_msg = ["", "-start", "p2a: Caterpie", "Encore"]
        start_volatile_status(self.battle, split_msg)

        expected_volatile_statuese = ["encore"]

        self.assertEqual(
            expected_volatile_statuese, self.battle.opponent.active.volatile_statuses
        )

    def test_substitute_gets_substitute_hit_flag_set_to_false(self):
        self.battle.user.active.max_hp = 100
        self.battle.user.active.hp = 100

        self.battle.opponent.active.hp = 100
        self.battle.user.active.hp = 100

        messages = [
            "|move|p2a: Pikachu|Substitute|p2a: Pikachu",
            "|-start|p2a: Pikachu|Substitute",
            "|-damage|p2a: Pikachu|75/100",  # damage from sub should not be caught
        ]

        split_msg = messages[1].split("|")

        start_volatile_status(self.battle, split_msg)
        self.assertFalse(self.battle.opponent.active.substitute_hit)

    def test_substitute_gets_shed_tailing_flag_set_to_true(self):
        self.battle.user.active.max_hp = 100
        self.battle.user.active.hp = 100

        self.battle.opponent.active.hp = 100
        self.battle.user.active.hp = 100

        messages = [
            "|move|p1a: Cyclizar|Shed Tail|p1a: Cyclizar",
            "|-start|p1a: Cyclizar|Substitute|[from] move: Shed Tail",
            "|-damage|p1a: Cyclizar|50/100",  # damage from sub should not be caught
        ]

        split_msg = messages[1].split("|")

        start_volatile_status(self.battle, split_msg)
        self.assertTrue(self.battle.user.shed_tailing)

    def test_flashfire_sets_ability_on_opponent(self):
        split_msg = ["", "-start", "p2a: Caterpie", "ability: Flash Fire"]
        start_volatile_status(self.battle, split_msg)

        self.assertEqual("flashfire", self.battle.opponent.active.ability)

    def test_flashfire_sets_ability_on_bot(self):
        split_msg = ["", "-start", "p1a: Caterpie", "ability: Flash Fire"]
        start_volatile_status(self.battle, split_msg)

        self.assertEqual("flashfire", self.battle.user.active.ability)

    def test_volatile_status_is_set_on_user_pokemon(self):
        split_msg = ["", "-start", "p1a: Weedle", "Encore"]
        start_volatile_status(self.battle, split_msg)

        expected_volatile_statuese = ["encore"]

        self.assertEqual(
            expected_volatile_statuese, self.battle.user.active.volatile_statuses
        )

    def test_adds_volatile_status_from_move_string(self):
        split_msg = ["", "-start", "p1a: Weedle", "move: Taunt"]
        start_volatile_status(self.battle, split_msg)

        expected_volatile_statuese = ["taunt"]

        self.assertEqual(
            expected_volatile_statuese, self.battle.user.active.volatile_statuses
        )

    def test_does_not_add_the_same_volatile_status_twice(self):
        self.battle.opponent.active.volatile_statuses = ["encore"]
        split_msg = ["", "-start", "p2a: Caterpie", "Encore"]
        start_volatile_status(self.battle, split_msg)

        expected_volatile_statuese = ["encore"]

        self.assertEqual(
            expected_volatile_statuese, self.battle.opponent.active.volatile_statuses
        )

    def test_doubles_hp_when_dynamax_starts_for_opponent(self):
        split_msg = ["", "-start", "p2a: Caterpie", "Dynamax"]
        hp, maxhp = self.battle.opponent.active.hp, self.battle.opponent.active.max_hp
        start_volatile_status(self.battle, split_msg)

        self.assertEqual(hp * 2, self.battle.opponent.active.hp)
        self.assertEqual(maxhp * 2, self.battle.opponent.active.max_hp)

    def test_doubles_hp_when_dynamax_starts_for_bot(self):
        split_msg = ["", "-start", "p1a: Caterpie", "Dynamax"]
        hp, maxhp = self.battle.user.active.hp, self.battle.user.active.max_hp
        start_volatile_status(self.battle, split_msg)

        self.assertEqual(hp * 2, self.battle.user.active.hp)
        self.assertEqual(maxhp * 2, self.battle.user.active.max_hp)

    def test_terastallize(self):
        split_msg = ["", "-terastallize", "p2a: Caterpie", "Fire"]
        terastallize(self.battle, split_msg)

        self.assertTrue(self.battle.opponent.active.terastallized)

    def test_terastallize_sets_tera_type(self):
        split_msg = ["", "-terastallize", "p2a: Caterpie", "Fire"]
        terastallize(self.battle, split_msg)

        self.assertEqual("fire", self.battle.opponent.active.tera_type)

    def test_sets_ability(self):
        # |-start|p1a: Cinderace|typechange|Fighting|[from] ability: Libero
        split_msg = [
            "",
            "-start",
            "p2a: Cinderace",
            "typechange",
            "Fighting",
            "[from] ability: Libero",
        ]
        start_volatile_status(self.battle, split_msg)

        self.assertEqual("libero", self.battle.opponent.active.ability)

    def test_typechange_starts_volatilestatus(self):
        # |-start|p1a: Cinderace|typechange|Fighting|[from] ability: Libero
        split_msg = [
            "",
            "-start",
            "p2a: Cinderace",
            "typechange",
            "Fighting",
            "[from] ability: Libero",
        ]
        start_volatile_status(self.battle, split_msg)

        self.assertIn(
            constants.TYPECHANGE, self.battle.opponent.active.volatile_statuses
        )

    def test_getting_confused_makes_lumberry_impossible(self):
        split_msg = [
            "",
            "-start",
            "p2a: Cinderace",
            "Confusion",
        ]
        start_volatile_status(self.battle, split_msg)

        self.assertIn("lumberry", self.battle.opponent.active.impossible_items)

    def test_getting_confused_from_fatigue_removes_lockedmove(self):
        self.battle.opponent.active.volatile_statuses.append("lockedmove")
        self.battle.opponent.active.volatile_status_durations[constants.LOCKED_MOVE] = 1
        split_msg = ["", "-start", "p2a: Cinderace", "Confusion", "[fatigue]"]
        start_volatile_status(self.battle, split_msg)

        self.assertNotIn(
            constants.LOCKED_MOVE, self.battle.opponent.active.volatile_statuses
        )
        self.assertEqual(
            0,
            self.battle.opponent.active.volatile_status_durations[
                constants.LOCKED_MOVE
            ],
        )

    def test_typechange_changes_the_type_of_the_user(self):
        # |-start|p1a: Cinderace|typechange|Fighting|[from] ability: Libero
        split_msg = [
            "",
            "-start",
            "p2a: Cinderace",
            "typechange",
            "Fighting",
            "[from] ability: Libero",
        ]
        start_volatile_status(self.battle, split_msg)

        self.assertEqual(["fighting"], self.battle.opponent.active.types)

    def test_typechange_works_with_reflect_type(self):
        # |-start|p1a: Starmie|typechange|[from] move: Reflect Type|[of] p2a: Dragapult
        split_msg = [
            "",
            "-start",
            "p2a: Starmie",
            "typechange",
            "[from] move: Reflect Type",
            "[of] p1a: Dragapult",
        ]
        start_volatile_status(self.battle, split_msg)

        self.assertEqual(["dragon", "ghost"], self.battle.opponent.active.types)

    def test_typechange_from_multiple_types(self):
        # |-start|p2a: Moltres|typechange|???/Flying|[from] move: Burn Up
        split_msg = [
            "",
            "-start",
            "p2a: Moltres",
            "typechange",
            "???/Flying",
            "[from] move: Burn Up",
        ]
        start_volatile_status(self.battle, split_msg)

        self.assertEqual(["???", "flying"], self.battle.opponent.active.types)


class TestTypechangeIsNoOpWhileTerastallized(unittest.TestCase):
    """PS `Pokemon#setType` (sim/pokemon.ts):

        // Terastallized Pokemon cannot have their base type changed
        // except via forme change
        if (this.terastallized) return false;

    Almost every emitter guards on that return value and so never sends the
    line at all while terastallized (Color Change data/abilities.ts:561,
    Protean :3494, Libero :2314, Conversion data/moves.ts:2806, Conversion 2
    :2841, Camouflage :2176, Soak :17193, Magic Powder :10751).  Three do NOT:

      * Reflect Type (data/moves.ts:14899) adds the message BEFORE setType,
      * Burn Up (:2110-2111) and Double Shock (:3963-3964) call setType
        unconditionally and then announce `pokemon.getTypes()`, which for a
        terastallized mon is the TERA type,
      * plus the per-request `[silent]` resync at sim/battle.ts:1714.

    In every one of those PS leaves `pokemon.types` untouched.  Writing the
    announced types onto `pkmn.types` destroys the pre-terastallization types
    that decide 1.5x vs 2.0x tera STAB (damage_membership._base_types).

    Live corpus repro: synthetic-corpus/battle-gen9randombattle-synth17693,
    `|-start|p1a: Pawmot|typechange|Electric|[from] move: Double Shock` on a
    Pawmot that had already terastallized Electric -- fp collapsed Pawmot's
    Electric/Fighting base types to ['electric'] and its Close Combat lost STAB.
    """

    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.opponent.active = Pokemon("pawmot", 80)
        self.battle.user.active = Pokemon("weedle", 100)

    def _tera(self, tera_type="electric"):
        self.battle.opponent.active.terastallized = True
        self.battle.opponent.active.tera_type = tera_type

    def test_double_shock_typechange_does_not_touch_base_types(self):
        # |-start|p2a: Pawmot|typechange|Electric|[from] move: Double Shock
        self._tera()
        before = list(self.battle.opponent.active.types)
        self.assertEqual(["electric", "fighting"], before)
        start_volatile_status(
            self.battle,
            ["", "-start", "p2a: Pawmot", "typechange", "Electric",
             "[from] move: Double Shock"],
        )
        self.assertEqual(before, self.battle.opponent.active.types)

    def test_burn_up_typechange_does_not_touch_base_types(self):
        self.battle.opponent.active = Pokemon("moltres", 80)
        self._tera("fire")
        before = list(self.battle.opponent.active.types)
        start_volatile_status(
            self.battle,
            ["", "-start", "p2a: Moltres", "typechange", "???/Flying",
             "[from] move: Burn Up"],
        )
        self.assertEqual(before, self.battle.opponent.active.types)

    def test_reflect_type_typechange_does_not_touch_base_types(self):
        # Reflect Type adds the message before setType, so PS DOES emit it for a
        # terastallized user -- and still leaves its types alone
        self.battle.opponent.active = Pokemon("starmie", 80)
        self._tera("water")
        before = list(self.battle.opponent.active.types)
        start_volatile_status(
            self.battle,
            ["", "-start", "p2a: Starmie", "typechange",
             "[from] move: Reflect Type", "[of] p1a: Dragapult"],
        )
        self.assertEqual(before, self.battle.opponent.active.types)

    def test_silent_typechange_resync_does_not_touch_base_types(self):
        # |-start|p2a: Cobalion|typechange|Fighting|[silent] on a Cobalion that
        # terastallized Fighting (corpus synth15271:148/417).  sim/battle.ts:1714
        # only refreshes the client's apparentType; it never calls setType.
        self.battle.opponent.active = Pokemon("cobalion", 80)
        self._tera("fighting")
        before = list(self.battle.opponent.active.types)
        self.assertEqual(["steel", "fighting"], before)
        start_volatile_status(
            self.battle,
            ["", "-start", "p2a: Cobalion", "typechange", "Fighting", "[silent]"],
        )
        self.assertEqual(before, self.battle.opponent.active.types)

    def test_typechange_still_applies_when_not_terastallized(self):
        start_volatile_status(
            self.battle,
            ["", "-start", "p2a: Pawmot", "typechange", "Electric",
             "[from] move: Double Shock"],
        )
        self.assertEqual(["electric"], self.battle.opponent.active.types)

    def test_typechange_applies_after_the_mon_stops_being_terastallized(self):
        # the guard reads the live flag, not a sticky one
        self._tera()
        start_volatile_status(
            self.battle,
            ["", "-start", "p2a: Pawmot", "typechange", "Ice"],
        )
        self.assertEqual(["electric", "fighting"], self.battle.opponent.active.types)
        self.battle.opponent.active.terastallized = False
        start_volatile_status(
            self.battle,
            ["", "-start", "p2a: Pawmot", "typechange", "Ice"],
        )
        self.assertEqual(["ice"], self.battle.opponent.active.types)


class TestStartVolatileStatusDisable(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("dragapult", 100)
        for mv in ["dracometeor", "shadowball", "uturn", "thunderbolt"]:
            self.opponent_active.add_move(mv)
        self.battle.opponent.active = self.opponent_active

        self.user_active = Pokemon("sableye", 100)
        self.battle.user.active = self.user_active

    def test_cursed_body_disable_marks_the_named_move_disabled(self):
        split_msg = [
            "",
            "-start",
            "p2a: Dragapult",
            "Disable",
            "Thunderbolt",
            "[from] ability: Cursed Body",
            "[of] p1a: Sableye",
        ]
        start_volatile_status(self.battle, split_msg)

        disabled = {m.name: m.disabled for m in self.opponent_active.moves}
        self.assertTrue(disabled["thunderbolt"])
        self.assertFalse(disabled["dracometeor"])
        self.assertFalse(disabled["shadowball"])
        self.assertFalse(disabled["uturn"])

    def test_disable_seeds_the_disable_duration(self):
        split_msg = ["", "-start", "p2a: Dragapult", "Disable", "Thunderbolt"]
        start_volatile_status(self.battle, split_msg)

        self.assertEqual(
            4,
            self.opponent_active.volatile_status_durations[constants.DISABLE],
        )
        self.assertIn(constants.DISABLE, self.opponent_active.volatile_statuses)

    def test_cursed_body_disable_does_not_set_opponent_ability(self):
        # Cursed Body belongs to the bot's Sableye ([of] p1a), not the disabled
        # opponent - the opponent's ability must stay unknown
        split_msg = [
            "",
            "-start",
            "p2a: Dragapult",
            "Disable",
            "Thunderbolt",
            "[from] ability: Cursed Body",
            "[of] p1a: Sableye",
        ]
        start_volatile_status(self.battle, split_msg)

        self.assertIsNone(self.opponent_active.ability)

    def test_end_disable_re_enables_move_and_clears_duration(self):
        start_split = ["", "-start", "p2a: Dragapult", "Disable", "Thunderbolt"]
        start_volatile_status(self.battle, start_split)

        end_split = ["", "-end", "p2a: Dragapult", "move: Disable"]
        end_volatile_status(self.battle, end_split)

        self.assertFalse(
            [m for m in self.opponent_active.moves if m.name == "thunderbolt"][
                0
            ].disabled
        )
        self.assertEqual(
            0,
            self.opponent_active.volatile_status_durations[constants.DISABLE],
        )
        self.assertNotIn(constants.DISABLE, self.opponent_active.volatile_statuses)


class TestEndVolatileStatus(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active

        self.user_active = Pokemon("weedle", 100)
        self.battle.user.active = self.user_active

    def test_end_fallen_removes_supreme_overlord_volatile(self):
        # PS data/abilities.ts:4727 announces Supreme Overlord's switch-in
        # snapshot as `-start ... fallen{N} [silent]`; :4732 ends it with
        # `-end ... fallen{N} [silent]`.
        self.battle.opponent.active.volatile_statuses = ["fallen3"]
        split_msg = ["", "-end", "p2a: Caterpie", "fallen3", "[silent]"]
        end_volatile_status(self.battle, split_msg)

        self.assertEqual([], self.battle.opponent.active.volatile_statuses)

    def test_end_fallenundefined_is_a_noop_without_warning(self):
        # When Supreme Overlord never activated (0 fainted allies at switch-in)
        # PS's unguarded onEnd template (data/abilities.ts:4731-4733) emits the
        # literal tag `fallenundefined`. There is no tracked volatile to remove
        # and this must not be treated as a desync.
        self.battle.opponent.active.volatile_statuses = []
        split_msg = ["", "-end", "p2a: Caterpie", "fallenundefined", "[silent]"]
        with self.assertNoLogs("fp.battle_modifier", level="WARNING"):
            end_volatile_status(self.battle, split_msg)

        self.assertEqual([], self.battle.opponent.active.volatile_statuses)

    def test_removes_partiallytrapped(self):
        # PS data/conditions.ts partiallytrapped onEnd:
        # `-end ... [partiallytrapped]` when the trap is outlasted. The elapsed
        # count reconstructed in `upkeep` must be zeroed with the volatile or a
        # re-trap seeds the engine with a stale mid-trap count.
        self.battle.opponent.active.volatile_statuses = [constants.PARTIALLY_TRAPPED]
        self.battle.opponent.active.volatile_status_durations[
            constants.PARTIALLY_TRAPPED
        ] = 3
        split_msg = ["", "-end", "p2a: Caterpie", "whirlpool", "[partiallytrapped]"]
        end_volatile_status(self.battle, split_msg)

        self.assertEqual([], self.battle.opponent.active.volatile_statuses)
        self.assertEqual(
            0,
            self.battle.opponent.active.volatile_status_durations[
                constants.PARTIALLY_TRAPPED
            ],
        )

    def test_removes_partiallytrapped_silent(self):
        # PS data/conditions.ts partiallytrapped onResidual emits
        # `-end ... [partiallytrapped] [silent]` when the trapper leaves the
        # field; same branch, so the elapsed count must be zeroed here too.
        self.battle.opponent.active.volatile_statuses = [constants.PARTIALLY_TRAPPED]
        self.battle.opponent.active.volatile_status_durations[
            constants.PARTIALLY_TRAPPED
        ] = 2
        split_msg = [
            "",
            "-end",
            "p2a: Caterpie",
            "whirlpool",
            "[partiallytrapped]",
            "[silent]",
        ]
        end_volatile_status(self.battle, split_msg)

        self.assertEqual([], self.battle.opponent.active.volatile_statuses)
        self.assertEqual(
            0,
            self.battle.opponent.active.volatile_status_durations[
                constants.PARTIALLY_TRAPPED
            ],
        )

    def test_removes_slowstart_volatile_duration(self):
        self.battle.opponent.active.volatile_statuses = ["slowstart"]
        self.battle.opponent.active.volatile_status_durations[constants.SLOW_START] = 1
        split_msg = [
            "",
            "-end",
            "p2a: Caterpie",
            "Slow Start",
            "[silent]",
        ]
        end_volatile_status(self.battle, split_msg)

        self.assertEqual([], self.battle.opponent.active.volatile_statuses)
        self.assertEqual(
            0,
            self.battle.opponent.active.volatile_status_durations[constants.SLOW_START],
        )

    def test_removes_taunt_volatile_duration(self):
        self.battle.opponent.active.volatile_statuses = ["taunt"]
        self.battle.opponent.active.volatile_status_durations[constants.TAUNT] = 1
        split_msg = [
            "",
            "-end",
            "p2a: Caterpie",
            "Taunt",
            "[silent]",
        ]
        end_volatile_status(self.battle, split_msg)

        self.assertEqual([], self.battle.opponent.active.volatile_statuses)
        self.assertEqual(
            0,
            self.battle.opponent.active.volatile_status_durations[constants.TAUNT],
        )

    def test_removes_yawn_volatile_duration(self):
        self.battle.opponent.active.volatile_statuses = ["yawn"]
        self.battle.opponent.active.volatile_status_durations[constants.YAWN] = 1
        split_msg = [
            "",
            "-end",
            "p2a: Caterpie",
            "Yawn",
            "[silent]",
        ]
        end_volatile_status(self.battle, split_msg)

        self.assertEqual([], self.battle.opponent.active.volatile_statuses)
        self.assertEqual(
            0, self.battle.opponent.active.volatile_status_durations[constants.YAWN]
        )

    def test_removes_volatile_status_from_opponent(self):
        self.battle.opponent.active.volatile_statuses = ["encore"]
        split_msg = ["", "-end", "p2a: Caterpie", "Encore"]
        end_volatile_status(self.battle, split_msg)

        expected_volatile_statuses = []

        self.assertEqual(
            expected_volatile_statuses, self.battle.opponent.active.volatile_statuses
        )

    def test_removes_protosynthesisspa_when_protocol_says_protosynthesis(self):
        self.battle.opponent.active.volatile_statuses = ["protosynthesisspa"]
        split_msg = ["", "-end", "p2a: Caterpie", "Protosynthesis"]
        end_volatile_status(self.battle, split_msg)

        expected_volatile_statuses = []

        self.assertEqual(
            expected_volatile_statuses, self.battle.opponent.active.volatile_statuses
        )

    def test_removes_quarkdriveatk_when_protocol_says_quark_drive(self):
        self.battle.opponent.active.volatile_statuses = ["quarkdriveatk"]
        split_msg = ["", "-end", "p2a: Caterpie", "Quark Drive"]
        end_volatile_status(self.battle, split_msg)

        expected_volatile_statuses = []

        self.assertEqual(
            expected_volatile_statuses, self.battle.opponent.active.volatile_statuses
        )

    def test_removes_volatile_status_from_user(self):
        self.battle.user.active.volatile_statuses = ["encore"]
        split_msg = ["", "-end", "p1a: Weedle", "Encore"]
        end_volatile_status(self.battle, split_msg)

        expected_volatile_statuses = []

        self.assertEqual(
            expected_volatile_statuses, self.battle.user.active.volatile_statuses
        )

    def test_halves_opponent_hp_when_dynamax_ends(self):
        self.battle.opponent.active.volatile_statuses = ["dynamax"]
        hp, maxhp = self.battle.opponent.active.hp, self.battle.opponent.active.max_hp
        split_msg = ["", "-end", "p2a: Weedle", "Dynamax"]
        end_volatile_status(self.battle, split_msg)

        self.assertEqual(hp / 2, self.battle.opponent.active.hp)
        self.assertEqual(maxhp / 2, self.battle.opponent.active.max_hp)

    def test_halves_bots_hp_when_dynamax_ends(self):
        self.battle.user.active.volatile_statuses = ["dynamax"]
        hp, maxhp = self.battle.user.active.hp, self.battle.user.active.max_hp
        split_msg = ["", "-end", "p1a: Weedle", "Dynamax"]
        end_volatile_status(self.battle, split_msg)

        self.assertEqual(hp / 2, self.battle.user.active.hp)
        self.assertEqual(maxhp / 2, self.battle.user.active.max_hp)

    def test_ending_substitute_sets_substitute_hit_to_false(self):
        self.battle.opponent.active.substitute_hit = True

        split_msg = ["", "-end", "p2a: Weedle", "Substitute"]
        end_volatile_status(self.battle, split_msg)
        self.assertFalse(self.battle.opponent.active.substitute_hit)

    def test_mismatched_end_zeroes_stale_confusion_duration_after_anim_removal(self):
        # PS emits `|-anim|<mon>|Confusion|<target>` when a mon animates the
        # MOVE named Confusion (e.g. picked by Sleep Talk); the -anim handler
        # removes any volatile matching the animated move's name, so the
        # CONFUSION volatile is dropped without touching its checks-survived
        # counter. The eventual real `|-end|<mon>|confusion` (PS
        # data/conditions.ts confusion onEnd) then lands in the
        # mismatched-volatile warning branch, which must still zero the stale
        # counter or the next confusion is seeded to the engine mid-aged.
        self.battle.opponent.active.volatile_statuses = [constants.CONFUSION]
        self.battle.opponent.active.volatile_status_durations[constants.CONFUSION] = 2

        anim_split_msg = ["", "-anim", "p2a: Caterpie", "Confusion", "p1a: Weedle"]
        anim(self.battle, anim_split_msg)

        # the -anim phantom-removal dropped the volatile but left the counter
        self.assertEqual([], self.battle.opponent.active.volatile_statuses)
        self.assertEqual(
            2,
            self.battle.opponent.active.volatile_status_durations[constants.CONFUSION],
        )

        end_split_msg = ["", "-end", "p2a: Caterpie", "confusion"]
        end_volatile_status(self.battle, end_split_msg)

        self.assertEqual([], self.battle.opponent.active.volatile_statuses)
        self.assertEqual(
            0,
            self.battle.opponent.active.volatile_status_durations[constants.CONFUSION],
        )

    def test_substitute_break_does_not_increment_times_attacked(self):
        # PS does not count the sub-breaking hit toward Rage Fist either: the
        # Substitute's onTryPrimaryHit returns HIT_SUBSTITUTE before the
        # timesAttacked counter runs, so the '-end' line must not increment it
        split_msg = ["", "-end", "p2a: Weedle", "Substitute"]
        end_volatile_status(self.battle, split_msg)
        self.assertEqual(0, self.battle.opponent.active.times_attacked)
        self.assertEqual(0, self.battle.user.active.times_attacked)


class TestUpdateAbility(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active
        self.battle.opponent.active.ability = None

        self.user_active = Pokemon("weedle", 100)
        self.battle.user.active = self.user_active

    def test_sets_as_one_spectrier(self):
        self.battle.opponent.active.name = "calyrexshadow"
        split_msg = ["", "-ability", "p2a: Calyrex", "As One"]
        update_ability(self.battle, split_msg)
        self.assertEqual("asonespectrier", self.battle.opponent.active.ability)

    def test_sets_as_one_glastrier(self):
        self.battle.opponent.active.name = "calyrexice"
        split_msg = ["", "-ability", "p2a: Calyrex", "As One"]
        update_ability(self.battle, split_msg)
        self.assertEqual("asoneglastrier", self.battle.opponent.active.ability)

    def test_does_not_update_asoneglastrier_to_unnerve(self):
        self.battle.opponent.active.name = "calyrexice"
        split_msg = ["", "-ability", "p2a: Calyrex", "As One"]
        update_ability(self.battle, split_msg)
        split_msg = ["", "-ability", "p2a: Calyrex", "Unnerve"]
        update_ability(self.battle, split_msg)
        self.assertEqual("asoneglastrier", self.battle.opponent.active.ability)

    def test_does_not_update_asonespectrier_to_unnerve(self):
        self.battle.opponent.active.name = "calyrexshadow"
        split_msg = ["", "-ability", "p2a: Calyrex", "As One"]
        update_ability(self.battle, split_msg)
        split_msg = ["", "-ability", "p2a: Calyrex", "Unnerve"]
        update_ability(self.battle, split_msg)
        self.assertEqual("asonespectrier", self.battle.opponent.active.ability)

    def test_alternate_set_trace_ability(self):
        # |-ability|p2a: Porygon2|Levitate|Trace|[from] ability: Trace|[of] p1a: Claydol
        self.battle.generation = "gen3"
        self.battle.user.active.ability = "levitate"
        self.battle.opponent.active.ability = None
        split_msg = [
            "",
            "-ability",
            "p2a: Caterpie",
            "Levitate",
            "Trace",
            "[from] ability: Trace",
            "[of] p1a: Caterpie",
        ]
        update_ability(self.battle, split_msg)
        self.assertEqual("levitate", self.battle.opponent.active.ability)
        self.assertEqual("trace", self.battle.opponent.active.original_ability)

    def test_sets_original_ability_from_trace(self):
        self.battle.user.active.ability = "intimidate"
        self.battle.opponent.active.ability = None

        split_msg = [
            "",
            "-ability",
            "p2a: Caterpie",
            "Intimidate",
            "[from] ability: Trace",
            "[of] p1a: Caterpie",
        ]
        update_ability(self.battle, split_msg)

        self.assertEqual("intimidate", self.battle.opponent.active.ability)
        self.assertEqual("trace", self.battle.opponent.active.original_ability)

    def test_sets_original_ability_from_trace_with_intimidate(self):
        self.battle.user.active.ability = "intimidate"
        self.battle.opponent.active.ability = None

        # PS protocol sends 2 `-ability` messages here so just make sure everything is set properly
        split_msg_1 = ["", "-ability", "p2a: Caterpie", "Intimidate", "boost"]
        split_msg_2 = [
            "",
            "-ability",
            "p2a: Caterpie",
            "Intimidate",
            "[from] ability: Trace",
            "[of] p1a: Caterpie",
        ]
        update_ability(self.battle, split_msg_1)
        update_ability(self.battle, split_msg_2)

        self.assertEqual("intimidate", self.battle.opponent.active.ability)
        self.assertEqual("trace", self.battle.opponent.active.original_ability)

    def test_sets_original_ability_from_trace_with_intimidate_for_bot(self):
        self.battle.user.active.ability = "trace"
        self.battle.opponent.active.ability = None

        # PS protocol sends 2 `-ability` messages here so just make sure everything is set properly
        split_msg_1 = ["", "-ability", "p1a: Caterpie", "Intimidate", "boost"]
        split_msg_2 = [
            "",
            "-ability",
            "p1a: Caterpie",
            "Intimidate",
            "[from] ability: Trace",
            "[of] p2a: Caterpie",
        ]
        update_ability(self.battle, split_msg_1)
        update_ability(self.battle, split_msg_2)

        self.assertEqual("intimidate", self.battle.opponent.active.ability)
        self.assertEqual("trace", self.battle.user.active.original_ability)
        self.assertEqual("intimidate", self.battle.user.active.ability)

    def test_update_ability_from_ability_string_properly_updates_ability(self):
        split_msg = ["", "-ability", "p2a: Caterpie", "Lightning Rod", "boost"]
        update_ability(self.battle, split_msg)

        expected_ability = "lightningrod"

        self.assertEqual(expected_ability, self.battle.opponent.active.ability)

    def test_update_ability_from_ability_string_properly_updates_ability_for_bot(self):
        split_msg = ["", "-ability", "p1a: Caterpie", "Lightning Rod", "boost"]
        update_ability(self.battle, split_msg)

        expected_ability = "lightningrod"

        self.assertEqual(expected_ability, self.battle.user.active.ability)


class TestSwapSideConditions(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active
        self.battle.opponent.active.ability = None

        self.user_active = Pokemon("weedle", 100)
        self.battle.user.active = self.user_active

    def get_expected_empty_dict(self):
        # The defaultdict's start empty, but swapping them adds the values of 0 to them
        return {k: 0 for k in constants.COURT_CHANGE_SWAPS}

    def test_does_nothing_when_no_side_conditions_are_present(self):
        split_msg = ["", "-swapsideconditions"]
        swapsideconditions(self.battle, split_msg)

        expected_dict = self.get_expected_empty_dict()

        self.assertEqual(expected_dict, self.battle.user.side_conditions)
        self.assertEqual(expected_dict, self.battle.opponent.side_conditions)

    def test_swaps_one_layer_of_spikes(self):
        split_msg = ["", "-swapsideconditions"]

        self.battle.user.side_conditions[constants.SPIKES] = 1

        swapsideconditions(self.battle, split_msg)

        expected_user_side_conditions = self.get_expected_empty_dict()

        expected_opponent_side_conditions = self.get_expected_empty_dict()
        expected_opponent_side_conditions[constants.SPIKES] = 1

        self.assertEqual(
            expected_user_side_conditions, self.battle.user.side_conditions
        )
        self.assertEqual(
            expected_opponent_side_conditions, self.battle.opponent.side_conditions
        )

    def test_swaps_one_layer_of_spikes_with_two_layers_of_spikes(self):
        split_msg = ["", "-swapsideconditions"]

        self.battle.user.side_conditions[constants.SPIKES] = 2
        self.battle.opponent.side_conditions[constants.SPIKES] = 1

        swapsideconditions(self.battle, split_msg)

        expected_user_side_conditions = self.get_expected_empty_dict()
        expected_user_side_conditions[constants.SPIKES] = 1

        expected_opponent_side_conditions = self.get_expected_empty_dict()
        expected_opponent_side_conditions[constants.SPIKES] = 2

        self.assertEqual(
            expected_user_side_conditions, self.battle.user.side_conditions
        )
        self.assertEqual(
            expected_opponent_side_conditions, self.battle.opponent.side_conditions
        )

    def test_swaps_multiple_side_conditions_on_either_side(self):
        split_msg = ["", "-swapsideconditions"]

        self.battle.user.side_conditions[constants.SPIKES] = 2
        self.battle.user.side_conditions[constants.REFLECT] = 3
        self.battle.user.side_conditions[constants.TAILWIND] = 2

        self.battle.opponent.side_conditions[constants.SPIKES] = 1
        self.battle.opponent.side_conditions[constants.LIGHT_SCREEN] = 2

        swapsideconditions(self.battle, split_msg)

        expected_user_side_conditions = self.get_expected_empty_dict()
        expected_user_side_conditions[constants.SPIKES] = 1
        expected_user_side_conditions[constants.LIGHT_SCREEN] = 2

        expected_opponent_side_conditions = self.get_expected_empty_dict()
        expected_opponent_side_conditions[constants.SPIKES] = 2
        expected_opponent_side_conditions[constants.REFLECT] = 3
        expected_opponent_side_conditions[constants.TAILWIND] = 2

        self.assertEqual(
            expected_user_side_conditions, self.battle.user.side_conditions
        )
        self.assertEqual(
            expected_opponent_side_conditions, self.battle.opponent.side_conditions
        )


class TestIllusionEnd(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active
        self.battle.opponent.active.ability = None

        self.user_active = Pokemon("weedle", 100)
        self.battle.user.active = self.user_active

    def test_zoroark_is_switched_in_pkmn(self):
        self.battle.opponent.active = Pokemon("meloetta", 100)
        self.battle.opponent.reserve = []
        split_msg = ["", "replace", "p2a: Zoroark", "Zoroark, L82, M"]
        illusion_end(self.battle, split_msg)

        self.assertEqual("zoroark", self.battle.opponent.active.name)

    def test_newly_discovered_zoroark_is_marked_revealed(self):
        # the zoroark object is created on the spot when it was not in the
        # reserves, but it was on the field (disguised) so it has been seen
        self.battle.opponent.active = Pokemon("meloetta", 100)
        self.battle.opponent.reserve = []
        split_msg = ["", "replace", "p2a: Zoroark", "Zoroark, L82, M"]
        illusion_end(self.battle, split_msg)

        self.assertEqual("zoroark", self.battle.opponent.active.name)
        self.assertTrue(self.battle.opponent.active.revealed)

    def test_newly_discovered_zoroark_is_marked_illusion_broken(self):
        self.battle.opponent.active = Pokemon("meloetta", 100)
        self.battle.opponent.reserve = []
        split_msg = ["", "replace", "p2a: Zoroark", "Zoroark, L82, M"]
        illusion_end(self.battle, split_msg)

        self.assertEqual("zoroark", self.battle.opponent.active.name)
        self.assertTrue(self.battle.opponent.active.illusion_broken)

    def test_user_side_replace_marks_active_zoroark_illusion_broken(self):
        # the user's own zoroark is always tracked truthfully, so a |replace|
        # on the user's side only needs to stamp the flag on the active pkmn
        self.battle.user.active = Pokemon("zoroark", 100)
        self.assertFalse(self.battle.user.active.illusion_broken)
        split_msg = ["", "replace", "p1a: Zoroark", "Zoroark, L82, M"]
        illusion_end(self.battle, split_msg)

        self.assertTrue(self.battle.user.active.illusion_broken)

    def test_already_discovered_zoroark_replace_marks_illusion_broken(self):
        # even when the zoroark was previously inferred (and is already the
        # active pkmn), the |replace| message means the illusion just broke
        self.battle.opponent.active = Pokemon("zoroark", 100)
        self.battle.opponent.active.zoroark_disguised_as = "meloetta"
        self.battle.opponent.reserve = [Pokemon("meloetta", 100)]
        split_msg = ["", "replace", "p2a: Zoroark", "Zoroark, L84, F"]
        illusion_end(self.battle, split_msg)

        self.assertTrue(self.battle.opponent.active.illusion_broken)

    def test_pkmn_disguised_as_gets_original_hp(self):
        self.battle.opponent.active = Pokemon("meloetta", 100)
        self.battle.opponent.active.hp = 50
        self.battle.opponent.active.max_hp = 100
        self.battle.opponent.active.hp_at_switch_in = 100
        self.battle.opponent.reserve = []
        split_msg = ["", "replace", "p2a: Zoroark", "Zoroark, L82, M"]
        illusion_end(self.battle, split_msg)

        self.assertEqual("meloetta", self.battle.opponent.reserve[0].name)
        self.assertEqual(100, self.battle.opponent.reserve[0].hp)

    def test_pkmn_disguised_as_gets_original_status(self):
        self.battle.opponent.active = Pokemon("meloetta", 100)
        self.battle.opponent.active.status = constants.PARALYZED
        self.battle.opponent.active.status_at_switch_in = None
        self.battle.opponent.reserve = []
        split_msg = ["", "replace", "p2a: Zoroark", "Zoroark, L82, M"]
        illusion_end(self.battle, split_msg)

        self.assertEqual("meloetta", self.battle.opponent.reserve[0].name)
        self.assertIsNone(self.battle.opponent.reserve[0].status)

    def test_revealed_zoroark_inherits_the_volatiles_it_was_carrying(self):
        # everything the disguise accumulated on the field happened to the
        # physical zoroark, so it moves across with the swap (without this the
        # same turn's `-end move: Heal Block` had nothing to end)
        self.battle.opponent.active = Pokemon("meloetta", 100)
        self.battle.opponent.active.volatile_statuses = ["healblock"]
        self.battle.opponent.active.volatile_status_durations["healblock"] = 1
        self.battle.opponent.reserve = []
        illusion_end(self.battle, ["", "replace", "p2a: Zoroark", "Zoroark, L82, M"])

        zoroark = self.battle.opponent.active
        self.assertEqual("zoroark", zoroark.name)
        self.assertEqual(["healblock"], zoroark.volatile_statuses)
        self.assertEqual(1, zoroark.volatile_status_durations["healblock"])

    def test_revealed_zoroark_inherits_the_sleep_counters(self):
        self.battle.opponent.active = Pokemon("meloetta", 100)
        self.battle.opponent.active.status = constants.SLEEP
        self.battle.opponent.active.sleep_turns = 2
        self.battle.opponent.reserve = []
        illusion_end(self.battle, ["", "replace", "p2a: Zoroark", "Zoroark, L82, M"])

        self.assertEqual(constants.SLEEP, self.battle.opponent.active.status)
        self.assertEqual(2, self.battle.opponent.active.sleep_turns)

    def test_disguise_keeps_its_own_sleep_counter(self):
        # the reconstruction keeps ONE object for the disguise species, and it
        # is also the REAL party member of that species: zeroing its sleep
        # counter here erases the real mon's own count (synth29948 T30)
        self.battle.opponent.active = Pokemon("meloetta", 100)
        self.battle.opponent.active.status = constants.SLEEP
        self.battle.opponent.active.status_at_switch_in = constants.SLEEP
        self.battle.opponent.active.sleep_turns = 2
        self.battle.opponent.reserve = []
        illusion_end(self.battle, ["", "replace", "p2a: Zoroark", "Zoroark, L82, M"])

        meloetta = self.battle.opponent.reserve[0]
        self.assertEqual("meloetta", meloetta.name)
        self.assertEqual(2, meloetta.sleep_turns)

    def test_pkmn_disguised_as_returns_to_reserve_with_cleared_volatiles(self):
        # the confusion volatile and its checks-survived counter accrued on the
        # field belong to the disguised zoroark, not to meloetta: when
        # |replace| reveals the zoroark, meloetta must return to reserve with
        # no volatiles and no duration counters, otherwise a later genuine
        # switch-in of meloetta forwards phantom volatiles and stale age
        # counters to the engine
        self.battle.opponent.active = Pokemon("meloetta", 100)
        self.battle.opponent.active.volatile_statuses = [constants.CONFUSION]
        self.battle.opponent.active.volatile_status_durations[constants.CONFUSION] = 2
        self.battle.opponent.reserve = []
        split_msg = ["", "replace", "p2a: Zoroark", "Zoroark, L82, M"]
        illusion_end(self.battle, split_msg)

        self.assertEqual("zoroark", self.battle.opponent.active.name)
        meloetta = self.battle.opponent.reserve[0]
        self.assertEqual("meloetta", meloetta.name)
        self.assertEqual([], meloetta.volatile_statuses)
        self.assertEqual({}, dict(meloetta.volatile_status_durations))
        self.assertEqual(0, meloetta.volatile_status_durations[constants.CONFUSION])

    def test_zoroark_disguising_as_pokemon_results_in_that_pkmn_in_reserve(
        self,
    ):
        """
        Weirdly worded test, but basically:

        If Zoroark was disguised as a previously unseen pkmn, that pkmn should be in the reserve
        Normally theres some fuckery around levels but PokemonShowdown has Illusion Level Mod
        """
        self.battle.opponent.active = Pokemon("meloetta", 100)
        self.battle.opponent.reserve = []
        split_msg = ["", "replace", "p2a: Zoroark", "Zoroark, L82, M"]
        illusion_end(self.battle, split_msg)

        self.assertEqual([Pokemon("meloetta", 100)], self.battle.opponent.reserve)

    def test_moves_used_while_disguised_are_associated_with_zoroark(
        self,
    ):
        """
        zoroark (disguised as meloetta) used focusblast and flamethrower since it switched in
        meloetta previously had hypervoice revealed
        zoroark previously had flamethrower and nastysplot revealed

        illusion ending in this scenario should apply focusblast to zoroark since that is the
        un-revealed zoroark move. meloetta should have focusblast and flamethrower removed
        """
        meloetta = Pokemon("meloetta", 100)
        meloetta.moves = [
            Move("focusblast"),
            Move("flamethrower"),
            Move("hypervoice"),
        ]
        meloetta.moves_used_since_switch_in = ["focusblast", "flamethrower"]
        zoroark = Pokemon("zoroark", 82)
        zoroark.moves = [
            Move("flamethrower"),
            Move("nastyplot"),
        ]
        self.battle.opponent.active = meloetta
        self.battle.opponent.reserve = [zoroark]
        split_msg = ["", "replace", "p2a: Zoroark", "Zoroark, L82, M"]
        illusion_end(self.battle, split_msg)

        self.assertEqual(Pokemon("zoroark", 82), self.battle.opponent.active)
        self.assertEqual([Pokemon("meloetta", 100)], self.battle.opponent.reserve)
        self.assertEqual([Move("hypervoice")], meloetta.moves)
        self.assertEqual(
            [Move("flamethrower"), Move("nastyplot"), Move("focusblast")], zoroark.moves
        )

    def test_moves_used_while_disguised_are_associated_with_previously_nonexistent_zoroark(
        self,
    ):
        meloetta = Pokemon("meloetta", 100)
        meloetta.moves = [
            Move("focusblast"),
            Move("flamethrower"),
            Move("hypervoice"),
        ]
        meloetta.moves_used_since_switch_in = ["focusblast", "flamethrower"]
        self.battle.opponent.active = meloetta
        self.battle.opponent.reserve = []
        split_msg = ["", "replace", "p2a: Zoroark", "Zoroark, L82, M"]
        illusion_end(self.battle, split_msg)

        self.assertEqual(Pokemon("zoroark", 82), self.battle.opponent.active)
        self.assertEqual([Pokemon("meloetta", 100)], self.battle.opponent.reserve)
        self.assertEqual([Move("hypervoice")], meloetta.moves)
        self.assertEqual(
            [Move("focusblast"), Move("flamethrower")],
            self.battle.opponent.active.moves,
        )

    def test_removes_zoroark_from_reserve_if_it_is_in_there(self):
        zoroark = Pokemon("zoroark", 82)
        self.battle.opponent.active = Pokemon("meloetta", 100)
        self.battle.opponent.reserve = [zoroark]
        split_msg = ["", "replace", "p2a: Zoroark", "Zoroark, L82, M"]
        illusion_end(self.battle, split_msg)

        self.assertNotIn(zoroark, self.battle.opponent.reserve)

    def test_does_not_set_base_name_for_illusion_ending(self):
        self.battle.opponent.active = Pokemon("meloetta", 100)
        split_msg = ["", "replace", "p2a: Zoroark", "Zoroark, L84, F"]
        illusion_end(self.battle, split_msg)

        self.assertEqual("zoroark", self.battle.opponent.active.base_name)

    def test_pulls_zoroark_out_of_reserves_if_it_is_in_there(self):
        self.battle.opponent.active = Pokemon("meloetta", 100)
        zoroark = Pokemon("zoroark", 100)
        zoroark.moves = [
            Move("flamethrower"),
            Move("nastyplot"),
            Move("focusblast"),
            Move("darkpulse"),
        ]
        self.battle.opponent.reserve = [zoroark]
        split_msg = ["", "replace", "p2a: Zoroark", "Zoroark, F"]
        illusion_end(self.battle, split_msg)

        self.assertEqual("zoroark", self.battle.opponent.active.base_name)
        self.assertEqual(4, len(self.battle.opponent.active.moves))

    def test_does_nothing_if_zoroark_was_already_active_pkmn(self):
        """
        Logically this seems impossible but the client has places where it tries to infer a
        zoroark based events that happen before the zoroark is revealed. If that was done
        the zoroark would've been set as the active pkmn and the illusion ending event should
        do nothing
        """
        self.battle.opponent.active = Pokemon("zoroark", 100)
        self.battle.opponent.active.zoroark_disguised_as = "meloetta"
        self.battle.opponent.active.moves = [
            Move("flamethrower"),
            Move("nastyplot"),
            Move("focusblast"),
            Move("darkpulse"),
        ]
        self.battle.opponent.reserve = [Pokemon("meloetta", 100)]
        split_msg = ["", "replace", "p2a: Zoroark", "Zoroark, L84, F"]
        illusion_end(self.battle, split_msg)

        self.assertEqual("zoroark", self.battle.opponent.active.base_name)
        self.assertEqual(None, self.battle.opponent.active.zoroark_disguised_as)
        self.assertEqual("meloetta", self.battle.opponent.reserve[0].name)
        self.assertEqual(4, len(self.battle.opponent.active.moves))


class TestLiveIllusionMoveDeattribution(unittest.TestCase):
    """Live (no exact-teams sidecar) purge of moves the disguise cannot own.

    `moves_used_since_switch_in` only remembers the LATEST stay, so a disguise
    that had an earlier disguised stay keeps the bearer's moves forever.
    """

    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.battle_type = BattleType.RANDOM_BATTLE
        self.battle.generation = "gen9"
        RandomBattleTeamDatasets.initialize("gen9randombattle")

        self.battle.user.active = Pokemon("weedle", 100)

        # gyarados' randbats movepool has no poltergeist: it was used by the
        # disguised zoroark during an EARLIER stay
        self.gyarados = Pokemon("gyarados", 79)
        self.gyarados.moves = [Move("waterfall"), Move("poltergeist")]
        self.gyarados.moves_used_since_switch_in = []
        self.battle.opponent.active = self.gyarados
        self.battle.opponent.reserve = []

    def test_earlier_stay_move_is_migrated_to_zoroark(self):
        illusion_end(self.battle, ["", "replace", "p2a: Zoroark", "Zoroark, L82, M"])

        self.assertEqual("zoroark", self.battle.opponent.active.name)
        self.assertEqual([Move("waterfall")], self.gyarados.moves)
        self.assertEqual(
            [Move("poltergeist")],
            self.battle.opponent.active.moves,
        )

    def test_no_purge_when_exact_roster_known(self):
        self.battle.exact_roster_known = True

        illusion_end(self.battle, ["", "replace", "p2a: Zoroark", "Zoroark, L82, M"])

        self.assertEqual("zoroark", self.battle.opponent.active.name)
        self.assertEqual(
            [Move("waterfall"), Move("poltergeist")],
            self.gyarados.moves,
        )
        self.assertEqual([], self.battle.opponent.active.moves)


class TestSwitchActiveWithZoroarkFromReserves(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.user.active = Pokemon("weedle", 100)

    def test_discovered_zoroark_is_marked_revealed_and_illusion_broken(self):
        self.battle.opponent.active = Pokemon("meloetta", 100)
        zoroark = Pokemon("zoroark", 100)
        self.battle.opponent.reserve = [zoroark]

        _switch_active_with_zoroark_from_reserves(self.battle.opponent, zoroark)

        self.assertIs(zoroark, self.battle.opponent.active)
        self.assertTrue(zoroark.revealed)
        self.assertTrue(zoroark.illusion_broken)


class TestFail(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active
        self.battle.opponent.active.ability = None

        self.user_active = Pokemon("weedle", 100)
        self.battle.user.active = self.user_active

    def test_failed_effect_due_to_clearbody_sets_ability(self):
        split_msg = [
            "",
            "-fail",
            "p2a: Caterpie",
            "unboost",
            "[from] ability: Clear Body",
            "[of] p2a: Caterpie",
        ]
        fail(self.battle, split_msg)
        self.assertEqual("clearbody", self.battle.opponent.active.ability)


class TestFormChange(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active
        self.battle.opponent.active.ability = None

        self.user_active = Pokemon("weedle", 100)
        self.battle.user.active = self.user_active

    def test_changes_with_formechange_message(self):
        self.battle.opponent.active = Pokemon("meloetta", 100)
        split_msg = [
            "",
            "-formechange",
            "p2a: Meloetta",
            "Meloetta - Pirouette",
            "[msg]",
        ]
        form_change(self.battle, split_msg)

        self.assertEqual("meloettapirouette", self.battle.opponent.active.name)

    def test_preserves_boosts(self):
        self.battle.opponent.active = Pokemon("meloetta", 100)
        self.battle.opponent.active.boosts = {constants.ATTACK: 2}
        split_msg = [
            "",
            "-formechange",
            "p2a: Meloetta",
            "Meloetta - Pirouette",
            "[msg]",
        ]
        form_change(self.battle, split_msg)

        self.assertEqual(2, self.battle.opponent.active.boosts[constants.ATTACK])

    def test_preserves_status(self):
        self.battle.opponent.active = Pokemon("meloetta", 100)
        self.battle.opponent.active.status = constants.BURN
        split_msg = [
            "",
            "-formechange",
            "p2a: Meloetta",
            "Meloetta - Pirouette",
            "[msg]",
        ]
        form_change(self.battle, split_msg)

        self.assertEqual(constants.BURN, self.battle.opponent.active.status)

    def test_preserves_item(self):
        self.battle.opponent.active = Pokemon("aegislash", 100)
        self.battle.opponent.active.item = "airballoon"
        split_msg = [
            "",
            "-formechange",
            "p2a: Aegislash",
            "Aegislash-Blade",
            "[from] ability: Stance Change",
        ]
        form_change(self.battle, split_msg)

        self.assertEqual("airballoon", self.battle.opponent.active.item)

    def test_preserves_base_name_when_form_changes(self):
        self.battle.opponent.active = Pokemon("meloetta", 100)
        split_msg = [
            "",
            "-formechange",
            "p2a: Meloetta",
            "Meloetta - Pirouette",
            "[msg]",
        ]
        form_change(self.battle, split_msg)

        self.assertEqual("meloetta", self.battle.opponent.active.base_name)

    def test_preserves_nature_on_forme_change(self):
        # Regression: forme_change must recompute stats with the stored nature/EVs
        # (Showdown setSpecies uses the set's actual nature). A Jolly Palafin-Hero must
        # keep its +10% Speed / -10% SpA rather than being reset to a neutral spread.
        palafin = Pokemon("palafin", 100)
        palafin.nature = "jolly"
        self.battle.opponent.active = palafin

        split_msg = ["", "-formechange", "p2a: Palafin", "Palafin-Hero", "[msg]"]
        form_change(self.battle, split_msg)

        hero = self.battle.opponent.active
        self.assertEqual("palafinhero", hero.name)

        neutral = calculate_stats(
            hero.base_stats, hero.level, evs=hero.evs, nature="serious"
        )
        jolly = calculate_stats(
            hero.base_stats, hero.level, evs=hero.evs, nature="jolly"
        )
        # Recomputed stats match the Jolly spread, not the neutral one.
        self.assertEqual(jolly[constants.SPEED], hero.stats[constants.SPEED])
        self.assertEqual(
            jolly[constants.SPECIAL_ATTACK], hero.stats[constants.SPECIAL_ATTACK]
        )
        self.assertGreater(hero.stats[constants.SPEED], neutral[constants.SPEED])
        self.assertLess(
            hero.stats[constants.SPECIAL_ATTACK], neutral[constants.SPECIAL_ATTACK]
        )

    def test_multiple_forme_changes_does_not_ruin_base_name(self):
        self.battle.user.active = Pokemon("pikachu", 100)
        self.battle.opponent.active = Pokemon("pikachu", 100)
        self.battle.opponent.reserve = []
        self.battle.opponent.reserve.append(Pokemon("wishiwashi", 100))

        m1 = ["", "switch", "p2a: Wishiwashi", "Wishiwashi, L100, M", "100/100"]
        m2 = [
            "",
            "-formechange",
            "p2a: Wishiwashi",
            "Wishiwashi-School",
            "",
            "[from] ability: Schooling",
        ]
        m3 = ["", "switch", "p2a: Pikachu", "Pikachu, L100, M", "100/100"]
        m4 = ["", "switch", "p2a: Wishiwashi", "Wishiwashi, L100, M", "100/100"]
        m5 = [
            "",
            "-formechange",
            "p2a: Wishiwashi",
            "Wishiwashi-School",
            "",
            "[from] ability: Schooling",
        ]
        m6 = ["", "switch", "p2a: Pikachu", "Pikachu, L100, M", "100/100"]
        m7 = ["", "switch", "p2a: Wishiwashi", "Wishiwashi, L100, M", "100/100"]
        m8 = [
            "",
            "-formechange",
            "p2a: Wishiwashi",
            "Wishiwashi-School",
            "",
            "[from] ability: Schooling",
        ]

        switch_or_drag(self.battle, m1)
        form_change(self.battle, m2)
        switch_or_drag(self.battle, m3)
        switch_or_drag(self.battle, m4)
        form_change(self.battle, m5)
        switch_or_drag(self.battle, m6)
        switch_or_drag(self.battle, m7)
        form_change(self.battle, m8)

        pkmn = Pokemon("wishiwashischool", 100)
        self.assertNotIn(pkmn, self.battle.opponent.reserve)


class TestClearNegativeBoost(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.user_active = Pokemon("weedle", 100)
        self.battle.user.active = self.user_active

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active

    def test_clears_negative_boosts(self):
        self.battle.opponent.active.boosts = {constants.ATTACK: -1}
        split_msg = ["-clearnegativeboost", "p2a: caterpie", "[silent]"]
        clearnegativeboost(self.battle, split_msg)

        self.assertEqual(0, self.battle.opponent.active.boosts[constants.ATTACK])

    def test_clears_multiple_negative_boosts(self):
        self.battle.opponent.active.boosts = {constants.ATTACK: -1, constants.SPEED: -1}
        split_msg = ["-clearnegativeboost", "p2a: caterpie", "[silent]"]
        clearnegativeboost(self.battle, split_msg)

        self.assertEqual(0, self.battle.opponent.active.boosts[constants.ATTACK])
        self.assertEqual(0, self.battle.opponent.active.boosts[constants.SPEED])

    def test_does_not_clear_positive_boost(self):
        self.battle.opponent.active.boosts = {constants.ATTACK: 1}
        split_msg = ["-clearnegativeboost", "p2a: caterpie", "[silent]"]
        clearnegativeboost(self.battle, split_msg)

        self.assertEqual(1, self.battle.opponent.active.boosts[constants.ATTACK])

    def test_clears_only_negative_boosts(self):
        self.battle.opponent.active.boosts = {
            constants.ATTACK: 1,
            constants.SPECIAL_ATTACK: 1,
            constants.SPEED: 1,
            constants.DEFENSE: -1,
            constants.SPECIAL_DEFENSE: -1,
        }
        split_msg = ["-clearnegativeboost", "p2a: caterpie", "[silent]"]
        clearnegativeboost(self.battle, split_msg)

        expected_boosts = {
            constants.ATTACK: 1,
            constants.SPECIAL_ATTACK: 1,
            constants.SPEED: 1,
            constants.DEFENSE: 0,
            constants.SPECIAL_DEFENSE: 0,
        }

        self.assertEqual(expected_boosts, self.battle.opponent.active.boosts)


class TestClearBoost(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.user_active = Pokemon("weedle", 100)
        self.battle.user.active = self.user_active

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active

    def test_clears_boost(self):
        self.battle.opponent.active.boosts = {constants.ATTACK: 2}
        split_msg = ["-clearboost", "p2a: caterpie", "[silent]"]
        clearboost(self.battle, split_msg)

        self.assertEqual(0, self.battle.opponent.active.boosts[constants.ATTACK])

    def test_clears_multiple_boosts(self):
        self.battle.opponent.active.boosts = {
            constants.ATTACK: 2,
            constants.SPEED: 1,
            constants.SPECIAL_ATTACK: -3,
        }
        split_msg = ["-clearboost", "p2a: caterpie", "[silent]"]
        clearboost(self.battle, split_msg)

        self.assertEqual(0, self.battle.opponent.active.boosts[constants.ATTACK])
        self.assertEqual(
            0, self.battle.opponent.active.boosts[constants.SPECIAL_ATTACK]
        )
        self.assertEqual(0, self.battle.opponent.active.boosts[constants.SPEED])


class TestZPower(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active
        self.battle.opponent.active.ability = None

        self.user_active = Pokemon("weedle", 100)
        self.battle.user.active = self.user_active

        self.username = "CoolUsername"

        self.battle.username = self.username

    def test_sets_item_to_none(self):
        split_msg = ["", "-zpower", "p2a: Pkmn"]
        self.battle.opponent.active.item = "some_item"
        zpower(self.battle, split_msg)

        self.assertEqual(None, self.battle.opponent.active.item)

    def test_does_not_set_item_when_the_bot_moves(self):
        split_msg = ["", "-zpower", "p1a: Pkmn"]
        self.battle.opponent.active.item = "some_item"
        zpower(self.battle, split_msg)

        self.assertEqual("some_item", self.battle.opponent.active.item)


class TestSideStart(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active
        self.battle.opponent.active.ability = None

        self.user_active = Pokemon("weedle", 100)
        self.battle.user.active = self.user_active

        self.username = "CoolUsername"

        self.battle.username = self.username

    def test_stealthrock_gets_1_layer(self):
        split_msg = ["", "-sidestart", "p2", "Stealth Rock"]
        sidestart(self.battle, split_msg)
        self.assertEqual(
            1, self.battle.opponent.side_conditions[constants.STEALTH_ROCK]
        )

    def test_spikes_increments_by_1(self):
        split_msg = ["", "-sidestart", "p2", "Spikes"]
        self.battle.opponent.side_conditions[constants.SPIKES] = 1
        sidestart(self.battle, split_msg)
        self.assertEqual(2, self.battle.opponent.side_conditions[constants.SPIKES])

    def test_reflect_gets_5_turns(self):
        split_msg = ["", "-sidestart", "p2", "Reflect"]
        sidestart(self.battle, split_msg)
        self.assertEqual(5, self.battle.opponent.side_conditions[constants.REFLECT])

    def test_lightscreen_gets_5_turns(self):
        split_msg = ["", "-sidestart", "p2", "move: Light Screen"]
        sidestart(self.battle, split_msg)
        self.assertEqual(
            5, self.battle.opponent.side_conditions[constants.LIGHT_SCREEN]
        )

    def test_lightscreen_gets_8_turns_with_lightclay(self):
        split_msg = ["", "-sidestart", "p2", "move: Light Screen"]
        self.battle.opponent.active.item = "lightclay"
        sidestart(self.battle, split_msg)
        self.assertEqual(
            8, self.battle.opponent.side_conditions[constants.LIGHT_SCREEN]
        )

    def test_auroraveil_gets_8_turns_with_lightclay(self):
        split_msg = ["", "-sidestart", "p2", "move: Aurora Veil"]
        self.battle.opponent.active.item = "lightclay"
        sidestart(self.battle, split_msg)
        self.assertEqual(8, self.battle.opponent.side_conditions[constants.AURORA_VEIL])

    def test_tailwind_gets_4_turns(self):
        split_msg = ["", "-sidestart", "p2", "move: Tail Wind"]
        sidestart(self.battle, split_msg)
        self.assertEqual(4, self.battle.opponent.side_conditions[constants.TAILWIND])


class TestSingleTurn(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active
        self.battle.opponent.active.ability = None

        self.user_active = Pokemon("weedle", 100)
        self.battle.user.active = self.user_active

        self.username = "CoolUsername"

        self.battle.username = self.username

    def test_sets_protect_side_condition_for_opponent_when_used(self):
        split_msg = ["", "-singleturn", "p2a: Caterpie", "Protect"]
        singleturn(self.battle, split_msg)

        self.assertEqual(2, self.battle.opponent.side_conditions[constants.PROTECT])

    def test_sets_protect_side_condition_when_endure_is_used(self):
        split_msg = ["", "-singleturn", "p2a: Caterpie", "Endure"]
        singleturn(self.battle, split_msg)

        self.assertEqual(2, self.battle.opponent.side_conditions[constants.PROTECT])

    def test_does_not_set_for_non_protect_move(self):
        split_msg = ["", "-singleturn", "p2a: Caterpie", "Roost"]
        singleturn(self.battle, split_msg)

        self.assertEqual(0, self.battle.opponent.side_conditions[constants.PROTECT])

    def test_sets_protect_side_condition_for_bot_when_used(self):
        split_msg = ["", "-singleturn", "p1a: Weedle", "Protect"]
        singleturn(self.battle, split_msg)

        self.assertEqual(2, self.battle.user.side_conditions[constants.PROTECT])

    def test_sets_protect_side_condition_when_prefixed_by_move(self):
        split_msg = ["", "-singleturn", "p2a: Caterpie", "move: Protect"]
        singleturn(self.battle, split_msg)

        self.assertEqual(2, self.battle.opponent.side_conditions[constants.PROTECT])


class TestTransform(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("Ditto", 100)
        self.battle.opponent.active = self.opponent_active

        self.user_active = Pokemon("weedle", 100)
        self.battle.user.active = self.user_active

        self.username = "CoolUsername"

        self.battle.username = self.username

        self.user_active_stats = {
            "atk": 103,
            "def": 214,
            "spa": 118,
            "spd": 132,
            "spe": 132,
        }
        self.user_active_ability = "levitate"
        self.user_active_moves = [
            "dracometeor",
            "darkpulse",
            "flashcannon",
            "fireblast",
        ]
        self.request_json = {
            "active": [
                {
                    "moves": [
                        {
                            "move": "Draco Meteor",
                            "id": "dracometeor",
                            "pp": 5,
                            "maxpp": 5,
                            "target": "normal",
                            "disabled": False,
                        },
                        {
                            "move": "Dark Pulse",
                            "id": "darkpulse",
                            "pp": 5,
                            "maxpp": 5,
                            "target": "any",
                            "disabled": False,
                        },
                        {
                            "move": "Flash Cannon",
                            "id": "flashcannon",
                            "pp": 5,
                            "maxpp": 5,
                            "target": "normal",
                            "disabled": False,
                        },
                        {
                            "move": "Fire Blast",
                            "id": "fireblast",
                            "pp": 5,
                            "maxpp": 5,
                            "target": "normal",
                            "disabled": False,
                        },
                    ],
                    "canDynamax": True,
                    "maxMoves": {
                        "maxMoves": [
                            {"move": "maxwyrmwind", "target": "adjacentFoe"},
                            {"move": "maxdarkness", "target": "adjacentFoe"},
                            {"move": "maxsteelspike", "target": "adjacentFoe"},
                            {"move": "maxflare", "target": "adjacentFoe"},
                        ]
                    },
                }
            ],
            "side": {
                "name": "BigBluePikachu",
                "id": "p2",
                "pokemon": [
                    {
                        "ident": "p1: Weedle",
                        "details": "Weedle",
                        "condition": "299/299",
                        "active": True,
                        "stats": self.user_active_stats,
                        "moves": self.user_active_moves,
                        "baseAbility": self.user_active_ability,
                        "item": "choicescarf",
                        "pokeball": "pokeball",
                        "ability": self.user_active_ability,
                    },
                    {
                        "ident": "p1: Charmander",
                        "details": "Charmander",
                        "condition": "299/299",
                        "active": False,
                        "stats": {"atk": 1, "def": 2, "spa": 3, "spd": 4, "spe": 5},
                        "moves": ["flamethrower", "firespin", "scratch", "growl"],
                        "baseAbility": "blaze",
                        "item": "sitrusberry",
                        "pokeball": "pokeball",
                        "ability": "blaze",
                    },
                ],
            },
        }

        self.battle.request_json = self.request_json

    def test_transform_sets_ability_to_opposing_pokemons_ability(self):
        self.battle.user.active.ability = self.user_active_ability
        self.battle.opponent.active.ability = None
        split_msg = [
            "",
            "-transform",
            "p2a: Ditto",
            "p1a: Weedle",
            "[from] ability: Imposter",
        ]

        if self.battle.user.active.ability == self.battle.opponent.active.ability:
            self.fail("Abilities were equal before transform")

        transform(self.battle, split_msg)

        self.assertEqual(self.user_active_ability, self.battle.opponent.active.ability)
        self.assertEqual("imposter", self.battle.opponent.active.original_ability)

    def test_transform_sets_moves_to_opposing_pokemons_moves(self):
        self.battle.user.active.moves = [
            Move("dracometeor"),
            Move("darkpulse"),
            Move("flashcannon"),
            Move("fireblast"),
        ]
        split_msg = [
            "",
            "-transform",
            "p2a: Ditto",
            "p1a: Weedle",
            "[from] ability: Imposter",
        ]

        if self.battle.user.active.moves == self.battle.opponent.active.moves:
            self.fail("Moves were equal before transform")

        transform(self.battle, split_msg)

        self.assertEqual(
            self.battle.user.active.moves, self.battle.opponent.active.moves
        )

    def test_transform_sets_types_to_opposing_pokemons_types(self):
        self.battle.user.active.types = ["flying", "dragon"]
        self.battle.opponent.active.types = ["normal"]
        split_msg = [
            "",
            "-transform",
            "p2a: Ditto",
            "p1a: Weedle",
            "[from] ability: Imposter",
        ]

        transform(self.battle, split_msg)

        self.assertEqual(
            self.battle.user.active.types, self.battle.opponent.active.types
        )

    def test_transform_sets_boosts_to_opposing_pokemons_boosts(self):
        self.battle.user.active.boosts = defaultdict(
            lambda: 0,
            {
                constants.ATTACK: 1,
                constants.DEFENSE: 2,
                constants.SPECIAL_ATTACK: 3,
                constants.SPECIAL_DEFENSE: 4,
                constants.SPEED: 5,
            },
        )
        self.battle.opponent.active.boosts = {}

        split_msg = [
            "",
            "-transform",
            "p2a: Ditto",
            "p1a: Weedle",
            "[from] ability: Imposter",
        ]

        transform(self.battle, split_msg)

        self.assertEqual(
            self.battle.user.active.boosts, self.battle.opponent.active.boosts
        )

    def test_transform_sets_transform_volatile_status(self):
        self.battle.user.active.volatile_statuses = []
        split_msg = [
            "",
            "-transform",
            "p2a: Ditto",
            "p1a: Weedle",
            "[from] ability: Imposter",
        ]

        transform(self.battle, split_msg)

        self.assertIn(
            constants.TRANSFORM, self.battle.opponent.active.volatile_statuses
        )

    def test_transform_sets_volatile_for_bots_side(self):
        self.battle.user.active.volatile_statuses = []
        split_msg = [
            "",
            "-transform",
            "p1a: Weedle",
            "p1a: Weedle",
            "[from] ability: Imposter",
        ]

        transform(self.battle, split_msg)

        self.assertIn(constants.TRANSFORM, self.battle.user.active.volatile_statuses)

    def test_transform_sets_transformed_into_to_target_species(self):
        # transformed_into carries the copied species identity (id/weight/
        # base-types) through to engine conversion
        self.assertIsNone(self.battle.opponent.active.transformed_into)
        split_msg = [
            "",
            "-transform",
            "p2a: Ditto",
            "p1a: Weedle",
            "[from] ability: Imposter",
        ]

        transform(self.battle, split_msg)

        self.assertEqual("weedle", self.battle.opponent.active.transformed_into)

    def test_transform_copies_times_attacked_from_the_target(self):
        # PS sim/pokemon.ts:1314 `this.timesAttacked = pokemon.timesAttacked;` inside
        # transformInto: a COPY, not a merge, so Rage Fist's BP
        # (data/moves.ts:14582-14584, 50 + 50 * timesAttacked) scales off the COPIED
        # mon's hit count and the transformer's own accumulated count is discarded.
        self.battle.opponent.active.times_attacked = 4
        self.battle.user.active.times_attacked = 2
        split_msg = [
            "",
            "-transform",
            "p2a: Ditto",
            "p1a: Weedle",
            "[from] ability: Imposter",
        ]

        transform(self.battle, split_msg)

        self.assertEqual(2, self.battle.opponent.active.times_attacked)
        self.assertEqual(2, self.battle.user.active.times_attacked)

    def test_transform_copies_a_zero_times_attacked_over_a_nonzero_one(self):
        self.battle.opponent.active.times_attacked = 3
        self.battle.user.active.times_attacked = 0
        split_msg = [
            "",
            "-transform",
            "p2a: Ditto",
            "p1a: Weedle",
            "[from] ability: Imposter",
        ]

        transform(self.battle, split_msg)

        self.assertEqual(0, self.battle.opponent.active.times_attacked)


class TestCant(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active

        self.user_active = Pokemon("weedle", 100)
        self.battle.user.active = self.user_active

    def test_increments_sleep_turns_when_cant_from_sleep(self):
        self.battle.user.active.sleep_turns = 0
        self.battle.user.active.status = constants.SLEEP
        cant(self.battle, ["", "-cant", "p1a: Weedle", "slp"])
        self.assertEqual(1, self.battle.user.active.sleep_turns)

    def test_removes_truant_when_cant_from_truant(self):
        self.battle.user.active.sleep_turns = 0
        self.battle.user.active.volatile_statuses.append("truant")
        cant(self.battle, ["", "-cant", "p1a: Slaking", "ability: Truant"])
        self.assertNotIn("truant", self.battle.user.active.volatile_statuses)

    def test_removes_mustrecharge_when_cant_from_recharge(self):
        self.battle.user.active.sleep_turns = 0
        self.battle.user.active.volatile_statuses.append("mustrecharge")
        cant(self.battle, ["", "-cant", "p1a: Slaking", "recharge"])
        self.assertNotIn("mustrecharge", self.battle.user.active.volatile_statuses)

    def test_only_decrements_rest_turns_when_cant_from_sleep_with_a_rest_turn(self):
        self.battle.user.active.sleep_turns = 0
        self.battle.user.active.rest_turns = 3
        self.battle.user.active.status = constants.SLEEP
        cant(self.battle, ["", "-cant", "p1a: Weedle", "slp"])
        self.assertEqual(0, self.battle.user.active.sleep_turns)
        self.assertEqual(2, self.battle.user.active.rest_turns)

    def test_cant_from_sleep_with_rest_turns_1_does_not_exit_the_process(self):
        # b004/g3/20260812T034638 and b000/g13/20260812T065006: a desynced
        # tracker reached this branch and an exit(1) here killed the host
        # worker.  The mon stays asleep at 1; |-curestatus| resolves it.
        self.battle.user.active.sleep_turns = 0
        self.battle.user.active.rest_turns = 1
        self.battle.user.active.status = constants.SLEEP
        cant(self.battle, ["", "-cant", "p1a: Weedle", "slp"])
        self.assertEqual(1, self.battle.user.active.rest_turns)
        self.assertEqual(0, self.battle.user.active.sleep_turns)

    def test_interrupted_two_turn_release_drops_the_charge_volatile(self):
        # PS: twoturnmove.onMoveAborted removes itself (data/conditions.ts:320-322)
        # and its onEnd takes the move-named volatile with it, so a release
        # stopped by full paralysis leaves NO charge behind
        self.battle.user.active.status = constants.PARALYZED
        self.battle.user.active.volatile_statuses.append("meteorbeam")
        cant(self.battle, ["", "-cant", "p1a: Weedle", "par"])
        self.assertNotIn("meteorbeam", self.battle.user.active.volatile_statuses)

    def test_interrupted_two_turn_release_from_flinch_drops_the_charge_volatile(self):
        self.battle.opponent.active.volatile_statuses.append("solarbeam")
        cant(self.battle, ["", "-cant", "p2a: Caterpie", "flinch"])
        self.assertNotIn("solarbeam", self.battle.opponent.active.volatile_statuses)

    def test_cant_leaves_non_charge_volatiles_alone(self):
        self.battle.user.active.status = constants.PARALYZED
        self.battle.user.active.volatile_statuses.append("substitute")
        self.battle.user.active.volatile_statuses.append("leechseed")
        cant(self.battle, ["", "-cant", "p1a: Weedle", "par"])
        self.assertIn("substitute", self.battle.user.active.volatile_statuses)
        self.assertIn("leechseed", self.battle.user.active.volatile_statuses)

    def test_cant_from_nopp_keeps_the_charge_volatile(self):
        # `nopp` is emitted after BeforeMove already succeeded
        # (sim/battle-actions.ts:283-286), so no MoveAborted runs
        self.battle.user.active.volatile_statuses.append("meteorbeam")
        cant(self.battle, ["", "-cant", "p1a: Weedle", "nopp"])
        self.assertIn("meteorbeam", self.battle.user.active.volatile_statuses)

    def test_gen1_pkmn_trapping_foe_releases_target_after_fully_paralyzed(
        self,
    ):
        self.battle.generation = "gen1"
        self.battle.opponent.active.volatile_statuses.append(
            constants.PARTIALLY_TRAPPED
        )
        self.battle.opponent.active.volatile_status_durations[
            constants.PARTIALLY_TRAPPED
        ] = 1
        split_msg = ["", "-cant", "p1a: Rhydon", "par"]
        cant(self.battle, split_msg)
        self.assertNotIn(
            constants.PARTIALLY_TRAPPED, self.battle.opponent.active.volatile_statuses
        )
        self.assertEqual(
            0,
            self.battle.opponent.active.volatile_status_durations[
                constants.PARTIALLY_TRAPPED
            ],
        )


class TestProcessBattleUpdatesErrorHandling(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.user.active = Pokemon("weedle", 100)
        self.battle.opponent.active = Pokemon("caterpie", 100)
        self.battle.generation = "gen9"
        self.battle.request_json = {
            constants.ACTIVE: [{constants.MOVES: []}],
            constants.SIDE: {
                constants.ID: None,
                constants.NAME: None,
                constants.POKEMON: [],
                constants.RQID: None,
            },
        }

    def test_partially_applied_block_is_discarded_not_replayed(self):
        # If a handler raises mid-block and the caller survives (the replay
        # and mask harnesses swallow per-chunk errors), the block must NOT be
        # re-applied on the next |request|: that double-apply is what
        # manufactured the rest_turns==1 + 'cant' state on the mask fleet.
        self.battle.user.active.status = constants.SLEEP
        self.battle.user.active.rest_turns = 3
        self.battle.msg_list = [
            "|cant|p1a: Weedle|slp",
            "|upkeep",
        ]
        with mock.patch(
            "fp.battle_modifier.upkeep", side_effect=ValueError("boom")
        ):
            with self.assertRaises(ValueError):
                process_battle_updates(self.battle)
        self.assertEqual(2, self.battle.user.active.rest_turns)
        self.assertEqual([], self.battle.msg_list)
        process_battle_updates(self.battle)
        self.assertEqual(2, self.battle.user.active.rest_turns)


class TestUpkeep(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active

        self.user_active = Pokemon("weedle", 100)
        self.battle.user.active = self.user_active

    def test_gen3_increments_taunt_duration_end_of_turn(self):
        self.battle.generation = "gen3"
        self.battle.opponent.active.volatile_statuses = [constants.TAUNT]
        self.battle.opponent.active.volatile_status_durations[constants.TAUNT] = 0
        upkeep(self.battle, "")
        self.assertEqual(
            1, self.battle.opponent.active.volatile_status_durations[constants.TAUNT]
        )

    def test_gen5_increments_taunt_duration_end_of_turn(self):
        # gen5+ mirrors PS: taunt decrements at every residual regardless of
        # whether the taunted mon moved (data/moves.ts taunt onResidualOrder
        # 15). The engine counts UP and releases at counter==2
        # (genx/generate_instructions.rs:6687-6746).
        self.battle.generation = "gen5"
        self.battle.opponent.active.volatile_statuses = [constants.TAUNT]
        self.battle.opponent.active.volatile_status_durations[constants.TAUNT] = 0
        upkeep(self.battle, "")
        self.assertEqual(
            1, self.battle.opponent.active.volatile_status_durations[constants.TAUNT]
        )

    def test_gen5_taunt_duration_capped_at_engine_release_counter(self):
        # the engine's arm releases at ==2 and a larger seed would never
        # release (generate_instructions.rs:6710-6746): the root count must
        # never exceed 2 (PS emits the release -end during residuals BEFORE
        # `upkeep`, so the release turn never ticks here)
        self.battle.generation = "gen5"
        self.battle.opponent.active.volatile_statuses = [constants.TAUNT]
        self.battle.opponent.active.volatile_status_durations[constants.TAUNT] = 2
        upkeep(self.battle, "")
        self.assertEqual(
            2, self.battle.opponent.active.volatile_status_durations[constants.TAUNT]
        )

    def test_gen9_increments_encore_duration_end_of_turn(self):
        # PS encore onResidualOrder 16; engine end-of-turn arm releases at
        # counter==2 (generate_instructions.rs:6752-6805)
        self.battle.generation = "gen9"
        self.battle.opponent.active.volatile_statuses = ["encore"]
        self.battle.opponent.active.volatile_status_durations["encore"] = -1
        upkeep(self.battle, "")
        self.assertEqual(
            0, self.battle.opponent.active.volatile_status_durations["encore"]
        )

    def test_gen4_does_not_increment_encore_duration_end_of_turn(self):
        # pre-gen5 keeps the legacy move-use tick; no end-of-turn tick
        self.battle.generation = "gen4"
        self.battle.opponent.active.volatile_statuses = ["encore"]
        self.battle.opponent.active.volatile_status_durations["encore"] = 0
        upkeep(self.battle, "")
        self.assertEqual(
            0, self.battle.opponent.active.volatile_status_durations["encore"]
        )

    def test_decrements_slowstart_volatile_duration(self):
        # PS slowstart onResidual is gated on pokemon.activeTurns
        # (data/abilities.ts:4309): a mon that has been out for at least one
        # |turn| boundary (active_turns >= 1) ticks at every upkeep
        self.battle.user.active.active_turns = 1
        self.battle.user.active.volatile_statuses.append(constants.SLOW_START)
        self.battle.user.active.volatile_status_durations[constants.SLOW_START] = 5
        upkeep(self.battle, "")
        self.assertEqual(
            4,
            self.battle.user.active.volatile_status_durations[constants.SLOW_START],
        )

    def test_slowstart_skips_decrement_on_entry_turn_upkeep(self):
        # PS slowstart onResidual: `if (pokemon.activeTurns && ...)`
        # (data/abilities.ts:4308-4315). switchIn zeroes activeTurns
        # (sim/battle-actions.ts:137) and nextTurn increments it AFTER the
        # residual phase (sim/battle.ts:1762), so the counter does NOT tick at
        # the upkeep of the turn the mon entered on.
        self.battle.user.active.active_turns = 0
        self.battle.user.active.volatile_statuses.append(constants.SLOW_START)
        self.battle.user.active.volatile_status_durations[constants.SLOW_START] = 5
        upkeep(self.battle, "")
        self.assertEqual(
            5,
            self.battle.user.active.volatile_status_durations[constants.SLOW_START],
        )

    def test_slowstart_lead_entry_ticks_at_first_upkeep(self):
        # Lead timing (synth02212: lead Regigigas -start precedes |turn|1):
        # the lead enters before |turn|1, nextTurn increments activeTurns to 1
        # while emitting the |turn| line (sim/battle.ts:1762/1782), so turn 1's
        # upkeep DOES tick: 5 -> 4, with the -end at turn 5's residual.
        switch_or_drag(
            self.battle, ["", "switch", "p2a: Regigigas", "Regigigas, L84", "100/100"]
        )
        start_volatile_status(
            self.battle, ["", "-start", "p2a: Regigigas", "ability: Slow Start"]
        )
        self.assertEqual(0, self.battle.opponent.active.active_turns)
        turn(self.battle, ["", "turn", "1"])
        upkeep(self.battle, "")
        self.assertEqual(
            4,
            self.battle.opponent.active.volatile_status_durations[constants.SLOW_START],
        )

    def test_slowstart_mid_battle_switch_skips_entry_turn_then_ticks(self):
        # Mid-turn switch timing (synth00046: Regigigas switches in DURING
        # turn 1, -end at turn 6): the entry turn's upkeep is skipped
        # (activeTurns still 0 during that residual), the first tick comes at
        # the next turn's upkeep.
        self.battle.turn = 1
        switch_or_drag(
            self.battle, ["", "switch", "p2a: Regigigas", "Regigigas, L84", "100/100"]
        )
        start_volatile_status(
            self.battle, ["", "-start", "p2a: Regigigas", "ability: Slow Start"]
        )
        upkeep(self.battle, "")  # entry turn's upkeep: no tick
        self.assertEqual(
            5,
            self.battle.opponent.active.volatile_status_durations[constants.SLOW_START],
        )
        turn(self.battle, ["", "turn", "2"])
        upkeep(self.battle, "")  # first full turn on the field: tick
        self.assertEqual(
            4,
            self.battle.opponent.active.volatile_status_durations[constants.SLOW_START],
        )

    def test_slowstart_faint_replacement_ticks_first_at_next_turn_upkeep(self):
        # Post-upkeep faint-replacement timing: the replacement enters AFTER
        # turn N's upkeep line (PS runs the faint turn's residuals before the
        # replacement switches in), |turn|N+1 increments its activeTurns to 1,
        # so its first tick is turn N+1's upkeep — same 5-upkeep window as a
        # lead, shifted to its entry point.
        self.battle.turn = 9
        # turn 9's upkeep already ran; replacement enters now
        switch_or_drag(
            self.battle, ["", "switch", "p2a: Regigigas", "Regigigas, L84", "100/100"]
        )
        start_volatile_status(
            self.battle, ["", "-start", "p2a: Regigigas", "ability: Slow Start"]
        )
        self.assertEqual(0, self.battle.opponent.active.active_turns)
        turn(self.battle, ["", "turn", "10"])
        upkeep(self.battle, "")
        self.assertEqual(
            4,
            self.battle.opponent.active.volatile_status_durations[constants.SLOW_START],
        )

    def test_decrements_disable_volatile_duration(self):
        # PS ticks effect durations down at the end of every turn
        # (sim/battle.ts:516 `handler.state.duration--`); the engine reads the
        # seeded value as turns-remaining, so the root value must shrink each
        # completed real turn
        self.battle.opponent.active.volatile_statuses.append(constants.DISABLE)
        self.battle.opponent.active.volatile_status_durations[constants.DISABLE] = 4
        upkeep(self.battle, "")
        self.assertEqual(
            3,
            self.battle.opponent.active.volatile_status_durations[constants.DISABLE],
        )

    def test_disable_duration_does_not_go_below_zero(self):
        # PS can hold the volatile one turn longer than the 4-turn seed
        # (data/moves.ts:3664-3674: duration 5 when the target already moved);
        # the timer floors at 0 until |-end|Disable arrives
        self.battle.opponent.active.volatile_statuses.append(constants.DISABLE)
        self.battle.opponent.active.volatile_status_durations[constants.DISABLE] = 0
        upkeep(self.battle, "")
        self.assertEqual(
            0,
            self.battle.opponent.active.volatile_status_durations[constants.DISABLE],
        )

    def test_no_disable_volatile_means_no_tick(self):
        self.battle.opponent.active.volatile_status_durations[constants.DISABLE] = 0
        upkeep(self.battle, "")
        self.assertEqual(
            0,
            self.battle.opponent.active.volatile_status_durations[constants.DISABLE],
        )

    def test_increments_lockedmove_end_of_turn(self):
        self.battle.opponent.active.volatile_statuses.append(constants.LOCKED_MOVE)
        self.battle.opponent.active.volatile_status_durations[constants.LOCKED_MOVE] = 0
        upkeep(self.battle, "")
        self.assertEqual(
            1,
            self.battle.opponent.active.volatile_status_durations[
                constants.LOCKED_MOVE
            ],
        )

    def test_decrements_reflect_end_of_turn(self):
        self.battle.opponent.side_conditions[constants.REFLECT] = 5
        upkeep(self.battle, "")
        self.assertEqual(4, self.battle.opponent.side_conditions[constants.REFLECT])

    def test_decrementing_reflect_to_0_extends_by_3(self):
        self.battle.opponent.side_conditions[constants.REFLECT] = 1
        upkeep(self.battle, "")
        self.assertEqual(3, self.battle.opponent.side_conditions[constants.REFLECT])

    def test_decrements_lightscreen_end_of_turn(self):
        self.battle.opponent.side_conditions[constants.LIGHT_SCREEN] = 5
        upkeep(self.battle, "")
        self.assertEqual(
            4, self.battle.opponent.side_conditions[constants.LIGHT_SCREEN]
        )

    def test_decrementing_lightscreen_to_0_extends_by_3(self):
        self.battle.opponent.side_conditions[constants.LIGHT_SCREEN] = 1
        upkeep(self.battle, "")
        self.assertEqual(
            3, self.battle.opponent.side_conditions[constants.LIGHT_SCREEN]
        )

    def test_decrements_auroraveil_end_of_turn(self):
        self.battle.opponent.side_conditions[constants.AURORA_VEIL] = 5
        upkeep(self.battle, "")
        self.assertEqual(4, self.battle.opponent.side_conditions[constants.AURORA_VEIL])

    def test_decrementing_auroraveil_to_0_extends_by_3(self):
        self.battle.opponent.side_conditions[constants.AURORA_VEIL] = 1
        upkeep(self.battle, "")
        self.assertEqual(3, self.battle.opponent.side_conditions[constants.AURORA_VEIL])

    def test_decrements_tailwind_end_of_turn(self):
        self.battle.opponent.side_conditions[constants.TAILWIND] = 2
        upkeep(self.battle, "")
        self.assertEqual(1, self.battle.opponent.side_conditions[constants.TAILWIND])

    def test_field_turns_remaining_is_decremented(self):
        self.battle.field_turns_remaining = 5
        self.battle.field = constants.GRASSY_TERRAIN
        upkeep(self.battle, "")
        self.assertEqual(4, self.battle.field_turns_remaining)

    def test_0_turns_remaining_field_sets_turns_remaining_to_3(self):
        self.battle.field_turns_remaining = 1
        self.battle.field = constants.GRASSY_TERRAIN
        upkeep(self.battle, "")
        self.assertEqual(3, self.battle.field_turns_remaining)

    def test_none_field_does_not_change_field_or_turns_remaining(self):
        self.battle.field_turns_remaining = 0
        self.battle.field = None
        upkeep(self.battle, "")
        self.assertEqual(0, self.battle.field_turns_remaining)

    def test_resets_sleep_turns_to_zero_after_not_using_sleeptalk(self):
        self.battle.generation = "gen3"
        self.battle.user.active.status = constants.SLEEP
        self.battle.user.active.gen_3_consecutive_sleep_talks = 1

        cant(self.battle, ["", "-cant", "p1a: Weedle", "slp"])
        upkeep(self.battle, "")

        self.assertEqual(0, self.battle.user.active.gen_3_consecutive_sleep_talks)

    def test_does_not_reset_sleep_turns_when_sleeptalk_used(self):
        self.battle.generation = "gen3"
        self.battle.user.active.status = constants.SLEEP
        self.battle.user.active.gen_3_consecutive_sleep_talks = 1

        cant(self.battle, ["", "-cant", "p1a: Weedle", "slp"])
        move(self.battle, ["", "move", "p1a: Weedle", "Sleeptalk"])
        move(self.battle, ["", "move", "p1a: Weedle", "Tackle", "[from]Sleep Talk"])
        upkeep(self.battle, "")

        self.assertEqual(2, self.battle.user.active.gen_3_consecutive_sleep_talks)
        self.assertEqual("sleeptalk", self.battle.user.last_used_move.move)

    def test_increments_yawn_duration(self):
        self.battle.user.active.volatile_statuses.append(constants.YAWN)
        upkeep(self.battle, "")
        self.assertEqual(
            1, self.battle.user.active.volatile_status_durations[constants.YAWN]
        )

    def test_decrements_trickroom_in_upkeep(self):
        self.battle.trick_room = True
        self.battle.trick_room_turns_remaining = 5
        upkeep(self.battle, "")
        self.assertEqual(4, self.battle.trick_room_turns_remaining)

    def test_swaps_out_yawn_for_yawnSleepThisTurn_opponent(self):
        self.battle.opponent.active.volatile_statuses.append(constants.YAWN)
        self.battle.opponent.active.volatile_status_durations[constants.YAWN] = 0
        upkeep(self.battle, "")
        self.assertIn(
            constants.YAWN,
            self.battle.opponent.active.volatile_statuses,
        )
        self.assertEqual(
            1, self.battle.opponent.active.volatile_status_durations[constants.YAWN]
        )

    def test_removes_yawnSleepNextTurn(self):
        self.battle.user.active.volatile_statuses.append(constants.YAWN)
        self.battle.user.active.volatile_status_durations[constants.YAWN] = 1
        upkeep(self.battle, "")
        self.assertEqual(
            0, self.battle.user.active.volatile_status_durations[constants.YAWN]
        )
        self.assertNotIn(constants.YAWN, self.battle.user.active.volatile_statuses)

    def test_reduces_protect_for_bot(self):
        self.battle.user.side_conditions[constants.PROTECT] = 1

        upkeep(self.battle, "")

        self.assertEqual(self.battle.user.side_conditions[constants.PROTECT], 0)

    def test_does_not_reduce_protect_when_it_is_0(self):
        self.battle.user.side_conditions[constants.PROTECT] = 0

        upkeep(self.battle, "")

        self.assertEqual(self.battle.user.side_conditions[constants.PROTECT], 0)

    def test_reduces_wish_if_it_is_larger_than_0_for_the_opponent(self):
        self.battle.opponent.wish = (2, 100)

        upkeep(self.battle, "")

        self.assertEqual(self.battle.opponent.wish, (1, 100))

    def test_reduces_wish_if_it_is_larger_than_0_for_the_bot(self):
        self.battle.user.wish = (2, 100)

        upkeep(self.battle, "")

        self.assertEqual(self.battle.user.wish, (1, 100))

    def test_does_not_reduce_wish_if_it_is_0(self):
        self.battle.user.wish = (0, 100)

        upkeep(self.battle, "")

        self.assertEqual(self.battle.user.wish, (0, 100))

    def test_reduces_future_sight_if_it_is_larger_than_0_for_the_bot(self):
        self.battle.user.future_sight = (2, "pokemon_name")

        upkeep(self.battle, "")

        self.assertEqual(self.battle.user.future_sight, (1, "pokemon_name"))

    def test_does_not_reduce_future_sight_if_it_is_0(self):
        self.battle.user.future_sight = (0, "pokemon_name")

        upkeep(self.battle, "")

        self.assertEqual(self.battle.user.future_sight, (0, "pokemon_name"))

    def test_adds_leftovers_blacksludge_to_impossible_items_at_end_of_turn(self):
        # RESIDUAL-OBSERVATION CONTRACT (Sally 2026-08-20). The rule no longer
        # differences two upkeeps -- residual effects resolve IN ORDER, and
        # Leftovers (order 5) fires before burn (10) / Leech Seed (8), so a mon
        # that ENTERED the residual at full HP and was chipped afterwards ends
        # below max having "not healed" without that proving anything. What it
        # now asks is: was the mon below max when the residual OPENED, and did
        # an item heal appear? `_pre_residual_hp` is stamped on the bare `|`
        # separator by _process_battle_updates.
        self.battle.opponent.active.hp = 50
        self.battle.opponent.active._pre_residual_hp = 50
        upkeep(self.battle, "")
        self.assertIn(constants.LEFTOVERS, self.battle.opponent.active.impossible_items)
        self.assertIn(
            constants.BLACK_SLUDGE, self.battle.opponent.active.impossible_items
        )

    def test_single_upkeep_below_maxhp_does_not_rule_out_leftovers(self):
        """Regression: this false elimination hid the opponent's TRUE set.

        A mon that switched into hazards is below max HP at its first upkeep and
        will still heal at the residual step. The old rule fired immediately and
        excluded every Leftovers set -- measured as the dominant `match_item`
        support gap (SPEC-B4 worked example: Floatzel, true item Leftovers,
        already in impossible_items).
        """
        self.battle.opponent.active.hp = 50
        upkeep(self.battle, "")
        self.assertNotIn(
            constants.LEFTOVERS, self.battle.opponent.active.impossible_items
        )

    def test_hp_gain_between_upkeeps_never_rules_out_leftovers(self):
        """If HP went UP across the turn boundary, Leftovers is consistent."""
        self.battle.opponent.active.hp = 50
        upkeep(self.battle, "")
        self.battle.opponent.active.hp = 62  # healed
        upkeep(self.battle, "")
        self.assertNotIn(
            constants.LEFTOVERS, self.battle.opponent.active.impossible_items
        )

    def test_adds_flameorb_toxicorb_if_status_is_none_at_end_of_turn(self):
        self.battle.opponent.active.status = None
        upkeep(self.battle, "")
        self.assertIn("flameorb", self.battle.opponent.active.impossible_items)
        self.assertIn("toxicorb", self.battle.opponent.active.impossible_items)

    def test_does_not_add_flameorb_toxicorb_if_status_exists_at_end_of_turn(self):
        self.battle.opponent.active.status = constants.FROZEN
        upkeep(self.battle, "")
        self.assertNotIn("flameorb", self.battle.opponent.active.impossible_items)
        self.assertNotIn("toxicorb", self.battle.opponent.active.impossible_items)


class TestCheckSpeedRanges(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active
        self.battle.opponent.active.ability = None

        self.user_active = Pokemon("caterpie", 100)
        self.battle.user.active = self.user_active

        self.username = "CoolUsername"

        self.battle.username = self.username

        self.battle.request_json = {
            constants.ACTIVE: [{constants.MOVES: []}],
            constants.SIDE: {
                constants.ID: None,
                constants.NAME: None,
                constants.POKEMON: [],
                constants.RQID: None,
            },
        }

    def test_protosynthesis_speed_is_accounted_for_in_speed_range_check(self):
        self.battle.user.active.stats[constants.SPEED] = 300
        self.battle.user.active.boosts[constants.SPEED] = 1
        self.battle.user.last_selected_move = LastUsedMove("caterpie", "tackle", 0)

        self.battle.opponent.active.stats[constants.SPEED] = 370
        self.battle.opponent.active.volatile_statuses.append("protosynthesisspe")

        messages = [
            "|move|p2a: Pikachu|U-turn|p1a: Caterpie",
            "|-damage|p1a: Caterpie|0 fnt",
            "|faint|p2a: Caterpie",
        ]
        check_speed_ranges(self.battle, messages)
        self.assertEqual(300, self.battle.opponent.active.speed_range.min)  # unchanged

    def test_revive_prompt_switch_selection_makes_this_check_not_happen(self):
        # After Revival Blessing, the revive target is answered as a "switch"
        # selection but no |switch| line is emitted (the mon is healed in the
        # party). This must not be looked up as a move (used to KeyError)
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.opponent.active.stats[constants.SPEED] = 100
        self.battle.user.last_selected_move = LastUsedMove(
            "pawmot", "switch shaymin", 0
        )

        messages = [
            "|move|p2a: Pikachu|Tackle|p1a: Caterpie",
            "|-damage|p1a: Caterpie|1/100",
            "|upkeep",
            "|turn|7",
        ]
        check_speed_ranges(self.battle, messages)
        self.assertEqual(0, self.battle.opponent.active.speed_range.min)  # unchanged

    def test_recharging_makes_this_check_not_happen(self):
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.opponent.active.stats[constants.SPEED] = 100
        self.battle.user.last_selected_move = LastUsedMove("caterpie", "agility", 0)

        messages = [
            "|cant|p1a: Caterpie|recharge",
            "|move|p2a: Pikachu|Tackle|p1a: Caterpie",
            "|-damage|p1a: Caterpie|1/100",
            "|upkeep",
            "|turn|7",
        ]
        check_speed_ranges(self.battle, messages)
        self.assertEqual(0, self.battle.opponent.active.speed_range.min)  # unchanged

    def test_hit_self_in_confusion_makes_this_check_not_happen(self):
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.opponent.active.stats[constants.SPEED] = 100
        self.battle.user.last_selected_move = LastUsedMove("caterpie", "agility", 0)

        messages = [
            "|-activate|p1a: Caterpie|confusion",
            "|-damage|p1a: Caterpie|15/100|[from] confusion",
            "|move|p2a: Pikachu|Tackle|p1a: Caterpie",
            "|-damage|p1a: Caterpie|1/100",
            "|upkeep",
            "|turn|7",
        ]
        check_speed_ranges(self.battle, messages)
        self.assertEqual(0, self.battle.opponent.active.speed_range.min)  # unchanged

    def test_boosting_speed_after_opponent_does_not_mess_up_speed_range_check(self):
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.opponent.active.stats[constants.SPEED] = 100
        self.battle.user.last_selected_move = LastUsedMove("caterpie", "agility", 0)

        messages = [
            "|move|p2a: Pikachu|Tackle|p1a: Caterpie",
            "|-damage|p1a: Caterpie|1/100",
            "|move|p1a: Caterpie|Agility|p1a: Caterpie",
            "|-boost|p1a: Caterpie|spe|2",
            "|upkeep",
            "|turn|7",
        ]
        check_speed_ranges(self.battle, messages)
        self.assertEqual(150, self.battle.opponent.active.speed_range.min)

    def test_boosting_speed_before_opponent_does_not_mess_up_speed_range_check(self):
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.opponent.active.stats[constants.SPEED] = 100
        self.battle.user.last_selected_move = LastUsedMove("caterpie", "agility", 0)

        messages = [
            "|move|p1a: Caterpie|Agility|p1a: Caterpie",
            "|-boost|p1a: Caterpie|spe|2",
            "|move|p2a: Pikachu|Tackle|p1a: Caterpie",
            "|-damage|p1a: Caterpie|1/100",
            "|upkeep",
            "|turn|7",
        ]
        check_speed_ranges(self.battle, messages)
        self.assertEqual(150, self.battle.opponent.active.speed_range.max)

    def test_opponent_knocking_out_user_sets_speed_range_if_bot_used_same_priority_move(
        self,
    ):
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.opponent.active.stats[constants.SPEED] = 100
        self.battle.user.last_selected_move = LastUsedMove("caterpie", "tackle", 0)

        messages = [
            "|move|p2a: Pikachu|Tackle|p1a: Caterpie",
            "|-damage|p1a: Caterpie|0 fnt",
            "|faint|p1a: Caterpie",
            "|upkeep",
            "|turn|7",
        ]
        check_speed_ranges(self.battle, messages)
        self.assertEqual(150, self.battle.opponent.active.speed_range.min)

    def test_user_knocking_out_opponent_does_nothing(
        self,
    ):
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.opponent.active.stats[constants.SPEED] = 100
        self.battle.user.last_selected_move = LastUsedMove("caterpie", "tackle", 0)

        messages = [
            "|move|p1a: Caterpie|Tackle|p2a: Pikachu",
            "|-damage|p2a: Pikachu|0 fnt",
            "|faint|p2a: Pikachu",
            "|upkeep",
            "|turn|7",
        ]
        check_speed_ranges(self.battle, messages)
        self.assertEqual(0, self.battle.opponent.active.speed_range.min)

    def test_suckerpunch_and_thunderclap_sets_speed_ranges(self):
        # opponent should have min speed equal to the bot's speed
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.opponent.active.stats[constants.SPEED] = 175

        messages = [
            "|move|p2a: Raging Bolt|Thunderclap|p1a: Kingambit"
            "|-damage|p1a: Kingambit|46/100",
            "|-enditem|p1a: Kingambit|Air Balloon",
            "|move|p1a: Kingambit|Sucker Punch||[still]",
            "|-fail|p1a: Kingambit",
            "|",
            "|upkeep",
            "|turn|7",
        ]

        check_speed_ranges(self.battle, messages)

        self.assertEqual(
            150,
            self.battle.opponent.active.speed_range.min,
        )

    def test_sets_minspeed_when_opponent_goes_first(self):
        # opponent should have min speed equal to the bot's speed
        self.battle.user.active.stats[constants.SPEED] = 150

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_speed_ranges(self.battle, messages)

        self.assertEqual(
            self.battle.user.active.stats[constants.SPEED],
            self.battle.opponent.active.speed_range.min,
        )

    def test_sets_maxspeed_when_opponent_goes_first_in_trickroom(self):
        # opponent should have min speed equal to the bot's speed
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.trick_room = True

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_speed_ranges(self.battle, messages)

        self.assertEqual(
            self.battle.user.active.stats[constants.SPEED],
            self.battle.opponent.active.speed_range.max,
        )

    def test_nothing_happens_with_priority_move_in_trickroom(self):
        # opponent should have min speed equal to the bot's speed
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.trick_room = True

        messages = [
            "|move|p2a: Caterpie|Aqua Jet|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_speed_ranges(self.battle, messages)

        self.assertEqual(float("inf"), self.battle.opponent.active.speed_range.max)
        self.assertEqual(0, self.battle.opponent.active.speed_range.min)

    def test_accounts_for_paralysis_when_calculating_speed_range(self):
        # opponent should have min speed equal to the bot's speed
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.opponent.active.status = constants.PARALYZED

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_speed_ranges(self.battle, messages)

        # bot_speed * 2 should be the minspeed it has b/c it went first with paralysis
        expected_min_speed = int(self.battle.user.active.stats[constants.SPEED] * 2)

        self.assertEqual(
            expected_min_speed, self.battle.opponent.active.speed_range.min
        )

    def test_accounts_for_paralysis_on_bots_side_when_calculating_speed_range(self):
        # opponent should have min speed equal to the bot's speed
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.user.active.status = constants.PARALYZED

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_speed_ranges(self.battle, messages)

        # bot_speed / 2 should be the minspeed it has b/c it went first with paralysis
        expected_min_speed = int(self.battle.user.active.stats[constants.SPEED] / 2)

        self.assertEqual(
            expected_min_speed, self.battle.opponent.active.speed_range.min
        )

    def test_accounts_for_tailwind_on_opponent_side_when_calculating_speed_ranges(self):
        # opponent should have min speed equal to the bot's speed
        self.battle.user.active.stats[constants.SPEED] = 300
        self.battle.opponent.side_conditions[constants.TAILWIND] = 1

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_speed_ranges(self.battle, messages)

        # bot_speed / 2 should be the minspeed it has b/c it went first with tailwind up
        expected_min_speed = int(self.battle.user.active.stats[constants.SPEED] / 2)

        self.assertEqual(
            expected_min_speed, self.battle.opponent.active.speed_range.min
        )

    def test_accounts_for_tailwind_on_bot_side_when_calculating_speed_ranges(self):
        # opponent should have min speed equal to the bot's speed
        self.battle.user.active.stats[constants.SPEED] = 300
        self.battle.user.side_conditions[constants.TAILWIND] = 1

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_speed_ranges(self.battle, messages)

        # bot_speed * 2 should be the minspeed it has b/c it went first with tailwind up
        expected_min_speed = int(self.battle.user.active.stats[constants.SPEED] * 2)

        self.assertEqual(
            expected_min_speed, self.battle.opponent.active.speed_range.min
        )

    def test_accounts_for_tailwind_on_both_side_when_calculating_speed_ranges(self):
        # opponent should have min speed equal to the bot's speed
        self.battle.user.active.stats[constants.SPEED] = 300
        self.battle.user.side_conditions[constants.TAILWIND] = 1
        self.battle.opponent.side_conditions[constants.TAILWIND] = 1

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_speed_ranges(self.battle, messages)

        # bot_speed / 2 should be the minspeed it has b/c it went first with tailwind up
        expected_min_speed = int(self.battle.user.active.stats[constants.SPEED] / 2)

        # bot_speed * 2 should be the minspeed it has b/c it went first with tailwind up
        expected_min_speed = int(expected_min_speed * 2)

        self.assertEqual(
            expected_min_speed, self.battle.opponent.active.speed_range.min
        )

    def test_does_not_set_minspeed_when_opponent_could_have_unburden_activated(self):
        # opponent should have min speed equal to the bot's speed
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.opponent.active.item = None
        self.battle.opponent.active.name = "hawlucha"  # can possibly have unburden

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_speed_ranges(self.battle, messages)

        self.assertEqual(0, self.battle.opponent.active.speed_range.min)

    def test_sets_maxspeed_when_bot_goes_first(self):
        # opponent should have max speed equal to the bot's speed
        self.battle.user.active.stats[constants.SPEED] = 150

        messages = [
            "|move|p1a: Caterpie|Stealth Rock|",
            "|move|p2a: Caterpie|Stealth Rock|",
        ]

        check_speed_ranges(self.battle, messages)

        self.assertEqual(
            self.battle.user.active.stats[constants.SPEED],
            self.battle.opponent.active.speed_range.max,
        )

    def test_minspeed_is_not_set_when_rain_is_up_and_opponent_can_have_swiftswim(self):
        # opponent should have max speed equal to the bot's speed
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.weather = constants.RAIN
        self.battle.opponent.active.name = "seismitoad"

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_speed_ranges(self.battle, messages)

        self.assertEqual(0, self.battle.opponent.active.speed_range.min)

    def test_minspeed_is_set_when_only_rain_is_up(self):
        # opponent should have max speed equal to the bot's speed
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.weather = constants.RAIN

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_speed_ranges(self.battle, messages)

        self.assertEqual(
            self.battle.user.active.stats[constants.SPEED],
            self.battle.opponent.active.speed_range.min,
        )

    def test_minspeed_is_set_when_rain_is_not_up_but_opponent_could_have_swiftswim(
        self,
    ):
        # opponent should have max speed equal to the bot's speed
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.opponent.active.name = "seismitoad"

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_speed_ranges(self.battle, messages)

        self.assertEqual(
            self.battle.user.active.stats[constants.SPEED],
            self.battle.opponent.active.speed_range.min,
        )

    def test_minspeed_is_not_set_when_opponent_has_choicescarf(self):
        # opponent should have max speed equal to the bot's speed
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.opponent.active.item = "choicescarf"

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_speed_ranges(self.battle, messages)

        self.assertEqual(0, self.battle.opponent.active.speed_range.min)

    def test_minspeed_is_correctly_set_when_bot_has_choicescarf(self):
        # opponent should have max speed equal to the bot's speed
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.user.active.item = "choicescarf"

        messages = [
            "|move|p1a: Caterpie|Stealth Rock|",
            "|move|p2a: Caterpie|Stealth Rock|",
        ]

        check_speed_ranges(self.battle, messages)

        self.assertEqual(
            self.battle.user.active.stats[constants.SPEED] * 1.5,
            self.battle.opponent.active.speed_range.max,
        )

    def test_minspeed_is_correctly_set_when_bot_has_choicescarf_and_opponent_is_boosted(
        self,
    ):
        # opponent should have max speed equal to the bot's speed
        self.battle.user.active.stats[constants.SPEED] = 317
        self.battle.opponent.active.stats[constants.SPEED] = 383
        self.battle.user.active.item = "choicescarf"
        self.battle.opponent.active.boosts[constants.SPEED] = 1

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_speed_ranges(self.battle, messages)

        # this is meant to show the rounding inherent with way pokemon floors values
        # floor(317 / 1.5) = 211
        # floor(211*1.5) = 316
        expected_speed = int(self.battle.user.active.stats[constants.SPEED] / 1.5)
        expected_speed = int(expected_speed * 1.5)

        self.assertEqual(expected_speed, self.battle.opponent.active.speed_range.min)

    def test_minspeed_interaction_with_boosted_speed(self):
        # opponent should have max speed equal to the bot's speed
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.opponent.active.boosts[constants.SPEED] = 1

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_speed_ranges(self.battle, messages)

        # the minspeed should take into account the fact that the opponent has a boost
        # therefore, the minimum (unboosted) speed must be divided by the boost multiplier
        expected_min_speed = int(
            150
            / boost_multiplier_lookup[
                self.battle.opponent.active.boosts[constants.SPEED]
            ]
        )

        self.assertEqual(
            expected_min_speed, self.battle.opponent.active.speed_range.min
        )

    def test_minspeed_interaction_with_bots_boosted_speed(self):
        # opponent should have max speed equal to the bot's speed
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.user.active.boosts[constants.SPEED] = 1

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_speed_ranges(self.battle, messages)

        # the minspeed should take into account the fact that the opponent has a boost
        # therefore, the minimum (unboosted) speed must be divided by the boost multiplier
        expected_min_speed = int(
            150
            * boost_multiplier_lookup[self.battle.user.active.boosts[constants.SPEED]]
            / boost_multiplier_lookup[
                self.battle.opponent.active.boosts[constants.SPEED]
            ]
        )

        self.assertEqual(
            expected_min_speed, self.battle.opponent.active.speed_range.min
        )

    def test_minspeed_interaction_with_bot_and_opponents_boosted_speed(self):
        # opponent should have max speed equal to the bot's speed
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.user.active.boosts[constants.SPEED] = 1
        self.battle.opponent.active.boosts[constants.SPEED] = 3

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_speed_ranges(self.battle, messages)

        # the minspeed should take into account the fact that the opponent has a boost
        # therefore, the minimum (unboosted) speed must be divided by the boost multiplier
        expected_min_speed = int(
            150
            * boost_multiplier_lookup[self.battle.user.active.boosts[constants.SPEED]]
            / boost_multiplier_lookup[
                self.battle.opponent.active.boosts[constants.SPEED]
            ]
        )

        self.assertEqual(
            expected_min_speed, self.battle.opponent.active.speed_range.min
        )

    def test_opponents_unknown_move_is_used_as_a_zero_priority_move(self):
        # opponent should have max speed equal to the bot's speed
        self.battle.user.active.stats[constants.SPEED] = 150

        messages = [
            "|move|p2a: Caterpie|unknown-move|",
            "|move|p1a: Caterpie|unknown-move|",
        ]

        check_speed_ranges(self.battle, messages)

        self.assertEqual(150, self.battle.opponent.active.speed_range.min)

    def test_bots_unknown_move_is_used_as_a_zero_priority_move(self):
        # opponent should have max speed equal to the bot's speed
        self.battle.user.active.stats[constants.SPEED] = 150

        messages = [
            "|move|p1a: Caterpie|unknown-move|",
            "|move|p2a: Caterpie|unknown-move|",
        ]

        check_speed_ranges(self.battle, messages)

        self.assertEqual(150, self.battle.opponent.active.speed_range.max)

    def test_opponent_has_unknown_choicescarf_causing_it_to_be_faster(self):
        # Situation:
        #   The opponent's pokemon has a choice scarf but the bot doesn't know that - it only sees it's item as unknown
        #   The choicescarf causes the opponent to go first, when it wouldn't have gone first normally
        #   If the opponent didn't have a choicescarf it COULD still be naturally faster than the bot's pokemon
        #   This means the check_choicescarf function won't assign a choicescarf
        # Expected Result:
        #   min_speed should be set to the bot's speed. The set inferral DOES take into account items when validating
        #   the final speed

        # opponent should have max speed equal to the bot's speed
        self.battle.user.active.stats[constants.SPEED] = 150

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_speed_ranges(self.battle, messages)
        expected_min_speed = 150
        self.assertEqual(
            expected_min_speed, self.battle.opponent.active.speed_range.min
        )

    def test_opponent_using_grassyglide_in_grassy_terrain_does_not_cause_minspeed_to_be_set(
        self,
    ):
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.field = constants.GRASSY_TERRAIN

        messages = [
            "|move|p2a: Caterpie|Grassy Glide|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_speed_ranges(self.battle, messages)
        self.assertEqual(0, self.battle.opponent.active.speed_range.min)

    def test_bot_using_grassyglide_in_grassy_terrain_does_not_cause_maxspeed_to_be_set(
        self,
    ):
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.field = constants.GRASSY_TERRAIN

        messages = [
            "|move|p1a: Caterpie|Grassy Glide|",
            "|move|p2a: Caterpie|Stealth Rock|",
        ]

        check_speed_ranges(self.battle, messages)
        self.assertEqual(float("inf"), self.battle.opponent.active.speed_range.max)

    def test_move_from_magicbounce_after_switching_does_not_set_speed_range(self):
        user_reserve_weedle = Pokemon("Weedle", 100)
        self.battle.user.reserve = [user_reserve_weedle]

        messages = [
            "|switch|p1a: Caterpie|Caterpie, F|255/255",
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|p2a: Caterpie|[from]ability: Magic Bounce",
        ]

        check_speed_ranges(self.battle, messages)

        # speed ranges should be unchanged because this was a switch-in
        self.assertEqual(float("inf"), self.battle.opponent.active.speed_range.max)
        self.assertEqual(0, self.battle.opponent.active.speed_range.min)


    def test_pivot_switch_after_both_moves_still_sets_speed_range(self):
        # U-turn's own |switch| is emitted AFTER both |move| lines: it cannot
        # have changed the ordering of moves that already resolved
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.user.last_selected_move = LastUsedMove("caterpie", "tackle", 0)

        messages = [
            "|move|p2a: Caterpie|Tackle|p1a: Caterpie",
            "|-damage|p1a: Caterpie|150/255",
            "|move|p1a: Caterpie|U-turn|p2a: Caterpie",
            "|-damage|p2a: Caterpie|150/255",
            "|switch|p1a: Weedle|Weedle, M|255/255",
        ]

        check_speed_ranges(self.battle, messages)

        self.assertEqual(150, self.battle.opponent.active.speed_range.min)

    def test_opponent_flinch_sets_max_speed(self):
        # the opponent's action slot came after our 0-priority hit
        self.battle.battle_type = BattleType.RANDOM_BATTLE
        RandomBattleTeamDatasets.initialize("gen9randombattle")
        # gyarados' randbats movepool has no negative-priority move
        self.battle.opponent.active = Pokemon("gyarados", 79)
        self.battle.opponent.active.ability = None
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.user.last_selected_move = LastUsedMove("caterpie", "tackle", 0)

        messages = [
            "|move|p1a: Caterpie|Tackle|p2a: Gyarados",
            "|-damage|p2a: Gyarados|150/255",
            "|cant|p2a: Gyarados|flinch",
        ]

        check_speed_ranges(self.battle, messages)

        self.assertEqual(150, self.battle.opponent.active.speed_range.max)

    def test_opponent_flinch_with_negative_priority_candidate_bails(self):
        self.battle.battle_type = BattleType.RANDOM_BATTLE
        RandomBattleTeamDatasets.initialize("gen9randombattle")
        self.battle.opponent.active = Pokemon("gyarados", 79)
        self.battle.opponent.active.ability = None
        self.battle.opponent.active.add_move("dragontail")  # priority -6
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.user.last_selected_move = LastUsedMove("caterpie", "tackle", 0)

        messages = [
            "|move|p1a: Caterpie|Tackle|p2a: Gyarados",
            "|-damage|p2a: Gyarados|150/255",
            "|cant|p2a: Gyarados|flinch",
        ]

        check_speed_ranges(self.battle, messages)

        self.assertEqual(float("inf"), self.battle.opponent.active.speed_range.max)

    def test_opponent_recharge_before_our_move_sets_min_speed(self):
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.user.last_selected_move = LastUsedMove("caterpie", "tackle", 0)

        messages = [
            "|cant|p2a: Caterpie|recharge",
            "|move|p1a: Caterpie|Tackle|p2a: Caterpie",
            "|-damage|p2a: Caterpie|150/255",
        ]

        check_speed_ranges(self.battle, messages)

        self.assertEqual(150, self.battle.opponent.active.speed_range.min)

    def test_our_own_cant_still_bails(self):
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.user.last_selected_move = LastUsedMove("caterpie", "tackle", 0)

        messages = [
            "|cant|p1a: Caterpie|par",
            "|move|p2a: Caterpie|Tackle|p1a: Caterpie",
        ]

        check_speed_ranges(self.battle, messages)

        self.assertEqual(0, self.battle.opponent.active.speed_range.min)

    def test_opponent_paralysis_cant_still_bails(self):
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.user.last_selected_move = LastUsedMove("caterpie", "tackle", 0)

        messages = [
            "|move|p1a: Caterpie|Tackle|p2a: Caterpie",
            "|cant|p2a: Caterpie|par",
        ]

        check_speed_ranges(self.battle, messages)

        self.assertEqual(float("inf"), self.battle.opponent.active.speed_range.max)

    def test_switch_before_moves_still_bails(self):
        self.battle.user.active.stats[constants.SPEED] = 150
        self.battle.user.last_selected_move = LastUsedMove("caterpie", "tackle", 0)

        messages = [
            "|switch|p2a: Caterpie|Caterpie, F|255/255",
            "|move|p2a: Caterpie|Tackle|p1a: Caterpie",
            "|move|p1a: Caterpie|Tackle|p2a: Caterpie",
        ]

        check_speed_ranges(self.battle, messages)

        self.assertEqual(0, self.battle.opponent.active.speed_range.min)
        self.assertEqual(float("inf"), self.battle.opponent.active.speed_range.max)


class TestGuessChoiceScarf(unittest.TestCase):
    def setUp(self):
        # These fixtures use Caterpie as a generic stand-in, but the item
        # inferences now refuse to guess an item no set of the species carries
        # (fp/inference.py species_can_hold) and Caterpie has neither Boots nor
        # a Choice Scarf. Clear the dataset so the guard no-ops and these tests
        # keep testing the INFERENCE MECHANICS they are about; a species-pool
        # test would be a different test.
        RandomBattleTeamDatasets.__init__()
        self.addCleanup(RandomBattleTeamDatasets.__init__)
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active
        self.battle.opponent.active.ability = None

        self.user_active = Pokemon("caterpie", 100)
        self.battle.user.active = self.user_active

        self.username = "CoolUsername"

        self.battle.username = self.username

        self.battle.request_json = {
            constants.ACTIVE: [{constants.MOVES: []}],
            constants.SIDE: {
                constants.ID: None,
                constants.NAME: None,
                constants.POKEMON: [],
                constants.RQID: None,
            },
        }

    def test_fainting_pkmn_with_priority_modified_does_not_infer_scarf(self):
        self.battle.user.active.stats[constants.SPEED] = (
            210  # opponent's speed should not be greater than 207 (max speed caterpie)
        )
        self.battle.user.active.ability = "myceliummight"
        self.battle.user.active.name = "toedscruel"
        self.battle.user.last_selected_move = LastUsedMove("toedscruel", "toxic", 0)

        messages = [
            "|move|p2a: Porygon2|Ice Beam|p1a: Toedscruel",
            "|-supereffective|p1a: Toedscruel",
            "|-damage|p1a: Toedscruel|0 fnt",
            "|faint|p1a: Toedscruel",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_guesses_choicescarf_when_opponent_should_always_be_slower(self):
        self.battle.user.active.stats[constants.SPEED] = (
            210  # opponent's speed should not be greater than 207 (max speed caterpie)
        )

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual("choicescarf", self.battle.opponent.active.item)

    def test_pivot_switch_after_both_moves_still_infers_scarf(self):
        self.battle.user.active.stats[constants.SPEED] = 210

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|U-turn|p2a: Caterpie",
            "|-damage|p2a: Caterpie|150/255",
            "|switch|p1a: Weedle|Weedle, M|255/255",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual("choicescarf", self.battle.opponent.active.item)

    def test_switch_before_moves_still_bails_for_scarf(self):
        self.battle.user.active.stats[constants.SPEED] = 210

        messages = [
            "|switch|p2a: Caterpie|Caterpie, F|255/255",
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_guesses_choicescarf_when_enemy_knocks_out_user(self):
        self.battle.user.active.stats[constants.SPEED] = (
            210  # opponent's speed should not be greater than 207 (max speed caterpie)
        )
        self.battle.user.last_selected_move = LastUsedMove("caterpie", "tackle", 0)
        messages = [
            "|move|p2a: Caterpie|Tackle| p1a: Caterpie",
            "|-damage|p1a: Caterpie|0 fnt",
            "|faint|p1a: Forretress",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual("choicescarf", self.battle.opponent.active.item)

    def test_does_not_guess_choicescarf_when_user_hits_self_in_confusion(self):
        self.battle.user.active.stats[constants.SPEED] = (
            210  # opponent's speed should not be greater than 207 (max speed caterpie)
        )
        self.battle.user.last_selected_move = LastUsedMove("caterpie", "tackle", 0)
        messages = [
            "|-activate|p1a: Caterpie|confusion",
            "|-damage|p1a: Caterpie|15/100|[from] confusion",
            "|move|p2a: Caterpie|Tackle| p1a: Caterpie",
            "|-damage|p1a: Caterpie|0 fnt",
            "|faint|p1a: Forretress",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_does_not_guess_choicescarf_when_opponent_knocks_out_user_with_priority_move(
        self,
    ):
        self.battle.user.active.stats[constants.SPEED] = (
            210  # opponent's speed should not be greater than 207 (max speed caterpie)
        )
        self.battle.user.last_selected_move = LastUsedMove("caterpie", "tackle", 0)
        messages = [
            "|move|p2a: Caterpie|Quick Attack| p1a: Caterpie",
            "|-damage|p1a: Caterpie|0 fnt",
            "|faint|p1a: Caterpie",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_does_not_guess_choicescarf_when_user_recharged(
        self,
    ):
        self.battle.user.active.stats[constants.SPEED] = (
            210  # opponent's speed should not be greater than 207 (max speed caterpie)
        )
        messages = [
            "|cant|p1a: Caterpie|recharge",
            "|move|p2a: Caterpie|Tackle| p1a: Caterpie",
            "|-damage|p1a: Caterpie|0 fnt",
            "|faint|p1a: Caterpie",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_does_not_guess_choicescarf_when_opponent_could_have_prankster(self):
        self.battle.opponent.active.name = "grimmsnarl"  # grimmsnarl could have prankster - it's non-damaging moves get +1 priority
        self.battle.user.active.stats[constants.SPEED] = (
            245  # opponent's speed should not be greater than 240 (max speed grimmsnarl)
        )

        messages = [
            "|move|p2a: Grimmsnarl|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_does_not_guess_choicescarf_when_opponent_is_speed_boosted(self):
        self.battle.user.active.stats[constants.SPEED] = (
            210  # opponent's speed should not be greater than 207 (max speed caterpie)
        )
        self.battle.opponent.active.boosts[constants.SPEED] = 1

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_does_not_guess_choicescarf_when_opponent_uses_grassyglide_in_grassy_terrain(
        self,
    ):
        self.battle.user.active.stats[constants.SPEED] = (
            210  # opponent's speed should not be greater than 207 (max speed caterpie)
        )
        self.battle.field = constants.GRASSY_TERRAIN

        messages = [
            "|move|p2a: Caterpie|Grassy Glide|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_does_not_guess_choicescarf_when_bot_is_speed_unboosted(self):
        self.battle.user.active.stats[constants.SPEED] = (
            210  # opponent's speed should not be greater than 207 (max speed caterpie)
        )
        self.battle.user.active.boosts[constants.SPEED] = -1

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_does_not_guess_scarf_in_trickroom(self):
        self.battle.trick_room = True
        self.battle.user.active.stats[constants.SPEED] = (
            210  # opponent's speed should not be greater than 207 (max speed caterpie)
        )

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_does_not_guess_scarf_under_trickroom_when_opponent_could_be_slower(self):
        self.battle.trick_room = True
        self.battle.user.active.stats[constants.SPEED] = (
            205  # opponent caterpie speed is 113 - 207
        )

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_never_guesses_scarf_in_trickroom(self):
        # INVERTED 2026-08-20. This used to assert a scarf GUESS here.
        #
        # Trick Room reverses the order, so the SLOWER mon moves first, and
        # check_choicescarf is only reached when the OPPONENT moved first. A
        # Choice Scarf makes a mon FASTER, which under Trick Room makes it move
        # LATER -- so moving first is evidence AGAINST a scarf and can never be
        # evidence for one. The old branch fired when the opponent could not be
        # slower than us, i.e. on a contradiction with our own speed model, and
        # resolved it by hard-writing item="choicescarf" -- a conclusion that
        # then survived to rung 1 of the relaxation ladder as if observed.
        # The sound response to that contradiction is to conclude nothing.
        self.battle.trick_room = True
        self.battle.user.active.stats[constants.SPEED] = (
            110  # opponent caterpie speed is 113 - 207
        )

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual(
            constants.UNKNOWN_ITEM, self.battle.opponent.active.item
        )

    def test_unknown_moves_defaults_to_0_priority(self):
        self.battle.user.active.stats[constants.SPEED] = (
            210  # opponent's speed should not be greater than 207 (max speed caterpie)
        )

        messages = [
            "|move|p2a: Caterpie|unknown-move|",
            "|move|p1a: Caterpie|unknown-move|",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual("choicescarf", self.battle.opponent.active.item)

    def test_priority_move_with_unknown_move_does_not_cause_guess(self):
        self.battle.user.active.stats[constants.SPEED] = (
            210  # opponent's speed should not be greater than 207 (max speed caterpie)
        )

        messages = [
            "|move|p2a: Caterpie|Quick Attack|",
            "|move|p1a: Caterpie|unknown-move|",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_does_not_guess_item_when_bot_moves_first(self):
        self.battle.user.active.stats[constants.SPEED] = (
            210  # opponent's speed should not be greater than 207 (max speed caterpie)
        )

        messages = [
            "|move|p1a: Caterpie|Stealth Rock|",
            "|move|p2a: Caterpie|Stealth Rock|",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_does_not_guess_item_when_moves_are_different_priority(self):
        self.battle.user.active.stats[constants.SPEED] = (
            210  # opponent's speed should not be greater than 207 (max speed caterpie)
        )

        messages = [
            "|move|p2a: Caterpie|Quick Attack|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_does_not_guess_item_when_opponent_can_be_faster(self):
        self.battle.user.active.stats[constants.SPEED] = (
            200  # opponent's speed can be 207 (max speed caterpie)
        )

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_swiftswim_causing_opponent_to_be_faster_results_in_not_guessing_choicescarf(
        self,
    ):
        self.battle.opponent.active.ability = "swiftswim"
        self.battle.weather = constants.RAIN
        self.battle.user.active.stats[constants.SPEED] = (
            300  # opponent's speed can be 414 (max speed caterpie plus swiftswim)
        )

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_pokemon_possibly_having_swiftswim_in_rain_does_not_result_in_a_choicescarf_guess(
        self,
    ):
        self.battle.opponent.active.name = "seismitoad"  # can have swiftswim
        self.battle.weather = constants.RAIN
        self.battle.user.active.stats[constants.SPEED] = (
            210  # opponent's speed can be 414 (max speed caterpie plus swiftswim)
        )

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_seismitoad_choicescarf_is_guessed_when_ability_has_been_revealed(self):
        self.battle.opponent.active.name = (
            "seismitoad"  # set ID so lookup says it has swiftswim
        )
        self.battle.opponent.active.ability = "waterabsorb"  # but ability has been revealed so if it is faster a choice item should be inferred
        self.battle.weather = constants.RAIN
        self.battle.user.active.stats[constants.SPEED] = (
            300  # opponent's speed can be 414 (max speed caterpie plus swiftswim). Yes it is still a caterpie
        )

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual("choicescarf", self.battle.opponent.active.item)

    def test_possible_surgesurfer_does_not_result_in_scarf_inferral(self):
        self.battle.opponent.active.name = (
            "raichualola"  # set ID so lookup says it has surgesurfer
        )
        self.battle.field = constants.ELECTRIC_TERRAIN
        self.battle.user.active.stats[constants.SPEED] = (
            300  # opponent's speed can be 414 (max speed caterpie plus swiftswim). Yes it is still a caterpie
        )

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_surgesurfer_pokemon_choice_item_is_guessed_if_ability_is_revealed_to_be_otherwise(
        self,
    ):
        self.battle.opponent.active.name = (
            "raichualola"  # set ID so lookup says it has surgesurfer
        )
        self.battle.opponent.active.ability = "some_weird_ability"
        self.battle.field = constants.ELECTRIC_TERRAIN
        self.battle.user.active.stats[constants.SPEED] = (
            300  # opponent's speed can be 414 (max speed caterpie plus swiftswim). Yes it is still a caterpie
        )

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual("choicescarf", self.battle.opponent.active.item)

    def test_pokemon_with_possible_quickfeet_does_not_have_choice_scarf_inferred(self):
        self.battle.opponent.active.name = (
            "ursaring"  # set ID so lookup says it has quickfeet
        )
        self.battle.opponent.active.status = constants.PARALYZED
        self.battle.user.active.stats[constants.SPEED] = 210

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_pokemon_with_possible_quickfeet_does_have_choice_scarf_inferred_if_ability_revealed_to_something_else(
        self,
    ):
        self.battle.opponent.active.name = (
            "ursaring"  # set ID so lookup says it has quickfeet
        )
        self.battle.opponent.active.ability = (
            "some_other_ability"  # ability cant be quickfeet
        )
        self.battle.opponent.active.status = constants.PARALYZED
        self.battle.user.active.stats[constants.SPEED] = 210

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual("choicescarf", self.battle.opponent.active.item)

    def test_does_not_guess_choicescarf_when_item_is_none(self):
        self.battle.opponent.active.item = None
        self.battle.user.active.stats[constants.SPEED] = (
            210  # opponent's speed should not be greater than 207 (max speed caterpie)
        )

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual(None, self.battle.opponent.active.item)

    def test_does_not_guess_choicescarf_when_item_is_known(self):
        self.battle.opponent.active.item = "leftovers"
        self.battle.user.active.stats[constants.SPEED] = (
            210  # opponent's speed should not be greater than 207 (max speed caterpie)
        )

        messages = [
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual("leftovers", self.battle.opponent.active.item)

    def test_uses_randombattle_spread_when_guessing_for_randombattle(self):
        self.battle.battle_type = BattleType.RANDOM_BATTLE

        # opponent's speed should be 193 WITHOUT a choicescarf
        # HOWEVER, max-speed should still outspeed this value
        self.battle.user.active.stats[constants.SPEED] = 195

        self.opponent_active = Pokemon(
            "floetteeternal", 80
        )  # randombattle level for Floette-E
        self.battle.opponent.active = self.opponent_active

        messages = [
            "|move|p2a: Floette-Eternal|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual("choicescarf", self.battle.opponent.active.item)

    def test_choicescarf_is_not_checked_when_switching_happens(self):
        self.battle.user.active.stats[constants.SPEED] = 210
        user_reserve_weedle = Pokemon("Weedle", 100)
        user_reserve_weedle.stats[constants.SPEED] = 75
        self.battle.user.reserve = [user_reserve_weedle]

        messages = [
            "|switch|p1a: Caterpie|Caterpie, F|255/255",
            "|move|p2a: Caterpie|Stealth Rock|",
            "|move|p1a: Caterpie|Stealth Rock|p2a: Caterpie|[from]ability: Magic Bounce",
        ]

        check_choicescarf(self.battle, messages)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)


class TestCheckHeavyDutyBoots(unittest.TestCase):
    def setUp(self):
        # These fixtures use Caterpie as a generic stand-in, but the item
        # inferences now refuse to guess an item no set of the species carries
        # (fp/inference.py species_can_hold) and Caterpie has neither Boots nor
        # a Choice Scarf. Clear the dataset so the guard no-ops and these tests
        # keep testing the INFERENCE MECHANICS they are about; a species-pool
        # test would be a different test.
        RandomBattleTeamDatasets.__init__()
        self.addCleanup(RandomBattleTeamDatasets.__init__)
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active
        self.battle.opponent.active.ability = None
        self.opponent_reserve_pkmn = Pokemon("weedle", 100)
        self.battle.opponent.reserve.append(self.opponent_reserve_pkmn)
        self.battle.opponent.active.item = constants.UNKNOWN_ITEM

        self.user_active = Pokemon("caterpie", 100)
        self.battle.user.active = self.user_active

        self.user_reserve_pkmn = Pokemon("weedle", 100)
        self.battle.user.reserve.append(self.user_reserve_pkmn)
        self.battle.user.active.item = constants.UNKNOWN_ITEM

        self.username = "CoolUsername"

        self.battle.username = self.username
        self.battle.generation = "gen9"

        self.battle.request_json = {
            constants.ACTIVE: [{constants.MOVES: []}],
            constants.SIDE: {
                constants.ID: None,
                constants.NAME: None,
                constants.POKEMON: [],
                constants.RQID: None,
            },
        }

    def test_basic_case_of_switching_in_and_not_taking_damage_sets_heavydutyboots(self):
        self.battle.opponent.side_conditions[constants.STEALTH_ROCK] = 1
        self.battle.msg_list = [
            "|switch|p2a: Weedle|Weedle, M|100/100",
            "|move|p1a: Caterpie|Tackle",
            "|-damage|p2a: Weedle|90/100",
        ]

        process_battle_updates(self.battle)

        self.assertEqual("heavydutyboots", self.battle.opponent.active.item)

    def test_parser_deals_with_empty_line(self):
        self.battle.opponent.side_conditions[constants.STEALTH_ROCK] = 1
        self.battle.msg_list = [
            "|switch|p2a: Weedle|Weedle, M|100/100",
            "|move|p1a: Caterpie|Tackle",
            "|-damage|p2a: Weedle|90/100",
            "",
        ]

        process_battle_updates(self.battle)

        self.assertEqual("heavydutyboots", self.battle.opponent.active.item)

    def test_parser_deals_with_empty_line_with_toxicspikes(self):
        self.battle.opponent.side_conditions[constants.TOXIC_SPIKES] = 1
        self.battle.msg_list = [
            "|switch|p2a: Pikachu|Pikachu, M|100/100",
            "",
            "|move|p1a: Caterpie|Tackle",
            "|-damage|p2a: Pikachu|90/100",
            "",
        ]

        process_battle_updates(self.battle)

        self.assertEqual("heavydutyboots", self.battle.opponent.active.item)

    def test_having_an_item_bypasses_this_check(self):
        self.battle.opponent.side_conditions[constants.STEALTH_ROCK] = 1
        self.battle.opponent.active.item = None
        messages = [
            "|switch|p2a: Weedle|Weedle, M|100/100",
            "|move|p1a: Caterpie|Tackle",
            "|-damage|p2a: Weedle|90/100",
        ]

        check_heavydutyboots(self.battle, messages)

        self.assertEqual(None, self.battle.opponent.active.item)

    def test_double_switch_where_other_side_takes_damage_does_not_set_hdb_for_the_first_side(
        self,
    ):
        self.battle.opponent.side_conditions[constants.STEALTH_ROCK] = 1
        self.battle.msg_list = [
            "|switch|p2a: Weedle|Weedle, M|100/100",
            "|switch|p1a: Weedle|Weedle, M|100/100",
            "|-damage|p1a: Weedle|88/100|[from] Stealth Rock",
        ]

        process_battle_updates(self.battle)

        self.assertEqual("heavydutyboots", self.battle.opponent.active.item)

    def test_basic_case_of_switching_in_and_taking_damage_does_not_set_heavydutyboots(
        self,
    ):
        self.battle.opponent.side_conditions[constants.STEALTH_ROCK] = 1
        self.battle.msg_list = [
            "|switch|p2a: Weedle|Weedle, M|100/100",
            "|-damage|p2a: Weedle|88/100|[from] Stealth Rock"
            "|move|p1a: Caterpie|Tackle",
            "|-damage|p2a: Weedle|78/100",
        ]

        process_battle_updates(self.battle)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_basic_case_of_switching_in_and_taking_damage_sets_heavydutyboots_to_impossible(
        self,
    ):
        self.battle.opponent.side_conditions[constants.STEALTH_ROCK] = 1
        self.battle.msg_list = [
            "|switch|p2a: Weedle|Weedle, M|100/100",
            "|-damage|p2a: Weedle|88/100|[from] Stealth Rock"
            "|move|p1a: Caterpie|Tackle",
            "|-damage|p2a: Weedle|78/100",
        ]

        process_battle_updates(self.battle)

        self.assertIn(
            constants.HEAVY_DUTY_BOOTS, self.battle.opponent.active.impossible_items
        )

    def test_not_taking_damage_from_spikes_sets_heavydutyboots(self):
        self.battle.opponent.side_conditions[constants.SPIKES] = 1
        self.battle.msg_list = [
            "|switch|p2a: Weedle|Weedle, M|100/100",
            "|move|p1a: Caterpie|Tackle",
            "|-damage|p2a: Weedle|78/100",
        ]

        process_battle_updates(self.battle)

        self.assertEqual("heavydutyboots", self.battle.opponent.active.item)

    def test_taking_damage_from_spikes_does_not_set_heavydutyboots(self):
        self.battle.opponent.side_conditions[constants.SPIKES] = 1
        self.battle.msg_list = [
            "|switch|p2a: Weedle|Weedle, M|100/100",
            "|-damage|p2a: Weedle|88/100|[from] Spikes" "|move|p1a: Caterpie|Tackle",
            "|-damage|p2a: Weedle|78/100",
        ]

        process_battle_updates(self.battle)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_taking_damage_from_spikes_sets_heavydutyboots_to_impossible(self):
        self.battle.opponent.side_conditions[constants.SPIKES] = 1
        self.battle.msg_list = [
            "|switch|p2a: Weedle|Weedle, M|100/100",
            "|-damage|p2a: Weedle|88/100|[from] Spikes" "|move|p1a: Caterpie|Tackle",
            "|-damage|p2a: Weedle|78/100",
        ]

        process_battle_updates(self.battle)

        self.assertIn(
            constants.HEAVY_DUTY_BOOTS, self.battle.opponent.active.impossible_items
        )

    def test_not_getting_poisoned_by_toxicspikes_sets_heavydutyboots(self):
        self.battle.opponent.side_conditions[constants.TOXIC_SPIKES] = 1
        self.battle.opponent.active = Pokemon("pikachu", 100)
        self.battle.msg_list = [
            "|switch|p2a: Pikachu|Pikachu, M|100/100",
            "|move|p1a: Caterpie|Tackle",
            "|-damage|p2a: Pikachu|78/100",
        ]

        process_battle_updates(self.battle)

        self.assertEqual("heavydutyboots", self.battle.opponent.active.item)

    def test_not_getting_poisoned_by_toxicspikes_does_not_set_heavydutyboots_if_already_poisoned(
        self,
    ):
        self.battle.opponent.side_conditions[constants.TOXIC_SPIKES] = 1
        pikachu = Pokemon("pikachu", 100)
        pikachu.status = constants.POISON
        self.battle.opponent.reserve.append(pikachu)
        # PS always appends the entrant's status to the |switch| line's
        # HP STATUS field (sim/pokemon.ts:2104-2107 `getHealth`), and
        # switch_or_drag now mirrors that field, so an already-poisoned entrant
        # has to be announced as such for this fixture to describe a real battle
        self.battle.msg_list = [
            "|switch|p2a: Pikachu|Pikachu, M|100/100 psn",
            "|move|p1a: Caterpie|Tackle",
            "|-damage|p2a: Pikachu|78/100",
        ]

        process_battle_updates(self.battle)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_does_not_infer_headydutyboots_if_levitate_is_possible_with_tspikes(self):
        self.battle.opponent.side_conditions[constants.TOXIC_SPIKES] = 1
        self.battle.opponent.active = Pokemon("azelf", 100)
        self.battle.msg_list = [
            "|switch|p2a: Azelf|Azelf, M|100/100",
            "|move|p1a: Caterpie|Tackle",
            "|-damage|p2a: Azelf|78/100",
        ]

        process_battle_updates(self.battle)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_does_not_infer_headydutyboots_if_levitate_is_possible_with_spikes(self):
        self.battle.opponent.side_conditions[constants.SPIKES] = 1
        self.battle.opponent.active = Pokemon("azelf", 100)
        self.battle.msg_list = [
            "|switch|p2a: Azelf|Azelf, M|100/100",
            "|move|p1a: Caterpie|Tackle",
            "|-damage|p2a: Azelf|78/100",
        ]

        process_battle_updates(self.battle)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_getting_poisoned_by_two_layers_of_toxicspikes_does_not_set_heavydutyboots(
        self,
    ):
        self.battle.opponent.side_conditions[constants.TOXIC_SPIKES] = 1
        self.battle.opponent.active = Pokemon("pikachu", 100)
        self.battle.msg_list = [
            "|switch|p2a: Pikachu|Pikachu, M|100/100",
            "|-status|p2a: Pikachu|tox",
            "|move|p1a: Caterpie|Tackle",
            "|-damage|p2a: Pikachu|78/100",
        ]

        process_battle_updates(self.battle)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_getting_toxiced_by_toxic_afterwards_still_sets_heavydutyboots(self):
        self.battle.opponent.side_conditions[constants.TOXIC_SPIKES] = 1
        self.battle.opponent.active = Pokemon("pikachu", 100)
        self.battle.msg_list = [
            "|switch|p2a: Pikachu|Pikachu, M|100/100",
            "|move|p1a: Caterpie|Toxic",
            "|-status|p2a: Pikachu|tox",
        ]

        process_battle_updates(self.battle)

        self.assertEqual("heavydutyboots", self.battle.opponent.active.item)

    def test_toxicorb_poisoning_at_the_end_of_the_turn_does_not_infer_heavydutyboots(
        self,
    ):
        self.battle.opponent.side_conditions[constants.TOXIC_SPIKES] = 1
        self.battle.msg_list = [
            "|switch|p1a: Pikachu|Pikachu, M|100/100",
            "|switch|p2a: Pikachu|Pikachu, M|100/100",
            "|",
            "|-status|p2a: Pikachu|tox|[from] item: Toxic Orb",
        ]

        process_battle_updates(self.battle)

        self.assertEqual("toxicorb", self.battle.opponent.active.item)

    def test_having_airballoon_does_notcause_a_heavydutyboost_inferral(self):
        self.battle.opponent.side_conditions[constants.TOXIC_SPIKES] = 1

        self.battle.msg_list = [
            "|switch|p2a: Pikachu|Pikachu, M|100/100",
            "|-item|p2a: Pikachu|Air Balloon",
        ]

        process_battle_updates(self.battle)

        self.assertEqual("airballoon", self.battle.opponent.active.item)

    def test_flying_type_does_not_trigger_heavydutyboots_check_on_toxicspikes(self):
        self.battle.opponent.side_conditions[constants.TOXIC_SPIKES] = 1

        self.battle.msg_list = [
            "|switch|p2a: Pidgey|Pidgey, M|100/100",
        ]

        process_battle_updates(self.battle)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_tera_flying_type_does_not_trigger_heavydutyboots_check_on_toxicspikes(
        self,
    ):
        caterpie = Pokemon("caterpie", 100)
        caterpie.types = ["normal", "water"]
        caterpie.tera_type = "flying"
        caterpie.terastallized = True
        self.battle.opponent.reserve.append(caterpie)
        self.battle.opponent.side_conditions[constants.TOXIC_SPIKES] = 1

        self.battle.msg_list = [
            "|switch|p2a: Caterpie|Caterpie, M|100/100",
        ]

        process_battle_updates(self.battle)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_tera_flying_type_does_not_trigger_heavydutyboots_check_on_spikes(
        self,
    ):
        caterpie = Pokemon("caterpie", 100)
        caterpie.types = ["normal", "water"]
        caterpie.tera_type = "flying"
        caterpie.terastallized = True
        self.battle.opponent.reserve.append(caterpie)
        self.battle.opponent.side_conditions[constants.SPIKES] = 1

        self.battle.msg_list = [
            "|switch|p2a: Caterpie|Caterpie, M|100/100",
        ]

        process_battle_updates(self.battle)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_tera_steel_type_does_not_trigger_heavydutyboots_check_on_toxicspikes(self):
        caterpie = Pokemon("caterpie", 100)
        caterpie.types = ["normal", "water"]
        caterpie.tera_type = "steel"
        caterpie.terastallized = True
        self.battle.opponent.reserve.append(caterpie)
        self.battle.opponent.side_conditions[constants.TOXIC_SPIKES] = 1

        self.battle.msg_list = [
            "|switch|p2a: Caterpie|Caterpie, M|100/100",
        ]

        process_battle_updates(self.battle)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_getting_poisoned_by_toxicspikes_does_not_set_heavydutyboots(self):
        self.battle.opponent.side_conditions[constants.TOXIC_SPIKES] = 1
        self.battle.opponent.active = Pokemon("pikachu", 100)
        self.battle.msg_list = [
            "|switch|p2a: Pikachu|Pikachu, M|100/100",
            "|-status|p2a: Pikachu|psn",
            "|move|p1a: Caterpie|Tackle",
            "|-damage|p2a: Pikachu|78/100",
        ]

        process_battle_updates(self.battle)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_nothing_is_set_when_there_are_no_hazards_on_the_field(self):
        self.battle.msg_list = [
            "|switch|p2a: Weedle|Weedle, M|100/100",
            "|move|p1a: Caterpie|Tackle",
            "|-damage|p2a: Weedle|78/100",
        ]

        process_battle_updates(self.battle)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_pokemon_that_could_have_magicguard_does_not_set_heavydutyboots_when_no_damage_is_taken(
        self,
    ):
        # clefable could have magicguard so HDB should not be set even though no damage was taken on switch
        self.battle.opponent.active = Pokemon("Clefable", 100)
        self.battle.msg_list = [
            "|switch|p2a: Clefable|Clefable, M|100/100",
            "|move|p1a: Caterpie|Tackle",
            "|-damage|p2a: Clefable|78/100",
        ]

        process_battle_updates(self.battle)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_being_caught_in_stickyweb_does_not_set_set_heavydutyboots(self):
        self.battle.opponent.side_conditions[constants.STICKY_WEB] = 1
        self.battle.msg_list = [
            "|switch|p2a: Weedle|Weedle, M|100/100",
            "|-activate|p2a: Weedle|move: Sticky Web",
            "|move|p1a: Caterpie|Tackle",
            "|-damage|p2a: Weedle|78/100",
        ]

        process_battle_updates(self.battle)

        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_being_caught_in_stickyweb_sets_heavydutyboots_to_impossible(self):
        self.battle.opponent.side_conditions[constants.STICKY_WEB] = 1
        self.battle.msg_list = [
            "|switch|p2a: Weedle|Weedle, M|100/100",
            "|-activate|p2a: Weedle|move: Sticky Web",
            "|move|p1a: Caterpie|Tackle",
            "|-damage|p2a: Weedle|78/100",
        ]

        process_battle_updates(self.battle)

        self.assertIn(
            constants.HEAVY_DUTY_BOOTS, self.battle.opponent.active.impossible_items
        )

    def test_not_being_caught_in_stickyweb_sets_item_to_heavydutyboots(self):
        self.battle.opponent.side_conditions[constants.STICKY_WEB] = 1
        self.battle.msg_list = [
            "|switch|p2a: Weedle|Weedle, M|100/100",
            "|move|p1a: Caterpie|Tackle",
            "|-damage|p2a: Weedle|78/100",
        ]

        process_battle_updates(self.battle)

        self.assertEqual("heavydutyboots", self.battle.opponent.active.item)

    def test_not_taking_spikes_with_possible_magicguard_does_not_set_heavydutyboots(
        self,
    ):
        self.battle.opponent.side_conditions[constants.SPIKES] = 1
        self.battle.opponent.reserve.append(Pokemon("Clefable", 100))
        self.battle.msg_list = [
            "|switch|p2a: Clefable|Clefable, M|100/100",
            "|move|p1a: Caterpie|Tackle",
            "|-damage|p2a: Clefable|78/100",
        ]

        process_battle_updates(self.battle)

        self.assertNotEqual("heavydutyboots", self.battle.opponent.active.item)
        self.assertNotIn("heavydutyboots", self.battle.opponent.active.impossible_items)


class TestRemoveItem(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active
        self.battle.opponent.active.ability = None

        self.user_active = Pokemon("weedle", 100)
        self.battle.user.active = self.user_active

        self.username = "CoolUsername"

        self.battle.username = self.username

    def test_adds_unburden_when_appropriate(self):
        self.battle.opponent.active.name = "hawlucha"
        self.battle.opponent.active.item = "sitrusberry"
        split_msg = ["", "-enditem", "p2a: Hawlucha", "Sitrus Berry"]

        remove_item(self.battle, split_msg)
        self.assertIn("unburden", self.battle.opponent.active.volatile_statuses)

    def test_basic_removes_item(self):
        self.battle.opponent.active.item = "airballoon"
        split_msg = ["", "-enditem", "p2a: Caterpie", "Air Balloon"]

        remove_item(self.battle, split_msg)
        self.assertEqual(None, self.battle.opponent.active.item)

    def test_sets_removed_item_when_item_ends(self):
        self.battle.opponent.active.item = "airballoon"
        split_msg = ["", "-enditem", "p2a: Caterpie", "Air Balloon"]

        remove_item(self.battle, split_msg)
        self.assertEqual("airballoon", self.battle.opponent.active.removed_item)


class TestImmune(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active
        self.battle.opponent.active.ability = None

        self.user_active = Pokemon("weedle", 100)
        self.battle.user.active = self.user_active

        self.username = "CoolUsername"

        self.battle.username = self.username

    def test_randbats_does_not_infer_zoroark_from_tera_immunity_on_judgment(self):
        self.battle.battle_type = BattleType.RANDOM_BATTLE
        self.battle.generation = "gen9"
        RandomBattleTeamDatasets.initialize("gen9randombattle")
        self.battle.opponent.reserve = []

        self.battle.opponent.active = Pokemon("enamorustherian", 83)
        self.battle.opponent.active.tera_type = "ground"
        self.battle.opponent.active.terastallized = True

        self.battle.user.active = Pokemon("arceuselectric", 70)
        self.battle.user.last_used_move = LastUsedMove("arceuselectric", "judgment", 0)
        split_msg = [
            "",
            "-immune",
            "p2a: Enamorus-Therian",
        ]
        immune(self.battle, split_msg)

        # tera-type renders immune to electric-judgment
        # make sure no zoroark-hisui is inferred here thinking judgment is a normal type
        self.assertEqual("enamorustherian", self.battle.opponent.active.name)
        self.assertEqual(0, len(self.battle.opponent.reserve))

    def test_randbats_infer_zoroark_from_immunity_when_in_reserves(self):
        self.battle.battle_type = BattleType.RANDOM_BATTLE
        self.battle.generation = "gen9"
        RandomBattleTeamDatasets.initialize("gen9randombattle")

        self.battle.opponent.reserve = [Pokemon("zoroarkhisui", 80)]
        self.battle.opponent.reserve[0].add_move("nastyplot")

        self.battle.opponent.active = Pokemon("gyarados", 100)
        self.battle.opponent.active.add_move("terablast")
        self.battle.opponent.active.moves_used_since_switch_in.add("terablast")
        self.battle.opponent.active.boosts[constants.SPECIAL_ATTACK] = 2

        self.battle.user.last_used_move = LastUsedMove("weedle", "shadowball", 0)
        split_msg = [
            "",
            "-immune",
            "p2a: Gyarados",
        ]
        immune(self.battle, split_msg)

        self.assertEqual("zoroarkhisui", self.battle.opponent.active.name)
        self.assertNotEqual(100, self.battle.opponent.active.level)

        # nastyplot was previously revealed on zoroarkhisui
        # terablast was used by gyarados since switching in, but should be re-associated with zoroarkhisui
        self.assertEqual(
            [Move("nastyplot"), Move("terablast")],
            self.battle.opponent.active.moves,
        )
        # the boosts that existed on gyarados should be on the active zoroarkhisui now
        self.assertEqual(
            {constants.SPECIAL_ATTACK: 2}, dict(self.battle.opponent.active.boosts)
        )

        self.assertEqual(1, len(self.battle.opponent.reserve))
        self.assertEqual("gyarados", self.battle.opponent.reserve[0].name)
        # terablast was used by gyarados since switching in so it should be dis-associated with gyarados
        self.assertEqual([], self.battle.opponent.reserve[0].moves)
        self.assertEqual({}, dict(self.battle.opponent.reserve[0].boosts))

    def test_randbats_infer_zoroarkhisui_from_immunity_when_not_in_reserves(self):
        self.battle.battle_type = BattleType.RANDOM_BATTLE
        self.battle.generation = "gen9"
        RandomBattleTeamDatasets.initialize("gen9randombattle")
        self.battle.opponent.reserve = []

        self.battle.opponent.active = Pokemon("gyarados", 100)
        self.battle.opponent.active.add_move("terablast")
        self.battle.opponent.active.moves_used_since_switch_in.add("terablast")
        self.battle.opponent.active.boosts[constants.SPECIAL_ATTACK] = 2

        self.battle.user.last_used_move = LastUsedMove("weedle", "shadowball", 0)
        split_msg = [
            "",
            "-immune",
            "p2a: Gyarados",
        ]
        immune(self.battle, split_msg)

        self.assertEqual("zoroarkhisui", self.battle.opponent.active.name)
        self.assertNotEqual(100, self.battle.opponent.active.level)

        self.assertEqual(
            [Move("terablast")],
            self.battle.opponent.active.moves,
        )
        self.assertEqual(
            {constants.SPECIAL_ATTACK: 2}, dict(self.battle.opponent.active.boosts)
        )

        self.assertEqual(1, len(self.battle.opponent.reserve))
        self.assertEqual("gyarados", self.battle.opponent.reserve[0].name)
        self.assertEqual([], self.battle.opponent.reserve[0].moves)
        self.assertEqual({}, dict(self.battle.opponent.reserve[0].boosts))

    def test_randbats_infer_zoroark_from_immunity_when_not_in_reserves(self):
        self.battle.battle_type = BattleType.RANDOM_BATTLE
        self.battle.generation = "gen9"
        RandomBattleTeamDatasets.initialize("gen9randombattle")
        self.battle.opponent.reserve = []

        self.battle.opponent.active = Pokemon("gyarados", 100)
        self.battle.opponent.active.add_move("terablast")
        self.battle.opponent.active.moves_used_since_switch_in.add("terablast")
        self.battle.opponent.active.boosts[constants.SPECIAL_ATTACK] = 2

        self.battle.user.last_used_move = LastUsedMove("weedle", "psychic", 0)
        split_msg = [
            "",
            "-immune",
            "p2a: Gyarados",
        ]
        immune(self.battle, split_msg)

        self.assertEqual("zoroark", self.battle.opponent.active.name)
        self.assertNotEqual(100, self.battle.opponent.active.level)

        self.assertEqual(
            [Move("terablast")],
            self.battle.opponent.active.moves,
        )
        self.assertEqual(
            {constants.SPECIAL_ATTACK: 2}, dict(self.battle.opponent.active.boosts)
        )

        self.assertEqual(1, len(self.battle.opponent.reserve))
        self.assertEqual("gyarados", self.battle.opponent.reserve[0].name)
        self.assertEqual([], self.battle.opponent.reserve[0].moves)
        self.assertEqual({}, dict(self.battle.opponent.reserve[0].boosts))

    def test_gen4_does_not_infer_zoroark(self):
        self.battle.battle_type = BattleType.RANDOM_BATTLE
        self.battle.generation = "gen4"
        RandomBattleTeamDatasets.initialize("gen4")
        self.battle.opponent.reserve = []

        self.battle.opponent.active = Pokemon("gyarados", 100)
        self.battle.opponent.active.add_move("terablast")
        self.battle.opponent.active.moves_used_since_switch_in.add("terablast")
        self.battle.opponent.active.boosts[constants.SPECIAL_ATTACK] = 2

        self.battle.user.last_used_move = LastUsedMove("weedle", "psychic", 0)
        split_msg = [
            "",
            "-immune",
            "p2a: Gyarados",
        ]
        immune(self.battle, split_msg)

        self.assertEqual("gyarados", self.battle.opponent.active.name)
        self.assertEqual(0, len(self.battle.opponent.reserve))

    def test_gen5_does_not_infer_zoroark_hisui(self):
        self.battle.battle_type = BattleType.RANDOM_BATTLE
        self.battle.generation = "gen5"
        RandomBattleTeamDatasets.initialize("gen5")
        self.battle.opponent.reserve = []

        self.battle.opponent.active = Pokemon("gyarados", 100)
        self.battle.opponent.active.add_move("terablast")
        self.battle.opponent.active.moves_used_since_switch_in.add("terablast")
        self.battle.opponent.active.boosts[constants.SPECIAL_ATTACK] = 2

        self.battle.user.last_used_move = LastUsedMove("weedle", "tackle", 0)
        split_msg = [
            "",
            "-immune",
            "p2a: Gyarados",
        ]
        immune(self.battle, split_msg)

        self.assertEqual("gyarados", self.battle.opponent.active.name)
        self.assertEqual(0, len(self.battle.opponent.reserve))

    def test_does_not_infer_zoroark_if_pkmn_terastallized_to_gain_immunity(self):
        self.battle.battle_type = BattleType.RANDOM_BATTLE
        self.battle.generation = "gen9"
        RandomBattleTeamDatasets.initialize("gen9randombattle")
        self.battle.opponent.reserve = []

        self.battle.opponent.active = Pokemon("gyarados", 100)
        self.battle.opponent.active.terastallized = True
        self.battle.opponent.active.tera_type = "dark"

        self.battle.user.last_used_move = LastUsedMove("weedle", "psychic", 0)
        split_msg = [
            "",
            "-immune",
            "p2a: Gyarados",
        ]
        immune(self.battle, split_msg)

        self.assertEqual("gyarados", self.battle.opponent.active.name)
        self.assertEqual(0, len(self.battle.opponent.reserve))

    def test_does_not_infer_zoroark_if_pkmn_naturally_immune(self):
        self.battle.battle_type = BattleType.RANDOM_BATTLE
        self.battle.generation = "gen9"
        RandomBattleTeamDatasets.initialize("gen9randombattle")
        self.battle.opponent.reserve = []

        self.battle.opponent.active = Pokemon("urshifu", 100)

        self.battle.user.last_used_move = LastUsedMove("weedle", "psychic", 0)
        split_msg = [
            "",
            "-immune",
            "p2a: Urshifu",
        ]
        immune(self.battle, split_msg)

        self.assertEqual("urshifu", self.battle.opponent.active.name)
        self.assertEqual(0, len(self.battle.opponent.reserve))

    def test_does_not_infer_zoroark_if_futuresight_ending(self):
        self.battle.battle_type = BattleType.RANDOM_BATTLE
        self.battle.generation = "gen9"
        RandomBattleTeamDatasets.initialize("gen9randombattle")
        self.battle.opponent.reserve = []

        self.battle.opponent.active = Pokemon("Urshifu", 100)
        self.battle.user.future_sight = (1, "weedle")

        self.battle.user.last_used_move = LastUsedMove("weedle", "tackle", 0)
        split_msg = [
            "",
            "-immune",
            "p2a: Urshifu",
        ]
        immune(self.battle, split_msg)

        self.assertEqual("urshifu", self.battle.opponent.active.name)
        self.assertEqual(0, len(self.battle.opponent.reserve))

    def test_infers_zoroark_from_immunity_that_pkmn_does_not_have(self):
        self.battle.battle_type = BattleType.BATTLE_FACTORY
        self.battle.generation = "gen9"
        TeamDatasets.initialize(
            "gen9battlefactory", ["zoroarkhisui", "gyarados"], "ru"
        )  # gen9 RU should always have these pokemon

        self.battle.opponent.reserve = [Pokemon("zoroarkhisui", 100)]
        self.battle.opponent.reserve[0].add_move("nastyplot")

        self.battle.opponent.active = Pokemon("gyarados", 100)
        self.battle.opponent.active.add_move("terablast")
        self.battle.opponent.active.moves_used_since_switch_in.add("terablast")
        self.battle.opponent.active.boosts[constants.SPECIAL_ATTACK] = 2

        self.battle.user.last_used_move = LastUsedMove("weedle", "shadowball", 0)
        split_msg = [
            "",
            "-immune",
            "p2a: Gyarados",
        ]  # Gyarados is not immune to shadowball
        immune(self.battle, split_msg)

        self.assertEqual("zoroarkhisui", self.battle.opponent.active.name)

        # nastyplot was previously revealed on zoroarkhisui
        # terablast was used by gyarados since switching in, but should be re-associated with zoroarkhisui
        self.assertEqual(
            [Move("nastyplot"), Move("terablast")],
            self.battle.opponent.active.moves,
        )
        # the boosts that existed on gyarados should be on the active zoroarkhisui now
        self.assertEqual(
            {constants.SPECIAL_ATTACK: 2}, dict(self.battle.opponent.active.boosts)
        )

        self.assertEqual("gyarados", self.battle.opponent.reserve[0].name)
        # terablast was used by gyarados since switching in so it should be dis-associated with gyarados
        self.assertEqual([], self.battle.opponent.reserve[0].moves)
        self.assertEqual({}, dict(self.battle.opponent.reserve[0].boosts))

    def test_does_not_infer_zoroark_when_tera_type_renders_it_immune(self):
        self.battle.battle_type = BattleType.BATTLE_FACTORY
        self.battle.generation = "gen9"
        TeamDatasets.initialize(
            "gen9battlefactory", ["zoroarkhisui", "gyarados"], "ru"
        )  # gen9 RU should always have these pokemon

        self.battle.opponent.reserve = [Pokemon("zoroarkhisui", 100)]

        self.battle.opponent.active = Pokemon("gyarados", 100)
        self.battle.opponent.active.tera_type = "ghost"
        self.battle.opponent.active.terastallized = True

        self.battle.user.last_used_move = LastUsedMove("weedle", "rapidspin", 0)
        split_msg = [
            "",
            "-immune",
            "p2a: Gyarados",
        ]  # Gyarados is immune to rapidspin when terastallized into a ghost type
        immune(self.battle, split_msg)

        # nothing changed
        self.assertEqual("gyarados", self.battle.opponent.active.name)
        self.assertEqual("zoroarkhisui", self.battle.opponent.reserve[0].name)

    def test_does_not_infer_zoroark_when_pkmn_is_actually_immune(self):
        self.battle.battle_type = BattleType.BATTLE_FACTORY
        self.battle.generation = "gen9"
        TeamDatasets.initialize(
            "gen9battlefactory", ["zoroarkhisui", "maushold"], "ru"
        )  # gen9 RU should always have these pokemon

        self.battle.opponent.reserve = [Pokemon("zoroarkhisui", 100)]
        self.battle.opponent.active = Pokemon("maushold", 100)

        self.battle.user.last_used_move = LastUsedMove("weedle", "shadowball", 0)
        split_msg = [
            "",
            "-immune",
            "p2a: Maushold",
        ]  # Maushold is immune to shadowball - no inferring zoroarkhisui
        immune(self.battle, split_msg)

        # did not change
        self.assertEqual("maushold", self.battle.opponent.active.name)
        self.assertEqual("zoroarkhisui", self.battle.opponent.reserve[0].name)

    def test_does_not_infer_zoroark_when_the_zoroark_is_not_immune(self):
        self.battle.battle_type = BattleType.BATTLE_FACTORY
        self.battle.generation = "gen9"
        TeamDatasets.initialize(
            "gen9battlefactory", ["zoroarkhisui", "salamence"], "ru"
        )  # gen9 RU should always have these pokemon

        self.battle.opponent.reserve = [Pokemon("zoroarkhisui", 100)]
        self.battle.opponent.active = Pokemon("salamence", 100)

        self.battle.user.last_used_move = LastUsedMove("weedle", "earthquake", 0)
        split_msg = [
            "",
            "-immune",
            "p2a: Salamence",
        ]  # Salamence is immune to earthquake - no inferring zoroarkhisui
        immune(self.battle, split_msg)

        # did not change
        self.assertEqual("salamence", self.battle.opponent.active.name)
        self.assertEqual("zoroarkhisui", self.battle.opponent.reserve[0].name)

    def test_does_not_infer_zoroark_when_ability_renders_immune(self):
        self.battle.battle_type = BattleType.BATTLE_FACTORY
        self.battle.generation = "gen9"
        TeamDatasets.initialize(
            "gen9battlefactory", ["zoroarkhisui", "rotomheat"], "ru"
        )  # gen9 RU should always have these pokemon

        self.battle.opponent.reserve = [Pokemon("zoroarkhisui", 100)]
        self.battle.opponent.active = Pokemon("rotomheat", 100)

        self.battle.user.last_used_move = LastUsedMove("weedle", "earthquake", 0)
        split_msg = [
            "",
            "-immune",
            "p2a: Rotom",
            "[from] ability: Levitate",
        ]  # rotomheat is immune to earthquake via levitate - no inferring zoroarkhisui
        immune(self.battle, split_msg)

        # did not change
        self.assertEqual("rotomheat", self.battle.opponent.active.name)
        self.assertEqual("zoroarkhisui", self.battle.opponent.reserve[0].name)

    def test_sets_ability_for_opponent(self):
        split_msg = ["", "-immune", "p2a: Caterpie", "[from] ability: Volt Absorb"]
        immune(self.battle, split_msg)

        expected_ability = "voltabsorb"

        self.assertEqual(expected_ability, self.battle.opponent.active.ability)

    def test_sets_ability_for_bot(self):
        split_msg = ["", "-immune", "p1a: Caterpie", "[from] ability: Volt Absorb"]
        immune(self.battle, split_msg)

        expected_ability = "voltabsorb"

        self.assertEqual(expected_ability, self.battle.user.active.ability)


class TestInactive(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active
        self.battle.opponent.active.ability = None

        self.user_active = Pokemon("weedle", 100)
        self.battle.user.active = self.user_active

        self.username = "CoolUsername"

        self.battle.username = self.username

    def test_sets_time_to_15_seconds(self):
        split_msg = ["", "inactive", "Time left: 135 sec this turn", "135 sec total"]
        inactive(self.battle, split_msg)

        self.assertEqual(135, self.battle.time_remaining)

    def test_sets_to_60_seconds(self):
        split_msg = ["", "inactive", "Time left: 60 sec this turn", "60 sec total"]
        inactive(self.battle, split_msg)

        self.assertEqual(60, self.battle.time_remaining)

    def test_capture_group_failing(self):
        self.battle.time_remaining = 1
        split_msg = ["", "inactive", "some random message"]
        inactive(self.battle, split_msg)

        self.assertEqual(1, self.battle.time_remaining)

    def test_capture_group_failing_but_message_starts_with_username(self):
        self.battle.time_remaining = 1
        split_msg = ["", "inactive", "Time left: some random message"]
        inactive(self.battle, split_msg)

        self.assertEqual(1, self.battle.time_remaining)

    def test_different_inactive_message_does_not_change_time(self):
        self.battle.time_remaining = 1
        split_msg = ["", "inactive", "Some Other Person has 10 seconds left"]
        inactive(self.battle, split_msg)

        self.assertEqual(1, self.battle.time_remaining)


class TestInactiveOff(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"

        self.opponent_active = Pokemon("caterpie", 100)
        self.battle.opponent.active = self.opponent_active
        self.battle.opponent.active.ability = None

        self.user_active = Pokemon("caterpie", 100)
        self.battle.user.active = self.user_active
        self.battle.user.active.previous_hp = self.battle.user.active.hp

        self.username = "CoolUsername"

        self.battle.username = self.username

        self.battle.user.last_used_move = LastUsedMove("caterpie", "tackle", 0)

        self.battle.request_json = {
            constants.ACTIVE: [{constants.MOVES: []}],
            constants.SIDE: {
                constants.ID: None,
                constants.NAME: None,
                constants.POKEMON: [],
                constants.RQID: None,
            },
        }

    def test_turns_timer_off(self):
        self.battle.time_remaining = 60
        self.battle.msg_list = [
            "|move|p2a: Caterpie|Tackle|",
            "|-damage|p1a: Caterpie|186/252",
            "|move|p1a: Caterpie|Tackle|",
            "|-damage|p2a: Caterpie|85/100",
            "|upkeep",
            "|inactiveoff|Battle timer is now OFF.",  # this line is being tested
            "|turn|4",
        ]
        process_battle_updates(self.battle)
        self.assertIsNone(self.battle.time_remaining)


class TestGetDamageDealt(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)

        self.battle.user.name = "p1"
        self.battle.user.active = Pokemon("Caterpie", 100)

        self.battle.opponent.name = "p2"
        self.battle.opponent.active = Pokemon("Pikachu", 100)

    def test_assigns_damage_dealt_from_opponent_to_bot(self):
        self.battle.user.active.max_hp = 250
        self.battle.user.active.hp = 250

        messages = [
            "|move|p2a: Pikachu|Tackle|p1a: Caterpie",
            "|-damage|p1a: Caterpie|200/250",  # 50 / 250 = 0.20 of total health
            "|",
            "|move|p1a: Caterpie|Tackle|p2a: Pikachu",
            "|-damage|p2a: Pikachu|90/100",
        ]

        split_msg = messages[0].split("|")

        damage_dealt = get_damage_dealt(self.battle, split_msg, messages[1:])

        expected_damage_amount_dealt = DamageDealt(
            attacker="pikachu",
            defender="caterpie",
            move="tackle",
            percent_damage=0.20,
            crit=False,
            exact_damage=50,
        )
        self.assertEqual(expected_damage_amount_dealt, damage_dealt)

    def test_assigns_damage_when_bots_pokemon_has_no_last_used_move(self):
        self.battle.user.active.max_hp = 250
        self.battle.user.active.hp = 250

        messages = [
            "|move|p2a: Pikachu|Tackle|p1a: Caterpie",
            "|-damage|p1a: Caterpie|200/250",  # 50 / 250 = 0.20 of total health
            "|",
            "|move|p1a: Caterpie|Tackle|p2a: Pikachu",
            "|-damage|p2a: Pikachu|90/100",
        ]

        split_msg = messages[0].split("|")

        damage_dealt = get_damage_dealt(self.battle, split_msg, messages[1:])

        expected_damage_amount_dealt = DamageDealt(
            attacker="pikachu",
            defender="caterpie",
            move="tackle",
            percent_damage=0.20,
            crit=False,
            exact_damage=50,
        )
        self.assertEqual(expected_damage_amount_dealt, damage_dealt)

    def test_supereffective_damage_is_captured(self):
        self.battle.user.active.max_hp = 250
        self.battle.user.active.hp = 250

        messages = [
            "|move|p2a: Pikachu|Tackle|p1a: Caterpie",
            "|supereffective|p1a: Caterpie",
            "|-damage|p1a: Caterpie|100/250",  # 150 / 250 = 0.60 of total health
            "|",
            "|move|p1a: Caterpie|Tackle|p2a: Pikachu",
            "|-damage|p2a: Pikachu|90/100",
        ]

        split_msg = messages[0].split("|")

        damage_dealt = get_damage_dealt(self.battle, split_msg, messages[1:])

        expected_damage_amount_dealt = DamageDealt(
            attacker="pikachu",
            defender="caterpie",
            move="tackle",
            percent_damage=0.60,
            crit=False,
            exact_damage=150,
        )
        self.assertEqual(expected_damage_amount_dealt, damage_dealt)

    def test_crit_sets_crit_flag(self):
        self.battle.user.active.max_hp = 250
        self.battle.user.active.hp = 250

        messages = [
            "|move|p2a: Pikachu|Tackle|p1a: Caterpie",
            "|-crit|p1a: Caterpie",  # should set crit to True
            "|-damage|p1a: Caterpie|100/250",  # 150 / 250 = 0.60 of total health
            "|",
            "|move|p1a: Caterpie|Tackle|p2a: Pikachu",
            "|-damage|p2a: Pikachu|90/100",
        ]

        split_msg = messages[0].split("|")

        damage_dealt = get_damage_dealt(self.battle, split_msg, messages[1:])

        expected_damage_amount_dealt = DamageDealt(
            attacker="pikachu",
            defender="caterpie",
            move="tackle",
            percent_damage=0.60,
            crit=True,
            exact_damage=150,
        )
        self.assertEqual(expected_damage_amount_dealt, damage_dealt)

    def test_stop_after_the_end_of_this_move(self):
        self.battle.user.active.max_hp = 250
        self.battle.user.active.hp = 250

        messages = [
            "|move|p2a: Pikachu|Tackle|p1a: Caterpie",
            "|-damage|p1a: Caterpie|200/250",  # 50 / 250 = 0.20 of total health
            "|",
            "|move|p1a: Caterpie|Tackle|p2a: Pikachu",
            "|-damage|p2a: Pikachu|90/100",
        ]

        split_msg = messages[0].split("|")

        damage_dealt = get_damage_dealt(self.battle, split_msg, messages[1:])

        expected_damage_amount_dealt = DamageDealt(
            attacker="pikachu",
            defender="caterpie",
            move="tackle",
            percent_damage=0.20,
            crit=False,
            exact_damage=50,
        )
        self.assertEqual(expected_damage_amount_dealt, damage_dealt)

    def test_does_not_assign_anything_when_move_does_no_damage(self):
        self.battle.user.active.max_hp = 250
        self.battle.user.active.hp = 250

        messages = [
            "|move|p2a: Pikachu|Recover|p2a: Pikachu",
            "|-heal|p2a: Pikachu|200/250",
        ]

        split_msg = messages[0].split("|")

        damage_dealt = get_damage_dealt(self.battle, split_msg, messages[1:])
        self.assertIsNone(damage_dealt)

    def test_does_not_catch_second_moves_damage_after_a_heal(self):
        self.battle.user.active.max_hp = 250
        self.battle.user.active.hp = 250

        messages = [
            "|move|p2a: Pikachu|Recover|p2a: Pikachu",
            "|-heal|p2a: Pikachu|200/250",
            "|move|p1a: Caterpie|Tackle|p2a: Pikachu",
            "|-damage|p2a: Pikachu|90/100",
        ]

        split_msg = messages[0].split("|")

        damage_dealt = get_damage_dealt(self.battle, split_msg, messages[1:])
        self.assertIsNone(damage_dealt)

    def test_does_not_set_damage_when_status_move_occurs(self):
        self.battle.user.active.max_hp = 250
        self.battle.user.active.hp = 250

        messages = [
            "|move|p2a: Pikachu|Thunder Wave|p1a: Caterpie",
            "|-status|p1a: Caterpie|par",
        ]

        split_msg = messages[0].split("|")

        damage_dealt = get_damage_dealt(self.battle, split_msg, messages[1:])
        self.assertIsNone(damage_dealt)

    def test_assigns_damage_from_move_that_causes_status_as_secondary(self):
        self.battle.user.active.max_hp = 250
        self.battle.user.active.hp = 250

        messages = [
            "|move|p2a: Pikachu|Thunderbolt|p1a: Caterpie",
            "|-damage|p1a: Caterpie|200/250",  # 50 / 250 = 0.20 of total health
            "|-status|p1a: Caterpie|par",
        ]

        split_msg = messages[0].split("|")

        damage_dealt = get_damage_dealt(self.battle, split_msg, messages[1:])

        expected_damage_amount_dealt = DamageDealt(
            attacker="pikachu",
            defender="caterpie",
            move="thunderbolt",
            percent_damage=0.20,
            crit=False,
            exact_damage=50,
        )
        self.assertEqual(expected_damage_amount_dealt, damage_dealt)

    def test_assigns_damage_to_bot_on_faint(self):
        self.battle.user.active.max_hp = 250
        self.battle.user.active.hp = 250

        self.battle.user.active.hp = 1

        messages = [
            "|move|p2a: Pikachu|Tackle|p1a: Caterpie",
            "|-damage|p1a: Caterpie|0 fnt",  # 1 / 250 of health was done
            "|faint|p1a: Caterpie",
        ]

        split_msg = messages[0].split("|")

        damage_dealt = get_damage_dealt(self.battle, split_msg, messages[1:])

        expected_damage_amount_dealt = DamageDealt(
            attacker="pikachu",
            defender="caterpie",
            move="tackle",
            percent_damage=1 / 250,
            crit=False,
            exact_damage=1,
            lethal=True,
        )
        self.assertEqual(expected_damage_amount_dealt, damage_dealt)

    def test_assigns_damage_to_opponent_on_faint(self):
        self.battle.opponent.active.max_hp = 250
        self.battle.opponent.active.hp = 2.5

        messages = [
            "|move|p1a: Caterpie|Tackle|p2a: Pikachu",
            "|-damage|p2a: Pikachu|0 fnt",
            "|faint|p2a: Pikachu",
        ]

        split_msg = messages[0].split("|")

        damage_dealt = get_damage_dealt(self.battle, split_msg, messages[1:])

        expected_damage_amount_dealt = DamageDealt(
            attacker="caterpie",
            defender="pikachu",
            move="tackle",
            percent_damage=0.01,
            crit=False,
            lethal=True,
        )
        self.assertEqual(expected_damage_amount_dealt, damage_dealt)

    def test_assigns_damage_to_opponent_on_faint_from_1_hp(self):
        self.battle.opponent.active.max_hp = 250
        self.battle.opponent.active.hp = 250

        self.battle.opponent.active.hp = 1

        messages = [
            "|move|p1a: Caterpie|Tackle|p2a: Pikachu",
            "|-damage|p2a: Pikachu|0 fnt",  # 1 / 250 of health was done
            "|faint|p1a: Pikachu",
        ]

        split_msg = messages[0].split("|")

        damage_dealt = get_damage_dealt(self.battle, split_msg, messages[1:])

        expected_damage_amount_dealt = DamageDealt(
            attacker="caterpie",
            defender="pikachu",
            move="tackle",
            percent_damage=1 / 250,
            crit=False,
            lethal=True,
        )
        self.assertEqual(expected_damage_amount_dealt, damage_dealt)

    def test_assigns_nothing_on_substitute(self):
        self.battle.user.active.max_hp = 100
        self.battle.user.active.hp = 100

        self.battle.opponent.active.hp = 100
        self.battle.user.active.hp = 100

        messages = [
            "|move|p2a: Pikachu|Substitute|p2a: Pikachu",
            "|-start|p2a: Pikachu|Substitute",
            "|-damage|p2a: Pikachu|75/100",  # damage from sub should not be caught
        ]

        split_msg = messages[0].split("|")

        damage_dealt = get_damage_dealt(self.battle, split_msg, messages[1:])
        self.assertIsNone(damage_dealt)

    def test_lifeorb_does_not_assign_damage(self):
        self.battle.user.active.max_hp = 250
        self.battle.user.active.hp = 250

        messages = [
            "|move|p2a: Pikachu|Tackle|p1a: Caterpie",
            "|-damage|p1a: Caterpie|200/250",  # 0.2 of total health
            "|-damage|p2a: Pikachu|90/100|[from] item: Life Orb",
        ]

        split_msg = messages[0].split("|")

        damage_dealt = get_damage_dealt(self.battle, split_msg, messages[1:])

        expected_damage_dealt = DamageDealt(
            attacker="pikachu",
            defender="caterpie",
            move="tackle",
            percent_damage=0.20,
            crit=False,
            exact_damage=50,
        )
        self.assertEqual(damage_dealt, expected_damage_dealt)

    def test_doing_damage_to_opponent_gets_correct_percentage(self):
        # start at 100% health
        self.battle.opponent.active.max_hp = 250
        self.battle.opponent.active.hp = 250

        messages = [
            "|move|p1a: Caterpie|Tackle|p2a: Pikachu",
            "|-damage|p2a: Pikachu|85/100",  # 0.15 of total health
        ]

        split_msg = messages[0].split("|")

        damage_dealt = get_damage_dealt(self.battle, split_msg, messages[1:])

        expected_damage_dealt = DamageDealt(
            attacker="caterpie",
            defender="pikachu",
            move="tackle",
            percent_damage=0.15,
            crit=False,
        )
        self.assertEqual(expected_damage_dealt, damage_dealt)

    def test_entire_message_finishing(self):
        # start at 100% health
        self.battle.opponent.active.max_hp = 250
        self.battle.opponent.active.hp = 250

        messages = [
            "|move|p1a: Caterpie|Parting Shot|p2a: Pikachu",
            "|-unboost|p2a: Pikachu|atk|1",
            "|-unboost|p2a: Pikachu|spa|1",
            "",
        ]

        split_msg = messages[0].split("|")

        damage_dealt = get_damage_dealt(self.battle, split_msg, messages[1:])

        self.assertIsNone(damage_dealt)


class TestNoInit(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)

        self.battle.user.name = "p1"
        self.battle.user.active = Pokemon("Caterpie", 100)

        self.battle.opponent.name = "p2"
        self.battle.opponent.active = Pokemon("Pikachu", 100)

    def test_renames_battle_when_rename_message_occurs(self):
        self.battle.battle_tag = "original_tag"
        new_battle_tag = "new_battle_tag"

        self.battle.msg_list = ["|noinit|rename|{}".format(new_battle_tag)]

        process_battle_updates(self.battle)

        self.assertEqual(self.battle.battle_tag, new_battle_tag)


class TestItemTransferLines(unittest.TestCase):
    """`|-item|` names only the RECEIVER of an item transfer.  Without the donor half the
    reconstruction keeps handing the loser an item it no longer has (synth10305: Basculin
    kept the Choice Band Hoopa's Magician stole; synth33779: Cyclizar's Tricked Choice
    Band was reset to a guess and then refilled from the sidecar)."""

    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.user.active = Pokemon("hoopa", 80)
        self.battle.opponent.active = Pokemon("basculin", 80)

    def test_magician_steal_empties_the_of_victim(self):
        self.battle.opponent.active.item = "choiceband"

        set_item(
            self.battle,
            [
                "",
                "-item",
                "p1a: Hoopa",
                "Choice Band",
                "[from] ability: Magician",
                "[of] p2a: Basculin",
            ],
        )

        self.assertEqual("choiceband", self.battle.user.active.item)
        self.assertIsNone(self.battle.opponent.active.item)
        self.assertEqual("choiceband", self.battle.opponent.active.removed_item)

    def test_frisk_is_a_reveal_and_transfers_nothing(self):
        self.battle.opponent.active.item = "heavydutyboots"

        set_item(
            self.battle,
            [
                "",
                "-item",
                "p2a: Basculin",
                "Heavy-Duty Boots",
                "[from] ability: Frisk",
                "[of] p1a: Hoopa",
            ],
        )

        self.assertEqual("heavydutyboots", self.battle.opponent.active.item)
        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.user.active.item)

    def test_trick_swapping_the_same_item_leaves_both_sides_holding_it(self):
        """PS emits ONE `-item` per side for a Trick (data/moves.ts:19888-19899), so
        the donor half never has to be inferred -- and inferring it broke the
        SAME-ITEM swap that randbats produces constantly.  The old rule ("the other
        side's active is the donor if it still holds the item just announced") fired
        on the second line and stripped the mon the FIRST line had just given the
        item to, leaving it item-less for the rest of the reconstruction (synth02950
        Golduck/Espeon Choice Specs, synth00260 Choice Scarf, synth27809 Choice
        Specs, synth39679 Leftovers)."""
        self.battle.user.active.item = "choicespecs"
        self.battle.opponent.active.item = "choicespecs"

        set_item(
            self.battle,
            ["", "-item", "p2a: Basculin", "Choice Specs", "[from] move: Trick"],
        )
        set_item(
            self.battle,
            ["", "-item", "p1a: Hoopa", "Choice Specs", "[from] move: Trick"],
        )

        self.assertEqual("choicespecs", self.battle.opponent.active.item)
        self.assertEqual("choicespecs", self.battle.user.active.item)

    def test_trick_swapping_different_items_ends_with_each_side_on_the_others(self):
        self.battle.user.active.item = "choiceband"
        self.battle.opponent.active.item = "leftovers"

        set_item(
            self.battle,
            ["", "-item", "p2a: Basculin", "Choice Band", "[from] move: Trick"],
        )
        set_item(
            self.battle,
            ["", "-item", "p1a: Hoopa", "Leftovers", "[from] move: Trick"],
        )

        self.assertEqual("choiceband", self.battle.opponent.active.item)
        self.assertEqual("leftovers", self.battle.user.active.item)

    def test_trick_donor_that_receives_nothing_is_cleared_by_its_own_enditem(self):
        """The half of a Trick that ends up empty-handed is not silent: PS gives it
        `-enditem <lost item>|[silent]|[from] move: Trick` (data/moves.ts:19892 and
        :19898), which `remove_item` already handles.  That is why `set_item` no
        longer has to guess a donor."""
        self.battle.user.active.item = "choiceband"

        set_item(
            self.battle,
            ["", "-item", "p2a: Basculin", "Choice Band", "[from] move: Trick"],
        )
        remove_item(
            self.battle,
            [
                "",
                "-enditem",
                "p1a: Hoopa",
                "Choice Band",
                "[silent]",
                "[from] move: Trick",
            ],
        )

        self.assertEqual("choiceband", self.battle.opponent.active.item)
        self.assertIsNone(self.battle.user.active.item)
        self.assertEqual("choiceband", self.battle.user.active.removed_item)

    def test_bestow_empties_the_of_giver(self):
        """PS names the GIVER in Bestow's `[of]` tag and emits no `-enditem` for it
        (data/moves.ts:1257), so it belongs with the steals, not the swaps."""
        self.battle.user.active.item = "leftovers"

        set_item(
            self.battle,
            [
                "",
                "-item",
                "p2a: Basculin",
                "Leftovers",
                "[from] move: Bestow",
                "[of] p1a: Hoopa",
            ],
        )

        self.assertEqual("leftovers", self.battle.opponent.active.item)
        self.assertIsNone(self.battle.user.active.item)
        self.assertEqual("leftovers", self.battle.user.active.removed_item)

    def test_a_protocol_item_is_no_longer_a_guess(self):
        # the stale `item_inferred` flag let the "used two different moves" rule (and the
        # exact-teams sidecar) overwrite a Tricked Choice Band
        self.battle.opponent.active.item_inferred = True

        set_item(
            self.battle,
            ["", "-item", "p2a: Basculin", "Choice Band", "[from] move: Trick"],
        )

        self.assertFalse(self.battle.opponent.active.item_inferred)


class TestStruggleLastUsedMove(unittest.TestCase):
    """PS calls `moveUsed()` for Struggle like any other move, and Encore reads it to
    FAIL (struggle carries `failencore` and owns no moveSlot, data/moves.ts:18205ff and
    :4725ff).  The handler used to return before recording it, leaving the PREVIOUS move
    standing (synth30440 T20, synth46824 T60)."""

    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.user.active = Pokemon("uxie", 83)
        self.battle.opponent.active = Pokemon("gogoat", 80)
        self.battle.turn = 19

    def test_struggle_becomes_the_last_used_move(self):
        self.battle.opponent.last_used_move = LastUsedMove("gogoat", "milkdrink", 18)

        move(self.battle, ["", "move", "p2a: Gogoat", "Struggle", "p1a: Uxie"])

        self.assertEqual("struggle", self.battle.opponent.last_used_move.move)
        self.assertEqual(19, self.battle.opponent.last_used_move.turn)

    def test_struggle_is_still_not_added_to_the_moveset(self):
        move(self.battle, ["", "move", "p2a: Gogoat", "Struggle", "p1a: Uxie"])

        self.assertNotIn(
            "struggle", [m.name for m in self.battle.opponent.active.moves]
        )


class TestOncePerBattleAbilityUsed(unittest.TestCase):
    """PS fires Intrepid Sword / Dauntless Shield / Battle Bond at most ONCE PER
    BATTLE (data/abilities.ts:2204-2208 swordBoost, :844-848 shieldBoost,
    :353-360 bondTriggered) and the flag survives switches. Seeing the protocol
    line is the proof the trigger is spent; the engine binding hard-coded the
    flag False until the binding-completeness pass, so search re-armed all three
    at every root."""

    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.opponent.active = Pokemon("zacian", 100)
        self.battle.user.active = Pokemon("zamazenta", 100)

    def test_intrepid_sword_ability_line_marks_trigger_spent(self):
        self.assertFalse(self.battle.opponent.active.once_per_battle_ability_used)
        update_ability(
            self.battle, ["", "-ability", "p2a: Zacian", "Intrepid Sword", "boost"]
        )
        self.assertTrue(self.battle.opponent.active.once_per_battle_ability_used)

    def test_dauntless_shield_ability_line_marks_trigger_spent(self):
        update_ability(
            self.battle, ["", "-ability", "p1a: Zamazenta", "Dauntless Shield", "boost"]
        )
        self.assertTrue(self.battle.user.active.once_per_battle_ability_used)

    def test_battle_bond_activate_line_marks_trigger_spent(self):
        self.battle.opponent.active = Pokemon("greninja", 100)
        activate(
            self.battle, ["", "-activate", "p2a: Greninja", "ability: Battle Bond"]
        )
        self.assertTrue(self.battle.opponent.active.once_per_battle_ability_used)

    def test_other_abilities_do_not_mark_the_trigger(self):
        update_ability(self.battle, ["", "-ability", "p2a: Zacian", "Pressure"])
        self.assertFalse(self.battle.opponent.active.once_per_battle_ability_used)


class TestStellarBoostedTypes(unittest.TestCase):
    """PS spends a Stellar-tera'd pkmn's one-time per-move-type boost inside the
    damage calculation (pokemon.stellarBoostedTypes, sim/pokemon.ts:264; pushed
    at sim/battle-actions.ts:1778-1784). Terapagos-Stellar and Stellar-typed
    moves never spend it, and a move that never reaches the damage calculation
    (miss / fail / immune) does not either."""

    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.opponent.active = Pokemon("caterpie", 100)
        self.user_active = Pokemon("dragonite", 100)
        self.user_active.terastallized = True
        self.user_active.tera_type = "stellar"
        self.battle.user.active = self.user_active

    def test_damaging_move_spends_the_boost_for_its_type(self):
        move(self.battle, ["", "move", "p1a: Dragonite", "Extreme Speed", "p2a: Caterpie"])
        self.assertIn("normal", self.user_active.stellar_boosted_types)

    def test_non_terastallized_pkmn_spends_nothing(self):
        self.user_active.terastallized = False
        move(self.battle, ["", "move", "p1a: Dragonite", "Extreme Speed", "p2a: Caterpie"])
        self.assertEqual(set(), self.user_active.stellar_boosted_types)

    def test_non_stellar_tera_spends_nothing(self):
        self.user_active.tera_type = "normal"
        move(self.battle, ["", "move", "p1a: Dragonite", "Extreme Speed", "p2a: Caterpie"])
        self.assertEqual(set(), self.user_active.stellar_boosted_types)

    def test_status_move_spends_nothing(self):
        move(self.battle, ["", "move", "p1a: Dragonite", "Dragon Dance", "p2a: Caterpie"])
        self.assertEqual(set(), self.user_active.stellar_boosted_types)

    def test_terapagos_stellar_never_spends(self):
        self.user_active = Pokemon("terapagosstellar", 100)
        self.user_active.terastallized = True
        self.user_active.tera_type = "stellar"
        self.battle.user.active = self.user_active
        move(self.battle, ["", "move", "p1a: Terapagos", "Tera Starstorm", "p2a: Caterpie"])
        self.assertEqual(set(), self.user_active.stellar_boosted_types)

    def test_miss_rolls_the_spend_back(self):
        move(self.battle, ["", "move", "p1a: Dragonite", "Extreme Speed", "p2a: Caterpie"])
        self.assertIn("normal", self.user_active.stellar_boosted_types)
        miss(self.battle, ["", "-miss", "p1a: Dragonite", "p2a: Caterpie"])
        self.assertEqual(set(), self.user_active.stellar_boosted_types)

    def test_immune_target_rolls_the_spend_back(self):
        # the -immune line names the TARGET; the spend belongs to the attacker
        move(self.battle, ["", "move", "p1a: Dragonite", "Extreme Speed", "p2a: Caterpie"])
        self.assertIn("normal", self.user_active.stellar_boosted_types)
        immune(self.battle, ["", "-immune", "p2a: Caterpie"])
        self.assertEqual(set(), self.user_active.stellar_boosted_types)

    def test_fail_rolls_the_spend_back(self):
        move(self.battle, ["", "move", "p1a: Dragonite", "Extreme Speed", "p2a: Caterpie"])
        fail(self.battle, ["", "-fail", "p1a: Dragonite"])
        self.assertEqual(set(), self.user_active.stellar_boosted_types)

    def test_second_move_of_a_spent_type_leaves_the_set_alone(self):
        move(self.battle, ["", "move", "p1a: Dragonite", "Extreme Speed", "p2a: Caterpie"])
        move(self.battle, ["", "move", "p1a: Dragonite", "Body Slam", "p2a: Caterpie"])
        self.assertEqual({"normal"}, self.user_active.stellar_boosted_types)
        # the second (already-spent) use has nothing pending, so a later miss
        # cannot un-spend the type
        miss(self.battle, ["", "-miss", "p1a: Dragonite", "p2a: Caterpie"])
        self.assertEqual({"normal"}, self.user_active.stellar_boosted_types)


class TestNegativeArmItemEliminations(unittest.TestCase):
    """Item eliminations from an effect ending exactly on its base schedule."""

    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.battle_type = BattleType.RANDOM_BATTLE

        self.battle.opponent.active = Pokemon("caterpie", 100)
        self.battle.opponent.active.ability = None
        self.battle.user.active = Pokemon("caterpie", 100)
        self.battle.user.last_selected_move = LastUsedMove("caterpie", "tackle", 0)
        self.battle.request_json = {
            constants.ACTIVE: [{constants.MOVES: []}],
            constants.SIDE: {
                constants.ID: None,
                constants.NAME: None,
                constants.POKEMON: [],
                constants.RQID: None,
            },
        }

    def test_opponent_moving_second_rules_out_choicescarf(self):
        # a scarfed caterpie (max 207 * 1.5) would have outsped 100
        self.battle.user.active.stats[constants.SPEED] = 100

        messages = [
            "|move|p1a: Caterpie|Tackle|p2a: Caterpie",
            "|move|p2a: Caterpie|Tackle|p1a: Caterpie",
        ]

        check_choicescarf(self.battle, messages)

        self.assertIn("choicescarf", self.battle.opponent.active.impossible_items)
        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    def test_opponent_moving_second_when_far_slower_does_not_rule_out_choicescarf(self):
        # even scarfed, caterpie could not reach 1000: moving second is expected
        self.battle.user.active.stats[constants.SPEED] = 1000

        messages = [
            "|move|p1a: Caterpie|Tackle|p2a: Caterpie",
            "|move|p2a: Caterpie|Tackle|p1a: Caterpie",
        ]

        check_choicescarf(self.battle, messages)

        self.assertNotIn("choicescarf", self.battle.opponent.active.impossible_items)

    def test_weather_ending_on_schedule_rules_out_the_rock(self):
        self.battle.weather = constants.RAIN
        self.battle.weather_source = "opponent:caterpie"
        self.battle.weather_turns_remaining = 1

        weather(self.battle, ["", "-weather", "none"])

        self.assertIn("damprock", self.battle.opponent.active.impossible_items)

    def test_weather_ending_early_does_not_rule_out_the_rock(self):
        self.battle.weather = constants.RAIN
        self.battle.weather_source = "opponent:caterpie"
        self.battle.weather_turns_remaining = 3

        weather(self.battle, ["", "-weather", "none"])

        self.assertNotIn("damprock", self.battle.opponent.active.impossible_items)

    def test_screen_ending_on_schedule_rules_out_lightclay(self):
        sidestart(self.battle, ["", "-sidestart", "p2", "Reflect"])
        self.assertEqual(5, self.battle.opponent.side_conditions[constants.REFLECT])
        self.battle.opponent.side_conditions[constants.REFLECT] = 1

        sideend(self.battle, ["", "-sideend", "p2", "Reflect"])

        self.assertIn("lightclay", self.battle.opponent.active.impossible_items)

    def test_screen_broken_early_does_not_rule_out_lightclay(self):
        sidestart(self.battle, ["", "-sidestart", "p2", "Reflect"])
        self.battle.opponent.side_conditions[constants.REFLECT] = 1

        sideend(
            self.battle,
            ["", "-sideend", "p2", "Reflect", "[from] move: Brick Break"],
        )

        self.assertNotIn("lightclay", self.battle.opponent.active.impossible_items)

    def test_screen_outlasting_five_turns_infers_lightclay(self):
        sidestart(self.battle, ["", "-sidestart", "p2", "Reflect"])
        self.battle.opponent.side_conditions[constants.REFLECT] = 1

        upkeep(self.battle, "")

        self.assertEqual("lightclay", self.battle.opponent.active.item)

    def test_screen_setter_is_remembered_after_it_leaves_the_field(self):
        sidestart(self.battle, ["", "-sidestart", "p2", "Light Screen"])
        setter = self.battle.opponent.active
        self.battle.opponent.reserve = [setter]
        self.battle.opponent.active = Pokemon("weedle", 100)
        self.battle.opponent.side_conditions[constants.LIGHT_SCREEN] = 1

        sideend(self.battle, ["", "-sideend", "p2", "Light Screen"])

        self.assertIn("lightclay", setter.impossible_items)
        self.assertNotIn("lightclay", self.battle.opponent.active.impossible_items)


class TestConsumableNonActivationMining(unittest.TestCase):
    """Non-activation of a consumable is a free elimination."""

    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.battle_type = BattleType.RANDOM_BATTLE
        self.battle.turn = 3

        self.battle.opponent.active = Pokemon("caterpie", 100)
        self.battle.opponent.active.ability = None
        self.battle.user.active = Pokemon("weedle", 100)
        self.battle.user.active.ability = "shielddust"
        self.battle.user.last_used_move = LastUsedMove("weedle", "tackle", 1)

    def test_sitrusberry_ruled_out_below_half_at_upkeep(self):
        self.battle.opponent.active.hp = int(
            self.battle.opponent.active.max_hp * 0.4
        )
        upkeep(self.battle, "")
        self.assertIn("sitrusberry", self.battle.opponent.active.impossible_items)

    def test_sitrusberry_not_ruled_out_above_half(self):
        self.battle.opponent.active.hp = int(
            self.battle.opponent.active.max_hp * 0.6
        )
        upkeep(self.battle, "")
        self.assertNotIn("sitrusberry", self.battle.opponent.active.impossible_items)

    def test_sitrusberry_not_ruled_out_under_unnerve(self):
        self.battle.user.active.ability = "unnerve"
        self.battle.opponent.active.hp = int(
            self.battle.opponent.active.max_hp * 0.4
        )
        upkeep(self.battle, "")
        self.assertNotIn("sitrusberry", self.battle.opponent.active.impossible_items)

    def test_custapberry_ruled_out_when_moving_below_quarter_hp(self):
        self.battle.opponent.active.hp = int(
            self.battle.opponent.active.max_hp * 0.2
        )
        check_opponent_custapberry(
            self.battle, ["", "move", "p2a: Caterpie", "Tackle", "p1a: Weedle"]
        )
        self.assertIn("custapberry", self.battle.opponent.active.impossible_items)

    def test_custapberry_not_ruled_out_when_we_already_moved_this_turn(self):
        self.battle.user.last_used_move = LastUsedMove("weedle", "tackle", 3)
        self.battle.opponent.active.hp = int(
            self.battle.opponent.active.max_hp * 0.2
        )
        check_opponent_custapberry(
            self.battle, ["", "move", "p2a: Caterpie", "Tackle", "p1a: Weedle"]
        )
        self.assertNotIn("custapberry", self.battle.opponent.active.impossible_items)

    def test_rockyhelmet_ruled_out_on_unanswered_contact_move(self):
        check_opponent_reactive_items(
            self.battle,
            ["", "move", "p1a: Weedle", "Tackle", "p2a: Caterpie"],
            ["|-damage|p2a: Caterpie|150/255"],
        )
        self.assertIn("rockyhelmet", self.battle.opponent.active.impossible_items)

    def test_rockyhelmet_not_ruled_out_when_it_activates(self):
        check_opponent_reactive_items(
            self.battle,
            ["", "move", "p1a: Weedle", "Tackle", "p2a: Caterpie"],
            [
                "|-damage|p2a: Caterpie|150/255",
                "|-damage|p1a: Weedle|200/255|[from] item: Rocky Helmet|[of] p2a: Caterpie",
            ],
        )
        self.assertNotIn("rockyhelmet", self.battle.opponent.active.impossible_items)

    def test_rockyhelmet_not_ruled_out_behind_a_substitute(self):
        check_opponent_reactive_items(
            self.battle,
            ["", "move", "p1a: Weedle", "Tackle", "p2a: Caterpie"],
            [
                "|-activate|p2a: Caterpie|move: Substitute|[damage]",
                "|-damage|p2a: Caterpie|150/255",
            ],
        )
        self.assertNotIn("rockyhelmet", self.battle.opponent.active.impossible_items)

    def test_weaknesspolicy_ruled_out_on_unanswered_super_effective_hit(self):
        check_opponent_reactive_items(
            self.battle,
            ["", "move", "p1a: Weedle", "Ember", "p2a: Caterpie"],
            [
                "|-supereffective|p2a: Caterpie",
                "|-damage|p2a: Caterpie|150/255",
            ],
        )
        self.assertIn("weaknesspolicy", self.battle.opponent.active.impossible_items)
        # ember is not a contact move
        self.assertNotIn("rockyhelmet", self.battle.opponent.active.impossible_items)

    def test_weaknesspolicy_not_ruled_out_when_it_activates(self):
        check_opponent_reactive_items(
            self.battle,
            ["", "move", "p1a: Weedle", "Ember", "p2a: Caterpie"],
            [
                "|-supereffective|p2a: Caterpie",
                "|-damage|p2a: Caterpie|150/255",
                "|-boost|p2a: Caterpie|atk|2|[from] item: Weakness Policy",
                "|-enditem|p2a: Caterpie|Weakness Policy",
            ],
        )
        self.assertNotIn("weaknesspolicy", self.battle.opponent.active.impossible_items)

    def test_nothing_ruled_out_when_the_target_faints(self):
        check_opponent_reactive_items(
            self.battle,
            ["", "move", "p1a: Weedle", "Tackle", "p2a: Caterpie"],
            ["|-damage|p2a: Caterpie|0 fnt", "|faint|p2a: Caterpie"],
        )
        self.assertNotIn("rockyhelmet", self.battle.opponent.active.impossible_items)


class TestLeftoversRuleOutRequiresSurvival(unittest.TestCase):
    """REGRESSION (Sally 2026-08-20).

    `upkeep` rules out Leftovers/Black Sludge once a mon has sat through a
    residual phase below max HP without healing. A mon that was KO'd this turn
    satisfies the arithmetic -- hp 0 < max_hp, and 0 <= last upkeep's hp --
    without ever having survived a residual, and Leftovers cannot fire on a
    fainted mon anyway.

    Measured in validation batch 20260820-151732 game g6: Hitmontop sat at
    100/100 for nine turns and was KO'd in one hit; the faint itself "proved"
    no Leftovers. Its true set was the Leftovers one, so every candidate set
    died and the empty-candidate fallback ran 112 times in that game.
    """

    def setUp(self):
        self.battle = Battle(None)
        self.battle.opponent.active = Pokemon("hitmontop", 88)
        self.battle.user.active = Pokemon("pikachu", 80)

    def test_fainting_does_not_rule_out_leftovers(self):
        opp = self.battle.opponent.active
        opp.hp = opp.max_hp                 # untouched for the whole stint
        upkeep(self.battle, "")
        opp.hp = 0                          # KO'd this turn
        upkeep(self.battle, "")
        self.assertNotIn(constants.LEFTOVERS, opp.impossible_items)
        self.assertNotIn(constants.BLACK_SLUDGE, opp.impossible_items)

    def test_surviving_below_max_without_healing_still_rules_it_out(self):
        # the real inference must keep working -- now expressed as "the mon was
        # below max when the residual phase OPENED and no item heal appeared"
        opp = self.battle.opponent.active
        opp.hp = opp.max_hp - 50
        opp._pre_residual_hp = opp.hp
        upkeep(self.battle, "")
        self.assertIn(constants.LEFTOVERS, opp.impossible_items)


class TestChoiceLockResetsOnSwitchOut(unittest.TestCase):
    """REGRESSION (choice-item inference keyed on the current stay).

    A Choice lock RESETS when the holder switches out, so "used two different
    moves" only disproves a Choice item when both were used in the SAME stay.
    The rule read `side.last_used_move`, which survives switches, so a mon that
    clicked one move, was forced out by Roar, came back and clicked another was
    ruled Choice-less on a perfectly legal sequence.

    Measured in validation batch 20260820-174544 game g6: an Indeedee-F whose
    TRUE item was Choice Specs. Its species has exactly three items
    (choicescarf, choicespecs, lifeorb); this rule killed both Choice ones and
    the Life Orb rule soundly killed the third, so EVERY candidate set died and
    the empty-candidate fallback ran 88 times in that single game.
    """

    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.opponent.active = Pokemon("caterpie", 80)
        self.battle.user.active = Pokemon("pikachu", 80)

    def test_two_moves_in_one_stay_still_rules_out_a_choice_item(self):
        # both DAMAGING: a status move with a stat drop trips the separate
        # `unlikely_to_have_choice_item` heuristic and would mask this rule
        self.battle.opponent.active.moves_used_since_switch_in.add("tackle")
        self.battle.opponent.last_used_move = LastUsedMove("caterpie", "tackle", 0)
        move(self.battle, ["", "move", "p2a: Caterpie", "Bug Bite"])
        self.assertFalse(self.battle.opponent.active.can_have_choice_item)

    def test_two_moves_across_a_switch_does_not(self):
        # the per-stay set was cleared by the switch-out; last_used_move still
        # remembers the pre-switch move, which is exactly the stale signal
        self.battle.opponent.last_used_move = LastUsedMove("caterpie", "tackle", 0)
        move(self.battle, ["", "move", "p2a: Caterpie", "Bug Bite"])
        self.assertTrue(
            self.battle.opponent.active.can_have_choice_item,
            "a Choice lock resets on switch-out",
        )


class TestHealBlockSuppressesItemEliminations(unittest.TestCase):
    """REGRESSION (Heal Block blind spot in the residual item rules).

    Heal Block stops Leftovers/Black Sludge healing AND stops a Sitrus Berry
    firing at 50%. Both `upkeep` rules read the absence of healing as proof the
    mon lacks the item, with no check for it.

    Measured: validation batch 20260820-175736 game g18, an Arboliva under Heal
    Block. Its species has exactly two items -- Leftovers and Sitrus Berry --
    so the two rules together took every candidate set and the empty-candidate
    fallback ran 88 times in that one game. Its TRUE item was the Sitrus Berry.
    """

    def setUp(self):
        self.battle = Battle(None)
        self.battle.opponent.active = Pokemon("arboliva", 91)
        self.battle.user.active = Pokemon("pikachu", 80)
        self.opp = self.battle.opponent.active
        self.opp.item = constants.UNKNOWN_ITEM

    def test_heal_block_blocks_the_leftovers_elimination(self):
        self.opp.hp = self.opp.max_hp - 40
        self.opp._pre_residual_hp = self.opp.hp
        self.opp.volatile_statuses.append(constants.HEAL_BLOCK)
        upkeep(self.battle, "")
        self.assertNotIn(constants.LEFTOVERS, self.opp.impossible_items)

    def test_heal_block_blocks_the_sitrus_elimination(self):
        self.opp.hp = int(self.opp.max_hp * 0.4)
        self.opp._pre_residual_hp = self.opp.hp
        self.opp.volatile_statuses.append(constants.HEAL_BLOCK)
        upkeep(self.battle, "")
        self.assertNotIn("sitrusberry", self.opp.impossible_items)

    def test_without_heal_block_both_still_fire(self):
        self.opp.hp = int(self.opp.max_hp * 0.4)
        self.opp._pre_residual_hp = self.opp.hp
        upkeep(self.battle, "")
        self.assertIn(constants.LEFTOVERS, self.opp.impossible_items)
        self.assertIn("sitrusberry", self.opp.impossible_items)
