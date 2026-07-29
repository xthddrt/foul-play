import unittest
from copy import deepcopy

from fp.battle import Battle
from fp.battle import LastUsedMove
from fp.battle import Battler
from fp.battle import Pokemon
from fp.battle import Move


class TestPokemon(unittest.TestCase):
    def test_alternate_pokemon_name_initializes(self):
        name = "florgeswhite"
        Pokemon(name, 100)

    def test_pokemon_is_not_revealed_by_default(self):
        self.assertFalse(Pokemon("pikachu", 100).revealed)

    def test_revealed_survives_deepcopy(self):
        pkmn = Pokemon("pikachu", 100)
        pkmn.revealed = True
        self.assertTrue(deepcopy(pkmn).revealed)

    def test_pokemon_illusion_is_not_broken_by_default(self):
        self.assertFalse(Pokemon("zoroark", 100).illusion_broken)

    def test_illusion_broken_survives_deepcopy(self):
        pkmn = Pokemon("zoroark", 100)
        pkmn.illusion_broken = True
        self.assertTrue(deepcopy(pkmn).illusion_broken)

    def test_get_mega_formes_one_mega(self):
        self.assertEqual(
            Pokemon("venusaur", 100).get_mega_pkmn_info(),
            [("venusaurmega", "venusaurite")],
        )

    def test_get_mega_formes_two_mega(self):
        self.assertEqual(
            Pokemon("charizard", 100).get_mega_pkmn_info(),
            [("charizardmegax", "charizarditex"), ("charizardmegay", "charizarditey")],
        )

    def test_get_mega_formes_none(self):
        self.assertEqual(Pokemon("umbreon", 100).get_mega_pkmn_info(), [])


class TestInitializeTeamPreview(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.active = Pokemon("pikachu", 100)
        self.battle.user.reserve = [Pokemon("weedle", 100)]

    def test_all_pkmn_are_marked_revealed(self):
        self.battle.initialize_team_preview(
            ["Caterpie, L100, M", "Metapod, L100, F"], "gen9ou"
        )

        # the user's active was moved into the reserve
        self.assertEqual(2, len(self.battle.user.reserve))
        self.assertEqual(2, len(self.battle.opponent.reserve))
        for pkmn in self.battle.user.reserve + self.battle.opponent.reserve:
            self.assertTrue(pkmn.revealed)


class TestBattlerActiveLockedIntoMove(unittest.TestCase):
    def setUp(self):
        self.battler = Battler()
        self.battler.active = Pokemon("pikachu", 100)
        self.battler.active.moves = [
            Move("thunderbolt"),
            Move("volttackle"),
            Move("agility"),
            Move("doubleteam"),
        ]

    def test_choice_item_with_previous_move_used_by_this_pokemon_returns_true(self):
        self.battler.active.item = "choicescarf"
        self.battler.last_used_move = LastUsedMove(
            pokemon_name="pikachu", move="volttackle", turn=0
        )

        self.battler.lock_moves()

        self.assertFalse(self.battler.active.get_move("volttackle").disabled)

        self.assertTrue(self.battler.active.get_move("thunderbolt").disabled)
        self.assertTrue(self.battler.active.get_move("agility").disabled)
        self.assertTrue(self.battler.active.get_move("doubleteam").disabled)

    def test_firstimpression_gets_locked_when_last_used_move_was_by_the_active_pokemon(
        self,
    ):
        self.battler.active.moves.append(Move("firstimpression"))
        self.battler.last_used_move = LastUsedMove(
            pokemon_name="pikachu",  # the current active pokemon
            move="volttackle",
            turn=0,
        )

        self.battler.lock_moves()

        self.assertTrue(self.battler.active.get_move("firstimpression").disabled)

    def test_taunt_locks_status_move(self):
        self.battler.active.moves.append(Move("calmmind"))
        self.battler.active.volatile_statuses.append("taunt")

        self.battler.lock_moves()

        self.assertTrue(self.battler.active.get_move("calmmind").disabled)

    def test_taunt_does_not_lock_physical_move(self):
        self.battler.active.moves.append(Move("tackle"))
        self.battler.active.volatile_statuses.append("taunt")

        self.battler.lock_moves()

        self.assertFalse(self.battler.active.get_move("tackle").disabled)

    def test_taunt_does_not_lock_special_move(self):
        self.battler.active.moves.append(Move("watergun"))
        self.battler.active.volatile_statuses.append("taunt")

        self.battler.lock_moves()

        self.assertFalse(self.battler.active.get_move("watergun").disabled)

    def test_taunt_with_multiple_moves(self):
        self.battler.active.moves.append(Move("watergun"))
        self.battler.active.moves.append(Move("tackle"))
        self.battler.active.moves.append(Move("calmmind"))
        self.battler.active.volatile_statuses.append("taunt")

        self.battler.lock_moves()

        self.assertFalse(self.battler.active.get_move("watergun").disabled)
        self.assertFalse(self.battler.active.get_move("tackle").disabled)
        self.assertTrue(self.battler.active.get_move("calmmind").disabled)

    def test_calmmind_gets_locked_when_user_has_assaultvest(self):
        self.battler.active.moves.append(Move("calmmind"))
        self.battler.active.item = "assaultvest"

        self.battler.lock_moves()

        self.assertTrue(self.battler.active.get_move("calmmind").disabled)

    def test_tackle_is_not_disabled_when_user_has_assaultvest(self):
        self.battler.active.moves.append(Move("tackle"))
        self.battler.active.item = "assaultvest"

        self.battler.lock_moves()

        self.assertFalse(self.battler.active.get_move("tackle").disabled)

    def test_fakeout_gets_locked_when_last_used_move_was_by_the_active_pokemon(self):
        self.battler.active.moves.append(Move("fakeout"))
        self.battler.last_used_move = LastUsedMove(
            pokemon_name="pikachu",  # the current active pokemon
            move="volttackle",
            turn=0,
        )

        self.battler.lock_moves()

        self.assertTrue(self.battler.active.get_move("fakeout").disabled)

    def test_firstimpression_is_not_disabled_when_the_last_used_move_was_a_switch(self):
        self.battler.active.moves.append(Move("firstimpression"))
        self.battler.last_used_move = LastUsedMove(
            pokemon_name="caterpie", move="switch", turn=0
        )

        self.battler.lock_moves()

        self.assertFalse(self.battler.active.get_move("firstimpression").disabled)

    def test_fakeout_is_not_disabled_when_the_last_used_move_was_a_switch(self):
        self.battler.active.moves.append(Move("fakeout"))
        self.battler.last_used_move = LastUsedMove(
            pokemon_name="caterpie", move="switch", turn=0
        )

        self.battler.lock_moves()

        self.assertFalse(self.battler.active.get_move("fakeout").disabled)

    def test_choice_item_with_previous_move_being_a_switch_returns_false(self):
        self.battler.active.item = "choicescarf"
        self.battler.last_used_move = LastUsedMove(
            pokemon_name="caterpie", move="switch", turn=0
        )
        self.battler.lock_moves()

        self.assertFalse(self.battler.active.get_move("volttackle").disabled)
        self.assertFalse(self.battler.active.get_move("thunderbolt").disabled)
        self.assertFalse(self.battler.active.get_move("agility").disabled)
        self.assertFalse(self.battler.active.get_move("doubleteam").disabled)

    def test_non_choice_item_possession_returns_false(self):
        self.battler.active.item = ""
        self.battler.last_used_move = LastUsedMove(
            pokemon_name="pikachu", move="tackle", turn=0
        )
        self.battler.lock_moves()

        self.assertFalse(self.battler.active.get_move("volttackle").disabled)
        self.assertFalse(self.battler.active.get_move("thunderbolt").disabled)
        self.assertFalse(self.battler.active.get_move("agility").disabled)
        self.assertFalse(self.battler.active.get_move("doubleteam").disabled)


class TestReInitializeActivePokemonFromRequestJson(unittest.TestCase):
    """`update_battle` buffers a turn's protocol and replays it only once the NEXT
    |request| has already replaced `battle.request_json`, so a MID-turn
    |detailschange| is always matched against the END-of-turn forme.  Matching the
    request entry by `details` therefore explodes whenever the forme oscillated or
    was reverted inside the turn; PS's own identifiers (`active`, positional per
    sim/pokemon.ts:1164, and `ident`, the never-rewritten nickname at :1161) do not.
    """

    def _request(self, active_entry, others=()):
        return {
            "side": {
                "name": "synthbot",
                "id": "p1",
                "pokemon": [active_entry] + list(others),
            },
            "rqid": 19,
        }

    def _entry(self, ident, details, condition, stats, active):
        return {
            "ident": ident,
            "details": details,
            "condition": condition,
            "active": active,
            "stats": stats,
        }

    def setUp(self):
        self.battler = Battler()
        self.battler.name = "p1"

    def test_same_forme_request_still_supplies_stats_and_hp(self):
        mon = Pokemon("eiscue", 88)
        mon.max_hp = 274
        mon.hp = 274
        self.battler.active = mon
        req = self._request(
            self._entry(
                "p1: Eiscue",
                "Eiscue, L88, M",
                "153/274",
                {"atk": 191, "def": 173, "spa": 165, "spd": 138, "spe": 111},
                True,
            )
        )

        self.battler.re_initialize_active_pokemon_from_request_json(req)

        self.assertEqual(153, mon.hp)
        self.assertEqual(111, mon.stats["speed"])

    def test_within_turn_ice_face_restore_matches_by_active_flag_and_keeps_stats(self):
        # synth20228 T14: switch in as Eiscue-Noice, Snowscape restores Eiscue, Tail
        # Slap re-busts it.  The restore is processed against the rqid-19 request,
        # which lists Eiscue-Noice -- neither the live forme name nor `base_name`.
        mon = Pokemon("eiscue", 88)
        mon.base_name = "eiscue"
        mon.max_hp = 274
        mon.hp = 153
        mon.stats["speed"] = 111  # base-forme speed, recomputed by forme_change()
        self.battler.active = mon
        req = self._request(
            self._entry(
                "p1: Eiscue",
                "Eiscue-Noice, L88, M",
                "56/274",
                {"atk": 191, "def": 173, "spa": 165, "spd": 138, "spe": 279},
                True,
            ),
            [
                self._entry(
                    "p1: Armarouge",
                    "Armarouge, L80, F",
                    "71/267",
                    {"atk": 101, "def": 206, "spa": 246, "spd": 174, "spe": 166},
                    False,
                )
            ],
        )

        self.battler.re_initialize_active_pokemon_from_request_json(req)

        # the request's stats belong to Eiscue-Noice; the live forme is base Eiscue
        self.assertEqual(111, mon.stats["speed"])
        self.assertEqual(56, mon.hp)

    def test_same_turn_ko_forme_revert_matches_by_active_flag(self):
        # synth33783 T5: Terapagos teras to Terapagos-Stellar and faints the same
        # turn; PS reverts it (`|detailschange|p1: Terapagos|Terapagos|[silent]`), so
        # the forceSwitch request lists plain Terapagos while `base_name` is
        # terapagosterastal (the first request already showed the Tera Shift forme).
        mon = Pokemon("terapagosstellar", 77)
        mon.base_name = "terapagosterastal"
        mon.max_hp = 373
        mon.hp = 373
        mon.stats["speed"] = 175
        self.battler.active = mon
        req = self._request(
            self._entry(
                "p1: Terapagos",
                "Terapagos, L77, M",
                "0 fnt",
                {"atk": 145, "def": 175, "spa": 145, "spd": 175, "spe": 137},
                True,
            )
        )

        self.battler.re_initialize_active_pokemon_from_request_json(req)

        self.assertEqual(175, mon.stats["speed"])  # base-forme stats NOT applied
        self.assertEqual(0, mon.hp)

    def test_ident_is_the_fallback_when_no_entry_is_flagged_active(self):
        mon = Pokemon("eiscue", 88)
        mon.base_name = "eiscue"
        mon.max_hp = 274
        mon.hp = 153
        self.battler.active = mon
        entry = self._entry(
            "p1: Eiscue",
            "Eiscue-Noice, L88, M",
            "56/274",
            {"atk": 191, "def": 173, "spa": 165, "spd": 138, "spe": 279},
            False,
        )
        del entry["active"]

        self.battler.re_initialize_active_pokemon_from_request_json(
            self._request(entry)
        )

        self.assertEqual(56, mon.hp)

    def test_still_raises_when_the_active_pokemon_is_absent_entirely(self):
        mon = Pokemon("eiscue", 88)
        mon.base_name = "eiscue"
        self.battler.active = mon
        req = self._request(
            self._entry(
                "p1: Armarouge",
                "Armarouge, L80, F",
                "71/267",
                {"atk": 101, "def": 206, "spa": 246, "spd": 174, "spe": 166},
                False,
            )
        )

        with self.assertRaises(AssertionError):
            self.battler.re_initialize_active_pokemon_from_request_json(req)
