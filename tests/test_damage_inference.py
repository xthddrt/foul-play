import unittest
from unittest import mock

import constants
from constants import BattleType
from data.pkmn_sets import PokemonMoveset, PokemonSet, PredictedPokemonSet
from fp.battle import Battle, DamageDealt, LastUsedMove, Pokemon
from fp.inference import (
    DAMAGE_CHECK_REFUSALS,
    _block_is_confounded,
    _damage_was_capped,
    _do_check,
    _use_exact_membership,
    get_damage_dealt,
    reset_damage_check_refusals,
)


def _set(item):
    return PokemonSet(
        ability="static",
        item=item,
        nature="serious",
        evs=(85, 85, 85, 85, 85, 85),
        count=1,
    )


class _DamageInferenceTestCase(unittest.TestCase):
    def setUp(self):
        reset_damage_check_refusals()
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.battle_type = BattleType.RANDOM_BATTLE

        self.battle.user.active = Pokemon("caterpie", 100)
        self.battle.user.active.max_hp = 300
        self.battle.user.active.hp = 300
        self.battle.user.last_selected_move = LastUsedMove("caterpie", "tackle", 1)

        self.battle.opponent.active = Pokemon("pikachu", 100)
        self.battle.opponent.active.ability = None
        self.battle.opponent.active.item = constants.UNKNOWN_ITEM

        self.battle_copy = self.battle

    def _run(self, possibilities, damage_dealt, check_lower_bound=True):
        _do_check(
            self.battle,
            self.battle_copy,
            possibilities,
            "damage_dealt",
            damage_dealt,
            bot_went_first=True,
            check_lower_bound=check_lower_bound,
        )


class TestExactRollSetMembership(_DamageInferenceTestCase):
    """Item 6: our hp is request-exact, so the delta is an integer fact."""

    def test_exact_delta_eliminates_only_the_set_whose_roll_set_misses_it(self):
        band_rolls = list(range(120, 136))  # a Choice Band set
        plain_rolls = list(range(85, 101))  # the item-less set
        # 96 is inside the plain set and nowhere near the band set - the old
        # +-2.5%/+-5hp band around the max roll admitted BOTH
        damage_dealt = DamageDealt(
            attacker="pikachu",
            defender="caterpie",
            move="tackle",
            percent_damage=0.32,
            crit=False,
            exact_damage=96,
        )
        possibilities = [_set("choiceband"), _set("leftovers")]

        roll_sets = iter([(band_rolls, band_rolls), (plain_rolls, plain_rolls)])
        with mock.patch(
            "fp.inference.poke_engine_get_damage_roll_sets",
            side_effect=lambda *a, **k: (None, next(roll_sets)),
        ):
            self._run(possibilities, damage_dealt)

        self.assertEqual(1, len(possibilities))
        self.assertEqual("leftovers", possibilities[0].item)

    def test_multihit_move_falls_back_to_the_band(self):
        damage_dealt = DamageDealt(
            attacker="pikachu",
            defender="caterpie",
            move="bulletseed",
            percent_damage=0.32,
            crit=False,
            exact_damage=96,
        )
        self.assertFalse(_use_exact_membership(damage_dealt, "damage_dealt"))

        possibilities = [_set("choiceband"), _set("leftovers")]
        with mock.patch(
            "fp.inference.poke_engine_get_damage_roll_sets",
            side_effect=AssertionError("roll sets must not be used for multihit"),
        ), mock.patch(
            "fp.inference.poke_engine_get_damage_rolls",
            return_value=(None, (100, 150)),
        ):
            self._run(possibilities, damage_dealt)

        # both survive the wide band
        self.assertEqual(2, len(possibilities))

    def test_damage_received_never_uses_exact_membership(self):
        damage_dealt = DamageDealt(
            attacker="caterpie",
            defender="pikachu",
            move="tackle",
            percent_damage=0.32,
            crit=False,
            exact_damage=None,
        )
        self.assertFalse(_use_exact_membership(damage_dealt, "damage_received"))


class TestLethalAndCappedLowerBound(_DamageInferenceTestCase):
    """Item 7: a clamped observation is only a LOWER bound on the roll."""

    def test_zero_fnt_sets_the_lethal_flag(self):
        self.battle.user.active.hp = 40
        messages = [
            "|move|p2a: Pikachu|Tackle|p1a: Caterpie",
            "|-damage|p1a: Caterpie|0 fnt",
            "|faint|p1a: Caterpie",
        ]
        damage_dealt = get_damage_dealt(
            self.battle, messages[0].split("|"), messages[1:]
        )
        self.assertTrue(damage_dealt.lethal)
        self.assertEqual(40, damage_dealt.exact_damage)

    def test_focus_sash_marker_sets_the_capped_flag(self):
        self.assertTrue(
            _damage_was_capped(
                [
                    "|-damage|p1a: Caterpie|1/300",
                    "|-enditem|p1a: Caterpie|Focus Sash",
                ],
                "p1",
            )
        )
        self.assertTrue(
            _damage_was_capped(
                [
                    "|-damage|p1a: Caterpie|1/300",
                    "|-activate|p1a: Caterpie|ability: Sturdy",
                ],
                "p1",
            )
        )
        self.assertFalse(
            _damage_was_capped(["|-damage|p1a: Caterpie|150/300"], "p1")
        )

    def test_capped_observation_keeps_a_stronger_set(self):
        # only 40hp of damage was PRINTED (the mon fainted), but the true roll
        # for the surviving set is 120-135: with the lower bound disabled that
        # set must survive
        damage_dealt = DamageDealt(
            attacker="pikachu",
            defender="caterpie",
            move="tackle",
            percent_damage=0.13,
            crit=False,
            exact_damage=40,
            lethal=True,
        )
        possibilities = [_set("choiceband")]
        with mock.patch(
            "fp.inference.poke_engine_get_damage_roll_sets",
            return_value=(None, (list(range(120, 136)), list(range(180, 196)))),
        ):
            self._run(possibilities, damage_dealt, check_lower_bound=False)

        self.assertEqual(1, len(possibilities))

    def test_capped_observation_still_eliminates_a_too_weak_set(self):
        damage_dealt = DamageDealt(
            attacker="pikachu",
            defender="caterpie",
            move="tackle",
            percent_damage=0.13,
            crit=False,
            exact_damage=40,
            lethal=True,
        )
        possibilities = [_set("choiceband"), _set("leftovers")]
        weak = list(range(5, 21))  # max 20 < the observed 40
        strong = list(range(120, 136))
        roll_sets = iter([(strong, strong), (weak, weak)])
        with mock.patch(
            "fp.inference.poke_engine_get_damage_roll_sets",
            side_effect=lambda *a, **k: (None, next(roll_sets)),
        ):
            self._run(possibilities, damage_dealt, check_lower_bound=False)

        self.assertEqual(1, len(possibilities))
        self.assertEqual("choiceband", possibilities[0].item)


class TestContradictionDiscipline(_DamageInferenceTestCase):
    """Item 8: a refusal is counted, not silently discarded."""

    def test_would_empty_is_counted_and_prunes_nothing(self):
        damage_dealt = DamageDealt(
            attacker="pikachu",
            defender="caterpie",
            move="tackle",
            percent_damage=0.32,
            crit=False,
            exact_damage=96,
        )
        possibilities = [_set("choiceband"), _set("leftovers")]
        impossible = list(range(200, 216))
        with mock.patch(
            "fp.inference.poke_engine_get_damage_roll_sets",
            return_value=(None, (impossible, impossible)),
        ):
            self._run(possibilities, damage_dealt)

        self.assertEqual(2, len(possibilities))
        self.assertEqual(
            1, DAMAGE_CHECK_REFUSALS[("damage_dealt", "tackle", "would_empty")]
        )

    def test_confounded_block_is_detected(self):
        self.assertTrue(
            _block_is_confounded(
                self.battle,
                ["|-boost|p2a: Pikachu|atk|2", "|move|p2a: Pikachu|Tackle|"],
            )
        )
        self.assertTrue(
            _block_is_confounded(
                self.battle, ["|-enditem|p2a: Pikachu|Weakness Policy"]
            )
        )
        # our own boosts are already in the reconstructed state
        self.assertFalse(
            _block_is_confounded(self.battle, ["|-boost|p1a: Caterpie|atk|2"])
        )
        self.assertFalse(_block_is_confounded(self.battle, []))


if __name__ == "__main__":
    unittest.main()


class TestRejectedSetSignatureProducer(_DamageInferenceTestCase):
    """Item 10: eliminations must reach the sampler, not a throwaway list."""

    @staticmethod
    def _predicted(item, moves):
        return PredictedPokemonSet(
            pkmn_set=_set(item),
            pkmn_moveset=PokemonMoveset(moves=moves),
        )

    def test_eliminated_set_signature_lands_on_the_pokemon(self):
        band = self._predicted("choiceband", ("tackle", "thunderbolt"))
        plain = self._predicted("leftovers", ("tackle", "thunderbolt"))
        possibilities = [band, plain]
        damage_dealt = DamageDealt(
            attacker="pikachu",
            defender="caterpie",
            move="tackle",
            percent_damage=0.32,
            crit=False,
            exact_damage=96,
        )

        band_rolls = list(range(120, 136))
        plain_rolls = list(range(85, 101))
        roll_sets = iter([(band_rolls, band_rolls), (plain_rolls, plain_rolls)])
        with mock.patch(
            "fp.inference.poke_engine_get_damage_roll_sets",
            side_effect=lambda *a, **k: (None, next(roll_sets)),
        ):
            self._run(possibilities, damage_dealt)

        signatures = self.battle.opponent.active.rejected_set_signatures
        self.assertIn(band.mechanics_signature(), signatures)
        self.assertNotIn(plain.mechanics_signature(), signatures)
        # and the recorded signature is what the sampler's filter consults
        self.assertFalse(
            band.full_set_pkmn_can_have_set(self.battle.opponent.active)
        )

    def test_nothing_is_recorded_when_the_check_refuses(self):
        band = self._predicted("choiceband", ("tackle",))
        plain = self._predicted("leftovers", ("tackle",))
        possibilities = [band, plain]
        damage_dealt = DamageDealt(
            attacker="pikachu",
            defender="caterpie",
            move="tackle",
            percent_damage=0.32,
            crit=False,
            exact_damage=96,
        )
        impossible = list(range(200, 216))
        with mock.patch(
            "fp.inference.poke_engine_get_damage_roll_sets",
            return_value=(None, (impossible, impossible)),
        ):
            self._run(possibilities, damage_dealt)

        self.assertEqual(set(), self.battle.opponent.active.rejected_set_signatures)

    def test_nothing_is_recorded_when_the_roster_is_known_exactly(self):
        # The replay checker sets `exact_roster_known`; live-only inference must
        # not write to state the checker reads, independently of the checker's
        # `update_dataset_possibilities` stub.
        self.battle.exact_roster_known = True
        band = self._predicted("choiceband", ("tackle", "thunderbolt"))
        plain = self._predicted("leftovers", ("tackle", "thunderbolt"))
        damage_dealt = DamageDealt(
            attacker="pikachu",
            defender="caterpie",
            move="tackle",
            percent_damage=0.32,
            crit=False,
            exact_damage=96,
        )
        band_rolls = list(range(120, 136))
        plain_rolls = list(range(85, 101))
        roll_sets = iter([(band_rolls, band_rolls), (plain_rolls, plain_rolls)])
        with mock.patch(
            "fp.inference.poke_engine_get_damage_roll_sets",
            side_effect=lambda *a, **k: (None, next(roll_sets)),
        ):
            self._run([band, plain], damage_dealt)

        self.assertEqual(set(), self.battle.opponent.active.rejected_set_signatures)
