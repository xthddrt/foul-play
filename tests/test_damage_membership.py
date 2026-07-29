import unittest

import constants
from fp.battle import Battler, Pokemon
from fp.replay.damage_membership import (
    DamageEvent,
    PsCombatContext,
    PsTailParams,
    apply_exact_team,
    check_roll_membership,
    derive_ps_damage,
    extract_direct_damage_events,
    ps_apply_modifier,
    ps_chain_mods,
    ps_modifier_4096,
    ps_modify,
    ps_randomizer,
    ps_roll,
    ps_sixteen_rolls,
    _parse_hp_token,
)


class TestPsPrimitives(unittest.TestCase):
    """Hand-computed against sim/battle.ts:2302-2340 / :2388-2391."""

    def test_modifier_4096_truncates_like_ps(self):
        # tr(numerator * 4096 / denominator)
        self.assertEqual(ps_modifier_4096(1.5), 6144)
        self.assertEqual(ps_modifier_4096(2), 8192)
        self.assertEqual(ps_modifier_4096(2.25), 9216)
        self.assertEqual(ps_modifier_4096(0.5), 2048)
        self.assertEqual(ps_modifier_4096(0.75), 3072)
        # 1.3 * 4096 = 5324.8 -> 5324, which is why PS spells the Protosynthesis
        # / Tough Claws modifier as the explicit pair [5325, 4096]
        self.assertEqual(ps_modifier_4096(1.3), 5324)
        self.assertEqual(ps_modifier_4096(5325, 4096), 5325)

    def test_modify_rounds_half_down(self):
        # modify(v, m) = tr((tr(v*m) + 2047) / 4096): 65 * 1.5 = 97.5 -> 97
        self.assertEqual(ps_modify(65, 1.5), 97)
        self.assertEqual(ps_modify(100, 1.5), 150)
        self.assertEqual(ps_modify(3, 0.5), 1)  # 1.5 -> 1
        self.assertEqual(ps_modify(5, 0.5), 2)  # 2.5 -> 2 (half DOWN)
        self.assertEqual(ps_apply_modifier(101, 4096), 101)  # modify(v,1) == v

    def test_chain_rounds_half_up_and_is_order_sensitive(self):
        self.assertEqual(ps_chain_mods([]), 4096)
        self.assertEqual(ps_chain_mods([6144]), 6144)
        # (5324 * 6144 + 2048) >> 12 = 32712704 >> 12 = 7986
        self.assertEqual(ps_chain_mods([5324, 6144]), 7986)
        self.assertEqual(ps_chain_mods([6144, 5324]), 7986)
        # three modifiers: chain() is NOT associative under the >>12 rounding,
        # which is why the derivation refuses an ambiguous handler order
        self.assertEqual(ps_chain_mods([5325, 5325, 5324]), 8999)
        self.assertEqual(ps_chain_mods([5325, 5324, 5325]), 8998)

    def test_randomizer_endpoints(self):
        self.assertEqual(ps_randomizer(100, 0), 100)  # r=0 is the max roll
        self.assertEqual(ps_randomizer(100, 15), 85)
        self.assertEqual(ps_randomizer(97, 15), 82)  # tr(97*85/100) = 82


class TestPsExactTail(unittest.TestCase):
    """The per-roll tail (spec 1.1 R1-R9 / battle-actions.ts:1755-1841)."""

    def test_plain_tail_is_the_randomizer(self):
        rolls = ps_sixteen_rolls(100, PsTailParams(), crit=False)
        self.assertEqual(rolls, list(range(85, 101)))
        self.assertEqual(rolls[15], 100)  # ascending: [15] is the max roll

    def test_stab_then_supereffective(self):
        # r=0: 100 -> modify(100,1.5)=150 -> *2 = 300
        # r=15: 85 -> modify(85,1.5)=127 (127.5 rounds half DOWN) -> *2 = 254
        params = PsTailParams(stab_4096=6144, type_levels=1)
        rolls = ps_sixteen_rolls(100, params, crit=False)
        self.assertEqual(rolls[15], 300)
        self.assertEqual(rolls[0], 254)

    def test_resist_is_a_floor_halving_per_level(self):
        # tr(d/2) per level, NOT one float multiply
        params = PsTailParams(type_levels=-1)
        self.assertEqual(ps_roll(101, 0, params, False), 50)
        params2 = PsTailParams(type_levels=-2)
        self.assertEqual(ps_roll(101, 0, params2, False), 25)

    def test_burn_and_final_modifier_truncate_separately(self):
        # 100 -> burn modify(0.5) = 50 -> Life Orb [5324,4096] = 65
        params = PsTailParams(burn=True, final_4096_noncrit=5324)
        self.assertEqual(ps_roll(100, 0, params, False), 65)

    def test_min_one_before_16_bit_truncation(self):
        self.assertEqual(ps_roll(0, 0, PsTailParams(), False), 1)
        # tr(d, 16) runs AFTER the min-1 check and can legally return 0
        self.assertEqual(ps_roll(65536, 0, PsTailParams(), False), 0)
        self.assertEqual(ps_roll(65537, 0, PsTailParams(), False), 1)

    def test_no_op_modifier_is_bit_identical(self):
        for base in (1, 7, 63, 100, 4095, 4096, 12345):
            self.assertEqual(
                ps_roll(base, 3, PsTailParams(stab_4096=4096), False),
                max(1, ps_randomizer(base, 3)),  # R8's min-1 still applies
            )


class TestRollMembership(unittest.TestCase):
    ROLLS = ps_sixteen_rolls(100, PsTailParams(), crit=False)  # 85..100

    def test_member(self):
        member, delta = check_roll_membership(self.ROLLS, 92, lethal=False)
        self.assertTrue(member)
        self.assertEqual(delta, 0)

    def test_non_member_above(self):
        member, delta = check_roll_membership(self.ROLLS, 103, lethal=False)
        self.assertFalse(member)
        self.assertEqual(delta, 3)

    def test_non_member_below(self):
        member, delta = check_roll_membership(self.ROLLS, 80, lethal=False)
        self.assertFalse(member)
        self.assertEqual(delta, -5)

    def test_non_member_gap(self):
        # roll set with a hole: 51..60ish for max 60 skips some values
        rolls = ps_sixteen_rolls(200, PsTailParams(), crit=False)  # 170,172,...
        self.assertNotIn(171, rolls)
        member, delta = check_roll_membership(rolls, 171, lethal=False)
        self.assertFalse(member)
        self.assertEqual(abs(delta), 1)

    def test_lethal_is_lower_bound(self):
        # defender had 90 HP; any roll >= 90 satisfies the capped observation
        member, delta = check_roll_membership(self.ROLLS, 90, lethal=True)
        self.assertTrue(member)
        self.assertEqual(delta, 0)

    def test_lethal_beyond_max_roll_fails(self):
        member, delta = check_roll_membership(self.ROLLS, 120, lethal=True)
        self.assertFalse(member)
        self.assertEqual(delta, 20)


class TestParseHp(unittest.TestCase):
    def test_exact(self):
        self.assertEqual(_parse_hp_token("199/269"), (199, 269, False))

    def test_with_status(self):
        self.assertEqual(_parse_hp_token("97/252 brn"), (97, 252, False))

    def test_faint(self):
        self.assertEqual(_parse_hp_token("0 fnt"), (0, None, True))

    def test_garbage(self):
        self.assertEqual(_parse_hp_token("[silent]"), (None, None, False))


def _extract(lines, **kw):
    args = dict(
        user_pid="p1",
        user_species="reuniclus",
        opp_species="gardevoir",
        user_hp=337,
        user_max_hp=337,
    )
    args.update(kw)
    return extract_direct_damage_events(lines, **args)


class TestExtraction(unittest.TestCase):
    def test_in_scope_direct_hit_with_crit(self):
        events, counters, side = _extract(
            [
                "|move|p2a: Gardevoir|Moonblast|p1a: Reuniclus",
                "|-crit|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|214/337",
            ]
        )
        self.assertEqual(len(events), 1)
        ev = events[0]
        self.assertIsNone(ev.exclusion)
        self.assertTrue(ev.crit)
        self.assertEqual(ev.delta, 337 - 214)
        self.assertFalse(ev.lethal)
        self.assertEqual(side["first_actor"], "p2a")

    def test_from_tagged_damage_is_not_a_candidate_but_updates_tracking(self):
        events, counters, _ = _extract(
            [
                "|move|p1a: Reuniclus|Psyshock|p2a: Gardevoir",
                "|-damage|p1a: Reuniclus|303/337|[from] item: Life Orb",
                "|move|p2a: Gardevoir|Moonblast|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|214/337",
            ]
        )
        hits = [e for e in events if e.defender_slot == "p1a"]
        self.assertEqual(len(hits), 1)
        # prev HP picked up the Life Orb chip: 303, not 337
        self.assertEqual(hits[0].delta, 303 - 214)

    def test_multihit_excluded_by_double_damage(self):
        events, counters, _ = _extract(
            [
                "|move|p2a: Gardevoir|Moonblast|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|300/337",
                "|-damage|p1a: Reuniclus|270/337",
            ]
        )
        # constant-BP multi-hit is checkable per hit under exact derivation:
        # PS reruns the whole chain per hit, so each delta must be in the same
        # 16-value set
        self.assertEqual(len(events), 2)
        self.assertTrue(all(e.exclusion is None for e in events))

    def test_multihit_excluded_by_move_json(self):
        events, _, _ = _extract(
            [
                "|move|p2a: Gardevoir|Bullet Seed|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|300/337",
            ]
        )
        self.assertIsNone(events[0].exclusion)

    def test_variable_bp_multihit_is_indexed_from_the_hitcount(self):
        # Triple Axel's base power escalates 20/40/60 across its hits, so each
        # hit needs its own derivation -- keyed on PS's `move.hit`, which the
        # |-hitcount| line pins down when every executed hit produced a -damage
        events, _, _ = _extract(
            [
                "|move|p2a: Gardevoir|Triple Axel|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|320/337",
                "|-damage|p1a: Reuniclus|300/337",
                "|-hitcount|p1a: Reuniclus|2",
            ]
        )
        self.assertTrue(all(e.exclusion is None for e in events))
        self.assertEqual([e.hit_index for e in events], [1, 2])

    def test_hitcount_is_read_across_the_faint_line(self):
        # PS runs faintMessages() BEFORE printing |-hitcount|, and the target
        # token loses its position letter once it has fainted
        # (sim/battle-actions.ts:975-978) -- synth37888 T1:
        #   |-damage|p1a: Gengar|0 fnt / |faint|p1a: Gengar / |-hitcount|p1: Gengar|3
        events, _, _ = _extract(
            [
                "|move|p2a: Gardevoir|Triple Axel|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|320/337",
                "|-damage|p1a: Reuniclus|300/337",
                "|-damage|p1a: Reuniclus|0 fnt",
                "|faint|p1a: Reuniclus",
                "|-hitcount|p1: Reuniclus|3",
            ]
        )
        self.assertEqual([e.hit_index for e in events], [1, 2, 3])

    def test_substitute_absorbed_hit_leaves_the_index_ambiguous(self):
        # 3 hits executed, only 1 reached the mon: the -damage line could be
        # hit 2 or hit 3, so the index (and every BP derived from it) refuses
        events, _, _ = _extract(
            [
                "|move|p2a: Gardevoir|Triple Axel|p1a: Reuniclus",
                "|-activate|p1a: Reuniclus|move: Substitute|[damage]",
                "|-end|p1a: Reuniclus|Substitute",
                "|-damage|p1a: Reuniclus|300/337",
                "|-hitcount|p1a: Reuniclus|3",
            ]
        )
        self.assertEqual([e.hit_index for e in events], [None])

    def test_single_hit_move_is_hit_one(self):
        events, _, _ = _extract(
            [
                "|move|p2a: Gardevoir|Moonblast|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|237/337",
            ]
        )
        self.assertEqual(events[0].hit_index, 1)

    def test_derivable_fixed_damage_stays_in_scope(self):
        # Seismic Toss / Super Fang have no roll set but ARE exact
        events, _, _ = _extract(
            [
                "|move|p2a: Gardevoir|Seismic Toss|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|237/337",
            ]
        )
        self.assertIsNone(events[0].exclusion)

    def test_undecidable_fixed_damage_still_excluded(self):
        # Final Gambit reads the ATTACKER's hp, which is /100-quantised
        events, _, _ = _extract(
            [
                "|move|p2a: Gardevoir|Final Gambit|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|237/337",
            ]
        )
        self.assertEqual(events[0].exclusion, "fixed_damage")

    def test_faint_lines_accumulate_side_totals(self):
        # PS increments side.totalFainted in the same branch that emits |faint|
        # (sim/battle.ts:2549-2551)
        events, _, _ = _extract(
            [
                "|move|p1a: Reuniclus|Psyshock|p2a: Gardevoir",
                "|-damage|p2a: Gardevoir|0 fnt",
                "|faint|p2a: Gardevoir",
                "|switch|p2a: Gengar|Gengar, L81, F|100/100",
                "|move|p2a: Gengar|Shadow Ball|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|237/337",
            ]
        )
        hit = [e for e in events if e.defender_slot == "p1a"][0]
        self.assertEqual(hit.attacker_side_faints, 1)

    def test_boost_before_hit_confounds(self):
        events, _, _ = _extract(
            [
                "|move|p1a: Reuniclus|Calm Mind|p1a: Reuniclus",
                "|-boost|p1a: Reuniclus|spa|1",
                "|-boost|p1a: Reuniclus|spd|1",
                "|move|p2a: Gardevoir|Moonblast|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|250/337",
            ]
        )
        self.assertEqual(events[0].exclusion, "confounded_boost")

    def test_boost_after_hit_does_not_confound(self):
        events, _, _ = _extract(
            [
                "|move|p2a: Gardevoir|Moonblast|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|250/337",
                "|-unboost|p1a: Reuniclus|spa|1",
            ]
        )
        self.assertIsNone(events[0].exclusion)

    def test_opponent_defender_is_fraction_limited(self):
        events, _, _ = _extract(
            [
                "|move|p1a: Reuniclus|Psyshock|p2a: Gardevoir",
                "|-damage|p2a: Gardevoir|55/100",
            ]
        )
        self.assertEqual(events[0].exclusion, "fraction_limited")

    def test_pivot_switch_in_hit_excluded(self):
        events, _, _ = _extract(
            [
                "|move|p1a: Reuniclus|U-turn|p2a: Gardevoir",
                "|-damage|p2a: Gardevoir|80/100",
                "|switch|p1a: Palkia|Palkia, L75|259/259",
                "|move|p2a: Gardevoir|Moonblast|p1a: Palkia",
                "|-damage|p1a: Palkia|180/259",
            ]
        )
        hit = [e for e in events if e.defender_slot == "p1a"][0]
        self.assertEqual(hit.exclusion, "combatant_not_turnstart")

    def test_self_cost_damage_not_a_candidate(self):
        events, counters, _ = _extract(
            [
                "|move|p1a: Reuniclus|Substitute|p1a: Reuniclus",
                "|-start|p1a: Reuniclus|Substitute",
                "|-damage|p1a: Reuniclus|253/337",
            ]
        )
        self.assertEqual(len(events), 0)
        self.assertEqual(counters["self_damage"], 1)

    def test_lethal_hit_flagged(self):
        events, _, _ = _extract(
            [
                "|move|p2a: Gardevoir|Moonblast|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|0 fnt",
                "|faint|p1a: Reuniclus",
            ]
        )
        self.assertIsNone(events[0].exclusion)
        self.assertTrue(events[0].lethal)
        self.assertEqual(events[0].delta, 337)

    def test_defender_roost_before_hit_excluded(self):
        events, _, _ = _extract(
            [
                "|move|p1a: Reuniclus|Roost|p1a: Reuniclus",
                "|-heal|p1a: Reuniclus|337/337",
                "|move|p2a: Gardevoir|Moonblast|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|250/337",
            ]
        )
        self.assertEqual(events[0].exclusion, "defender_silent_volatile")

    def test_typechange_before_hit_confounds(self):
        # Libero/Protean: the defender's typing changed mid-turn, so the
        # pre-turn state computes effectiveness with stale types
        events, _, _ = _extract(
            [
                "|move|p1a: Reuniclus|Psyshock|p2a: Gardevoir",
                "|-start|p1a: Reuniclus|typechange|Psychic|[from] ability: Protean",
                "|-damage|p2a: Gardevoir|60/100",
                "|move|p2a: Gardevoir|Moonblast|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|250/337",
            ]
        )
        hit = [e for e in events if e.defender_slot == "p1a"][0]
        self.assertEqual(hit.exclusion, "confounded_typechange")

    def test_hp_desync_on_wrong_max(self):
        events, _, _ = _extract(
            [
                "|move|p2a: Gardevoir|Moonblast|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|250/300",
            ]
        )
        self.assertEqual(events[0].exclusion, "hp_desync")


class TestApplyExactTeam(unittest.TestCase):
    def _lookup(self):
        return {
            "reuniclus": {
                "species": "Reuniclus",
                "ability": "Magic Guard",
                "item": "Life Orb",
                "moves": ["calmmind", "recover", "psyshock", "focusblast"],
                "teraType": "Steel",
                "evs": {"hp": 85, "atk": 0, "def": 85, "spa": 85, "spd": 85, "spe": 85},
                "ivs": {"hp": 31, "atk": 0, "def": 31, "spa": 31, "spd": 31, "spe": 31},
                "stats": {
                    "hp": 337,
                    "atk": 100,
                    "def": 182,
                    "spa": 270,
                    "spd": 200,
                    "spe": 103,
                },
            }
        }

    def _battler(self):
        b = Battler()
        b.active = Pokemon("reuniclus", 88)
        b.reserve = []
        return b

    def test_opponent_unknowns_filled_and_stats_overridden(self):
        b = self._battler()
        pkmn = b.active
        pkmn.hp = pkmn.max_hp // 2  # half HP, tracked in reconstructed units
        apply_exact_team(b, self._lookup(), is_user=False)
        self.assertEqual(pkmn.ability, "magicguard")
        self.assertEqual(pkmn.item, "lifeorb")
        self.assertEqual(pkmn.tera_type, "steel")
        self.assertEqual(pkmn.max_hp, 337)
        self.assertEqual(pkmn.stats[constants.SPECIAL_ATTACK], 270)
        self.assertEqual(pkmn.stats[constants.ATTACK], 100)
        # HP fraction preserved under the exact max
        self.assertEqual(pkmn.hp, round(337 * 0.5))
        self.assertEqual(len(pkmn.moves), 4)
        self.assertEqual(pkmn.evs, (85, 0, 85, 85, 85, 85))

    def test_known_fields_not_clobbered(self):
        b = self._battler()
        pkmn = b.active
        pkmn.ability = "levitate"  # e.g. skill-swapped: tracking is authoritative
        pkmn.item = None  # knocked off
        apply_exact_team(b, self._lookup(), is_user=False)
        self.assertEqual(pkmn.ability, "levitate")
        self.assertIsNone(pkmn.item)

    def test_user_side_only_fills_tera_type(self):
        b = self._battler()
        pkmn = b.active
        old_stats = dict(pkmn.stats)
        apply_exact_team(b, self._lookup(), is_user=True)
        self.assertEqual(pkmn.tera_type, "steel")
        self.assertIsNone(pkmn.ability)
        self.assertEqual(pkmn.stats, old_stats)
        self.assertEqual(pkmn.moves, [])

    def test_moves_capped_at_four(self):
        b = self._battler()
        pkmn = b.active
        pkmn.add_move("thunderbolt")
        pkmn.add_move("icebeam")
        pkmn.add_move("shadowball")
        apply_exact_team(b, self._lookup(), is_user=False)
        self.assertEqual(len(pkmn.moves), 4)
        self.assertEqual(pkmn.moves[3].name, "calmmind")

    def test_transformed_mon_stats_untouched(self):
        b = self._battler()
        pkmn = b.active
        pkmn.transformed_into = "gardevoir"
        old_max = pkmn.max_hp
        apply_exact_team(b, self._lookup(), is_user=False)
        self.assertEqual(pkmn.max_hp, old_max)


class TestDamageEventDelta(unittest.TestCase):
    def test_delta(self):
        ev = DamageEvent(
            line_index=0,
            ctx_id=1,
            attacker_slot="p2a",
            defender_slot="p1a",
            move="moonblast",
            crit=False,
            prev_hp=337,
            new_hp=214,
            shown_max=337,
            lethal=False,
        )
        self.assertEqual(ev.delta, 123)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------
# Fix wave: itemless sidecar fill, Fickle Beam both-arm membership, Charge /
# Rage Fist confounders, Avalanche / Stomping Tantrum scope bucket
# ---------------------------------------------------------------------------
from fp.replay.damage_membership import ficklebeam_membership


class TestItemlessSidecarFill(unittest.TestCase):
    def _lookup_itemless(self):
        return {
            "jumpluff": {
                "species": "Jumpluff",
                "ability": "Infiltrator",
                "item": "",  # itemless set: knowledge, not ignorance
                "moves": ["encore", "sleeppowder", "acrobatics", "strengthsap"],
            }
        }

    def test_empty_item_fills_none(self):
        # synth00344: itemless Jumpluff's Acrobatics doubles (engine
        # Items::NONE); UNKNOWN_ITEM must not survive the exact-team override
        b = Battler()
        b.active = Pokemon("jumpluff", 87)
        b.reserve = []
        self.assertEqual(b.active.item, constants.UNKNOWN_ITEM)
        apply_exact_team(b, self._lookup_itemless(), is_user=False)
        self.assertIsNone(b.active.item)

    def test_missing_item_key_stays_unknown(self):
        lookup = self._lookup_itemless()
        del lookup["jumpluff"]["item"]
        b = Battler()
        b.active = Pokemon("jumpluff", 87)
        b.reserve = []
        apply_exact_team(b, lookup, is_user=False)
        self.assertEqual(b.active.item, constants.UNKNOWN_ITEM)

    def test_real_item_still_fills(self):
        lookup = self._lookup_itemless()
        lookup["jumpluff"]["item"] = "Heavy-Duty Boots"
        b = Battler()
        b.active = Pokemon("jumpluff", 87)
        b.reserve = []
        apply_exact_team(b, lookup, is_user=False)
        self.assertEqual(b.active.item, "heavydutyboots")

    def test_protocol_tracked_item_not_clobbered(self):
        # an item already tracked from the protocol (e.g. knocked-off -> None,
        # or revealed) is authoritative; the sidecar fill is fill-if-unknown
        b = Battler()
        b.active = Pokemon("jumpluff", 87)
        b.active.item = "sitrusberry"
        b.reserve = []
        apply_exact_team(b, self._lookup_itemless(), is_user=False)
        self.assertEqual(b.active.item, "sitrusberry")


def _mon(name, level, stats, ability, item=None, types=None):
    pkmn = Pokemon(name, level)
    pkmn.stats = {
        constants.ATTACK: stats[0],
        constants.DEFENSE: stats[1],
        constants.SPECIAL_ATTACK: stats[2],
        constants.SPECIAL_DEFENSE: stats[3],
        constants.SPEED: stats[4],
    }
    pkmn.ability = ability
    pkmn.item = item
    if types is not None:
        pkmn.types = list(types)
    return pkmn


class TestPsExactDerivation(unittest.TestCase):
    """End-to-end derivation against hand-computed PS arithmetic."""

    def test_neutral_physical_hit(self):
        # Scrafty L83 (Atk 197) Knock Off vs Slowking-Galar (Def 185) holding
        # Leftovers, from synth02302 T37.
        #   BP:   modify(65, 1.5) = tr((65*6144 + 2047)/4096) = 97   [Knock Off]
        #   base: tr(tr(tr(tr(2*83/5 + 2) * 97 * 197)/185)/50)
        #       = tr(tr(tr(35 * 97 * 197)/185)/50) = tr(tr(668815/185)/50)
        #       = tr(3615/50) = 72, +2 = 74
        #   STAB: Dark on a Dark/Fighting attacker -> 1.5
        #   type: Dark vs Poison = 1x, Dark vs Psychic = 2x -> typeMod +1
        #   max roll: r=0 -> 74 -> modify(74,1.5) = 111 (111.5 half-down) -> 222
        attacker = _mon("scrafty", 83, (197, 239, 122, 239, 144), "shedskin", "leftovers")
        defender = _mon("slowkinggalar", 85, (115, 185, 236, 236, 100), "regenerator", "leftovers")
        ps = derive_ps_damage(attacker, defender, "knockoff", PsCombatContext())
        self.assertEqual(ps.base_power, 97)
        self.assertEqual(ps.attack, 197)
        self.assertEqual(ps.defense, 185)
        self.assertEqual(ps.base_noncrit, 74)
        self.assertEqual(ps.base_crit, 111)  # tr(74 * 1.5)
        self.assertEqual(ps.params.stab_4096, 6144)
        self.assertEqual(ps.params.type_levels, 1)
        rolls = ps.rolls(crit=False)
        self.assertEqual(len(rolls), 16)
        self.assertEqual(rolls[15], 222)
        self.assertEqual(rolls[0], 2 * ps_modify(ps_randomizer(74, 15), 1.5))
        self.assertEqual(rolls, sorted(rolls))

    def test_knock_off_boost_needs_a_removable_item(self):
        attacker = _mon("scrafty", 83, (197, 239, 122, 239, 144), "shedskin", "leftovers")
        bare = _mon("slowkinggalar", 85, (115, 185, 236, 236, 100), "regenerator", None)
        ps = derive_ps_damage(attacker, bare, "knockoff", PsCombatContext())
        self.assertEqual(ps.base_power, 65)

    def test_itemless_acrobatics_doubles_ps_base_power(self):
        # PS's Acrobatics is 55 BP with a callback that doubles it when the user
        # holds nothing (foul-play's moves.json stores the post-double 110)
        attacker = _mon("jumpluff", 87, (150, 150, 150, 150, 250), "infiltrator", None)
        defender = _mon("heracross", 84, (200, 150, 100, 150, 150), "guts", "leftovers")
        ps = derive_ps_damage(attacker, defender, "acrobatics", PsCombatContext())
        self.assertEqual(ps.base_power, 110)
        held = _mon("jumpluff", 87, (150, 150, 150, 150, 250), "infiltrator", "sitrusberry")
        self.assertEqual(
            derive_ps_damage(held, defender, "acrobatics", PsCombatContext()).base_power, 55
        )

    def test_engine_none_item_string_is_no_item(self):
        # battle_to_poke_engine_state mutates an itemless mon's item to "None"
        attacker = _mon("jumpluff", 87, (150, 150, 150, 150, 250), "infiltrator", "None")
        defender = _mon("heracross", 84, (200, 150, 100, 150, 150), "guts", "None")
        self.assertEqual(
            derive_ps_damage(attacker, defender, "acrobatics", PsCombatContext()).base_power,
            110,
        )

    def test_technician_gates_on_raw_base_power(self):
        attacker = _mon("scyther", 82, (228, 178, 137, 178, 219), "technician", None)
        defender = _mon("heracross", 84, (200, 150, 100, 150, 150), "guts", None)
        ps = derive_ps_damage(attacker, defender, "bugbite", PsCombatContext())
        self.assertEqual(ps.base_power, 90)  # modify(60, 1.5)

    def test_reflect_halves_only_the_non_crit_arm(self):
        attacker = _mon("scrafty", 83, (197, 239, 122, 239, 144), "shedskin", None)
        defender = _mon("slowkinggalar", 85, (115, 185, 236, 236, 100), "regenerator", None)
        ctx = PsCombatContext(defender_screens=frozenset((constants.REFLECT,)))
        ps = derive_ps_damage(attacker, defender, "knockoff", ctx)
        self.assertEqual(ps.params.final_4096_noncrit, 2048)
        self.assertEqual(ps.params.final_4096_crit, 4096)

    def test_unmodelled_effects_refuse_rather_than_guess(self):
        from fp.replay.damage_membership import PsRefusal

        attacker = _mon("scrafty", 83, (197, 239, 122, 239, 144), "rivalry", None)
        defender = _mon("slowkinggalar", 85, (115, 185, 236, 236, 100), "regenerator", None)
        with self.assertRaises(PsRefusal):
            derive_ps_damage(attacker, defender, "knockoff", PsCombatContext())
        unknown_ability = _mon("scrafty", 83, (197, 239, 122, 239, 144), None, None)
        with self.assertRaises(PsRefusal):
            derive_ps_damage(unknown_ability, defender, "knockoff", PsCombatContext())


class TestExactTeamOverride(unittest.TestCase):
    def _lookup(self):
        return {
            "eiscue": {
                "species": "Eiscue",
                "ability": "Ice Face",
                "item": "Leftovers",
                "moves": ["liquidation", "zenheadbutt", "iceshard", "substitute"],
                "evs": {"hp": 81, "atk": 85, "def": 85, "spa": 85, "spd": 85, "spe": 85},
                "ivs": {"hp": 31, "atk": 31, "def": 31, "spa": 31, "spd": 31, "spe": 31},
                "stats": {"hp": 274, "atk": 219, "def": 165, "spa": 121, "spd": 165, "spe": 218},
            }
        }

    def test_evs_and_ivs_applied_after_a_forme_change(self):
        # the old guard skipped the whole block once forme_changed was set, so
        # a broken Ice Face reverted the mon to fp's default 85-EV spread
        # (max_hp 275 instead of the true 274) for the rest of the battle
        b = Battler()
        b.active = Pokemon("eiscue", 88)
        b.reserve = []
        b.active.forme_changed = True
        apply_exact_team(b, self._lookup(), is_user=False)
        self.assertEqual(b.active.evs[0], 81)
        self.assertEqual(b.active.ivs[0], 31)

    def test_forme_change_recomputes_stats_from_the_live_forme(self):
        # stats follow the CURRENT forme's base stats (so a forme whose base HP
        # genuinely differs keeps its real max HP), but with the exact EVs/IVs
        b = Battler()
        b.active = Pokemon("eiscue", 88)
        b.reserve = []
        b.active.forme_changed = True
        apply_exact_team(b, self._lookup(), is_user=False)
        self.assertEqual(b.active.max_hp, 274)

    def test_base_forme_uses_the_sidecar_stats_verbatim(self):
        b = Battler()
        b.active = Pokemon("eiscue", 88)
        b.reserve = []
        apply_exact_team(b, self._lookup(), is_user=False)
        self.assertEqual(b.active.max_hp, 274)
        self.assertEqual(b.active.stats[constants.ATTACK], 219)

    def test_inferred_item_loses_to_the_sidecar(self):
        # fp guesses Heavy-Duty Boots from "took no Stealth Rock damage"; the
        # sidecar knows it is a Choice Band, which is a 1.5x Atk difference
        b = Battler()
        b.active = Pokemon("komala", 87)
        b.reserve = []
        b.active.item = "heavydutyboots"
        b.active.item_inferred = True
        apply_exact_team(
            b,
            {"komala": {"species": "Komala", "ability": "Comatose", "item": "Choice Band"}},
            is_user=False,
        )
        self.assertEqual(b.active.item, "choiceband")
        self.assertFalse(b.active.item_inferred)

    def test_protocol_removal_beats_the_sidecar(self):
        b = Battler()
        b.active = Pokemon("komala", 87)
        b.reserve = []
        b.active.item = None
        b.active.item_inferred = True
        b.active.knocked_off = True
        apply_exact_team(
            b,
            {"komala": {"species": "Komala", "ability": "Comatose", "item": "Choice Band"}},
            is_user=False,
        )
        self.assertIsNone(b.active.item)


class TestFickleBeamBothArms(unittest.TestCase):
    """PS doubles Fickle Beam's BASE POWER, not its damage: the doubled arm has
    to be a second derivation (2 * damage != damage(2 * bp))."""

    def _mons(self):
        attacker = _mon("drampa", 83, (150, 150, 220, 150, 100), "berserk", None)
        defender = _mon("heracross", 84, (200, 150, 100, 150, 150), "guts", None)
        return attacker, defender

    def test_doubled_arm_is_bp_doubling_not_damage_doubling(self):
        from fp.replay.damage_membership import derive_ps_damage as d

        attacker, defender = self._mons()
        base = d(attacker, defender, "ficklebeam", PsCombatContext())
        doubled = d(attacker, defender, "ficklebeam", PsCombatContext(), ficklebeam_doubled=True)
        self.assertEqual(base.base_power, 80)
        self.assertEqual(doubled.base_power, 160)
        # the engine's `folded * 2` model would predict exactly 2x here
        self.assertNotEqual(doubled.rolls(False), [2 * r for r in base.rolls(False)])

    def test_arm_selection(self):
        from fp.replay.damage_membership import ficklebeam_membership

        attacker, defender = self._mons()
        ctx = PsCombatContext()
        base_rolls = derive_ps_damage(attacker, defender, "ficklebeam", ctx).rolls(False)
        doubled_rolls = derive_ps_damage(
            attacker, defender, "ficklebeam", ctx, ficklebeam_doubled=True
        ).rolls(False)
        member, nearest, rolls, arm = ficklebeam_membership(
            attacker, defender, ctx, base_rolls[7], lethal=False, crit=False
        )
        self.assertTrue(member)
        self.assertEqual(arm, "base")
        member, nearest, rolls, arm = ficklebeam_membership(
            attacker, defender, ctx, doubled_rolls[3], lethal=False, crit=False
        )
        self.assertTrue(member)
        self.assertEqual(arm, "doubled")
        between = (base_rolls[15] + doubled_rolls[0]) // 2
        member, nearest, rolls, arm = ficklebeam_membership(
            attacker, defender, ctx, between, lethal=False, crit=False
        )
        self.assertFalse(member)
        self.assertIsNone(arm)


class TestChargeConfounder(unittest.TestCase):
    def test_charge_before_hit_excluded(self):
        # Bellibolt gains Charge (Electromorphosis) from the user's hit, then
        # its Electric attack lands doubled relative to the pre-turn state
        events, counters, _ = _extract(
            [
                "|move|p1a: Reuniclus|Psyshock|p2a: Bellibolt",
                "|-damage|p2a: Bellibolt|60/100",
                "|-start|p2a: Bellibolt|Charge|Psyshock|[from] ability: Electromorphosis",
                "|move|p2a: Bellibolt|Parabolic Charge|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|250/337",
            ],
            opp_species="bellibolt",
        )
        hits = [e for e in events if e.defender_slot == "p1a"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].exclusion, "confounded_charge")

    def test_charge_after_hit_not_excluded(self):
        # the attacker's hit resolved BEFORE it gained Charge -> in scope
        events, counters, _ = _extract(
            [
                "|move|p2a: Bellibolt|Parabolic Charge|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|250/337",
                "|move|p1a: Reuniclus|Psyshock|p2a: Bellibolt",
                "|-damage|p2a: Bellibolt|60/100",
                "|-start|p2a: Bellibolt|Charge|Psyshock|[from] ability: Electromorphosis",
            ],
            opp_species="bellibolt",
        )
        hits = [e for e in events if e.defender_slot == "p1a"]
        self.assertEqual(len(hits), 1)
        self.assertIsNone(hits[0].exclusion)

    def test_charge_on_other_slot_does_not_confound(self):
        # Charge gained by the DEFENDER is irrelevant to the attacker's hit
        events, counters, _ = _extract(
            [
                "|move|p2a: Gardevoir|Thunderbolt|p1a: Bellibolt",
                "|-start|p1a: Bellibolt|Charge|Thunderbolt|[from] ability: Electromorphosis",
                "|-damage|p1a: Bellibolt|250/337",
            ],
            user_species="bellibolt",
        )
        hits = [e for e in events if e.defender_slot == "p1a"]
        self.assertEqual(len(hits), 1)
        self.assertIsNone(hits[0].exclusion)


class TestRageFistConfounder(unittest.TestCase):
    def test_prior_hit_excludes_ragefist(self):
        # the user hits Annihilape first: timesAttacked increments mid-turn
        # (sim/battle-actions.ts:993-995), so Rage Fist BP (data/moves.ts:14583)
        # is higher than the pre-turn state models
        events, counters, _ = _extract(
            [
                "|move|p1a: Reuniclus|Psyshock|p2a: Annihilape",
                "|-damage|p2a: Annihilape|70/100",
                "|move|p2a: Annihilape|Rage Fist|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|230/337",
            ],
            opp_species="annihilape",
        )
        hits = [e for e in events if e.defender_slot == "p1a"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].exclusion, "confounded_timesattacked")

    def test_ragefist_without_prior_hit_in_scope(self):
        events, counters, _ = _extract(
            [
                "|move|p2a: Annihilape|Rage Fist|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|230/337",
            ],
            opp_species="annihilape",
        )
        hits = [e for e in events if e.defender_slot == "p1a"]
        self.assertEqual(len(hits), 1)
        self.assertIsNone(hits[0].exclusion)

    def test_sub_absorbed_prior_hit_excludes_ragefist(self):
        # a hit absorbed by the Substitute still increments timesAttacked
        events, counters, _ = _extract(
            [
                "|move|p1a: Reuniclus|Psyshock|p2a: Annihilape",
                "|-activate|p2a: Annihilape|move: Substitute|[damage]",
                "|move|p2a: Annihilape|Rage Fist|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|230/337",
            ],
            opp_species="annihilape",
        )
        hits = [e for e in events if e.defender_slot == "p1a"]
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0].exclusion, "confounded_timesattacked")


class TestEphemeralScopeBucket(unittest.TestCase):
    def test_avalanche_excluded_by_scope(self):
        events, counters, _ = _extract(
            [
                "|move|p2a: Beartic|Avalanche|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|230/337",
            ],
            opp_species="beartic",
        )
        self.assertEqual(events[0].exclusion, "scope_ephemeral_state")

    def test_stomping_tantrum_excluded_by_scope(self):
        events, counters, _ = _extract(
            [
                "|move|p2a: Mudsdale|Stomping Tantrum|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|230/337",
            ],
            opp_species="mudsdale",
        )
        self.assertEqual(events[0].exclusion, "scope_ephemeral_state")


class TestCollisionCourseElectroDrift(unittest.TestCase):
    """data/moves.ts:2634-2640 (Collision Course) / :4620-4626 (Electro Drift):

        onBasePower(bp, source, target, move) {
          if (target.runEffectiveness(move) > 0) return this.chainModify([5461, 4096]);
        }

    chain: acc = (4096*5461 + 2048) >> 12 = 5461
    finalModify: modify(100, 5461) = tr((100*5461 + 2047)/4096)
                                   = tr(548147/4096) = 133
    """

    def _atk(self):
        return _mon("scrafty", 83, (197, 239, 122, 239, 144), "shedskin", None)

    def test_collision_course_boosts_on_super_effective(self):
        se = _mon("heracross", 84, (200, 150, 100, 150, 150), "guts", None, types=["normal"])
        ps = derive_ps_damage(self._atk(), se, "collisioncourse", PsCombatContext())
        self.assertEqual(ps.base_power, 133)
        self.assertEqual(ps.params.type_levels, 1)

    def test_collision_course_unchanged_on_neutral(self):
        neutral = _mon("heracross", 84, (200, 150, 100, 150, 150), "guts", None, types=["dragon"])
        ps = derive_ps_damage(self._atk(), neutral, "collisioncourse", PsCombatContext())
        self.assertEqual(ps.base_power, 100)
        self.assertEqual(ps.params.type_levels, 0)

    def test_collision_course_unchanged_on_resisted(self):
        resist = _mon("heracross", 84, (200, 150, 100, 150, 150), "guts", None, types=["flying"])
        ps = derive_ps_damage(self._atk(), resist, "collisioncourse", PsCombatContext())
        self.assertEqual(ps.base_power, 100)
        self.assertEqual(ps.params.type_levels, -1)

    def test_electro_drift_boosts_on_super_effective(self):
        # Electric vs Water = 2x
        attacker = _mon("miraidon", 78, (150, 150, 250, 150, 200), "hadronengine", None)
        se = _mon("heracross", 84, (200, 150, 100, 150, 150), "guts", None, types=["water"])
        ps = derive_ps_damage(attacker, se, "electrodrift", PsCombatContext())
        self.assertEqual(ps.base_power, 133)
        neutral = _mon("heracross", 84, (200, 150, 100, 150, 150), "guts", None, types=["normal"])
        self.assertEqual(
            derive_ps_damage(attacker, neutral, "electrodrift", PsCombatContext()).base_power,
            100,
        )

    def test_double_super_effective_still_chains_once(self):
        # runEffectiveness sums the levels; the handler tests `> 0`, so a 4x hit
        # gets the SAME single chainModify
        quad = _mon("heracross", 84, (200, 150, 100, 150, 150), "guts", None, types=["normal", "rock"])
        ps = derive_ps_damage(self._atk(), quad, "collisioncourse", PsCombatContext())
        self.assertEqual(ps.base_power, 133)
        self.assertEqual(ps.params.type_levels, 2)


class TestHydroSteamWeather(unittest.TestCase):
    """data/conditions.ts:556-569 -- sunnyday's onWeatherModifyDamage returns
    chainModify(1.5) for hydrosteam BEFORE it reaches the Water-suppress branch.

    L82 attacker, 80 BP, SpA 250 vs SpD 200:
      tr(2*82/5 + 2) = 34
      tr(tr(tr(34 * 80 * 250)/200)/50) = tr(tr(680000/200)/50) = tr(3400/50) = 68
      +2 = 70
      sun boost  modify(70, 1.5) = tr((70*6144 + 2047)/4096) = tr(432127/4096) = 105
      sun suppress modify(70, 0.5) = tr((70*2048 + 2047)/4096) = tr(145407/4096) = 35
    """

    def _pair(self):
        attacker = _mon("walkingwake", 82, (150, 150, 250, 150, 200), "shedskin", None)
        defender = _mon("heracross", 84, (200, 150, 100, 200, 150), "guts", None, types=["normal"])
        return attacker, defender

    def test_no_weather_baseline(self):
        a, d = self._pair()
        self.assertEqual(
            derive_ps_damage(a, d, "hydrosteam", PsCombatContext()).base_noncrit, 70
        )

    def test_hydro_steam_is_boosted_in_sun(self):
        a, d = self._pair()
        ps = derive_ps_damage(a, d, "hydrosteam", PsCombatContext(weather="sunnyday"))
        self.assertEqual(ps.base_noncrit, 105)

    def test_other_water_moves_are_still_suppressed_in_sun(self):
        # Scald is the same 80 BP special Water move with no special case
        a, d = self._pair()
        ps = derive_ps_damage(a, d, "scald", PsCombatContext(weather="sunnyday"))
        self.assertEqual(ps.base_noncrit, 35)

    def test_hydro_steam_is_not_boosted_under_desolate_land(self):
        # desolateland's handler (conditions.ts:605-611) boosts Fire only and
        # carries no hydrosteam case; Water moves fail outright there anyway
        a, d = self._pair()
        ps = derive_ps_damage(a, d, "hydrosteam", PsCombatContext(weather="desolateland"))
        self.assertEqual(ps.base_noncrit, 70)

    def test_hydro_steam_is_boosted_in_rain_like_any_water_move(self):
        a, d = self._pair()
        ps = derive_ps_damage(a, d, "hydrosteam", PsCombatContext(weather="raindance"))
        self.assertEqual(ps.base_noncrit, 105)


class TestTeraLowBpFloor(unittest.TestCase):
    """battle-actions.ts:1663-1666 -- the 60-BP floor skips moves whose DEX
    basePower is 0 or 150 AND that carry a basePowerCallback."""

    def _tera(self, name, level, stats, ability, tera_type):
        pkmn = _mon(name, level, stats, ability, None)
        pkmn.terastallized = True
        pkmn.tera_type = tera_type
        return pkmn

    def test_gyro_ball_is_exempt_from_the_floor(self):
        # 25 * 100 // 200 + 1 = 13, and Gyro Ball is `basePower: 0` + callback
        attacker = self._tera("scrafty", 83, (197, 239, 122, 239, 200), "shedskin", "steel")
        defender = _mon("heracross", 84, (200, 150, 100, 150, 100), "guts", None)
        ps = derive_ps_damage(attacker, defender, "gyroball", PsCombatContext())
        self.assertEqual(ps.base_power, 13)

    def test_low_kick_is_exempt_from_the_floor(self):
        # Heracross is 54.0 kg -> 540 hg -> the 500 bracket -> 80 BP; use a
        # featherweight target to land under 60
        attacker = self._tera("scrafty", 83, (197, 239, 122, 239, 144), "shedskin", "fighting")
        light = _mon("flabebe", 84, (200, 150, 100, 150, 100), "flowerveil", None)
        ps = derive_ps_damage(attacker, light, "lowkick", PsCombatContext())
        self.assertEqual(ps.base_power, 20)

    def test_fixed_low_bp_move_still_gets_the_floor(self):
        # Water Gun is a plain `basePower: 40` with no callback
        attacker = self._tera("walkingwake", 82, (150, 150, 250, 150, 200), "shedskin", "water")
        defender = _mon("heracross", 84, (200, 150, 100, 200, 150), "guts", None, types=["normal"])
        ps = derive_ps_damage(attacker, defender, "watergun", PsCombatContext())
        self.assertEqual(ps.base_power, 60)


class TestParentalBondHitTwo(unittest.TestCase):
    """battle-actions.ts:1738-1743 -- hit 2 gets modify(baseDamage, 0.25) after
    the +2 and before WeatherModifyDamage.
    modify(70, 0.25) = tr((70*1024 + 2047)/4096) = tr(73727/4096) = 17
    """

    def _pair(self):
        attacker = _mon("walkingwake", 82, (150, 150, 250, 150, 200), "shedskin", None)
        defender = _mon("heracross", 84, (200, 150, 100, 200, 150), "guts", None, types=["normal"])
        return attacker, defender

    def test_hit_one_is_a_normal_derivation(self):
        a, d = self._pair()
        ctx = PsCombatContext(hit_index=1, parental_bond_hit2=False)
        self.assertEqual(derive_ps_damage(a, d, "scald", ctx).base_noncrit, 70)

    def test_hit_two_is_quartered_before_the_weather_event(self):
        a, d = self._pair()
        ctx = PsCombatContext(hit_index=2, parental_bond_hit2=True)
        self.assertEqual(derive_ps_damage(a, d, "scald", ctx).base_noncrit, 17)

    def test_parental_bond_skips_multihit_and_status_moves(self):
        from fp.replay.damage_membership import _parental_bond_applies

        self.assertTrue(_parental_bond_applies("scald"))
        self.assertFalse(_parental_bond_applies("tripleaxel"))  # already multihit
        self.assertFalse(_parental_bond_applies("calmmind"))  # status


class TestVariableBpMultihitDerivation(unittest.TestCase):
    """data/moves.ts tripleaxel `return 20 * move.hit`, triplekick
    `return 10 * move.hit`, beatup `5 + floor(baseStats.atk / 10)` per ally."""

    def _pair(self):
        attacker = _mon("scrafty", 83, (197, 239, 122, 239, 144), "shedskin", None)
        defender = _mon("heracross", 84, (200, 150, 100, 150, 150), "guts", None, types=["normal"])
        return attacker, defender

    def test_triple_axel_base_power_escalates_with_the_hit_index(self):
        a, d = self._pair()
        for hit, bp in ((1, 20), (2, 40), (3, 60)):
            ps = derive_ps_damage(a, d, "tripleaxel", PsCombatContext(hit_index=hit))
            self.assertEqual(ps.base_power, bp)

    def test_triple_kick_base_power_escalates_with_the_hit_index(self):
        a, d = self._pair()
        for hit, bp in ((1, 10), (2, 20), (3, 30)):
            ps = derive_ps_damage(a, d, "triplekick", PsCombatContext(hit_index=hit))
            self.assertEqual(ps.base_power, bp)

    def test_unknown_hit_index_refuses(self):
        from fp.replay.damage_membership import PsRefusal

        a, d = self._pair()
        with self.assertRaises(PsRefusal) as cm:
            derive_ps_damage(a, d, "tripleaxel", PsCombatContext(hit_index=None))
        self.assertEqual(cm.exception.reason, "ps_multihit_index_ambiguous")

    def test_beat_up_walks_the_party_in_order(self):
        from fp.replay.damage_membership import _beatup_base_powers

        # base Atk: Fezandipiti 91 -> 5 + 9 = 14
        #           Clefable    70 -> 5 + 7 = 12
        #           Gyarados   125 -> 5 + 12 = 17
        user = Pokemon("fezandipiti", 79)
        clef = Pokemon("clefable", 80)
        gyara = Pokemon("gyarados", 80)
        party = {"fezandipiti": user, "clefable": clef, "gyarados": gyara}
        order = ["fezandipiti", "clefable", "gyarados"]
        self.assertEqual(_beatup_base_powers(user, party, order), (14, 12, 17))

    def test_beat_up_skips_fainted_and_statused_allies(self):
        from fp.replay.damage_membership import _beatup_base_powers

        user = Pokemon("fezandipiti", 79)
        clef = Pokemon("clefable", 80)
        clef.status = constants.BURN
        gyara = Pokemon("gyarados", 80)
        gyara.hp = 0
        gyara.fainted = True
        party = {"fezandipiti": user, "clefable": clef, "gyarados": gyara}
        order = ["fezandipiti", "clefable", "gyarados"]
        self.assertEqual(_beatup_base_powers(user, party, order), (14,))

    def test_beat_up_keeps_the_user_even_when_statused(self):
        # PS: `ally === pokemon || (!ally.fainted && !ally.status)`
        from fp.replay.damage_membership import _beatup_base_powers

        user = Pokemon("fezandipiti", 79)
        user.status = constants.BURN
        party = {"fezandipiti": user}
        self.assertEqual(_beatup_base_powers(user, party, ["fezandipiti"]), (14,))

    def test_beat_up_unrevealed_party_members_count(self):
        # a mon that never switched in has never fainted and carries no status
        from fp.replay.damage_membership import _beatup_base_powers

        user = Pokemon("fezandipiti", 79)
        party = {"fezandipiti": user}
        order = ["fezandipiti", "clefable", "gyarados"]
        self.assertEqual(_beatup_base_powers(user, party, order), (14, 12, 17))

    def test_beat_up_collapses_forme_variants_onto_the_set_species(self):
        # PS's `setSpecies` is the SET species, and one party slot can show up in
        # fp as several forme objects (Terapagos / -Terastal / -Stellar)
        from fp.replay.damage_membership import _beatup_base_powers

        user = Pokemon("fezandipiti", 79)
        party = {
            "fezandipiti": user,
            "terapagos": Pokemon("terapagos", 77),
            "terapagosterastal": Pokemon("terapagosterastal", 77),
        }
        order = ["fezandipiti", "terapagos"]
        bps = _beatup_base_powers(user, party, order)
        self.assertEqual(len(bps), 2)
        self.assertEqual(bps[0], 14)

    def test_beat_up_without_a_party_order_refuses(self):
        from fp.replay.damage_membership import PsRefusal, _beatup_base_powers

        user = Pokemon("fezandipiti", 79)
        with self.assertRaises(PsRefusal) as cm:
            _beatup_base_powers(user, {"fezandipiti": user}, [])
        self.assertEqual(cm.exception.reason, "ps_unknown_beatup_party")

    def test_beat_up_base_power_comes_from_the_ctx(self):
        a, d = self._pair()
        ctx = PsCombatContext(hit_index=2, beatup_bp=(14, 12, 17))
        self.assertEqual(derive_ps_damage(a, d, "beatup", ctx).base_power, 12)


class TestFixedDamageDerivation(unittest.TestCase):
    """battle-actions.ts:1604-1610 -- getDamage returns before the roll chain."""

    def test_seismic_toss_is_the_attacker_level(self):
        from fp.replay.damage_membership import derive_fixed_damage

        a = _mon("scrafty", 83, (197, 239, 122, 239, 144), "shedskin", None)
        d = _mon("heracross", 84, (200, 150, 100, 150, 150), "guts", None)
        self.assertEqual(derive_fixed_damage(a, d, "seismictoss", 337), 83)
        self.assertEqual(derive_fixed_damage(a, d, "nightshade", 337), 83)

    def test_super_fang_halves_the_defenders_current_hp(self):
        # clampIntRange floors first (lib/utils.ts:320-326), min 1
        from fp.replay.damage_membership import derive_fixed_damage

        a = _mon("scrafty", 83, (197, 239, 122, 239, 144), "shedskin", None)
        d = _mon("heracross", 84, (200, 150, 100, 150, 150), "guts", None)
        self.assertEqual(derive_fixed_damage(a, d, "superfang", 337), 168)
        self.assertEqual(derive_fixed_damage(a, d, "ruination", 336), 168)
        self.assertEqual(derive_fixed_damage(a, d, "naturesmadness", 1), 1)
        self.assertEqual(derive_fixed_damage(a, d, "superfang", 3), 1)

    def test_undecidable_fixed_damage_refuses(self):
        from fp.replay.damage_membership import PsRefusal, derive_fixed_damage

        a = _mon("scrafty", 83, (197, 239, 122, 239, 144), "shedskin", None)
        d = _mon("heracross", 84, (200, 150, 100, 150, 150), "guts", None)
        with self.assertRaises(PsRefusal):
            derive_fixed_damage(a, d, "finalgambit", 337)


class TestLastRespectsCumulativeFaints(unittest.TestCase):
    """data/moves.ts lastrespects: `50 + 50 * pokemon.side.totalFainted`, and
    sim/battle.ts:2551 only ever INCREMENTS totalFainted -- a Revival Blessing
    revive does not take it back down."""

    def _pair(self):
        attacker = _mon("basculegion", 80, (250, 150, 100, 150, 150), "adaptability", None)
        defender = _mon("heracross", 84, (200, 150, 100, 150, 150), "guts", None, types=["water"])
        return attacker, defender

    def test_base_power_scales_with_cumulative_faints(self):
        a, d = self._pair()
        for faints, bp in ((0, 50), (1, 100), (3, 200), (5, 300)):
            ps = derive_ps_damage(
                a, d, "lastrespects", PsCombatContext(attacker_fainted_allies=faints)
            )
            self.assertEqual(ps.base_power, bp)

    def test_base_power_is_capped_at_a_hundred_faints(self):
        a, d = self._pair()
        ps = derive_ps_damage(
            a, d, "lastrespects", PsCombatContext(attacker_fainted_allies=200)
        )
        self.assertEqual(ps.base_power, 5050)


class TestNeutralNatureRefusal(unittest.TestCase):
    """The sidecar carries no nature; gen9 randbats sets are all neutral, so the
    default `serious` is exact.  Anything else is a reconstruction the module
    cannot vouch for."""

    def test_neutral_nature_derives(self):
        a = _mon("scrafty", 83, (197, 239, 122, 239, 144), "shedskin", None)
        d = _mon("heracross", 84, (200, 150, 100, 150, 150), "guts", None)
        self.assertEqual(a.nature, "serious")
        derive_ps_damage(a, d, "knockoff", PsCombatContext())

    def test_non_neutral_nature_refuses(self):
        from fp.replay.damage_membership import PsRefusal

        a = _mon("scrafty", 83, (197, 239, 122, 239, 144), "shedskin", None)
        a.nature = "adamant"
        d = _mon("heracross", 84, (200, 150, 100, 150, 150), "guts", None)
        with self.assertRaises(PsRefusal) as cm:
            derive_ps_damage(a, d, "knockoff", PsCombatContext())
        self.assertEqual(cm.exception.reason, "ps_non_neutral_nature_attacker")


class TestPreTeraTypesAreLive(unittest.TestCase):
    """battle-actions.ts:1765/1786 consults `pokemon.getTypes(false, true)`,
    which is the LIVE types array (post-Protean), not the dex species'."""

    def test_protean_then_tera_loses_the_two_x_stab(self):
        # synth26954: Meowscarada uses Toxic Spikes (Protean -> pure Poison),
        # then teras Grass.  PS: hasType('Grass') is true (it IS Grass now) so
        # STAB is 1.5, but getTypes(false, true) == ['Poison'] does NOT include
        # Grass, so the 2.0 tera-STAB does not apply.
        attacker = _mon("meowscarada", 78, (250, 150, 100, 150, 200), "protean", None)
        attacker.types = ["poison"]
        attacker.terastallized = True
        attacker.tera_type = "grass"
        defender = _mon("heracross", 84, (200, 150, 100, 150, 150), "guts", None, types=["normal"])
        ps = derive_ps_damage(attacker, defender, "flowertrick", PsCombatContext())
        self.assertEqual(ps.params.stab_4096, ps_modifier_4096(1.5))

    def test_protean_then_tera_loses_stab_on_the_old_type(self):
        # Knock Off is Dark: neither the live types (['Poison']) nor the tera
        # type (Grass) match, so there is no STAB at all
        attacker = _mon("meowscarada", 78, (250, 150, 100, 150, 200), "protean", None)
        attacker.types = ["poison"]
        attacker.terastallized = True
        attacker.tera_type = "grass"
        defender = _mon("heracross", 84, (200, 150, 100, 150, 150), "guts", None, types=["normal"])
        ps = derive_ps_damage(attacker, defender, "knockoff", PsCombatContext())
        self.assertEqual(ps.params.stab_4096, 4096)

    def test_plain_tera_on_a_base_type_still_gets_two_x(self):
        attacker = _mon("scrafty", 83, (197, 239, 122, 239, 144), "shedskin", None)
        attacker.terastallized = True
        attacker.tera_type = "dark"
        defender = _mon("heracross", 84, (200, 150, 100, 150, 150), "guts", None, types=["normal"])
        ps = derive_ps_damage(attacker, defender, "knockoff", PsCombatContext())
        self.assertEqual(ps.params.stab_4096, ps_modifier_4096(2.0))


class TestWeatherBallClearWeather(unittest.TestCase):
    """data/moves.ts:20714-20731 -- Weather Ball's onModifyMove is a switch over
    `pokemon.effectiveWeather()` with one `case` per REAL weather and NO
    `default:` arm, so clear weather leaves the 50 BP alone.

    `battle_modifier.weather()` used to store the literal string "none" for
    `|-weather|none`, which is truthy, so the old `bp * 2 if weather else bp`
    doubled Weather Ball in cleared weather (815 instances across 507 corpus
    games, e.g. synth05725 T-of-line-891 Victreebel).
    """

    def _pair(self):
        attacker = _mon("sunflora", 88, (150, 150, 250, 150, 100), "chlorophyll", None)
        defender = _mon("heracross", 84, (200, 150, 100, 200, 150), "guts", None, types=["normal"])
        return attacker, defender

    def _bp(self, weather):
        a, d = self._pair()
        return derive_ps_damage(a, d, "weatherball", PsCombatContext(weather=weather)).base_power

    def test_clear_weather_does_not_double(self):
        self.assertEqual(self._bp(None), 50)

    def test_literal_none_string_does_not_double(self):
        # defence in depth: even if a caller hands the derivation the protocol's
        # own spelling, effectiveWeather() normalizes it away
        self.assertEqual(self._bp("none"), 50)

    def test_every_real_weather_doubles(self):
        for w in ("sunnyday", "desolateland", "raindance", "primordialsea",
                  "sandstorm", "hail", "snowscape"):
            self.assertEqual(self._bp(w), 100, w)

    def test_delta_stream_does_not_double(self):
        # deltastream has no `case` in the switch
        self.assertEqual(self._bp("deltastream"), 50)

    def test_clear_weather_keeps_the_normal_type(self):
        a, d = self._pair()
        ps = derive_ps_damage(a, d, "weatherball", PsCombatContext(weather=None))
        self.assertEqual(ps.params.stab_4096, 4096)  # Sunflora is Grass, move is Normal

    def test_air_lock_suppresses_the_doubling(self):
        a, d = self._pair()
        d.ability = "airlock"
        ps = derive_ps_damage(a, d, "weatherball", PsCombatContext(weather="sunnyday"))
        self.assertEqual(ps.base_power, 50)


class TestPartyOrderSlotResolution(unittest.TestCase):
    """`side.pokemon` entries keep their SET species forever, but a |switch|
    details string carries the mon's CURRENT forme.  Resolving one onto the
    other is what keeps the reconstructed party order (the order Beat Up walks,
    data/moves.ts beatup) in sync with PS."""

    def setUp(self):
        from fp.replay.damage_membership import (
            PARTY_ORDER_UNRESOLVED,
            resolve_party_slot,
        )

        self.resolve = resolve_party_slot
        self.SENTINEL = PARTY_ORDER_UNRESOLVED

    def test_exact_key_resolves(self):
        self.assertEqual(self.resolve(["palafin", "gyarados"], "palafin"), "palafin")

    def test_mid_battle_formes_resolve_onto_the_set_species(self):
        order = ["palafin", "terapagos", "mimikyu", "eiscue", "morpeko",
                 "ogerponwellspring", "zacian"]
        for details, slot in (
            ("palafinhero", "palafin"),
            ("terapagosterastal", "terapagos"),
            ("terapagosstellar", "terapagos"),
            ("mimikyubusted", "mimikyu"),
            ("eiscuenoice", "eiscue"),
            ("morpekohangry", "morpeko"),
            ("ogerponwellspringtera", "ogerponwellspring"),
            ("zaciancrowned", "zacian"),
        ):
            self.assertEqual(self.resolve(order, details), slot, details)

    def test_set_species_longer_than_the_details_also_resolves(self):
        # Minior's set species is Minior-Meteor; it switches in as `Minior`
        self.assertEqual(self.resolve(["miniormeteor"], "minior"), "miniormeteor")

    def test_exact_match_wins_over_an_ambiguous_prefix(self):
        order = ["urshifu", "urshifurapidstrike"]
        self.assertEqual(self.resolve(order, "urshifu"), "urshifu")
        self.assertEqual(
            self.resolve(order, "urshifurapidstrike"), "urshifurapidstrike"
        )

    def test_ambiguous_prefix_refuses(self):
        # "urshifurapid" extends "urshifu" AND is extended by
        # "urshifurapidstrike": two slots match, so there is no safe answer
        self.assertIsNone(
            self.resolve(["urshifu", "urshifurapidstrike"], "urshifurapid")
        )

    def test_unknown_species_refuses(self):
        self.assertIsNone(self.resolve(["palafin", "gyarados"], "missingno"))

    def test_empty_key_refuses(self):
        self.assertIsNone(self.resolve(["palafin"], ""))
        self.assertIsNone(self.resolve(["palafin"], None))

    def test_sentinel_never_matches_a_real_species(self):
        self.assertIsNone(self.resolve([self.SENTINEL], "palafin"))


class TestBeatUpRefusesAnUnresolvedPartyOrder(unittest.TestCase):
    """REFUSE-DON'T-GUESS: once a |switch| could not be mapped onto a party
    slot, the `side.pokemon` permutation is unknown and Beat Up's per-hit base
    powers are read straight off it."""

    def test_poisoned_order_refuses(self):
        from fp.replay.damage_membership import (
            PARTY_ORDER_UNRESOLVED,
            PsRefusal,
            _beatup_base_powers,
        )

        user = Pokemon("fezandipiti", 79)
        order = ["fezandipiti", "clefable", PARTY_ORDER_UNRESOLVED]
        with self.assertRaises(PsRefusal) as cm:
            _beatup_base_powers(user, {"fezandipiti": user}, order)
        self.assertEqual(cm.exception.reason, "ps_beatup_party_order_unresolved")

    def test_clean_order_still_derives(self):
        from fp.replay.damage_membership import _beatup_base_powers

        user = Pokemon("fezandipiti", 79)
        order = ["fezandipiti", "clefable"]
        self.assertEqual(_beatup_base_powers(user, {"fezandipiti": user}, order), (14, 12))


class TestCategorySwitchMoves(unittest.TestCase):
    """PS `onModifyMove` Special->Physical switches (data/moves.ts) and the
    `ignoreAbility` gate that rides along with Photon Geyser.

    Every expected number below is hand-computed from
    sim/battle-actions.ts:1712-1719 (`tr(tr(tr(tr(2L/5+2)*bp*A)/D)/50)`, +2) and
    sim/battle.ts modify/chainModify (`tr((value*mod + 2047)/4096)`)."""

    def _necrozma(self):
        # synth25401 p2: Necrozma-Dusk-Mane L69, Atk 257 > SpA 196, Prism Armor.
        return _mon(
            "necrozmaduskmane", 69, (257, 216, 196, 191, 147), "prismarmor",
            "weaknesspolicy", types=("psychic", "steel"),
        )

    def _appletun(self):
        # synth25401 p1: Appletun L92, Def 200 == SpD 200, Thick Fat.
        return _mon(
            "appletun", 92, (209, 200, 236, 200, 108), "thickfat", "leftovers",
            types=("grass", "dragon"),
        )

    def test_photongeyser_flips_to_physical(self):
        # synth25401 T9/T25.  Atk 257 > SpA 196 -> Physical, so the chain uses
        # Atk 257 vs Def 200 (NOT SpA 196 vs SpD 200).
        #   tr(2*69/5 + 2) = tr(29.6)          = 29
        #   tr(29 * 100 * 257)                 = 745300
        #   tr(745300 / 200) = tr(3726.5)      = 3726
        #   tr(3726 / 50)    = tr(74.52)       = 74      -> +2 = 76
        #   STAB 1.5 (Psychic on a Psychic/Steel user); Psychic vs Grass/Dragon
        #   is 1x -> typeMod 0.
        #   max roll (r=0): tr(76*100/100) = 76
        #                   modify(76, 1.5) = tr((76*6144 + 2047)/4096)
        #                                   = tr(468991/4096) = tr(114.5) = 114
        # 114 is exactly the observed T9 -damage, and the old Special-only
        # derivation topped out at 87 (SpA 196: 29*100*196/200/50 + 2 = 58 ->
        # modify(58, 1.5) = 87).
        ps = derive_ps_damage(
            self._necrozma(), self._appletun(), "photongeyser", PsCombatContext()
        )
        self.assertEqual(ps.attack, 257)
        self.assertEqual(ps.defense, 200)
        self.assertEqual(ps.base_noncrit, 76)
        self.assertEqual(ps.rolls(False)[15], 114)

    def test_photongeyser_t25_tera_steel_defender(self):
        # synth25401 T25: same pair, but Appletun has terastallized into STEEL
        # (`|-resisted|` in the log), so Psychic is 0.5x.  The category switch
        # still runs off the ATTACKER's stats and is unaffected:
        #   base 76 (as above), STAB 1.5, typeMod -1
        #   max roll: modify(76, 1.5) = 114 -> tr(114/2) = 57
        #   observed 48 = tr(modify(tr(76*85/100), 1.5) / 2)
        #               = tr(modify(64, 1.5) / 2) = tr(96/2) = 48
        # The old Special-only derivation gave base 58 -> max tr(87/2) = 43,
        # i.e. the observed 48 sat ABOVE the whole set.
        defender = self._appletun()
        defender.terastallized = True
        defender.tera_type = "steel"
        ps = derive_ps_damage(
            self._necrozma(), defender, "photongeyser", PsCombatContext()
        )
        self.assertEqual(ps.attack, 257)
        self.assertEqual(ps.base_noncrit, 76)
        self.assertEqual(ps.rolls(False)[15], 57)
        self.assertEqual(ps.rolls(False)[0], 48)
        self.assertIn(48, ps.rolls(False))

    def test_photongeyser_tie_stays_special(self):
        # `atk > spa` is STRICT (data/moves.ts:13349) -- equal stats stay Special.
        even = _mon("mew", 80, (200, 200, 200, 200, 200), "synchronize", None,
                    types=("psychic",))
        target = _mon("snorlax", 80, (200, 180, 120, 200, 60), "immunity", None,
                      types=("normal",))
        ps = derive_ps_damage(even, target, "photongeyser", PsCombatContext())
        self.assertEqual(ps.defense, 200)  # SpD, i.e. Special

    def test_photongeyser_ignores_breakable_defender_ability(self):
        # data/moves.ts:13351 `ignoreAbility: true` -> sim/battle.ts:836-841
        # suppresses the DEFENDER's `breakable` abilities.  Multiscale
        # (flags: { breakable: 1 }) would otherwise chainModify(0.5) in
        # ModifyDamage at full HP.
        atk = self._necrozma()
        wall = _mon("dragonite", 80, (200, 180, 150, 180, 120), "multiscale", None,
                    types=("dragon", "flying"))
        wall.hp = wall.max_hp
        plain = _mon("dragonite", 80, (200, 180, 150, 180, 120), "innerfocus", None,
                     types=("dragon", "flying"))
        plain.hp = plain.max_hp
        self.assertEqual(
            derive_ps_damage(atk, wall, "photongeyser", PsCombatContext()).rolls(False),
            derive_ps_damage(atk, plain, "photongeyser", PsCombatContext()).rolls(False),
        )

    def test_photongeyser_does_not_ignore_unbreakable_defender_ability(self):
        # Prism Armor has `flags: {}` (data/abilities.ts) -> NOT suppressible.
        # Psychic vs a pure Poison defender is 2x, so Prism Armor's
        # chainModify(0.75) must still fire.
        atk = self._necrozma()
        armored = _mon("necrozmaduskmane", 80, (200, 180, 150, 180, 120),
                       "prismarmor", None, types=("poison",))
        armored.hp = armored.max_hp
        plain = _mon("necrozmaduskmane", 80, (200, 180, 150, 180, 120),
                     "innerfocus", None, types=("poison",))
        plain.hp = plain.max_hp
        hard = derive_ps_damage(atk, armored, "photongeyser", PsCombatContext())
        soft = derive_ps_damage(atk, plain, "photongeyser", PsCombatContext())
        # ModifyDamage is a single chained modifier, so the Prism Armor arm is
        # exactly modify(<soft pre-final value>, 0.75); comparing the max rolls
        # is enough to prove it fired.
        self.assertLess(hard.rolls(False)[15], soft.rolls(False)[15])

    def test_sunsteelstrike_also_ignores_abilities(self):
        # data/moves.ts:16226 sunsteelstrike `ignoreAbility: true` -- the flag is
        # read off the move, not off the attacker's ability, so a
        # Prism-Armor/no-Mold-Breaker user still breaks Multiscale.
        atk = self._necrozma()
        wall = _mon("dragonite", 80, (200, 180, 150, 180, 120), "multiscale", None,
                    types=("dragon", "flying"))
        wall.hp = wall.max_hp
        plain = _mon("dragonite", 80, (200, 180, 150, 180, 120), "innerfocus", None,
                     types=("dragon", "flying"))
        plain.hp = plain.max_hp
        self.assertEqual(
            derive_ps_damage(atk, wall, "sunsteelstrike", PsCombatContext()).rolls(False),
            derive_ps_damage(atk, plain, "sunsteelstrike", PsCombatContext()).rolls(False),
        )

    def test_shellsidearm_picks_the_bigger_arm(self):
        # data/moves.ts:16223-16234.  Physical arm wins outright.
        user = _mon("slowbrogalar", 87, (224, 215, 150, 172, 102), "regenerator",
                    None, types=("poison", "psychic"))
        target = _mon("blissey", 80, (10, 50, 100, 250, 55), "naturalcure", None,
                      types=("normal",))
        ps = derive_ps_damage(user, target, "shellsidearm", PsCombatContext())
        self.assertEqual(ps.attack, 224)   # Atk, not SpA
        self.assertEqual(ps.defense, 50)   # Def, not SpD

    def test_shellsidearm_tie_resolves_when_both_arms_agree(self):
        # synth40601 T10: Slowbro-Galar L87 (Atk 224 == SpA 224) into Arceus-Bug
        # L73 (Def 218 == SpD 218) -- PS's `physical === special` tie, decided by
        # randomChance(1, 2), which the protocol never reports.  Nothing
        # downstream distinguishes the arms here (no screens, no Muscle
        # Band/Wise Glasses, no Ice Scales, no burn, no contact-sensitive
        # defender ability), so both derivations give the same 16 values and the
        # event stays decidable:
        #   tr(2*87/5 + 2) = tr(36.8)        = 36
        #   tr(36 * 90 * 224)                = 725760
        #   tr(725760 / 218) = tr(3329.17)   = 3329
        #   tr(3329 / 50)    = tr(66.58)     = 66   -> +2 = 68
        #   STAB 1.5 (Poison on Poison/Psychic); Poison vs Bug = 1x -> typeMod 0
        #   max roll: modify(68, 1.5) = tr((68*6144 + 2047)/4096) = tr(102.5) = 102
        #   observed 91 = modify(tr(68*90/100), 1.5) = modify(61, 1.5) = 91
        user = _mon("slowbrogalar", 87, (224, 215, 224, 172, 102), "regenerator",
                    "assaultvest", types=("poison", "psychic"))
        target = _mon("arceusbug", 73, (180, 218, 218, 218, 218), "multitype",
                      "insectplate", types=("bug",))
        target.hp = target.max_hp
        ps = derive_ps_damage(user, target, "shellsidearm", PsCombatContext())
        self.assertEqual(ps.base_noncrit, 68)
        self.assertEqual(ps.rolls(False)[15], 102)
        self.assertIn(91, ps.rolls(False))

    def test_shellsidearm_tie_refuses_when_the_arms_differ(self):
        # Same tie, but Reflect is up: the Physical arm eats chainModify(0.5) in
        # ModifyDamage and the Special arm does not, so the coin flip is
        # observable in the damage and the event MUST be refused.
        from fp.replay.damage_membership import PsRefusal

        user = _mon("slowbrogalar", 87, (224, 215, 224, 172, 102), "regenerator",
                    None, types=("poison", "psychic"))
        target = _mon("arceusbug", 73, (180, 218, 218, 218, 218), "multitype",
                      None, types=("bug",))
        target.hp = target.max_hp
        ctx = PsCombatContext(defender_screens=frozenset((constants.REFLECT,)))
        with self.assertRaises(PsRefusal) as cm:
            derive_ps_damage(user, target, "shellsidearm", ctx)
        self.assertEqual(cm.exception.reason, "ps_shellsidearm_category_coinflip")


class TestRuinAbilities(unittest.TestCase):
    """The four Ruin abilities (data/abilities.ts tabletsofruin:4864-4881,
    vesselofruin:5277-5294, swordofruin:4811-4828, beadsofruin:374-391).

    Each is an `onAny` handler: the HOLDER lowers the corresponding stat of
    every OTHER active Pokemon, and the `hasAbility` line inside is the
    self-exemption ("does not stack with itself").  None of the four carries
    `breakable: 1`, so Mold Breaker / Photon Geyser / Sunsteel Strike do NOT
    turn them off."""

    def _haxorus(self, ability="moldbreaker"):
        # synth35801 p2: Haxorus L77, Atk 271.
        return _mon("haxorus", 77, (271, 183, 137, 152, 194), ability, "lumberry",
                    types=("dragon",))

    def _wochien(self, ability="tabletsofruin"):
        # synth35801 p1: Wo-Chien L83, Def 214 / SpD 272.
        return _mon("wochien", 83, (189, 214, 205, 272, 164), ability, "leftovers",
                    types=("dark", "grass"))

    def test_tablets_applies_through_mold_breaker(self):
        # synth35801 T12: Mold Breaker Haxorus Outrage (120 BP) into Tablets of
        # Ruin Wo-Chien.  Tablets has flags: {} -> not breakable -> it fires.
        #   Atk: modify(271, 0.75) = tr((271*3072 + 2047)/4096)
        #                          = tr(834559/4096) = tr(203.75) = 203
        #   tr(2*77/5 + 2) = tr(32.8)          = 32
        #   tr(32 * 120 * 203)                 = 779520
        #   tr(779520 / 214) = tr(3642.62)     = 3642
        #   tr(3642 / 50)    = tr(72.84)       = 72   -> +2 = 74
        #   STAB 1.5 (Dragon on Dragon); Dragon vs Dark 1x, vs Grass 1x -> 0
        #   max roll: modify(74, 1.5) = tr((74*6144 + 2047)/4096) = tr(111.5) = 111
        #   observed 96 = modify(tr(74*87/100), 1.5) = modify(64, 1.5) = 96
        # The old blanket suppression left Atk at 271 -> base 99 -> max 148,
        # and 96 was BELOW the whole set.
        ps = derive_ps_damage(
            self._haxorus(), self._wochien(), "outrage", PsCombatContext()
        )
        self.assertEqual(ps.attack, 203)
        self.assertEqual(ps.base_noncrit, 74)
        self.assertEqual(ps.rolls(False)[15], 111)
        self.assertIn(96, ps.rolls(False))

    def test_no_tablets_leaves_attack_alone(self):
        # Control: same pair, defender without the ability -> Atk 271,
        # base = tr(tr(tr(32*120*271)/214)/50) + 2 = 97 + 2 = 99, max roll
        # modify(99, 1.5) = tr(610303/4096) = tr(148.999) = 148.
        ps = derive_ps_damage(
            self._haxorus(), self._wochien("regenerator"), "outrage", PsCombatContext()
        )
        self.assertEqual(ps.attack, 271)
        self.assertEqual(ps.base_noncrit, 99)
        self.assertEqual(ps.rolls(False)[15], 148)

    def test_tablets_does_not_stack_with_itself(self):
        # `if (source.hasAbility('Tablets of Ruin')) return;` -- a Tablets
        # attacker's own Atk is untouched by the defender's Tablets.
        ps = derive_ps_damage(
            self._haxorus("tabletsofruin"), self._wochien(), "outrage",
            PsCombatContext(),
        )
        self.assertEqual(ps.attack, 271)

    def test_tablets_does_not_touch_a_special_move(self):
        # onAnyModifyAtk only: a Special move runs ModifySpA.
        ps = derive_ps_damage(
            self._haxorus(), self._wochien(), "dracometeor", PsCombatContext()
        )
        self.assertEqual(ps.attack, 137)  # SpA, unmodified

    def test_vessel_lowers_attacker_spa_only(self):
        # SpA 137 -> modify(137, 0.75) = tr((137*3072 + 2047)/4096)
        #                              = tr(422911/4096) = tr(103.25) = 103
        ps = derive_ps_damage(
            self._haxorus(), self._wochien("vesselofruin"), "dracometeor",
            PsCombatContext(),
        )
        self.assertEqual(ps.attack, 103)
        # ...and leaves a physical move's Atk alone.
        self.assertEqual(
            derive_ps_damage(
                self._haxorus(), self._wochien("vesselofruin"), "outrage",
                PsCombatContext(),
            ).attack,
            271,
        )

    def test_sword_lowers_defender_def(self):
        # Holder is the ATTACKER (onAnyModifyDef, event target = defender).
        # Def 214 -> modify(214, 0.75) = tr((214*3072 + 2047)/4096)
        #                              = tr(659455/4096) = tr(161.0) = 160
        ps = derive_ps_damage(
            self._haxorus("swordofruin"), self._wochien("regenerator"), "outrage",
            PsCombatContext(),
        )
        self.assertEqual(ps.defense, 160)

    def test_sword_does_not_stack_with_itself(self):
        ps = derive_ps_damage(
            self._haxorus("swordofruin"), self._wochien("swordofruin"), "outrage",
            PsCombatContext(),
        )
        self.assertEqual(ps.defense, 214)

    def test_beads_lowers_defender_spd(self):
        # SpD 272 -> modify(272, 0.75) = tr((272*3072 + 2047)/4096)
        #                              = tr(837631/4096) = tr(204.5) = 204
        ps = derive_ps_damage(
            self._haxorus("beadsofruin"), self._wochien("regenerator"),
            "dracometeor", PsCombatContext(),
        )
        self.assertEqual(ps.defense, 204)

    def test_beads_does_not_apply_to_psyshock(self):
        # battle-actions.ts:1676/:1709 run `'Modify' + statTable[defenseStat]`
        # off the OVERRIDDEN stat, and Psyshock's overrideDefensiveStat is
        # 'def'.  So Psyshock fires ModifyDef -- Beads (onAnyModifySpD) must NOT
        # apply, and Sword (onAnyModifyDef) MUST.
        # (Psychic is 0x into Dark, so use a Grass-only stand-in with
        # Wo-Chien's exact Def 214 / SpD 272.)
        target = _mon("tangrowth", 83, (189, 214, 205, 272, 164), "regenerator",
                      None, types=("grass",))
        beads = derive_ps_damage(
            self._haxorus("beadsofruin"), target, "psyshock", PsCombatContext()
        )
        self.assertEqual(beads.defense, 214)  # raw Def, untouched
        sword = derive_ps_damage(
            self._haxorus("swordofruin"), target, "psyshock", PsCombatContext()
        )
        self.assertEqual(sword.defense, 160)  # modify(214, 0.75)
        # ...while a genuinely Special move DOES take the Beads drop.
        self.assertEqual(
            derive_ps_damage(
                self._haxorus("beadsofruin"), target, "psychic", PsCombatContext()
            ).defense,
            204,  # modify(272, 0.75)
        )

    def test_mold_breaker_still_breaks_a_breakable_ability(self):
        # Regression guard the other way: the breakable gate must not have
        # turned Mold Breaker off.  Thick Fat (flags: { breakable: 1 }) halves
        # a Fire attacker's Atk -- unless the attacker is a Mold Breaker.
        thickfat = _mon("appletun", 92, (209, 200, 236, 200, 108), "thickfat",
                        None, types=("grass", "dragon"))
        plain = _mon("appletun", 92, (209, 200, 236, 200, 108), "regenerator",
                     None, types=("grass", "dragon"))
        normal_atk = self._haxorus("unnerve")
        broken_atk = self._haxorus("moldbreaker")
        self.assertEqual(
            derive_ps_damage(normal_atk, thickfat, "firepunch", PsCombatContext()).attack,
            135,  # modify(271, 0.5) = tr((271*2048 + 2047)/4096) = tr(135.5) = 135
        )
        self.assertEqual(
            derive_ps_damage(broken_atk, thickfat, "firepunch", PsCombatContext()).attack,
            derive_ps_damage(normal_atk, plain, "firepunch", PsCombatContext()).attack,
        )


class TestKnockOffUnremovableItems(unittest.TestCase):
    """Knock Off's 1.5x is gated on the ITEM's own onTakeItem
    (data/moves.ts:9971-9977 `singleEvent('TakeItem', item, ...)`): a plate on
    an Arceus forme (data/items.ts flameplate:2163-2167, num-493 gate), a
    memory on Silvally (:704-709), a drive on Genesect (:719-724) and the
    Red/Blue Orb (:5173-5174/:589-590) all return false there, so the hit is
    UNBOOSTED.  This was the 258-event beyond-tolerance class in the 50k
    resweep (the checker boosted, PS and the engine did not)."""

    def test_plated_arceus_takes_unboosted_knock_off(self):
        # synth00341 T28: Mew L82 (Atk 211, Synchronize, Leftovers) Knock Off
        # vs Arceus-Poison L70 (Def 209, Multitype, Toxic Plate).
        #   BP stays 65 (Toxic Plate on Arceus is untakeable -> no chainModify)
        #   base: tr(2*82/5 + 2) = tr(34.8) = 34
        #         tr(tr(tr(34 * 65 * 211)/209)/50) = tr(tr(466310/209)/50)
        #       = tr(2231/50) = 44, +2 = 46
        #   STAB: Dark on a Psychic attacker -> none
        #   type: Dark vs Poison -> 1x
        #   rolls: r=15 tr(46*85/100) = 39 ... r=0 46
        # PS-observed damage that game was 39; the engine agreed ([39..46]).
        attacker = _mon("mew", 82, (211, 211, 211, 211, 211), "synchronize",
                        "leftovers", types=("psychic",))
        defender = _mon("arceuspoison", 70, (209, 209, 209, 209, 209), "multitype",
                        "toxicplate", types=("poison",))
        ps = derive_ps_damage(attacker, defender, "knockoff", PsCombatContext())
        self.assertEqual(ps.base_power, 65)
        self.assertEqual(ps.base_noncrit, 46)
        rolls = ps.rolls(crit=False)
        self.assertEqual(rolls[0], 39)
        self.assertEqual(rolls[15], 46)
        self.assertIn(39, rolls)  # the PS-observed value

    def test_same_arceus_with_removable_item_is_boosted(self):
        # Identical matchup but the plate swapped for Leftovers: BP becomes
        # modify(65, 1.5) = 97, base tr(tr(tr(34*97*211)/209)/50) =
        # tr(tr(695878/209)/50) = tr(3329/50) = 66, +2 = 68.
        attacker = _mon("mew", 82, (211, 211, 211, 211, 211), "synchronize",
                        "leftovers", types=("psychic",))
        defender = _mon("arceuspoison", 70, (209, 209, 209, 209, 209), "multitype",
                        "leftovers", types=("poison",))
        ps = derive_ps_damage(attacker, defender, "knockoff", PsCombatContext())
        self.assertEqual(ps.base_power, 97)
        self.assertEqual(ps.base_noncrit, 68)

    def test_other_unremovable_holders(self):
        attacker = _mon("mew", 82, (211, 211, 211, 211, 211), "synchronize",
                        "leftovers", types=("psychic",))
        for holder, item, types in (
            ("silvallybug", "bugmemory", ("bug",)),
            ("genesectdouse", "dousedrive", ("bug", "steel")),
            ("groudon", "redorb", ("ground",)),
            ("kyogre", "blueorb", ("water",)),
        ):
            defender = _mon(holder, 80, (200, 200, 200, 200, 200), "multitype",
                            item, types=types)
            ps = derive_ps_damage(attacker, defender, "knockoff", PsCombatContext())
            self.assertEqual(ps.base_power, 65, holder)

    def test_booster_energy_follows_the_holders_paradox_tag(self):
        # data/items.ts:643-646: Booster Energy is untakeable iff the holder's
        # baseSpecies has the "Paradox" pokedex tag.  In this checkout that is
        # exactly 16 species; Gouging Fire has NO tags entry (data/pokedex.ts
        # gougingfire), so its Booster Energy is takeable and DOES boost.
        attacker = _mon("mew", 82, (211, 211, 211, 211, 211), "synchronize",
                        "leftovers", types=("psychic",))
        tagged = _mon("greattusk", 80, (200, 200, 200, 200, 200), "protosynthesis",
                      "boosterenergy", types=("ground", "fighting"))
        untagged = _mon("gougingfire", 74, (213, 222, 139, 181, 178), "protosynthesis",
                        "boosterenergy", types=("fire", "dragon"))
        self.assertEqual(
            derive_ps_damage(attacker, tagged, "knockoff", PsCombatContext()).base_power, 65
        )
        self.assertEqual(
            derive_ps_damage(attacker, untagged, "knockoff", PsCombatContext()).base_power, 97
        )

    def test_unremovable_item_on_the_wrong_holder_still_boosts(self):
        # The plate gate keys on the HOLDER being Arceus (num 493); a Tricked
        # plate on anything else is takeable and boosts.
        attacker = _mon("mew", 82, (211, 211, 211, 211, 211), "synchronize",
                        "leftovers", types=("psychic",))
        defender = _mon("slowkinggalar", 85, (115, 185, 236, 236, 100),
                        "regenerator", "toxicplate")
        ps = derive_ps_damage(attacker, defender, "knockoff", PsCombatContext())
        self.assertEqual(ps.base_power, 97)


class TestSacredSwordIgnoreDefensive(unittest.TestCase):
    """`ignoreDefensive` (data/moves.ts sacredsword:15565, darkestlariat:3334)
    zeroes the defender's defensive stage in BOTH directions on every hit
    (battle-actions.ts:1691,1697-1700) -- unlike a crit, which only ignores
    POSITIVE stages.  This was the 27-event Sacred Sword class in the 50k
    resweep: the checker honoured the stage, PS and the engine ignored it."""

    def _samurott(self):
        return _mon("samurotthisui", 77, (211, 168, 199, 145, 175), "sharpness",
                    "choicescarf", types=("water", "dark"))

    def _clodsire(self, def_boost):
        d = _mon("clodsire", 81, (168, 144, 120, 209, 79), "waterabsorb",
                 "leftovers", types=("poison", "ground"))
        d.boosts[constants.DEFENSE] = def_boost
        return d

    def test_positive_defense_stage_is_ignored(self):
        # synth01295 T6: Samurott-Hisui L77 (Atk 211, Sharpness) Sacred Sword
        # vs Clodsire L81 (Def 144) at +2 Def from Curse.
        #   BP:   Sharpness on a slicing move: modify(90, 1.5) = 135
        #   def:  144 -- the +2 stage is IGNORED (not 288)
        #   base: tr(2*77/5 + 2) = 32
        #         tr(tr(tr(32 * 135 * 211)/144)/50) = tr(tr(911520/144)/50)
        #       = tr(6330/50) = 126, +2 = 128
        #   STAB: Fighting on Water/Dark -> none
        #   type: Fighting vs Poison 0.5x, vs Ground 1x -> one resist level
        #   rolls: r=0 tr(128/2) = 64; r=15 tr(tr(128*85/100)/2) = tr(108/2) = 54
        # PS-observed damage that game was 62; the engine agreed ([54..64]).
        ps = derive_ps_damage(self._samurott(), self._clodsire(2), "sacredsword",
                              PsCombatContext())
        self.assertEqual(ps.base_power, 135)
        self.assertEqual(ps.defense, 144)
        self.assertEqual(ps.base_noncrit, 128)
        rolls = ps.rolls(crit=False)
        self.assertEqual(rolls[0], 54)
        self.assertEqual(rolls[15], 64)
        self.assertIn(62, rolls)  # the PS-observed value
        # respecting the stage (the old bug) would have given base
        # tr(tr(911520/288)/50) = tr(3165/50) = 63, +2 = 65 -> max tr(65/2)=32
        self.assertNotIn(32, rolls)

    def test_negative_defense_stage_is_ignored_too(self):
        # A crit already neutralises a POSITIVE stage, so the flag's only
        # non-crit-equivalent effect is on NEGATIVE stages: -2 Def must derive
        # exactly like 0 Def (a plain move would deal 2x here).
        base = derive_ps_damage(self._samurott(), self._clodsire(0), "sacredsword",
                                PsCombatContext())
        dropped = derive_ps_damage(self._samurott(), self._clodsire(-2), "sacredsword",
                                   PsCombatContext())
        self.assertEqual(dropped.defense, base.defense)
        self.assertEqual(dropped.rolls(crit=False), base.rolls(crit=False))
        self.assertEqual(dropped.rolls(crit=True), base.rolls(crit=True))

    def test_plain_move_still_respects_the_stage(self):
        # Razor Shell (also slicing, no ignoreDefensive) must keep honouring
        # the defender's stage: +2 -> defense 288.
        ps = derive_ps_damage(self._samurott(), self._clodsire(2), "razorshell",
                              PsCombatContext())
        self.assertEqual(ps.defense, 288)


class TestGlaiveRushExpiry(unittest.TestCase):
    """Glaive Rush's drawback is spent on the HOLDER's own next action attempt.

    PS data/moves.ts:6660-6675 -- the glaiverush condition is

        onAccuracy() { return true; },
        onSourceModifyDamage() { return this.chainModify(2); },
        onBeforeMovePriority: 100,
        onBeforeMove(pokemon) { pokemon.removeVolatile('glaiverush'); },

    Priority 100 puts the removal ahead of every abort handler (flinch 8,
    sleep/freeze 10, recharge 11, paralysis 1), so ANY action attempt spends it --
    including one that ends in |cant|.  `runMove` runs `runEvent('BeforeMove')`
    unconditionally (sim/battle-actions.ts:254), external Dancer copies included.

    The membership derivation runs off the TURN-START snapshot, which still carries
    the volatile for a holder that has already moved this turn, so the 2x has to be
    gated on whether the holder's BeforeMove has already run.  Verified against
    synth01079 T110 (Baxcalibur used Glaive Rush on T109, moved first with Earthquake
    on T110, then took 44 from Corviknight's U-turn -- a member of the UNdoubled set
    [42..50] and exactly half the doubled [84..100]).
    """

    def _pair(self):
        attacker = _mon(
            "corviknight", 82, (200, 200, 150, 200, 180), "pressure", None
        )
        defender = _mon(
            "baxcalibur", 78, (250, 180, 120, 150, 170), "thermalexchange", None
        )
        defender.volatile_statuses = ["glaiverush"]
        return attacker, defender

    def test_doubled_while_the_holder_has_not_acted(self):
        attacker, defender = self._pair()
        ps = derive_ps_damage(attacker, defender, "uturn", PsCombatContext())
        bare = _mon(
            "baxcalibur", 78, (250, 180, 120, 150, 170), "thermalexchange", None
        )
        base = derive_ps_damage(attacker, bare, "uturn", PsCombatContext())
        self.assertEqual(ps.params.final_4096_noncrit, 8192)
        self.assertEqual(
            ps.rolls(crit=False), [2 * r for r in base.rolls(crit=False)]
        )

    def test_not_doubled_once_the_holder_has_attempted_an_action(self):
        attacker, defender = self._pair()
        ctx = PsCombatContext(defender_before_move_ran=True)
        ps = derive_ps_damage(attacker, defender, "uturn", ctx)
        bare = _mon(
            "baxcalibur", 78, (250, 180, 120, 150, 170), "thermalexchange", None
        )
        base = derive_ps_damage(attacker, bare, "uturn", PsCombatContext())
        self.assertEqual(ps.params.final_4096_noncrit, 4096)
        self.assertEqual(ps.rolls(crit=False), base.rolls(crit=False))

    def test_minimize_is_not_expired_by_the_holder_acting(self):
        # Minimize has no onBeforeMove removal (data/moves.ts minimize condition):
        # it lasts until the holder leaves the field, so the same flag must not
        # touch it.
        attacker = _mon("corviknight", 82, (200, 200, 150, 200, 180), "pressure", None)
        defender = _mon(
            "baxcalibur", 78, (250, 180, 120, 150, 170), "thermalexchange", None
        )
        defender.volatile_statuses = ["minimize"]
        ctx = PsCombatContext(defender_before_move_ran=True)
        ps = derive_ps_damage(attacker, defender, "bodyslam", ctx)
        self.assertEqual(ps.params.final_4096_noncrit, 8192)


class TestDefenderActedBeforeExtraction(unittest.TestCase):
    """`DamageEvent.defender_acted_before` -- did PS already run BeforeMove on the
    defender before this -damage line?"""

    def test_set_when_the_defender_moved_earlier_in_the_block(self):
        events, _, _ = _extract(
            [
                "|move|p1a: Reuniclus|Psychic|p2a: Gardevoir",
                "|-damage|p2a: Gardevoir|200/300",
                "|move|p2a: Gardevoir|Moonblast|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|280/337",
            ]
        )
        ev = [e for e in events if e.defender_slot == "p1a"][0]
        self.assertTrue(ev.defender_acted_before)

    def test_clear_when_the_defender_has_not_acted_yet(self):
        events, _, _ = _extract(
            [
                "|move|p2a: Gardevoir|Moonblast|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|280/337",
                "|move|p1a: Reuniclus|Psychic|p2a: Gardevoir",
                "|-damage|p2a: Gardevoir|200/300",
            ]
        )
        ev = [e for e in events if e.defender_slot == "p1a"][0]
        self.assertFalse(ev.defender_acted_before)

    def test_a_cant_line_counts_as_an_action_attempt(self):
        # BeforeMove ran; the abort came FROM one of its lower-priority handlers
        events, _, _ = _extract(
            [
                "|cant|p1a: Reuniclus|par",
                "|move|p2a: Gardevoir|Moonblast|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|280/337",
            ]
        )
        ev = [e for e in events if e.defender_slot == "p1a"][0]
        self.assertTrue(ev.defender_acted_before)


class TestPerHitCritAttribution(unittest.TestCase):
    """A |-crit| belongs to the hit it precedes, not to the whole move use.

    PS prints it from getDamage, which runs once per hit inside
    hitStepMoveHitLoop (sim/battle-actions.ts), and a hit can end with no
    |-damage| line at all when a Substitute takes it (data/moves.ts:18334-18355
    onTryPrimaryHit -> `-activate ... move: Substitute|[damage]` when the sub
    survives, `target.removeVolatile('substitute')` -> `-end ... Substitute`
    when it breaks).  Both shapes consume the flag."""

    def test_crit_on_a_sub_absorbed_hit_does_not_leak_to_the_next_hit(self):
        events, _, _ = _extract(
            [
                "|move|p2a: Gardevoir|Bullet Seed|p1a: Reuniclus",
                "|-crit|p1a: Reuniclus",
                "|-activate|p1a: Reuniclus|move: Substitute|[damage]",
                "|-damage|p1a: Reuniclus|300/337",
            ]
        )
        hits = [e for e in events if e.defender_slot == "p1a"]
        self.assertEqual(len(hits), 1)
        self.assertFalse(hits[0].crit)

    def test_crit_on_the_sub_breaking_hit_does_not_leak_to_the_next_hit(self):
        # the synth19270 T26 shape: 5 Bullet Seed hits, 3 absorbed, the 4th
        # crits and breaks the sub, only the 5th produces a -damage line
        events, _, _ = _extract(
            [
                "|move|p2a: Gardevoir|Bullet Seed|p1a: Reuniclus",
                "|-activate|p1a: Reuniclus|move: Substitute|[damage]",
                "|-activate|p1a: Reuniclus|move: Substitute|[damage]",
                "|-activate|p1a: Reuniclus|move: Substitute|[damage]",
                "|-crit|p1a: Reuniclus",
                "|-end|p1a: Reuniclus|Substitute",
                "|-damage|p1a: Reuniclus|300/337",
                "|-hitcount|p1a: Reuniclus|5",
            ]
        )
        hits = [e for e in events if e.defender_slot == "p1a"]
        self.assertEqual(len(hits), 1)
        self.assertFalse(hits[0].crit)

    def test_crit_still_attaches_to_its_own_damage_line(self):
        events, _, _ = _extract(
            [
                "|move|p2a: Gardevoir|Bullet Seed|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|320/337",
                "|-crit|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|280/337",
                "|-damage|p1a: Reuniclus|260/337",
            ]
        )
        hits = [e for e in events if e.defender_slot == "p1a"]
        self.assertEqual([e.crit for e in hits], [False, True, False])


class TestEventTimeVolatiles(unittest.TestCase):
    """`-start` lines earlier in the block are state the turn-start snapshot
    does not carry; the foldable ones ride along on the attacker."""

    def test_within_turn_flash_fire_is_folded_onto_the_attacker(self):
        # the synth21362 T2 shape: the user's own Fire move activates the
        # opponent's Flash Fire, then the opponent attacks with a Fire move
        events, _, _ = _extract(
            [
                "|move|p1a: Reuniclus|Fiery Dance|p2a: Gardevoir",
                "|-start|p2a: Gardevoir|ability: Flash Fire",
                "|move|p2a: Gardevoir|Magma Storm|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|255/337",
            ]
        )
        ev = [e for e in events if e.defender_slot == "p1a"][0]
        self.assertIsNone(ev.exclusion)
        self.assertEqual(ev.attacker_gained_volatiles, frozenset({"flashfire"}))

    def test_a_start_after_the_damage_line_is_not_folded(self):
        events, _, _ = _extract(
            [
                "|move|p2a: Gardevoir|Magma Storm|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|255/337",
                "|move|p1a: Reuniclus|Fiery Dance|p2a: Gardevoir",
                "|-start|p2a: Gardevoir|ability: Flash Fire",
            ]
        )
        ev = [e for e in events if e.defender_slot == "p1a"][0]
        self.assertEqual(ev.attacker_gained_volatiles, frozenset())

    def test_a_start_on_the_defender_is_not_folded_onto_the_attacker(self):
        events, _, _ = _extract(
            [
                "|-start|p1a: Reuniclus|ability: Flash Fire",
                "|move|p2a: Gardevoir|Magma Storm|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|255/337",
            ]
        )
        ev = [e for e in events if e.defender_slot == "p1a"][0]
        self.assertEqual(ev.attacker_gained_volatiles, frozenset())

    def test_paradox_booster_start_carries_its_stat_suffix(self):
        events, _, _ = _extract(
            [
                "|-start|p2a: Gardevoir|protosynthesisspa",
                "|move|p2a: Gardevoir|Moonblast|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|255/337",
            ]
        )
        ev = [e for e in events if e.defender_slot == "p1a"][0]
        self.assertEqual(ev.attacker_gained_volatiles, frozenset({"protosynthesisspa"}))

    def test_a_damage_relevant_unfoldable_start_confounds_instead(self):
        # Tar Shot is in _VOLATILE_RELEVANT but not modelled here: excluded
        # rather than derived off a state that does not have it
        events, _, _ = _extract(
            [
                "|-start|p1a: Reuniclus|Tar Shot",
                "|move|p2a: Gardevoir|Moonblast|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|255/337",
            ]
        )
        ev = [e for e in events if e.defender_slot == "p1a"][0]
        self.assertEqual(ev.exclusion, "confounded_volatile_start")

    def test_a_damage_neutral_start_is_ignored(self):
        events, _, _ = _extract(
            [
                "|-start|p1a: Reuniclus|confusion",
                "|move|p2a: Gardevoir|Moonblast|p1a: Reuniclus",
                "|-damage|p1a: Reuniclus|255/337",
            ]
        )
        ev = [e for e in events if e.defender_slot == "p1a"][0]
        self.assertIsNone(ev.exclusion)
        self.assertEqual(ev.attacker_gained_volatiles, frozenset())
