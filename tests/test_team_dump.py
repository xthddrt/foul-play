import json
import os
import tempfile
import unittest
from unittest import mock

from config import FoulPlayConfig
from data import pkmn_sets
from data.pkmn_sets import user_sets_cache_path
from fp import team_dump
from fp.team_dump import (
    dump_team_from_request_json,
    record_user_team_observations,
    user_observation_from_request_dict,
)

LAPRAS_REQUEST_DICT = {
    "details": "Lapras, L87, M",
    "item": "choicespecs",
    "ability": "waterabsorb",
    "moves": ["hydropump", "sparklingaria", "icebeam", "freezedry"],
    "teraType": "Ice",
}
LAPRAS_SET_KEY = (
    "87,choicespecs,waterabsorb,freezedry,hydropump,icebeam,sparklingaria,ice"
)

GHOLDENGO_REQUEST_DICT = {
    "details": "Gholdengo, L77",
    "item": "leftovers",
    "ability": "goodasgold",
    "moves": ["recover", "makeitrain", "shadowball", "nastyplot"],
    "teraType": "Steel",
}
GHOLDENGO_SET_KEY = (
    "77,leftovers,goodasgold,makeitrain,nastyplot,recover,shadowball,steel"
)


class TestUserObservationFromRequestDict(unittest.TestCase):
    def test_builds_key_in_dataset_format(self):
        species, set_key = user_observation_from_request_dict(LAPRAS_REQUEST_DICT)

        self.assertEqual("lapras", species)
        self.assertEqual(LAPRAS_SET_KEY, set_key)

    def test_species_name_is_normalized(self):
        pkmn_dict = dict(LAPRAS_REQUEST_DICT, details="Typhlosion-Hisui, L83, F")
        species, _ = user_observation_from_request_dict(pkmn_dict)

        self.assertEqual("typhlosionhisui", species)

    def test_level_defaults_to_100_when_absent_from_details(self):
        pkmn_dict = dict(LAPRAS_REQUEST_DICT, details="Lapras, M")
        _, set_key = user_observation_from_request_dict(pkmn_dict)

        self.assertTrue(set_key.startswith("100,"))

    def test_no_tera_type_omits_trailing_segment(self):
        pkmn_dict = dict(LAPRAS_REQUEST_DICT)
        del pkmn_dict["teraType"]
        _, set_key = user_observation_from_request_dict(pkmn_dict)

        self.assertEqual(
            "87,choicespecs,waterabsorb,freezedry,hydropump,icebeam,sparklingaria",
            set_key,
        )


class TestRecordUserTeamObservations(unittest.TestCase):
    def setUp(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.cache_dir = tmp_dir.name
        patcher = mock.patch.object(pkmn_sets, "PKMN_SETS_CACHE_DIR", self.cache_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

        self.request_json = {
            "side": {"pokemon": [LAPRAS_REQUEST_DICT, GHOLDENGO_REQUEST_DICT]}
        }

    def read_overlay(self, pkmn_mode):
        with open(user_sets_cache_path(pkmn_mode), "r") as f:
            return json.load(f)

    def test_appends_each_pokemon_as_observation(self):
        record_user_team_observations(self.request_json, "gen9randombattleblitz")

        overlay = self.read_overlay("gen9randombattleblitz")
        self.assertEqual(1, overlay["lapras"][LAPRAS_SET_KEY])
        self.assertEqual(1, overlay["gholdengo"][GHOLDENGO_SET_KEY])

    def test_repeated_observations_increment_counts(self):
        record_user_team_observations(self.request_json, "gen9randombattleblitz")
        record_user_team_observations(self.request_json, "gen9randombattleblitz")

        overlay = self.read_overlay("gen9randombattleblitz")
        self.assertEqual(2, overlay["lapras"][LAPRAS_SET_KEY])
        self.assertEqual(2, overlay["gholdengo"][GHOLDENGO_SET_KEY])

    def test_dump_team_from_request_json_appends_observations(self):
        dump_dir = tempfile.TemporaryDirectory()
        self.addCleanup(dump_dir.cleanup)
        team_dir = tempfile.TemporaryDirectory()
        self.addCleanup(team_dir.cleanup)

        with mock.patch.object(
            FoulPlayConfig, "dump_team_dir", dump_dir.name, create=True
        ), mock.patch.object(
            FoulPlayConfig, "username", "testbot", create=True
        ), mock.patch.object(team_dump, "TEAM_DIR", team_dir.name):
            dump_team_from_request_json(
                self.request_json,
                "gen9randombattleblitz",
                "battle-gen9randombattleblitz-1",
            )

        overlay = self.read_overlay("gen9randombattleblitz")
        self.assertEqual(1, overlay["lapras"][LAPRAS_SET_KEY])
        self.assertEqual(1, overlay["gholdengo"][GHOLDENGO_SET_KEY])

        # the export dump itself is still written
        dump_path = os.path.join(
            dump_dir.name,
            "gen9randombattleblitz",
            "battle-gen9randombattleblitz-1_testbot.txt",
        )
        self.assertTrue(os.path.exists(dump_path))

    def test_dump_team_does_not_record_for_non_randombattle_modes(self):
        dump_dir = tempfile.TemporaryDirectory()
        self.addCleanup(dump_dir.cleanup)
        team_dir = tempfile.TemporaryDirectory()
        self.addCleanup(team_dir.cleanup)

        with mock.patch.object(
            FoulPlayConfig, "dump_team_dir", dump_dir.name, create=True
        ), mock.patch.object(
            FoulPlayConfig, "username", "testbot", create=True
        ), mock.patch.object(team_dump, "TEAM_DIR", team_dir.name):
            dump_team_from_request_json(
                self.request_json, "gen9battlefactory", "battle-gen9battlefactory-1"
            )

        self.assertFalse(os.path.exists(user_sets_cache_path("gen9battlefactory")))
