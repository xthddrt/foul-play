"""Regression tests for SAMPLING_WAVE2_SPEC.md.

One class per spec item.  Every assertion is anchored to Pokemon-Showdown /
poke-engine source, or - for the display-rounding arithmetic - to REAL protocol
lines lifted from the holdout corpora together with the true max HP the
matching `.teams.json` sidecar states for that pokemon.

  1. crit/hitcount dispatch hygiene  (the safety floor for 2-4)
  2. exact inbound roll-set membership, on the item-1 crit stamp
  3. chip-damage max-HP sieve
  4. absolute-HP display sieve
  5. stratified (largest-remainder) world allocation
"""

import unittest
from unittest import mock

import constants
from constants import BattleType
from data.pkmn_sets import PokemonMoveset, PokemonSet, PredictedPokemonSet
from fp.battle import Battle, DamageDealt, LastUsedMove, Pokemon
from fp.battle_modifier import heal_or_damage, process_battle_updates, turn
from fp.hp_certificate import (
    display_pct,
    max_hp_consistent_with_absolute,
    max_hp_consistent_with_residual,
)
from fp.inference import (
    DAMAGE_CHECK_REFUSALS,
    _do_check,
    clear_hit_flags,
    get_damage_dealt,
    reset_damage_check_refusals,
    sieve_sets_by_max_hp,
)
from fp.search.random_battles import (
    _stratified_active_plan,
    largest_remainder_allocation,
)


def _battle():
    battle = Battle(None)
    battle.user.name = "p1"
    battle.opponent.name = "p2"
    battle.battle_type = BattleType.RANDOM_BATTLE
    battle.user.active = Pokemon("caterpie", 100)
    battle.user.active.max_hp = 300
    battle.user.active.hp = 300
    battle.user.last_selected_move = LastUsedMove("caterpie", "tackle", 1)
    battle.opponent.active = Pokemon("pikachu", 100)
    battle.opponent.active.max_hp = 250
    battle.opponent.active.hp = 250
    return battle


# ---------------------------------------------------------------------------
# Item 1: crit / hitcount dispatch hygiene
# ---------------------------------------------------------------------------
class TestCritHitcountDispatchHygiene(unittest.TestCase):
    def setUp(self):
        self.battle = _battle()

    def test_crit_line_stamps_the_pokemon_it_names(self):
        # PS emits `|-crit|<target>` from modifyDamage
        # (sim/battle-actions.ts:1814), so the line names the DEFENDER.
        process_battle_updates(
            _msgs(
                self.battle,
                "|move|p2a: Pikachu|Tackle|p1a: Caterpie",
                "|-crit|p1a: Caterpie",
                "|-damage|p1a: Caterpie|200/300",
            )
        )
        self.assertTrue(self.battle.user.active.crit_this_turn)
        # ...and nothing is stamped onto the attacker
        self.assertFalse(self.battle.opponent.active.crit_this_turn)

    def test_hitcount_line_stamps_the_count(self):
        process_battle_updates(
            _msgs(
                self.battle,
                "|move|p2a: Pikachu|Bullet Seed|p1a: Caterpie",
                "|-damage|p1a: Caterpie|280/300",
                "|-damage|p1a: Caterpie|260/300",
                "|-hitcount|p1a: Caterpie|2",
            )
        )
        self.assertEqual(2, self.battle.user.active.hitcount_this_turn)

    def test_a_new_move_opens_a_fresh_hit_context(self):
        # the crit belongs to the FIRST move only; the second move's consumers
        # must not see it
        process_battle_updates(
            _msgs(
                self.battle,
                "|move|p2a: Pikachu|Tackle|p1a: Caterpie",
                "|-crit|p1a: Caterpie",
                "|-damage|p1a: Caterpie|200/300",
                "|move|p1a: Caterpie|Tackle|p2a: Pikachu",
                "|-damage|p2a: Pikachu|200/250",
            )
        )
        self.assertFalse(self.battle.user.active.crit_this_turn)

    def test_turn_boundary_clears_the_stamps(self):
        self.battle.user.active.crit_this_turn = True
        self.battle.user.active.hitcount_this_turn = 3
        turn(self.battle, ["", "turn", "5"])
        self.assertFalse(self.battle.user.active.crit_this_turn)
        self.assertIsNone(self.battle.user.active.hitcount_this_turn)

    def test_reserve_pokemon_cannot_carry_a_stale_stamp_into_the_field(self):
        # a mon that switches in mid-turn must not inherit an earlier turn's
        # crit; `clear_hit_flags` covers the whole party, not just the actives
        benched = Pokemon("metapod", 100)
        benched.crit_this_turn = True
        self.battle.user.reserve.append(benched)
        clear_hit_flags(self.battle)
        self.assertFalse(benched.crit_this_turn)


def _msgs(battle, *lines):
    battle.msg_list = list(lines)
    return battle


# ---------------------------------------------------------------------------
# Item 2: exact inbound roll-set membership, driven by the item-1 stamp
# ---------------------------------------------------------------------------
class TestExactMembershipUsesTheCritStamp(unittest.TestCase):
    """The crit arm is READ, never re-derived.

    `get_damage_dealt` used to scan the block for `|-crit|` on its own terms
    and did not check which mon the line named, while the substitute
    reconstruction scanned backwards and did.  Two derivations of one fact can
    disagree; picking the crit arm when the hit was not a crit prunes the
    strongest - i.e. the true - candidate set.
    """

    def setUp(self):
        reset_damage_check_refusals()
        self.battle = _battle()
        self.battle.opponent.active.ability = None
        self.battle.opponent.active.item = constants.UNKNOWN_ITEM

    def test_damage_dealt_takes_its_crit_from_the_stamp(self):
        damage = get_damage_dealt(
            self.battle,
            "|move|p2a: Pikachu|Tackle|p1a: Caterpie".split("|"),
            ["|-crit|p1a: Caterpie", "|-damage|p1a: Caterpie|204/300"],
        )
        self.assertTrue(damage.crit)
        self.assertTrue(self.battle.user.active.crit_this_turn)
        self.assertEqual(96, damage.exact_damage)

    def test_a_crit_on_the_attacker_is_not_the_defenders_crit(self):
        # a `|-crit|` naming the other slot (a Rocky-Helmet-style block that
        # carries someone else's crit) must not select the crit arm here
        damage = get_damage_dealt(
            self.battle,
            "|move|p2a: Pikachu|Tackle|p1a: Caterpie".split("|"),
            ["|-crit|p2a: Pikachu", "|-damage|p1a: Caterpie|204/300"],
        )
        self.assertFalse(damage.crit)

    def test_the_crit_arm_selects_which_roll_set_must_contain_the_delta(self):
        non_crit = list(range(85, 101))
        crit_rolls = list(range(120, 136))
        possibilities = [
            PokemonSet(
                ability="static",
                item="leftovers",
                nature="serious",
                evs=(85, 85, 85, 85, 85, 85),
                count=1,
            )
        ]
        damage_dealt = DamageDealt(
            attacker="pikachu",
            defender="caterpie",
            move="tackle",
            percent_damage=0.42,
            crit=True,  # stamped by item 1
            exact_damage=127,
        )
        with mock.patch(
            "fp.inference.poke_engine_get_damage_roll_sets",
            return_value=(None, (non_crit, crit_rolls)),
        ):
            _do_check(
                self.battle,
                self.battle,
                possibilities,
                "damage_dealt",
                damage_dealt,
                bot_went_first=True,
                check_lower_bound=True,
            )
        # 127 is in the CRIT set only: reading the non-crit arm would have
        # eliminated the true set
        self.assertEqual(1, len(possibilities))
        self.assertFalse(DAMAGE_CHECK_REFUSALS)


# ---------------------------------------------------------------------------
# Item 3: chip-damage max-HP sieve
#
# Each row is a real protocol line from the holdout corpora
# (/Users/sallyliu/pokemon-ai/synthetic-corpus-holdout*), paired with the
# percent that pokemon was showing immediately before it and the TRUE max HP
# its game's `.teams.json` sidecar states.
# ---------------------------------------------------------------------------
_CORPUS_RESIDUALS = [
    # (species, before, after, true_max_hp, amount_of_max, healing, source line)
    ("cresselia", 59, 65, 323, lambda m: m // 16, True,
     "|-heal|p2a: Cresselia|65/100|[from] item: Leftovers"),
    ("tinglu", 53, 60, 370, lambda m: m // 16, True,
     "|-heal|p2a: Ting-Lu|60/100|[from] item: Leftovers"),
    ("tinglu", 9, 15, 370, lambda m: m // 16, True,
     "|-heal|p2a: Ting-Lu|15/100|[from] item: Leftovers"),
    ("cresselia", 61, 55, 323, lambda m: m // 16, False,
     "|-damage|p2a: Cresselia|55/100 brn|[from] brn"),
    ("cresselia", 99, 93, 323, lambda m: m // 16, False,
     "|-damage|p2a: Cresselia|93/100 brn|[from] brn"),
    ("leafeon", 45, 36, 258, lambda m: m // 10, False,
     "|-damage|p2a: Leafeon|36/100|[from] item: Life Orb"),
    ("mimikyu", 75, 66, 216, lambda m: m // 10, False,
     "|-damage|p2a: Mimikyu|66/100 psn|[from] item: Life Orb"),
    ("mimikyu", 88, 75, 216, lambda m: m // 8, False,
     "|-damage|p2a: Mimikyu|75/100 psn|[from] psn"),
    ("mimikyu", 53, 41, 216, lambda m: m // 8, False,
     "|-damage|p2a: Mimikyu|41/100 psn|[from] psn"),
    ("krookodile", 68, 62, 280, lambda m: m // 16, False,
     "|-damage|p2a: Krookodile|62/100|[from] Sandstorm"),
    ("blissey", 100, 94, 572, lambda m: m // 16, False,
     "|-damage|p2a: Blissey|94/100|[from] Sandstorm"),
    ("golduck", 100, 88, 290, lambda m: m // 8, False,
     "|-damage|p2a: Golduck|88/100|[from] Stealth Rock"),
    ("amoonguss", 100, 88, 321, lambda m: m // 8, False,
     "|-damage|p2a: Amoonguss|88/100|[from] Stealth Rock"),
    ("scizor", 30, 17, 240, lambda m: m // 8, False,
     "|-damage|p2a: Scizor|17/100|[from] Stealth Rock"),
    # toxic: data/statuses.ts increments the stage BEFORE the damage, so these
    # three consecutive ticks are stages 1, 2 and 3
    ("zangoose", 79, 73, 263, lambda m: max(1, m // 16) * 1, False,
     "|-damage|p2a: Zangoose|73/100 tox|[from] psn"),
    ("zangoose", 73, 61, 263, lambda m: max(1, m // 16) * 2, False,
     "|-damage|p2a: Zangoose|61/100 tox|[from] psn"),
    ("zangoose", 61, 43, 263, lambda m: max(1, m // 16) * 3, False,
     "|-damage|p2a: Zangoose|43/100 tox|[from] psn"),
]


class TestChipDamageMaxHpSieve(unittest.TestCase):
    def test_the_true_max_hp_is_admitted_by_every_real_corpus_transition(self):
        # the one property that must NEVER fail: a sieve that rejects the true
        # max HP eliminates the true candidate set
        for species, before, after, true_max, amount, healing, line in (
            _CORPUS_RESIDUALS
        ):
            with self.subTest(line=line):
                self.assertTrue(
                    max_hp_consistent_with_residual(
                        true_max, before, after, amount, healing
                    )
                )

    def test_the_sieve_actually_rejects_wrong_max_hps(self):
        # ...and it is not vacuous: the same transitions rule values out
        rejected = 0
        for species, before, after, true_max, amount, healing, line in (
            _CORPUS_RESIDUALS
        ):
            for candidate in range(true_max - 40, true_max + 41):
                if candidate <= 0 or candidate == true_max:
                    continue
                if not max_hp_consistent_with_residual(
                    candidate, before, after, amount, healing
                ):
                    rejected += 1
        self.assertGreater(rejected, 0)

    def test_one_tick_rules_out_part_of_the_range_and_several_rule_out_more(self):
        # a single observation is a weak-but-real filter; the channel's value is
        # the INTERSECTION across a game, which is why each observation is
        # applied exactly rather than folded into a lossy [lo, hi] hull.
        def admitted(before, after, amount, healing):
            return {
                m
                for m in range(1, 1001)
                if max_hp_consistent_with_residual(m, before, after, amount, healing)
            }

        leftovers = lambda x: x // 16
        one = admitted(9, 15, leftovers, True)
        self.assertIn(370, one)
        self.assertLess(len(one), 1000)
        both = one & admitted(53, 60, leftovers, True)
        self.assertIn(370, both)  # Ting-Lu's true max HP survives both ticks
        self.assertLess(len(both), len(one))

    def test_a_residual_line_sieves_the_candidate_sets(self):
        battle = _battle()
        opponent = battle.opponent.active
        opponent.hp_display_pct = 59
        opponent.max_hp_exact = False

        fat = _predicted("leftovers", level=100, hp_evs=252)  # max hp 274
        thin = _predicted("leftovers", level=20, hp_evs=252)  # max hp 62
        with mock.patch(
            "fp.inference.RandomBattleTeamDatasets.get_pkmn_sets_from_pkmn_name",
            return_value=[fat, thin],
        ):
            heal_or_damage(
                battle,
                "|-heal|p2a: Pikachu|65/100|[from] item: Leftovers".split("|"),
            )
        # a max-62 Pikachu cannot go 59/100 -> 65/100 on a max//16 tick
        self.assertIn(thin.mechanics_signature(), opponent.rejected_set_signatures)
        self.assertNotIn(fat.mechanics_signature(), opponent.rejected_set_signatures)

    def test_a_would_empty_sieve_is_refused_rather_than_applied(self):
        reset_damage_check_refusals()
        battle = _battle()
        opponent = battle.opponent.active
        only = _predicted("leftovers", level=100, hp_evs=252)
        with mock.patch(
            "fp.inference.RandomBattleTeamDatasets.get_pkmn_sets_from_pkmn_name",
            return_value=[only],
        ):
            sieve_sets_by_max_hp(battle, opponent, lambda m: False, "impossible")
        self.assertFalse(opponent.rejected_set_signatures)
        self.assertTrue(DAMAGE_CHECK_REFUSALS)

    def test_the_replay_checker_is_never_sieved(self):
        battle = _battle()
        battle.exact_roster_known = True
        opponent = battle.opponent.active
        only = _predicted("leftovers", level=100, hp_evs=252)
        with mock.patch(
            "fp.inference.RandomBattleTeamDatasets.get_pkmn_sets_from_pkmn_name",
            return_value=[only, _predicted("lifeorb", level=5, hp_evs=0)],
        ):
            sieve_sets_by_max_hp(battle, opponent, lambda m: m > 10_000, "gated")
        self.assertFalse(opponent.rejected_set_signatures)


def _predicted(item, level, hp_evs):
    return PredictedPokemonSet(
        pkmn_set=PokemonSet(
            ability="static",
            item=item,
            nature="serious",
            evs=(hp_evs, 85, 85, 85, 85, 85),
            count=1,
            level=level,
        ),
        pkmn_moveset=PokemonMoveset(moves=("tackle", "thunderbolt")),
    )


# ---------------------------------------------------------------------------
# Item 4: absolute-HP display sieve
# ---------------------------------------------------------------------------
class TestAbsoluteHpDisplaySieve(unittest.TestCase):
    def test_an_absolute_hp_and_its_percent_pin_max_hp(self):
        # PS getHealth: ceil(100*hp/maxhp), forced to 99 below full
        # (sim/pokemon.ts:2079-2086)
        hp = 186
        admitted = [m for m in range(hp, 1001) if
                    max_hp_consistent_with_absolute(m, hp, 69)]
        self.assertTrue(admitted)
        for m in admitted:
            self.assertEqual(69, display_pct(hp, m))
        self.assertNotIn(265, admitted)  # 186/265 displays as 71, not 69

    def test_hp_above_max_is_never_admissible(self):
        self.assertFalse(max_hp_consistent_with_absolute(100, 150, 99))

    def test_a_max_relative_identity_states_nothing(self):
        # `hp == max_hp` shows 100/100 for EVERY max, so it must not sieve
        for m in (100, 250, 400):
            self.assertTrue(max_hp_consistent_with_absolute(m, m, 100))

    def test_an_endeavor_certificate_sieves_the_candidate_sets(self):
        battle = _battle()
        opponent = battle.opponent.active
        opponent.max_hp_exact = False
        # a certificate filed by this line: hp 186 shown as 69/100
        opponent.hp_certificate_hp = 186
        opponent.hp_certificate_pct = 69
        opponent.hp_certificate_fraction = None

        good = _predicted("leftovers", level=100, hp_evs=252)
        bad = _predicted("lifeorb", level=100, hp_evs=0)
        captured = {}

        def _fake_sieve(battle_, pkmn, predicate, reason):
            captured["kept"] = [
                s for s in (good, bad) if predicate(_max_hp_of(pkmn, s))
            ]

        with mock.patch("fp.battle_modifier.sieve_sets_by_max_hp", _fake_sieve):
            import fp.battle_modifier as bm

            bm._sieve_max_hp_from_absolute_certificate(battle, opponent, (None, None))
        self.assertIn("kept", captured)
        for kept in captured["kept"]:
            self.assertEqual(69, display_pct(186, _max_hp_of(opponent, kept)))

    def test_an_unchanged_certificate_is_not_re_sieved(self):
        battle = _battle()
        opponent = battle.opponent.active
        opponent.hp_certificate_hp = 186
        opponent.hp_certificate_pct = 69
        opponent.hp_certificate_fraction = None
        with mock.patch("fp.battle_modifier.sieve_sets_by_max_hp") as sieve:
            import fp.battle_modifier as bm

            bm._sieve_max_hp_from_absolute_certificate(battle, opponent, (186, 69))
        sieve.assert_not_called()


def _max_hp_of(pkmn, predicted_set):
    from fp.inference import _candidate_set_max_hp

    return _candidate_set_max_hp(pkmn, predicted_set)


# ---------------------------------------------------------------------------
# Item 5: stratified (largest-remainder) world allocation
# ---------------------------------------------------------------------------
class TestStratifiedWorldAllocation(unittest.TestCase):
    def test_exact_allocation_for_a_fixed_posterior(self):
        # 0.5 / 0.3 / 0.2 over 10 worlds is exact
        self.assertEqual([5, 3, 2], largest_remainder_allocation([50, 30, 20], 10))

    def test_remainders_go_to_the_largest_fractions(self):
        # shares 3.33 / 3.33 / 3.33 -> floors 3/3/3, one seat left, ties by index
        self.assertEqual([4, 3, 3], largest_remainder_allocation([1, 1, 1], 10))
        # shares 4.5 / 3.0 / 2.5 -> floors 4/3/2, one seat to the .5 with the
        # lower index
        self.assertEqual([5, 3, 2], largest_remainder_allocation([9, 6, 5], 10))

    def test_every_set_above_one_over_2n_is_represented(self):
        # the whole point: an independent per-world draw gives a 1/(2n) set only
        # a ~39% chance of appearing at all
        weights = [1] * 3 + [97]
        allocation = largest_remainder_allocation(weights, 100)
        self.assertEqual(100, sum(allocation))
        for count in allocation:
            self.assertGreaterEqual(count, 1)

    def test_allocation_totals_the_requested_number_of_worlds(self):
        for n in (1, 2, 3, 7, 16, 100):
            for weights in ([1, 2, 3], [5], [0, 0, 1], [7, 7, 7, 7, 7]):
                with self.subTest(n=n, weights=weights):
                    self.assertEqual(n, sum(largest_remainder_allocation(weights, n)))

    def test_zero_weight_sets_get_no_worlds(self):
        self.assertEqual([0, 10, 0], largest_remainder_allocation([0, 5, 0], 10))

    def test_sample_chances_sum_to_one(self):
        sets = ["a", "b", "c"]
        weights = [50, 30, 20]
        plan = _stratified_active_plan(sets, weights, 16)
        self.assertEqual(16, len(plan))
        self.assertAlmostEqual(1.0, sum(chance for _, chance in plan))

    def test_sample_chances_sum_to_one_when_sets_outnumber_worlds(self):
        # more candidate sets than worlds: the sets that get no seat must not
        # take their posterior mass out of the batch, or `pooled_share` in
        # fp/search/selection.py is deflated against its ABSOLUTE thresholds
        sets = ["s{}".format(i) for i in range(22)]
        weights = [22 - i for i in range(22)]
        for n in (1, 2, 4, 8, 16, 32):
            with self.subTest(n=n):
                plan = _stratified_active_plan(sets, weights, n)
                self.assertEqual(n, len(plan))
                self.assertAlmostEqual(1.0, sum(c for _, c in plan))

    def test_total_chance_behind_a_set_is_its_posterior(self):
        sets = ["a", "b", "c"]
        weights = [50, 30, 20]
        plan = _stratified_active_plan(sets, weights, 7)
        for name, weight in zip(sets, weights):
            total = sum(c for s, c in plan if s == name)
            self.assertAlmostEqual(weight / sum(weights), total)

    def test_the_allocation_is_deterministic(self):
        sets = ["a", "b", "c", "d"]
        weights = [11, 7, 5, 3]
        first = _stratified_active_plan(sets, weights, 13)
        for _ in range(5):
            self.assertEqual(first, _stratified_active_plan(sets, weights, 13))


if __name__ == "__main__":
    unittest.main()
