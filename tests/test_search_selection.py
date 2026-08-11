import logging
import unittest
from concurrent.futures.process import BrokenProcessPool
from types import SimpleNamespace

from config import FoulPlayConfig
from fp.search.main import (
    _discard_search_executor,
    _get_search_executor,
    dedupe_states,
    gather_mcts_results,
    search_time_num_battles_randombattles,
    search_time_num_battles_standard_battle,
    select_move_from_mcts_results,
)


def mcts_result(options):
    side_one = [
        SimpleNamespace(move_choice=name, visits=visits, total_score=visits * 0.5)
        for name, visits in options
    ]
    return SimpleNamespace(side_one=side_one, total_visits=sum(v for _, v in options))


class TestSwitchWeightMultiplier(unittest.TestCase):
    def setUp(self):
        self.original = FoulPlayConfig.switch_weight_multiplier

    def tearDown(self):
        FoulPlayConfig.switch_weight_multiplier = self.original

    def test_multiplier_flips_marginal_switch_to_attack(self):
        # REWORKED for the score-dominance gate: the old equal-score stub let
        # the gate (not the multiplier) reject the switch, masking what this
        # test pins. The switch is now score-dominant (0.52 > 0.50) so it is
        # gate-ELIGIBLE and narrowly wins undamped; 0.8 damping flips it.
        FoulPlayConfig.switch_weight_multiplier = 0.8
        results = [
            (
                mcts_result_scored(
                    [("switch landorustherian", 40, 0.52), ("moonblast", 36, 0.50)]
                ),
                1.0,
                0,
            )
        ]
        self.assertEqual("moonblast", select_move_from_mcts_results(results))

    def test_multiplier_disabled_keeps_equal_score_switch(self):
        # with damping off, an equal-score (0.5 vs 0.5) share-leading switch
        # is within the gate tolerance, stays eligible, and wins on share
        FoulPlayConfig.switch_weight_multiplier = 1.0
        results = [
            (mcts_result([("switch landorustherian", 40), ("moonblast", 36)]), 1.0, 0)
        ]
        self.assertEqual(
            "switch landorustherian", select_move_from_mcts_results(results)
        )

    def test_all_switch_turns_are_untouched(self):
        # forced-switch style turn: every option is a switch; damping must not
        # apply (and the score-dominance gate is skipped: no plain moves)
        FoulPlayConfig.switch_weight_multiplier = 0.8
        results = [(mcts_result([("switch a", 40), ("switch b", 36)]), 1.0, 0)]
        self.assertEqual("switch a", select_move_from_mcts_results(results))

    def test_clearly_best_switch_survives_damping(self):
        # REWORKED for the score-dominance gate: with the old equal-score stub
        # the gate would now disallow the switch outright. The switch is made
        # score-dominant (0.6 > 0.5) so this still pins the original intent:
        # a clearly better switch survives 0.8 damping (and the gate).
        FoulPlayConfig.switch_weight_multiplier = 0.8
        results = [
            (
                mcts_result_scored(
                    [("switch corviknight", 80, 0.6), ("tackle", 10, 0.5)]
                ),
                1.0,
                0,
            )
        ]
        self.assertEqual("switch corviknight", select_move_from_mcts_results(results))


def mcts_result_scored(options):
    """Like mcts_result, but each option is (name, visits, avg_score)."""
    side_one = [
        SimpleNamespace(move_choice=name, visits=visits, total_score=visits * avg_score)
        for name, visits, avg_score in options
    ]
    return SimpleNamespace(
        side_one=side_one, total_visits=sum(v for _, v, _ in options)
    )


class TestScoreBlendedSelection(unittest.TestCase):
    _ATTRS = ("losing_attack_fallback_threshold", "switch_weight_multiplier")

    def setUp(self):
        self.originals = {attr: getattr(FoulPlayConfig, attr) for attr in self._ATTRS}
        # neutralize damping and the losing fallback: these tests isolate the
        # share x score blending itself
        FoulPlayConfig.losing_attack_fallback_threshold = 0.0
        FoulPlayConfig.switch_weight_multiplier = 1.0

    def tearDown(self):
        for attr, value in self.originals.items():
            setattr(FoulPlayConfig, attr, value)

    def test_higher_score_lower_share_attack_beats_switch(self):
        # the T8 pattern: the switch narrowly leads the pooled share vote
        # (.38 vs .33) but every scoring lens ranks the attack higher
        # (.375 vs .28); blending share with score elects the attack
        results = [
            (
                mcts_result_scored(
                    [
                        ("thunderbolt", 33, 0.375),
                        ("switch gholdengo", 38, 0.28),
                        ("voltswitch", 29, 0.20),
                    ]
                ),
                1.0,
                0,
            )
        ]
        self.assertEqual("thunderbolt", select_move_from_mcts_results(results))

    def test_equal_scores_fall_back_to_share_ordering(self):
        # when scores tie, blending is monotone in share: share argmax wins
        results = [
            (
                mcts_result_scored([("tackle", 40, 0.5), ("icebeam", 36, 0.5)]),
                1.0,
                0,
            )
        ]
        self.assertEqual("tackle", select_move_from_mcts_results(results))


class TestScoreDominanceGate(unittest.TestCase):
    """Switch/tera/mega options need an agg score within
    FoulPlayConfig.switch_gate_tolerance of the best visited plain move
    (or better) to be eligible for selection."""

    _ATTRS = (
        "losing_attack_fallback_threshold",
        "switch_weight_multiplier",
        "switch_gate_tolerance",
    )

    def setUp(self):
        self.originals = {attr: getattr(FoulPlayConfig, attr) for attr in self._ATTRS}
        # neutralize damping and the losing fallback unless a test opts in
        FoulPlayConfig.losing_attack_fallback_threshold = 0.0
        FoulPlayConfig.switch_weight_multiplier = 1.0
        FoulPlayConfig.switch_gate_tolerance = 0.04
        # tests/__init__.py globally disables logging, which suppresses
        # records even inside assertLogs; lift it for the log assertions here
        self._previous_logging_disable = logging.root.manager.disable
        logging.disable(logging.NOTSET)

    def tearDown(self):
        logging.disable(self._previous_logging_disable)
        for attr, value in self.originals.items():
            setattr(FoulPlayConfig, attr, value)

    def test_t14_share_leading_switch_gated_to_attack(self):
        # the T14 pattern: the switch leads on share and wins the raw blended
        # vote (0.6 x 0.48 = 0.288 vs 0.4 x 0.645 = 0.258) but scores worse
        # than the plain attack, so the gate disallows it and logs why
        results = [
            (
                mcts_result_scored(
                    [("switch blissey", 60, 0.48), ("closecombat", 40, 0.645)]
                ),
                1.0,
                0,
            )
        ]
        with self.assertLogs("fp.search.main", level="INFO") as logs:
            choice = select_move_from_mcts_results(results)
        self.assertEqual("closecombat", choice)
        self.assertTrue(
            any(
                "Score-gate: disallowed switch blissey" in line for line in logs.output
            ),
            logs.output,
        )

    def test_score_dominant_switch_allowed_and_chosen(self):
        # the switch strictly outscores the best plain move (0.6 > 0.5):
        # it is eligible and wins the blended vote, and no gate log fires
        results = [
            (
                mcts_result_scored([("switch blissey", 50, 0.6), ("tackle", 50, 0.5)]),
                1.0,
                0,
            )
        ]
        with self.assertLogs("fp.search.main", level="INFO") as logs:
            choice = select_move_from_mcts_results(results)
        self.assertEqual("switch blissey", choice)
        self.assertFalse(any("Score-gate" in line for line in logs.output))

    def test_tera_option_gated_identically(self):
        # a '-tera' option is restricted just like a switch: 0.45 is more
        # than the 0.04 tolerance below the plain move's 0.5, so the
        # share-leading tera variant is disallowed and the plain move played
        results = [
            (
                mcts_result_scored(
                    [("gigadrain-tera", 60, 0.45), ("gigadrain", 40, 0.5)]
                ),
                1.0,
                0,
            )
        ]
        self.assertEqual("gigadrain", select_move_from_mcts_results(results))

    def test_mega_option_gated_identically(self):
        # '-mega' is treated exactly like '-tera'
        results = [
            (
                mcts_result_scored(
                    [("gigadrain-mega", 60, 0.45), ("gigadrain", 40, 0.5)]
                ),
                1.0,
                0,
            )
        ]
        self.assertEqual("gigadrain", select_move_from_mcts_results(results))

    def test_switch_within_tolerance_allowed(self):
        # 0.47 is within the 0.04 tolerance of the plain move's 0.5: the
        # share-leading switch is eligible and wins, and no gate log fires
        results = [
            (
                mcts_result_scored(
                    [("switch blissey", 60, 0.47), ("closecombat", 40, 0.5)]
                ),
                1.0,
                0,
            )
        ]
        with self.assertLogs("fp.search.main", level="INFO") as logs:
            choice = select_move_from_mcts_results(results)
        self.assertEqual("switch blissey", choice)
        self.assertFalse(any("Score-gate" in line for line in logs.output))

    def test_zero_tolerance_restores_strict_dominance(self):
        # with the tolerance at 0 an equal-score restricted option is gated
        FoulPlayConfig.switch_gate_tolerance = 0.0
        results = [
            (
                mcts_result_scored(
                    [("gigadrain-tera", 60, 0.5), ("gigadrain", 40, 0.5)]
                ),
                1.0,
                0,
            )
        ]
        self.assertEqual("gigadrain", select_move_from_mcts_results(results))

    def test_forced_all_switch_turn_ungated(self):
        # no visited plain move exists (forced switch): the gate is skipped,
        # every option is eligible, and the blended argmax switch is played
        results = [
            (
                mcts_result_scored([("switch a", 60, 0.5), ("switch b", 40, 0.6)]),
                1.0,
                0,
            )
        ]
        with self.assertLogs("fp.search.main", level="INFO") as logs:
            choice = select_move_from_mcts_results(results)
        self.assertEqual("switch a", choice)
        self.assertFalse(any("Score-gate" in line for line in logs.output))

    def test_fallback_still_fires_after_gating(self):
        # a score-dominant switch (0.09 > 0.08) survives the gate and wins
        # the blended vote, but its score is below the 0.1 fallback
        # threshold: the losing-attack fallback still runs on the gated
        # winner and picks the best-scoring attack with >=5% share
        FoulPlayConfig.losing_attack_fallback_threshold = 0.1
        results = [
            (
                mcts_result_scored(
                    [
                        ("switch a", 50, 0.09),
                        ("tackle", 30, 0.05),
                        ("icebeam", 20, 0.08),
                    ]
                ),
                1.0,
                0,
            )
        ]
        self.assertEqual("icebeam", select_move_from_mcts_results(results))


class TestLosingAttackFallback(unittest.TestCase):
    _ATTRS = ("losing_attack_fallback_threshold", "switch_weight_multiplier")

    def setUp(self):
        self.originals = {attr: getattr(FoulPlayConfig, attr) for attr in self._ATTRS}
        FoulPlayConfig.losing_attack_fallback_threshold = 0.35
        FoulPlayConfig.switch_weight_multiplier = 1.0

    def tearDown(self):
        for attr, value in self.originals.items():
            setattr(FoulPlayConfig, attr, value)

    def test_losing_position_prefers_best_scoring_attack(self):
        # everything is hopeless (~0.1) and the switch leads the share vote:
        # the fallback should pick the best-scoring non-switch instead
        # (the score gate also disallows the 0.12 switch vs icebeam's 0.15,
        # but the pinned outcome is unchanged)
        results = [
            (
                mcts_result_scored(
                    [
                        ("switch corviknight", 60, 0.12),
                        ("surf", 30, 0.10),
                        ("icebeam", 10, 0.15),
                    ]
                ),
                1.0,
                0,
            )
        ]
        self.assertEqual("icebeam", select_move_from_mcts_results(results))

    def test_healthy_scores_keep_share_argmax(self):
        # REWORKED for the score-dominance gate: the old stub (switch 0.55 vs
        # surf 0.70) had the switch scoring BELOW the attack, which the gate
        # now disallows outright. The switch is made score-dominant
        # (0.75 > 0.70) so it stays eligible; it scores above the threshold,
        # so no fallback fires and the blended argmax switch is kept.
        results = [
            (
                mcts_result_scored(
                    [("switch corviknight", 60, 0.75), ("surf", 40, 0.70)]
                ),
                1.0,
                0,
            )
        ]
        self.assertEqual("switch corviknight", select_move_from_mcts_results(results))

    def test_forced_all_switch_turn_unchanged(self):
        # forced switch: every option is a switch, fallback cannot apply and
        # the blended argmax (share x score) is played even though it scores
        # below the fallback threshold
        results = [
            (
                mcts_result_scored([("switch a", 60, 0.10), ("switch b", 40, 0.05)]),
                1.0,
                0,
            )
        ]
        self.assertEqual("switch a", select_move_from_mcts_results(results))

    def test_low_visit_attack_excluded_falls_back_to_next(self):
        # REWORKED for the score-dominance gate: prayer's old 0.90 score made
        # it the gated blended argmax (0.04 x 0.90 beat tackle's 0.30 x 0.08),
        # sidestepping the fallback entirely. Prayer now scores 0.50 - still
        # the best score, but its <5% pooled share keeps it out of the
        # fallback's eligible set, which is what this test pins. The gated
        # winner tackle (switch is gate-disallowed: 0.10 <= 0.50) trips the
        # threshold and the fallback lands on the only eligible attack.
        results = [
            (
                mcts_result_scored(
                    [
                        ("switch a", 66, 0.10),
                        ("tackle", 30, 0.08),
                        ("prayer", 4, 0.50),
                    ]
                ),
                1.0,
                0,
            )
        ]
        self.assertEqual("tackle", select_move_from_mcts_results(results))

    def test_only_low_visit_attack_leaves_choice_unchanged(self):
        # REWORKED for the score-dominance gate: with the old stub (switch
        # 0.10 vs prayer 0.90) the gate would disallow the switch and elect
        # prayer. The switch is now score-dominant (0.30 > 0.25) so it stays
        # the gated winner; it scores below the 0.35 threshold, but the sole
        # non-switch is under the 5% share floor, so no fallback fires.
        results = [
            (
                mcts_result_scored([("switch a", 96, 0.30), ("prayer", 4, 0.25)]),
                1.0,
                0,
            )
        ]
        self.assertEqual("switch a", select_move_from_mcts_results(results))

    def test_fallback_fires_on_blended_argmax_score(self):
        # the share argmax is the switch (0.5) but the BLENDED argmax is
        # tackle (0.3 x 0.20 = 0.06 beats 0.5 x 0.10 = 0.05); the score gate
        # also removes the switch (0.10 <= 0.25). The fallback threshold
        # check runs against tackle's agg score (0.20 < 0.35), so it fires
        # and lands on the best-scoring eligible attack
        results = [
            (
                mcts_result_scored(
                    [
                        ("switch corviknight", 50, 0.10),
                        ("tackle", 30, 0.20),
                        ("icebeam", 20, 0.25),
                    ]
                ),
                1.0,
                0,
            )
        ]
        self.assertEqual("icebeam", select_move_from_mcts_results(results))

    def test_threshold_zero_disables_fallback(self):
        # REWORKED for the score-dominance gate: the old 0.12 switch score
        # lost to icebeam's 0.15, so the gate would disallow it and the test
        # would no longer isolate the threshold. The switch now scores 0.16
        # (> 0.15, gate-eligible); with the threshold at 0 no fallback fires
        # even in this losing position, and the blended argmax switch stays.
        FoulPlayConfig.losing_attack_fallback_threshold = 0.0
        results = [
            (
                mcts_result_scored(
                    [
                        ("switch corviknight", 60, 0.16),
                        ("surf", 30, 0.10),
                        ("icebeam", 10, 0.15),
                    ]
                ),
                1.0,
                0,
            )
        ]
        self.assertEqual("switch corviknight", select_move_from_mcts_results(results))


def battle_stub(
    *,
    turn=False,
    team_preview=False,
    reserve_count=0,
    active_moves=0,
    active_hp=100,
    time_remaining=None,
    decisions_made=None,
):
    # production battles always carry `decisions_made`: Battle.__init__ seeds 0
    # and async_pick_move increments it on the REAL battle before every
    # find_best_move call (fp/run_battle.py), so during a search it is >= 1.
    # decisions_made=None models a legacy battle object without the counter,
    # exercising the turn-based fallback branch of is_first_decision.
    active = SimpleNamespace(moves=["tackle"] * active_moves, hp=active_hp)
    opponent = SimpleNamespace(
        active=active, reserve=[SimpleNamespace() for _ in range(reserve_count)]
    )
    return SimpleNamespace(
        turn=turn,
        team_preview=team_preview,
        opponent=opponent,
        time_remaining=time_remaining,
        decisions_made=decisions_made,
    )


class TestFirstTurnSearchTime(unittest.TestCase):
    _MISSING = object()
    _ATTRS = ("parallelism", "search_time_ms", "first_turn_search_time_ms")

    def setUp(self):
        self.originals = {
            attr: getattr(FoulPlayConfig, attr, self._MISSING) for attr in self._ATTRS
        }
        FoulPlayConfig.parallelism = 1
        FoulPlayConfig.search_time_ms = 7500
        FoulPlayConfig.first_turn_search_time_ms = 20000

    def tearDown(self):
        for attr, value in self.originals.items():
            if value is self._MISSING:
                delattr(FoulPlayConfig, attr)
            else:
                setattr(FoulPlayConfig, attr, value)

    def test_randombattles_first_decision_uses_first_turn_time(self):
        # first decision, nothing revealed: the early-shallow branch divides
        # the base time by the wave count (num_battles_multiplier) so wall
        # time matches the configured budget. decisions_made=1 is the
        # production path: the counter was just incremented by
        # async_pick_move for this very decision
        battle = battle_stub(turn=1, decisions_made=1)
        num_battles, search_time = search_time_num_battles_randombattles(battle)
        self.assertEqual(4, num_battles)
        self.assertEqual(5000, search_time)

    def test_randombattles_later_decision_uses_search_time(self):
        battle = battle_stub(turn=5, decisions_made=6)
        num_battles, search_time = search_time_num_battles_randombattles(battle)
        # wide x4 sampling is first-decision-only now: later decisions use x2
        self.assertEqual(2, num_battles)
        self.assertEqual(7500, search_time)  # full time in the x2 branch

    def test_randombattles_first_decision_deep_branch(self):
        # opponent revealed moves: the non-shallow branch uses the full base time
        battle = battle_stub(turn=1, active_moves=2, decisions_made=1)
        _, search_time = search_time_num_battles_randombattles(battle)
        self.assertEqual(20000, search_time)

    def test_randombattles_later_decision_deep_branch(self):
        battle = battle_stub(turn=12, active_moves=2, decisions_made=14)
        _, search_time = search_time_num_battles_randombattles(battle)
        self.assertEqual(7500, search_time)

    def test_randombattles_turn1_second_decision_is_not_first(self):
        # a turn-1 pivot (u-turn) creates a SECOND decision inside turn 1;
        # the counter (not the turn number) must deny the extended search
        battle = battle_stub(turn=1, active_moves=2, decisions_made=2)
        _, search_time = search_time_num_battles_randombattles(battle)
        self.assertEqual(7500, search_time)

    def test_randombattles_legacy_battle_without_counter_uses_turn_fallback(self):
        # a battle object with no usable counter (e.g. an old pickle) falls
        # back to the turn-number heuristic
        battle = battle_stub(turn=1, active_moves=2, decisions_made=None)
        _, search_time = search_time_num_battles_randombattles(battle)
        self.assertEqual(20000, search_time)

    def test_randombattles_unset_first_turn_time_falls_back(self):
        FoulPlayConfig.first_turn_search_time_ms = None
        battle = battle_stub(turn=1, active_moves=2, decisions_made=1)
        _, search_time = search_time_num_battles_randombattles(battle)
        self.assertEqual(7500, search_time)

    def test_standard_team_preview_uses_first_turn_time(self):
        # team preview happens before |turn|1, so battle.turn is still False;
        # the team-preview pick is the battle's first decision
        battle = battle_stub(turn=False, team_preview=True, decisions_made=1)
        num_battles, search_time = search_time_num_battles_standard_battle(battle)
        self.assertEqual(2, num_battles)
        self.assertEqual(20000, search_time)

    def test_standard_first_move_decision_uses_first_turn_time(self):
        battle = battle_stub(turn=1, active_moves=4, decisions_made=1)
        num_battles, search_time = search_time_num_battles_standard_battle(battle)
        self.assertEqual(1, num_battles)
        self.assertEqual(20000, search_time)

    def test_standard_turn1_move_after_team_preview_is_not_first(self):
        # formats with team preview: the turn-1 move is decision #2 and must
        # NOT get extended time even though battle.turn is 1
        battle = battle_stub(turn=1, active_moves=4, decisions_made=2)
        _, search_time = search_time_num_battles_standard_battle(battle)
        self.assertEqual(7500, search_time)

    def test_standard_later_decision_uses_search_time(self):
        battle = battle_stub(turn=7, active_moves=4, decisions_made=9)
        num_battles, search_time = search_time_num_battles_standard_battle(battle)
        self.assertEqual(1, num_battles)
        self.assertEqual(7500, search_time)

    def test_standard_later_decision_few_moves_branch(self):
        battle = battle_stub(turn=7, active_moves=1, decisions_made=9)
        num_battles, search_time = search_time_num_battles_standard_battle(battle)
        self.assertEqual(2, num_battles)
        self.assertEqual(7500, search_time)


def mcts_result_iwsd(options):
    """options: list of (name, visits, avg_score, iwsd) for ONE world."""
    side_one = []
    for name, visits, avg, iwsd in options:
        total_score = visits * avg
        total_score_sq = visits * (iwsd * iwsd + avg * avg)
        side_one.append(
            SimpleNamespace(
                move_choice=name,
                visits=visits,
                total_score=total_score,
                total_score_sq=total_score_sq,
            )
        )
    return SimpleNamespace(
        side_one=side_one, total_visits=sum(o.visits for o in side_one)
    )


class TestTeraMarginGate(unittest.TestCase):
    """Tera/mega spends a once-per-battle resource: beyond the leniency gate
    it must BEAT the best non-tera alternative by tera_margin_gate. The T1
    tera-Fire Flare Blitz (esmolmightbetaken loss) won the argmax by 0.006 and
    the spent tera turned out to be the endgame answer to the Leafeon sweep."""

    def setUp(self):
        self.original = FoulPlayConfig.tera_margin_gate

    def tearDown(self):
        FoulPlayConfig.tera_margin_gate = self.original

    def test_tie_break_level_tera_edge_is_gated(self):
        # tera edges the plain move by 0.006 (< 0.025 margin): gated, plain played
        FoulPlayConfig.tera_margin_gate = 0.025
        results = [
            (
                mcts_result_scored(
                    [("flareblitz-tera", 55, 0.543), ("flareblitz", 45, 0.537)]
                ),
                1.0,
                0,
            )
        ]
        self.assertEqual("flareblitz", select_move_from_mcts_results(results))

    def test_clearly_better_tera_clears_the_margin(self):
        # tera beats the best non-tera by 0.05 (> 0.025): allowed and chosen
        FoulPlayConfig.tera_margin_gate = 0.025
        results = [
            (
                mcts_result_scored(
                    [("flareblitz-tera", 55, 0.59), ("flareblitz", 45, 0.54)]
                ),
                1.0,
                0,
            )
        ]
        self.assertEqual("flareblitz-tera", select_move_from_mcts_results(results))

    def test_margin_compares_against_best_non_tera_not_same_move(self):
        # the baseline is the BEST non-tera alternative (here the switch at
        # 0.58), not the tera'd move's own plain variant: 0.59 < 0.58+0.025
        FoulPlayConfig.tera_margin_gate = 0.025
        results = [
            (
                mcts_result_scored(
                    [
                        ("flareblitz-tera", 40, 0.59),
                        ("switch golduck", 35, 0.58),
                        ("flareblitz", 25, 0.54),
                    ]
                ),
                1.0,
                0,
            )
        ]
        self.assertEqual("switch golduck", select_move_from_mcts_results(results))

    def test_zero_margin_disables_the_gate(self):
        FoulPlayConfig.tera_margin_gate = 0.0
        results = [
            (
                mcts_result_scored(
                    [("flareblitz-tera", 55, 0.543), ("flareblitz", 45, 0.537)]
                ),
                1.0,
                0,
            )
        ]
        self.assertEqual("flareblitz-tera", select_move_from_mcts_results(results))


class TestVariancePenaltyGuardGate(unittest.TestCase):
    """The negative-regime guard only exists to undo the variance penalty;
    at lambda=0 there is no penalty and the values already ARE the plain
    per-world blend, so the guard (and its log line) must not fire."""

    _ATTRS = (
        "losing_attack_fallback_threshold",
        "switch_weight_multiplier",
        "variance_penalty_lambda",
    )

    def setUp(self):
        self.originals = {attr: getattr(FoulPlayConfig, attr) for attr in self._ATTRS}
        FoulPlayConfig.losing_attack_fallback_threshold = 0.0
        FoulPlayConfig.switch_weight_multiplier = 1.0
        # tests/__init__.py globally disables logging; lift it so assertLogs
        # can observe (or observe the absence of) the guard's log line
        self._previous_logging_disable = logging.root.manager.disable
        logging.disable(logging.NOTSET)

    def tearDown(self):
        logging.disable(self._previous_logging_disable)
        for attr, value in self.originals.items():
            setattr(FoulPlayConfig, attr, value)

    @staticmethod
    def _two_world_results():
        # tackle's score swings across worlds (0.2 vs 0.8, sd 0.3) while
        # watergun's swings identically lower (0.1 vs 0.7): with a big enough
        # lambda both values go negative; at lambda=0 everything is >= 0 but
        # the all-zero-scores edge below still makes min(value) <= 0
        return [
            (mcts_result_scored([("tackle", 60, 0.2), ("watergun", 40, 0.1)]), 0.5, 0),
            (mcts_result_scored([("tackle", 60, 0.8), ("watergun", 40, 0.7)]), 0.5, 1),
        ]

    def test_lambda_zero_never_fires_the_guard(self):
        FoulPlayConfig.variance_penalty_lambda = 0.0
        # all-zero scores: min(option_value) == 0 would have tripped the old
        # ungated guard even though suspending a zero penalty is a no-op
        results = [
            (mcts_result_scored([("tackle", 60, 0.0), ("watergun", 40, 0.0)]), 1.0, 0)
        ]
        with self.assertLogs("fp.search.main", level="INFO") as logs:
            choice = select_move_from_mcts_results(results)
        self.assertEqual("tackle", choice)
        self.assertFalse(
            any("Variance penalty suspended" in line for line in logs.output),
            logs.output,
        )

    def test_lambda_zero_choice_matches_plain_blend(self):
        # behavior parity: gating the guard must not change the lambda=0
        # ranking (the suspended ranking IS the plain blend)
        FoulPlayConfig.variance_penalty_lambda = 0.0
        choice = select_move_from_mcts_results(self._two_world_results())
        self.assertEqual("tackle", choice)

    def test_positive_lambda_still_fires_the_guard(self):
        # lambda=2: tackle value = 0.3 - 2*0.6*0.3 = -0.06 <= 0, so the
        # guard suspends the penalty and ranks by the plain blend
        FoulPlayConfig.variance_penalty_lambda = 2.0
        with self.assertLogs("fp.search.main", level="INFO") as logs:
            choice = select_move_from_mcts_results(self._two_world_results())
        self.assertEqual("tackle", choice)
        self.assertTrue(
            any("Variance penalty suspended" in line for line in logs.output),
            logs.output,
        )


class _FakeFuture:
    def __init__(self, result=None, exc=None):
        self._result = result
        self._exc = exc

    def result(self):
        if self._exc is not None:
            raise self._exc
        return self._result


class TestGatherMctsResults(unittest.TestCase):
    """One dead search worker must not forfeit the whole decision: dead
    worlds are dropped and the sample chances renormalized over survivors."""

    def setUp(self):
        # tests/__init__.py globally disables logging, which suppresses
        # records even inside assertLogs; lift it for the log assertions here
        self._previous_logging_disable = logging.root.manager.disable
        logging.disable(logging.NOTSET)

    def tearDown(self):
        logging.disable(self._previous_logging_disable)

    def test_all_futures_ok_passes_through_unchanged(self):
        futures = [
            (_FakeFuture(result="r0"), 0.6, 0),
            (_FakeFuture(result="r1"), 0.4, 1),
        ]
        results, pool_broken = gather_mcts_results(futures)
        self.assertEqual([("r0", 0.6, 0), ("r1", 0.4, 1)], results)
        self.assertFalse(pool_broken)

    def test_dead_world_is_dropped_and_chances_renormalized(self):
        futures = [
            (_FakeFuture(result="r0"), 0.5, 0),
            (_FakeFuture(exc=BrokenProcessPool("worker died")), 0.3, 1),
            (_FakeFuture(result="r2"), 0.2, 2),
        ]
        with self.assertLogs("fp.search.main", level="ERROR") as logs:
            results, pool_broken = gather_mcts_results(futures)
        self.assertEqual(["r0", "r2"], [r[0] for r in results])
        self.assertEqual([0, 2], [r[2] for r in results])
        # 0.5/0.7 and 0.2/0.7: survivors sum back to 1.0
        self.assertAlmostEqual(0.5 / 0.7, results[0][1])
        self.assertAlmostEqual(0.2 / 0.7, results[1][1])
        self.assertTrue(pool_broken)
        self.assertTrue(any("World 1 died" in line for line in logs.output))

    def test_non_pool_exception_drops_world_without_flagging_pool(self):
        futures = [
            (_FakeFuture(result="r0"), 0.5, 0),
            (_FakeFuture(exc=ValueError("bad state string")), 0.5, 1),
        ]
        with self.assertLogs("fp.search.main", level="ERROR"):
            results, pool_broken = gather_mcts_results(futures)
        self.assertEqual(["r0"], [r[0] for r in results])
        self.assertFalse(pool_broken)

    def test_all_worlds_dead_raises(self):
        futures = [
            (_FakeFuture(exc=BrokenProcessPool("worker died")), 0.5, 0),
            (_FakeFuture(exc=BrokenProcessPool("worker died")), 0.5, 1),
        ]
        with self.assertLogs("fp.search.main", level="ERROR"):
            with self.assertRaises(RuntimeError):
                gather_mcts_results(futures)


class TestSearchExecutorReuse(unittest.TestCase):
    """The search pool is kept alive across decisions and only recreated on
    breakage (or a parallelism change). ProcessPoolExecutor spawns workers
    lazily on first submit, so these lifecycle tests never fork processes."""

    def setUp(self):
        self._original_parallelism = getattr(FoulPlayConfig, "parallelism", None)
        FoulPlayConfig.parallelism = 2
        _discard_search_executor()

    def tearDown(self):
        _discard_search_executor()
        if self._original_parallelism is None:
            if hasattr(FoulPlayConfig, "parallelism"):
                delattr(FoulPlayConfig, "parallelism")
        else:
            FoulPlayConfig.parallelism = self._original_parallelism

    def test_same_pool_is_returned_across_decisions(self):
        first = _get_search_executor()
        second = _get_search_executor()
        self.assertIs(first, second)

    def test_discard_forces_a_fresh_pool(self):
        first = _get_search_executor()
        _discard_search_executor()
        second = _get_search_executor()
        self.assertIsNot(first, second)

    def test_parallelism_change_recreates_the_pool(self):
        first = _get_search_executor()
        FoulPlayConfig.parallelism = 3
        second = _get_search_executor()
        self.assertIsNot(first, second)
        self.assertEqual(3, second._max_workers)


class TestSelectionPipelineOrdering(unittest.TestCase):
    """Interaction-matrix coverage of the full selection pipeline order:
    score-gate -> tera-margin-gate -> losing-fallback.
    (The fallback-after-gate legs are pinned by
    TestScoreDominanceGate.test_fallback_still_fires_after_gating and
    TestLosingAttackFallback.test_fallback_fires_on_blended_argmax_score.)"""

    _ATTRS = (
        "losing_attack_fallback_threshold",
        "switch_weight_multiplier",
        "switch_gate_tolerance",
        "tera_margin_gate",
    )

    def setUp(self):
        self.originals = {attr: getattr(FoulPlayConfig, attr) for attr in self._ATTRS}
        FoulPlayConfig.losing_attack_fallback_threshold = 0.0
        FoulPlayConfig.switch_weight_multiplier = 1.0
        FoulPlayConfig.switch_gate_tolerance = 0.04
        FoulPlayConfig.tera_margin_gate = 0.025

    def tearDown(self):
        for attr, value in self.originals.items():
            setattr(FoulPlayConfig, attr, value)

    def test_tera_passing_score_gate_still_fails_margin_gate(self):
        # the tera option OUTSCORES the plain move (0.55 > 0.54), so the
        # leniency score-gate cannot touch it; the margin gate still must
        # (0.55 < 0.54 + 0.025) and the plain move is played
        results = [
            (
                mcts_result_scored(
                    [("flareblitz-tera", 60, 0.55), ("flareblitz", 40, 0.54)]
                ),
                1.0,
                0,
            )
        ]
        self.assertEqual("flareblitz", select_move_from_mcts_results(results))



if __name__ == "__main__":
    unittest.main()


def mcts_result_pairs(options, s2_names, pairs):
    """Options: (name, visits, avg). pairs: {(s1_name, s2_name): (visits, avg)}
    encoded into a root_pairs table index-aligned with side_one/side_two."""
    side_one = [
        SimpleNamespace(move_choice=n, visits=v, total_score=v * a)
        for n, v, a in options
    ]
    side_two = [SimpleNamespace(move_choice=n, visits=1, total_score=0.5) for n in s2_names]
    table = []
    for n, _, _ in options:
        row = []
        for b in s2_names:
            v, a = pairs.get((n, b), (0, 0.0))
            row.append((v, v * a))
        table.append(row)
    return SimpleNamespace(
        side_one=side_one,
        side_two=side_two,
        total_visits=sum(v for _, v, _ in options),
        root_pairs=table,
    )


class TestLosingUpsideTiebreak(unittest.TestCase):
    """In lost positions the reply-mix averages converge; near-ties re-rank by
    the best explored outcome vs any single opponent reply (T23: heatwave
    0.129 vs suckerpunch 0.128 hid sucker's ~0.5 vs the attack replies)."""

    def setUp(self):
        self.orig_thr = FoulPlayConfig.losing_upside_threshold
        self.orig_fallback = FoulPlayConfig.losing_attack_fallback_threshold
        FoulPlayConfig.losing_upside_threshold = 0.15
        FoulPlayConfig.losing_attack_fallback_threshold = 0.05

    def tearDown(self):
        FoulPlayConfig.losing_upside_threshold = self.orig_thr
        FoulPlayConfig.losing_attack_fallback_threshold = self.orig_fallback

    def _t23(self, heat_avg=0.129, sucker_avg=0.128):
        # heatwave slightly ahead on avg + share; sucker's upside lives in the
        # attack-reply cells
        return mcts_result_pairs(
            [("heatwave", 580, heat_avg), ("suckerpunch", 420, sucker_avg)],
            ["willowisp", "drainingkiss"],
            {
                ("heatwave", "willowisp"): (410, 0.15),
                ("heatwave", "drainingkiss"): (170, 0.02),
                ("suckerpunch", "willowisp"): (300, 0.05),
                ("suckerpunch", "drainingkiss"): (120, 0.50),
            },
        )

    def test_losing_near_tie_picks_higher_upside(self):
        result = select_move_from_mcts_results([(self._t23(), 1.0, 0)])
        self.assertEqual("suckerpunch", result)

    def test_not_losing_keeps_normal_argmax(self):
        r = mcts_result_pairs(
            [("heatwave", 580, 0.42), ("suckerpunch", 420, 0.41)],
            ["willowisp", "drainingkiss"],
            {
                ("heatwave", "willowisp"): (410, 0.42),
                ("suckerpunch", "drainingkiss"): (120, 0.90),
            },
        )
        self.assertEqual("heatwave", select_move_from_mcts_results([(r, 1.0, 0)]))

    def test_real_score_edge_is_never_overridden(self):
        # sucker's avg is 0.02 below: outside the 0.01 tie band
        result = select_move_from_mcts_results(
            [(self._t23(heat_avg=0.14, sucker_avg=0.12), 1.0, 0)]
        )
        self.assertEqual("heatwave", result)

    def test_junk_cell_below_visit_floor_cannot_fake_upside(self):
        r = mcts_result_pairs(
            [("heatwave", 580, 0.129), ("suckerpunch", 420, 0.128)],
            ["willowisp", "drainingkiss"],
            {
                ("heatwave", "willowisp"): (578, 0.14),
                # 2 visits at 0.99: under the 2% floor of heatwave's row - but
                # give sucker no qualifying upside either
                ("heatwave", "drainingkiss"): (2, 0.99),
                ("suckerpunch", "willowisp"): (418, 0.11),
                ("suckerpunch", "drainingkiss"): (2, 0.95),
            },
        )
        # both options' best QUALIFYING cells: heatwave 0.14 vs sucker 0.11
        self.assertEqual("heatwave", select_move_from_mcts_results([(r, 1.0, 0)]))

    def test_disabled_by_zero_threshold(self):
        FoulPlayConfig.losing_upside_threshold = 0.0
        result = select_move_from_mcts_results([(self._t23(), 1.0, 0)])
        self.assertEqual("heatwave", result)

    def test_wheel_without_root_pairs_is_safe(self):
        r = self._t23()
        del r.root_pairs
        result = select_move_from_mcts_results([(r, 1.0, 0)])
        self.assertEqual("heatwave", result)


class TestDeadZoneUpsideUnification(unittest.TestCase):
    """Below the dead-zone bound (losing_attack_fallback_threshold) the upside
    tie band widens to ALL eligible options, subsuming the old attack-only
    fallback; exact upside ties break toward non-switch options."""

    def setUp(self):
        self.orig = (
            FoulPlayConfig.losing_upside_threshold,
            FoulPlayConfig.losing_attack_fallback_threshold,
        )
        FoulPlayConfig.losing_upside_threshold = 0.15
        FoulPlayConfig.losing_attack_fallback_threshold = 0.05

    def tearDown(self):
        (
            FoulPlayConfig.losing_upside_threshold,
            FoulPlayConfig.losing_attack_fallback_threshold,
        ) = self.orig

    def test_dead_zone_wide_band_overrides_score_edge_for_ko_cell(self):
        # switch "leads" the attack by 0.02 - outside the 0.01 tie band, but
        # both are in the dead zone (<0.05) so everything competes on upside;
        # the attack's KO cell wins over the stall switch
        r = mcts_result_pairs(
            [("switch corviknight", 600, 0.04), ("suckerpunch", 400, 0.02)],
            ["willowisp", "drainingkiss"],
            {
                ("switch corviknight", "willowisp"): (400, 0.05),
                ("switch corviknight", "drainingkiss"): (200, 0.04),
                ("suckerpunch", "willowisp"): (250, 0.01),
                ("suckerpunch", "drainingkiss"): (150, 0.55),
            },
        )
        self.assertEqual("suckerpunch", select_move_from_mcts_results([(r, 1.0, 0)]))

    def test_all_zero_cells_tie_break_to_non_switch(self):
        # truly dead: every pair cell is 0.0 -> upsides tie exactly -> prefer
        # the attack (crit/miss tails live outside the model)
        r = mcts_result_pairs(
            [("switch corviknight", 600, 0.0), ("bravebird", 400, 0.0)],
            ["shadowball", "drainingkiss"],
            {
                ("switch corviknight", "shadowball"): (400, 0.0),
                ("switch corviknight", "drainingkiss"): (200, 0.0),
                ("bravebird", "shadowball"): (250, 0.0),
                ("bravebird", "drainingkiss"): (150, 0.0),
            },
        )
        self.assertEqual("bravebird", select_move_from_mcts_results([(r, 1.0, 0)]))

    def test_mid_zone_sliding_band_admits_wider_gap(self):
        # SLIDING band: at ref 0.09 (below the 0.10 midpoint of the 0.05-0.15
        # range in this fixture) the band has widened to ~0.026, so a 0.02
        # score edge no longer excludes the big-ceiling challenger
        r = mcts_result_pairs(
            [("heatwave", 600, 0.09), ("suckerpunch", 400, 0.07)],
            ["willowisp", "drainingkiss"],
            {
                ("heatwave", "willowisp"): (400, 0.1),
                ("suckerpunch", "willowisp"): (250, 0.05),
                ("suckerpunch", "drainingkiss"): (150, 0.6),
            },
        )
        self.assertEqual("suckerpunch", select_move_from_mcts_results([(r, 1.0, 0)]))

    def test_near_threshold_band_is_nearly_zero(self):
        # at ref 0.14 (near the 0.15 upper activation in this fixture) the
        # band has shrunk to ~0.002: a 0.02 score edge is decisive
        r = mcts_result_pairs(
            [("heatwave", 600, 0.14), ("suckerpunch", 400, 0.12)],
            ["willowisp", "drainingkiss"],
            {
                ("heatwave", "willowisp"): (400, 0.15),
                ("suckerpunch", "drainingkiss"): (150, 0.6),
            },
        )
        self.assertEqual("heatwave", select_move_from_mcts_results([(r, 1.0, 0)]))


class TestUpsideDisplacementMargin(unittest.TestCase):
    """The upside challenger must beat the incumbent's ceiling by the
    displacement margin (0.05) - near-equal ceilings are noise and the
    normal pick stands. Switch incumbents still lose to equal-upside
    attacks (dead-zone preference survives the margin)."""

    def setUp(self):
        self.orig = (
            FoulPlayConfig.losing_upside_threshold,
            FoulPlayConfig.losing_attack_fallback_threshold,
            FoulPlayConfig.losing_upside_displacement_multiplier,
        )
        FoulPlayConfig.losing_upside_threshold = 0.15
        FoulPlayConfig.losing_attack_fallback_threshold = 0.05
        FoulPlayConfig.losing_upside_displacement_multiplier = 2.0

    def tearDown(self):
        (
            FoulPlayConfig.losing_upside_threshold,
            FoulPlayConfig.losing_attack_fallback_threshold,
            FoulPlayConfig.losing_upside_displacement_multiplier,
        ) = self.orig

    def test_sub_margin_ceiling_difference_keeps_incumbent(self):
        # challenger ceiling 0.30 vs incumbent 0.29: noise - incumbent stays
        r = mcts_result_pairs(
            [("heatwave", 580, 0.129), ("suckerpunch", 420, 0.128)],
            ["willowisp", "drainingkiss"],
            {
                ("heatwave", "willowisp"): (410, 0.29),
                ("heatwave", "drainingkiss"): (170, 0.02),
                ("suckerpunch", "willowisp"): (300, 0.05),
                ("suckerpunch", "drainingkiss"): (120, 0.30),
            },
        )
        self.assertEqual("heatwave", select_move_from_mcts_results([(r, 1.0, 0)]))

    def test_margin_clearing_challenger_still_displaces(self):
        # T23-class gap (0.50 vs 0.15) clears the margin easily
        r = mcts_result_pairs(
            [("heatwave", 580, 0.129), ("suckerpunch", 420, 0.128)],
            ["willowisp", "drainingkiss"],
            {
                ("heatwave", "willowisp"): (410, 0.15),
                ("heatwave", "drainingkiss"): (170, 0.02),
                ("suckerpunch", "willowisp"): (300, 0.05),
                ("suckerpunch", "drainingkiss"): (120, 0.50),
            },
        )
        self.assertEqual("suckerpunch", select_move_from_mcts_results([(r, 1.0, 0)]))


class TestUpsidePhantomSwitchFilter(unittest.TestCase):
    """The losing-position upside ceiling must ignore opponent switches into
    sampled unrevealed (phantom) mons - a phantom matchup is a fiction invented
    by determinization, so 'if they switch to X and we crush it' cannot be a
    real swindle when X may not exist. Opponent moves and switches into REVEALED
    benched mons are real replies."""

    def setUp(self):
        self.orig_thr = FoulPlayConfig.losing_upside_threshold
        self.orig_fallback = FoulPlayConfig.losing_attack_fallback_threshold
        self.orig_mult = FoulPlayConfig.losing_upside_displacement_multiplier
        FoulPlayConfig.losing_upside_threshold = 0.15
        FoulPlayConfig.losing_attack_fallback_threshold = 0.05
        FoulPlayConfig.losing_upside_displacement_multiplier = 2.0

    def tearDown(self):
        FoulPlayConfig.losing_upside_threshold = self.orig_thr
        FoulPlayConfig.losing_attack_fallback_threshold = self.orig_fallback
        FoulPlayConfig.losing_upside_displacement_multiplier = self.orig_mult

    def _phantom_scenario(self, switch_target):
        # suckerpunch's only high ceiling is a switch into `switch_target`; its
        # real-move reply is a mediocre 0.11. heatwave's ceiling is a real move.
        return mcts_result_pairs(
            [("heatwave", 580, 0.129), ("suckerpunch", 420, 0.128)],
            ["playrough", "switch {}".format(switch_target)],
            {
                ("heatwave", "playrough"): (580, 0.14),
                ("suckerpunch", "playrough"): (300, 0.11),
                ("suckerpunch", "switch {}".format(switch_target)): (120, 0.90),
            },
        )

    def test_phantom_switch_ceiling_is_rejected(self):
        # phantommon is not in the revealed set => sucker's 0.90 cell is a
        # fiction; heatwave's real 0.14 ceiling wins and the incumbent stays.
        result = select_move_from_mcts_results(
            [(self._phantom_scenario("phantommon"), 1.0, 0)],
            {"corviknight"},
        )
        self.assertEqual("heatwave", result)

    def test_revealed_switch_ceiling_still_counts(self):
        # identical table, but the switch target IS revealed => the 0.90 ceiling
        # is a real swindle and sucker displaces.
        result = select_move_from_mcts_results(
            [(self._phantom_scenario("garchomp"), 1.0, 0)],
            {"corviknight", "garchomp"},
        )
        self.assertEqual("suckerpunch", result)

    def test_none_revealed_set_keeps_legacy_behavior(self):
        # legacy callers / older wheels pass no revealed set => no filtering,
        # the phantom ceiling still counts (pre-fix behavior preserved).
        result = select_move_from_mcts_results(
            [(self._phantom_scenario("phantommon"), 1.0, 0)]
        )
        self.assertEqual("suckerpunch", result)

    def test_opponent_move_replies_are_never_filtered(self):
        # heatwave's whole ceiling is an opponent MOVE reply; even with a
        # revealed set that lists nothing, the move ceiling must survive.
        r = mcts_result_pairs(
            [("heatwave", 580, 0.129), ("suckerpunch", 420, 0.128)],
            ["playrough", "drainingkiss"],
            {
                ("heatwave", "drainingkiss"): (400, 0.50),
                ("heatwave", "playrough"): (180, 0.02),
                ("suckerpunch", "playrough"): (420, 0.11),
            },
        )
        self.assertEqual(
            "heatwave", select_move_from_mcts_results([(r, 1.0, 0)], {"corviknight"})
        )


class TestPhase1WorldDedupe(unittest.TestCase):
    """Identical sampled worlds are duplicate compute, not information."""

    def test_eight_worlds_over_two_states_reduce_to_four_entries(self):
        states = [("A", 0.125)] * 5 + [("B", 0.125)] * 3

        reduced = dedupe_states(states)

        self.assertEqual(4, len(reduced))
        self.assertAlmostEqual(1.0, sum(c for _s, c in reduced))
        by_state = {}
        for state_string, chance in reduced:
            by_state[state_string] = by_state.get(state_string, 0.0) + chance
        # per-state weight matches the original multiplicity
        self.assertAlmostEqual(5 * 0.125, by_state["A"])
        self.assertAlmostEqual(3 * 0.125, by_state["B"])

    def test_a_state_sampled_once_keeps_exactly_one_replica(self):
        reduced = dedupe_states([("A", 0.5), ("B", 0.5)])

        self.assertEqual([("A", 0.5), ("B", 0.5)], reduced)

    def test_all_unique_states_are_untouched(self):
        states = [(chr(ord("A") + i), 0.25) for i in range(4)]

        self.assertEqual(states, dedupe_states(states))


class TestArgmaxTeraGate(unittest.TestCase):
    """The argmax-path tera gate (selection.py _apply_argmax_tera_gate).

    Terastallizing spends a once-per-battle resource, so the pooled-visit argmax
    winning by a hair must not spend it. Both fixtures below are REAL turns from
    ladder games on 2026-08-08, kept as regressions because they exercise the two
    branches in opposite directions.
    """

    _ATTRS = (
        "selection_argmax_only",
        "tera_gate_score_per_mon",
        "tera_gate_visit_frac",
    )

    def setUp(self):
        self.originals = {a: getattr(FoulPlayConfig, a) for a in self._ATTRS}
        FoulPlayConfig.selection_argmax_only = True
        FoulPlayConfig.tera_gate_score_per_mon = 0.001
        FoulPlayConfig.tera_gate_visit_frac = 0.5

    def tearDown(self):
        for a, v in self.originals.items():
            setattr(FoulPlayConfig, a, v)

    # game 2, turn 3: tera argmax 46.67%, switch glaceon at 34.87% clears the
    # 23.33% bar and outscores it, so the resource must be held.
    _G2T3 = [
        ("shoreup-tera", 4667, 0.5199),
        ("switch glaceon", 3487, 0.5645),
        ("switch zangoose", 349, 0.5011),
        ("shadowball", 233, 0.4860),
        ("sludgebomb-tera", 141, 0.4630),
    ]

    # game 1, turn 24: tera argmax 78.97%, best non-tera only 4.99% -- nothing
    # reaches the 39.48% bar, so the tera is NOT a close call and is allowed.
    _G1T24 = [
        ("bravebird-tera", 7897, 0.6305),
        ("closecombat-tera", 873, 0.6636),
        ("bravebird", 499, 0.6456),
        ("swordsdance", 373, 0.6950),
        ("closecombat", 349, 0.6710),
    ]

    def _choose(self, rows, opp_alive, opp_unrevealed, opp_tera_used=False):
        return select_move_from_mcts_results(
            [(mcts_result_scored(rows), 1.0, 0)],
            opp_alive=opp_alive,
            opp_unrevealed=opp_unrevealed,
            opp_tera_used=opp_tera_used,
        )

    def test_blocks_when_a_considered_non_tera_outscores_it(self):
        # 6 alive + 5 unrevealed => margin 0.011; tera needs >= 0.5755, has 0.5199
        self.assertEqual("switch glaceon", self._choose(self._G2T3, 6, 5))

    def test_allows_when_no_non_tera_reaches_the_visit_bar(self):
        # swordsdance outSCORES the tera (0.6950 > 0.6305) but holds only 4.99%
        # share against the 39.48% bar, so it never gets to veto.
        self.assertEqual("bravebird-tera", self._choose(self._G1T24, 4, 2))

    def test_margin_shrinks_as_the_opponent_is_revealed_and_killed(self):
        # same position, 1 alive + 0 unrevealed => margin 0.001. Still blocked,
        # because the tera is 0.0446 behind on score regardless of the margin.
        self.assertEqual("switch glaceon", self._choose(self._G2T3, 1, 0))

    def test_tera_wins_when_it_clears_the_margin(self):
        rows = [
            ("flareblitz-tera", 5000, 0.600),
            ("flareblitz", 4000, 0.560),  # clears the 25% bar, 0.04 behind
        ]
        # 2 alive + 1 unrevealed => margin 0.003 + 0.002 opp-tera-held;
        # 0.600 >= 0.560 + 0.005
        self.assertEqual("flareblitz-tera", self._choose(rows, 2, 1))

    # game 3 (2026-08-08, loss to "sobs again"), decision 6: bulkup-tera argmax
    # at 32.1% won by only +0.042 over bulkup with ~10 effective opponent mons
    # and the opponent's tera unspent. At visit_frac 1/3 the higher-SCORING
    # closecombat (14.0% > 10.7% bar) joins the considered set and vetoes the
    # spend; fallback is the considered visit argmax (bulkup), not closecombat.
    _G3T6 = [
        ("bulkup-tera", 3210, 0.445),
        ("bulkup", 1760, 0.403),
        ("switch sandaconda", 1430, 0.391),
        ("closecombat", 1400, 0.452),
        ("earthquake", 920, 0.386),
    ]

    def test_third_of_top_visit_bar_lets_high_score_move_veto(self):
        FoulPlayConfig.tera_gate_visit_frac = 1 / 3
        # floor = 0.452 + 0.001x10 + 0.002 = 0.464 > 0.445 => blocked
        self.assertEqual("bulkup", self._choose(self._G3T6, 6, 4))

    def test_opp_tera_held_bonus_is_decisive_at_the_margin(self):
        rows = [
            ("flareblitz-tera", 5000, 0.600),
            ("flareblitz", 4000, 0.596),
        ]
        # margin 0.003; opp tera spent => floor 0.599, allowed
        self.assertEqual("flareblitz-tera", self._choose(rows, 2, 1, opp_tera_used=True))
        # opp tera held => floor 0.601, blocked
        self.assertEqual("flareblitz", self._choose(rows, 2, 1, opp_tera_used=False))

    def test_disabled_by_default(self):
        FoulPlayConfig.tera_gate_score_per_mon = 0.0
        self.assertEqual("shoreup-tera", self._choose(self._G2T3, 6, 5))

    def test_non_tera_argmax_is_untouched(self):
        rows = [("earthquake", 5000, 0.60), ("flareblitz-tera", 1000, 0.99)]
        self.assertEqual("earthquake", self._choose(rows, 6, 5))
