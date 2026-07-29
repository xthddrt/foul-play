"""Exact-HP certificates for the opponent-side reconstruction (fp/hp_certificate.py).

Each test names the PS source it mirrors and, where one exists, the synthetic
corpus game whose finding it locks down.  These replace APPROXIMATIONS.md U3,
which was DELETED when the identities below started being propagated.
"""

import unittest

import constants
from constants import BattleType
from fp import hp_certificate as hc
from fp.battle import Battle, Pokemon
from fp.battle_modifier import heal_or_damage, move, sethp, switch
from fp.replay.damage_membership import apply_exact_team


class TestDisplayEncoding(unittest.TestCase):
    """PS sim/pokemon.ts:2080-2086 -- `Math.ceil(100 * hp / maxhp)`, forced down
    to 99 whenever `hp < maxhp`."""

    def test_display_pct_matches_ps_formula_exhaustively(self):
        for max_hp in (1, 4, 100, 231, 247, 248, 310, 352, 429, 714):
            for hp in range(0, max_hp + 1):
                expected = 0
                if hp > 0:
                    expected = -((-100 * hp) // max_hp)
                    if expected == 100 and hp < max_hp:
                        expected = 99
                self.assertEqual(hc.display_pct(hp, max_hp), expected, (hp, max_hp))

    def test_bounds_are_the_exact_inverse_of_the_display(self):
        # every hp the protocol could be describing lies in the band, and every
        # value in the band re-displays as that same pct -- the band is tight
        for max_hp in (1, 4, 100, 231, 247, 310, 352, 429, 714):
            for hp in range(1, max_hp + 1):
                pct = hc.display_pct(hp, max_hp)
                lo, hi = hc.display_bounds(pct, max_hp)
                self.assertTrue(lo <= hp <= hi, (hp, max_hp, pct, lo, hi))
                for candidate in range(lo, hi + 1):
                    self.assertEqual(hc.display_pct(candidate, max_hp), pct)

    def test_full_hp_display_is_unambiguous(self):
        # the 99-clamp is what makes `100/100` an identity rather than a band
        self.assertEqual(hc.display_bounds(100, 310), (310, 310))
        self.assertEqual(hc.display_pct(309, 310), 99)

    def test_estimate_never_leaves_the_band(self):
        # APPROXIMATIONS U3's hard finding was `round(310 * 0.25) == 78` for a
        # Slowking the protocol had pinned to [75, 77]
        self.assertEqual(hc.display_bounds(25, 310), (75, 77))
        self.assertEqual(hc.estimate_from_display(25, 310), 76)
        for max_hp in (1, 4, 100, 231, 247, 310, 352, 429, 714):
            for pct in range(1, 101):
                lo, hi = hc.display_bounds(pct, max_hp)
                self.assertTrue(lo <= hc.estimate_from_display(pct, max_hp) <= hi)


class TestCertificateLifetime(unittest.TestCase):
    def _mon(self, max_hp=310):
        p = Pokemon("slowking", 88)
        p.max_hp = max_hp
        p.max_hp_exact = True
        hc.apply_display(p, 50)
        return p

    def test_display_ends_a_certificate(self):
        p = self._mon()
        hc.certify(p, 75, "test")
        self.assertTrue(hc.is_exact(p))
        # a new percent display means the HP moved by an amount the percent
        # protocol does not state -- the certificate ends there
        hc.apply_display(p, 20)
        self.assertFalse(hc.is_exact(p))

    def test_singleton_band_is_exact_without_a_certificate(self):
        p = Pokemon("shedinja", 100)
        p.max_hp = 1
        p.max_hp_exact = True
        hc.apply_display(p, 100)
        self.assertTrue(hc.is_exact(p))
        self.assertEqual(p.hp, 1)

    def test_certificate_refused_when_it_contradicts_its_own_display(self):
        hc.reset_refusals()
        p = self._mon()
        hc.apply_display(p, 25)
        # 200/310 displays as 65, not 25: the chain is broken, so REFUSE
        self.assertFalse(hc.certify(p, 200, "bogus", shown_pct=25))
        self.assertFalse(hc.is_exact(p))
        self.assertEqual(len(hc.CERTIFICATE_REFUSALS), 1)

    def test_certificate_accepted_when_it_reproduces_its_display(self):
        hc.reset_refusals()
        p = self._mon()
        hc.apply_display(p, 25)
        self.assertTrue(hc.certify(p, 75, "endeavor", shown_pct=25))
        self.assertEqual(p.hp, 75)
        self.assertTrue(hc.is_exact(p))
        self.assertEqual(hc.CERTIFICATE_REFUSALS, [])


class TestDeferredCheckAgainstGuessedMaxHp(unittest.TestCase):
    """The check divides by max_hp, so it is only decidable once max_hp is
    exact.  Checking against a GUESS indicts the wrong party -- synth43722 T20's
    Endeavor certificate (186, correct) implies 71/100 against the guessed
    max_hp 265 while the protocol showed 69/100."""

    def test_check_is_deferred_while_max_hp_is_a_guess(self):
        hc.reset_refusals()
        p = Pokemon("terapagosterastal", 77)
        p.max_hp = 265
        p.max_hp_exact = False
        hc.apply_display(p, 69)
        self.assertTrue(hc.certify(p, 186, "endeavor", shown_pct=69))
        self.assertEqual(hc.CERTIFICATE_REFUSALS, [])
        # ... and settles once the sidecar supplies the real max HP
        p.max_hp = 271
        p.max_hp_exact = True
        self.assertTrue(hc.verify_against_exact_max(p))
        self.assertTrue(hc.is_exact(p))
        self.assertEqual(p.hp, 186)

    def test_deferred_check_still_catches_a_real_contradiction(self):
        hc.reset_refusals()
        p = Pokemon("slowking", 88)
        p.max_hp = 300
        p.max_hp_exact = False
        hc.apply_display(p, 25)
        hc.certify(p, 74, "endeavor", shown_pct=25)
        p.max_hp = 310  # true max: 74/310 displays as 24, not 25
        p.max_hp_exact = True
        self.assertFalse(hc.verify_against_exact_max(p))
        self.assertFalse(hc.is_exact(p))
        self.assertEqual(len(hc.CERTIFICATE_REFUSALS), 1)

    def test_max_hp_relative_certificate_is_re_evaluated_not_just_re_checked(self):
        # synth15945: a Revival Blessing certified as `248 // 2 == 124` against a
        # GUESSED max HP, where the truth is `247 // 2 == 123`.  PS revives at
        # `sethp(maxhp / 2)` and `sethp` truncs (sim/battle.ts:2791-2792,
        # sim/pokemon.ts:1663), so the value is a FUNCTION of max HP.
        hc.reset_refusals()
        p = Pokemon("luvdisc", 100)
        p.max_hp = 248
        p.max_hp_exact = False
        hc.apply_display(p, 50)
        hc.certify(p, None, "revival blessing", shown_pct=50, fraction=(1, 2))
        self.assertEqual(p.hp, 124)
        p.max_hp = 247
        p.max_hp_exact = True
        self.assertTrue(hc.verify_against_exact_max(p))
        self.assertEqual(p.hp, 123)
        self.assertTrue(hc.is_exact(p))
        self.assertEqual(hc.CERTIFICATE_REFUSALS, [])

    def test_stale_certificate_drops_exactness_quietly(self):
        # synth08292: an Alomomola certified at 220 was Regenerator-healed to
        # 363 on switch-out.  The remembered pct describes a value that no
        # longer exists -- lost provenance, not a contradiction.
        hc.reset_refusals()
        p = Pokemon("alomomola", 84)
        p.max_hp = 429
        p.max_hp_exact = False
        hc.apply_display(p, 52)
        hc.certify(p, 220, "endeavor", shown_pct=52)
        p.hp = 363  # a write that bypassed this module
        p.max_hp_exact = True
        # False so the caller falls back to the ordinary estimator, but NOT
        # counted as a refusal -- nothing contradicted anything
        self.assertFalse(hc.verify_against_exact_max(p))
        self.assertFalse(hc.is_exact(p))
        self.assertEqual(hc.CERTIFICATE_REFUSALS, [])


class TestEndeavorCertificate(unittest.TestCase):
    """PS data/moves.ts:4789-4791 `damageCallback` = `target.getUndynamaxedHP()
    - pokemon.hp`, so after a landed Endeavor `target.hp == pokemon.hp`."""

    def setUp(self):
        self.battle = Battle(None)
        self.battle.battle_type = BattleType.RANDOM_BATTLE
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.user.active = Pokemon("luvdisc", 100)
        self.battle.user.active.max_hp = 247
        self.battle.user.active.max_hp_exact = True
        hc.set_exact(self.battle.user.active, 75)
        self.battle.opponent.active = Pokemon("slowking", 88)
        self.battle.opponent.active.max_hp = 310
        self.battle.opponent.active.max_hp_exact = True
        hc.apply_display(self.battle.opponent.active, 40)

    def test_landed_endeavor_pins_the_target_to_the_attacker_hp(self):
        # synth22135 T38-39, the gate-4 hard finding: the reconstruction used to
        # read 310 * 0.25 -> 78 and model a 3 HP Endeavor into a mon PS showed
        # immune on the following turn.
        move(self.battle, ["", "move", "p1a: Luvdisc", "Endeavor", "p2a: Slowking"])
        heal_or_damage(self.battle, ["", "-damage", "p2a: Slowking", "25/100"])
        self.assertEqual(self.battle.opponent.active.hp, 75)
        self.assertTrue(hc.is_exact(self.battle.opponent.active))

    def test_identity_also_runs_backwards(self):
        # the opponent Endeavors US: our `-damage` line is exact `hp/maxhp`, so
        # it certifies the OPPONENT attacker (synth15945 T18)
        hc.apply_display(self.battle.opponent.active, 50)
        self.assertFalse(hc.is_exact(self.battle.opponent.active))
        move(self.battle, ["", "move", "p2a: Slowking", "Endeavor", "p1a: Luvdisc"])
        heal_or_damage(self.battle, ["", "-damage", "p1a: Luvdisc", "123/247"])
        self.assertEqual(self.battle.opponent.active.hp, 123)
        self.assertTrue(hc.is_exact(self.battle.opponent.active))

    def test_substitute_absorb_certifies_nothing(self):
        # PS's Substitute returns HIT_SUBSTITUTE before the target's own HP is
        # touched, and emits `-activate ... Substitute` instead of `-damage`
        from fp.battle_modifier import activate

        move(self.battle, ["", "move", "p1a: Luvdisc", "Endeavor", "p2a: Slowking"])
        activate(
            self.battle,
            ["", "-activate", "p2a: Slowking", "move: Substitute", "[damage]"],
        )
        self.assertIsNone(self.battle.opponent.active.hp_certificate_pending)
        self.assertFalse(hc.is_exact(self.battle.opponent.active))

    def test_immunity_certifies_nothing(self):
        from fp.battle_modifier import immune

        move(self.battle, ["", "move", "p1a: Luvdisc", "Endeavor", "p2a: Slowking"])
        immune(self.battle, ["", "-immune", "p2a: Slowking"])
        self.assertIsNone(self.battle.opponent.active.hp_certificate_pending)

    def test_chip_damage_never_consumes_the_certificate(self):
        # a `[from]` tag means the damage is not the move's
        move(self.battle, ["", "move", "p1a: Luvdisc", "Endeavor", "p2a: Slowking"])
        heal_or_damage(
            self.battle,
            ["", "-damage", "p2a: Slowking", "35/100", "[from] Stealth Rock"],
        )
        self.assertFalse(hc.is_exact(self.battle.opponent.active))


class TestHalvingCertificate(unittest.TestCase):
    """Super Fang / Nature's Madness / Ruination deal `max(1, floor(hp/2))`
    (PS data/moves.ts:18459-18461, 12634-12636, 15526-15528): exact OUT only
    when the prior HP was exact IN."""

    def setUp(self):
        self.battle = Battle(None)
        self.battle.battle_type = BattleType.RANDOM_BATTLE
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.user.active = Pokemon("luvdisc", 100)
        self.battle.user.active.max_hp_exact = True
        self.battle.opponent.active = Pokemon("slowking", 88)
        self.battle.opponent.active.max_hp = 310
        self.battle.opponent.active.max_hp_exact = True

    def test_exact_prior_gives_exact_result(self):
        hc.set_exact(self.battle.opponent.active, 310)
        move(self.battle, ["", "move", "p1a: Luvdisc", "Super Fang", "p2a: Slowking"])
        heal_or_damage(self.battle, ["", "-damage", "p2a: Slowking", "50/100"])
        self.assertEqual(self.battle.opponent.active.hp, 155)
        self.assertTrue(hc.is_exact(self.battle.opponent.active))

    def test_inexact_prior_only_narrows_and_is_not_claimed(self):
        hc.apply_display(self.battle.opponent.active, 80)
        move(self.battle, ["", "move", "p1a: Luvdisc", "Super Fang", "p2a: Slowking"])
        heal_or_damage(self.battle, ["", "-damage", "p2a: Slowking", "40/100"])
        self.assertFalse(hc.is_exact(self.battle.opponent.active))


class TestMaxRelativeHalvingChain(unittest.TestCase):
    """Gate-5 B5: all three corpus-wide `deferred check against exact max_hp`
    refusals were the SAME defect -- a Super Fang-family halving of an HP that
    was exact only as a FUNCTION of a guessed max (`hp == max_hp` after a Rest /
    Strength Sap full-heal) was frozen as an absolute integer computed off the
    guess.  The derivation must be carried as (fraction, halvings) and replayed
    against the true max: `ceil(max/2)` is not any (num, den) under floor
    division, so the fraction alone cannot express it."""

    def _full_hp_opponent(self, species, level, guessed_max):
        battle = Battle(None)
        battle.battle_type = BattleType.RANDOM_BATTLE
        battle.user.name = "p1"
        battle.opponent.name = "p2"
        battle.user.active = Pokemon("wochien", 83)
        battle.user.active.max_hp_exact = True
        p = Pokemon(species, level)
        p.max_hp = guessed_max
        p.max_hp_exact = False
        battle.opponent.active = p
        hc.apply_display(p, 100)  # Rest's `-heal ... 100/100 slp`
        self.assertTrue(hc.is_exact(p))
        self.assertEqual(p.hp, guessed_max)
        return battle, p

    def test_synth43106_terapagos_ruination_chain_survives_the_true_max(self):
        # guessed max 266: Ruination -> 133 (shown 51/100); true max 273 makes
        # the honest value 137, which displays as 51 -- the certificate was
        # RIGHT as a derivation and wrong only as a frozen integer.
        hc.reset_refusals()
        battle, p = self._full_hp_opponent("terapagosterastal", 77, 266)
        move(battle, ["", "move", "p1a: Wo-Chien", "Ruination", "p2a: Terapagos"])
        heal_or_damage(battle, ["", "-damage", "p2a: Terapagos", "51/100"])
        self.assertTrue(hc.is_exact(p))
        self.assertEqual(p.hp, 133)  # working value against the guess
        self.assertEqual(p.hp_certificate_fraction, (1, 1))
        self.assertEqual(p.hp_certificate_halvings, 1)
        p.max_hp = 273
        p.max_hp_exact = True
        self.assertTrue(hc.verify_against_exact_max(p))
        self.assertEqual(p.hp, 137)  # 273 -> halve -> 137, displays as 51
        self.assertTrue(hc.is_exact(p))
        self.assertEqual(hc.CERTIFICATE_REFUSALS, [])

    def test_second_halving_composes(self):
        # synth43106's second refusal: 133 -> 67 (shown 26/100); the honest
        # chain is 273 -> 137 -> 69, which displays as 26.
        hc.reset_refusals()
        battle, p = self._full_hp_opponent("terapagosterastal", 77, 266)
        move(battle, ["", "move", "p1a: Wo-Chien", "Ruination", "p2a: Terapagos"])
        heal_or_damage(battle, ["", "-damage", "p2a: Terapagos", "51/100"])
        move(battle, ["", "move", "p1a: Wo-Chien", "Ruination", "p2a: Terapagos"])
        heal_or_damage(battle, ["", "-damage", "p2a: Terapagos", "26/100"])
        self.assertEqual(p.hp, 67)
        self.assertEqual(p.hp_certificate_halvings, 2)
        p.max_hp = 273
        p.max_hp_exact = True
        self.assertTrue(hc.verify_against_exact_max(p))
        self.assertEqual(p.hp, 69)
        self.assertEqual(hc.CERTIFICATE_REFUSALS, [])

    def test_synth15836_drifblim_shape(self):
        # guessed max 398 -> 199 shown 50/100; true max 396 -> 198, displays 50
        hc.reset_refusals()
        battle, p = self._full_hp_opponent("drifblim", 86, 398)
        move(battle, ["", "move", "p1a: Wo-Chien", "Ruination", "p2a: Drifblim"])
        heal_or_damage(battle, ["", "-damage", "p2a: Drifblim", "50/100"])
        self.assertEqual(p.hp, 199)
        p.max_hp = 396
        p.max_hp_exact = True
        self.assertTrue(hc.verify_against_exact_max(p))
        self.assertEqual(p.hp, 198)
        self.assertEqual(hc.CERTIFICATE_REFUSALS, [])

    def test_halving_an_absolute_prior_stays_absolute(self):
        # an Endeavor-certified integer is max-independent: halving it must NOT
        # become max-relative, and the deferred display check still applies
        hc.reset_refusals()
        battle, p = self._full_hp_opponent("slowking", 88, 300)
        hc.apply_display(p, 65)
        hc.certify(p, 200, "endeavor", shown_pct=65)
        move(battle, ["", "move", "p1a: Wo-Chien", "Ruination", "p2a: Slowking"])
        # PS displays against the TRUE max 310: ceil(100*100/310) == 33
        heal_or_damage(battle, ["", "-damage", "p2a: Slowking", "33/100"])
        self.assertEqual(p.hp, 100)
        self.assertIsNone(p.hp_certificate_fraction)
        p.max_hp = 310
        p.max_hp_exact = True
        self.assertTrue(hc.verify_against_exact_max(p))
        self.assertEqual(p.hp, 100)  # absolute: the max correction moves nothing

    def test_max_relative_value_never_donates_through_endeavor(self):
        # the attacker's `hp == guessed max` is not an absolute number; copying
        # it onto the target would sever it from the max it re-evaluates against
        battle, p = self._full_hp_opponent("drifblim", 86, 398)
        self.assertTrue(hc.is_exact(p))
        self.assertFalse(hc.is_absolute_exact(p))
        target = battle.user.active
        hc.clear(target)
        target.max_hp = 247
        hc.arm(target, "endeavor")
        self.assertFalse(
            hc.consume(target, p, target.hp, False, shown_pct=None)
        )
        self.assertFalse(hc.is_exact(target))


class TestRefusalContext(unittest.TestCase):
    def test_refusal_records_carry_game_and_turn(self):
        hc.reset_refusals()
        hc.set_context(game="battle-gen9randombattle-synth43106_synthopp.log")
        hc.set_context(turn=48)
        p = Pokemon("slowking", 88)
        p.max_hp = 310
        p.max_hp_exact = True
        hc.apply_display(p, 25)
        self.assertFalse(hc.certify(p, 200, "bogus", shown_pct=25))
        self.assertEqual(len(hc.CERTIFICATE_REFUSALS), 1)
        rec = hc.CERTIFICATE_REFUSALS[0]
        self.assertEqual(
            rec["game"], "battle-gen9randombattle-synth43106_synthopp.log"
        )
        self.assertEqual(rec["turn"], 48)


class TestPainSplitCertificate(unittest.TestCase):
    """PS data/moves.ts:13140-13148 sets BOTH sides to
    `averagehp = floor((targetHP + pokemon.hp) / 2)`, each clamped into its own
    [1, maxhp] by `sethp` (sim/pokemon.ts:1661-1673).  Our side's `-sethp` is
    exact, so when it is not clamped it IS the average."""

    def setUp(self):
        self.battle = Battle(None)
        self.battle.battle_type = BattleType.RANDOM_BATTLE
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.user.active = Pokemon("jellicent", 84)
        self.battle.user.active.max_hp = 403
        self.battle.user.active.max_hp_exact = True
        self.battle.opponent.active = Pokemon("slowking", 88)
        self.battle.opponent.active.max_hp = 310
        self.battle.opponent.active.max_hp_exact = True
        hc.apply_display(self.battle.opponent.active, 40)

    def test_our_exact_sethp_certifies_the_opponent(self):
        tag = "[from] move: Pain Split"
        sethp(self.battle, ["", "-sethp", "p2a: Slowking", "70/100", tag, "[silent]"])
        sethp(self.battle, ["", "-sethp", "p1a: Jellicent", "217/403", tag])
        self.assertEqual(self.battle.opponent.active.hp, 217)
        self.assertTrue(hc.is_exact(self.battle.opponent.active))

    def test_opponent_sethp_arriving_second_confirms_rather_than_demotes(self):
        # when the OPPONENT used Pain Split, PS emits our (target) line first
        tag = "[from] move: Pain Split"
        sethp(self.battle, ["", "-sethp", "p1a: Jellicent", "217/403", tag, "[silent]"])
        sethp(self.battle, ["", "-sethp", "p2a: Slowking", "70/100", tag])
        self.assertEqual(self.battle.opponent.active.hp, 217)
        self.assertTrue(hc.is_exact(self.battle.opponent.active))

    def test_clamped_average_certifies_nothing(self):
        # `sethp` capped our side at max_hp, so our value is not the average
        tag = "[from] move: Pain Split"
        sethp(self.battle, ["", "-sethp", "p2a: Slowking", "70/100", tag, "[silent]"])
        sethp(self.battle, ["", "-sethp", "p1a: Jellicent", "403/403", tag])
        self.assertFalse(hc.is_exact(self.battle.opponent.active))


class TestSwitchEndsCertificates(unittest.TestCase):
    def setUp(self):
        self.battle = Battle(None)
        self.battle.battle_type = BattleType.RANDOM_BATTLE
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.user.active = Pokemon("luvdisc", 100)
        self.battle.opponent.active = Pokemon("slowking", 88)

    def test_switch_in_display_replaces_any_certificate(self):
        # HP can move off the field (Regenerator, a Wish landing on the
        # entrant) with no event to follow arithmetically
        bench = Pokemon("alomomola", 84)
        bench.max_hp = 429
        bench.max_hp_exact = True
        hc.certify(bench, 220, "test")
        self.battle.opponent.reserve.append(bench)
        switch(
            self.battle,
            ["", "switch", "p2a: Alomomola", "Alomomola, L84, F", "52/100"],
        )
        self.assertFalse(hc.is_exact(self.battle.opponent.active))

    def test_regenerator_heal_on_a_guessed_max_hp_drops_exactness(self):
        # synth08292: `floor(max_hp / 3)` is an exact delta only if max_hp is
        # the real one
        active = self.battle.opponent.active
        active.max_hp = 429
        active.max_hp_exact = False
        active.ability = "regenerator"
        hc.certify(active, 220, "test")
        self.battle.opponent.reserve.append(Pokemon("slowking", 88))
        switch(self.battle, ["", "switch", "p2a: Slowking", "Slowking, L88, M", "80/100"])
        self.assertFalse(hc.is_exact(active))


class TestApplyExactTeamHonoursCertificates(unittest.TestCase):
    """The sidecar corrects max HP; the checker must not rescale a certified
    absolute HP by a ratio of max HPs when it does."""

    def _lookup(self, hp):
        return {
            "slowking": {
                "species": "Slowking",
                "stats": {
                    "hp": hp,
                    "atk": 100,
                    "def": 182,
                    "spa": 270,
                    "spd": 200,
                    "spe": 103,
                },
            }
        }

    def _battler(self, max_hp):
        from fp.battle import Battler

        b = Battler()
        b.active = Pokemon("slowking", 88)
        b.active.max_hp = max_hp
        b.reserve = []
        return b

    def test_certified_hp_survives_the_max_hp_correction_verbatim(self):
        hc.reset_refusals()
        b = self._battler(300)
        hc.apply_display(b.active, 25)
        hc.certify(b.active, 75, "endeavor", shown_pct=25)
        apply_exact_team(b, self._lookup(310), is_user=False)
        self.assertEqual(b.active.max_hp, 310)
        self.assertEqual(b.active.hp, 75)
        self.assertTrue(hc.is_exact(b.active))
        self.assertEqual(hc.CERTIFICATE_REFUSALS, [])

    def test_uncertified_hp_is_re_derived_from_its_display_not_its_fraction(self):
        # `round(new_max_hp * hp_frac)` could land outside the band the protocol
        # stated -- that was APPROXIMATIONS U3's hard finding
        b = self._battler(300)
        hc.apply_display(b.active, 25)
        apply_exact_team(b, self._lookup(310), is_user=False)
        lo, hi = hc.display_bounds(25, 310)
        self.assertTrue(lo <= b.active.hp <= hi)
        self.assertFalse(hc.is_exact(b.active))

    def test_full_hp_follows_the_corrected_max(self):
        b = self._battler(300)
        hc.apply_display(b.active, 100)
        apply_exact_team(b, self._lookup(310), is_user=False)
        self.assertEqual(b.active.hp, 310)
        self.assertTrue(hc.is_exact(b.active))

    def test_a_stale_certificate_falls_back_to_the_fraction_rescale(self):
        # an HP written outside this module leaves the certificate vouching for
        # a value that no longer exists; the correction must not treat it as
        # exact, and must not re-evaluate a fraction against it either
        hc.reset_refusals()
        b = self._battler(300)
        b.active.hp = 150  # bypasses hp_certificate entirely
        apply_exact_team(b, self._lookup(310), is_user=False)
        self.assertEqual(b.active.max_hp, 310)
        self.assertFalse(hc.is_exact(b.active))
        self.assertEqual(b.active.hp, round(310 * (150 / 300)))
        self.assertEqual(hc.CERTIFICATE_REFUSALS, [])


if __name__ == "__main__":
    unittest.main()
