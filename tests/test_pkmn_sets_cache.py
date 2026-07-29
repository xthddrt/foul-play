import json
import os
import tempfile
import time
import unittest
from unittest import mock

import requests

from data import pkmn_sets
from data.pkmn_sets import (
    SETS_CACHE_TTL_SECONDS,
    USER_SETS_INGESTED_KEY,
    get_sets_file,
    make_user_set_key,
    record_user_set_observations,
    user_sets_cache_path,
    _RandomBattleSets,
)

PIKACHU_SET_KEY = "85,lightball,static,irontail,surf,thunderbolt,voltswitch,electric"
PIKACHU_OTHER_SET_KEY = (
    "85,lightball,lightningrod,irontail,surf,thunderbolt,voltswitch,water"
)
EEVEE_SET_KEY = "88,eviolite,adaptability,doubleedge,protect,quickattack,wish,normal"


def _mock_response(status_code=200, json_data=None):
    response = mock.MagicMock()
    response.status_code = status_code
    if json_data is None:
        json_data = {}
    response.json.return_value = json_data
    return response


class TestGetSetsFile(unittest.TestCase):
    def setUp(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.cache_path = os.path.join(tmp_dir.name, "sets", "gen9randombattle.json")
        self.remote_url = "https://example.com/gen9randombattle.json"

    def write_cache(self, data, age_seconds=0):
        os.makedirs(os.path.dirname(self.cache_path), exist_ok=True)
        with open(self.cache_path, "w") as f:
            json.dump(data, f)
        if age_seconds:
            cache_time = time.time() - age_seconds
            os.utime(self.cache_path, (cache_time, cache_time))

    def read_cache(self):
        with open(self.cache_path, "r") as f:
            return json.load(f)

    def test_fresh_cache_is_used_without_hitting_remote(self):
        self.write_cache({"pikachu": {PIKACHU_SET_KEY: 1}})
        with mock.patch("data.pkmn_sets.requests.get") as mock_get:
            sets = get_sets_file(self.cache_path, self.remote_url)

        mock_get.assert_not_called()
        self.assertEqual({"pikachu": {PIKACHU_SET_KEY: 1}}, sets)

    def test_cache_older_than_ttl_is_redownloaded(self):
        self.write_cache(
            {"pikachu": {PIKACHU_SET_KEY: 1}},
            age_seconds=SETS_CACHE_TTL_SECONDS + 60,
        )
        new_data = {"pikachu": {PIKACHU_SET_KEY: 2}}
        with mock.patch(
            "data.pkmn_sets.requests.get",
            return_value=_mock_response(200, new_data),
        ) as mock_get:
            sets = get_sets_file(self.cache_path, self.remote_url)

        mock_get.assert_called_once_with(self.remote_url)
        self.assertEqual(new_data, sets)
        self.assertEqual(new_data, self.read_cache())

    def test_stale_cache_is_used_when_refresh_gets_non_200(self):
        old_data = {"pikachu": {PIKACHU_SET_KEY: 1}}
        self.write_cache(old_data, age_seconds=SETS_CACHE_TTL_SECONDS + 60)
        with mock.patch(
            "data.pkmn_sets.requests.get", return_value=_mock_response(500)
        ):
            sets = get_sets_file(self.cache_path, self.remote_url)

        self.assertEqual(old_data, sets)
        self.assertEqual(old_data, self.read_cache())

    def test_stale_cache_is_used_when_refresh_raises(self):
        old_data = {"pikachu": {PIKACHU_SET_KEY: 1}}
        self.write_cache(old_data, age_seconds=SETS_CACHE_TTL_SECONDS + 60)
        with mock.patch(
            "data.pkmn_sets.requests.get",
            side_effect=requests.RequestException("connection error"),
        ):
            sets = get_sets_file(self.cache_path, self.remote_url)

        self.assertEqual(old_data, sets)
        self.assertEqual(old_data, self.read_cache())

    def test_failed_download_is_not_cached(self):
        with mock.patch(
            "data.pkmn_sets.requests.get", return_value=_mock_response(404)
        ):
            sets = get_sets_file(self.cache_path, self.remote_url)

        self.assertEqual({}, sets)
        self.assertFalse(os.path.exists(self.cache_path))

    def test_empty_successful_response_is_not_cached(self):
        with mock.patch(
            "data.pkmn_sets.requests.get", return_value=_mock_response(200, {})
        ):
            sets = get_sets_file(self.cache_path, self.remote_url)

        self.assertEqual({}, sets)
        self.assertFalse(os.path.exists(self.cache_path))

    def test_empty_cache_file_is_treated_as_missing(self):
        self.write_cache({})
        new_data = {"pikachu": {PIKACHU_SET_KEY: 1}}
        with mock.patch(
            "data.pkmn_sets.requests.get",
            return_value=_mock_response(200, new_data),
        ) as mock_get:
            sets = get_sets_file(self.cache_path, self.remote_url)

        mock_get.assert_called_once_with(self.remote_url)
        self.assertEqual(new_data, sets)
        self.assertEqual(new_data, self.read_cache())

    def test_empty_cache_file_and_failed_download_returns_empty(self):
        self.write_cache({})
        with mock.patch(
            "data.pkmn_sets.requests.get", return_value=_mock_response(404)
        ):
            sets = get_sets_file(self.cache_path, self.remote_url)

        self.assertEqual({}, sets)


class TestUserObservedSets(unittest.TestCase):
    def setUp(self):
        tmp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_dir.cleanup)
        self.cache_dir = tmp_dir.name
        patcher = mock.patch.object(pkmn_sets, "PKMN_SETS_CACHE_DIR", self.cache_dir)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_make_user_set_key_matches_dataset_format(self):
        # this exact key exists in the real gen9randombattle dataset
        key = make_user_set_key(
            level=87,
            item="choicespecs",
            ability="Water Absorb",
            moves=["Hydro Pump", "Sparkling Aria", "Ice Beam", "Freeze-Dry"],
            tera_type="Ice",
        )
        self.assertEqual(
            "87,choicespecs,waterabsorb,freezedry,hydropump,icebeam,sparklingaria,ice",
            key,
        )

    def test_make_user_set_key_without_tera_type(self):
        key = make_user_set_key(
            level=100,
            item="leftovers",
            ability="levitate",
            moves=["shadowball", "psychic"],
        )
        self.assertEqual("100,leftovers,levitate,psychic,shadowball", key)

    def test_make_user_set_key_with_no_item(self):
        key = make_user_set_key(
            level=87,
            item=None,
            ability="infiltrator",
            moves=["acrobatics", "leechseed"],
            tera_type="steel",
        )
        self.assertEqual("87,,infiltrator,acrobatics,leechseed,steel", key)

    def test_record_user_set_observations_increments_counts(self):
        record_user_set_observations("gen9randombattle", [("pikachu", PIKACHU_SET_KEY)])
        record_user_set_observations(
            "gen9randombattle",
            [("pikachu", PIKACHU_SET_KEY), ("eevee", EEVEE_SET_KEY)],
        )

        with open(user_sets_cache_path("gen9randombattle"), "r") as f:
            overlay = json.load(f)

        self.assertEqual(2, overlay["pikachu"][PIKACHU_SET_KEY])
        self.assertEqual(1, overlay["eevee"][EEVEE_SET_KEY])

    def test_blitz_suffix_is_stripped_from_overlay_path(self):
        record_user_set_observations(
            "gen9randombattleblitz", [("pikachu", PIKACHU_SET_KEY)]
        )

        expected_path = os.path.join(self.cache_dir, "gen9randombattle_user.json")
        self.assertEqual(expected_path, user_sets_cache_path("gen9randombattleblitz"))
        self.assertTrue(os.path.exists(expected_path))

    def test_record_user_set_observations_tracks_ingested_marker(self):
        record_user_set_observations(
            "gen9randombattle",
            [("pikachu", PIKACHU_SET_KEY)],
            ingested_marker="team_file.txt",
        )
        record_user_set_observations(
            "gen9randombattle",
            [("eevee", EEVEE_SET_KEY)],
            ingested_marker="team_file.txt",
        )

        with open(user_sets_cache_path("gen9randombattle"), "r") as f:
            overlay = json.load(f)

        self.assertEqual(["team_file.txt"], overlay[USER_SETS_INGESTED_KEY])

    def test_load_raw_sets_merges_overlay_into_downloaded_sets(self):
        with open(os.path.join(self.cache_dir, "gen9randombattle.json"), "w") as f:
            json.dump({"pikachu": {PIKACHU_SET_KEY: 10}}, f)
        with open(os.path.join(self.cache_dir, "gen9randombattle_user.json"), "w") as f:
            json.dump(
                {
                    "pikachu": {PIKACHU_SET_KEY: 2, PIKACHU_OTHER_SET_KEY: 1},
                    "eevee": {EEVEE_SET_KEY: 1},
                    USER_SETS_INGESTED_KEY: ["team_file.txt"],
                },
                f,
            )

        random_battle_sets = _RandomBattleSets()
        random_battle_sets.initialize("gen9randombattleblitz")

        raw = random_battle_sets.raw_pkmn_sets
        self.assertEqual(12, raw["pikachu"][PIKACHU_SET_KEY])
        self.assertEqual(1, raw["pikachu"][PIKACHU_OTHER_SET_KEY])
        self.assertEqual(1, raw["eevee"][EEVEE_SET_KEY])
        self.assertNotIn(USER_SETS_INGESTED_KEY, raw)

        # the merged counts flow through to the parsed sets and the
        # precomputed species sampling weights
        species_weights = dict(
            zip(
                random_battle_sets.species_sample_names,
                random_battle_sets.species_sample_weights,
            )
        )
        self.assertEqual({"pikachu": 13, "eevee": 1}, species_weights)

        # the downloaded cache file itself is never modified
        with open(os.path.join(self.cache_dir, "gen9randombattle.json"), "r") as f:
            self.assertEqual({"pikachu": {PIKACHU_SET_KEY: 10}}, json.load(f))

    def test_load_raw_sets_without_overlay_file(self):
        with open(os.path.join(self.cache_dir, "gen9randombattle.json"), "w") as f:
            json.dump({"pikachu": {PIKACHU_SET_KEY: 10}}, f)

        random_battle_sets = _RandomBattleSets()
        random_battle_sets.initialize("gen9randombattle")

        self.assertEqual(
            {"pikachu": {PIKACHU_SET_KEY: 10}}, random_battle_sets.raw_pkmn_sets
        )
