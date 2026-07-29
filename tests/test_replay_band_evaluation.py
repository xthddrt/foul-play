"""B1 interval-aware assertion machinery and the B2 UNBURDEN ability hypothesis
(fp/replay/checker.py).

Gate 5 proved that collapsing an opponent's `pct/100` display band to ANY point
estimate manufactures hard findings on whichever side of a KO threshold the
constant lands (band-top: 3 hard, band-midpoint: 6 hard).  The checker now
re-evaluates a finding turn at every other value in the certified band and
reports the finding only when EVERY value fails to reproduce the observation.
These tests pin the band-candidate selection and the finding-identity used to
intersect evaluations; the end-to-end behavior is locked by the corpus games
named in the comments (synth07075 T9, synth08464 T39, synth11048 T33,
synth33896 T2, synth40757 T3 all report zero findings under the band walk).
"""

import os
import unittest
from unittest import mock

import constants
from fp import hp_certificate as hc
from fp.battle import Pokemon
from fp.replay.checker import (
    _band_assignments,
    _band_candidate_values,
    _finding_key,
    _substitute_band_values,
    _unburden_ability_ambiguous,
)
from fp.replay.comparator import Finding, Severity, parse_instruction


def _mon(species="hariyama", level=86, max_hp=392, pct=None):
    p = Pokemon(species, level)
    p.max_hp = max_hp
    p.max_hp_exact = False
    if pct is not None:
        hc.apply_display(p, pct)
    return p


class TestBandCandidateValues(unittest.TestCase):
    def test_band_is_enumerated_around_the_estimate(self):
        # synth07075's Hariyama: shown 1/100 on max 392 -> the protocol pins
        # hp to [1, 3]; the midpoint estimate is 2, so the other candidates
        # are exactly the rest of the band
        p = _mon(max_hp=392, pct=1)
        lo, hi = hc.display_bounds(1, 392)
        self.assertEqual((lo, hi), (1, 3))
        self.assertEqual(p.hp, 2)
        self.assertEqual(_band_candidate_values(p, []), [1, 3])

    def test_certified_exact_hp_takes_the_fast_path(self):
        p = _mon(pct=50)
        hc.certify(p, p.hp, "endeavor")
        self.assertEqual(_band_candidate_values(p, []), [])

    def test_full_display_band_is_a_singleton(self):
        # 100/100 states hp == max_hp exactly; there is nothing to walk
        p = _mon(pct=100)
        self.assertEqual(_band_candidate_values(p, []), [])

    def test_no_display_no_band(self):
        # a fresh mon is at full HP by construction, with no display to
        # derive a band from
        p = _mon()
        self.assertEqual(_band_candidate_values(p, []), [])

    def test_hp_moved_since_the_display_disables_the_band(self):
        # a Substitute-[weak] clamp (or any later write) means the display no
        # longer describes the value: re-deriving a band would assert HPs
        # nothing certified
        p = _mon(pct=50)
        p.hp = p.hp - 5
        self.assertEqual(_band_candidate_values(p, []), [])

    def test_candidates_stay_inside_the_band_and_exclude_the_estimate(self):
        for max_hp in (231, 310, 392, 714):
            for pct in (1, 13, 50, 99):
                p = _mon(max_hp=max_hp, pct=pct)
                lo, hi = hc.display_bounds(pct, max_hp)
                cands = _band_candidate_values(p, [])
                self.assertNotIn(p.hp, cands)
                for v in cands:
                    self.assertTrue(lo <= v <= hi, (max_hp, pct, v))
                # every other in-band value is walked (bands here are narrow)
                self.assertEqual(len(cands), (hi - lo + 1) - 1)


class TestFindingKey(unittest.TestCase):
    def test_ko_margin_decoration_does_not_split_identity(self):
        a = Finding(9, Severity.HARD, "boost", "observed - def on opp", "raw")
        b = Finding(9, Severity.SOFT, "boost", "observed - def on opp [ko-margin]", "raw")
        self.assertEqual(_finding_key(a), _finding_key(b))

    def test_different_events_stay_distinct(self):
        a = Finding(9, Severity.HARD, "boost", "observed - def on opp", "raw1")
        b = Finding(9, Severity.HARD, "boost", "observed - spd on opp", "raw2")
        self.assertNotEqual(_finding_key(a), _finding_key(b))


class _FakeBattler:
    def __init__(self, active):
        self.active = active


class TestUnburdenAmbiguity(unittest.TestCase):
    """battle-gen9customgame-2651860374_beatmesilly T4: the opponent Hitmonlee's
    White Herb was consumed on T3 (unburden volatile tracked), its ability is
    unknowable from a customgame log, and the engine only doubles speed when the
    ability says UNBURDEN -- the turn must be widened with that hypothesis."""

    def test_unknown_ability_with_unburden_volatile_is_ambiguous(self):
        p = _mon("hitmonlee", 85)
        p.volatile_statuses.append("unburden")
        self.assertIsNone(p.ability)
        self.assertTrue(_unburden_ability_ambiguous(_FakeBattler(p)))

    def test_known_ability_is_not_ambiguous(self):
        p = _mon("hitmonlee", 85)
        p.volatile_statuses.append("unburden")
        p.ability = "unburden"
        self.assertFalse(_unburden_ability_ambiguous(_FakeBattler(p)))
        p.ability = "limber"
        self.assertFalse(_unburden_ability_ambiguous(_FakeBattler(p)))

    def test_no_volatile_is_not_ambiguous(self):
        p = _mon("hitmonlee", 85)
        self.assertFalse(_unburden_ability_ambiguous(_FakeBattler(p)))
        self.assertFalse(_unburden_ability_ambiguous(_FakeBattler(None)))


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# B3: the Substitute HP interval axis
# ---------------------------------------------------------------------------
# A hit a Substitute absorbs never puts its magnitude on the wire, so the sub's
# remaining HP after one is a RANGE.  It is walked for exactly the reason B1's
# HP band is (HANDOFF section 4 rule 10): whichever side of a break threshold a
# point estimate lands on decides the turn's outcome.  End-to-end behaviour is
# locked by the corpus games in the comments (synth18317 T46, synth45492 T27,
# synth10932 T37 all report zero findings under the interval walk, and each
# returns under its own disjoint control flag).
class TestSubstituteBandValues(unittest.TestCase):
    def _sub_mon(self, low, high):
        p = _mon()
        p.volatile_statuses.append(constants.SUBSTITUTE)
        p.substitute_health = high
        p.substitute_health_low = low
        return p

    def test_singleton_interval_takes_the_fast_path(self):
        # a freshly created substitute is EXACT (floor(maxhp/4)); nothing to walk
        self.assertEqual(_substitute_band_values(self._sub_mon(82, 82), [], "s1"), [])

    def test_no_substitute_no_axis(self):
        p = _mon()
        p.substitute_health = 40  # stale, no volatile
        p.substitute_health_low = 10
        self.assertEqual(_substitute_band_values(p, [], "s1"), [])

    def test_narrow_interval_is_enumerated_exhaustively(self):
        # synth45492 T27: a Choice Specs Psyshock rolls 63-74 into an 82 HP sub,
        # so the survivor is 8-19 and the tracked value (the upper bound) is 19
        self.assertEqual(
            _substitute_band_values(self._sub_mon(8, 19), [], "s1"),
            list(range(8, 19)),
        )

    def test_wide_interval_falls_back_to_the_break_thresholds(self):
        # a REFUSED derivation widens the interval to [1, prior]; enumerating 82
        # values would be pointless, so the fallback keeps the endpoints and the
        # running DamageSubstitute totals (where the outcome actually flips)
        branch = [
            parse_instruction("DamageSubstitute SideOne: 30 [move]"),
            parse_instruction("DamageSubstitute SideOne: 25 [move]"),
            parse_instruction("DamageSubstitute SideTwo: 99 [move]"),  # other side
        ]
        got = _substitute_band_values(self._sub_mon(1, 82), [branch], "s1")
        self.assertIn(1, got)
        self.assertIn(30, got)  # first hit's total
        self.assertIn(31, got)
        self.assertIn(55, got)  # cumulative after the second hit
        self.assertIn(56, got)
        self.assertNotIn(99, got)  # the other side's sub is not this axis
        self.assertNotIn(82, got)  # the current value is not a candidate
        self.assertTrue(all(1 <= v <= 82 for v in got))

    def test_control_flag_disables_the_axis(self):
        with mock.patch.dict(os.environ, {"FP_CONTROL_NO_SUBSTITUTE_BAND": "1"}):
            self.assertEqual(
                _substitute_band_values(self._sub_mon(8, 19), [], "s1"), []
            )


class TestBandAssignments(unittest.TestCase):
    def test_single_axis_walks_its_values(self):
        obj = _mon()
        self.assertEqual(
            _band_assignments([(obj, "hp", 5, [4, 6])]),
            [((obj, "hp", 4),), ((obj, "hp", 6),)],
        )

    def test_two_axes_walk_their_product_and_exclude_the_current_point(self):
        a, b = _mon(), _mon()
        got = _band_assignments([(a, "hp", 5, [4]), (b, "substitute_health", 9, [8])])
        self.assertEqual(len(got), 3)  # 2*2 combinations minus the current point
        self.assertNotIn(((a, "hp", 5), (b, "substitute_health", 9)), got)
        self.assertIn(((a, "hp", 4), (b, "substitute_health", 8)), got)

    def test_an_empty_axis_is_dropped(self):
        obj = _mon()
        self.assertEqual(_band_assignments([(obj, "hp", 5, [])]), [])

    def test_a_pathological_product_degrades_to_axis_aligned_walks(self):
        a, b = _mon(), _mon()
        got = _band_assignments(
            [
                (a, "hp", 0, list(range(1, 40))),
                (b, "substitute_health", 0, list(range(1, 40))),
            ]
        )
        # 40*40-1 exceeds the cap, so each axis is walked alone
        self.assertEqual(len(got), 78)
        self.assertTrue(all(len(g) == 1 for g in got))
