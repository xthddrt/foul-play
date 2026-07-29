import importlib.util
import json
import os
import tempfile
import unittest
from unittest import mock

from data import pkmn_sets
from data.pkmn_sets import USER_SETS_INGESTED_KEY, user_sets_cache_path

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKFILL_SCRIPT_PATH = os.path.join(
    os.path.dirname(REPO_ROOT), "backfill_user_observations.py"
)

LAPRAS_EXPORT = """Lapras (M) @ choicespecs
Ability: Water Absorb
Level: 87
Tera Type: Ice
EVs: 85 HP / 85 Atk / 85 Def / 85 SpA / 85 SpD / 85 Spe
Hardy Nature
- Hydro Pump
- Sparkling Aria
- Ice Beam
- Freeze-Dry

Gholdengo @ leftovers
Ability: Good as Gold
Level: 77
Tera Type: Steel
EVs: 85 HP / 85 Atk / 85 Def / 85 SpA / 85 SpD / 85 Spe
Hardy Nature
- Recover
- Make It Rain
- Shadow Ball
- Nasty Plot"""

LAPRAS_SET_KEY = (
    "87,choicespecs,waterabsorb,freezedry,hydropump,icebeam,sparklingaria,ice"
)
GHOLDENGO_SET_KEY = (
    "77,leftovers,goodasgold,makeitrain,nastyplot,recover,shadowball,steel"
)


def _load_backfill_module():
    spec = importlib.util.spec_from_file_location(
        "backfill_user_observations", BACKFILL_SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@unittest.skipUnless(
    os.path.exists(BACKFILL_SCRIPT_PATH), "backfill script not present"
)
class TestBackfillIdempotency(unittest.TestCase):
    def setUp(self):
        cache_dir = tempfile.TemporaryDirectory()
        self.addCleanup(cache_dir.cleanup)
        patcher = mock.patch.object(pkmn_sets, "PKMN_SETS_CACHE_DIR", cache_dir.name)
        patcher.start()
        self.addCleanup(patcher.stop)

        teams_dir = tempfile.TemporaryDirectory()
        self.addCleanup(teams_dir.cleanup)
        self.teams_dir = teams_dir.name
        with open(os.path.join(self.teams_dir, "battle-1_testbot.txt"), "w") as f:
            f.write(LAPRAS_EXPORT)

        self.backfill = _load_backfill_module()

    def read_overlay(self):
        with open(user_sets_cache_path("gen9randombattleblitz"), "r") as f:
            return json.load(f)

    def test_ingests_observations_from_export_files(self):
        files_ingested, files_skipped, observations_added = (
            self.backfill.ingest_teams_dir(self.teams_dir, "gen9randombattleblitz")
        )

        self.assertEqual(1, files_ingested)
        self.assertEqual(0, files_skipped)
        self.assertEqual(2, observations_added)

        overlay = self.read_overlay()
        self.assertEqual(1, overlay["lapras"][LAPRAS_SET_KEY])
        self.assertEqual(1, overlay["gholdengo"][GHOLDENGO_SET_KEY])
        self.assertEqual(["battle-1_testbot.txt"], overlay[USER_SETS_INGESTED_KEY])

    def test_second_run_is_a_noop(self):
        self.backfill.ingest_teams_dir(self.teams_dir, "gen9randombattleblitz")
        files_ingested, files_skipped, observations_added = (
            self.backfill.ingest_teams_dir(self.teams_dir, "gen9randombattleblitz")
        )

        self.assertEqual(0, files_ingested)
        self.assertEqual(1, files_skipped)
        self.assertEqual(0, observations_added)

        overlay = self.read_overlay()
        self.assertEqual(1, overlay["lapras"][LAPRAS_SET_KEY])
        self.assertEqual(1, overlay["gholdengo"][GHOLDENGO_SET_KEY])

    def test_new_files_are_ingested_on_subsequent_runs(self):
        self.backfill.ingest_teams_dir(self.teams_dir, "gen9randombattleblitz")

        with open(os.path.join(self.teams_dir, "battle-2_testbot.txt"), "w") as f:
            f.write(LAPRAS_EXPORT)
        files_ingested, files_skipped, observations_added = (
            self.backfill.ingest_teams_dir(self.teams_dir, "gen9randombattleblitz")
        )

        self.assertEqual(1, files_ingested)
        self.assertEqual(1, files_skipped)
        self.assertEqual(2, observations_added)

        overlay = self.read_overlay()
        self.assertEqual(2, overlay["lapras"][LAPRAS_SET_KEY])
        self.assertEqual(2, overlay["gholdengo"][GHOLDENGO_SET_KEY])
