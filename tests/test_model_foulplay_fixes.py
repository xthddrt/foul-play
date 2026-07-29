"""
Regression tests for the three judge-confirmed foul-play modeling fixes:

  1. Partial-trap / Magnet-Rise volatile durations forwarded to the engine as an
     elapsed-EOT count instead of being silently dropped to 0.
  2. `maybeTrapped` requests no longer folded into a hard trap (which deleted
     every legal root switch).
  3. Substitute HP seeded from a tracked running estimate instead of a fixed
     maxhp/10 (never-reached "hit") / maxhp/4 heuristic.

Each assertion is anchored to Pokemon-Showdown / poke-engine source in comments.
"""

import json
import os
import unittest
from unittest import mock

import constants
from fp.battle import Battle, Battler, Move, Pokemon, LastUsedMove
from fp.battle_modifier import (
    activate,
    anim,
    cant,
    error,
    faint,
    form_change,
    move,
    prepare,
    remove_item,
    set_item,
    start_volatile_status,
    end_volatile_status,
    switch_or_drag,
    turn,
    update_battle,
    upkeep,
)
import fp.battle_modifier as battle_modifier
import fp.search.poke_engine_helpers as poke_engine_helpers
from fp.search.poke_engine_helpers import (
    POKE_ENGINE_SUPPORTS_TRAP_MAGNETRISE_DURATION,
    POKE_ENGINE_SUPPORTS_HEALBLOCK_THROATCHOP_SYRUPBOMB_DURATION,
    POKE_ENGINE_SUPPORTS_ACTIVE_MOVE_ACTIONS,
    PokeEnginePokemon,
    battler_to_poke_engine_side,
    pokemon_to_poke_engine_pkmn,
)


# ---------------------------------------------------------------------------
# Fix 2: maybeTrapped must not be folded into a hard trap
# ---------------------------------------------------------------------------
class TestMaybeTrappedNotHardTrap(unittest.TestCase):
    def _request(self, trapped=None, maybe_trapped=None):
        active = {
            "moves": [
                {
                    "move": "Thunderbolt",
                    "id": "thunderbolt",
                    "pp": 8,
                    "maxpp": 8,
                    "target": "normal",
                    "disabled": False,
                }
            ]
        }
        if trapped is not None:
            active["trapped"] = trapped
        if maybe_trapped is not None:
            active["maybeTrapped"] = maybe_trapped
        return {
            "active": [active],
            "side": {
                "name": "BigBluePikachu",
                "id": "p2",
                "pokemon": [
                    {
                        "ident": "p2: PikachuNickname",
                        "details": "Pikachu, L84, M",
                        "condition": "152/335",
                        "active": True,
                        "stats": {
                            "atk": 200,
                            "def": 210,
                            "spa": 220,
                            "spd": 230,
                            "spe": 240,
                        },
                        "moves": ["thunderbolt"],
                        "baseAbility": "static",
                        "item": "lightball",
                        "ability": "static",
                    }
                ],
            },
        }

    def setUp(self):
        self.battler = Battler()
        self.battler.active = Pokemon("pikachu", 100)

    def test_maybe_trapped_does_not_set_trapped(self):
        # PS sim/pokemon.ts:1137-1138 only emits maybeTrapped when
        # pokemon.trapped is false; sim/side.ts:996 lets that switch resolve
        # (choice.cantUndo only). So the bot must remain un-trapped.
        self.battler.update_from_request_json(self._request(maybe_trapped=True))
        self.assertFalse(self.battler.trapped)

    def test_hard_trapped_still_sets_trapped(self):
        # PS sim/pokemon.ts:1124/1136 writes data.trapped only for a confirmed
        # hard trap; sim/side.ts:983 emitChoiceErrors and rejects the switch.
        self.battler.update_from_request_json(self._request(trapped=True))
        self.assertTrue(self.battler.trapped)

    def test_first_turn_maybe_trapped_does_not_set_trapped(self):
        self.battler.initialize_first_turn_user_from_json(
            self._request(maybe_trapped=True)
        )
        self.assertFalse(self.battler.trapped)

    def test_first_turn_hard_trapped_sets_trapped(self):
        self.battler.initialize_first_turn_user_from_json(
            self._request(trapped=True)
        )
        self.assertTrue(self.battler.trapped)

    def test_force_trapped_reflects_only_hard_trap(self):
        # force_trapped is what makes the engine strip every root switch
        # (poke-engine state.rs:1082-1090); it must stay False for maybeTrapped.
        self.battler.update_from_request_json(self._request(maybe_trapped=True))
        side = battler_to_poke_engine_side(self.battler)
        self.assertFalse(side.force_trapped)

        self.battler.update_from_request_json(self._request(trapped=True))
        side = battler_to_poke_engine_side(self.battler)
        self.assertTrue(side.force_trapped)


# ---------------------------------------------------------------------------
# Fix 3: Substitute HP tracked as a running estimate
# ---------------------------------------------------------------------------
class TestSubstituteHealthTracking(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.user.active = Pokemon("pikachu", 100)
        self.opponent_active = Pokemon("caterpie", 100)
        self.opponent_active.max_hp = 200
        self.opponent_active.hp = 200
        self.battle.opponent.active = self.opponent_active

    def test_start_seeds_quarter_maxhp(self):
        # PS data/moves.ts substitute onStart: effectState.hp = floor(maxhp/4)
        split_msg = ["", "-start", "p2a: Caterpie", "Substitute"]
        start_volatile_status(self.battle, split_msg)
        self.assertEqual(200 // 4, self.battle.opponent.active.substitute_health)
        # creation is the one moment the value is EXACT: a singleton interval
        self.assertEqual(200 // 4, self.battle.opponent.active.substitute_health_low)
        self.assertIn(
            constants.SUBSTITUTE, self.battle.opponent.active.volatile_statuses
        )

    def test_end_zeroes_substitute_health(self):
        self.battle.opponent.active.substitute_health = 50
        self.battle.opponent.active.volatile_statuses.append(constants.SUBSTITUTE)
        split_msg = ["", "-end", "p2a: Caterpie", "Substitute"]
        end_volatile_status(self.battle, split_msg)
        self.assertEqual(0, self.battle.opponent.active.substitute_health)
        self.assertEqual(0, self.battle.opponent.active.substitute_health_low)

    # --- absorbed-hit reconstruction: an INTERVAL, never a point -----------
    # `-activate ... move: Substitute|[damage]` carries no magnitude, so the
    # absorbed hit is one unknown draw from Showdown's 16 rolls and the sub's
    # remaining HP is a range.  `substitute_health` is the range's UPPER bound
    # (the value the engine boundary consumes); `substitute_health_low` the
    # lower.  Blocks are supplied the way `process_battle_updates` supplies
    # them, because the attacking hit is only identifiable from the block.

    @staticmethod
    def _block(*lines):
        """(msg_lines, index_of_last_line)"""
        return list(lines), len(lines) - 1

    @mock.patch("fp.battle_modifier.poke_engine_get_damage_roll_sets")
    def test_activate_narrows_to_interval_opponent_sub(self, mock_sets):
        # bot attacked the opponent's sub -> uses the s1 (side_one) roll set
        mock_sets.return_value = (([40, 42, 45, 50], [60, 65, 70, 75]), None)
        self.battle.opponent.active.substitute_health = 50
        self.battle.opponent.active.substitute_health_low = 50
        self.battle.opponent.active.volatile_statuses.append(constants.SUBSTITUTE)

        lines, idx = self._block(
            "|move|p1a: Pikachu|Tackle|p2a: Caterpie",
            "|-activate|p2a: Caterpie|move: Substitute|[damage]",
        )
        activate(self.battle, lines[idx].split("|"), lines, idx)

        self.assertTrue(self.battle.opponent.active.substitute_hit)
        # 50 - max_roll .. 50 - min_roll
        self.assertEqual(1, self.battle.opponent.active.substitute_health_low)
        self.assertEqual(10, self.battle.opponent.active.substitute_health)
        # oriented as (bot_move, "none", bot_first)
        args = mock_sets.call_args[0]
        self.assertEqual("tackle", args[1])
        self.assertEqual("none", args[2])
        self.assertTrue(args[3])

    @mock.patch("fp.battle_modifier.poke_engine_get_damage_roll_sets")
    def test_activate_narrows_to_interval_user_sub(self, mock_sets):
        # opponent attacked the bot's sub -> uses the s2 (side_two) roll set
        mock_sets.return_value = (None, ([10, 12, 14, 20], [30, 35, 40, 45]))
        self.battle.user.active.max_hp = 200
        self.battle.user.active.substitute_health = 50
        self.battle.user.active.substitute_health_low = 50
        self.battle.user.active.volatile_statuses.append(constants.SUBSTITUTE)

        lines, idx = self._block(
            "|move|p2a: Caterpie|Tackle|p1a: Pikachu",
            "|-activate|p1a: Pikachu|move: Substitute|[damage]",
        )
        activate(self.battle, lines[idx].split("|"), lines, idx)

        self.assertEqual(30, self.battle.user.active.substitute_health_low)
        self.assertEqual(40, self.battle.user.active.substitute_health)
        # oriented as ("none", opp_move, not bot_first)
        args = mock_sets.call_args[0]
        self.assertEqual("none", args[1])
        self.assertEqual("tackle", args[2])
        self.assertFalse(args[3])

    @mock.patch("fp.battle_modifier.poke_engine_get_damage_roll_sets")
    def test_activate_uses_the_crit_arm_when_the_block_shows_a_crit(self, mock_sets):
        # PS's substitute condition calls getDamage WITHOUT suppressMessages
        # (data/moves.ts:18340), and modifyDamage adds `-crit` under exactly that
        # flag (sim/battle-actions.ts:1814), so a crit into a sub IS on the wire
        # and the crit roll set is the decidable one.
        mock_sets.return_value = (([10, 12], [40, 50]), None)
        self.battle.opponent.active.substitute_health = 60
        self.battle.opponent.active.substitute_health_low = 60
        self.battle.opponent.active.volatile_statuses.append(constants.SUBSTITUTE)

        lines, idx = self._block(
            "|move|p1a: Pikachu|Tackle|p2a: Caterpie",
            "|-crit|p2a: Caterpie",
            "|-activate|p2a: Caterpie|move: Substitute|[damage]",
        )
        activate(self.battle, lines[idx].split("|"), lines, idx)

        self.assertEqual(10, self.battle.opponent.active.substitute_health_low)
        self.assertEqual(20, self.battle.opponent.active.substitute_health)

    @mock.patch("fp.battle_modifier.poke_engine_get_damage_roll_sets")
    def test_activate_refuses_on_a_delayed_move(self, mock_sets):
        # synth18317 T42: a Future Sight resolving into the sub has no |move|
        # line of its own and its source need not even be on the field, so
        # `last_used_move` names an unrelated move.  REFUSE (HANDOFF 4.4)
        # instead of subtracting the wrong move's damage.
        battle_modifier.reset_substitute_absorb_refusals()
        self.battle.opponent.active.substitute_health = 82
        self.battle.opponent.active.substitute_health_low = 82
        self.battle.opponent.active.volatile_statuses.append(constants.SUBSTITUTE)

        lines, idx = self._block(
            "|",
            "|-end|p2a: Caterpie|move: Future Sight",
            "|-resisted|p2a: Caterpie",
            "|-activate|p2a: Caterpie|move: Substitute|[damage]",
        )
        activate(self.battle, lines[idx].split("|"), lines, idx)

        mock_sets.assert_not_called()
        # the sub survived, so it holds at least 1 and cannot have gained HP
        self.assertEqual(1, self.battle.opponent.active.substitute_health_low)
        self.assertEqual(82, self.battle.opponent.active.substitute_health)
        self.assertEqual(
            1, battle_modifier.SUBSTITUTE_ABSORB_REFUSALS["delayed_move"]
        )

    @mock.patch("fp.battle_modifier.poke_engine_get_damage_roll_sets")
    def test_activate_refuses_on_a_multi_hit_context(self, mock_sets):
        # one `-activate` per landed hit, but the preview API takes no hit index
        battle_modifier.reset_substitute_absorb_refusals()
        self.battle.opponent.active.substitute_health = 50
        self.battle.opponent.active.substitute_health_low = 50
        self.battle.opponent.active.volatile_statuses.append(constants.SUBSTITUTE)

        lines = [
            "|move|p1a: Pikachu|Bullet Seed|p2a: Caterpie",
            "|-activate|p2a: Caterpie|move: Substitute|[damage]",
            "|-activate|p2a: Caterpie|move: Substitute|[damage]",
        ]
        activate(self.battle, lines[1].split("|"), lines, 1)

        mock_sets.assert_not_called()
        self.assertEqual(1, self.battle.opponent.active.substitute_health_low)
        self.assertEqual(50, self.battle.opponent.active.substitute_health)
        self.assertEqual(
            1, battle_modifier.SUBSTITUTE_ABSORB_REFUSALS["multi_hit_context"]
        )

    @mock.patch("fp.battle_modifier.poke_engine_get_damage_roll_sets")
    def test_activate_refuses_without_block_context(self, mock_sets):
        # no block given -> the absorbing hit is unidentifiable.  The old code
        # fell back to `last_used_move` here; that fallback is what made a
        # delayed move silently wrong, so it is gone.
        battle_modifier.reset_substitute_absorb_refusals()
        self.battle.opponent.active.substitute_health = 50
        self.battle.opponent.active.substitute_health_low = 50
        self.battle.opponent.active.volatile_statuses.append(constants.SUBSTITUTE)
        self.battle.user.last_used_move = LastUsedMove("pikachu", "tackle", 1)

        split_msg = ["", "-activate", "p2a: Caterpie", "move: Substitute", "[damage]"]
        activate(self.battle, split_msg)

        mock_sets.assert_not_called()
        self.assertEqual(1, self.battle.opponent.active.substitute_health_low)
        self.assertEqual(50, self.battle.opponent.active.substitute_health)
        self.assertEqual(
            1, battle_modifier.SUBSTITUTE_ABSORB_REFUSALS["no_hit_context"]
        )

    @mock.patch("fp.battle_modifier.poke_engine_get_damage_roll_sets")
    def test_activate_floors_surviving_sub_at_one(self, mock_sets):
        # PS only emits `-activate ... [damage]` when the sub SURVIVES (hp > 0),
        # so an absorbed range whose TOP overshoots must floor at 1, never 0 --
        # and the same fact bounds the damage from above (it was strictly less
        # than the HP the sub held), which is what keeps the floor at 1 rather
        # than dragging the lower bound below it.
        mock_sets.return_value = (([45, 48, 60], [0, 0, 0]), None)
        self.battle.opponent.active.substitute_health = 50
        self.battle.opponent.active.substitute_health_low = 50
        self.battle.opponent.active.volatile_statuses.append(constants.SUBSTITUTE)

        lines, idx = self._block(
            "|move|p1a: Pikachu|Tackle|p2a: Caterpie",
            "|-activate|p2a: Caterpie|move: Substitute|[damage]",
        )
        activate(self.battle, lines[idx].split("|"), lines, idx)

        self.assertEqual(1, self.battle.opponent.active.substitute_health_low)
        self.assertEqual(5, self.battle.opponent.active.substitute_health)

    @mock.patch("fp.battle_modifier.poke_engine_get_damage_roll_sets")
    def test_activate_reports_a_survival_contradiction(self, mock_sets):
        # the SMALLEST roll the engine allows already meets the LARGEST HP the
        # sub could hold, yet PS said the sub survived.  Something upstream is
        # wrong; nothing here may be asserted, and the disagreement is COUNTED
        # rather than papered over.
        battle_modifier.reset_substitute_absorb_refusals()
        mock_sets.return_value = (([80, 90, 100], [0, 0, 0]), None)
        self.battle.opponent.active.substitute_health = 50
        self.battle.opponent.active.substitute_health_low = 50
        self.battle.opponent.active.volatile_statuses.append(constants.SUBSTITUTE)

        lines, idx = self._block(
            "|move|p1a: Pikachu|Tackle|p2a: Caterpie",
            "|-activate|p2a: Caterpie|move: Substitute|[damage]",
        )
        activate(self.battle, lines[idx].split("|"), lines, idx)

        self.assertEqual(
            1, battle_modifier.SUBSTITUTE_ABSORB_REFUSALS["survival_contradiction"]
        )
        self.assertEqual(1, self.battle.opponent.active.substitute_health_low)
        self.assertEqual(50, self.battle.opponent.active.substitute_health)

    @mock.patch("fp.battle_modifier.poke_engine_get_damage_roll_sets")
    def test_activate_uses_the_exact_team_sidecar_when_attached(self, mock_sets):
        # synth45492 T27: deriving with the LIVE-TRACKING opponent set (unknown
        # item, guessed spread) contradicted the exact set the rest of the
        # checker replays with.  When the sidecar is attached to the battle the
        # derivation must compute with it.
        mock_sets.return_value = (None, ([10], [20]))
        self.battle.user.active.max_hp = 200
        self.battle.user.active.substitute_health = 50
        self.battle.user.active.substitute_health_low = 50
        self.battle.user.active.volatile_statuses.append(constants.SUBSTITUTE)
        self.battle.exact_teams = {
            "p2": {"caterpie": {"item": "Choice Specs", "ability": "Shield Dust"}}
        }

        lines, idx = self._block(
            "|move|p2a: Caterpie|Tackle|p1a: Pikachu",
            "|-activate|p1a: Pikachu|move: Substitute|[damage]",
        )
        activate(self.battle, lines[idx].split("|"), lines, idx)

        # the battle handed to the engine call is the sidecar-applied COPY
        derived_battle = mock_sets.call_args[0][0]
        self.assertIsNot(self.battle, derived_battle)
        self.assertEqual("choicespecs", derived_battle.opponent.active.item)
        # ...and the live battle is untouched by the derivation
        self.assertEqual(constants.UNKNOWN_ITEM, self.battle.opponent.active.item)

    @mock.patch("fp.battle_modifier.poke_engine_get_damage_roll_sets")
    def test_activate_without_tracked_sub_skips_damage_calc(self, mock_rolls):
        # a bare state (no SUBSTITUTE volatile / no tracked hp) must not invoke
        # the engine damage calc, but still records substitute_hit
        split_msg = ["", "-activate", "p2a: Caterpie", "Substitute", "[damage]"]
        activate(self.battle, split_msg)
        self.assertTrue(self.battle.opponent.active.substitute_hit)
        mock_rolls.assert_not_called()

    def test_seed_uses_tracked_health(self):
        battler = Battler()
        battler.active = Pokemon("pawmot", 82)
        battler.active.volatile_statuses.append(constants.SUBSTITUTE)
        battler.active.substitute_health = 33
        side = battler_to_poke_engine_side(battler)
        self.assertEqual(33, side.substitute_health)

    def test_seed_falls_back_to_quarter_maxhp_when_untracked(self):
        battler = Battler()
        battler.active = Pokemon("pawmot", 82)
        battler.active.volatile_statuses.append(constants.SUBSTITUTE)
        battler.active.substitute_health = 0  # creation event never observed
        side = battler_to_poke_engine_side(battler)
        self.assertEqual(battler.active.max_hp // 4, side.substitute_health)

    def test_seed_zero_without_substitute(self):
        battler = Battler()
        battler.active = Pokemon("pawmot", 82)
        battler.active.substitute_health = 99  # stale, no volatile
        side = battler_to_poke_engine_side(battler)
        self.assertEqual(0, side.substitute_health)


class TestSubstituteHealthBatonPass(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.user.active = Pokemon("pikachu", 100)
        self.battle.opponent.active = Pokemon("caterpie", 100)
        self.battle.opponent.reserve = []

    def test_baton_pass_carries_substitute_health(self):
        self.battle.user.active.volatile_statuses = [constants.SUBSTITUTE]
        self.battle.user.active.substitute_health = 40
        self.battle.user.active.substitute_health_low = 35
        weedle = Pokemon("weedle", 100)
        self.battle.user.reserve = [weedle]

        split_msg = [
            "",
            "switch",
            "p1a: Weedle",
            "Weedle, L100, M",
            "100/100",
            "[from] Baton Pass",
        ]
        switch_or_drag(self.battle, split_msg)

        self.assertIs(weedle, self.battle.user.active)
        self.assertEqual(40, self.battle.user.active.substitute_health)
        self.assertEqual(35, self.battle.user.active.substitute_health_low)
        self.assertIn(constants.SUBSTITUTE, self.battle.user.active.volatile_statuses)

    def test_normal_switch_out_clears_substitute_health(self):
        self.battle.user.active.volatile_statuses = [constants.SUBSTITUTE]
        self.battle.user.active.substitute_health = 40
        outgoing = self.battle.user.active
        weedle = Pokemon("weedle", 100)
        self.battle.user.reserve = [weedle]

        split_msg = ["", "switch", "p1a: Weedle", "Weedle, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual(0, outgoing.substitute_health)
        self.assertEqual(0, outgoing.substitute_health_low)
        self.assertEqual(0, self.battle.user.active.substitute_health)


# ---------------------------------------------------------------------------
# Fix 1: partial-trap / Magnet-Rise elapsed-count reconstruction + forwarding
# ---------------------------------------------------------------------------
class TestPartialTrapMagnetRiseUpkeep(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.user.active = Pokemon("weedle", 100)
        self.battle.opponent.active = Pokemon("caterpie", 100)

    def test_partiallytrapped_elapsed_count_increments_each_eot(self):
        # engine consumes this as an elapsed count and ticks it up while < 4
        # (generate_instructions.rs:6657-6667); reconstruct that per real EOT.
        self.battle.generation = "gen9"
        self.battle.opponent.active.volatile_statuses.append(
            constants.PARTIALLY_TRAPPED
        )
        upkeep(self.battle, "")
        self.assertEqual(
            1,
            self.battle.opponent.active.volatile_status_durations[
                constants.PARTIALLY_TRAPPED
            ],
        )

    def test_partiallytrapped_count_caps_at_four(self):
        # magnetrise/partiallytrapped seeds > 4 panic / mis-release the engine
        # match arms, so the reconstructed count must never exceed 4.
        self.battle.generation = "gen9"
        self.battle.opponent.active.volatile_statuses.append(
            constants.PARTIALLY_TRAPPED
        )
        self.battle.opponent.active.volatile_status_durations[
            constants.PARTIALLY_TRAPPED
        ] = 4
        upkeep(self.battle, "")
        self.assertEqual(
            4,
            self.battle.opponent.active.volatile_status_durations[
                constants.PARTIALLY_TRAPPED
            ],
        )

    def test_gen1_partiallytrapped_not_ticked_at_eot(self):
        # gen1 applies and releases the partial trap at move-time (fp move
        # handler), so the EOT tick must skip gen1 to avoid double-counting.
        self.battle.generation = "gen1"
        self.battle.opponent.active.volatile_statuses.append(
            constants.PARTIALLY_TRAPPED
        )
        upkeep(self.battle, "")
        self.assertEqual(
            0,
            self.battle.opponent.active.volatile_status_durations[
                constants.PARTIALLY_TRAPPED
            ],
        )

    def test_magnetrise_elapsed_count_increments_each_eot(self):
        # PS moves.ts magnetrise duration 5, onResidualOrder 18; the engine ticks
        # 0..=3 -> +1, 4 -> release (generate_instructions.rs:6700-6733).
        self.battle.generation = "gen9"
        self.battle.opponent.active.volatile_statuses.append(constants.MAGNET_RISE)
        upkeep(self.battle, "")
        self.assertEqual(
            1,
            self.battle.opponent.active.volatile_status_durations[
                constants.MAGNET_RISE
            ],
        )

    def test_magnetrise_count_caps_at_four(self):
        self.battle.generation = "gen9"
        self.battle.opponent.active.volatile_statuses.append(constants.MAGNET_RISE)
        self.battle.opponent.active.volatile_status_durations[
            constants.MAGNET_RISE
        ] = 4
        upkeep(self.battle, "")
        self.assertEqual(
            4,
            self.battle.opponent.active.volatile_status_durations[
                constants.MAGNET_RISE
            ],
        )

    def test_no_volatile_means_no_tick(self):
        self.battle.generation = "gen9"
        upkeep(self.battle, "")
        self.assertEqual(
            0,
            self.battle.opponent.active.volatile_status_durations[
                constants.PARTIALLY_TRAPPED
            ],
        )
        self.assertEqual(
            0,
            self.battle.opponent.active.volatile_status_durations[
                constants.MAGNET_RISE
            ],
        )


class TestPartialTrapMagnetRiseForwarding(unittest.TestCase):
    def setUp(self):
        self.battler = Battler()
        self.battler.active = Pokemon("pawmot", 82)

    def test_support_flag_matches_binary(self):
        from poke_engine import VolatileStatusDurations as VSD

        self.assertEqual(
            POKE_ENGINE_SUPPORTS_TRAP_MAGNETRISE_DURATION,
            hasattr(VSD, "partiallytrapped"),
        )

    def test_conversion_succeeds_regardless_of_support(self):
        # wheel-compat: whether or not the binary exposes the fields, a battler
        # carrying the volatiles + durations must convert without raising.
        self.battler.active.volatile_statuses.append(constants.PARTIALLY_TRAPPED)
        self.battler.active.volatile_status_durations[
            constants.PARTIALLY_TRAPPED
        ] = 3
        self.battler.active.volatile_statuses.append(constants.MAGNET_RISE)
        self.battler.active.volatile_status_durations[constants.MAGNET_RISE] = 2
        side = battler_to_poke_engine_side(self.battler)
        self.assertEqual(6, len(side.pokemon))

    def test_forwards_clamped_counts_when_supported(self):
        if not POKE_ENGINE_SUPPORTS_TRAP_MAGNETRISE_DURATION:
            self.skipTest(
                "installed poke_engine binary does not expose the "
                "partiallytrapped/magnetrise duration fields"
            )
        self.battler.active.volatile_statuses.append(constants.PARTIALLY_TRAPPED)
        # a random(5,7) overshoot must be clamped to the folded 5-turn model
        self.battler.active.volatile_status_durations[
            constants.PARTIALLY_TRAPPED
        ] = 7
        self.battler.active.volatile_statuses.append(constants.MAGNET_RISE)
        self.battler.active.volatile_status_durations[constants.MAGNET_RISE] = 3
        side = battler_to_poke_engine_side(self.battler)
        self.assertEqual(4, side.volatile_status_durations.partiallytrapped)
        self.assertEqual(3, side.volatile_status_durations.magnetrise)


# ---------------------------------------------------------------------------
# Hardening: gaps not covered by the assertions above
# ---------------------------------------------------------------------------
class TestPartialTrapMagnetRiseUpkeepBothSides(unittest.TestCase):
    """`upkeep` loops over `[battle.user, battle.opponent]`; the bot's OWN
    elapsed count is load-bearing for search (how long *we* have been trapped /
    levitating), so assert the user side ticks too - the other upkeep tests only
    exercise the opponent side."""

    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.user.active = Pokemon("weedle", 100)
        self.battle.opponent.active = Pokemon("caterpie", 100)
        self.battle.generation = "gen9"

    def test_user_side_partiallytrapped_and_magnetrise_tick(self):
        self.battle.user.active.volatile_statuses.append(constants.PARTIALLY_TRAPPED)
        self.battle.user.active.volatile_statuses.append(constants.MAGNET_RISE)
        upkeep(self.battle, "")
        self.assertEqual(
            1,
            self.battle.user.active.volatile_status_durations[
                constants.PARTIALLY_TRAPPED
            ],
        )
        self.assertEqual(
            1,
            self.battle.user.active.volatile_status_durations[constants.MAGNET_RISE],
        )

    def test_both_sides_tick_independently_in_one_upkeep(self):
        self.battle.user.active.volatile_statuses.append(constants.MAGNET_RISE)
        self.battle.opponent.active.volatile_statuses.append(
            constants.PARTIALLY_TRAPPED
        )
        # opponent is further along
        self.battle.opponent.active.volatile_status_durations[
            constants.PARTIALLY_TRAPPED
        ] = 2
        upkeep(self.battle, "")
        self.assertEqual(
            1,
            self.battle.user.active.volatile_status_durations[constants.MAGNET_RISE],
        )
        self.assertEqual(
            3,
            self.battle.opponent.active.volatile_status_durations[
                constants.PARTIALLY_TRAPPED
            ],
        )


# ---------------------------------------------------------------------------
# Stale trap counter: `-end ... [partiallytrapped]` must zero the elapsed count
# ---------------------------------------------------------------------------
class TestPartialTrapEndZeroesElapsedCount(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.user.active = Pokemon("weedle", 100)
        self.battle.opponent.active = Pokemon("caterpie", 100)
        self.battle.generation = "gen9"

    def test_retrap_after_silent_end_converts_with_counter_zero(self):
        # trap runs two EOTs, then the trapper leaves the field: PS
        # data/conditions.ts partiallytrapped onResidual emits the SILENT
        # `-end ... [partiallytrapped] [silent]`. A later re-trap
        # (`-activate ... move: Whirlpool`) starts a FRESH trap, so the root
        # conversion must seed the engine's elapsed count at 0, not the stale 2.
        self.battle.opponent.active.volatile_statuses.append(
            constants.PARTIALLY_TRAPPED
        )
        upkeep(self.battle, "")
        upkeep(self.battle, "")
        self.assertEqual(
            2,
            self.battle.opponent.active.volatile_status_durations[
                constants.PARTIALLY_TRAPPED
            ],
        )

        end_split = [
            "",
            "-end",
            "p2a: Caterpie",
            "whirlpool",
            "[partiallytrapped]",
            "[silent]",
        ]
        end_volatile_status(self.battle, end_split)

        retrap_split = [
            "",
            "-activate",
            "p2a: Caterpie",
            "move: Whirlpool",
            "[of] p1a: Weedle",
        ]
        activate(self.battle, retrap_split)
        self.assertIn(
            constants.PARTIALLY_TRAPPED,
            self.battle.opponent.active.volatile_statuses,
        )
        # the tracked count (the conversion's source of truth) must be fresh
        self.assertEqual(
            0,
            self.battle.opponent.active.volatile_status_durations[
                constants.PARTIALLY_TRAPPED
            ],
        )

        # conversion must succeed on every wheel; the engine-side field only
        # exists once the binary exposes partiallytrapped (0.0.48+)
        side = battler_to_poke_engine_side(self.battle.opponent)
        if POKE_ENGINE_SUPPORTS_TRAP_MAGNETRISE_DURATION:
            self.assertEqual(0, side.volatile_status_durations.partiallytrapped)


# ---------------------------------------------------------------------------
# Confusion age counter: count PS `-activate ... confusion` checks-survived
# ---------------------------------------------------------------------------
class TestConfusionAgeCounter(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.user.active = Pokemon("weedle", 100)
        self.battle.opponent.active = Pokemon("caterpie", 100)

    def _activate_confusion(self, times=1):
        for _ in range(times):
            activate(self.battle, ["", "-activate", "p2a: Caterpie", "confusion"])

    def test_activate_increments_counter_from_zero(self):
        # PS data/conditions.ts:186 emits `-activate ... confusion` on every
        # before-move check where confusion did NOT wear off (after the
        # wear-off early-return :180-185, before the 33% self-hit roll :187),
        # so each line is exactly one engine check-survived tick.
        self.battle.opponent.active.volatile_statuses.append(constants.CONFUSION)
        self._activate_confusion()
        self.assertEqual(
            1,
            self.battle.opponent.active.volatile_status_durations[
                constants.CONFUSION
            ],
        )
        self._activate_confusion()
        self.assertEqual(
            2,
            self.battle.opponent.active.volatile_status_durations[
                constants.CONFUSION
            ],
        )

    def test_counter_clamps_at_four(self):
        # engine MAX_CONFUSION_TURNS = 4 (generate_instructions.rs:136): a
        # counter of 4 forces removal on the next simulated check, and PS's
        # time = random(2,6) yields at most 4 surviving checks anyway.
        self.battle.opponent.active.volatile_statuses.append(constants.CONFUSION)
        self._activate_confusion(times=6)
        self.assertEqual(
            4,
            self.battle.opponent.active.volatile_status_durations[
                constants.CONFUSION
            ],
        )

    def test_end_confusion_resets_counter(self):
        # PS conditions.ts confusion onEnd: `-end ... confusion` on wear-off
        self.battle.opponent.active.volatile_statuses.append(constants.CONFUSION)
        self._activate_confusion(times=3)
        end_volatile_status(self.battle, ["", "-end", "p2a: Caterpie", "confusion"])
        self.assertNotIn(
            constants.CONFUSION, self.battle.opponent.active.volatile_statuses
        )
        self.assertEqual(
            0,
            self.battle.opponent.active.volatile_status_durations[
                constants.CONFUSION
            ],
        )

    def test_switch_resets_counter(self):
        # PS clearVolatile on switch-out drops confusion entirely; foul-play's
        # switch_or_drag clears every volatile_status_duration with it
        self.battle.user.active.volatile_statuses.append(constants.CONFUSION)
        self.battle.user.active.volatile_status_durations[constants.CONFUSION] = 3
        outgoing = self.battle.user.active
        self.battle.user.reserve = [Pokemon("pikachu", 100)]

        split_msg = ["", "switch", "p1a: Pikachu", "Pikachu, L100, M", "100/100"]
        switch_or_drag(self.battle, split_msg)

        self.assertEqual(
            0, outgoing.volatile_status_durations[constants.CONFUSION]
        )
        self.assertEqual([], outgoing.volatile_statuses)

    def test_counter_forwarded_by_conversion(self):
        # poke_engine_helpers forwards the raw counter (every wheel exposes
        # `confusion`); the engine folds it via chance_confusion_active
        battler = Battler()
        battler.active = Pokemon("caterpie", 100)
        battler.active.volatile_statuses.append(constants.CONFUSION)
        battler.active.volatile_status_durations[constants.CONFUSION] = 3
        side = battler_to_poke_engine_side(battler)
        self.assertEqual(3, side.volatile_status_durations.confusion)


# ---------------------------------------------------------------------------
# Heal Block / Throat Chop / Syrup Bomb elapsed-EOT reconstruction + forwarding
# ---------------------------------------------------------------------------
class TestHealblockThroatchopSyrupbombUpkeep(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.user.active = Pokemon("weedle", 100)
        self.battle.opponent.active = Pokemon("caterpie", 100)
        self.battle.generation = "gen9"

    def test_start_protocol_lines_track_the_volatiles(self):
        # the generic `-start` handler must catch all three PS shapes:
        # `-start ... move: Heal Block` (data/moves.ts:8300),
        # `-start ... Throat Chop [silent]` (data/moves.ts:19395),
        # `-start ... Syrup Bomb` (data/moves.ts:18768)
        start_volatile_status(
            self.battle, ["", "-start", "p2a: Caterpie", "move: Heal Block"]
        )
        start_volatile_status(
            self.battle, ["", "-start", "p2a: Caterpie", "Throat Chop", "[silent]"]
        )
        start_volatile_status(
            self.battle, ["", "-start", "p2a: Caterpie", "Syrup Bomb"]
        )
        for volatile in (
            constants.HEAL_BLOCK,
            constants.THROAT_CHOP,
            constants.SYRUP_BOMB,
        ):
            self.assertIn(volatile, self.battle.opponent.active.volatile_statuses)
            self.assertEqual(
                0,
                self.battle.opponent.active.volatile_status_durations[volatile],
            )

    def test_healblock_ticks_and_clamps_at_one(self):
        # engine healblock EOT arm: 0 -> tick to 1, release at 1, panic above 1
        self.battle.opponent.active.volatile_statuses.append(constants.HEAL_BLOCK)
        upkeep(self.battle, "")
        self.assertEqual(
            1,
            self.battle.opponent.active.volatile_status_durations[
                constants.HEAL_BLOCK
            ],
        )
        upkeep(self.battle, "")
        self.assertEqual(
            1,
            self.battle.opponent.active.volatile_status_durations[
                constants.HEAL_BLOCK
            ],
        )

    def test_throatchop_ticks_and_clamps_at_one(self):
        # engine throatchop EOT arm: release at exactly 1 - a higher seed
        # would tick forever and never release
        self.battle.opponent.active.volatile_statuses.append(constants.THROAT_CHOP)
        upkeep(self.battle, "")
        self.assertEqual(
            1,
            self.battle.opponent.active.volatile_status_durations[
                constants.THROAT_CHOP
            ],
        )
        upkeep(self.battle, "")
        self.assertEqual(
            1,
            self.battle.opponent.active.volatile_status_durations[
                constants.THROAT_CHOP
            ],
        )

    def test_syrupbomb_ticks_and_clamps_at_three(self):
        # engine syrupbomb EOT arm: 0|1|2 -> drop + tick, release at 3, panic
        # above 3
        self.battle.opponent.active.volatile_statuses.append(constants.SYRUP_BOMB)
        for expected in (1, 2, 3, 3):
            upkeep(self.battle, "")
            self.assertEqual(
                expected,
                self.battle.opponent.active.volatile_status_durations[
                    constants.SYRUP_BOMB
                ],
            )

    def test_no_volatile_means_no_tick(self):
        upkeep(self.battle, "")
        for volatile in (
            constants.HEAL_BLOCK,
            constants.THROAT_CHOP,
            constants.SYRUP_BOMB,
        ):
            self.assertEqual(
                0,
                self.battle.opponent.active.volatile_status_durations[volatile],
            )

    def test_end_protocol_lines_zero_the_counters(self):
        # PS release lines: `-end ... move: Heal Block` (data/moves.ts:8325),
        # `-end ... Throat Chop [silent]` (:19419),
        # `-end ... Syrup Bomb [silent]` (:18780) - the generic `-end` handler
        # removes the volatile and zeroes its duration
        for volatile, end_split in (
            (constants.HEAL_BLOCK, ["", "-end", "p2a: Caterpie", "move: Heal Block"]),
            (
                constants.THROAT_CHOP,
                ["", "-end", "p2a: Caterpie", "Throat Chop", "[silent]"],
            ),
            (
                constants.SYRUP_BOMB,
                ["", "-end", "p2a: Caterpie", "Syrup Bomb", "[silent]"],
            ),
        ):
            self.battle.opponent.active.volatile_statuses.append(volatile)
            self.battle.opponent.active.volatile_status_durations[volatile] = 1
            end_volatile_status(self.battle, end_split)
            self.assertNotIn(
                volatile, self.battle.opponent.active.volatile_statuses
            )
            self.assertEqual(
                0,
                self.battle.opponent.active.volatile_status_durations[volatile],
            )


class TestHealblockThroatchopSyrupbombForwarding(unittest.TestCase):
    def setUp(self):
        self.battler = Battler()
        self.battler.active = Pokemon("caterpie", 100)

    def test_support_flag_matches_binary(self):
        from poke_engine import VolatileStatusDurations as VSD

        self.assertEqual(
            POKE_ENGINE_SUPPORTS_HEALBLOCK_THROATCHOP_SYRUPBOMB_DURATION,
            all(
                hasattr(VSD, f) for f in ("healblock", "throatchop", "syrupbomb")
            ),
        )

    def test_conversion_succeeds_regardless_of_support(self):
        self.battler.active.volatile_statuses.extend(
            [constants.HEAL_BLOCK, constants.THROAT_CHOP, constants.SYRUP_BOMB]
        )
        self.battler.active.volatile_status_durations[constants.HEAL_BLOCK] = 1
        self.battler.active.volatile_status_durations[constants.THROAT_CHOP] = 1
        self.battler.active.volatile_status_durations[constants.SYRUP_BOMB] = 2
        side = battler_to_poke_engine_side(self.battler)
        self.assertEqual(6, len(side.pokemon))

    def test_forwards_counts_when_supported(self):
        if not POKE_ENGINE_SUPPORTS_HEALBLOCK_THROATCHOP_SYRUPBOMB_DURATION:
            self.skipTest(
                "installed poke_engine binary does not expose the "
                "healblock/throatchop/syrupbomb duration fields"
            )
        self.battler.active.volatile_statuses.extend(
            [constants.HEAL_BLOCK, constants.THROAT_CHOP, constants.SYRUP_BOMB]
        )
        self.battler.active.volatile_status_durations[constants.HEAL_BLOCK] = 1
        self.battler.active.volatile_status_durations[constants.THROAT_CHOP] = 1
        self.battler.active.volatile_status_durations[constants.SYRUP_BOMB] = 2
        side = battler_to_poke_engine_side(self.battler)
        self.assertEqual(1, side.volatile_status_durations.healblock)
        self.assertEqual(1, side.volatile_status_durations.throatchop)
        self.assertEqual(2, side.volatile_status_durations.syrupbomb)

    def test_forwards_clamped_counts_when_supported(self):
        # stale/overshot counts must clamp to the engine release counters:
        # healblock panics above 1, throatchop never releases above 1,
        # syrupbomb panics above 3
        if not POKE_ENGINE_SUPPORTS_HEALBLOCK_THROATCHOP_SYRUPBOMB_DURATION:
            self.skipTest(
                "installed poke_engine binary does not expose the "
                "healblock/throatchop/syrupbomb duration fields"
            )
        self.battler.active.volatile_statuses.extend(
            [constants.HEAL_BLOCK, constants.THROAT_CHOP, constants.SYRUP_BOMB]
        )
        self.battler.active.volatile_status_durations[constants.HEAL_BLOCK] = 5
        self.battler.active.volatile_status_durations[constants.THROAT_CHOP] = 3
        self.battler.active.volatile_status_durations[constants.SYRUP_BOMB] = 7
        side = battler_to_poke_engine_side(self.battler)
        self.assertEqual(1, side.volatile_status_durations.healblock)
        self.assertEqual(1, side.volatile_status_durations.throatchop)
        self.assertEqual(3, side.volatile_status_durations.syrupbomb)


class TestSubstituteSeedBoundary(unittest.TestCase):
    def test_seed_int_casts_float_maxhp_fallback(self):
        # max_hp can be a float after the `/= 2` un-Dynamax, and the engine field
        # is an integer; the maxhp/4 fallback must be int()-coerced (a float would
        # be rejected at the FFI boundary).
        battler = Battler()
        battler.active = Pokemon("pawmot", 82)
        battler.active.max_hp = 201.0  # float
        battler.active.volatile_statuses.append(constants.SUBSTITUTE)
        battler.active.substitute_health = 0  # untracked -> maxhp/4 fallback
        side = battler_to_poke_engine_side(battler)
        self.assertEqual(int(201.0 / 4), side.substitute_health)
        self.assertIsInstance(side.substitute_health, int)


def _fresh_battle(generation="gen9"):
    battle = Battle(None)
    battle.user.name = "p1"
    battle.opponent.name = "p2"
    battle.generation = generation
    battle.user.active = Pokemon("weedle", 100)
    battle.opponent.active = Pokemon("caterpie", 100)
    return battle


# ---------------------------------------------------------------------------
# Volatile name-strip restricted to genuine two-turn charge moves
# ---------------------------------------------------------------------------
# move() used to strip ANY volatile matching the used move's name; a failed
# Substitute/Magnet Rise re-use deleted the real tracked volatile (and its
# ground-immunity/sub state). Only moves with PS's `charge` flag
# (data/moves.ts flags.charge - the twoturnmove chargers) may strip.
class TestChargeOnlyVolatileNameStrip(unittest.TestCase):
    def setUp(self):
        self.battle = _fresh_battle()

    def test_failed_substitute_reuse_retains_substitute_volatile(self):
        self.battle.opponent.active.volatile_statuses = [constants.SUBSTITUTE]
        move(self.battle, ["", "move", "p2a: Caterpie", "Substitute"])
        self.assertIn(
            constants.SUBSTITUTE, self.battle.opponent.active.volatile_statuses
        )

    def test_magnet_rise_reuse_retains_magnetrise_volatile(self):
        self.battle.opponent.active.volatile_statuses = [constants.MAGNET_RISE]
        move(self.battle, ["", "move", "p2a: Caterpie", "Magnet Rise"])
        self.assertIn(
            constants.MAGNET_RISE, self.battle.opponent.active.volatile_statuses
        )

    def test_fly_release_still_strips_charge_volatile(self):
        # data/moves.ts fly: flags.charge - the release |move| line must still
        # clear the charging volatile
        self.battle.opponent.active.volatile_statuses = ["fly"]
        move(self.battle, ["", "move", "p2a: Caterpie", "Fly"])
        self.assertNotIn("fly", self.battle.opponent.active.volatile_statuses)


# ---------------------------------------------------------------------------
# Taunt/Encore apply seed: PS's 'already acted this turn' bump
# ---------------------------------------------------------------------------
# PS taunt/encore onStart: `if (!this.queue.willMove(target)) duration++`
# (data/moves.ts). The engine's gen5+ end-of-turn arm counts UP and releases
# at counter==2, seeding -1 on its own slow-apply branch
# (genx/generate_instructions.rs:1035-1071). The seed keys on BLOCK POSITION:
# whether the target's own action line already appeared this turn.
class TestTauntEncoreApplySeed(unittest.TestCase):
    def setUp(self):
        self.battle = _fresh_battle()

    def test_seed_minus_one_when_target_move_line_already_appeared(self):
        move(self.battle, ["", "move", "p2a: Caterpie", "Tackle"])
        start_volatile_status(
            self.battle, ["", "-start", "p2a: Caterpie", "move: Taunt"]
        )
        self.assertEqual(
            -1, self.battle.opponent.active.volatile_status_durations[constants.TAUNT]
        )

    def test_seed_zero_when_start_precedes_target_move_line(self):
        start_volatile_status(
            self.battle, ["", "-start", "p2a: Caterpie", "move: Taunt"]
        )
        self.assertEqual(
            0, self.battle.opponent.active.volatile_status_durations[constants.TAUNT]
        )

    def test_seed_minus_one_when_target_cant_line_already_appeared(self):
        # a consumed-but-aborted action (full para etc.) also means
        # `!this.queue.willMove(target)` in PS - the bump applies
        cant(self.battle, ["", "cant", "p2a: Caterpie", "par"])
        start_volatile_status(
            self.battle, ["", "-start", "p2a: Caterpie", "Encore"]
        )
        self.assertEqual(
            -1, self.battle.opponent.active.volatile_status_durations["encore"]
        )

    def test_called_move_line_alone_does_not_mark_target_as_acted(self):
        # pinned on block position: a CALLED move's line ('[from]move: Sleep
        # Talk') is not the action line - the calling |move|Sleep Talk line
        # (which always precedes it) is what marks the action
        move(
            self.battle,
            ["", "move", "p2a: Caterpie", "Tackle", "[from]move: Sleep Talk"],
        )
        start_volatile_status(
            self.battle, ["", "-start", "p2a: Caterpie", "move: Taunt"]
        )
        self.assertEqual(
            0, self.battle.opponent.active.volatile_status_durations[constants.TAUNT]
        )

    def test_sleep_talk_calling_line_marks_target_as_acted(self):
        move(self.battle, ["", "move", "p2a: Caterpie", "Sleep Talk"])
        move(
            self.battle,
            ["", "move", "p2a: Caterpie", "Tackle", "[from]move: Sleep Talk"],
        )
        start_volatile_status(
            self.battle, ["", "-start", "p2a: Caterpie", "move: Taunt"]
        )
        self.assertEqual(
            -1, self.battle.opponent.active.volatile_status_durations[constants.TAUNT]
        )

    def test_turn_boundary_resets_the_acted_marker(self):
        move(self.battle, ["", "move", "p2a: Caterpie", "Tackle"])
        turn(self.battle, ["", "turn", "7"])
        start_volatile_status(
            self.battle, ["", "-start", "p2a: Caterpie", "move: Taunt"]
        )
        self.assertEqual(
            0, self.battle.opponent.active.volatile_status_durations[constants.TAUNT]
        )

    def test_pre_gen5_does_not_seed(self):
        # legacy gens keep their move-tick modeling: the apply seed is gen5+
        battle = _fresh_battle(generation="gen4")
        battle.opponent.active.volatile_status_durations[constants.TAUNT] = 1
        move(battle, ["", "move", "p2a: Caterpie", "Tackle"])
        start_volatile_status(battle, ["", "-start", "p2a: Caterpie", "move: Taunt"])
        self.assertEqual(
            1, battle.opponent.active.volatile_status_durations[constants.TAUNT]
        )


# ---------------------------------------------------------------------------
# Transformed mon: request refresh must not overwrite copied stats/moves
# ---------------------------------------------------------------------------
# The transformed active's REQUEST BLOCK is authoritative for the moveset:
# `getMoveRequestData` -> `getMoves()` iterates `this.moveSlots`
# (sim/pokemon.ts:993-1046) and `transformInto` replaced moveSlots with the
# copied set at `pp = Math.min(5, move.pp)` / `maxpp = pp` for gen>=5
# (sim/pokemon.ts:1364-1379).  The side entry's `stats` are `baseStoredStats`
# (sim/pokemon.ts:1165-1171) - the UNtransformed values - so THAT is the field
# the transform guard protects.
#
# An earlier version of this class pinned the opposite (an early return that
# skipped the whole refresh).  That premise is falsified by the logs: over
# 20,000 corpus games, 1,338 requests taken while the user's active was
# transformed all carried the copied moveset, 1,333 with every `maxpp == 5` and
# the 5 exceptions Revival Blessing at maxpp 1 - the same `min(5, pp)` rule.
class TestTransformedRequestRefresh(unittest.TestCase):
    COPIED = ("stoneedge", "crunch", "earthquake", "dragondance")

    def _request(self, psyshock_pp=None):
        moves = [
            {
                "move": name.title(),
                "id": name,
                "pp": 5,
                "maxpp": 5,
                "target": "normal",
                "disabled": False,
            }
            for name in self.COPIED
        ]
        if psyshock_pp is not None:
            # the synth09899 shape: a move revealed AFTER the transform, whose
            # live PP only the request knows
            moves = [
                {
                    "move": "Psyshock",
                    "id": "psyshock",
                    "pp": psyshock_pp,
                    "maxpp": 5,
                    "target": "normal",
                    "disabled": False,
                }
            ] + moves[1:]
        return {
            "active": [{"moves": moves}],
            "side": {
                "name": "BigBluePikachu",
                "id": "p1",
                "pokemon": [
                    {
                        "ident": "p1: Ditto",
                        "details": "Ditto, L87",
                        "condition": "152/225",
                        "active": True,
                        # baseStoredStats: Ditto's own, NOT the copy's
                        "stats": {
                            "atk": 133,
                            "def": 133,
                            "spa": 133,
                            "spd": 133,
                            "spe": 133,
                        },
                        "moves": [m["id"] for m in moves],
                        "baseAbility": "imposter",
                        "item": "choicescarf",
                        "ability": "imposter",
                    }
                ],
            },
        }

    def _untransformed_request(self):
        return {
            "active": [
                {
                    "moves": [
                        {
                            "move": "Transform",
                            "id": "transform",
                            "pp": 15,
                            "maxpp": 16,
                            "target": "normal",
                            "disabled": False,
                        }
                    ]
                }
            ],
            "side": {
                "name": "BigBluePikachu",
                "id": "p1",
                "pokemon": [
                    {
                        "ident": "p1: Ditto",
                        "details": "Ditto, L87",
                        "condition": "152/225",
                        "active": True,
                        "stats": {
                            "atk": 133,
                            "def": 133,
                            "spa": 133,
                            "spd": 133,
                            "spe": 133,
                        },
                        "moves": ["transform"],
                        "baseAbility": "imposter",
                        "item": "choicescarf",
                        "ability": "imposter",
                    }
                ],
            },
        }

    def _transformed_ditto(self, extra_move=None, extra_move_pp=None):
        ditto = Pokemon("ditto", 87)
        ditto.volatile_statuses.append(constants.TRANSFORM)
        ditto.transformed_into = "tyranitar"
        ditto.stats = {
            constants.ATTACK: 268,
            constants.DEFENSE: 217,
            constants.SPECIAL_ATTACK: 203,
            constants.SPECIAL_DEFENSE: 217,
            constants.SPEED: 148,
        }
        ditto.moves = []
        for mv in self.COPIED:
            ditto.add_move(mv)
        for mv in ditto.moves:
            mv.current_pp = 5
        if extra_move is not None:
            # a move first seen AFTER the transform is added by the `move`
            # handler with the base-species PP object (max_pp = dex pp * 1.6)
            ditto.moves = list(ditto.moves)
            ditto.moves[0] = Move(extra_move)
            ditto.moves[0].current_pp = extra_move_pp
        return ditto

    def test_transformed_active_keeps_copied_stats(self):
        battler = Battler()
        battler.active = self._transformed_ditto()
        battler.update_from_request_json(self._request())

        # copied Tyranitar def stays 217 - not reverted to Ditto's baseStoredStats
        # 133 (synth01707: Ditto-as-Tyranitar was hit at def 133 instead of 217)
        self.assertEqual(217, battler.active.stats[constants.DEFENSE])

    def test_transformed_active_moveset_refreshed_from_request(self):
        battler = Battler()
        battler.active = self._transformed_ditto()
        battler.update_from_request_json(self._request())

        self.assertEqual(list(self.COPIED), [m.name for m in battler.active.moves])
        self.assertEqual([5, 5, 5, 5], [m.current_pp for m in battler.active.moves])

    def test_transformed_active_pp_of_post_transform_reveal_comes_from_request(self):
        # synth09899 T19: Ditto (Imposter -> Flutter Mane) had Psyshock revealed
        # after the transform, so it carried the base-species PP object and read
        # 16-4=12 while the request said 1.  Encore's "encored move hit 0 PP"
        # termination is a function of that number, so the engine never ended the
        # Encore the log shows ending.
        battler = Battler()
        battler.active = self._transformed_ditto(
            extra_move="psyshock", extra_move_pp=12
        )
        self.assertEqual(12, battler.active.moves[0].current_pp)

        battler.update_from_request_json(self._request(psyshock_pp=1))

        self.assertEqual("psyshock", battler.active.moves[0].name)
        self.assertEqual(1, battler.active.moves[0].current_pp)

    def test_control_flag_restores_the_stale_pp(self):
        # PROVE THE FIX CAN FAIL.  With the pre-fix early return forced back on,
        # the stale base-species PP survives the request.  If this assertion ever
        # stops holding the reproducer went stale and a new one is owed - it does
        # NOT mean the refresh became unnecessary.
        battler = Battler()
        battler.active = self._transformed_ditto(
            extra_move="psyshock", extra_move_pp=12
        )
        with mock.patch.dict(
            os.environ, {"FP_CONTROL_TRANSFORMED_ACTIVE_SKIP": "1"}, clear=False
        ):
            battler.update_from_request_json(self._request(psyshock_pp=1))
        self.assertEqual(12, battler.active.moves[0].current_pp)

    def test_untransformed_active_still_refreshed_from_request(self):
        battler = Battler()
        battler.active = Pokemon("ditto", 87)
        battler.active.stats[constants.DEFENSE] = 999
        battler.update_from_request_json(self._untransformed_request())

        self.assertEqual(133, battler.active.stats[constants.DEFENSE])
        self.assertEqual(["transform"], [m.name for m in battler.active.moves])


# ---------------------------------------------------------------------------
# Tera cleared on faint; side tera stays spent
# ---------------------------------------------------------------------------
class TestTeraClearedOnFaint(unittest.TestCase):
    def setUp(self):
        self.battle = _fresh_battle()

    def test_faint_clears_terastallized_and_marks_side_tera_spent(self):
        # PS deletes `terastallized` on faint (sim/battle.ts:2565): a
        # Revival-Blessing-revived mon returns UN-tera'd
        self.battle.opponent.active.terastallized = True
        self.battle.opponent.active.tera_type = "flying"
        faint(self.battle, ["", "faint", "p2a: Caterpie"])
        self.assertFalse(self.battle.opponent.active.terastallized)
        self.assertTrue(self.battle.opponent.tera_spent)

    def test_faint_of_untera_mon_does_not_mark_tera_spent(self):
        faint(self.battle, ["", "faint", "p2a: Caterpie"])
        self.assertFalse(self.battle.opponent.tera_spent)

    def test_conversion_reflags_a_fainted_slot_when_tera_spent(self):
        # engine can_use_tera() scans the party for any terastallized pkmn
        # (genx/state.rs:980-987); with the faint-cleared flag the spent tera
        # is carried by re-flagging a FAINTED slot at conversion
        battler = Battler()
        battler.active = Pokemon("weedle", 100)
        fainted = Pokemon("caterpie", 100)
        fainted.hp = 0
        battler.reserve = [fainted]
        battler.tera_spent = True

        side = battler_to_poke_engine_side(battler)
        engine_party = side.pokemon
        self.assertFalse(engine_party[0].terastallized)
        self.assertTrue(engine_party[1].terastallized)

    def test_conversion_does_not_reflag_when_a_live_tera_exists(self):
        battler = Battler()
        battler.active = Pokemon("weedle", 100)
        battler.active.terastallized = True
        fainted = Pokemon("caterpie", 100)
        fainted.hp = 0
        battler.reserve = [fainted]
        battler.tera_spent = True

        side = battler_to_poke_engine_side(battler)
        engine_party = side.pokemon
        self.assertTrue(engine_party[0].terastallized)
        self.assertFalse(engine_party[1].terastallized)

    def test_conversion_without_tera_spent_flags_nothing(self):
        battler = Battler()
        battler.active = Pokemon("weedle", 100)
        fainted = Pokemon("caterpie", 100)
        fainted.hp = 0
        battler.reserve = [fainted]

        side = battler_to_poke_engine_side(battler)
        self.assertFalse(any(p.terastallized for p in side.pokemon))


# ---------------------------------------------------------------------------
# Glaive Rush volatile cleared on the holder's next action
# ---------------------------------------------------------------------------
# PS data/moves.ts glaiverush condition: onBeforeMove at priority 100 removes
# the volatile silently before every abort check (flinch 8, slp/frz 10,
# recharge 11, par 1) - any action attempt consumes the drawback.
class TestGlaiveRushClearedOnAction(unittest.TestCase):
    def setUp(self):
        self.battle = _fresh_battle()

    def test_cleared_on_holders_next_move_line(self):
        self.battle.opponent.active.volatile_statuses = ["glaiverush"]
        move(self.battle, ["", "move", "p2a: Caterpie", "Tackle"])
        self.assertNotIn(
            "glaiverush", self.battle.opponent.active.volatile_statuses
        )

    def test_cleared_on_holders_cant_line(self):
        self.battle.opponent.active.volatile_statuses = ["glaiverush"]
        cant(self.battle, ["", "cant", "p2a: Caterpie", "par"])
        self.assertNotIn(
            "glaiverush", self.battle.opponent.active.volatile_statuses
        )

    def test_not_cleared_by_the_other_sides_action(self):
        self.battle.opponent.active.volatile_statuses = ["glaiverush"]
        move(self.battle, ["", "move", "p1a: Weedle", "Tackle"])
        self.assertIn("glaiverush", self.battle.opponent.active.volatile_statuses)


# ---------------------------------------------------------------------------
# knocked_off cleared when a new item is gained
# ---------------------------------------------------------------------------
class TestKnockedOffClearedOnItemGain(unittest.TestCase):
    def test_item_gain_clears_knocked_off(self):
        # gen5+ Knock Off REMOVES the item and PS has no 'cannot regain' latch:
        # a later Trick/Pickpocket/Covet gain is a real held item. Without the
        # clear, engine conversion nulls the new item forever
        # (poke_engine_helpers itemless check; synth00911 T5).
        battle = _fresh_battle()
        battle.user.active.knocked_off = True
        set_item(
            battle,
            ["", "-item", "p1a: Weedle", "Life Orb", "[from] ability: Pickpocket"],
        )
        self.assertEqual("lifeorb", battle.user.active.item)
        self.assertFalse(battle.user.active.knocked_off)

        engine_pkmn = pokemon_to_poke_engine_pkmn(battle.user.active)
        self.assertEqual("lifeorb", str(engine_pkmn.item).lower())


# ---------------------------------------------------------------------------
# Non-permanent (battleOnly) formes revert on switch-out
# ---------------------------------------------------------------------------
# PS clearVolatile ends with setSpecies(this.baseSpecies)
# (sim/pokemon.ts:1514-1565): formes entered via |-formechange| (isPermanent
# falsy) revert when the mon leaves the field; |detailschange| formes
# reassigned baseSpecies and persist.
class TestBattleOnlyFormeRevertsOnSwitchOut(unittest.TestCase):
    def setUp(self):
        self.battle = _fresh_battle()

    def test_meloetta_pirouette_reverts_to_meloetta_on_switch_out(self):
        self.battle.opponent.active = Pokemon("meloetta", 82)
        form_change(
            self.battle,
            ["", "-formechange", "p2a: Meloetta", "Meloetta-Pirouette", "[msg]"],
        )
        self.assertEqual("meloettapirouette", self.battle.opponent.active.name)
        pirouette = self.battle.opponent.active

        switch_or_drag(
            self.battle, ["", "switch", "p2a: Caterpie", "Caterpie, L100", "100/100"]
        )

        # types/stats revert to base Meloetta (synth00266: Air Slash was
        # previewed at 2x into Pirouette's Normal/Fighting typing)
        self.assertEqual("meloetta", pirouette.name)
        self.assertEqual(["normal", "psychic"], list(pirouette.types))
        # base Meloetta L82 spd: floor(floor(2*128+31+floor(85/4))*82/100)+5
        # = floor(308*0.82)+5 = 252+5 = 257
        self.assertEqual(257, pirouette.stats[constants.SPECIAL_DEFENSE])
        self.assertFalse(pirouette.reverts_forme_on_switch_out)
        self.assertFalse(pirouette.forme_changed)

    def test_detailschange_forme_persists_on_switch_out(self):
        self.battle.opponent.active = Pokemon("terapagosterastal", 77)
        form_change(
            self.battle,
            [
                "",
                "detailschange",
                "p2a: Terapagos",
                "Terapagos-Stellar, L77, M, tera:Stellar",
            ],
        )
        self.assertEqual("terapagosstellar", self.battle.opponent.active.name)
        self.assertFalse(self.battle.opponent.active.reverts_forme_on_switch_out)
        stellar = self.battle.opponent.active

        switch_or_drag(
            self.battle, ["", "switch", "p2a: Caterpie", "Caterpie, L100", "100/100"]
        )

        self.assertEqual("terapagosstellar", stellar.name)

    def test_terapagos_stellar_detailschange_recalcs_stats(self):
        # Terapagos-Stellar base atk 105; L77 with the randbats 85/31 spread:
        # floor(floor(2*105+31+floor(85/4))*77/100)+5 = floor(262*0.77)+5
        # = 201+5 = 206 (synth02086: attack stat used by Rapid Spin)
        self.battle.opponent.active = Pokemon("terapagosterastal", 77)
        form_change(
            self.battle,
            [
                "",
                "detailschange",
                "p2a: Terapagos",
                "Terapagos-Stellar, L77, M, tera:Stellar",
            ],
        )
        self.assertEqual(
            206, self.battle.opponent.active.stats[constants.ATTACK]
        )


# ---------------------------------------------------------------------------
# activeMoveActions tracking (Fake Out / First Impression gating)
# ---------------------------------------------------------------------------
# PS pokemon.activeMoveActions (sim/pokemon.ts:245-255): incremented in
# runMove BEFORE the move executes (sim/battle-actions.ts:217) - aborted
# attempts (|cant|) count - and reset to 0 on switch-in (:138).
class TestActiveMoveActions(unittest.TestCase):
    def setUp(self):
        self.battle = _fresh_battle()

    def test_own_move_line_increments(self):
        move(self.battle, ["", "move", "p2a: Caterpie", "Tackle"])
        self.assertEqual(1, self.battle.opponent.active.active_move_actions)

    def test_lockedmove_continuation_increments(self):
        # an Outrage continuation is still a runMove action
        move(
            self.battle,
            ["", "move", "p2a: Caterpie", "Outrage", "[from]lockedmove"],
        )
        self.assertEqual(1, self.battle.opponent.active.active_move_actions)

    def test_called_move_line_does_not_increment(self):
        # a Sleep Talk-called move runs through useMove, not runMove: only the
        # calling line counts
        move(self.battle, ["", "move", "p2a: Caterpie", "Sleep Talk"])
        move(
            self.battle,
            ["", "move", "p2a: Caterpie", "Tackle", "[from]move: Sleep Talk"],
        )
        self.assertEqual(1, self.battle.opponent.active.active_move_actions)

    def test_cant_line_increments(self):
        # PS increments on runMove entry, before the full-para/sleep/flinch
        # abort: a para'd first turn still burns the Fake Out window
        cant(self.battle, ["", "cant", "p2a: Caterpie", "par"])
        self.assertEqual(1, self.battle.opponent.active.active_move_actions)

    def test_dancer_copied_move_increments_but_does_not_mark_moved_this_turn(self):
        # a Dancer copy re-enters runMove with externalMove=true
        # (sim/battle-actions.ts:343), and activeMoveActions increments
        # unconditionally at runMove entry (:217) - so the copied dance
        # COUNTS, unlike useMove-internal called moves. externalMove skips
        # moveUsed()/moveThisTurn (:279-292), so moved_this_turn stays unset,
        # and the copied move is not added to the dancer's own moveset.
        pkmn = self.battle.opponent.active
        pkmn.moved_this_turn = False
        move(
            self.battle,
            [
                "",
                "move",
                "p2a: Caterpie",
                "Quiver Dance",
                "p2a: Caterpie",
                "[from] ability: Dancer",
            ],
        )
        self.assertEqual(1, pkmn.active_move_actions)
        self.assertFalse(pkmn.moved_this_turn)
        self.assertEqual("dancer", pkmn.ability)
        self.assertIsNone(pkmn.get_move("quiverdance"))

    def test_sleep_talk_called_move_still_does_not_increment(self):
        # regression guard for the Dancer exception: other '[from]' called
        # moves stay non-incrementing
        move(
            self.battle,
            ["", "move", "p2a: Caterpie", "Tackle", "[from]move: Sleep Talk"],
        )
        self.assertEqual(0, self.battle.opponent.active.active_move_actions)

    def test_switch_in_resets_counter(self):
        self.battle.opponent.active.active_move_actions = 3
        incoming = Pokemon("pidgey", 100)
        incoming.active_move_actions = 2  # stale from an earlier stint
        self.battle.opponent.reserve = [incoming]
        switch_or_drag(
            self.battle, ["", "switch", "p2a: Pidgey", "Pidgey, L100", "100/100"]
        )
        self.assertEqual(0, self.battle.opponent.active.active_move_actions)

    def test_forwarding_gate_matches_wheel_capability(self):
        self.assertEqual(
            hasattr(PokeEnginePokemon, "active_move_actions"),
            POKE_ENGINE_SUPPORTS_ACTIVE_MOVE_ACTIONS,
        )
        # conversion must succeed on the current wheel either way
        pkmn = Pokemon("weedle", 100)
        pkmn.active_move_actions = 3
        pokemon_to_poke_engine_pkmn(pkmn)

    def test_forwarding_when_wheel_supports_field(self):
        # binding field ships with wheel 0.0.49: when supported, the tracked
        # count is forwarded (getattr default 0 for older pickles)
        class _CapturePkmn:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        pkmn = Pokemon("weedle", 100)
        pkmn.active_move_actions = 3
        with mock.patch.object(
            poke_engine_helpers, "POKE_ENGINE_SUPPORTS_ACTIVE_MOVE_ACTIONS", True
        ), mock.patch.object(poke_engine_helpers, "PokeEnginePokemon", _CapturePkmn):
            out = poke_engine_helpers.pokemon_to_poke_engine_pkmn(pkmn)
        self.assertEqual(3, out.kwargs["active_move_actions"])

    def test_no_forwarding_when_wheel_lacks_field(self):
        class _CapturePkmn:
            def __init__(self, **kwargs):
                self.kwargs = kwargs

        pkmn = Pokemon("weedle", 100)
        pkmn.active_move_actions = 3
        with mock.patch.object(
            poke_engine_helpers, "POKE_ENGINE_SUPPORTS_ACTIVE_MOVE_ACTIONS", False
        ), mock.patch.object(poke_engine_helpers, "PokeEnginePokemon", _CapturePkmn):
            out = poke_engine_helpers.pokemon_to_poke_engine_pkmn(pkmn)
        self.assertNotIn("active_move_actions", out.kwargs)


# ---------------------------------------------------------------------------
# Instant charge release must not leave a stale charge volatile
# ---------------------------------------------------------------------------
# PS protocol for a Power Herb instant release (synth00028 T16):
#   |move|p1a: Iron Jugulis|Meteor Beam||[still]
#   |-prepare|p1a: Iron Jugulis|Meteor Beam     <- prepare() adds the volatile
#   |-boost|p1a: Iron Jugulis|spa|1
#   |-enditem|p1a: Iron Jugulis|Power Herb      <- powerherb onChargeMove useItem
#   |-anim|p1a: Iron Jugulis|Meteor Beam|p2a: Iron Leaves
#   |-damage|p2a: Iron Leaves|60/100
# There is NO second |move| line, so the charge-flag strip in move() never
# fires. On the PS side the twoturnmove volatile is never even added in this
# branch: meteorbeam's onTryMove only calls addVolatile when the ChargeMove
# event is not consumed by the herb (data/moves.ts:11751-11762), and powerherb
# onChargeMove emits the -anim (data/items.ts:4771-4783). The -anim handler
# strips the stale volatile. The same protocol shape - with no -enditem line
# at all - covers Solar Beam / Solar Blade in sun (data/moves.ts:17230-17272)
# and Electro Shot in rain (data/moves.ts:4640-4655).
class TestInstantChargeReleaseVolatile(unittest.TestCase):
    def setUp(self):
        self.battle = _fresh_battle()

    def test_power_herb_meteor_beam_release_strips_charge_volatile(self):
        pkmn = self.battle.user.active
        move(
            self.battle,
            ["", "move", "p1a: Weedle", "Meteor Beam", "", "[still]"],
        )
        prepare(self.battle, ["", "-prepare", "p1a: Weedle", "Meteor Beam"])
        self.assertIn("meteorbeam", pkmn.volatile_statuses)
        remove_item(self.battle, ["", "-enditem", "p1a: Weedle", "Power Herb"])
        anim(
            self.battle,
            ["", "-anim", "p1a: Weedle", "Meteor Beam", "p2a: Caterpie"],
        )
        self.assertNotIn("meteorbeam", pkmn.volatile_statuses)
        self.assertIsNone(pkmn.item)

    def test_solar_beam_in_sun_release_strips_charge_volatile(self):
        # sun branch: -prepare then -anim, no -enditem line at all
        pkmn = self.battle.user.active
        move(
            self.battle,
            ["", "move", "p1a: Weedle", "Solar Beam", "", "[still]"],
        )
        prepare(self.battle, ["", "-prepare", "p1a: Weedle", "Solar Beam"])
        self.assertIn("solarbeam", pkmn.volatile_statuses)
        anim(
            self.battle,
            ["", "-anim", "p1a: Weedle", "Solar Beam", "p2a: Caterpie"],
        )
        self.assertNotIn("solarbeam", pkmn.volatile_statuses)

    def test_normal_two_turn_charge_keeps_volatile_until_release_move_line(self):
        # regression guard: a REAL charge turn (no -anim) must keep the
        # volatile, and the release turn's |move| line strips it
        pkmn = self.battle.user.active
        move(
            self.battle,
            ["", "move", "p1a: Weedle", "Meteor Beam", "", "[still]"],
        )
        prepare(self.battle, ["", "-prepare", "p1a: Weedle", "Meteor Beam"])
        self.assertIn("meteorbeam", pkmn.volatile_statuses)
        # release turn
        move(
            self.battle,
            ["", "move", "p1a: Weedle", "Meteor Beam", "p2a: Caterpie", "[from]lockedmove"],
        )
        self.assertNotIn("meteorbeam", pkmn.volatile_statuses)


# ---------------------------------------------------------------------------
# BUG 1: locked-move PP over-decrement
# ---------------------------------------------------------------------------
# PS deducts PP exactly once per lock, on the turn the lock is INITIATED.
# sim/battle-actions.ts:279-291
#     if (!externalMove) {
#         const lockedMove = pokemon.getLockedMove();
#         if (!lockedMove) { if (!pokemon.deductPP(baseMove, null, target)) {...} }
#         else { sourceEffect = this.dex.conditions.get('lockedmove'); }
#     }
# and the continuation's `[from] lockedmove` tag comes from that same
# sourceEffect (:451). Pressure's extra PP lives behind the same gate:
# :472-484 `if (!sourceEffect || callerMoveForPressure)` where
# `callerMoveForPressure` is null for the pp-less lockedmove CONDITION.
class TestLockedMovePpDeduction(unittest.TestCase):
    def setUp(self):
        self.battle = _fresh_battle()

    def test_three_turn_outrage_costs_exactly_one_pp(self):
        outrage = self.battle.opponent.active.add_move("outrage")
        starting_pp = outrage.current_pp
        move(self.battle, ["", "move", "p2a: Caterpie", "Outrage", "p1a: Weedle"])
        move(
            self.battle,
            ["", "move", "p2a: Caterpie", "Outrage", "p1a: Weedle", "[from]lockedmove"],
        )
        move(
            self.battle,
            [
                "",
                "move",
                "p2a: Caterpie",
                "Outrage",
                "p1a: Weedle",
                "[from] lockedmove",
            ],
        )
        self.assertEqual(starting_pp - 1, outrage.current_pp)

    def test_three_turn_outrage_into_pressure_costs_exactly_two_pp(self):
        # Pressure adds its 1 only on the initiating turn (sourceEffect is
        # undefined there); continuations skip the whole Pressure block
        self.battle.user.active.ability = "pressure"
        outrage = self.battle.opponent.active.add_move("outrage")
        starting_pp = outrage.current_pp
        move(self.battle, ["", "move", "p2a: Caterpie", "Outrage", "p1a: Weedle"])
        move(
            self.battle,
            ["", "move", "p2a: Caterpie", "Outrage", "p1a: Weedle", "[from]lockedmove"],
        )
        move(
            self.battle,
            ["", "move", "p2a: Caterpie", "Outrage", "p1a: Weedle", "[from]lockedmove"],
        )
        self.assertEqual(starting_pp - 2, outrage.current_pp)

    def test_two_turn_charge_move_charges_pp_on_the_charge_turn_only(self):
        # the RELEASE turn of a two-turn move is also a locked continuation
        # (data/moves.ts twoturnmove condition onLockMove), so the charge turn
        # is the one that pays
        phantom_force = self.battle.opponent.active.add_move("phantomforce")
        starting_pp = phantom_force.current_pp
        move(self.battle, ["", "move", "p2a: Caterpie", "Phantom Force", "", "[still]"])
        self.assertEqual(starting_pp - 1, phantom_force.current_pp)
        move(
            self.battle,
            [
                "",
                "move",
                "p2a: Caterpie",
                "Phantom Force",
                "p1a: Weedle",
                "[from]lockedmove",
            ],
        )
        self.assertEqual(starting_pp - 1, phantom_force.current_pp)

    def test_sleep_talk_called_move_does_not_spend_pp(self):
        # Sleep Talk reaches its pick through `this.actions.useMove(...)`
        # (data/moves.ts sleeptalk onHit), which never enters runMove - so
        # deductPP (sim/battle-actions.ts:282) never runs for the CALLED move.
        # Sleep Talk itself was used normally and pays its own 1.
        sleep_talk = self.battle.opponent.active.add_move("sleeptalk")
        tackle = self.battle.opponent.active.add_move("tackle")
        sleep_talk_pp = sleep_talk.current_pp
        tackle_pp = tackle.current_pp
        move(self.battle, ["", "move", "p2a: Caterpie", "Sleep Talk", "p2a: Caterpie"])
        move(
            self.battle,
            [
                "",
                "move",
                "p2a: Caterpie",
                "Tackle",
                "p1a: Weedle",
                "[from]move: Sleep Talk",
            ],
        )
        self.assertEqual(sleep_talk_pp - 1, sleep_talk.current_pp)
        self.assertEqual(tackle_pp, tackle.current_pp)

    def test_encore_forced_move_still_spends_pp(self):
        # Encore is a SOFT lock: PS restricts the choice via moveSlot.disabled
        # (data/moves.ts encore onDisableMove) and getLockedMove() stays null,
        # so runMove takes the deductPP branch normally and the |move| line
        # carries no `[from]` tag at all
        self.battle.opponent.active.volatile_statuses.append("encore")
        tackle = self.battle.opponent.active.add_move("tackle")
        starting_pp = tackle.current_pp
        move(self.battle, ["", "move", "p2a: Caterpie", "Tackle", "p1a: Weedle"])
        self.assertEqual(starting_pp - 1, tackle.current_pp)

    def test_lockedmove_continuation_of_an_unseen_move_is_still_recorded(self):
        # the move must still be added to the moveset, just at full PP
        move(
            self.battle,
            [
                "",
                "move",
                "p2a: Caterpie",
                "Petal Dance",
                "p1a: Weedle",
                "[from]lockedmove",
            ],
        )
        petal_dance = self.battle.opponent.active.get_move("petaldance")
        self.assertIsNotNone(petal_dance)
        self.assertEqual(petal_dance.max_pp, petal_dance.current_pp)


# ---------------------------------------------------------------------------
# BUG 2: rejected-switch trap inference
# ---------------------------------------------------------------------------
# sim/side.ts:983-995 rejects a switch with
#   emitChoiceError("Can't switch: The active Pokémon is trapped", {pokemon, update})
# and sim/side.ts:525-534 emits it as `|error|[Unavailable choice] <msg>`
# (or `[Invalid choice]` when the request needed no patch). PS only sets
# `pokemon.trapped` once the trap is confirmed, so the rejection proves the
# foe's active holds a trapping ability - unless something on our own side
# already explains it.
_TRAPPED_ERROR = "[Unavailable choice] Can't switch: The active Pokémon is trapped"

# the corrected request PS re-emits right after the rejection (sim/side.ts:529
# `if (updated) this.emitRequest(this.activeRequest!, true)`)
_TRAPPED_REREQUEST = {
    "active": [
        {
            "moves": [
                {
                    "move": "Poison Sting",
                    "id": "poisonsting",
                    "pp": 56,
                    "maxpp": 56,
                    "target": "normal",
                    "disabled": False,
                }
            ],
            "trapped": True,
        }
    ],
    "side": {
        "name": "BotName",
        "id": "p1",
        "pokemon": [
            {
                "ident": "p1: Weedle",
                "details": "Weedle, L100, M",
                "condition": "100/100",
                "active": True,
                "stats": {"atk": 1, "def": 1, "spa": 1, "spd": 1, "spe": 1},
                "moves": ["poisonsting"],
                "baseAbility": "shielddust",
                "item": "",
                "ability": "shielddust",
            }
        ],
    },
    "rqid": 7,
}


def _trap_battle(opponent_name, user_name="weedle", generation="gen9"):
    battle = _fresh_battle(generation=generation)
    battle.user.active = Pokemon(user_name, 100)
    battle.opponent.active = Pokemon(opponent_name, 100)
    battle.opponent.active.ability = None
    return battle


class TestRejectedSwitchTrapInference(unittest.TestCase):
    def test_single_candidate_trapping_ability_is_pinned(self):
        # dugtrio: {Sand Veil, Arena Trap, Sand Force} -> only arenatrap traps
        battle = _trap_battle("dugtrio")
        error(battle, ["", "error", _TRAPPED_ERROR])
        self.assertEqual("arenatrap", battle.opponent.active.ability)
        self.assertIn("sandveil", battle.opponent.active.impossible_abilities)
        self.assertIn("sandforce", battle.opponent.active.impossible_abilities)
        self.assertNotIn("arenatrap", battle.opponent.active.impossible_abilities)

    def test_shadow_tag_is_pinned(self):
        # gothitelle: {Frisk, Competitive, Shadow Tag}
        battle = _trap_battle("gothitelle")
        error(battle, ["", "error", _TRAPPED_ERROR])
        self.assertEqual("shadowtag", battle.opponent.active.ability)

    def test_invalid_choice_tag_is_handled_too(self):
        battle = _trap_battle("dugtrio")
        error(
            battle,
            [
                "",
                "error",
                "[Invalid choice] Can't switch: The active Pokémon is trapped",
            ],
        )
        self.assertEqual("arenatrap", battle.opponent.active.ability)

    def test_magnet_pull_requires_us_to_be_steel(self):
        # magnezone: {Magnet Pull, Sturdy, Analytic}. Weedle is Bug/Poison, so
        # data/abilities.ts:2508 `pokemon.hasType('Steel')` could not have
        # fired - the trap came from somewhere else, infer nothing.
        battle = _trap_battle("magnezone")
        error(battle, ["", "error", _TRAPPED_ERROR])
        self.assertIsNone(battle.opponent.active.ability)
        self.assertEqual(set(), battle.opponent.active.impossible_abilities)

    def test_magnet_pull_is_pinned_against_a_steel_type(self):
        battle = _trap_battle("magnezone", user_name="skarmory")
        error(battle, ["", "error", _TRAPPED_ERROR])
        self.assertEqual("magnetpull", battle.opponent.active.ability)

    def test_arena_trap_requires_us_to_be_grounded(self):
        # skarmory is Steel/Flying: ungrounded, so Arena Trap
        # (data/abilities.ts:199 `pokemon.isGrounded()`) is impossible
        battle = _trap_battle("dugtrio", user_name="skarmory")
        error(battle, ["", "error", _TRAPPED_ERROR])
        self.assertIsNone(battle.opponent.active.ability)

    def test_gravity_regrounds_a_flying_type_for_arena_trap(self):
        battle = _trap_battle("dugtrio", user_name="skarmory")
        battle.gravity = True
        error(battle, ["", "error", _TRAPPED_ERROR])
        self.assertEqual("arenatrap", battle.opponent.active.ability)

    def test_ghost_type_bot_infers_nothing_in_gen9(self):
        # data/typechart.ts:202-204 ghost damageTaken.trapped = 3 -> tryTrap
        # (sim/pokemon.ts:1614) refuses, so no ability could have trapped us
        battle = _trap_battle("dugtrio", user_name="gengar")
        error(battle, ["", "error", _TRAPPED_ERROR])
        self.assertIsNone(battle.opponent.active.ability)
        self.assertEqual(set(), battle.opponent.active.impossible_abilities)

    def test_ghost_type_bot_still_infers_in_gen4(self):
        # data/mods/gen5/typechart.ts:24-44 re-lists ghost's damageTaken with
        # no `trapped` key, so pre-gen6 ghosts ARE ability-trappable
        battle = _trap_battle("dugtrio", user_name="gengar", generation="gen4")
        error(battle, ["", "error", _TRAPPED_ERROR])
        self.assertEqual("arenatrap", battle.opponent.active.ability)

    def test_self_trapping_volatile_blocks_the_inference(self):
        for volatile in ("partiallytrapped", "trapped", "ingrain", "lockedmove"):
            with self.subTest(volatile=volatile):
                battle = _trap_battle("dugtrio")
                battle.user.active.volatile_statuses.append(volatile)
                error(battle, ["", "error", _TRAPPED_ERROR])
                self.assertIsNone(battle.opponent.active.ability)

    def test_shed_shell_blocks_the_inference(self):
        # data/items.ts:5635-5638 clears `trapped` after every ability handler
        battle = _trap_battle("dugtrio")
        battle.user.active.item = "shedshell"
        error(battle, ["", "error", _TRAPPED_ERROR])
        self.assertIsNone(battle.opponent.active.ability)

    def test_neutralizing_gas_blocks_the_inference(self):
        battle = _trap_battle("dugtrio")
        battle.user.active.ability = "neutralizinggas"
        error(battle, ["", "error", _TRAPPED_ERROR])
        self.assertIsNone(battle.opponent.active.ability)

    def test_species_with_no_trapping_ability_infers_nothing(self):
        battle = _trap_battle("caterpie")
        error(battle, ["", "error", _TRAPPED_ERROR])
        self.assertIsNone(battle.opponent.active.ability)
        self.assertEqual(set(), battle.opponent.active.impossible_abilities)

    def test_already_known_ability_is_not_overwritten(self):
        battle = _trap_battle("dugtrio")
        battle.opponent.active.ability = "sandveil"
        error(battle, ["", "error", _TRAPPED_ERROR])
        self.assertEqual("sandveil", battle.opponent.active.ability)

    def test_error_line_reaches_the_handler_through_update_battle(self):
        # PS delivers the rejection and the corrected request as two separate
        # sideupdates: the |error| line is buffered by update_battle and only
        # dispatched when the follow-up |request| triggers
        # process_battle_updates. This pins the dispatch-table registration.
        battle = _trap_battle("dugtrio")
        battle.user.last_selected_move = LastUsedMove("weedle", "switch caterpie", 3)
        update_battle(battle, ">battle-x\n|error|" + _TRAPPED_ERROR)
        self.assertEqual(["|error|" + _TRAPPED_ERROR], battle.msg_list)

        update_battle(battle, ">battle-x\n|request|" + json.dumps(_TRAPPED_REREQUEST))
        self.assertEqual("arenatrap", battle.opponent.active.ability)

    def test_unrelated_error_is_a_no_op(self):
        battle = _trap_battle("dugtrio")
        error(
            battle,
            [
                "",
                "error",
                "[Invalid choice] Can't move: Your Dugtrio doesn't have a move 3",
            ],
        )
        self.assertIsNone(battle.opponent.active.ability)
        self.assertEqual(set(), battle.opponent.active.impossible_abilities)


# ---------------------------------------------------------------------------
# BUG 4: ability-flavored |cant| attribution
# ---------------------------------------------------------------------------
# data/abilities.ts:225/805/864/3716 emit
#   this.add('cant', <ABILITY HOLDER>, 'ability: <Name>', move, `[of] <blocked>`)
# The block fires at the TryMove event, which useMoveInner runs AFTER emitting
# the |move| line (sim/battle-actions.ts:457 vs :485-490), so the blocked mon
# has already been charged its runMove costs by move(). The |cant| line's only
# new information is the holder's ability.
class TestAbilityBlockedCantAttribution(unittest.TestCase):
    def setUp(self):
        self.battle = _fresh_battle()
        self.battle.user.active = Pokemon("tsareena", 100)
        self.battle.user.active.ability = None
        self.battle.opponent.active = Pokemon("mightyena", 100)

    def test_holder_on_our_side_is_credited_and_not_charged(self):
        # |move|p2a: Mightyena|Sucker Punch||[still]
        # |cant|p1a: Tsareena|ability: Queenly Majesty|Sucker Punch|[of] p2a: Mightyena
        move(self.battle, ["", "move", "p2a: Mightyena", "Sucker Punch", "", "[still]"])
        cant(
            self.battle,
            [
                "",
                "cant",
                "p1a: Tsareena",
                "ability: Queenly Majesty",
                "Sucker Punch",
                "[of] p2a: Mightyena",
            ],
        )
        holder = self.battle.user.active
        blocked = self.battle.opponent.active
        self.assertEqual("queenlymajesty", holder.ability)
        # the holder did not act
        self.assertEqual(0, holder.active_move_actions)
        self.assertFalse(holder.moved_this_turn)
        # the blocked mon was charged exactly once, by its own |move| line
        self.assertEqual(1, blocked.active_move_actions)
        self.assertTrue(blocked.moved_this_turn)

    def test_holder_on_the_opponents_side_is_credited_and_not_charged(self):
        self.battle.user.active = Pokemon("mightyena", 100)
        self.battle.opponent.active = Pokemon("farigiraf", 100)
        self.battle.opponent.active.ability = None
        move(self.battle, ["", "move", "p1a: Mightyena", "Sucker Punch", "", "[still]"])
        cant(
            self.battle,
            [
                "",
                "cant",
                "p2a: Farigiraf",
                "ability: Armor Tail",
                "Sucker Punch",
                "[of] p1a: Mightyena",
            ],
        )
        self.assertEqual("armortail", self.battle.opponent.active.ability)
        self.assertEqual(0, self.battle.opponent.active.active_move_actions)
        self.assertFalse(self.battle.opponent.active.moved_this_turn)
        self.assertEqual(1, self.battle.user.active.active_move_actions)

    def test_holder_does_not_lose_its_glaive_rush_volatile(self):
        self.battle.user.active.volatile_statuses.append("glaiverush")
        cant(
            self.battle,
            [
                "",
                "cant",
                "p1a: Tsareena",
                "ability: Queenly Majesty",
                "Sucker Punch",
                "[of] p2a: Mightyena",
            ],
        )
        self.assertIn("glaiverush", self.battle.user.active.volatile_statuses)

    def test_damp_self_block_names_the_same_pokemon_twice(self):
        # Damp uses onAnyTryMove, so the holder can be the move's own user
        # (data/abilities.ts:796-806); `[of]` still names the blocked mon
        move(self.battle, ["", "move", "p1a: Tsareena", "Explosion", "", "[still]"])
        cant(
            self.battle,
            [
                "",
                "cant",
                "p1a: Tsareena",
                "ability: Damp",
                "Explosion",
                "[of] p1a: Tsareena",
            ],
        )
        self.assertEqual("damp", self.battle.user.active.ability)
        self.assertEqual(1, self.battle.user.active.active_move_actions)

    def test_truant_still_charges_the_named_pokemon(self):
        # regression guard: Truant (data/abilities.ts:5183) names the pokemon
        # that cannot move and carries no `[of]`
        self.battle.opponent.active.volatile_statuses.append("truant")
        cant(self.battle, ["", "cant", "p2a: Mightyena", "ability: Truant"])
        self.assertEqual(1, self.battle.opponent.active.active_move_actions)
        self.assertTrue(self.battle.opponent.active.moved_this_turn)
        self.assertEqual(0, self.battle.user.active.active_move_actions)

    def test_plain_cant_still_charges_the_named_pokemon(self):
        cant(self.battle, ["", "cant", "p2a: Mightyena", "par"])
        self.assertEqual(1, self.battle.opponent.active.active_move_actions)
        self.assertEqual(0, self.battle.user.active.active_move_actions)


# ---------------------------------------------------------------------------
# BUG 3: forced/locked request must not collapse the moveset
# ---------------------------------------------------------------------------
# sim/pokemon.ts:970-990 `getMoves(lockedMove)` short-circuits to a single
# `{move, id}` entry with NO pp/maxpp/disabled/target when getLockedMove() is
# truthy (:1089-1095). Rebuilding the moveset from it dropped three moves and
# reset PP to the `1` default for the whole search horizon.
def _request(moves, active_moves, trapped=False):
    request = {
        "active": [{"moves": active_moves}],
        "side": {
            "name": "BotName",
            "id": "p1",
            "pokemon": [
                {
                    "ident": "p1: Dragonite",
                    "details": "Dragonite, L78, M",
                    "condition": "300/300",
                    "active": True,
                    "stats": {
                        "atk": 250,
                        "def": 200,
                        "spa": 180,
                        "spd": 200,
                        "spe": 190,
                    },
                    "moves": moves,
                    "baseAbility": "multiscale",
                    "item": "heavydutyboots",
                    "ability": "multiscale",
                }
            ],
        },
    }
    if trapped:
        request["active"][0]["trapped"] = True
    return request


_DRAGONITE_MOVES = ["outrage", "earthquake", "roost", "dragondance"]


def _normal_active_moves():
    return [
        {
            "move": "Outrage",
            "id": "outrage",
            "pp": 16,
            "maxpp": 16,
            "target": "randomNormal",
            "disabled": False,
        },
        {
            "move": "Earthquake",
            "id": "earthquake",
            "pp": 16,
            "maxpp": 16,
            "target": "allAdjacent",
            "disabled": False,
        },
        {
            "move": "Roost",
            "id": "roost",
            "pp": 16,
            "maxpp": 16,
            "target": "self",
            "disabled": False,
        },
        {
            "move": "Dragon Dance",
            "id": "dragondance",
            "pp": 32,
            "maxpp": 32,
            "target": "self",
            "disabled": False,
        },
    ]


class TestForcedRequestKeepsMoveset(unittest.TestCase):
    def setUp(self):
        self.battler = Battler()
        self.battler.active = Pokemon("dragonite", 78)

    def _apply(self, active_moves, trapped=False):
        self.battler.update_from_request_json(
            _request(_DRAGONITE_MOVES, active_moves, trapped=trapped)
        )

    def test_locked_request_keeps_full_moveset_and_pp(self):
        self._apply(_normal_active_moves())
        # spend a couple of PP so a silent reset would be visible
        self.battler.active.get_move("outrage").current_pp = 13
        self.battler.active.get_move("roost").current_pp = 9

        # locked turn: single move, no pp key, plus the hard-trap flag PS
        # always attaches to a locked request (sim/pokemon.ts:1091-1093)
        self._apply([{"move": "Outrage", "id": "outrage"}], trapped=True)

        names = [m.name for m in self.battler.active.moves]
        self.assertEqual(_DRAGONITE_MOVES, names)
        self.assertEqual(13, self.battler.active.get_move("outrage").current_pp)
        self.assertEqual(9, self.battler.active.get_move("roost").current_pp)
        self.assertEqual(16, self.battler.active.get_move("earthquake").current_pp)

    def test_locked_request_leaves_only_the_locked_move_selectable(self):
        self._apply(_normal_active_moves())
        self._apply([{"move": "Outrage", "id": "outrage"}], trapped=True)

        for m in self.battler.active.moves:
            self.assertEqual(m.name != "outrage", m.disabled, m.name)

    def test_moveset_survives_lock_then_normal_turn(self):
        self._apply(_normal_active_moves())
        self.battler.active.get_move("outrage").current_pp = 13
        self._apply([{"move": "Outrage", "id": "outrage"}], trapped=True)

        # lock broke: PS sends the normal 4-move shape again
        after_lock = _normal_active_moves()
        after_lock[0]["pp"] = 13
        self._apply(after_lock)

        names = [m.name for m in self.battler.active.moves]
        self.assertEqual(_DRAGONITE_MOVES, names)
        self.assertEqual(13, self.battler.active.get_move("outrage").current_pp)
        self.assertFalse(any(m.disabled for m in self.battler.active.moves))

    def test_two_turn_charge_release_request_keeps_moveset(self):
        # the release turn of Fly/Phantom Force/... is the same locked shape
        self._apply(_normal_active_moves())
        self._apply([{"move": "Roost", "id": "roost"}], trapped=True)
        self.assertEqual(4, len(self.battler.active.moves))
        self.assertFalse(self.battler.active.get_move("roost").disabled)

    def test_recharge_request_still_collapses_to_the_pseudo_move(self):
        # `recharge` is a pseudo-move PS invents for the slot
        # (sim/pokemon.ts:973-977); it is never in moveSlots, so there is
        # nothing to keep and the collapse is what drives
        # `/choose move recharge`. Documented carve-out, see fp/battle.py.
        self._apply(_normal_active_moves())
        self._apply([{"move": "Recharge", "id": "recharge"}], trapped=True)
        self.assertEqual(["recharge"], [m.name for m in self.battler.active.moves])

    def test_struggle_request_still_collapses(self):
        self._apply(_normal_active_moves())
        self._apply(
            [
                {
                    "move": "Struggle",
                    "id": "struggle",
                    "target": "randomNormal",
                    "disabled": False,
                }
            ]
        )
        self.assertEqual(["struggle"], [m.name for m in self.battler.active.moves])

    def test_single_move_request_with_pp_is_not_treated_as_locked(self):
        # a genuine one-move pokemon still carries pp/maxpp - only the pp-less
        # shape means "locked"
        self._apply(_normal_active_moves())
        self._apply(
            [
                {
                    "move": "Outrage",
                    "id": "outrage",
                    "pp": 5,
                    "maxpp": 16,
                    "target": "randomNormal",
                    "disabled": False,
                }
            ]
        )
        self.assertEqual(["outrage"], [m.name for m in self.battler.active.moves])
        self.assertEqual(5, self.battler.active.get_move("outrage").current_pp)


# ---------------------------------------------------------------------------
# The request's party list is the roster; an over-length party is REFUSED
# ---------------------------------------------------------------------------
class TestRequestAuthoritativeParty(unittest.TestCase):
    """`sim/side.ts:355-366 getRequestData` pushes exactly one entry per member
    of `this.pokemon`, so the request's party list IS the server's roster: six
    rows, one physical pokemon each.

    Reproduced defect: Terapagos enters as `Terapagos` and immediately
    |detailschange|s to `Terapagos-Terastal`.  Replayed against a state already
    built from the (post-detailschange) request, `switch_or_drag`'s
    `find_pokemon_in_reserves("terapagos")` misses -- the Terastal forme is the
    ACTIVE, not a reserve -- so it builds a second object and benches the first,
    leaving SEVEN party rows.  `PySide::new` then silently dropped the tail,
    which was the side's last living reserve (synth29732 Arceus-Dark 288/288,
    synth41888 Hydrapple 312/312), and the engine suppressed a correct
    end-of-turn residual because it read the side as wiped.
    """

    def _request(self):
        def entry(ident, details, condition, active, moves):
            return {
                "ident": "p1: " + ident,
                "details": details,
                "condition": condition,
                "active": active,
                "stats": {"atk": 100, "def": 100, "spa": 100, "spd": 100, "spe": 100},
                "moves": moves,
                "baseAbility": "terashell" if active else "levitate",
                "item": "chestoberry",
                "ability": "terashell" if active else "levitate",
            }

        return {
            "side": {
                "name": "BigBluePikachu",
                "id": "p1",
                "pokemon": [
                    entry(
                        "Terapagos",
                        "Terapagos-Terastal, L77, F",
                        "273/273",
                        True,
                        ["rest"],
                    ),
                    entry("Arceus", "Arceus-Dark, L74", "288/288", False, ["judgment"]),
                ],
            }
        }

    def _battler_with_duplicate(self):
        battler = Battler()
        battler.name = "p1"
        active = Pokemon("terapagosterastal", 77)
        active.max_hp = 273
        active.hp = 273
        arceus = Pokemon("arceusdark", 74)
        arceus.max_hp = 288
        arceus.hp = 288
        stale = Pokemon("terapagosterastal", 77)
        stale.max_hp = 273
        stale.hp = 0
        battler.active = active
        battler.reserve = [arceus, stale]
        return battler

    def test_unclaimed_forme_duplicate_is_dropped(self):
        battler = self._battler_with_duplicate()
        battler.update_from_request_json(self._request())
        self.assertEqual(["arceusdark"], [p.name for p in battler.reserve])

    def test_control_flag_keeps_the_duplicate(self):
        # PROVE THE FIX CAN FAIL
        battler = self._battler_with_duplicate()
        with mock.patch.dict(
            os.environ, {"FP_CONTROL_KEEP_FORME_DUPLICATES": "1"}, clear=False
        ):
            battler.update_from_request_json(self._request())
        self.assertEqual(
            ["arceusdark", "terapagosterastal"], [p.name for p in battler.reserve]
        )

    def test_row_the_request_does_not_cover_is_left_alone(self):
        # conservative on purpose: only PROVABLE duplicates are dropped, so a
        # merely-unrecognised mon still reaches the (now refusing) binding
        battler = self._battler_with_duplicate()
        battler.reserve[1] = Pokemon("gyarados", 80)
        battler.update_from_request_json(self._request())
        self.assertEqual(["arceusdark", "gyarados"], [p.name for p in battler.reserve])

    def test_conversion_refuses_an_over_length_party(self):
        # a silent discard is exactly the class of bug that hid this one
        battler = Battler()
        battler.name = "p1"
        battler.active = Pokemon("pikachu", 100)
        battler.reserve = [Pokemon("caterpie", 100) for _ in range(6)]
        with self.assertRaises(poke_engine_helpers.OverlongPartyError):
            poke_engine_helpers._padded_party(battler)

    def test_six_is_still_accepted_and_short_parties_are_padded(self):
        battler = Battler()
        battler.name = "p1"
        battler.active = Pokemon("pikachu", 100)
        battler.reserve = [Pokemon("caterpie", 100) for _ in range(2)]
        party = poke_engine_helpers._padded_party(battler)
        self.assertEqual(6, len(party))
        # the pad slots are EMPTY (maxhp 0), not fainted pokemon
        self.assertEqual([0, 0, 0], [p.maxhp for p in party[3:]])


# ---------------------------------------------------------------------------
# Wish lifecycle: arms once, never re-arms on a failed Wish, clears on resolve
# ---------------------------------------------------------------------------
class TestWishLifecycle(unittest.TestCase):
    """PS: Wish is a SLOT condition and `Side#addSlotCondition` returns false
    when the slot already holds it and the condition has no `onRestart`
    (sim/side.ts:472-474); Wish has none (data/moves.ts:20909-20945), so a
    second Wish while one is pending FAILS.

    The clearing half must not be gated on an observable heal: `onEnd` adds the
    `|-heal|` line only `if (damage)` and `Battle#heal` returns false at full HP
    (sim/battle.ts:2272), so a wish that resolves onto a full-HP mon leaves no
    protocol line while the slot condition is removed regardless.

    synth41888: Farigiraf wished on T24 and wished again on T25 into a
    `|-fail|`; the re-arm left `wish=(1, 181)` live in T26's pre-state, which
    manufactures a phantom `Heal SideTwo: 181` there.
    """

    def test_wish_arms_when_the_slot_is_empty(self):
        battler = Battler()
        self.assertTrue(battler.arm_wish(181))
        self.assertEqual((2, 181), battler.wish)

    def test_second_wish_while_one_is_pending_fails_and_does_not_rearm(self):
        battler = Battler()
        battler.arm_wish(181)
        battler.wish = (1, 181)  # one upkeep has passed
        self.assertFalse(battler.arm_wish(181))
        self.assertEqual((1, 181), battler.wish)

    def test_wish_can_be_used_again_once_the_slot_cleared(self):
        battler = Battler()
        battler.wish = (0, 181)
        self.assertTrue(battler.arm_wish(200))
        self.assertEqual((2, 200), battler.wish)

    def test_control_flag_restores_the_rearm(self):
        # PROVE THE FIX CAN FAIL
        battler = Battler()
        battler.wish = (1, 181)
        with mock.patch.dict(
            os.environ, {"FP_CONTROL_WISH_REARM_ON_FAIL": "1"}, clear=False
        ):
            self.assertTrue(battler.arm_wish(181))
        self.assertEqual((2, 181), battler.wish)

    def test_upkeep_clears_the_wish_with_no_heal_observed(self):
        # the resolve half: two upkeeps clear the slot whether or not a `-heal`
        # was ever emitted
        battle = _fresh_battle()
        battle.opponent.arm_wish(181)
        upkeep(battle, ["", "upkeep"])
        self.assertEqual((1, 181), battle.opponent.wish)
        upkeep(battle, ["", "upkeep"])
        self.assertEqual((0, 181), battle.opponent.wish)


# ---------------------------------------------------------------------------
# Opponent-side forme-duplicate prevention (freeze round 10)
# ---------------------------------------------------------------------------
class TestOpponentSideFormeIdentity(unittest.TestCase):
    """The forme-duplicate prune (`Battler._drop_unclaimed_forme_duplicates`) is
    REQUEST-DRIVEN, so it structurally covers the bot's side only: the opponent
    never sends a request, and an over-long opponent party reaches the binding
    and raises `OverlongPartyError`.  That refusal is CORRECT and stays in place;
    what is fixed here are the two reasons an opponent party ever grew to seven.

    synth15853: `|switch|p2a: Shaymin|Shaymin-Sky, ...|44/100`, then the mon is
    frozen and PS PERMANENTLY reverts its forme --
    `data/conditions.ts:92-93` -> `target.formeChange('Shaymin', this.effect,
    /*isPermanent*/ true)` -- emitting `|detailschange|` AND, because
    `source.effectType === 'Status'`, a trailing `|-formechange|` naming the same
    species (`sim/pokemon.ts:1449-1476`).  Reading that echo as the NON-permanent
    path made the mon revert to `shayminsky` on switch-out, so its next
    `|switch|p2a: Shaymin|Shaymin, ...|` matched nothing in the reserves and a
    SECOND Shaymin object was built.  24 binding refusals, 20 checked turns lost.
    """

    def _shaymin_battle(self):
        battle = _fresh_battle()
        battle.opponent.active = Pokemon("shayminsky", 73)
        battle.opponent.reserve = [Pokemon("torkoal", 88)]
        return battle

    def _freeze_forme_revert(self, battle):
        # exactly what PS emits, in order
        form_change(battle, ["", "detailschange", "p2a: Shaymin", "Shaymin, L73"])
        form_change(battle, ["", "-formechange", "p2a: Shaymin", "Shaymin", ""])

    def test_permanent_formechange_echo_does_not_arm_the_switch_out_revert(self):
        battle = self._shaymin_battle()
        self._freeze_forme_revert(battle)
        self.assertEqual("shaymin", battle.opponent.active.name)
        self.assertFalse(battle.opponent.active.reverts_forme_on_switch_out)

        switch_or_drag(
            battle, ["", "switch", "p2a: Torkoal", "Torkoal, L88, M", "100/100"]
        )
        benched = [p for p in battle.opponent.reserve if p.name == "shaymin"]
        self.assertEqual(
            1, len(benched), "the mon must stay on its permanent forme off-field"
        )

    def test_control_flag_restores_the_wrong_revert(self):
        # PROVE THE FIX CAN FAIL
        battle = self._shaymin_battle()
        with mock.patch.dict(
            os.environ, {"FP_CONTROL_PERMANENT_FORME_ECHO_REVERTS": "1"}, clear=False
        ):
            self._freeze_forme_revert(battle)
            self.assertTrue(battle.opponent.active.reverts_forme_on_switch_out)
            switch_or_drag(
                battle, ["", "switch", "p2a: Torkoal", "Torkoal, L88, M", "100/100"]
            )
        self.assertEqual(
            ["shayminsky"],
            [p.name for p in battle.opponent.reserve if "shaymin" in p.name],
        )

    def test_forme_lineage_recognises_the_mon_under_a_name_it_has_worn(self):
        # even WITH the wrong revert forced back on, the lineage lookup must not
        # build a second object: this is the layer that covers the class on the
        # opponent side generally, not just this row
        battle = self._shaymin_battle()
        with mock.patch.dict(
            os.environ, {"FP_CONTROL_PERMANENT_FORME_ECHO_REVERTS": "1"}, clear=False
        ):
            self._freeze_forme_revert(battle)
            switch_or_drag(
                battle, ["", "switch", "p2a: Torkoal", "Torkoal, L88, M", "100/100"]
            )
            switch_or_drag(
                battle, ["", "switch", "p2a: Shaymin", "Shaymin, L73", "17/100"]
            )
        party = [battle.opponent.active] + battle.opponent.reserve
        self.assertEqual(
            2,
            len(party),
            "one physical mon must not become two rows: {}".format(
                [p.name for p in party]
            ),
        )

    def test_both_controls_off_reproduces_the_duplicate_row(self):
        # PROVE THE FIX CAN FAIL, the other half.  Each flag gates exactly ONE
        # mechanism (process rule 18), so NEITHER alone reproduces the bug --
        # which is itself why two flags were needed instead of one.
        battle = self._shaymin_battle()
        with mock.patch.dict(
            os.environ,
            {
                "FP_CONTROL_PERMANENT_FORME_ECHO_REVERTS": "1",
                "FP_CONTROL_NO_FORME_LINEAGE_MATCH": "1",
            },
            clear=False,
        ):
            self._freeze_forme_revert(battle)
            switch_or_drag(
                battle, ["", "switch", "p2a: Torkoal", "Torkoal, L88, M", "100/100"]
            )
            switch_or_drag(
                battle, ["", "switch", "p2a: Shaymin", "Shaymin, L73", "17/100"]
            )
        names = sorted(
            p.name for p in [battle.opponent.active] + battle.opponent.reserve
        )
        self.assertEqual(["shaymin", "shayminsky", "torkoal"], names)

    def test_lineage_match_refuses_when_it_is_not_unique(self):
        # REFUSE-DON'T-GUESS: two reserve rows that have worn the same name are
        # genuinely ambiguous, so the lookup returns None and the over-length
        # party reaches the binding's refusal rather than being merged on a hunch
        battler = Battler()
        a, b = Pokemon("terapagos", 77), Pokemon("terapagos", 77)
        a.forme_lineage.add("terapagosterastal")
        b.forme_lineage.add("terapagosterastal")
        a.name = b.name = "weedle"
        battler.reserve = [a, b]
        self.assertIsNone(battler.find_pokemon_in_reserves("terapagosterastal"))

    def test_non_permanent_formechange_still_reverts_on_switch_out(self):
        # CONTROL in the other direction: a plain |-formechange| with no
        # preceding |detailschange| is PS's non-permanent path and MUST still
        # revert (Meloetta-Pirouette -> Meloetta, sim/pokemon.ts:1514-1565)
        battle = _fresh_battle()
        battle.opponent.active = Pokemon("meloetta", 80)
        battle.opponent.reserve = [Pokemon("torkoal", 88)]
        form_change(
            battle, ["", "-formechange", "p2a: Meloetta", "Meloetta-Pirouette", ""]
        )
        self.assertEqual("meloettapirouette", battle.opponent.active.name)
        self.assertTrue(battle.opponent.active.reverts_forme_on_switch_out)
        switch_or_drag(
            battle, ["", "switch", "p2a: Torkoal", "Torkoal, L88, M", "100/100"]
        )
        self.assertEqual(
            ["meloetta"],
            [p.name for p in battle.opponent.reserve if "meloetta" in p.name],
        )


if __name__ == "__main__":
    unittest.main()
