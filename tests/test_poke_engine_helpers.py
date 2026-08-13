import unittest

import constants
from fp.battle import Battle, Battler, LastUsedMove, Pokemon
from fp.search.poke_engine_helpers import (
    annotate_reveal_masks,
    battle_to_poke_engine_state,
    POKE_ENGINE_SUPPORTS_DISABLE_DURATION,
    POKE_ENGINE_SUPPORTS_ILLUSION_BROKEN,
    POKE_ENGINE_SUPPORTS_KNOWN,
    POKE_ENGINE_SUPPORTS_LAST_CONSUMED_ITEM,
    POKE_ENGINE_SUPPORTS_REVEALED,
    POKE_ENGINE_SUPPORTS_REVEAL_MASK,
    POKE_ENGINE_SUPPORTS_REVIVAL_BLESSING,
    POKE_ENGINE_SUPPORTS_TIMES_ATTACKED,
    POKE_ENGINE_SUPPORTS_TIMES_REVIVED,
    POKE_ENGINE_SUPPORTS_TRANSFORMED,
    battler_has_revive_prompt,
    battler_to_poke_engine_side,
    pokemon_to_poke_engine_pkmn,
)

from poke_engine import State as PokeEngineState


class TestBattlerHasRevivePrompt(unittest.TestCase):
    def setUp(self):
        self.battler = Battler()

    def _revive_request_json(self, reviving):
        active_pkmn = {
            "ident": "p1: Pawmot",
            "details": "Pawmot, L82, M",
            "condition": "160/263",
            "active": True,
            "stats": {
                "atk": 250,
                "def": 160,
                "spa": 150,
                "spd": 160,
                "spe": 220,
            },
            "moves": [
                "revivalblessing",
                "closecombat",
                "doubleshock",
                "machpunch",
            ],
            "baseAbility": "voltabsorb",
            "item": "leftovers",
            "ability": "voltabsorb",
        }
        if reviving:
            active_pkmn["reviving"] = True

        return {
            "forceSwitch": [True],
            "noCancel": True,
            "side": {
                "name": "BigBluePikachu",
                "id": "p1",
                "pokemon": [
                    active_pkmn,
                    {
                        "ident": "p1: Kingambit",
                        "details": "Kingambit, L76, F",
                        "condition": "0 fnt",
                        "active": False,
                        "stats": {
                            "atk": 240,
                            "def": 220,
                            "spa": 130,
                            "spd": 170,
                            "spe": 120,
                        },
                        "moves": [
                            "suckerpunch",
                            "kowtowcleave",
                            "ironhead",
                            "swordsdance",
                        ],
                        "baseAbility": "supremeoverlord",
                        "item": "blackglasses",
                        "ability": "supremeoverlord",
                    },
                ],
            },
        }

    def test_revive_request_json_sets_reviving_and_revive_prompt_is_detected(self):
        self.battler.active = Pokemon("pawmot", 82)
        self.battler.reserve = [Pokemon("kingambit", 76)]

        self.battler.update_from_request_json(self._revive_request_json(reviving=True))

        self.assertTrue(self.battler.active.reviving)
        self.assertEqual(0, self.battler.reserve[0].hp)
        self.assertTrue(battler_has_revive_prompt(self.battler))

    def test_fainted_reserve_keeps_previously_known_max_hp(self):
        self.battler.active = Pokemon("pawmot", 82)
        reserve = Pokemon("kingambit", 76)
        reserve.max_hp = 310
        self.battler.reserve = [reserve]

        self.battler.update_from_request_json(self._revive_request_json(reviving=True))

        self.assertEqual(0, self.battler.reserve[0].hp)
        self.assertEqual(310, self.battler.reserve[0].max_hp)

    def test_normal_force_switch_request_json_does_not_have_revive_prompt(self):
        self.battler.active = Pokemon("pawmot", 82)
        self.battler.reserve = [Pokemon("kingambit", 76)]

        self.battler.update_from_request_json(self._revive_request_json(reviving=False))

        self.assertFalse(self.battler.active.reviving)
        self.assertFalse(battler_has_revive_prompt(self.battler))


class TestBattlerToPokeEngineSideRevivalBlessing(unittest.TestCase):
    def setUp(self):
        self.battler = Battler()
        self.battler.active = Pokemon("pawmot", 82)
        fainted_reserve = Pokemon("kingambit", 76)
        fainted_reserve.hp = 0
        fainted_reserve.fainted = True
        self.battler.reserve = [fainted_reserve]

    def test_revive_prompt_battler_produces_side_with_revival_blessing(self):
        if not POKE_ENGINE_SUPPORTS_REVIVAL_BLESSING:
            self.skipTest(
                "installed poke_engine binary does not support the "
                "`revival_blessing` field"
            )
        self.battler.active.reviving = True

        side = battler_to_poke_engine_side(self.battler, force_switch=True)

        self.assertTrue(side.revival_blessing)
        self.assertTrue(side.force_switch)

    def test_battler_without_revive_prompt_produces_side_without_revival_blessing(
        self,
    ):
        if not POKE_ENGINE_SUPPORTS_REVIVAL_BLESSING:
            self.skipTest(
                "installed poke_engine binary does not support the "
                "`revival_blessing` field"
            )
        side = battler_to_poke_engine_side(self.battler)

        self.assertFalse(side.revival_blessing)

    def test_conversion_succeeds_regardless_of_revival_blessing_support(self):
        # the kwarg is wheel-compat guarded like every other newer field:
        # conversion must not raise whether or not the binary has it
        self.battler.active.reviving = True

        side = battler_to_poke_engine_side(self.battler, force_switch=True)

        self.assertEqual(6, len(side.pokemon))

    def test_times_revived_is_forwarded_to_the_engine_side(self):
        if not POKE_ENGINE_SUPPORTS_TIMES_REVIVED:
            self.skipTest(
                "installed poke_engine binary does not support the "
                "`times_revived` field"
            )
        self.battler.times_revived = 2

        side = battler_to_poke_engine_side(self.battler)

        # PS totalFainted == currently-fainted + revives; the engine adds the two
        # (genx/abilities.rs:3127), so the forwarded value must be the revive count
        # alone, not the sum.
        self.assertEqual(2, side.times_revived)

    def test_times_revived_defaults_to_zero_on_a_fresh_battler(self):
        if not POKE_ENGINE_SUPPORTS_TIMES_REVIVED:
            self.skipTest(
                "installed poke_engine binary does not support the "
                "`times_revived` field"
            )
        side = battler_to_poke_engine_side(self.battler)

        self.assertEqual(0, side.times_revived)

    def test_conversion_succeeds_regardless_of_times_revived_support(self):
        # wheel-compat guarded exactly like revival_blessing / last_move_failed:
        # an older binary rejects the kwarg, and conversion must still not raise
        self.battler.times_revived = 3

        side = battler_to_poke_engine_side(self.battler)

        self.assertEqual(6, len(side.pokemon))

    def test_battler_missing_the_times_revived_attribute_defaults_to_zero(self):
        # a Battler restored from an older pickle/replay path may predate the field
        if not POKE_ENGINE_SUPPORTS_TIMES_REVIVED:
            self.skipTest(
                "installed poke_engine binary does not support the "
                "`times_revived` field"
            )
        del self.battler.times_revived

        side = battler_to_poke_engine_side(self.battler)

        self.assertEqual(0, side.times_revived)

    def test_side_keeps_fainted_reserve_maxhp_and_pads_with_maxhp_0_dummies(self):
        self.battler.reserve[0].max_hp = 310

        side = battler_to_poke_engine_side(self.battler)

        # active + 1 reserve + 4 padding dummies
        self.assertEqual(6, len(side.pokemon))

        fainted_reserve = side.pokemon[1]
        self.assertEqual(0, fainted_reserve.hp)
        self.assertEqual(310, fainted_reserve.maxhp)

        # empty party slots must have maxhp == 0 so the engine never
        # treats them as fainted pkmn (e.g. as Revival Blessing targets)
        for dummy in side.pokemon[2:]:
            self.assertEqual(0, dummy.hp)
            self.assertEqual(0, dummy.maxhp)


class TestBattlerToPokeEngineSideRevealed(unittest.TestCase):
    def setUp(self):
        self.battler = Battler()
        self.battler.active = Pokemon("pawmot", 82)
        self.battler.active.revealed = True
        self.battler.reserve = [Pokemon("kingambit", 76)]

    def test_unrevealed_pkmn_converts_without_error(self):
        # must not raise regardless of whether the installed poke_engine
        # binary supports the `revealed` field
        self.battler.reserve[0].revealed = False

        side = battler_to_poke_engine_side(self.battler)

        self.assertEqual(6, len(side.pokemon))

    def test_revealed_passes_through_to_engine_pkmn(self):
        if not POKE_ENGINE_SUPPORTS_REVEALED:
            self.skipTest(
                "installed poke_engine binary does not support the `revealed` field"
            )
        self.battler.reserve[0].revealed = False

        side = battler_to_poke_engine_side(self.battler)

        self.assertTrue(side.pokemon[0].revealed)
        self.assertFalse(side.pokemon[1].revealed)

    def test_pkmn_missing_the_revealed_attribute_defaults_to_revealed(self):
        if not POKE_ENGINE_SUPPORTS_REVEALED:
            self.skipTest(
                "installed poke_engine binary does not support the `revealed` field"
            )
        # e.g. an old pickled Pokemon object from before the attribute existed
        del self.battler.reserve[0].revealed

        side = battler_to_poke_engine_side(self.battler)

        self.assertTrue(side.pokemon[1].revealed)


class TestBattlerToPokeEngineSideIllusionBroken(unittest.TestCase):
    def setUp(self):
        self.battler = Battler()
        self.battler.active = Pokemon("zoroark", 82)
        self.battler.active.revealed = True
        self.battler.reserve = [Pokemon("kingambit", 76)]

    def test_illusion_broken_pkmn_converts_without_error(self):
        # must not raise regardless of whether the installed poke_engine
        # binary supports the `illusion_broken` field
        self.battler.active.illusion_broken = True

        side = battler_to_poke_engine_side(self.battler)

        self.assertEqual(6, len(side.pokemon))

    def test_illusion_broken_passes_through_to_engine_pkmn(self):
        if not POKE_ENGINE_SUPPORTS_ILLUSION_BROKEN:
            self.skipTest(
                "installed poke_engine binary does not support the "
                "`illusion_broken` field"
            )
        self.battler.active.illusion_broken = True

        side = battler_to_poke_engine_side(self.battler)

        self.assertTrue(side.pokemon[0].illusion_broken)
        self.assertFalse(side.pokemon[1].illusion_broken)

    def test_pkmn_missing_the_illusion_broken_attribute_defaults_to_false(self):
        if not POKE_ENGINE_SUPPORTS_ILLUSION_BROKEN:
            self.skipTest(
                "installed poke_engine binary does not support the "
                "`illusion_broken` field"
            )
        # e.g. an old pickled Pokemon object from before the attribute existed
        del self.battler.active.illusion_broken

        side = battler_to_poke_engine_side(self.battler)

        self.assertFalse(side.pokemon[0].illusion_broken)


class TestBattlerToPokeEngineSideKnown(unittest.TestCase):
    def setUp(self):
        self.battler = Battler()
        self.battler.active = Pokemon("pawmot", 82)
        self.battler.active.revealed = True
        self.battler.reserve = [Pokemon("kingambit", 76)]

    def test_known_matches_revealed_at_construction(self):
        if not POKE_ENGINE_SUPPORTS_KNOWN:
            self.skipTest(
                "installed poke_engine binary does not support the `known` field"
            )
        # `known` is a snapshot of `revealed` at conversion; they diverge
        # only in-search (revealed is toggled by the engine, known never is)
        self.battler.reserve[0].revealed = False

        side = battler_to_poke_engine_side(self.battler)

        self.assertTrue(side.pokemon[0].known)
        self.assertFalse(side.pokemon[1].known)

    def test_pkmn_missing_the_revealed_attribute_defaults_to_known(self):
        if not POKE_ENGINE_SUPPORTS_KNOWN:
            self.skipTest(
                "installed poke_engine binary does not support the `known` field"
            )
        # e.g. an old pickled Pokemon object from before the attribute existed
        del self.battler.reserve[0].revealed

        side = battler_to_poke_engine_side(self.battler)

        self.assertTrue(side.pokemon[1].known)


class TestBattlerToPokeEngineSideTimesAttacked(unittest.TestCase):
    def setUp(self):
        self.battler = Battler()
        self.battler.active = Pokemon("annihilape", 82)
        self.battler.active.revealed = True
        self.battler.reserve = [Pokemon("kingambit", 76)]

    def test_pkmn_with_times_attacked_converts_without_error(self):
        # must not raise regardless of whether the installed poke_engine
        # binary supports the `times_attacked` field
        self.battler.active.times_attacked = 3

        side = battler_to_poke_engine_side(self.battler)

        self.assertEqual(6, len(side.pokemon))

    def test_times_attacked_passes_through_to_engine_pkmn(self):
        if not POKE_ENGINE_SUPPORTS_TIMES_ATTACKED:
            self.skipTest(
                "installed poke_engine binary does not support the `times_attacked` field"
            )
        self.battler.active.times_attacked = 3

        side = battler_to_poke_engine_side(self.battler)

        self.assertEqual(3, side.pokemon[0].times_attacked)
        self.assertEqual(0, side.pokemon[1].times_attacked)

    def test_pkmn_missing_the_times_attacked_attribute_defaults_to_zero(self):
        if not POKE_ENGINE_SUPPORTS_TIMES_ATTACKED:
            self.skipTest(
                "installed poke_engine binary does not support the `times_attacked` field"
            )
        # e.g. an old pickled Pokemon object from before the attribute existed
        del self.battler.active.times_attacked

        side = battler_to_poke_engine_side(self.battler)

        self.assertEqual(0, side.pokemon[0].times_attacked)


class TestBattlerToPokeEngineSideTransformed(unittest.TestCase):
    def setUp(self):
        # a Ditto that has transformed into Gengar: the `transform` battle-
        # modifier copies stats/types onto the Ditto object but leaves `name`
        # as 'ditto' and records the copied species in `transformed_into`
        self.battler = Battler()
        self.ditto = Pokemon("ditto", 100)
        self.ditto.revealed = True
        self.ditto.transformed_into = "gengar"
        self.ditto.types = ["ghost", "poison"]
        self.battler.active = self.ditto
        self.battler.reserve = [Pokemon("kingambit", 76)]

    def test_transformed_pkmn_uses_target_species_id_and_weight(self):
        side = battler_to_poke_engine_side(self.battler)

        # id, weight, and base_types follow the transformed-into species
        # (gengar 40.5kg / ghost-poison) rather than base Ditto (4.0kg / normal)
        self.assertEqual("gengar", side.pokemon[0].id)
        self.assertEqual(40.5, side.pokemon[0].weight_kg)
        self.assertEqual(("ghost", "poison"), side.pokemon[0].base_types)

    def test_non_transformed_pkmn_uses_own_species(self):
        # regression: a plain (untransformed) Ditto still resolves to itself
        self.ditto.transformed_into = None
        self.ditto.types = ["normal"]

        side = battler_to_poke_engine_side(self.battler)

        self.assertEqual("ditto", side.pokemon[0].id)
        self.assertEqual(4.0, side.pokemon[0].weight_kg)

    def test_transformed_pkmn_missing_attribute_defaults_to_own_species(self):
        # e.g. an old pickled Pokemon object from before the attribute existed
        del self.ditto.transformed_into

        side = battler_to_poke_engine_side(self.battler)

        self.assertEqual("ditto", side.pokemon[0].id)
        self.assertEqual(4.0, side.pokemon[0].weight_kg)

    def test_transformed_flag_passes_through_when_supported(self):
        if not POKE_ENGINE_SUPPORTS_TRANSFORMED:
            self.skipTest(
                "installed poke_engine binary does not support the `transformed` field"
            )
        side = battler_to_poke_engine_side(self.battler)

        self.assertTrue(side.pokemon[0].transformed)
        # a non-transformed reserve pkmn is not flagged
        self.assertFalse(side.pokemon[1].transformed)


class TestBattlerToPokeEngineSideAccuracyEvasion(unittest.TestCase):
    def setUp(self):
        self.battler = Battler()
        self.battler.active = Pokemon("dragapult", 82)
        self.battler.reserve = []

    def test_accuracy_and_evasion_boosts_pass_through(self):
        self.battler.active.boosts[constants.ACCURACY] = 2
        self.battler.active.boosts[constants.EVASION] = -1

        side = battler_to_poke_engine_side(self.battler)

        self.assertEqual(2, side.accuracy_boost)
        self.assertEqual(-1, side.evasion_boost)

    def test_default_accuracy_and_evasion_boosts_are_zero(self):
        side = battler_to_poke_engine_side(self.battler)

        self.assertEqual(0, side.accuracy_boost)
        self.assertEqual(0, side.evasion_boost)


class TestBattlerToPokeEngineSideLastConsumedItem(unittest.TestCase):
    def setUp(self):
        self.battler = Battler()
        self.battler.active = Pokemon("farigiraf", 82)
        self.battler.active.revealed = True
        self.battler.reserve = [Pokemon("kingambit", 76)]

    def test_consumed_berry_forwards_to_last_consumed_item(self):
        if not POKE_ENGINE_SUPPORTS_LAST_CONSUMED_ITEM:
            self.skipTest(
                "installed poke_engine binary does not support the "
                "`last_consumed_item` field"
            )
        # a berry eaten before this decision must be visible at root or the
        # engine can never explore Harvest/Cud Chew recycling
        # (poke-engine genx/abilities.rs:1314-1315)
        self.battler.active.item = None
        self.battler.active.removed_item = "sitrusberry"

        side = battler_to_poke_engine_side(self.battler)

        self.assertEqual("sitrusberry", side.pokemon[0].last_consumed_item)

    def test_no_removed_item_leaves_engine_default(self):
        if not POKE_ENGINE_SUPPORTS_LAST_CONSUMED_ITEM:
            self.skipTest(
                "installed poke_engine binary does not support the "
                "`last_consumed_item` field"
            )
        side = battler_to_poke_engine_side(self.battler)

        self.assertEqual("none", side.pokemon[0].last_consumed_item.lower())

    def test_knocked_off_item_is_not_forwarded(self):
        if not POKE_ENGINE_SUPPORTS_LAST_CONSUMED_ITEM:
            self.skipTest(
                "installed poke_engine binary does not support the "
                "`last_consumed_item` field"
            )
        # a knocked-off item was removed, not consumed: PS only sets lastItem
        # on use/eat, so Harvest must not be able to regrow it
        self.battler.active.item = None
        self.battler.active.removed_item = "sitrusberry"
        self.battler.active.knocked_off = True

        side = battler_to_poke_engine_side(self.battler)

        self.assertEqual("none", side.pokemon[0].last_consumed_item.lower())

    def test_pkmn_missing_the_removed_item_attribute_converts(self):
        # e.g. an old pickled Pokemon object from before the attribute existed
        del self.battler.active.removed_item

        side = battler_to_poke_engine_side(self.battler)

        self.assertEqual(6, len(side.pokemon))

    def test_conversion_succeeds_regardless_of_last_consumed_item_support(self):
        self.battler.active.removed_item = "sitrusberry"

        side = battler_to_poke_engine_side(self.battler)

        self.assertEqual(6, len(side.pokemon))


class TestBattlerToPokeEngineSideDisable(unittest.TestCase):
    def setUp(self):
        self.battler = Battler()
        self.battler.active = Pokemon("dragapult", 82)
        for mv in ["dracometeor", "shadowball", "uturn", "thunderbolt"]:
            self.battler.active.add_move(mv)
        self.battler.reserve = []

    def test_disabled_move_is_passed_to_engine_as_disabled(self):
        for m in self.battler.active.moves:
            if m.name == "thunderbolt":
                m.disabled = True

        side = battler_to_poke_engine_side(self.battler)

        engine_moves = {m.id: m.disabled for m in side.pokemon[0].moves}
        self.assertTrue(engine_moves["thunderbolt"])
        self.assertFalse(engine_moves["dracometeor"])

    def test_disable_duration_passes_through_when_supported(self):
        if not POKE_ENGINE_SUPPORTS_DISABLE_DURATION:
            self.skipTest(
                "installed poke_engine binary does not support the `disable` "
                "volatile-status duration field"
            )
        self.battler.active.volatile_status_durations[constants.DISABLE] = 4

        side = battler_to_poke_engine_side(self.battler)

        self.assertEqual(4, side.volatile_status_durations.disable)

    def test_conversion_succeeds_regardless_of_disable_support(self):
        # must not raise whether or not the installed binary has the field
        self.battler.active.volatile_status_durations[constants.DISABLE] = 4

        side = battler_to_poke_engine_side(self.battler)

        self.assertEqual(6, len(side.pokemon))


class TestMoreThan4RevealedMoves(unittest.TestCase):
    """A pokemon can reveal more than 4 moves (e.g. a Zoroark illusion piling its
    own moves onto the disguised species). The engine moveset has exactly 4 slots,
    so the conversion must truncate rather than hand the engine an oversized
    moveset or an out-of-range last_used_move index: the wheel's
    PokemonMoveIndex::deserialize panics on "4" and the resulting PanicException
    (a BaseException) escapes `except Exception` and kills the bot."""

    FIVE_MOVES = ["fakeout", "doublehit", "uturn", "knockoff", "gunkshot"]

    def setUp(self):
        self.battler = Battler()
        self.battler.active = Pokemon("ambipom", 82)
        for mv in self.FIVE_MOVES:
            self.battler.active.add_move(mv)
        self.battler.reserve = []

    def test_pkmn_with_5_moves_keeps_the_most_recent_4(self):
        # `pkmn.moves` is in REVEAL order: the newest evidence is the most
        # decision-relevant, so the window is the TAIL, not the head
        engine_pkmn = pokemon_to_poke_engine_pkmn(self.battler.active)

        self.assertEqual(4, len(engine_pkmn.moves))
        self.assertEqual(self.FIVE_MOVES[1:], [m.id for m in engine_pkmn.moves])

    def test_conversion_does_not_mutate_the_input_pokemon(self):
        pokemon_to_poke_engine_pkmn(self.battler.active)

        self.assertEqual(
            self.FIVE_MOVES, [m.name for m in self.battler.active.moves]
        )

    def test_side_with_5_move_active_converts_without_error(self):
        side = battler_to_poke_engine_side(self.battler)

        self.assertEqual(4, len(side.pokemon[0].moves))

    def test_last_used_move_inside_the_window_uses_its_window_index(self):
        self.battler.last_used_move = LastUsedMove("ambipom", "knockoff", 1)

        side = battler_to_poke_engine_side(self.battler)

        # knockoff is slot 3 of 5 revealed, slot 2 of the kept tail window
        self.assertEqual("move:2", side.last_used_move)
        self.assertEqual(
            "knockoff", side.pokemon[0].moves[2].id
        )

    def test_newest_move_survives_truncation_and_keeps_its_index(self):
        # "gunkshot" is the 5th revealed move: under the old head-window it was
        # truncated away and last_used_move silently relabelled as slot 0,
        # Encore/choice-locking the engine onto the wrong move
        self.battler.last_used_move = LastUsedMove("ambipom", "gunkshot", 1)

        side = battler_to_poke_engine_side(self.battler)

        self.assertEqual("move:3", side.last_used_move)
        self.assertEqual("gunkshot", side.pokemon[0].moves[3].id)

    def test_last_used_move_outside_the_window_is_pulled_into_it(self):
        # fakeout is the OLDEST revealed move: the tail window would drop it,
        # but a move the side actually just used must keep an index
        self.battler.last_used_move = LastUsedMove("ambipom", "fakeout", 1)

        side = battler_to_poke_engine_side(self.battler)

        self.assertEqual("move:0", side.last_used_move)
        self.assertEqual("fakeout", side.pokemon[0].moves[0].id)


class TestEngineToleratesOutOfRangeLastUsedMoveIndex(unittest.TestCase):
    """The engine itself must also degrade gracefully if an out-of-range
    last_used_move index reaches it through a path the python-side guards do not
    cover. poke-engine src/state.rs now treats an out-of-range index as
    "move:none" (with a stderr warning) instead of panicking."""

    def test_state_string_with_move_index_4_parses_without_panic(self):
        battler = Battler()
        battler.active = Pokemon("ambipom", 82)
        for mv in ["fakeout", "doublehit", "uturn", "knockoff"]:
            battler.active.add_move(mv)
        battler.reserve = []

        state = PokeEngineState(
            side_one=battler_to_poke_engine_side(battler),
            side_two=battler_to_poke_engine_side(battler),
        )
        serialized = state.to_string()
        # both sides serialize their (unset) last_used_move as "move:none" and
        # nothing else in the state string contains that substring; patch side
        # one's to the out-of-range index a 5th revealed move would produce
        self.assertIn("move:none", serialized)
        patched = serialized.replace("move:none", "move:4", 1)

        # an old wheel panics inside from_string (PanicException); the fixed
        # engine degrades the unrepresentable index to "unknown"
        reparsed = PokeEngineState.from_string(patched)

        self.assertEqual("move:none", reparsed.side_one.last_used_move)


class TestBaseAbilitySeeding(unittest.TestCase):
    """The engine reverts `ability` to `base_ability` on every in-tree
    switch-out, so an empty base_ability permanently strips the real ability
    from both sides after the first simulated pivot."""

    def setUp(self):
        self.pkmn = Pokemon("slowbro", 84)
        self.pkmn.ability = "regenerator"

    def test_real_ability_is_used_as_base_ability(self):
        engine_pkmn = pokemon_to_poke_engine_pkmn(self.pkmn)

        self.assertEqual("regenerator", engine_pkmn.base_ability)

    def test_original_ability_still_wins(self):
        # something CHANGED the ability mid-battle: the pre-change ability is
        # the one the engine must revert to
        self.pkmn.original_ability = "oblivious"
        self.pkmn.ability = "regenerator"

        engine_pkmn = pokemon_to_poke_engine_pkmn(self.pkmn)

        self.assertEqual("oblivious", engine_pkmn.base_ability)

    def test_unknown_ability_stays_empty(self):
        self.pkmn.ability = None

        engine_pkmn = pokemon_to_poke_engine_pkmn(self.pkmn)

        # the engine renders its NONE ability as "None"
        self.assertEqual("None", engine_pkmn.base_ability)


class TestRevealMaskAnnotationAndWiring(unittest.TestCase):
    """PKNN v7 reveal-mask wiring: annotate_reveal_masks on the root battle ->
    Pokemon.reveal_mask/.reveal_mask_moves attributes -> engine field 36.

    Expected ints are HAND-COMPUTED from the bit table in
    poke-engine/src/state.rs / valuenet/reveal_masks.py:
    ITEM=0x01 ABILITY=0x02 VALID=0x04 MOVE_i=0x08<<i TERA=0x80.

    The dataset fixture has exactly two pincurchin sets:
      S1: terrainextender / electricsurge / tera electric,
          moves risingvoltage, thunderbolt, surf, recover
      S2: choicespecs / lightningrod / tera grass,
          moves thunderbolt, surf, voltswitch, scald
    so item/ability/tera all stay ambiguous until evidence eliminates a set.
    """

    def setUp(self):
        from data.pkmn_sets import RandomBattleTeamDatasets

        RandomBattleTeamDatasets.__init__()
        RandomBattleTeamDatasets.pkmn_mode = "gen9randombattle"
        RandomBattleTeamDatasets.raw_pkmn_sets = {
            "pincurchin": {
                "82,terrainextender,electricsurge,risingvoltage,thunderbolt,surf,recover,electric": 10,
                "82,choicespecs,lightningrod,thunderbolt,surf,voltswitch,scald,grass": 5,
            }
        }
        RandomBattleTeamDatasets._initialize_pkmn_sets()
        self.addCleanup(RandomBattleTeamDatasets.__init__)

        self.battle = Battle("test-reveal-masks")
        self.opp = Pokemon("pincurchin", 82)
        self.opp.revealed = True
        self.battle.opponent.active = self.opp
        self.me = Pokemon("pincurchin", 82)
        self.me.revealed = True
        # our OWN attributes are privately known in full - the mask must not
        # leak them (it encodes what the OPPONENT knows of us)
        self.me.item = "terrainextender"
        self.me.ability = "electricsurge"
        self.me.tera_type = "electric"
        for mv in ("risingvoltage", "thunderbolt", "surf", "recover"):
            self.me.add_move(mv)
        self.battle.user.active = self.me

    def test_opponent_nothing_known_is_valid_only(self):
        annotate_reveal_masks(self.battle)
        self.assertEqual(0x04, self.opp.reveal_mask)
        self.assertEqual(frozenset(), self.opp.reveal_mask_moves)

    def test_opponent_announced_item_outside_dataset_sets_item_bit_only(self):
        # announced knowledge is read off the tracker sentinels even when the
        # dataset knows nothing (no set carries leftovers -> no collapse)
        self.opp.item = "leftovers"
        annotate_reveal_masks(self.battle)
        self.assertEqual(0x04 | 0x01, self.opp.reveal_mask)

    def test_opponent_item_none_counts_as_knowledge(self):
        # `item is None` means known-to-hold-NOTHING (consumed/knocked off):
        # that is knowledge, only UNKNOWN_ITEM is ignorance
        self.opp.item = None
        annotate_reveal_masks(self.battle)
        self.assertEqual(0x04 | 0x01, self.opp.reveal_mask)

    def test_opponent_observed_move_collapses_candidate_set(self):
        # risingvoltage exists only in S1 -> item+ability+tera all deduced to
        # certainty: announced-only would be wrong (V7_ENCODER_SPEC.md 4.2)
        self.opp.add_move("risingvoltage")
        annotate_reveal_masks(self.battle)
        self.assertEqual(0x04 | 0x01 | 0x02 | 0x80, self.opp.reveal_mask)
        self.assertEqual(frozenset(["risingvoltage"]), self.opp.reveal_mask_moves)

    def test_opponent_never_seen_is_exactly_valid(self):
        self.opp.revealed = False
        annotate_reveal_masks(self.battle)
        self.assertEqual(0x04, self.opp.reveal_mask)

    def test_user_private_knowledge_does_not_leak_into_mask(self):
        # us knowing our own item/ability/tera is NOT the opponent knowing it:
        # with no public evidence the mask stays bare VALID
        annotate_reveal_masks(self.battle)
        self.assertEqual(0x04, self.me.reveal_mask)
        self.assertEqual(frozenset(), self.me.reveal_mask_moves)

    def test_user_unrevealed_mon_is_exactly_valid(self):
        self.me.revealed = False
        annotate_reveal_masks(self.battle)
        self.assertEqual(0x04, self.me.reveal_mask)

    def test_user_used_move_is_public_and_collapses_their_view(self):
        # pp spent IS "the opponent has seen this move" (fp/infostate.py:229);
        # risingvoltage eliminates S2 for the opponent's rational inference,
        # so they now know our item/ability/tera too
        self.me.moves[0].current_pp -= 1
        annotate_reveal_masks(self.battle)
        self.assertEqual(0x04 | 0x01 | 0x02 | 0x80, self.me.reveal_mask)
        self.assertEqual(frozenset(["risingvoltage"]), self.me.reveal_mask_moves)

    def test_user_knocked_off_item_is_public(self):
        self.me.knocked_off = True
        annotate_reveal_masks(self.battle)
        self.assertEqual(0x04 | 0x01, self.me.reveal_mask)

    def test_conversion_without_annotation_leaves_mask_zero(self):
        if not POKE_ENGINE_SUPPORTS_REVEAL_MASK:
            self.skipTest("wheel lacks reveal_mask")
        pkmn = Pokemon("pincurchin", 82)
        pkmn.item = "leftovers"
        engine_pkmn = pokemon_to_poke_engine_pkmn(pkmn)
        # 0 = "no mask supplied": the replay checker / damage paths must keep
        # constructing byte-identical states
        self.assertEqual(0, engine_pkmn.reveal_mask)

    def test_conversion_maps_observed_move_names_to_final_engine_slots(self):
        if not POKE_ENGINE_SUPPORTS_REVEAL_MASK:
            self.skipTest("wheel lacks reveal_mask")
        self.opp.add_move("risingvoltage")
        annotate_reveal_masks(self.battle)
        self.assertEqual(0x87, self.opp.reveal_mask)
        # simulate the sampled world: populate rebuilt pkmn.moves in the
        # SET's order, so the observed move landed in a different slot
        self.opp.moves = []
        for mv in ("surf", "thunderbolt", "recover", "risingvoltage"):
            self.opp.add_move(mv)
        self.opp.item = "terrainextender"
        self.opp.ability = "electricsurge"
        engine_pkmn = pokemon_to_poke_engine_pkmn(self.opp)
        # base 0x87 plus MOVE bit for slot 3 (0x08 << 3 = 0x40) = 0xC7; the
        # unobserved sampled moves must NOT get bits
        self.assertEqual(0x87 | 0x40, engine_pkmn.reveal_mask)

    def test_sampler_fill_in_on_annotated_battler_gets_valid_not_zero(self):
        if not POKE_ENGINE_SUPPORTS_REVEAL_MASK:
            self.skipTest("wheel lacks reveal_mask")
        annotate_reveal_masks(self.battle)
        # a never-seen mon invented by the world sampler AFTER annotation:
        # "we have seen nothing of it" = RM_VALID, never 0 = "no mask"
        fill_in = Pokemon("pawmot", 82)
        fill_in.item = "leftovers"
        fill_in.ability = "ironfist"
        self.battle.opponent.reserve.append(fill_in)
        state = battle_to_poke_engine_state(self.battle)
        self.assertEqual(0x04, state.side_two.pokemon[1].reveal_mask)

    def test_masks_survive_deepcopy_and_serialize_into_field_36(self):
        if not POKE_ENGINE_SUPPORTS_REVEAL_MASK:
            self.skipTest("wheel lacks reveal_mask")
        from copy import deepcopy

        self.opp.item = None  # knocked off: ITEM bit
        annotate_reveal_masks(self.battle)
        world = deepcopy(self.battle)  # what prepare_random_battles does
        world.opponent.active.item = "choicespecs"  # determinizer fill
        state = battle_to_poke_engine_state(world)
        self.assertEqual(0x05, state.side_two.pokemon[0].reveal_mask)
        self.assertEqual(0x04, state.side_one.pokemon[0].reveal_mask)
        # field 36 of the serialized mon is the byte the engine's NN encoder
        # and every downstream worker parse
        side_two_mon0 = state.to_string().split("/")[1].split("=")[0]
        self.assertEqual("5", side_two_mon0.split(",")[36])
