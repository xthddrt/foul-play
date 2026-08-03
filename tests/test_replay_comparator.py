import os
import unittest
from unittest import mock

import constants
from fp.helpers import normalize_name
from fp.replay.comparator import (
    ObservedEvent,
    Severity,
    TurnContext,
    compare_turn,
    parse_branches,
    parse_instruction,
)
from fp.replay.protocol import extract_observed_events
from fp.replay.comparator import Finding
from fp.replay.checker import (
    _harvest_reveals,
    _backfill_revealed_knowledge,
    _bounded_item_gains,
    _infer_illusion_spans,
    _no_prior_acquisition,
    _species_keyed_event_is_reliable,
    _backfill_user_tera,
    _apply_illusion,
    _bearer_tera_from,
    _apply_forme_abilities,
    _clamp_used_move_pp,
    _demote_ko_margin_findings,
    _encore_overridden_side,
    _extract_side_action,
    _ko_margin_sides,
    _protocol_faints,
)


class _FakeInstr:
    """Stands in for a poke_engine Instruction; repr() is the Display string
    the comparator parses."""

    def __init__(self, s):
        self._s = s

    def __repr__(self):
        return self._s


class _FakeBranch:
    def __init__(self, instr_strings):
        self.instruction_list = [_FakeInstr(s) for s in instr_strings]
        self.percentage = 100.0


def branches(*lists):
    return parse_branches([_FakeBranch(l) for l in lists])


def ctx(turn_branches, observed):
    return TurnContext(turn=1, branches=turn_branches, observed=observed)


class TestInstructionParsing(unittest.TestCase):
    def test_damage(self):
        i = parse_instruction("Damage SideOne: 66")
        self.assertEqual(i.kind, "Damage")
        self.assertEqual(i.side, "s1")
        self.assertEqual(i.amount(), 66)

    def test_change_status(self):
        i = parse_instruction("ChangeStatus SideOne-P0: NONE -> PARALYZE")
        self.assertEqual(i.kind, "ChangeStatus")
        self.assertEqual(i.side, "s1")
        self.assertEqual(i.idx, "P0")
        self.assertIn("PARALYZE", i.payload)

    def test_boost(self):
        i = parse_instruction("Boost SideTwo Attack: -1")
        self.assertEqual(i.kind, "Boost")
        self.assertEqual(i.side, "s2")
        self.assertEqual(i.sub, "Attack")
        self.assertEqual(i.amount(), -1)

    def test_side_condition(self):
        i = parse_instruction("ChangeSideCondition SideTwo Stealthrock: 1")
        self.assertEqual(i.kind, "ChangeSideCondition")
        self.assertEqual(i.side, "s2")
        self.assertEqual(i.sub, "Stealthrock")

    def test_weather(self):
        i = parse_instruction("ChangeWeather: NONE,0 -> RAIN,5")
        self.assertEqual(i.kind, "ChangeWeather")
        self.assertIn("RAIN", i.payload)


class TestStatus(unittest.TestCase):
    def test_status_reproduced_no_finding(self):
        b = branches(
            ["Damage SideTwo: 40", "ChangeStatus SideTwo-P0: NONE -> PARALYZE"],
            ["Damage SideTwo: 40"],  # a miss/no-proc branch
        )
        obs = [ObservedEvent("status", "opp", detail="par")]
        self.assertEqual(compare_turn(ctx(b, obs)), [])

    def test_status_not_reproduced_fires(self):
        b = branches(["Damage SideTwo: 40"])  # never paralyzes
        obs = [ObservedEvent("status", "opp", detail="par")]
        fs = compare_turn(ctx(b, obs))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].category, "status")
        self.assertIs(fs[0].severity, Severity.HARD)

    def test_wrong_status_fires(self):
        b = branches(["ChangeStatus SideTwo-P0: NONE -> BURN"])
        obs = [ObservedEvent("status", "opp", detail="par")]
        self.assertEqual(len(compare_turn(ctx(b, obs))), 1)

    def test_status_on_user_maps_to_side_one(self):
        b = branches(["ChangeStatus SideOne-P0: NONE -> BURN"])
        obs = [ObservedEvent("status", "user", detail="brn")]
        self.assertEqual(compare_turn(ctx(b, obs)), [])

    def test_residual_sourced_status_is_not_excused_by_its_source_tag(self):
        # APPROXIMATIONS.md U2 WAS REVOKED (freeze gate attempt 3): synth40894
        # T25's `|-status|p2a: Conkeldurr|brn|[from] item: Flame Orb` was
        # registered as an unassertable "PS ran its residual phase past the
        # engine's turn boundary" scope boundary; re-derivation by execution
        # proved it is the Sparkling Aria engine defect (the burn is never
        # cured, so there is nothing for the Flame Orb re-burn to match).
        # This pins that the comparator has NO source-tag / residual-phase
        # excuse arm: an item- or ability-sourced status that no branch applies
        # stays HARD, exactly like a move-sourced one.  If a future change
        # re-introduces the U2 justification in code, this fails.
        for raw in (
            "|-status|p2a: Conkeldurr|brn|[from] item: Flame Orb",
            "|-status|p2a: Conkeldurr|brn|[from] ability: Flame Body",
            "|-status|p2a: Conkeldurr|brn",
        ):
            with self.subTest(raw=raw):
                b = branches(["Damage SideTwo: 121"])  # never burns
                obs = [ObservedEvent("status", "opp", detail="brn", raw=raw)]
                fs = compare_turn(ctx(b, obs))
                self.assertEqual(len(fs), 1)
                self.assertEqual(fs[0].category, "status")
                self.assertIs(fs[0].severity, Severity.HARD)


class TestBoost(unittest.TestCase):
    def test_boost_reproduced(self):
        b = branches(["Boost SideOne Speed: 2"])
        obs = [ObservedEvent("boost", "user", detail="spe", sign=1)]
        self.assertEqual(compare_turn(ctx(b, obs)), [])

    def test_boost_sign_mismatch_fires(self):
        b = branches(["Boost SideOne Speed: -1"])  # drop, but a raise was seen
        obs = [ObservedEvent("boost", "user", detail="spe", sign=1)]
        self.assertEqual(len(compare_turn(ctx(b, obs))), 1)

    def test_unboost_reproduced(self):
        b = branches(["Boost SideTwo Attack: -1"])
        obs = [ObservedEvent("boost", "opp", detail="atk", sign=-1)]
        self.assertEqual(compare_turn(ctx(b, obs)), [])


class TestImmunity(unittest.TestCase):
    def test_immune_but_all_branches_damage_fires(self):
        b = branches(["Damage SideTwo: 40"], ["Damage SideTwo: 55"])
        obs = [ObservedEvent("immune", "opp")]
        fs = compare_turn(ctx(b, obs))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].category, "immunity")

    def test_immune_respected_when_a_branch_deals_no_damage(self):
        b = branches(["Damage SideTwo: 40"], [])  # a branch with no damage
        obs = [ObservedEvent("immune", "opp")]
        self.assertEqual(compare_turn(ctx(b, obs)), [])


class TestFieldAndVolatile(unittest.TestCase):
    def test_weather_reproduced(self):
        b = branches(["ChangeWeather: NONE,0 -> RAIN,5"])
        obs = [ObservedEvent("weather", "user", detail="RAIN")]
        self.assertEqual(compare_turn(ctx(b, obs)), [])

    def test_weather_missing_fires(self):
        b = branches(["Damage SideTwo: 10"])
        obs = [ObservedEvent("weather", "user", detail="RAIN")]
        self.assertEqual(len(compare_turn(ctx(b, obs))), 1)

    def test_side_condition_reproduced(self):
        b = branches(["ChangeSideCondition SideTwo Stealthrock: 1"])
        obs = [ObservedEvent("side_condition", "opp", detail="stealthrock")]
        self.assertEqual(compare_turn(ctx(b, obs)), [])

    def test_volatile_start_reproduced(self):
        b = branches(["ApplyVolatileStatus SideTwo: LEECHSEED"])
        obs = [ObservedEvent("volatile_start", "opp", detail="leechseed")]
        self.assertEqual(compare_turn(ctx(b, obs)), [])

    def test_volatile_start_missing_fires(self):
        b = branches(["Damage SideTwo: 10"])
        obs = [ObservedEvent("volatile_start", "opp", detail="leechseed")]
        self.assertEqual(len(compare_turn(ctx(b, obs))), 1)

    def test_item_end_reproduced(self):
        b = branches(["ChangeItem SideTwo: EVIOLITE -> NONE"])
        obs = [ObservedEvent("item_end", "opp", detail="Eviolite")]
        self.assertEqual(compare_turn(ctx(b, obs)), [])


class TestEmptyBranchesIsSafe(unittest.TestCase):
    def test_no_branches_no_findings(self):
        obs = [ObservedEvent("status", "opp", detail="par")]
        self.assertEqual(compare_turn(ctx([], obs)), [])


# ---------------------------------------------------------------------------
# Hardening: comparator status berry-cure collapse (triage id 12)
# ---------------------------------------------------------------------------
class TestStatusBerryCure(unittest.TestCase):
    def test_berry_cured_status_matched_by_item_consumption(self):
        # engine collapses apply+immediate-cure into the bare berry consumption:
        # a branch that eats the curing berry models the observed -status net.
        b = branches(
            ["ChangeItem SideOne: CHESTOBERRY -> NONE", "Heal SideTwo: 17"],
            ["Heal SideTwo: 17"],  # miss branch (no status, no berry)
        )
        obs = [ObservedEvent("status", "user", detail="slp", cure_berry="chestoberry")]
        self.assertEqual(compare_turn(ctx(b, obs)), [])

    def test_berry_cured_status_fires_when_no_branch_eats_the_berry(self):
        b = branches(["Damage SideTwo: 40"])  # never applies status nor eats a berry
        obs = [ObservedEvent("status", "user", detail="slp", cure_berry="chestoberry")]
        fs = compare_turn(ctx(b, obs))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].category, "status")

    def test_status_without_cure_berry_still_requires_changestatus(self):
        b = branches(["ChangeItem SideOne: CHESTOBERRY -> NONE"])
        obs = [ObservedEvent("status", "user", detail="slp")]  # cure_berry=""
        self.assertEqual(len(compare_turn(ctx(b, obs))), 1)


# ---------------------------------------------------------------------------
# Hardening: immunity predicate scoped to the move, not whole-turn residual
# (triage id 6 -- Pincurchin electric immunity + Leech Seed chip)
# ---------------------------------------------------------------------------
class TestImmunityResidualScoping(unittest.TestCase):
    def test_uniform_residual_damage_does_not_contradict_immunity(self):
        # Stun Spore hits the electric-immune target; the only SideOne damage in
        # every branch is the identical Leech Seed residual (32) -> not a breach.
        b = branches(
            ["Damage SideOne: 32"],
            ["Damage SideOne: 32"],
            ["Damage SideOne: 32"],
            ["Damage SideOne: 32"],
        )
        obs = [ObservedEvent("immune", "user")]
        self.assertEqual(compare_turn(ctx(b, obs)), [])

    def test_move_damage_on_top_of_residual_still_fires(self):
        # residual 32 in every branch is background; each branch ALSO deals move
        # damage to the immune side -> a genuine immunity breach must fire.
        b = branches(
            ["Damage SideOne: 32", "Damage SideOne: 40"],
            ["Damage SideOne: 32", "Damage SideOne: 55"],
        )
        obs = [ObservedEvent("immune", "user")]
        fs = compare_turn(ctx(b, obs))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].category, "immunity")

    def test_profiles_not_mutated_regression(self):
        # guards the Counter-aliasing bug: differing per-branch damage must fire
        # (background &= must not mutate the first profile in place).
        b = branches(["Damage SideTwo: 40"], ["Damage SideTwo: 55"])
        obs = [ObservedEvent("immune", "opp")]
        self.assertEqual(len(compare_turn(ctx(b, obs))), 1)


# ---------------------------------------------------------------------------
# Hardening: phase-aware observed-event extraction (protocol.py)
# ---------------------------------------------------------------------------
def _obs(block, user_pid, restrict):
    return extract_observed_events(block.strip().splitlines(), user_pid, restrict)


class TestProtocolPhaseAware(unittest.TestCase):
    def test_post_faint_replacement_intimidate_dropped(self):
        # Intimidate -unboost on the UNCHANGED opposing active, from a post-faint
        # replacement switch-in (a separate decision) -> must be truncated. (id 8/11)
        block = """
|move|p1a: Pincurchin|Liquidation|p2a: Probopass
|-damage|p2a: Probopass|0 fnt
|faint|p2a: Probopass
|upkeep
|switch|p2a: Wyrdeer|Wyrdeer, L80, M|100/100
|-ability|p2a: Wyrdeer|Intimidate|boost
|-unboost|p1a: Pincurchin|atk|1
"""
        evs = _obs(block, "p1", {"user": "pincurchin", "opp": "probopass"})
        self.assertEqual([e for e in evs if e.kind == "boost"], [])

    def test_normal_turn_unboost_is_kept(self):
        block = """
|move|p2a: Foo|Growl|p1a: Bar
|-unboost|p1a: Bar|atk|1
"""
        evs = _obs(block, "p1", {"user": "bar", "opp": "foo"})
        boosts = [e for e in evs if e.kind == "boost"]
        self.assertEqual(len(boosts), 1)
        self.assertEqual(boosts[0].side, "user")
        self.assertEqual(boosts[0].sign, -1)

    def test_pivot_switchin_terrain_dropped(self):
        # U-turn: the mover's slot already acted, so its switch-in and the
        # Grassy Surge terrain belong to a deferred phase. (id 22)
        block = """
|move|p2a: Iron Valiant|U-turn|p1a: Foo
|-damage|p1a: Foo|50/100
|switch|p2a: Rillaboom|Rillaboom, L80|100/100
|-fieldstart|move: Grassy Terrain|[from] ability: Grassy Surge|[of] p2a: Rillaboom
"""
        evs = _obs(block, "p1", {"user": "foo", "opp": "ironvaliant"})
        self.assertEqual([e for e in evs if e.kind == "terrain"], [])

    def test_out_of_scope_weather_source_dropped(self):
        # weather set by a switched-in mon that is NOT the turn-start active is
        # scoped out by its '[of]' source token. (id 2/3/7)
        block = """
|switch|p2a: Tyranitar|Tyranitar, L80|100/100
|-weather|Sandstorm|[from] ability: Sand Stream|[of] p2a: Tyranitar
|move|p1a: Foo|Tackle|p2a: Tyranitar
"""
        evs = _obs(block, "p1", {"user": "foo", "opp": "dragonite"})
        self.assertEqual([e for e in evs if e.kind == "weather"], [])

    def test_move_set_weather_without_source_is_kept(self):
        block = """
|move|p1a: Foo|Rain Dance|p1a: Foo
|-weather|RainDance
"""
        evs = _obs(block, "p1", {"user": "foo", "opp": "bar"})
        weather = [e for e in evs if e.kind == "weather"]
        self.assertEqual(len(weather), 1)
        self.assertEqual(weather[0].detail, "RAIN")


class TestProtocolBerryCureExtraction(unittest.TestCase):
    def test_status_carries_cure_berry(self):
        block = """
|move|p1a: Venusaur|Sleep Powder|p2a: Bastiodon
|-status|p2a: Bastiodon|slp
|-enditem|p2a: Bastiodon|Chesto Berry|[eat]
|-curestatus|p2a: Bastiodon|slp|[msg]
"""
        evs = _obs(block, "p1", {"user": "venusaur", "opp": "bastiodon"})
        statuses = [e for e in evs if e.kind == "status"]
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0].cure_berry, "chestoberry")

    def test_status_without_immediate_cure_has_no_berry(self):
        block = """
|move|p1a: Venusaur|Sleep Powder|p2a: Bastiodon
|-status|p2a: Bastiodon|slp
"""
        evs = _obs(block, "p1", {"user": "venusaur", "opp": "bastiodon"})
        statuses = [e for e in evs if e.kind == "status"]
        self.assertEqual(len(statuses), 1)
        self.assertEqual(statuses[0].cure_berry, "")


# ---------------------------------------------------------------------------
# Hardening: full-log reveal pre-pass + back-fill + PP clamp (checker.py)
# ---------------------------------------------------------------------------
class _Move:
    def __init__(self, name, current_pp):
        self.name = name
        self.current_pp = current_pp


class _Pkmn:
    def __init__(
        self,
        name,
        ability="",
        item=constants.UNKNOWN_ITEM,
        moves=None,
        hp=100,
        max_hp=100,
    ):
        self.name = name
        self.ability = ability
        self.item = item
        self.moves = moves or []
        self.terastallized = False
        self.tera_type = None
        self.types = None
        self.hp = hp
        self.max_hp = max_hp


class _Battler:
    def __init__(self, active=None, reserve=None):
        self.active = active
        self.reserve = reserve or []


class _Snap:
    def __init__(self, user, opponent):
        self.user = user
        self.opponent = opponent


class TestHarvestReveals(unittest.TestCase):
    def _chunks(self, text):
        return [text.strip()]

    def test_ability_reveal_harvested(self):
        chunks = self._chunks(
            """
|turn|17
|switch|p2a: Klawf|Klawf, L82, M|100/100
|move|p1a: Bastiodon|Body Press|p2a: Klawf
|-ability|p2a: Klawf|Anger Shell|boost
"""
        )
        rev = _harvest_reveals(chunks)
        self.assertEqual(rev["abilities"].get(("p2", "klawf")), normalize_name("Anger Shell"))

    def test_ability_from_move_trace_is_skipped(self):
        # a |-ability| carrying [from] (Trace/Skill Swap) is not the mon's own set ability
        chunks = self._chunks(
            """
|turn|3
|-ability|p2a: Gardevoir|Intimidate|[from] ability: Trace|[of] p1a: Landorus
"""
        )
        rev = _harvest_reveals(chunks)
        self.assertIsNone(rev["abilities"].get(("p2", "gardevoir")))

    def test_from_ability_of_source_harvested(self):
        # '[from] ability: X|[of] pYa: Mon' attributes X to the [of] mon for owning actions
        chunks = self._chunks(
            """
|turn|8
|move|p2a: Muk|Knock Off|p1a: Magcargo
|-status|p1a: Magcargo|brn|[from] ability: Flame Body|[of] p1a: Magcargo
"""
        )
        rev = _harvest_reveals(chunks)
        self.assertEqual(
            rev["abilities"].get(("p1", "magcargo")), normalize_name("Flame Body")
        )

    def test_from_ability_on_a_move_line_harvested(self):
        # synth00304 T17 / synth01094 T8: a Magic Bounce reflection is announced
        # ONLY as a "[from] ability:" tag on the bounced |move| line -- PS defaults
        # the called move's sourceEffect to the running ability
        # (sim/battle-actions.ts:387) and prints it at :452 -- so an |-ability| line
        # never appears and the bouncer's ability is otherwise never learned.
        chunks = self._chunks(
            """
|turn|17
|switch|p2a: Hatterene|Hatterene, L85, F|100/100
|move|p1a: Donphan|Stealth Rock|p2a: Hatterene
|move|p2a: Hatterene|Stealth Rock|p1a: Donphan|[from] ability: Magic Bounce
|-sidestart|p1: synthbot|move: Stealth Rock
"""
        )
        rev = _harvest_reveals(chunks)
        self.assertEqual(
            rev["abilities"].get(("p2", "hatterene")), normalize_name("Magic Bounce")
        )
        # the bounced move is NOT harvested as the bouncer's own moveset entry
        self.assertNotIn("stealthrock", rev["moves"].get(("p2", "hatterene"), set()))

    def test_from_ability_on_a_fail_line_attributed_to_the_of_holder(self):
        # PS's unboost-blocking abilities emit "-fail <target>|unboost|...|
        # [from] ability: X|[of] <target>" -- the "[of]" repeats the HOLDER
        # (data/abilities.ts:525 Clear Body, :2150 Inner Focus, ...)
        chunks = self._chunks(
            """
|turn|4
|switch|p2a: Metagross|Metagross, L80|100/100
|move|p1a: Ambipom|Fake Out|p2a: Metagross
|-fail|p2a: Metagross|unboost|[from] ability: Clear Body|[of] p2a: Metagross
"""
        )
        rev = _harvest_reveals(chunks)
        self.assertEqual(
            rev["abilities"].get(("p2", "metagross")), normalize_name("Clear Body")
        )

    def test_dancer_copy_attributes_the_ability_to_the_copier(self):
        # the |move| line of a Dancer copy names the COPIER as its subject and has
        # no "[of]", so the ability belongs to that subject
        chunks = self._chunks(
            """
|turn|2
|switch|p2a: Oricorio|Oricorio, L88, F|100/100
|move|p1a: Oricorio|Revelation Dance|p2a: Oricorio
|move|p2a: Oricorio|Revelation Dance|p1a: Oricorio|[from] ability: Dancer
"""
        )
        rev = _harvest_reveals(chunks)
        self.assertEqual(
            rev["abilities"].get(("p2", "oricorio")), normalize_name("Dancer")
        )

    def test_enditem_records_removal_turn(self):
        chunks = self._chunks(
            """
|turn|8
|move|p1a: Lilligant|Close Combat|p2a: Galvantula
|-enditem|p2a: Galvantula|Focus Sash
"""
        )
        rev = _harvest_reveals(chunks)
        self.assertEqual(rev["items"].get(("p2", "galvantula")), ("focussash", 8))

    def test_flame_orb_status_reveals_item_from_battle_start(self):
        # Flame Orb is never |-item|'d/|-enditem|'d; its ONLY protocol trace is the
        # end-of-turn self-burn "-status ...|[from] item: Flame Orb" line. Harvest
        # it as present from battle start (removal_turn None) so pre-turn snapshots
        # back-fill the orb and the engine's EOT self-burn is reproduced.
        # (Heracross Flame Orb, battle-2651908107 T1)
        chunks = self._chunks(
            """
|turn|1
|switch|p1a: Heracross|Heracross, L81, M|100/100
|move|p1a: Heracross|Close Combat|p2a: Toxapex
|-status|p1a: Heracross|brn|[from] item: Flame Orb
"""
        )
        rev = _harvest_reveals(chunks)
        self.assertEqual(rev["items"].get(("p1", "heracross")), ("flameorb", None))

    def test_toxic_orb_status_reveals_item_from_battle_start(self):
        # same mechanism for Toxic Orb (self-inflicted tox at end of turn)
        chunks = self._chunks(
            """
|turn|3
|switch|p2a: Gliscor|Gliscor, L79, F|100/100
|-status|p2a: Gliscor|tox|[from] item: Toxic Orb
"""
        )
        rev = _harvest_reveals(chunks)
        self.assertEqual(rev["items"].get(("p2", "gliscor")), ("toxicorb", None))

    def test_non_item_status_does_not_record_item(self):
        # a plain status (no "[from] item:") must not fabricate an item reveal
        chunks = self._chunks(
            """
|turn|2
|move|p1a: Muk|Toxic|p2a: Skeledirge
|-status|p2a: Skeledirge|tox
"""
        )
        rev = _harvest_reveals(chunks)
        self.assertIsNone(rev["items"].get(("p2", "skeledirge")))

    def test_trick_acquisition_is_a_gain_not_a_battle_start_holding(self):
        # `items` means "held from battle START", so an acquisition must not land
        # there -- but it must not be dropped either (the arm used to skip every
        # "[from] move:" line outright).
        chunks = self._chunks(
            """
|turn|30
|move|p1a: Oranguru|Trick|p2a: Cobalion
|-activate|p1a: Oranguru|move: Trick|[of] p2a: Cobalion
|-item|p2a: Cobalion|Choice Specs|[from] move: Trick
|-item|p1a: Oranguru|Leftovers|[from] move: Trick
"""
        )
        rev = _harvest_reveals(chunks)
        self.assertIsNone(rev["items"].get(("p2", "cobalion")))
        self.assertEqual(
            rev["item_gains"].get(("p2", "cobalion")), [[30, "choicespecs", None]]
        )
        self.assertEqual(
            rev["item_gains"].get(("p1", "oranguru")), [[30, "leftovers", None]]
        )

    def test_enditem_of_an_acquired_item_does_not_back_date_it_to_turn_zero(self):
        # a Cobalion Tricked Choice Specs on T30 and Knocked Off on T40 must NOT
        # read as "held Choice Specs from battle start until T40"
        chunks = self._chunks(
            """
|turn|30
|-item|p2a: Cobalion|Choice Specs|[from] move: Trick
|turn|40
|move|p1a: Klawf|Knock Off|p2a: Cobalion
|-enditem|p2a: Cobalion|Choice Specs|[from] move: Knock Off|[of] p1a: Klawf
"""
        )
        rev = _harvest_reveals(chunks)
        self.assertIsNone(rev["items"].get(("p2", "cobalion")))
        self.assertEqual(
            rev["item_gains"].get(("p2", "cobalion")), [[30, "choicespecs", 40]]
        )

    def test_thief_records_the_victims_removal_turn(self):
        # the `move: (Thief|Covet)` alternation of _ITEM_TRANSFER_LINE was dead code:
        # the arm above it dropped every "[from] move:" line before it could run, so
        # the victim never got a removal turn
        chunks = self._chunks(
            """
|turn|12
|move|p2a: Weavile|Covet|p1a: Blissey
|-item|p2a: Weavile|Leftovers|[from] move: Covet|[of] p1a: Blissey
"""
        )
        rev = _harvest_reveals(chunks)
        self.assertEqual(rev["items"].get(("p1", "blissey")), ("leftovers", 12))
        self.assertEqual(
            rev["item_gains"].get(("p2", "weavile")), [[12, "leftovers", None]]
        )

    def test_frisk_is_still_a_plain_battle_start_reveal(self):
        # Frisk is formatted exactly like a steal but transfers nothing
        chunks = self._chunks(
            """
|turn|9
|-item|p1a: Rayquaza|Loaded Dice|[from] ability: Frisk|[of] p2a: Furret
"""
        )
        rev = _harvest_reveals(chunks)
        self.assertEqual(rev["items"].get(("p1", "rayquaza")), ("loadeddice", None))
        self.assertNotIn(("p1", "rayquaza"), rev["item_gains"])


class TestBackfillRevealedKnowledge(unittest.TestCase):
    def test_ability_and_item_backfilled(self):
        klawf = _Pkmn("klawf")  # ability "" , item unknownitem
        battler = _Battler(active=klawf)
        reveals = {
            "abilities": {("p2", "klawf"): "angershell"},
            "items": {("p2", "klawf"): ("leftovers", None)},
        }
        _backfill_revealed_knowledge(battler, "p2", reveals, snapshot_turn=5)
        self.assertEqual(klawf.ability, "angershell")
        self.assertEqual(klawf.item, "leftovers")

    def test_item_not_backfilled_after_removal_turn(self):
        galv = _Pkmn("galvantula")
        battler = _Battler(active=galv)
        reveals = {"abilities": {}, "items": {("p2", "galvantula"): ("focussash", 8)}}
        # snapshot AFTER the item left play must not receive it
        _backfill_revealed_knowledge(battler, "p2", reveals, snapshot_turn=9)
        self.assertEqual(galv.item, constants.UNKNOWN_ITEM)

    def test_item_backfilled_on_removal_turn_itself(self):
        galv = _Pkmn("galvantula")
        battler = _Battler(active=galv)
        reveals = {"abilities": {}, "items": {("p2", "galvantula"): ("focussash", 8)}}
        # pre-turn state of the removal turn still holds the item
        _backfill_revealed_knowledge(battler, "p2", reveals, snapshot_turn=8)
        self.assertEqual(galv.item, "focussash")

    def test_known_ability_not_overwritten(self):
        mon = _Pkmn("klawf", ability="regenerator")
        battler = _Battler(active=mon)
        reveals = {"abilities": {("p2", "klawf"): "angershell"}, "items": {}}
        _backfill_revealed_knowledge(battler, "p2", reveals, snapshot_turn=5)
        self.assertEqual(mon.ability, "regenerator")

    def test_acquired_item_backfilled_only_after_the_turn_it_was_acquired_on(self):
        reveals = {
            "abilities": {},
            "items": {},
            "item_gains": {("p2", "cobalion"): [[30, "choicespecs", 40]]},
        }
        for snapshot_turn, expected in (
            (30, constants.UNKNOWN_ITEM),  # acquisition resolves MID-turn 30
            (31, "choicespecs"),
            (40, "choicespecs"),  # pre-turn state of the removal turn still holds it
            (41, constants.UNKNOWN_ITEM),
        ):
            mon = _Pkmn("cobalion")
            _backfill_revealed_knowledge(
                _Battler(active=mon), "p2", reveals, snapshot_turn=snapshot_turn
            )
            self.assertEqual(mon.item, expected, "turn %d" % snapshot_turn)

    def test_battle_start_item_not_backfilled_past_an_acquisition(self):
        # every acquisition route requires the receiver to be holding nothing (or
        # hands its own item away in the same breath), so a gain bounds the
        # battle-start item exactly like a removal turn does
        reveals = {
            "abilities": {},
            "items": {("p2", "cobalion"): ("leftovers", None)},
            "item_gains": {("p2", "cobalion"): [[30, "choicespecs", None]]},
        }
        before = _Pkmn("cobalion")
        _backfill_revealed_knowledge(
            _Battler(active=before), "p2", reveals, snapshot_turn=29
        )
        self.assertEqual(before.item, "leftovers")

        after = _Pkmn("cobalion")
        _backfill_revealed_knowledge(
            _Battler(active=after), "p2", reveals, snapshot_turn=31
        )
        self.assertEqual(after.item, "choicespecs")

    def test_acquisition_timeline_overrides_an_already_written_item(self):
        # APPROXIMATIONS.md 16.3 / synth15565 T26.  `apply_exact_teams` writes
        # the sidecar's TURN-0 item a few lines earlier in `_fire_turn`; gating
        # the protocol timeline on `item == UNKNOWN_ITEM` let that weaker,
        # older source suppress the stronger, newer one.  A set list is ground
        # truth only at turn 0.
        reveals = {
            "abilities": {},
            "items": {},
            "item_gains": {
                ("p2", "hoopaunbound"): [
                    [7, "sitrusberry", 26],
                    [26, "lifeorb", None],
                ]
            },
        }
        hoopa = _Pkmn("hoopaunbound")
        hoopa.item = "choiceband"  # what the sidecar wrote
        _backfill_revealed_knowledge(
            _Battler(active=hoopa), "p2", reveals, snapshot_turn=26
        )
        self.assertEqual(hoopa.item, "sitrusberry")

    def test_acquisition_timeline_leaves_windows_it_does_not_cover_alone(self):
        # the two sources COMPOSE: outside every acquisition window the sidecar
        # item stands, because the timeline witnessed nothing there
        reveals = {
            "abilities": {},
            "items": {},
            "item_gains": {("p2", "hoopaunbound"): [[7, "sitrusberry", 26]]},
        }
        for snapshot_turn, expected in (
            (7, "choiceband"),  # the Trick resolves MID-turn 7
            (8, "sitrusberry"),
            (26, "sitrusberry"),  # pre-turn state of the eat turn still holds it
            (27, "choiceband"),  # timeline silent again -> the sidecar stands
        ):
            hoopa = _Pkmn("hoopaunbound")
            hoopa.item = "choiceband"
            _backfill_revealed_knowledge(
                _Battler(active=hoopa), "p2", reveals, snapshot_turn=snapshot_turn
            )
            self.assertEqual(hoopa.item, expected, "turn %d" % snapshot_turn)

    def test_an_open_gain_record_is_bounded_by_the_next_acquisition(self):
        # synth08772: Roaring Moon gained a Choice Band on T1 and traded it back
        # on T2.  PS reports the GIVER's loss as the RECEIVER's `|-item|`, so
        # `_harvest_reveals` never closes the T1 record -- read literally it
        # claims the Band for the rest of the battle, and the engine's T6 Iron
        # Head came out 1.5x too big (a HARD damage-membership miss).
        self.assertEqual(
            _bounded_item_gains([[1, "choiceband", None], [2, "lumberry", 3]]),
            [(1, "choiceband", 2), (2, "lumberry", 3)],
        )
        # an explicit end EARLIER than the next gain is kept as-is
        self.assertEqual(
            _bounded_item_gains([[7, "sitrusberry", 26], [26, "lifeorb", None]]),
            [(7, "sitrusberry", 26), (26, "lifeorb", None)],
        )
        # unsorted input is handled, and a lone record keeps its open end
        self.assertEqual(
            _bounded_item_gains([[30, "lifeorb", None], [10, "leftovers", None]]),
            [(10, "leftovers", 30), (30, "lifeorb", None)],
        )

    def test_an_illusion_span_makes_the_species_key_unreliable(self):
        # synth47388: a Zoroark wearing Pincurchin's face Tricked on T1, and the
        # `(p2, pincurchin)` gain record put the item it received onto the REAL
        # Pincurchin that switched in on T2.
        reveals = {
            "illusions": [
                {"pid": "p2", "disguise": "pincurchin", "start_turn": 0, "end_turn": 2}
            ],
            "illusion_unresolved": {"p2": [(8, 9)]},
        }
        self.assertFalse(
            _species_keyed_event_is_reliable(reveals, "p2", "pincurchin", 1)
        )
        # ...inclusive at both ends: an event resolves DURING its turn
        self.assertFalse(
            _species_keyed_event_is_reliable(reveals, "p2", "pincurchin", 2)
        )
        # outside the span the same species is reliable again
        self.assertTrue(
            _species_keyed_event_is_reliable(reveals, "p2", "pincurchin", 3)
        )
        # a DIFFERENT species on the same side is unaffected -- the guard asks
        # about one species at one turn, not "does this side have a Zoroark"
        self.assertTrue(_species_keyed_event_is_reliable(reveals, "p2", "hoopa", 1))
        # ...and an occupancy the illusion inference could not decide blocks it
        self.assertFalse(_species_keyed_event_is_reliable(reveals, "p2", "hoopa", 9))
        # other side entirely
        self.assertTrue(
            _species_keyed_event_is_reliable(reveals, "p1", "pincurchin", 1)
        )

    def test_timeline_does_not_override_across_an_illusion_span(self):
        reveals = {
            "abilities": {},
            "items": {},
            "item_gains": {("p2", "pincurchin"): [[1, "loadeddice", None]]},
            "illusions": [
                {"pid": "p2", "disguise": "pincurchin", "start_turn": 0, "end_turn": 2}
            ],
            "illusion_unresolved": {},
        }
        mon = _Pkmn("pincurchin")
        mon.item = "leftovers"  # what the sidecar / live tracking knows
        _backfill_revealed_knowledge(
            _Battler(active=mon), "p2", reveals, snapshot_turn=3
        )
        self.assertEqual(mon.item, "leftovers")

        # ...but it still FILLS a genuinely unknown item, which is no worse than
        # the guess it replaces
        unknown = _Pkmn("pincurchin")
        _backfill_revealed_knowledge(
            _Battler(active=unknown), "p2", reveals, snapshot_turn=3
        )
        self.assertEqual(unknown.item, "loadeddice")

    def test_control_flag_restores_the_suppressing_gate(self):
        # the negative control must actually restore the defect, or the arm that
        # uses it proves nothing (HANDOFF section 4 rule 13)
        reveals = {
            "abilities": {},
            "items": {},
            "item_gains": {("p2", "hoopaunbound"): [[7, "sitrusberry", 26]]},
        }
        hoopa = _Pkmn("hoopaunbound")
        hoopa.item = "choiceband"
        with mock.patch.dict(
            os.environ, {"FP_CONTROL_ITEM_TIMELINE_UNKNOWN_ONLY": "1"}
        ):
            _backfill_revealed_knowledge(
                _Battler(active=hoopa), "p2", reveals, snapshot_turn=26
            )
        self.assertEqual(hoopa.item, "choiceband")


# --------------------------------------------------------------------------
# APPROXIMATIONS.md 16.3(c): settling an Illusion occupancy from the ITEM when
# the MOVE evidence is a coin flip.
# --------------------------------------------------------------------------

_ITEM_EVIDENCE_CONTROL = "FP_CONTROL_ILLUSION_ITEM_EVIDENCE_OFF"


def _occ(pid, species, start, end, held=(), moves=()):
    return {
        "pid": pid,
        "slot": pid + "a",
        "species": species,
        "start_turn": start,
        "end_turn": end,
        "moves": set(moves),
        "held_items": list(held),
        "transformed": False,
        "entry_tera": None,
        "tera_during": None,
        "tera_during_turn": None,
    }


class TestIllusionItemEvidence(unittest.TestCase):
    """synth15565's T7 occupancy shows Hoopa-Unbound and `trick` is in BOTH
    Hoopa-Unbound's and Zoroark's sidecar movesets, so neither move-evidence arm
    of `_infer_illusion_spans` can fire.  The sidecar ITEM is the same kind of
    fixed set property and settles it -- but only while the held item is still
    PROVABLY the set item."""

    # Hoopa-Unbound: Choice Band.  Zoroark (the Illusion bearer): Choice Specs.
    TEAMS = {
        "p2": {
            "hoopaunbound": {
                "ability": "Magician",
                "item": "Choice Band",
                "moves": ["drainpunch", "trick", "hyperspacefury", "zenheadbutt"],
            },
            "zoroark": {
                "ability": "Illusion",
                "item": "Choice Specs",
                "moves": ["psychic", "sludgebomb", "darkpulse", "trick"],
            },
        },
        "p1": {},
    }

    def _run(self, occ, teams=None, item_line_turns=None):
        reveals = {
            "occupancies": [occ],
            "illusions": [],
            "item_line_turns": item_line_turns
            if item_line_turns is not None
            else {"p1": [7], "p2": [7]},
        }
        _infer_illusion_spans(reveals, teams or self.TEAMS)
        return reveals

    def test_item_settles_the_occupancy_as_the_genuine_species(self):
        # THE POSITIVE CASE.  Trick hands over the GIVER's held item
        # (data/moves.ts:19873 `source.takeItem()` -> :19890 the TARGET's
        # `-item` line), the item handed over was a Choice Band, and that is
        # Hoopa-Unbound's set item -- so the Tricker was the real Hoopa.
        occ = _occ("p2", "hoopaunbound", 6, 8, held=[(7, "choiceband")], moves=["trick"])
        rev = self._run(occ)
        self.assertEqual(rev["illusion_unresolved"].get("p2", []), [])
        self.assertEqual(rev["illusions"], [])

    def test_item_settles_the_occupancy_as_the_disguised_bearer(self):
        # the mirror arm, and it must be REACHABLE: a probe that cannot fail is
        # a false pass.  Same shape, but the item handed over is Zoroark's
        # Choice Specs, so the mon wearing Hoopa's face was the bearer.
        occ = _occ(
            "p2", "hoopaunbound", 6, 8, held=[(7, "choicespecs")], moves=["trick"]
        )
        rev = self._run(occ)
        self.assertEqual(rev["illusion_unresolved"].get("p2", []), [])
        self.assertEqual(len(rev["illusions"]), 1)
        span = rev["illusions"][0]
        self.assertEqual(
            (span["pid"], span["disguise"], span["true_species"]),
            ("p2", "hoopaunbound", "zoroark"),
        )
        self.assertEqual((span["start_turn"], span["end_turn"]), (6, 8))
        self.assertEqual(span["inferred_from"], ["item:choicespecs"])

    def test_a_prior_acquisition_forces_a_refusal(self):
        # THE PRECONDITION.  An earlier `|-item|` on this side proves SOMEBODY
        # picked something up, so the item handed over on T7 is no longer
        # guaranteed to be a set item and identifies nothing.  This is the
        # defect class 16.3(b) had to repair from the other side; the inference
        # must REFUSE, not guess.
        occ = _occ("p2", "hoopaunbound", 6, 8, held=[(7, "choiceband")], moves=["trick"])
        rev = self._run(occ, item_line_turns={"p2": [3, 7]})
        self.assertEqual(rev["illusion_unresolved"].get("p2"), [(6, 8)])
        self.assertEqual(rev["illusions"], [])

    def test_a_second_item_line_on_the_same_turn_forces_a_refusal(self):
        # ...and so does a second acquisition resolving in the observation turn
        # itself: PS gives no ordering guarantee this could lean on.
        occ = _occ("p2", "hoopaunbound", 6, 8, held=[(7, "choiceband")], moves=["trick"])
        rev = self._run(occ, item_line_turns={"p2": [7, 7]})
        self.assertEqual(rev["illusion_unresolved"].get("p2"), [(6, 8)])

    def test_shared_sidecar_item_is_genuinely_ambiguous(self):
        # both candidates hold a Choice Band -> the item discriminates nothing,
        # exactly as `trick` in both movesets discriminates nothing.
        teams = {
            "p1": {},
            "p2": {
                "hoopaunbound": dict(self.TEAMS["p2"]["hoopaunbound"]),
                "zoroark": dict(self.TEAMS["p2"]["zoroark"], item="Choice Band"),
            },
        }
        occ = _occ("p2", "hoopaunbound", 6, 8, held=[(7, "choiceband")], moves=["trick"])
        rev = self._run(occ, teams=teams)
        self.assertEqual(rev["illusion_unresolved"].get("p2"), [(6, 8)])
        self.assertEqual(rev["illusions"], [])

    def test_a_holding_matching_neither_candidate_discards_the_occupancy(self):
        # the premises are contradicted (some acquisition route this does not
        # model must have fired), so nothing on this occupancy is trusted --
        # not even the observation that DOES match.  Both observations are
        # `-enditem`-shaped here, so both clear the precondition and the
        # contradiction is real rather than merely skipped.
        occ = _occ(
            "p2",
            "hoopaunbound",
            6,
            9,
            held=[(7, "choiceband"), (8, "leftovers")],
            moves=["trick"],
        )
        rev = self._run(occ, item_line_turns={})
        self.assertEqual(rev["illusion_unresolved"].get("p2"), [(6, 9)])

    def test_no_item_evidence_at_all_still_refuses(self):
        occ = _occ("p2", "hoopaunbound", 6, 8, moves=["trick"])
        rev = self._run(occ)
        self.assertEqual(rev["illusion_unresolved"].get("p2"), [(6, 8)])

    def test_control_flag_restores_the_refusal(self):
        # the negative control must actually restore the old behavior, or the
        # acceptance arm that uses it proves nothing (HANDOFF section 4 rule 13)
        occ = _occ("p2", "hoopaunbound", 6, 8, held=[(7, "choiceband")], moves=["trick"])
        with mock.patch.dict(os.environ, {_ITEM_EVIDENCE_CONTROL: "1"}):
            rev = self._run(occ)
        self.assertEqual(rev["illusion_unresolved"].get("p2"), [(6, 8)])
        self.assertEqual(rev["illusions"], [])

    def test_control_flag_gates_only_this_mechanism(self):
        # the MOVE evidence must keep working under the control: the flag is not
        # a blanket "turn the inference off" switch (HANDOFF section 4 rule 18).
        occ = _occ("p2", "hoopaunbound", 6, 8, moves=["darkpulse"])
        with mock.patch.dict(os.environ, {_ITEM_EVIDENCE_CONTROL: "1"}):
            rev = self._run(occ)
        self.assertEqual(rev["illusion_unresolved"].get("p2", []), [])
        self.assertEqual(len(rev["illusions"]), 1)
        self.assertEqual(rev["illusions"][0]["inferred_from"], ["darkpulse"])

    def test_no_prior_acquisition_counts_before_and_at_separately(self):
        reveals = {"item_line_turns": {"p2": [7, 16, 26]}}
        self.assertTrue(_no_prior_acquisition(reveals, "p2", 7))
        self.assertFalse(_no_prior_acquisition(reveals, "p2", 16))
        self.assertTrue(_no_prior_acquisition(reveals, "p1", 16))  # other side
        self.assertFalse(_no_prior_acquisition({"item_line_turns": {"p2": [4, 4]}}, "p2", 4))

    def test_no_prior_acquisition_requires_the_observation_to_precede_the_ledger(self):
        # the counts alone cannot tell the RECEIVING half of the transfer being
        # read from an unrelated observation that resolved LATER in the same
        # turn.  Log order can: `this.add(...)` appends in resolution order.
        reveals = {
            "item_line_turns": {"p2": [7]},
            "item_line_events": {"p2": [(7, 40)]},
        }
        # observation emitted BEFORE the side's acquisition line -> still a set
        # property (this is synth15565's shape: Trick prints the TARGET's
        # `-item` at data/moves.ts:19890 before the SOURCE's at :19896)
        self.assertTrue(_no_prior_acquisition(reveals, "p2", 7, 39))
        # ...emitted AFTER it -> the holding may be the thing just acquired
        self.assertFalse(_no_prior_acquisition(reveals, "p2", 7, 41))
        # no ordinal / no ordinal ledger -> fall back to the counts
        self.assertTrue(_no_prior_acquisition(reveals, "p2", 7))
        self.assertTrue(_no_prior_acquisition({"item_line_turns": {"p2": [7]}}, "p2", 7, 41))


class TestIllusionItemEvidencePrecondition(unittest.TestCase):
    """REGRESSION PIN, adversarial probe axis (1): THE PRECONDITION.

    `_no_prior_acquisition` used to prove "nothing was acquired" from `|-item|`
    lines and turn NUMBERS only.  Two composing defects: the ledger is blind to
    `-enditem` LOSSES, and its "at most one line on the observation turn"
    allowance ASSUMES that line is the receiving half of the transfer being
    read, with nothing verifying the pairing.

    Every line below is PS-legal.  Knock Off empties the GENUINE Hoopa's hand on
    T1 (an `-enditem` only -- invisible to the ledger).  On T5 the empty-handed
    Hoopa Tricks: `takeItem()` on an empty hand returns `undefined`, not false
    (sim/pokemon.ts:1856-1859), so trick's `myItem === false` guard does not fire
    (data/moves.ts:19873-19877) and the swap proceeds ONE-SIDED -- a `[silent]`
    `-enditem` for the target and a single `-item` for the source
    (data/moves.ts:19887-19899).  The foe then Knock Offs the just-received item
    in the SAME turn, producing a second `-enditem` AFTER the acquisition.

    Pre-repair the item arm read that `-enditem` as a fixed set property and
    emitted a PROVEN Zoroark span (`inferred_from ["item:choicespecs"]`) on a mon
    the log shows is genuine -- a disguised Zoroark would still have been holding
    its own Choice Specs, so its Trick would have been TWO-sided.  This test
    FAILS against that code.

    The CONTROL arm moves the Knock Off one turn later so `before != 0` refuses
    on the old counts alone: it pins that the probe is a real oracle rather than
    a rubber stamp -- both arms must refuse, but only the attack arm needs the
    repair to do so."""

    TEAMS = {
        "p1": {},
        "p2": {
            "hoopaunbound": {
                "ability": "Magician",
                "item": "Choice Band",
                "moves": ["drainpunch", "trick", "hyperspacefury", "zenheadbutt"],
            },
            "zoroark": {
                "ability": "Illusion",
                "item": "Choice Specs",
                "moves": ["psychic", "sludgebomb", "darkpulse", "trick"],
            },
            "grimmsnarl": {
                "ability": "Prankster",
                "item": "Light Clay",
                "moves": ["spiritbreak", "lightscreen", "reflect", "thunderwave"],
            },
        },
    }

    ATTACK = """|switch|p1a: Weezing|Weezing, L85, M|100/100
|switch|p2a: Hoopa|Hoopa-Unbound, L80|100/100
|turn|1
|move|p1a: Weezing|Knock Off|p2a: Hoopa
|-damage|p2a: Hoopa|70/100
|-enditem|p2a: Hoopa|Choice Band|[from] move: Knock Off|[of] p1a: Weezing
|turn|2
|switch|p2a: Grimmsnarl|Grimmsnarl, L82, M|100/100
|turn|3
|move|p2a: Grimmsnarl|Spirit Break|p1a: Weezing
|-damage|p1a: Weezing|80/100
|turn|4
|switch|p2a: Hoopa|Hoopa-Unbound, L80|70/100
|turn|5
|move|p2a: Hoopa|Trick|p1a: Weezing
|-activate|p2a: Hoopa|move: Trick|[of] p1a: Weezing
|-enditem|p1a: Weezing|Choice Specs|[silent]|[from] move: Trick
|-item|p2a: Hoopa|Choice Specs|[from] move: Trick
|move|p1a: Weezing|Knock Off|p2a: Hoopa
|-damage|p2a: Hoopa|40/100
|-enditem|p2a: Hoopa|Choice Specs|[from] move: Knock Off|[of] p1a: Weezing
|turn|6
"""

    # identical, except the second Knock Off lands on T6.  `before != 0` then
    # refuses on the pre-repair counts alone.
    CONTROL = ATTACK.replace(
        """|move|p1a: Weezing|Knock Off|p2a: Hoopa
|-damage|p2a: Hoopa|40/100
|-enditem|p2a: Hoopa|Choice Specs|[from] move: Knock Off|[of] p1a: Weezing
|turn|6
""",
        """|turn|6
|move|p1a: Weezing|Knock Off|p2a: Hoopa
|-damage|p2a: Hoopa|40/100
|-enditem|p2a: Hoopa|Choice Specs|[from] move: Knock Off|[of] p1a: Weezing
|turn|7
""",
    )

    def _run(self, log):
        rev = _harvest_reveals([log.strip()])
        _infer_illusion_spans(rev, self.TEAMS)
        return rev

    def _assert_refuses(self, rev, end_turn):
        # no PROVEN span on the post-switch-in Hoopa occupancy...
        self.assertEqual(
            [il for il in rev["illusions"] if il["start_turn"] >= 4],
            [],
        )
        # ...and specifically none founded on the item evidence
        self.assertEqual(
            [
                il
                for il in rev["illusions"]
                if any(w.startswith("item:") for w in il.get("inferred_from", ()))
            ],
            [],
        )
        # the occupancy is recorded UNRESOLVED, so the gate and the damage check
        # refuse on it rather than asserting a species nothing certified
        self.assertIn((4, end_turn), rev["illusion_unresolved"].get("p2", []))

    def test_attack_arm_refuses(self):
        self.assertNotEqual(self.CONTROL, self.ATTACK)
        self._assert_refuses(self._run(self.ATTACK), 6)

    def test_control_arm_refuses(self):
        self._assert_refuses(self._run(self.CONTROL), 7)

    def test_the_one_sided_trick_is_parsed_as_the_probe_assumes(self):
        # a probe that silently stopped exercising the arm would pass forever.
        # Pin the shape the refusal is about: ONE `-item` line for p2 on T5, and
        # a T5 holding observation whose log ordinal lands AFTER it.
        rev = self._harvested_attack = _harvest_reveals([self.ATTACK.strip()])
        self.assertEqual(rev["item_line_turns"], {"p2": [5]})
        acq = rev["item_line_events"]["p2"]
        self.assertEqual([t for t, _ in acq], [5])
        occ = [
            o
            for o in rev["occupancies"]
            if o["pid"] == "p2" and o["start_turn"] == 4
        ][0]
        self.assertEqual([(t, i) for t, i, _ in occ["held_items"]], [(5, "choicespecs")])
        self.assertGreater(occ["held_items"][0][2], acq[0][1])


class TestIllusionBearerDamagingHitTripwire(unittest.TestCase):
    """PS breaks Illusion on any damaging move hit the disguise SURVIVES --
    `onDamagingHit` -> `singleEvent('End', ...)` -> `this.add('replace', ...)`
    (data/abilities.ts:2061-2071) -- and clears it silently only on faint
    (:2078 `onFaint`).  So a "bearer" verdict on an occupancy that ate a
    damaging hit, lived, and produced no |replace| asserts something PS cannot
    generate.  The bearer arm has zero corpus support (all 71 corpus
    resolutions came out "shown"), and it is the direction a premise violation
    fabricates through, so it refuses instead."""

    TEAMS = TestIllusionItemEvidence.TEAMS

    def _run(self, occ):
        reveals = {
            "occupancies": [occ],
            "illusions": [],
            "item_line_turns": {"p1": [7], "p2": [7]},
        }
        _infer_illusion_spans(reveals, self.TEAMS)
        return reveals

    def test_bearer_verdict_refused_after_a_survived_damaging_hit(self):
        occ = _occ(
            "p2", "hoopaunbound", 6, 8, held=[(7, "choicespecs")], moves=["trick"]
        )
        occ["survived_damaging_hit"] = True
        rev = self._run(occ)
        self.assertEqual(rev["illusions"], [])
        self.assertEqual(rev["illusion_unresolved"].get("p2"), [(6, 8)])

    def test_a_replace_in_the_occupancy_disarms_the_tripwire(self):
        # the disguise DID break; the hit is then perfectly consistent
        occ = _occ(
            "p2", "hoopaunbound", 6, 8, held=[(7, "choicespecs")], moves=["trick"]
        )
        occ["survived_damaging_hit"] = True
        occ["revealed_true_species"] = "zoroark"
        rev = self._run(occ)
        self.assertEqual(len(rev["illusions"]), 1)
        self.assertEqual(rev["illusions"][0]["inferred_from"], ["item:choicespecs"])

    def test_the_tripwire_never_flips_a_refusal_into_a_shown(self):
        # a SHOWN verdict is not evidence about Illusion breaking, so the
        # tripwire must leave it alone (this is synth15565's arm).
        occ = _occ("p2", "hoopaunbound", 6, 8, held=[(7, "choiceband")], moves=["trick"])
        occ["survived_damaging_hit"] = True
        rev = self._run(occ)
        self.assertEqual(rev["illusions"], [])
        self.assertEqual(rev["illusion_unresolved"].get("p2", []), [])

    def test_harvest_marks_only_move_damage_the_mon_survived(self):
        log = """|switch|p1a: Weezing|Weezing, L85, M|100/100
|switch|p2a: Hoopa|Hoopa-Unbound, L80|100/100
|turn|1
|-damage|p2a: Hoopa|90/100|[from] Stealth Rock
|move|p1a: Weezing|Will-O-Wisp|p2a: Hoopa
|-status|p2a: Hoopa|brn
|-damage|p2a: Hoopa|80/100|[from] brn
|turn|2
|switch|p2a: Grimmsnarl|Grimmsnarl, L82, M|100/100
|turn|3
|move|p1a: Weezing|Sludge Bomb|p2a: Grimmsnarl
|-damage|p2a: Grimmsnarl|55/100
|turn|4
|switch|p2a: Hoopa|Hoopa-Unbound, L80|80/100
|turn|5
|move|p1a: Weezing|Sludge Bomb|p2a: Hoopa
|-damage|p2a: Hoopa|0 fnt
|faint|p2a: Hoopa
"""
        rev = _harvest_reveals([log.strip()])
        got = {
            (o["species"], o["start_turn"]): o["survived_damaging_hit"]
            for o in rev["occupancies"]
            if o["pid"] == "p2"
        }
        # hazard + burn residual are tagged [from] -> not a move hit;
        # the lethal Sludge Bomb is a faint -> Illusion clears with no |replace|
        self.assertEqual(
            got, {("hoopaunbound", 0): False, ("grimmsnarl", 2): True, ("hoopaunbound", 4): False}
        )


class TestHarvestHeldItems(unittest.TestCase):
    """`held_items` is SLOT-keyed, which is what makes it usable under Illusion:
    PS renders the per-mon lines through the disguise's name
    (sim/pokemon.ts:531), but the slot is the physical pokemon."""

    def _chunks(self, text):
        return [text.strip()]

    def _held(self, rev, slot):
        # held_items entries are (turn, item, log_ordinal); the ordinal is the
        # `_no_prior_acquisition` ordering proof and is asserted separately in
        # TestIllusionItemEvidencePrecondition, so drop it here.
        return [
            (o["start_turn"], o["end_turn"], [(t, i) for t, i, _ in o["held_items"]])
            for o in rev["occupancies"]
            if o["slot"] == slot
        ]

    def test_trick_credits_the_item_to_the_PARTNER_slot(self):
        # synth15565 T7 verbatim.  Each `-item` line names the RECEIVER and
        # carries no `[of]`; the donor is recoverable only through the
        # `-activate ... move: Trick|[of] ...` pairing (data/moves.ts:19887).
        chunks = self._chunks(
            """
|switch|p1a: Greedent|Greedent, L86, F|347/347
|switch|p2a: Hoopa|Hoopa-Unbound, L80|100/100
|turn|7
|move|p2a: Hoopa|Trick|p1a: Greedent
|-activate|p2a: Hoopa|move: Trick|[of] p1a: Greedent
|-item|p1a: Greedent|Choice Band|[from] move: Trick
|-item|p2a: Hoopa|Sitrus Berry|[from] move: Trick
"""
        )
        rev = _harvest_reveals(chunks)
        # Greedent RECEIVED the Band, so the Band proves what HOOPA held
        self.assertEqual(self._held(rev, "p2a"), [(0, 7, [(7, "choiceband")])])
        self.assertEqual(self._held(rev, "p1a"), [(0, 7, [(7, "sitrusberry")])])
        # ...and the acquisition proof sees one `-item` line per side on T7
        self.assertEqual(rev["item_line_turns"], {"p1": [7], "p2": [7]})

    def test_enditem_credits_the_item_to_its_own_slot(self):
        chunks = self._chunks(
            """
|switch|p2a: Regirock|Regirock, L83|100/100
|turn|21
|-enditem|p2a: Regirock|Chesto Berry|[eat]
"""
        )
        rev = _harvest_reveals(chunks)
        self.assertEqual(self._held(rev, "p2a"), [(0, 21, [(21, "chestoberry")])])
        # an `-enditem` is a LOSS, not an acquisition -- it must not block the
        # precondition
        self.assertEqual(rev["item_line_turns"], {})

    def test_steal_credits_the_item_to_the_of_slot(self):
        chunks = self._chunks(
            """
|switch|p1a: Plusle|Plusle, L95, M|268/268
|switch|p2a: Hoopa|Hoopa-Unbound, L80|100/100
|turn|26
|-item|p2a: Hoopa|Life Orb|[from] ability: Magician|[of] p1a: Plusle
"""
        )
        rev = _harvest_reveals(chunks)
        self.assertEqual(self._held(rev, "p1a"), [(0, 26, [(26, "lifeorb")])])
        self.assertEqual(self._held(rev, "p2a"), [(0, 26, [])])
        self.assertEqual(rev["item_line_turns"], {"p2": [26]})

    def test_symbiosis_is_counted_as_an_acquisition_without_an_item_line(self):
        # data/abilities.ts:4842 announces only `-activate`, so it would
        # otherwise be invisible to the no-prior-acquisition proof
        chunks = self._chunks(
            """
|switch|p2a: Florges|Florges-Blue, L85, F|100/100
|turn|4
|-activate|p2b: Oranguru|ability: Symbiosis|Sitrus Berry|[of] p2a: Florges
"""
        )
        rev = _harvest_reveals(chunks)
        self.assertEqual(rev["item_line_turns"], {"p2": [4]})
        self.assertFalse(_no_prior_acquisition(rev, "p2", 9))


class TestClampUsedMovePP(unittest.TestCase):
    def test_used_move_pp_clamped_to_one(self):
        rb = _Move("revivalblessing", 0)
        pawmot = _Pkmn("pawmot", moves=[rb])
        snap = _Snap(user=_Battler(), opponent=_Battler(active=pawmot))
        block = ["|move|p1a: Pawmot|Revival Blessing|p1a: Pawmot"]
        _clamp_used_move_pp(block, snap, user_pid="p2")  # opponent is p1
        self.assertEqual(rb.current_pp, 1)

    def test_unused_move_pp_left_alone(self):
        m = _Move("thunderbolt", 0)
        mon = _Pkmn("pawmot", moves=[m])
        snap = _Snap(user=_Battler(), opponent=_Battler(active=mon))
        block = ["|move|p1a: Pawmot|Revival Blessing|p1a: Pawmot"]
        _clamp_used_move_pp(block, snap, user_pid="p2")
        self.assertEqual(m.current_pp, 0)

    def test_positive_pp_not_touched(self):
        m = _Move("revivalblessing", 5)
        mon = _Pkmn("pawmot", moves=[m])
        snap = _Snap(user=_Battler(), opponent=_Battler(active=mon))
        block = ["|move|p1a: Pawmot|Revival Blessing|p1a: Pawmot"]
        _clamp_used_move_pp(block, snap, user_pid="p2")
        self.assertEqual(m.current_pp, 5)


if __name__ == "__main__":
    unittest.main()


class TestItemEndSecondaryCauseArtifact(unittest.TestCase):
    """A berry consumed to cure a [fatigue]/[silent] volatile the reconstructed
    pre-turn state can't express (Outrage fatigue -> confusion -> Lum eats) is a
    reconstruction artifact, not an item-loss the engine should reproduce."""

    def test_lum_curing_outrage_fatigue_confusion_is_not_asserted(self):
        block = [
            "|move|p2a: Flapple|Outrage|p1a: Skeledirge|[from]lockedmove",
            "|-damage|p1a: Skeledirge|40/100",
            "|-start|p2a: Flapple|confusion|[fatigue]",
            "|-enditem|p2a: Flapple|Lum Berry|[eat]",
            "|-end|p2a: Flapple|confusion",
        ]
        evs = extract_observed_events(block, "p2")
        self.assertFalse(
            any(e.kind == "item_end" for e in evs),
            "Lum-cures-fatigue-confusion item loss must be suppressed",
        )

    def test_normal_item_loss_still_asserted(self):
        block = [
            "|move|p1a: Weavile|Knock Off|p2a: Ferrothorn",
            "|-enditem|p2a: Ferrothorn|Leftovers|[from] move: Knock Off|[of] p1a: Weavile",
        ]
        evs = extract_observed_events(block, "p1")
        self.assertTrue(
            any(e.kind == "item_end" for e in evs),
            "a genuine Knock Off item loss must still be flagged",
        )


class TestRevealBackfill(unittest.TestCase):
    """The whole-log reveal pre-pass harvests moves + roster so first-use moves
    and switches into not-yet-seen mons become checkable (retroactive population)."""

    def test_harvest_moves_and_roster(self):
        chunks = [
            ">battle-x",
            "|switch|p1a: Raikou|Raikou, L84|100/100",
            "|switch|p2a: Vivillon|Vivillon, L88|100/100",
            "|turn|1",
            "|move|p2a: Vivillon|Quiver Dance|p2a: Vivillon",
            "|move|p1a: Raikou|Thunderbolt|p2a: Vivillon",
            "|switch|p1a: Dondozo|Dondozo, L79|100/100",  # first reveal of Dondozo
            "|move|p1a: Dondozo|Sleep Talk|p1a: Dondozo",
            "|move|p1a: Dondozo|Wave Crash|p2a: Vivillon|[from]move: Sleep Talk",
        ]
        reveals = _harvest_reveals(chunks)
        # p1 Raikou's directly-selected Thunderbolt harvested
        self.assertIn("thunderbolt", reveals["moves"][("p1", "raikou")])
        # Dondozo's Sleep Talk (selected) harvested; Wave Crash (called) excluded
        self.assertIn("sleeptalk", reveals["moves"][("p1", "dondozo")])
        self.assertNotIn("wavecrash", reveals["moves"].get(("p1", "dondozo"), set()))
        # roster records both p1 species with their switch details
        self.assertIn("raikou", reveals["roster"]["p1"])
        self.assertIn("dondozo", reveals["roster"]["p1"])


# ---------------------------------------------------------------------------
# Hardening: a `-start` for a volatile the target ALREADY holds pre-turn is a PS
# onRestart re-emission with no engine state change (triage id 2 -- Charge from
# Electromorphosis re-announced on a re-hit)
# ---------------------------------------------------------------------------
class TestPreHeldVolatileRestart(unittest.TestCase):
    def test_restart_of_already_held_volatile_not_flagged(self):
        # target already holds CHARGE at turn start -> no ApplyVolatileStatus is
        # (or should be) produced; the re-announced -start is not a miss.
        b = branches(["Damage SideTwo: 40"])
        obs = [
            ObservedEvent(
                "volatile_start",
                "opp",
                detail="Charge",
                raw="|-start|p2a: Bellibolt|Charge|Earthquake|[from] ability: Electromorphosis",
            )
        ]
        c = TurnContext(
            turn=1, branches=b, observed=obs, pre_volatiles={"s2": {"CHARGE", "NONE"}}
        )
        self.assertEqual(compare_turn(c), [])

    def test_first_time_volatile_still_flagged(self):
        # same volatile but NOT held pre-turn -> a genuine missing apply must fire
        b = branches(["Damage SideTwo: 40"])
        obs = [
            ObservedEvent(
                "volatile_start", "opp", detail="Charge", raw="|-start|p2a: Bellibolt|Charge"
            )
        ]
        c = TurnContext(turn=1, branches=b, observed=obs, pre_volatiles={"s2": set()})
        fs = compare_turn(c)
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].category, "volatile")


# ---------------------------------------------------------------------------
# Hardening: a `-start ... typechange` is the engine's ChangeType (or a folded
# base-type change), NOT an ApplyVolatileStatus (triage: Meowscarada/Protean)
# ---------------------------------------------------------------------------
class TestTypeChange(unittest.TestCase):
    def test_typechange_without_changetype_not_flagged(self):
        # Protean folds the new type into base state and emits no separate
        # instruction; the display-only -start must not be a HARD miss.
        b = branches(["ChangeItem SideTwo: LIFEORB -> NONE"])
        obs = [
            ObservedEvent(
                "volatile_start",
                "opp",
                detail="typechange",
                raw="|-start|p1a: Meowscarada|typechange|Dark|[from] ability: Protean",
            )
        ]
        self.assertEqual(compare_turn(ctx(b, obs)), [])

    def test_typechange_satisfied_by_changetype(self):
        b = branches(["ChangeType SideTwo: GRASS,DARK -> DARK,TYPELESS"])
        obs = [
            ObservedEvent(
                "volatile_start",
                "opp",
                detail="typechange",
                raw="|-start|p1a: Meowscarada|typechange|Dark",
            )
        ]
        self.assertEqual(compare_turn(ctx(b, obs)), [])

    def test_non_typechange_volatile_still_flagged(self):
        # regression guard: removing TYPECHANGE from the tracked set must not stop
        # a real tracked-volatile miss (Leech Seed) from firing.
        b = branches(["Damage SideTwo: 10"])
        obs = [ObservedEvent("volatile_start", "opp", detail="leechseed")]
        self.assertEqual(len(compare_turn(ctx(b, obs))), 1)


# ---------------------------------------------------------------------------
# Hardening: an HP-threshold berry self-consumption (`-enditem ... [eat]`) is
# magnitude-gated -> SOFT; a forced removal (Knock Off/Trick) stays HARD
# ---------------------------------------------------------------------------
class TestBerryEatItemSeverity(unittest.TestCase):
    def test_eaten_berry_no_branch_is_soft(self):
        b = branches(["Boost SideOne Attack: 2"])  # no ChangeItem
        obs = [
            ObservedEvent(
                "item_end",
                "user",
                detail="Sitrus Berry",
                raw="|-enditem|p2a: Veluza|Sitrus Berry|[eat]",
            )
        ]
        fs = compare_turn(ctx(b, obs))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].category, "item")
        self.assertIs(fs[0].severity, Severity.SOFT)

    def test_forced_removal_no_branch_stays_hard(self):
        b = branches(["Boost SideOne Attack: 2"])
        obs = [
            ObservedEvent(
                "item_end",
                "user",
                detail="Heavy-Duty Boots",
                raw="|-enditem|p1a: Whimsicott|Heavy-Duty Boots|[from] move: Knock Off",
            )
        ]
        fs = compare_turn(ctx(b, obs))
        self.assertEqual(len(fs), 1)
        self.assertIs(fs[0].severity, Severity.HARD)


# ---------------------------------------------------------------------------
# Hardening: a Cud Chew end-of-turn berry RE-EAT removes no item (triage id 3 --
# Tauros re-eats its already-consumed Sitrus); the item_end must be suppressed
# ---------------------------------------------------------------------------
class TestCudChewReeat(unittest.TestCase):
    def test_cudchew_reeat_item_suppressed(self):
        block = [
            "|move|p2a: Entei|Extreme Speed|p1a: Tauros",
            "|faint|p2a: Entei",
            "|",
            "|-activate|p1a: Tauros|ability: Cud Chew",
            "|-enditem|p1a: Tauros|Sitrus Berry|[eat]",
            "|-heal|p1a: Tauros|54/100|[from] item: Sitrus Berry",
        ]
        evs = extract_observed_events(block, "p1")
        self.assertFalse(
            any(e.kind == "item_end" for e in evs),
            "a Cud Chew re-eat removes no item and must not be asserted",
        )

    def test_plain_eaten_berry_item_still_emitted(self):
        # a real HP-threshold Sitrus eat (no Cud Chew activate) is still an event
        block = [
            "|move|p2a: Veluza|Fillet Away|p2a: Veluza",
            "|-damage|p2a: Veluza|146/292",
            "|-enditem|p2a: Veluza|Sitrus Berry|[eat]",
        ]
        evs = extract_observed_events(block, "p1")
        self.assertTrue(any(e.kind == "item_end" for e in evs))


# ---------------------------------------------------------------------------
# Hardening: back-fill the USER side's tera type from full-log reveals (triage
# ids 6/8 -- Leavanny->Ghost, Blaziken->Dark tera type-immunities)
# ---------------------------------------------------------------------------
class TestBackfillUserTera(unittest.TestCase):
    def test_user_tera_backfilled_from_reveal(self):
        leav = _Pkmn("leavanny")
        battler = _Battler(active=leav)
        reveals = {"tera": {("p2", "leavanny"): "ghost"}}
        _backfill_user_tera(battler, "p2", reveals)
        self.assertEqual(leav.tera_type, "ghost")

    def test_user_tera_not_overwritten_when_known(self):
        mon = _Pkmn("leavanny")
        mon.tera_type = "grass"
        battler = _Battler(active=mon)
        reveals = {"tera": {("p2", "leavanny"): "ghost"}}
        _backfill_user_tera(battler, "p2", reveals)
        self.assertEqual(mon.tera_type, "grass")

    def test_user_tera_skipped_if_already_terastallized(self):
        mon = _Pkmn("leavanny")
        mon.terastallized = True
        battler = _Battler(active=mon)
        reveals = {"tera": {("p2", "leavanny"): "ghost"}}
        _backfill_user_tera(battler, "p2", reveals)
        self.assertIsNone(mon.tera_type)


# ---------------------------------------------------------------------------
# Hardening: back-propagate a disguised Zoroark's real types (triage id 5 --
# 'Zacian' that is really a Dark Zoroark is immune to Psychic Expanding Force)
# ---------------------------------------------------------------------------
class TestIllusion(unittest.TestCase):
    def test_harvest_span_starts_at_the_revealed_mons_own_switch_in(self):
        # A mon shown as "Zacian" holds the slot at T11-T12, leaves, another
        # "Zacian" comes in at T13, and the |replace| at T14 reveals THAT one as a
        # disguised Zoroark.  Illusion copies a party member, so the REAL Zacian is
        # on the same team and the T11 stay may well have been it: the span must
        # cover only the revealed OCCUPANCY (T13-T14).  Widening it to the disguise
        # species' first appearance hands the real mon Zoroark's Dark typing and
        # corrupts every type/damage check on its own turns (synth00633 T19: the
        # real Mienshao's Knock Off gains a bogus Dark STAB).
        chunks = [
            ">battle-x",
            "|turn|11",
            "|switch|p1a: Zacian|Zacian, L69, N|100/100",  # possibly the REAL one
            "|turn|12",
            "|switch|p1a: Rhydon|Rhydon, L85, M|100/100",  # it leaves
            "|turn|13",
            "|switch|p1a: Zacian|Zacian, L69, N|100/100",  # the disguised Zoroark
            "|move|p2a: Indeedee|Expanding Force|p1a: Zacian",
            "|-immune|p1a: Zacian",
            "|turn|14",
            "|replace|p1a: Zoroark|Zoroark, L83, F",  # disguise drops
            "|-end|p1a: Zoroark|Illusion",
        ]
        rev = _harvest_reveals(chunks)
        ils = rev["illusions"]
        self.assertEqual(len(ils), 1)
        il = ils[0]
        self.assertEqual(il["pid"], "p1")
        self.assertEqual(il["disguise"], "zacian")
        self.assertEqual(il["true_species"], "zoroark")
        self.assertEqual(il["start_turn"], 13)  # own switch-in, NOT the T11 stay
        self.assertEqual(il["end_turn"], 14)
        # the T13 stay -- the one the immunity is observed on -- is still covered
        self.assertTrue(il["start_turn"] <= 13 <= il["end_turn"])

    def test_harvest_span_per_occupancy_when_the_zoroark_re_disguises(self):
        # A Zoroark that switches out and back in re-applies Illusion and is
        # revealed by its OWN later |replace|; each stay gets its own span, and the
        # real mon's earlier stay is covered by neither (synth00633: real Mienshao
        # T1, Zoroark revealed T2, re-disguised T3 and revealed again T5).
        chunks = [
            ">battle-x",
            "|turn|1",
            "|switch|p2a: Mienshao|Mienshao, L83, F|100/100",  # the real Mienshao
            "|turn|2",
            "|switch|p2a: Mienshao|Mienshao, L83, F|100/100",  # the Zoroark
            "|replace|p2a: Zoroark|Zoroark, L83, M",
            "|turn|3",
            "|switch|p2a: Mienshao|Mienshao, L83, F|66/100",  # re-disguised
            "|turn|4",
            "|turn|5",
            "|replace|p2a: Zoroark|Zoroark, L83, M",
        ]
        ils = _harvest_reveals(chunks)["illusions"]
        self.assertEqual(
            [(i["start_turn"], i["end_turn"]) for i in ils], [(2, 2), (3, 5)]
        )
        self.assertFalse(any(i["start_turn"] <= 1 <= i["end_turn"] for i in ils))

    def test_apply_illusion_overrides_types_in_span(self):
        disguise = _Pkmn("zacian")
        disguise.types = ["fairy"]
        battler = _Battler(active=disguise)
        reveals = {
            "illusions": [
                {
                    "pid": "p1",
                    "disguise": "zacian",
                    "true_species": "zoroark",
                    "start_turn": 11,
                    "end_turn": 14,
                }
            ]
        }
        _apply_illusion(battler, "p1", reveals, turn=13)
        self.assertEqual(disguise.types, ["dark"])  # Zoroark's real type

    def test_apply_illusion_skipped_outside_span(self):
        disguise = _Pkmn("zacian")
        disguise.types = ["fairy"]
        battler = _Battler(active=disguise)
        reveals = {
            "illusions": [
                {
                    "pid": "p1",
                    "disguise": "zacian",
                    "true_species": "zoroark",
                    "start_turn": 11,
                    "end_turn": 14,
                }
            ]
        }
        _apply_illusion(battler, "p1", reveals, turn=20)
        self.assertEqual(disguise.types, ["fairy"])  # unchanged past the reveal

    def test_bearer_tera_from_uses_the_tera_turn_not_the_entry_turn(self):
        self.assertEqual(
            _bearer_tera_from(
                {"start_turn": 0, "tera_during": "normal", "tera_during_turn": 5}
            ),
            5,
        )

    def test_bearer_tera_from_uses_the_entry_turn_for_an_already_tera_entrant(self):
        # a `tera:` suffix on the entry line means it walked in terastallized
        self.assertEqual(
            _bearer_tera_from(
                {"start_turn": 7, "tera_during": None, "tera_during_turn": None}
            ),
            7,
        )

    def test_bearer_tera_only_applies_from_the_turn_it_terastallized(self):
        # a |-terastallize| DURING the occupancy lands mid-resolution, so the
        # pre-state of that turn (and every earlier one) is still un-tera'd.
        # Back-dating it over the whole span made a Normal/Ghost Zoroark-Hisui
        # read as pure Normal for four turns (synth45461 T3).
        span = {
            "pid": "p1",
            "disguise": "flamigo",
            "true_species": "zoroarkhisui",
            "start_turn": 0,
            "end_turn": 8,
            "bearer_tera": "normal",
            "bearer_tera_from": 5,
        }
        before = _Pkmn("flamigo")
        before.types = ["flying", "fighting"]
        _apply_illusion(_Battler(active=before), "p1", {"illusions": [span]}, turn=3)
        self.assertEqual(before.types, ["normal", "ghost"])
        self.assertFalse(before.terastallized)

        after = _Pkmn("flamigo")
        after.types = ["flying", "fighting"]
        _apply_illusion(_Battler(active=after), "p1", {"illusions": [span]}, turn=6)
        # `types` stays the BEARER'S BASE types -- the engine reads it as PS's
        # `getTypes(false, true)` for STAB and takes the defensive typing from
        # `terastallized`/`tera_type`
        self.assertEqual(after.types, ["normal", "ghost"])
        self.assertTrue(after.terastallized)
        self.assertEqual(after.tera_type, "normal")

    def test_bearer_tera_reaches_the_mon_standing_under_its_true_name(self):
        # after the |replace| the reconstruction stands the same physical mon
        # as the Zoroark; the tera was applied to the disguise object and must
        # still reach it (synth25867 T3 / synth39515 T8)
        revealed = _Pkmn("zoroarkhisui")
        revealed.types = ["normal", "ghost"]
        battler = _Battler(active=revealed)
        reveals = {
            "illusions": [
                {
                    "pid": "p1",
                    "disguise": "volbeat",
                    "true_species": "zoroarkhisui",
                    "start_turn": 0,
                    "end_turn": 3,
                    "bearer_tera": "fighting",
                    "bearer_tera_from": 2,
                }
            ]
        }
        _apply_illusion(battler, "p1", reveals, turn=3)
        self.assertTrue(revealed.terastallized)
        self.assertEqual(revealed.tera_type, "fighting")
        # the tera reaches it through `terastallized`/`tera_type`; `types` keeps
        # the base pair the offensive STAB stage needs
        self.assertEqual(revealed.types, ["normal", "ghost"])

    def test_true_species_types_are_not_re_derived_from_the_pokedex(self):
        # standing under its own name, its types are already right -- a Soak /
        # Reflect Type product must not be clobbered
        revealed = _Pkmn("zoroark")
        revealed.types = ["water"]
        battler = _Battler(active=revealed)
        reveals = {
            "illusions": [
                {
                    "pid": "p1",
                    "disguise": "zacian",
                    "true_species": "zoroark",
                    "start_turn": 11,
                    "end_turn": 14,
                }
            ]
        }
        _apply_illusion(battler, "p1", reveals, turn=13)
        self.assertEqual(revealed.types, ["water"])


# ---------------------------------------------------------------------------
# Fix wave: zero-stage boost, immunity recoil discount, status-trigger
# immunity + chip-scale downgrade, Dancer/drag action parsing
# ---------------------------------------------------------------------------
class TestZeroStageBoost(unittest.TestCase):
    """PS emits a clamped cosmetic |-boost|X|stat|0 when the stat is already at
    the +-6 cap (sim/battle.ts:2030 getCappedBoost zeroes the delta, :2045
    `if (boostBy)` skips the real arm, :2074-2077 still add the 0-stage line
    for ability/self sources); the engine deliberately emits no 0-delta Boost."""

    def test_zero_stage_boost_without_instruction_not_flagged(self):
        b = branches(["Damage SideTwo: 40"])  # no Boost instruction anywhere
        obs = [
            ObservedEvent(
                "boost",
                "opp",
                detail="atk",
                sign=1,
                value=0,
                raw="|-boost|p2a: Fezandipiti|atk|0",
            )
        ]
        self.assertEqual(compare_turn(ctx(b, obs)), [])

    def test_nonzero_stage_boost_still_fires(self):
        # control: a REAL boost with no producing branch must keep firing
        b = branches(["Damage SideTwo: 40"])
        obs = [
            ObservedEvent(
                "boost",
                "opp",
                detail="atk",
                sign=1,
                value=1,
                raw="|-boost|p2a: Fezandipiti|atk|1",
            )
        ]
        fs = compare_turn(ctx(b, obs))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].category, "boost")
        self.assertIs(fs[0].severity, Severity.HARD)

    def test_unparsed_stage_still_fires(self):
        # a stage that could not be parsed (value None) must not be skipped
        b = branches(["Damage SideTwo: 40"])
        obs = [ObservedEvent("boost", "opp", detail="atk", sign=1, value=None)]
        self.assertEqual(len(compare_turn(ctx(b, obs))), 1)


class TestProtocolBoostStageParsing(unittest.TestCase):
    def test_stage_parsed_into_value(self):
        block = """
|move|p2a: Foo|Swords Dance|p2a: Foo
|-boost|p2a: Foo|atk|2
"""
        evs = _obs(block, "p1", {"user": "bar", "opp": "foo"})
        boosts = [e for e in evs if e.kind == "boost"]
        self.assertEqual(len(boosts), 1)
        self.assertEqual(boosts[0].value, 2)

    def test_zero_stage_parsed(self):
        block = """
|move|p2a: Foo|Howl|p2a: Foo
|-boost|p2a: Foo|atk|0
"""
        evs = _obs(block, "p1", {"user": "bar", "opp": "foo"})
        boosts = [e for e in evs if e.kind == "boost"]
        self.assertEqual(len(boosts), 1)
        self.assertEqual(boosts[0].value, 0)

    def test_garbage_stage_degrades_to_none(self):
        block = """
|move|p2a: Foo|Howl|p2a: Foo
|-boost|p2a: Foo|atk|garbled
"""
        evs = _obs(block, "p1", {"user": "bar", "opp": "foo"})
        boosts = [e for e in evs if e.kind == "boost"]
        self.assertEqual(len(boosts), 1)
        self.assertIsNone(boosts[0].value)


class TestImmunityRecoilDiscount(unittest.TestCase):
    """The immune side's own RECOIL move damages itself by trunc(damage * r)
    (engine genx/generate_instructions.rs:2937), which varies with its own
    roll across branches and must not read as an immunity breach."""

    def test_recoil_damage_discounted(self):
        # immune user's Brave Bird (r=0.33): each branch damages the OPPONENT
        # and recoils ~1/3 of it onto the user; without the discount every
        # branch shows non-background user damage -> FP
        b = branches(
            ["Damage SideTwo: 120", "Damage SideOne: 39"],  # trunc(120*0.33)=39
            ["Damage SideTwo: 102", "Damage SideOne: 33"],
        )
        obs = [ObservedEvent("immune", "user", trigger_move="earthquake")]
        c = TurnContext(
            turn=1, branches=b, observed=obs, user_move="bravebird", opp_move="earthquake"
        )
        self.assertEqual(compare_turn(c), [])

    def test_recoil_discount_with_tera_suffix(self):
        b = branches(
            ["Damage SideTwo: 120", "Damage SideOne: 39"],
            ["Damage SideTwo: 102", "Damage SideOne: 33"],
        )
        obs = [ObservedEvent("immune", "user", trigger_move="earthquake")]
        c = TurnContext(
            turn=1,
            branches=b,
            observed=obs,
            user_move="bravebird-tera",
            opp_move="earthquake",
        )
        self.assertEqual(compare_turn(c), [])

    def test_at_most_one_damage_discounted_per_branch(self):
        # two recoil-sized user damages per branch: only ONE may be discounted,
        # the second stays and must fire (move-scale vs the 25% chip threshold:
        # 33 >= 0.25*102)
        b = branches(
            ["Damage SideTwo: 120", "Damage SideOne: 39", "Damage SideOne: 40"],
            ["Damage SideTwo: 102", "Damage SideOne: 33", "Damage SideOne: 34"],
        )
        obs = [ObservedEvent("immune", "user", trigger_move="earthquake")]
        c = TurnContext(
            turn=1, branches=b, observed=obs, user_move="bravebird", opp_move="earthquake"
        )
        fs = compare_turn(c)
        self.assertEqual(len(fs), 1)
        self.assertIs(fs[0].severity, Severity.HARD)

    def test_klefki_real_breach_still_fires_hard(self):
        # true-positive control (the Klefki 195/233 class): the immune side's
        # own move is NOT a recoil move and every branch damages it at move
        # scale -> a genuine immunity breach must stay HARD
        b = branches(["Damage SideOne: 195"], ["Damage SideOne: 233"])
        obs = [ObservedEvent("immune", "user", trigger_move="earthquake")]
        c = TurnContext(
            turn=1, branches=b, observed=obs, user_move="foulplay", opp_move="earthquake"
        )
        fs = compare_turn(c)
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].category, "immunity")
        self.assertIs(fs[0].severity, Severity.HARD)

    def test_recoil_move_with_nonmatching_numbers_still_fires_hard(self):
        # recoil move chosen, but the immune-side damage does NOT satisfy
        # |a - r*D| <= 2 -> no discount, breach fires HARD
        b = branches(
            ["Damage SideTwo: 120", "Damage SideOne: 195"],
            ["Damage SideTwo: 102", "Damage SideOne: 233"],
        )
        obs = [ObservedEvent("immune", "user", trigger_move="earthquake")]
        c = TurnContext(
            turn=1, branches=b, observed=obs, user_move="bravebird", opp_move="earthquake"
        )
        fs = compare_turn(c)
        self.assertEqual(len(fs), 1)
        self.assertIs(fs[0].severity, Severity.HARD)


class TestImmunityStatusTrigger(unittest.TestCase):
    def test_status_move_trigger_skips_damage_contradiction(self):
        # Thunder Wave vs a Ground type: the trigger deals no damage, so
        # varying end-of-turn chip on the immune side cannot contradict it
        b = branches(["Damage SideTwo: 30"], ["Damage SideTwo: 47"])
        obs = [ObservedEvent("immune", "opp", trigger_move="thunderwave")]
        self.assertEqual(compare_turn(ctx(b, obs)), [])

    def test_synchronize_reflection_skips(self):
        b = branches(["Damage SideTwo: 30"], ["Damage SideTwo: 47"])
        obs = [ObservedEvent("immune", "opp", trigger_move="synchronize")]
        self.assertEqual(compare_turn(ctx(b, obs)), [])

    def test_damaging_trigger_still_checked(self):
        # control: a DAMAGING trigger keeps the full contradiction check
        b = branches(["Damage SideTwo: 30"], ["Damage SideTwo: 47"])
        obs = [ObservedEvent("immune", "opp", trigger_move="earthquake")]
        fs = compare_turn(ctx(b, obs))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].category, "immunity")

    def test_no_trigger_still_checked(self):
        # unattributable trigger must not weaken the check
        b = branches(["Damage SideTwo: 30"], ["Damage SideTwo: 47"])
        obs = [ObservedEvent("immune", "opp")]
        self.assertEqual(len(compare_turn(ctx(b, obs))), 1)


class TestImmunityChipScaleDowngrade(unittest.TestCase):
    def test_chip_scale_residual_downgrades_to_soft(self):
        # residuals 10/12 on the immune side vs same-branch move damage
        # 100/90: 12 < 0.25*90 -> end-of-turn-chip variance, SOFT
        b = branches(
            ["Damage SideTwo: 100", "Damage SideOne: 10"],
            ["Damage SideTwo: 90", "Damage SideOne: 12"],
        )
        obs = [ObservedEvent("immune", "user", trigger_move="tackle")]
        fs = compare_turn(ctx(b, obs))
        self.assertEqual(len(fs), 1)
        self.assertIs(fs[0].severity, Severity.SOFT)

    def test_move_scale_residual_stays_hard(self):
        # control: residuals 60/55 >= 0.25*90 -> move-scale breach, HARD
        b = branches(
            ["Damage SideTwo: 100", "Damage SideOne: 60"],
            ["Damage SideTwo: 90", "Damage SideOne: 55"],
        )
        obs = [ObservedEvent("immune", "user", trigger_move="tackle")]
        fs = compare_turn(ctx(b, obs))
        self.assertEqual(len(fs), 1)
        self.assertIs(fs[0].severity, Severity.HARD)

    def test_no_move_damage_scale_stays_hard(self):
        # no other-side damage to set the scale -> conservative HARD
        b = branches(["Damage SideOne: 10"], ["Damage SideOne: 12"])
        obs = [ObservedEvent("immune", "user", trigger_move="tackle")]
        fs = compare_turn(ctx(b, obs))
        self.assertEqual(len(fs), 1)
        self.assertIs(fs[0].severity, Severity.HARD)


class TestProtocolImmuneTrigger(unittest.TestCase):
    def test_trigger_is_nearest_move_targeting_immune_slot(self):
        block = """
|move|p2a: Klefki|Foul Play|p1a: Gumshoos
|-damage|p1a: Gumshoos|120/220
|move|p1a: Gumshoos|Earthquake|p2a: Klefki
|-immune|p2a: Klefki
"""
        evs = _obs(block, "p1", None)
        imm = [e for e in evs if e.kind == "immune"]
        self.assertEqual(len(imm), 1)
        self.assertEqual(imm[0].trigger_move, "earthquake")

    def test_status_move_trigger_attached(self):
        block = """
|move|p1a: Klefki|Thunder Wave|p2a: Rayquaza
|-immune|p2a: Rayquaza
"""
        evs = _obs(block, "p1", None)
        imm = [e for e in evs if e.kind == "immune"]
        self.assertEqual(imm[0].trigger_move, "thunderwave")

    def test_magic_bounce_reflection_attributed(self):
        # the bounced move line carries [from] and still names the move
        block = """
|move|p1a: Gliscor|Thunder Wave|p2a: Espeon
|move|p2a: Espeon|Thunder Wave|p1a: Gliscor|[from] ability: Magic Bounce
|-immune|p1a: Gliscor
"""
        evs = _obs(block, "p1", None)
        imm = [e for e in evs if e.kind == "immune"]
        self.assertEqual(imm[0].trigger_move, "thunderwave")

    def test_synchronize_marks_status_only(self):
        block = """
|move|p2a: Dedenne|Nuzzle|p1a: Umbreon
|-damage|p1a: Umbreon|180/220
|-status|p1a: Umbreon|par
|-activate|p1a: Umbreon|ability: Synchronize
|-immune|p2a: Dedenne
"""
        evs = _obs(block, "p1", None)
        imm = [e for e in evs if e.kind == "immune"]
        self.assertEqual(len(imm), 1)
        self.assertEqual(imm[0].trigger_move, "synchronize")

    def test_no_attributable_trigger_is_empty(self):
        block = """
|-immune|p2a: Klefki
"""
        evs = _obs(block, "p1", None)
        imm = [e for e in evs if e.kind == "immune"]
        self.assertEqual(imm[0].trigger_move, "")


class TestExtractSideActionDancerDrag(unittest.TestCase):
    def test_dancer_copied_move_is_not_the_decision(self):
        # synth02764 T2: Oricorio's Dancer copy precedes its chosen Quiver Dance
        block = [
            "|move|p1a: Oricorio|Revelation Dance|p2a: Oricorio",
            "|-damage|p2a: Oricorio|53/100",
            "|-activate|p2a: Oricorio|ability: Dancer",
            "|move|p2a: Oricorio|Revelation Dance|p1a: Oricorio|[from] ability: Dancer",
            "|-damage|p1a: Oricorio|38/272",
            "|move|p2a: Oricorio|Quiver Dance|p2a: Oricorio",
            "|-boost|p2a: Oricorio|spa|1",
        ]
        self.assertEqual(_extract_side_action(block, "p2a"), ("move", "quiverdance"))
        # the p1 side's first move line is its real (non-Dancer) decision
        self.assertEqual(
            _extract_side_action(block, "p1a"), ("move", "revelationdance")
        )

    def test_dancer_only_moves_yield_no_decision(self):
        block = [
            "|-activate|p2a: Oricorio|ability: Dancer",
            "|move|p2a: Oricorio|Quiver Dance|p2a: Oricorio|[from] ability: Dancer",
        ]
        self.assertIsNone(_extract_side_action(block, "p2a"))

    def test_drag_is_not_a_chosen_switch(self):
        # synth02360 T5: Mudsdale is phazed by Dragon Tail before acting; the
        # dragged-in Overqwil must not parse as p1's chosen switch
        block = [
            "|move|p2a: Appletun|Dragon Tail|p1a: Mudsdale",
            "|-damage|p1a: Mudsdale|90/302",
            "|drag|p1a: Overqwil|Overqwil, L82, F|274/274",
        ]
        self.assertIsNone(_extract_side_action(block, "p1a"))
        self.assertEqual(_extract_side_action(block, "p2a"), ("move", "dragontail"))

    def test_move_before_drag_still_returns_move(self):
        # the side acted BEFORE being phazed: its decision is the move
        block = [
            "|move|p1a: Mudsdale|Stealth Rock|p2a: Appletun",
            "|move|p2a: Appletun|Dragon Tail|p1a: Mudsdale",
            "|drag|p1a: Overqwil|Overqwil, L82, F|274/274",
        ]
        self.assertEqual(_extract_side_action(block, "p1a"), ("move", "stealthrock"))

    def test_chosen_switch_still_parses(self):
        block = ["|switch|p1a: Politoed|Politoed, L88, F|302/302"]
        self.assertEqual(_extract_side_action(block, "p1a"), ("switch", "politoed"))


# ---------------------------------------------------------------------------
# Fix wave: forme-linked abilities, the Encore-override turn gate, and the
# KO-boundary margin severity downgrade
# ---------------------------------------------------------------------------
class TestFormeLinkedAbilities(unittest.TestCase):
    """Terapagos-Terastal's ability is Tera Shell, not the base forme's Tera
    Shift (PS data/abilities.ts:4956 terashift.onSwitchIn -> formeChange).  The
    engine performs that swap itself only when the switch-in happens inside the
    simulated turn (src/genx/abilities.rs:2176-2201), so a reconstruction that
    meets the mon ALREADY in the Terastal forme must set it."""

    def test_terastal_forme_gets_tera_shell(self):
        mon = _Pkmn("terapagosterastal", ability="terashift")
        _apply_forme_abilities(_Battler(active=mon))
        self.assertEqual(mon.ability, "terashell")

    def test_applies_to_reserve_mons_too(self):
        mon = _Pkmn("terapagosterastal", ability="terashift")
        _apply_forme_abilities(_Battler(active=_Pkmn("pikachu"), reserve=[mon]))
        self.assertEqual(mon.ability, "terashell")

    def test_unknown_ability_filled(self):
        mon = _Pkmn("terapagosterastal", ability="")
        _apply_forme_abilities(_Battler(active=mon))
        self.assertEqual(mon.ability, "terashell")

    def test_stellar_forme_gets_teraform_zero(self):
        mon = _Pkmn("terapagosstellar", ability="terashell")
        _apply_forme_abilities(_Battler(active=mon))
        self.assertEqual(mon.ability, "teraformzero")

    def test_base_forme_left_on_tera_shift(self):
        # base Terapagos still transforms inside the engine's own switch-in
        mon = _Pkmn("terapagos", ability="terashift")
        _apply_forme_abilities(_Battler(active=mon))
        self.assertEqual(mon.ability, "terashift")

    def test_foreign_ability_not_clobbered(self):
        # a Skill Swap / Gastro Acid product is real knowledge; leave it alone
        mon = _Pkmn("terapagosterastal", ability="insomnia")
        _apply_forme_abilities(_Battler(active=mon))
        self.assertEqual(mon.ability, "insomnia")

    def test_other_species_untouched(self):
        mon = _Pkmn("hatterene", ability="magicbounce")
        _apply_forme_abilities(_Battler(active=mon))
        self.assertEqual(mon.ability, "magicbounce")


class TestEncoreOverrideGate(unittest.TestCase):
    """A `-start ... Encore` landing BEFORE its target's own |move| line means
    the shown move is the Encore-locked one, not the side's decision, so the
    turn is not replayable as a 1-decision-each engine call (synth00543 T31)."""

    def test_encore_before_target_move_is_gated(self):
        block = [
            "|move|p2a: Volbeat|Encore|p1a: Hypno",
            "|-start|p1a: Hypno|Encore",
            "|move|p1a: Hypno|Protect||[still]",
            "|-fail|p1a: Hypno",
        ]
        self.assertEqual(_encore_overridden_side(block, "p1"), "user")
        # same block from the other perspective reports the other side
        self.assertEqual(_encore_overridden_side(block, "p2"), "opp")

    def test_encore_after_target_already_moved_is_not_gated(self):
        # the target acted on its OWN choice this turn; the Encore only binds
        # the NEXT turn, which the pre-turn ENCORE volatile already models
        block = [
            "|move|p1a: Hypno|Protect||[still]",
            "|-fail|p1a: Hypno",
            "|move|p2a: Volbeat|Encore|p1a: Hypno",
            "|-start|p1a: Hypno|Encore",
        ]
        self.assertIsNone(_encore_overridden_side(block, "p1"))

    def test_encore_start_with_move_prefix_recognized(self):
        block = [
            "|move|p2a: Volbeat|Encore|p1a: Hypno",
            "|-start|p1a: Hypno|move: Encore",
            "|move|p1a: Hypno|Protect",
        ]
        self.assertEqual(_encore_overridden_side(block, "p1"), "user")

    def test_encored_side_that_never_moves_is_not_gated(self):
        # nothing to mis-attribute: the side has no |move| line at all, and the
        # existing no-clean-action skip already covers it
        block = [
            "|move|p2a: Volbeat|Encore|p1a: Hypno",
            "|-start|p1a: Hypno|Encore",
        ]
        self.assertIsNone(_encore_overridden_side(block, "p1"))

    def test_unrelated_volatile_start_is_not_gated(self):
        block = [
            "|move|p2a: Volbeat|Taunt|p1a: Hypno",
            "|-start|p1a: Hypno|move: Taunt",
            "|move|p1a: Hypno|Protect",
        ]
        self.assertIsNone(_encore_overridden_side(block, "p1"))


class TestKoMarginDemotion(unittest.TestCase):
    """The installed poke_engine binding takes no damage-branching argument
    (poke_engine.pyi:509), so a hit is folded to one 0.925*max roll.  When the
    real roll sat within a few HP of the defender's HP the folded roll lands on
    the wrong side of the KO threshold and every faint-gated companion event is
    unreachable -- reported SOFT, not HARD."""

    def _snap(self, user_hp, opp_hp):
        return _Snap(
            _Battler(active=_Pkmn("gurdurr", hp=user_hp, max_hp=283)),
            _Battler(active=_Pkmn("glastrier", hp=opp_hp, max_hp=312)),
        )

    def test_protocol_faints_engine_survives_by_three_hp(self):
        # synth00611 T4: crit High Horsepower folds to 199 against Gurdurr's 202,
        # so no branch faints it and Chilling Neigh's +1 atk never fires
        b = branches(
            ["Damage SideTwo: 68"],
            ["Damage SideTwo: 68", "Damage SideOne: 133"],
            ["Damage SideTwo: 68", "Damage SideOne: 199"],
        )
        block = [
            "|move|p1a: Gurdurr|Mach Punch|p2a: Glastrier",
            "|move|p2a: Glastrier|High Horsepower|p1a: Gurdurr",
            "|-damage|p1a: Gurdurr|0 fnt",
            "|faint|p1a: Gurdurr",
            "|-ability|p2a: Glastrier|Chilling Neigh|boost",
            "|-boost|p2a: Glastrier|atk|1",
        ]
        snap = self._snap(202, 262)
        self.assertEqual(
            _ko_margin_sides(b, snap, ("move", "machpunch"), ("move", "highhorsepower"), block, "p1"),
            ["user"],
        )
        fs = [Finding(4, Severity.HARD, "boost", "observed + atk on opp")]
        stats = {}
        _demote_ko_margin_findings(
            fs, b, snap, ("move", "machpunch"), ("move", "highhorsepower"), block, "p1", stats
        )
        self.assertIs(fs[0].severity, Severity.SOFT)
        self.assertIn("[ko-margin]", fs[0].message)
        self.assertEqual(stats["ko_margin_demotions"], 1)

    def test_engine_faints_protocol_survives_is_also_marginal(self):
        # synth02538 T33: folded Surf (168) + Gulp Missile (64) take Heatran to
        # exactly 0, suppressing the missile's Defense drop; the real Surf rolled
        # low and left it alive
        b = branches(
            ["Damage SideTwo: 168", "Damage SideOne: 72", "Damage SideTwo: 64"],
            ["Damage SideTwo: 232"],
        )
        block = [
            "|move|p1a: Cramorant|Surf|p2a: Heatran",
            "|-damage|p2a: Heatran|26/100",
            "|move|p2a: Heatran|Heavy Slam|p1a: Cramorant",
            "|-damage|p2a: Heatran|1/100|[from] ability: Gulp Missile|[of] p1a: Cramorant",
            "|-unboost|p2a: Heatran|def|1",
        ]
        snap = _Snap(
            _Battler(active=_Pkmn("cramorant", hp=183, max_hp=261)),
            _Battler(active=_Pkmn("heatran", hp=232, max_hp=273)),
        )
        self.assertEqual(
            _ko_margin_sides(b, snap, ("move", "surf"), ("move", "heavyslam"), block, "p1"),
            ["opp"],
        )

    def test_agreeing_faint_is_not_demoted(self):
        # synth02878 T3: the engine faints the same mon the protocol does and
        # still drops the companion effect -- a real defect, stays HARD
        b = branches(["Damage SideTwo: 70", "Damage SideOne: 64"])
        block = [
            "|move|p1a: Revavroom|Iron Head|p2a: Cramorant",
            "|-damage|p2a: Cramorant|0 fnt",
            "|-status|p1a: Revavroom|par",
            "|faint|p2a: Cramorant",
        ]
        snap = _Snap(
            _Battler(active=_Pkmn("revavroom", hp=196, max_hp=256)),
            _Battler(active=_Pkmn("cramorant", hp=70, max_hp=261)),
        )
        self.assertEqual(
            _ko_margin_sides(b, snap, ("move", "ironhead"), ("move", "surf"), block, "p1"),
            [],
        )
        fs = [Finding(3, Severity.HARD, "status", "observed par on user")]
        _demote_ko_margin_findings(
            fs, b, snap, ("move", "ironhead"), ("move", "surf"), block, "p1", {}
        )
        self.assertIs(fs[0].severity, Severity.HARD)
        self.assertNotIn("[ko-margin]", fs[0].message)

    def test_wide_faint_disagreement_stays_hard(self):
        # off by 40 HP: not a roll-spread artifact, so the finding is untouched
        b = branches(["Damage SideOne: 162"])
        block = [
            "|move|p2a: Glastrier|High Horsepower|p1a: Gurdurr",
            "|faint|p1a: Gurdurr",
            "|-boost|p2a: Glastrier|atk|1",
        ]
        snap = self._snap(202, 262)
        self.assertEqual(
            _ko_margin_sides(b, snap, ("move", "machpunch"), ("move", "highhorsepower"), block, "p1"),
            [],
        )

    def test_switch_in_target_supplies_the_hp(self):
        # synth02943 T3: the user SWITCHES to Lurantis and Triple Axel folds to
        # 45+88+130 = 263 against its 264 HP -- the margin is measured against the
        # switch target, not the turn-start active
        b = branches(
            ["Switch SideOne: P0 -> P3"],
            ["Switch SideOne: P0 -> P3", "Damage SideOne: 45"],
            ["Switch SideOne: P0 -> P3", "Damage SideOne: 45", "Damage SideOne: 88"],
            [
                "Switch SideOne: P0 -> P3",
                "Damage SideOne: 45",
                "Damage SideOne: 88",
                "Damage SideOne: 130",
            ],
        )
        block = [
            "|switch|p1a: Lurantis|Lurantis, L87, M|264/264",
            "|move|p2a: Quaquaval|Triple Axel|p1a: Lurantis",
            "|-damage|p1a: Lurantis|0 fnt",
            "|faint|p1a: Lurantis",
            "|-ability|p2a: Quaquaval|Moxie|boost",
            "|-boost|p2a: Quaquaval|atk|1",
        ]
        snap = _Snap(
            _Battler(
                active=_Pkmn("ceruledge", hp=54, max_hp=245),
                reserve=[_Pkmn("lurantis", hp=264, max_hp=264)],
            ),
            _Battler(active=_Pkmn("quaquaval", hp=264, max_hp=264)),
        )
        self.assertEqual(
            _ko_margin_sides(
                b, snap, ("switch", "lurantis"), ("move", "tripleaxel"), block, "p1"
            ),
            ["user"],
        )

    def test_negative_heal_counts_as_damage(self):
        # Life Orb recoil stringifies as a negative Heal; netting it is what puts
        # the mon at exactly 0 (so protocol and engine AGREE and nothing is demoted)
        b = branches(["Damage SideOne: 181", "Heal SideOne: -15"])
        block = ["|faint|p1a: Revavroom"]
        snap = _Snap(
            _Battler(active=_Pkmn("revavroom", hp=196, max_hp=256)),
            _Battler(active=_Pkmn("cramorant", hp=261, max_hp=261)),
        )
        self.assertEqual(
            _ko_margin_sides(b, snap, ("move", "ironhead"), ("move", "surf"), block, "p1"),
            [],
        )

    def test_protocol_faints_stops_at_a_post_faint_replacement(self):
        # a REPLACEMENT that faints later in the same block is not the mon the
        # engine simulated, so it must not register as that side's faint
        block = [
            "|move|p2a: Glastrier|High Horsepower|p1a: Gurdurr",
            "|faint|p1a: Gurdurr",
            "|switch|p1a: Ditto|Ditto, L87|1/100",
            "|faint|p2a: Glastrier",
        ]
        self.assertEqual(_protocol_faints(block, "p1"), {"user": True, "opp": False})

    def test_soft_findings_are_left_alone(self):
        b = branches(["Damage SideOne: 199"])
        block = ["|faint|p1a: Gurdurr", "|-heal|p2a: Glastrier|90/100"]
        snap = self._snap(202, 262)
        fs = [Finding(4, Severity.SOFT, "heal", "observed heal on opp")]
        _demote_ko_margin_findings(
            fs, b, snap, ("move", "machpunch"), ("move", "highhorsepower"), block, "p1", {}
        )
        self.assertIs(fs[0].severity, Severity.SOFT)
        self.assertNotIn("[ko-margin]", fs[0].message)


# ---------------------------------------------------------------------------
# PHASE-2 DEFERRED-TURN REPLAY
# ---------------------------------------------------------------------------
import fp.replay.checker as _checker_mod
from fp.replay.checker import (
    _PHASE2_MAX_CALLS,
    _index_int,
    _observed_pivot_switch,
    _observed_revive_target,
    _replay_deferred_phase,
    _reserve_move_string,
)


class _FakePkmn:
    def __init__(self, pid, hp=100, maxhp=100):
        self.id = pid
        self.hp = hp
        self.maxhp = maxhp


class _FakeSide:
    def __init__(
        self,
        pokemon,
        active_index="P0",
        force_switch=False,
        revival_blessing=False,
        saved_move="none",
    ):
        self.pokemon = pokemon
        self.active_index = active_index
        self.force_switch = force_switch
        self.revival_blessing = revival_blessing
        self.switch_out_move_second_saved_move = saved_move


class _FakeState:
    """Stands in for a poke_engine State: apply_instructions returns the
    caller-supplied MID-turn state (what the engine would be looking at when it
    issues the mid-turn decision request)."""

    def __init__(self, mid=None, raises=False):
        self.mid = mid
        self.raises = raises
        self.applied = []

    def apply_instructions(self, b):
        self.applied.append(b)
        if self.raises:
            raise RuntimeError("apply blew up")
        return self.mid


class _Phase2Harness(unittest.TestCase):
    """Installs a recording stub over the module-global generate_instructions
    the checker calls, so the phase-2 path can be driven without the engine."""

    def _install(self, result, raises=False):
        self.calls = []

        def stub(state, s1_move, s2_move):
            self.calls.append((state, s1_move, s2_move))
            if raises:
                raise ValueError("Invalid move for s1: nonsense")
            return [_FakeBranch(r) for r in result]

        self._saved = _checker_mod.generate_instructions
        _checker_mod.generate_instructions = stub
        self.addCleanup(
            setattr, _checker_mod, "generate_instructions", self._saved
        )


def _state_pair(s1, s2, mid_s1, mid_s2, raises=False):
    """(pre-turn state, mid-turn state) fakes wired so
    state.apply_instructions(branch) -> mid."""
    mid = _FakeState()
    mid.side_one, mid.side_two = mid_s1, mid_s2
    pre = _FakeState(mid=mid, raises=raises)
    pre.side_one, pre.side_two = s1, s2
    return pre, mid


class TestDeferredDetection(_Phase2Harness):
    def test_non_deferred_turn_is_untouched_and_makes_no_engine_call(self):
        self._install([["Damage SideOne: 5"]])
        parsed = branches(["Damage SideTwo: 120", "Boost SideOne Attack: 1"])
        pre, _ = _state_pair(
            _FakeSide([_FakePkmn("SNORLAX")]), _FakeSide([_FakePkmn("PAWMOT")]),
            _FakeSide([_FakePkmn("SNORLAX")]), _FakeSide([_FakePkmn("PAWMOT")]),
        )
        stats = {}
        out, ran, _split = _replay_deferred_phase(pre, [object()], parsed, [], "p1", stats)
        self.assertIs(out, parsed)  # same object: the common path is a no-op
        self.assertFalse(ran)
        self.assertEqual(self.calls, [])
        self.assertEqual(pre.applied, [])
        self.assertEqual(stats, {})

    def test_marker_present_but_branch_did_not_defer_is_kept(self):
        # a U-turn that toggled force_switch and then toggled it back (the
        # empty-bench completion): nothing to continue
        self._install([["Damage SideOne: 5"]])
        parsed = branches(
            ["ToggleSideOneForceSwitch", "ToggleSideOneForceSwitch", "Damage SideTwo: 40"]
        )
        pre, _ = _state_pair(
            _FakeSide([_FakePkmn("FLAPPLE")]), _FakeSide([_FakePkmn("DRIFBLIM")]),
            _FakeSide([_FakePkmn("FLAPPLE")], force_switch=False),
            _FakeSide([_FakePkmn("DRIFBLIM")]),
        )
        stats = {}
        out, ran, _split = _replay_deferred_phase(pre, [object()], parsed, [], "p1", stats)
        self.assertFalse(ran)
        self.assertEqual(out, parsed)
        self.assertEqual(self.calls, [])


class TestPhase2RevivalBlessing(_Phase2Harness):
    BLOCK = [
        "|move|p2a: Pawmot|Revival Blessing|p2a: Pawmot",
        "|-heal|p2: Squawkabilly|50/100|[from] move: Revival Blessing",
        "|move|p1a: Snorlax|Curse|p1a: Snorlax",
        "|-unboost|p1a: Snorlax|spe|1",
        "|-boost|p1a: Snorlax|atk|1",
    ]

    def _setup(self):
        # opponent party: active Pawmot + the fainted Squawkabilly-Blue the
        # protocol names (PS reports the BASE species for a cosmetic forme)
        opp = [
            _FakePkmn("PAWMOT", hp=51),
            _FakePkmn("MOLTRESGALAR", hp=271),
            _FakePkmn("ORTHWORM", hp=0),
            _FakePkmn("SQUAWKABILLYBLUE", hp=0),
        ]
        user = [_FakePkmn("SNORLAX", hp=371)]
        return _state_pair(
            _FakeSide(user),
            _FakeSide(opp),
            _FakeSide(user, saved_move="CURSE"),
            _FakeSide(opp, force_switch=True, revival_blessing=True),
        )

    def test_phase2_called_with_revive_target_and_parked_move(self):
        self._install(
            [
                [
                    "ToggleSideTwoForceSwitch",
                    "ToggleRevivalBlessing SideTwo",
                    "Revive SideTwo-P3: 133",
                    "Boost SideOne Attack: 1",
                    "Boost SideOne Speed: -1",
                ]
            ]
        )
        parsed = branches(
            [
                "DecrementPP SideTwo: M0 1",
                "ToggleRevivalBlessing SideTwo",
                "ToggleSideTwoForceSwitch",
                "SideOneMoveSecondSwitchOutMove: NONE -> CURSE",
            ]
        )
        pre, _ = self._setup()
        stats = {}
        out, ran, _split = _replay_deferred_phase(
            pre, [object()], parsed, self.BLOCK, "p1", stats
        )
        self.assertTrue(ran)
        self.assertEqual(len(self.calls), 1)
        _, s1_move, s2_move = self.calls[0]
        # SideOne replays its parked move; SideTwo "switches" to the fainted
        # reserve named by the protocol -- the BARE lowercase id
        self.assertEqual(s1_move, "curse")
        self.assertEqual(s2_move, "squawkabillyblue")
        self.assertEqual(stats["phase2_turns"], 1)
        # one combined branch: phase 1 ++ phase 2, in order
        self.assertEqual(len(out), 1)
        kinds = [i.kind for i in out[0]]
        self.assertEqual(kinds[0], "DecrementPP")
        self.assertIn("Revive", kinds)
        self.assertIn("Boost", kinds)

    def test_union_lets_a_deferred_boost_match(self):
        self._install([["Boost SideOne Attack: 1"]])
        parsed = branches(
            [
                "ToggleRevivalBlessing SideTwo",
                "ToggleSideTwoForceSwitch",
                "SideOneMoveSecondSwitchOutMove: NONE -> CURSE",
            ]
        )
        pre, _ = self._setup()
        out, _, _split = _replay_deferred_phase(
            pre, [object()], parsed, self.BLOCK, "p1", {}
        )
        obs = [ObservedEvent("boost", "user", detail="atk", sign=1, value=1)]
        self.assertEqual(compare_turn(ctx(parsed, obs))[0].category, "boost")
        self.assertEqual(compare_turn(ctx(out, obs)), [])

    def test_no_observed_revive_line_keeps_phase_one_only(self):
        self._install([["Boost SideOne Attack: 1"]])
        parsed = branches(
            [
                "ToggleRevivalBlessing SideTwo",
                "ToggleSideTwoForceSwitch",
                "SideOneMoveSecondSwitchOutMove: NONE -> CURSE",
            ]
        )
        pre, _ = self._setup()
        stats = {}
        out, ran, _split = _replay_deferred_phase(pre, [object()], parsed, [], "p1", stats)
        self.assertFalse(ran)
        self.assertEqual(out, parsed)
        self.assertEqual(self.calls, [])
        self.assertEqual(stats["phase2_no_observed_switch"], 1)

    def test_engine_failure_degrades_to_phase_one(self):
        self._install([[]], raises=True)
        parsed = branches(
            [
                "ToggleRevivalBlessing SideTwo",
                "ToggleSideTwoForceSwitch",
                "SideOneMoveSecondSwitchOutMove: NONE -> CURSE",
            ]
        )
        pre, _ = self._setup()
        stats = {}
        out, ran, _split = _replay_deferred_phase(
            pre, [object()], parsed, self.BLOCK, "p1", stats
        )
        self.assertFalse(ran)
        self.assertEqual(out, parsed)
        self.assertEqual(stats["phase2_call_errors"], 1)

    def test_state_apply_failure_degrades_to_phase_one(self):
        self._install([["Boost SideOne Attack: 1"]])
        parsed = branches(
            ["ToggleRevivalBlessing SideTwo", "ToggleSideTwoForceSwitch"]
        )
        pre, _ = self._setup()
        pre.raises = True
        stats = {}
        out, ran, _split = _replay_deferred_phase(
            pre, [object()], parsed, self.BLOCK, "p1", stats
        )
        self.assertFalse(ran)
        self.assertEqual(stats["phase2_apply_errors"], 1)
        self.assertEqual(self.calls, [])


class TestPhase2Pivot(_Phase2Harness):
    BLOCK = [
        "|move|p2a: Drifblim|Calm Mind|p2a: Drifblim",
        "|move|p1a: Flapple|U-turn|p2a: Drifblim",
        "|-damage|p2a: Drifblim|37/100 brn",
        "|switch|p1a: Ho-Oh|Ho-Oh, L71|268/268|[from] U-turn",
    ]

    def _setup(self, saved="CALMMIND"):
        user = [_FakePkmn("FLAPPLE", hp=200), _FakePkmn("HOOH", hp=268)]
        opp = [_FakePkmn("DRIFBLIM", hp=90)]
        return _state_pair(
            _FakeSide(user),
            _FakeSide(opp),
            _FakeSide(user, force_switch=True),
            _FakeSide(opp, saved_move=saved),
        )

    def test_user_pivot_switch_target_from_protocol(self):
        self._install([["Switch SideOne: P0 -> P1", "Boost SideTwo Attack: 1"]])
        parsed = branches(
            [
                "Damage SideTwo: 40",
                "ToggleSideOneForceSwitch",
                "SideTwoMoveSecondSwitchOutMove: NONE -> CALMMIND",
            ]
        )
        pre, _ = self._setup()
        out, ran, _split = _replay_deferred_phase(
            pre, [object()], parsed, self.BLOCK, "p1", {}
        )
        self.assertTrue(ran)
        self.assertEqual(self.calls[0][1:], ("hooh", "calmmind"))
        self.assertEqual([i.kind for i in out[0]][-1], "Boost")

    def test_parked_mover_that_fainted_replays_as_none(self):
        self._install([["Switch SideOne: P0 -> P1"]])
        parsed = branches(["ToggleSideOneForceSwitch"])
        pre, mid = self._setup()
        mid.side_two.pokemon[0].hp = 0
        _replay_deferred_phase(pre, [object()], parsed, self.BLOCK, "p1", {})
        self.assertEqual(self.calls[0][1:], ("hooh", "none"))

    def test_no_saved_move_replays_as_none(self):
        self._install([["Switch SideOne: P0 -> P1"]])
        parsed = branches(["ToggleSideOneForceSwitch"])
        pre, _ = self._setup(saved="none")
        _replay_deferred_phase(pre, [object()], parsed, self.BLOCK, "p1", {})
        self.assertEqual(self.calls[0][1:], ("hooh", "none"))

    def test_per_branch_continuation_is_branch_consistent(self):
        # two phase-1 branches, each continued separately; the union keeps each
        # phase-1 prefix glued to its own phase-2 suffix
        self._install([["Switch SideOne: P0 -> P1"], ["Switch SideOne: P0 -> P1", "Damage SideOne: 9"]])
        parsed = branches(
            ["Damage SideTwo: 40", "ToggleSideOneForceSwitch"],
            ["Damage SideTwo: 47", "ToggleSideOneForceSwitch"],
        )
        pre, _ = self._setup()
        out, ran, _split = _replay_deferred_phase(
            pre, [object(), object()], parsed, self.BLOCK, "p1", {}
        )
        self.assertTrue(ran)
        self.assertEqual(len(out), 4)  # 2 phase-1 x 2 phase-2
        self.assertEqual(len(self.calls), 2)
        firsts = {b[0].amount() for b in out}
        self.assertEqual(firsts, {40, 47})

    def test_call_budget_is_capped(self):
        self._install([["Switch SideOne: P0 -> P1"]])
        n = _PHASE2_MAX_CALLS + 4
        parsed = branches(*([["ToggleSideOneForceSwitch"]] * n))
        pre, _ = self._setup()
        out, ran, _split = _replay_deferred_phase(
            pre, [object()] * n, parsed, self.BLOCK, "p1", {}
        )
        self.assertTrue(ran)
        self.assertEqual(len(self.calls), _PHASE2_MAX_CALLS)
        self.assertEqual(len(out), n)  # capped branches keep their phase-1 form


class TestDeferredTargetResolution(unittest.TestCase):
    def test_pivot_switch_must_follow_the_side_s_own_move(self):
        block = [
            "|switch|p1a: Flapple|Flapple, L88, M|200/200",  # turn-start decision
            "|move|p2a: Drifblim|Calm Mind|p2a: Drifblim",
        ]
        self.assertIsNone(_observed_pivot_switch(block, "p1"))

    def test_pivot_switch_after_move_is_found(self):
        block = [
            "|move|p1a: Flapple|U-turn|p2a: Drifblim",
            "|switch|p1a: Ho-Oh|Ho-Oh, L71|268/268|[from] U-turn",
        ]
        self.assertEqual(_observed_pivot_switch(block, "p1"), "hooh")

    def test_post_faint_replacement_is_not_a_pivot_switch(self):
        block = [
            "|move|p1a: Flapple|U-turn|p2a: Drifblim",
            "|faint|p1a: Flapple",
            "|switch|p1a: Ho-Oh|Ho-Oh, L71|268/268",
        ]
        self.assertIsNone(_observed_pivot_switch(block, "p1"))

    def test_other_side_s_switch_is_ignored(self):
        block = [
            "|move|p1a: Flapple|U-turn|p2a: Drifblim",
            "|switch|p2a: Ho-Oh|Ho-Oh, L71|268/268",
        ]
        self.assertIsNone(_observed_pivot_switch(block, "p1"))

    def test_revive_target_read_from_the_heal_line(self):
        block = ["|-heal|p2: Vigoroth|50/100|[from] move: Revival Blessing"]
        self.assertEqual(_observed_revive_target(block, "p2"), "vigoroth")
        self.assertIsNone(_observed_revive_target(block, "p1"))

    def test_plain_heal_is_not_a_revive(self):
        block = ["|-heal|p2a: Pawmot|90/100|[from] item: Leftovers"]
        self.assertIsNone(_observed_revive_target(block, "p2"))

    def test_reserve_id_exact_match(self):
        side = _FakeSide(
            [_FakePkmn("PAWMOT", hp=51), _FakePkmn("ORTHWORM", hp=0)]
        )
        self.assertEqual(_reserve_move_string(side, "orthworm", True), "orthworm")

    def test_cosmetic_forme_resolved_by_prefix(self):
        # protocol says "p2: Squawkabilly"; the engine id is SQUAWKABILLYBLUE
        side = _FakeSide(
            [
                _FakePkmn("PAWMOT", hp=51),
                _FakePkmn("ORTHWORM", hp=0),
                _FakePkmn("SQUAWKABILLYBLUE", hp=0),
            ]
        )
        self.assertEqual(
            _reserve_move_string(side, "squawkabilly", True), "squawkabillyblue"
        )

    def test_ambiguous_prefix_refuses_to_guess(self):
        side = _FakeSide(
            [
                _FakePkmn("PAWMOT", hp=51),
                _FakePkmn("SQUAWKABILLYBLUE", hp=0),
                _FakePkmn("SQUAWKABILLYWHITE", hp=0),
            ]
        )
        self.assertIsNone(_reserve_move_string(side, "squawkabilly", True))

    def test_revive_target_must_be_fainted_and_pivot_target_alive(self):
        side = _FakeSide(
            [_FakePkmn("PAWMOT", hp=51), _FakePkmn("ORTHWORM", hp=0)]
        )
        self.assertIsNone(_reserve_move_string(side, "orthworm", False))
        alive = _FakeSide(
            [_FakePkmn("FLAPPLE", hp=200), _FakePkmn("HOOH", hp=268)]
        )
        self.assertEqual(_reserve_move_string(alive, "hooh", False), "hooh")
        self.assertIsNone(_reserve_move_string(alive, "hooh", True))

    def test_active_is_never_a_target(self):
        side = _FakeSide([_FakePkmn("PAWMOT", hp=51)], active_index="P0")
        self.assertIsNone(_reserve_move_string(side, "pawmot", False))

    def test_empty_party_slot_is_not_revivable(self):
        side = _FakeSide(
            [_FakePkmn("PAWMOT", hp=51), _FakePkmn("NONE", hp=0, maxhp=0)]
        )
        self.assertIsNone(_reserve_move_string(side, "none", True))

    def test_index_int(self):
        self.assertEqual(_index_int("P0"), 0)
        self.assertEqual(_index_int("PokemonIndex.P4"), 4)


class TestImmunitySelfInflictedDamageDiscount(unittest.TestCase):
    """synth02141 T40: Shadow Ball into a Normal type while the same turn's
    Boomburst is blocked by a Ghost.  With NO move damage anywhere there is no
    background to subtract, so the confusion self-hit (branch A) and the toxic
    tick (branch B) used to read as 'every branch damages the immune side'."""

    def test_confusion_self_hit_and_toxic_tick_are_discounted(self):
        b = branches(
            ["ChangeVolatileStatusDuration SideTwo CONFUSION: 1", "Damage SideTwo: 10"],
            [
                "ChangeVolatileStatusDuration SideTwo CONFUSION: 1",
                "Heal SideTwo: 21",
                "Damage SideTwo: 31",
                "ChangeSideCondition SideTwo ToxicCount: 1",
            ],
        )
        obs = [ObservedEvent("immune", "opp", trigger_move="shadowball")]
        self.assertEqual(compare_turn(ctx(b, obs)), [])

    def test_toxic_tick_alone_is_discounted(self):
        # synth00165 T23 shape: Brave Bird recoil (already discounted) plus the
        # toxic tick that immediately precedes the ToxicCount increment
        b = branches(
            [
                "Damage SideTwo: 296",
                "Damage SideOne: 97",
                "Damage SideOne: 64",
                "ChangeSideCondition SideOne ToxicCount: 1",
            ],
            [
                "Damage SideTwo: 314",
                "Damage SideOne: 103",
                "Damage SideOne: 62",
                "ChangeSideCondition SideOne ToxicCount: 1",
            ],
        )
        obs = [ObservedEvent("immune", "user", trigger_move="headlongrush")]
        c = TurnContext(
            turn=1,
            branches=b,
            observed=obs,
            user_move="bravebird",
            opp_move="headlongrush",
        )
        self.assertEqual(compare_turn(c), [])

    def test_burn_and_poison_chip_sized_off_maxhp(self):
        # maxhp 320 -> burn trunc(320/16)=20, poison trunc(320/8)=40
        b = branches(["Damage SideOne: 20"], ["Damage SideOne: 40"])
        obs = [ObservedEvent("immune", "user", trigger_move="earthquake")]
        c = TurnContext(
            turn=1, branches=b, observed=obs, side_maxhp={"s1": 320, "s2": 300}
        )
        self.assertEqual(compare_turn(c), [])

    def test_without_maxhp_the_fraction_rule_is_skipped(self):
        b = branches(["Damage SideOne: 20"], ["Damage SideOne: 40"])
        obs = [ObservedEvent("immune", "user", trigger_move="earthquake")]
        self.assertEqual(len(compare_turn(ctx(b, obs))), 1)

    def test_move_scale_breach_still_fires_hard(self):
        # true-positive control: the confusion self-hit IS discounted, but the
        # move-scale damage sitting on the immune side in every branch is not
        b = branches(
            [
                "ChangeVolatileStatusDuration SideOne CONFUSION: 1",
                "Damage SideOne: 12",
                "Damage SideOne: 195",
            ],
            [
                "ChangeVolatileStatusDuration SideOne CONFUSION: 1",
                "Damage SideOne: 12",
                "Damage SideOne: 233",
            ],
        )
        obs = [ObservedEvent("immune", "user", trigger_move="earthquake")]
        c = TurnContext(
            turn=1, branches=b, observed=obs, side_maxhp={"s1": 320, "s2": 300}
        )
        fs = compare_turn(c)
        self.assertEqual(len(fs), 1)
        self.assertIs(fs[0].severity, Severity.HARD)

    def test_only_one_damage_per_rule_is_discounted(self):
        # two damages after the confusion marker: only the adjacent one goes
        b = branches(
            [
                "ChangeVolatileStatusDuration SideOne CONFUSION: 1",
                "Damage SideOne: 60",
                "Damage SideOne: 61",
            ],
            [
                "ChangeVolatileStatusDuration SideOne CONFUSION: 1",
                "Damage SideOne: 55",
                "Damage SideOne: 56",
            ],
        )
        obs = [ObservedEvent("immune", "user", trigger_move="earthquake")]
        fs = compare_turn(ctx(b, obs))
        self.assertEqual(len(fs), 1)

    def test_confusion_marker_without_adjacent_damage_discounts_nothing(self):
        # the move-continuation sibling carries the same duration change but no
        # self-damage; a real breach behind it must survive
        b = branches(
            ["ChangeVolatileStatusDuration SideOne CONFUSION: 1", "Boost SideOne Attack: 1", "Damage SideOne: 195"],
            ["ChangeVolatileStatusDuration SideOne CONFUSION: 1", "Boost SideOne Attack: 1", "Damage SideOne: 233"],
        )
        obs = [ObservedEvent("immune", "user", trigger_move="earthquake")]
        fs = compare_turn(ctx(b, obs))
        self.assertEqual(len(fs), 1)
        self.assertIs(fs[0].severity, Severity.HARD)


class TestHealMatchesRevive(unittest.TestCase):
    def test_revival_blessing_revive_satisfies_the_heal_event(self):
        b = branches(["ToggleRevivalBlessing SideTwo", "Revive SideTwo-P3: 133"])
        obs = [
            ObservedEvent(
                "heal",
                "opp",
                raw="|-heal|p2: Squawkabilly|50/100|[from] move: Revival Blessing",
            )
        ]
        self.assertEqual(compare_turn(ctx(b, obs)), [])

    def test_revive_on_the_other_side_does_not_satisfy_it(self):
        b = branches(["Revive SideOne-P3: 133"])
        obs = [ObservedEvent("heal", "opp", raw="|-heal|p2: X|50/100")]
        fs = compare_turn(ctx(b, obs))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].category, "heal")


# ---------------------------------------------------------------------------
# PS-exactness wave: (1) roster fill must precede exact-team application,
# (2) illusion span excludes the entry turn, (3) deferred-turn phase
# attribution, (4) ChangeMaxHP heals + fatigue-confusion -END symmetry,
# (5) Cud Chew arming
# ---------------------------------------------------------------------------
from fp.replay.checker import (  # noqa: E402
    _arm_cudchew,
    _backfill_roster,
    _eot_skipped_by_faint_replacement,
    _residual_phase_start,
    _second_t_boundary,
)


class TestRosterFillOrdering(unittest.TestCase):
    """A mon making its FIRST appearance during the modeled turn is not on the
    snapshot's roster, so every knowledge pass that walks the roster (the exact
    full-knowledge sidecar, the randbats inference) used to miss it entirely --
    it reached the engine with no ability, no item and default-EV stats even on
    a full-knowledge corpus (synth01954 T14: Amoonguss)."""

    CHUNKS = [
        ">battle-x",
        "|turn|13",
        "|switch|p2a: Klawf|Klawf, L82, M|100/100",
        "|turn|14",
        "|switch|p2a: Amoonguss|Amoonguss, L82, F|100/100",
    ]

    def test_backfill_roster_adds_the_unseen_mon(self):
        battler = _Battler(active=_Pkmn("klawf"))
        rev = _harvest_reveals(self.CHUNKS)
        _backfill_roster(battler, "p2", rev)
        self.assertIn("amoonguss", [p.name for p in battler.reserve])

    def test_backfill_roster_is_idempotent(self):
        # _backfill_revealed_knowledge still calls it, so a second call from
        # _fire_turn must not duplicate the party
        battler = _Battler(active=_Pkmn("klawf"))
        rev = _harvest_reveals(self.CHUNKS)
        _backfill_roster(battler, "p2", rev)
        _backfill_roster(battler, "p2", rev)
        _backfill_revealed_knowledge(battler, "p2", rev, 14)
        self.assertEqual(
            [p.name for p in battler.reserve].count("amoonguss"),
            1,
            "roster fill must be idempotent across the pre-pass and the "
            "_backfill_revealed_knowledge call that still performs it",
        )

    def test_exact_team_reaches_a_debut_mon_only_after_the_roster_fill(self):
        from fp.replay.damage_membership import apply_exact_team

        lookup = {
            "amoonguss": {
                "species": "Amoonguss",
                "ability": "Regenerator",
                "item": "Rocky Helmet",
                "moves": [],
            }
        }
        rev = _harvest_reveals(self.CHUNKS)

        # OLD order: exact teams before the roster fill -> the debut mon is
        # simply not there yet and gets nothing
        battler = _Battler(active=_Pkmn("klawf"))
        apply_exact_team(battler, lookup, is_user=False)
        _backfill_roster(battler, "p2", rev)
        debut = next(p for p in battler.reserve if p.name == "amoonguss")
        self.assertFalse(debut.ability)
        self.assertEqual(debut.item, constants.UNKNOWN_ITEM)

        # NEW order: roster fill first -> the sidecar reaches it
        battler = _Battler(active=_Pkmn("klawf"))
        _backfill_roster(battler, "p2", rev)
        apply_exact_team(battler, lookup, is_user=False)
        debut = next(p for p in battler.reserve if p.name == "amoonguss")
        self.assertEqual(debut.ability, "regenerator")
        self.assertEqual(debut.item, "rockyhelmet")


class TestIllusionEntryTurn(unittest.TestCase):
    """`start_turn` is the turn during whose RESOLUTION the disguised mon
    switched in, so it is NOT on the field in that turn's pre-state -- very
    often the REAL mon of the disguise species is (synth00421 T23: Forretress
    typed Normal/Ghost made Drain Punch read as immune)."""

    REVEALS = {
        "illusions": [
            {
                "pid": "p2",
                "disguise": "forretress",
                "true_species": "zoroarkhisui",
                "start_turn": 23,
                "end_turn": 24,
            }
        ]
    }

    def _typed(self, turn):
        mon = _Pkmn("forretress")
        mon.types = ["bug", "steel"]
        _apply_illusion(_Battler(active=mon), "p2", self.REVEALS, turn=turn)
        return mon.types

    def test_entry_turn_keeps_the_real_mons_types(self):
        self.assertEqual(self._typed(23), ["bug", "steel"])

    def test_reveal_turn_still_gets_the_disguise_types(self):
        # the |replace| happens mid-turn, so the reveal turn's PRE-state still
        # holds the disguise: end_turn stays inclusive
        self.assertEqual(self._typed(24), ["normal", "ghost"])

    def test_turn_before_entry_unaffected(self):
        self.assertEqual(self._typed(22), ["bug", "steel"])


class TestPhase2Attribution(unittest.TestCase):
    """The deferred-turn branch list is `phase-1 ++ phase-2`; an event from
    before the block's second `|t:|` may only be satisfied by a phase-1
    instruction, and vice versa."""

    BLOCK = [
        "|",
        "|t:|1",
        "|move|p1a: A|U-turn|p2a: B",
        "|-enditem|p2a: B|Sitrus Berry|[eat]",  # index 3 -- PHASE 1
        "|",
        "|t:|2",  # index 5 -- the boundary
        "|switch|p1a: C|C, L80|100/100",
        "|",
        "|-enditem|p2a: B|Sitrus Berry|[eat]",  # index 8 -- PHASE 2
        "|upkeep",
    ]

    def test_second_t_boundary(self):
        self.assertEqual(_second_t_boundary(self.BLOCK), 5)
        self.assertIsNone(_second_t_boundary(self.BLOCK[:5]))

    def test_phase2_instruction_cannot_satisfy_a_phase1_event(self):
        # one combined branch: 2 phase-1 instructions, then the phase-2 eat
        b = branches(
            ["Damage SideTwo: 40", "ToggleSideOneForceSwitch", "ChangeItem SideTwo: SITRUSBERRY -> NONE"]
        )
        obs = [ObservedEvent("item_end", "opp", detail="Sitrus Berry", raw="|-enditem|p2a: B|Sitrus Berry|[eat]", line_index=3)]
        c = TurnContext(
            turn=1,
            branches=b,
            observed=obs,
            phase_split=[2],
            phase2_line_index=5,
        )
        fs = compare_turn(c)
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].category, "item")

    def test_phase2_instruction_satisfies_a_phase2_event(self):
        b = branches(
            ["Damage SideTwo: 40", "ToggleSideOneForceSwitch", "ChangeItem SideTwo: SITRUSBERRY -> NONE"]
        )
        obs = [ObservedEvent("item_end", "opp", detail="Sitrus Berry", raw="|-enditem|p2a: B|Sitrus Berry|[eat]", line_index=8)]
        c = TurnContext(
            turn=1,
            branches=b,
            observed=obs,
            phase_split=[2],
            phase2_line_index=5,
        )
        self.assertEqual(compare_turn(c), [])

    def test_without_attribution_the_union_is_matched_as_before(self):
        b = branches(
            ["Damage SideTwo: 40", "ToggleSideOneForceSwitch", "ChangeItem SideTwo: SITRUSBERRY -> NONE"]
        )
        obs = [ObservedEvent("item_end", "opp", detail="Sitrus Berry", raw="|-enditem|p2a: B|X|[eat]", line_index=3)]
        self.assertEqual(compare_turn(ctx(b, obs)), [])

    def test_extraction_records_the_block_line_index(self):
        evs = extract_observed_events(
            ["|move|p1a: A|Toxic|p2a: B", "|-status|p2a: B|tox"], "p1"
        )
        self.assertEqual([e.line_index for e in evs], [1])

    def test_replay_deferred_phase_reports_no_split_when_nothing_deferred(self):
        parsed = parse_branches([_FakeBranch(["Damage SideOne: 10"])])
        out, ran, split = _replay_deferred_phase(
            _FakeState(), [object()], parsed, [], "p1", {}
        )
        self.assertIs(out, parsed)
        self.assertFalse(ran)
        self.assertIsNone(split)


class TestChangeMaxHpSatisfiesHeal(unittest.TestCase):
    """A max-HP-raising forme change (Terapagos-Terastal -> -Stellar) is a
    SILENT `-heal` in PS and a `ChangeMaxHP` in the engine."""

    def test_changemaxhp_satisfies_the_silent_heal(self):
        b = branches(
            [
                "ToggleTerastallized SideOne",
                "FormeChange SideOne 1",
                "ChangeMaxHP SideOne: maxhp 100 hp 100",
            ]
        )
        obs = [
            ObservedEvent(
                "heal", "user", raw="|-heal|p1a: Terapagos|311/373|[silent]"
            )
        ]
        self.assertEqual(compare_turn(ctx(b, obs)), [])

    def test_changemaxhp_on_the_other_side_does_not_satisfy_it(self):
        b = branches(["ChangeMaxHP SideTwo: maxhp 100 hp 100"])
        obs = [ObservedEvent("heal", "user", raw="|-heal|p1a: Terapagos|311/373|[silent]")]
        fs = compare_turn(ctx(b, obs))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].category, "heal")


class TestSecondaryCauseVolatileEnd(unittest.TestCase):
    """The `-end` paired with a same-instant `[fatigue]`/`[silent]` `-start` is
    the same reconstruction limit as the `-start` (already excused) and the
    berry `-enditem` (already excused): Outrage fatigue -> confusion -> Lum."""

    BLOCK = [
        "|move|p1a: Flygon|Outrage|p2a: Ursaring|[from] lockedmove",
        "|-damage|p2a: Ursaring|68/100",
        "|-start|p1a: Flygon|confusion|[fatigue]",
        "|-enditem|p1a: Flygon|Lum Berry|[eat]",
        "|-end|p1a: Flygon|confusion",
    ]

    def test_paired_end_is_suppressed(self):
        evs = extract_observed_events(self.BLOCK, "p1")
        self.assertFalse(any(e.kind == "volatile_end" for e in evs))

    def test_start_and_item_stay_suppressed_too(self):
        evs = extract_observed_events(self.BLOCK, "p1")
        self.assertFalse(any(e.kind == "item_end" for e in evs))
        self.assertEqual(compare_turn(ctx(branches([]), evs)), [])

    def test_unrelated_end_still_emitted(self):
        block = [
            "|move|p1a: Dragonite|Dragon Dance|p1a: Dragonite",
            "|-end|p1a: Dragonite|confusion",
        ]
        evs = extract_observed_events(block, "p1")
        self.assertTrue(any(e.kind == "volatile_end" for e in evs))

    def test_end_of_a_different_volatile_still_emitted(self):
        # the back-scan is PAIRED: only the volatile the tagged -start applied
        block = [
            "|move|p1a: Flygon|Outrage|p2a: Ursaring|[from] lockedmove",
            "|-start|p1a: Flygon|confusion|[fatigue]",
            "|-end|p1a: Flygon|Taunt",
        ]
        evs = extract_observed_events(block, "p1")
        self.assertEqual(
            [e.detail for e in evs if e.kind == "volatile_end"], ["Taunt"]
        )


class TestAbilityCuredStatus(unittest.TestCase):
    """A status-immunity ability applies-and-cures in one instant in PS; the
    engine models it as a plain immunity and emits no ChangeStatus."""

    def test_ability_cured_status_is_not_asserted(self):
        block = [
            "|move|p1a: Reshiram|Will-O-Wisp|p2a: Floatzel",
            "|-status|p2a: Floatzel|brn",
            "|-activate|p2a: Floatzel|ability: Water Veil",
            "|-curestatus|p2a: Floatzel|brn|[msg]",
        ]
        evs = extract_observed_events(block, "p1")
        st = [e for e in evs if e.kind == "status"]
        self.assertEqual([e.cure_ability for e in st], ["waterveil"])
        self.assertEqual(compare_turn(ctx(branches([]), evs)), [])

    def test_a_status_that_sticks_is_still_asserted(self):
        block = [
            "|move|p1a: Reshiram|Will-O-Wisp|p2a: Floatzel",
            "|-status|p2a: Floatzel|brn",
        ]
        evs = extract_observed_events(block, "p1")
        self.assertEqual([e.cure_ability for e in evs if e.kind == "status"], [""])
        fs = compare_turn(ctx(branches(["Damage SideOne: 10"]), evs))
        self.assertEqual([f.category for f in fs], ["status"])


class TestFaintReplacementSkipsEndOfTurn(unittest.TestCase):
    """`skip_end_of_turn = s1_replacing_fainted_pkmn || s2_replacing_fainted_pkmn`
    (genx/generate_instructions.rs:9504): a faint-replacement call has no
    residual phase, so the block's residual lines must not be asserted."""

    BLOCK = [
        "|",
        "|t:|1",
        "|switch|p1a: Zebstrika|Zebstrika, L87, M|245/272|[from] U-turn",
        "|",  # index 3 -- opens the residual phase
        "|-weather|none",
        "|upkeep",
        "|",
        "|t:|1",
        "|switch|p2a: Florges|Florges-Yellow, L85, F|100/100",
    ]

    def test_residual_phase_start(self):
        self.assertEqual(_residual_phase_start(self.BLOCK), 3)
        self.assertIsNone(_residual_phase_start(["|", "|move|p1a: A|Tackle|p2a: B"]))

    def test_detected_when_a_side_switches_in_a_fainted_active(self):
        snap = _Snap(
            _Battler(active=_Pkmn("pelipper", hp=0)),
            _Battler(active=_Pkmn("scizor", hp=50)),
        )
        self.assertTrue(
            _eot_skipped_by_faint_replacement(
                snap, ("switch", "zebstrika"), ("switch", "florges")
            )
        )

    def test_not_detected_for_a_plain_double_switch(self):
        snap = _Snap(
            _Battler(active=_Pkmn("pelipper", hp=50)),
            _Battler(active=_Pkmn("scizor", hp=50)),
        )
        self.assertFalse(
            _eot_skipped_by_faint_replacement(
                snap, ("switch", "zebstrika"), ("switch", "florges")
            )
        )

    def test_not_detected_when_the_action_is_a_move(self):
        snap = _Snap(
            _Battler(active=_Pkmn("pelipper", hp=0)),
            _Battler(active=_Pkmn("scizor", hp=50)),
        )
        self.assertFalse(
            _eot_skipped_by_faint_replacement(
                snap, ("move", "tackle"), ("move", "tackle")
            )
        )


class TestCudChewArming(unittest.TestCase):
    """The engine's `cudchew_end_of_turn` is gated on the CUDCHEW volatile AND a
    two-state duration; nothing in the reconstruction set either, so the
    observed end-of-turn re-eat was unreproducible (15 sweep findings)."""

    CHUNKS = [
        ">battle-x",
        "|turn|51",
        "|switch|p1a: Tauros|Tauros-Paldea-Aqua, L81, M|252/252",
        "|turn|52",
        "|move|p1a: Tauros|Substitute|p1a: Tauros",
        "|-damage|p1a: Tauros|101/252",
        "|-enditem|p1a: Tauros|Sitrus Berry|[eat]",
        "|-heal|p1a: Tauros|164/252|[from] item: Sitrus Berry",
        "|turn|53",
    ]

    def _mon(self):
        m = _Pkmn("taurospaldeaaqua", ability="cudchew")
        m.volatile_statuses = []
        from collections import defaultdict

        m.volatile_status_durations = defaultdict(lambda: 0)
        return m

    def test_berry_eat_turn_is_harvested(self):
        rev = _harvest_reveals(self.CHUNKS)
        self.assertEqual(rev["berry_eats"], {("p1", "taurospaldeaaqua"): {52}})

    def test_cudchew_reeat_does_not_rearm(self):
        chunks = self.CHUNKS + [
            "|move|p1a: Tauros|Bulk Up|p1a: Tauros",
            "|",
            "|-activate|p1a: Tauros|ability: Cud Chew",
            "|-enditem|p1a: Tauros|Sitrus Berry|[eat]",
            "|turn|54",
        ]
        rev = _harvest_reveals(chunks)
        self.assertEqual(rev["berry_eats"], {("p1", "taurospaldeaaqua"): {52}})

    def test_armed_on_the_turn_after_the_eat(self):
        mon = self._mon()
        _arm_cudchew(_Battler(active=mon), "p1", _harvest_reveals(self.CHUNKS), 53)
        self.assertIn("cudchew", mon.volatile_statuses)
        self.assertEqual(mon.volatile_status_durations["cudchew"], 1)

    def test_not_armed_on_the_eat_turn_itself(self):
        mon = self._mon()
        _arm_cudchew(_Battler(active=mon), "p1", _harvest_reveals(self.CHUNKS), 52)
        self.assertNotIn("cudchew", mon.volatile_statuses)

    def test_not_armed_two_turns_later(self):
        mon = self._mon()
        _arm_cudchew(_Battler(active=mon), "p1", _harvest_reveals(self.CHUNKS), 54)
        self.assertNotIn("cudchew", mon.volatile_statuses)

    def test_not_armed_without_the_ability(self):
        mon = self._mon()
        mon.ability = "intimidate"
        _arm_cudchew(_Battler(active=mon), "p1", _harvest_reveals(self.CHUNKS), 53)
        self.assertNotIn("cudchew", mon.volatile_statuses)


class TestCudChewDurationForwarded(unittest.TestCase):
    def test_helper_forwards_the_cudchew_duration_when_supported(self):
        import fp.search.poke_engine_helpers as helpers
        from poke_engine import VolatileStatusDurations

        self.assertTrue(helpers.POKE_ENGINE_SUPPORTS_CUDCHEW_DURATION)
        self.assertTrue(hasattr(VolatileStatusDurations, "cudchew"))


class TestCumulativeSideState(unittest.TestCase):
    """`side.totalFainted` (sim/battle.ts:2549-2551) and the `side.pokemon`
    permutation `switchIn` performs (sim/battle-actions.ts:129-131)."""

    def setUp(self):
        from fp.replay.checker import _apply_block_to_side_state, _initial_party_order

        self.apply = _apply_block_to_side_state
        self.initial = _initial_party_order

    def test_initial_order_is_the_sidecar_team_order(self):
        exact = {
            "p1": {"gengar": {}, "forretress": {}},
            "p2": {"fezandipiti": {}, "terapagos": {}, "gyarados": {}},
        }
        self.assertEqual(
            self.initial(exact)["p2"], ["fezandipiti", "terapagos", "gyarados"]
        )

    def test_no_sidecar_means_no_party_order(self):
        self.assertEqual(self.initial(None), {"p1": [], "p2": []})

    def test_faints_accumulate_and_never_decrease(self):
        faints = {"p1": 0, "p2": 0}
        order = {"p1": [], "p2": []}
        self.apply(["|faint|p2a: Gyarados", "|faint|p1a: Gengar"], faints, order)
        self.assertEqual(faints, {"p1": 1, "p2": 1})
        # a Revival Blessing revive leaves totalFainted alone
        self.apply(
            [
                "|-heal|p2a: Gyarados|50/100|[from] move: Revival Blessing",
                "|switch|p2a: Gyarados|Gyarados, L80, M|50/100",
            ],
            faints,
            order,
        )
        self.assertEqual(faints["p2"], 1)

    def test_switch_in_swaps_the_party_slots(self):
        faints = {"p1": 0, "p2": 0}
        order = {"p1": [], "p2": ["fezandipiti", "terapagos", "gyarados"]}
        self.apply(["|switch|p2a: Gyarados|Gyarados, L80, M|100/100"], faints, order)
        self.assertEqual(order["p2"], ["gyarados", "terapagos", "fezandipiti"])
        self.apply(["|switch|p2a: Terapagos|Terapagos, L77, F|100/100"], faints, order)
        self.assertEqual(order["p2"], ["terapagos", "gyarados", "fezandipiti"])

    def test_replace_is_not_a_switch(self):
        # |replace| is Illusion revealing itself, not a switchIn
        faints = {"p1": 0, "p2": 0}
        order = {"p1": [], "p2": ["fezandipiti", "zoroark"]}
        self.apply(["|replace|p2a: Zoroark|Zoroark, L80, M|100/100"], faints, order)
        self.assertEqual(order["p2"], ["fezandipiti", "zoroark"])

    def test_switch_in_of_a_mid_battle_forme_still_swaps(self):
        # corpus synth49466: `|switch|p1a: Palafin|Palafin-Hero, L77, F|...`
        # against a sidecar keyed `palafin`.  The old exact `order.index(key)`
        # raised ValueError and silently `continue`d, leaving the party order
        # permanently out of step with PS's `side.pokemon` -- and Beat Up then
        # read per-hit base powers straight off that stale order.
        faints = {"p1": 0, "p2": 0}
        order = {"p1": ["duraludon", "skeledirge", "palafin"], "p2": []}
        self.apply(
            ["|switch|p1a: Palafin|Palafin-Hero, L77, F|183/281"], faints, order
        )
        self.assertEqual(order["p1"], ["palafin", "skeledirge", "duraludon"])

    def test_every_common_persisting_forme_resolves(self):
        for details, base in (
            ("Terapagos-Terastal, L77", "terapagos"),
            ("Mimikyu-Busted, L80, F", "mimikyu"),
            ("Eiscue-Noice, L80", "eiscue"),
            ("Morpeko-Hangry, L88, M", "morpeko"),
            ("Ogerpon-Wellspring-Tera, L77, F", "ogerponwellspring"),
            ("Zacian-Crowned, L72", "zacian"),
        ):
            faints = {"p1": 0, "p2": 0}
            order = {"p1": [], "p2": ["gyarados", base]}
            self.apply(["|switch|p2a: X|" + details + "|100/100"], faints, order)
            self.assertEqual(order["p2"], [base, "gyarados"], details)

    def test_unresolvable_species_poisons_the_order_instead_of_guessing(self):
        from fp.replay.damage_membership import PARTY_ORDER_UNRESOLVED

        faints = {"p1": 0, "p2": 0}
        order = {"p1": [], "p2": ["fezandipiti", "terapagos", "gyarados"]}
        self.apply(["|switch|p2a: X|Missingno, L80, M|100/100"], faints, order)
        self.assertIn(PARTY_ORDER_UNRESOLVED, order["p2"])
        # the slots themselves are untouched, but the order is now marked
        # unknown so Beat Up refuses rather than walking a stale permutation
        self.assertEqual(order["p2"][:3], ["fezandipiti", "terapagos", "gyarados"])

    def test_poisoned_order_stays_poisoned_and_stops_swapping(self):
        from fp.replay.damage_membership import PARTY_ORDER_UNRESOLVED

        faints = {"p1": 0, "p2": 0}
        order = {"p1": [], "p2": ["fezandipiti", "terapagos", "gyarados"]}
        self.apply(
            [
                "|switch|p2a: X|Missingno, L80, M|100/100",
                "|switch|p2a: Gyarados|Gyarados, L80, M|100/100",
            ],
            faints,
            order,
        )
        self.assertEqual(order["p2"].count(PARTY_ORDER_UNRESOLVED), 1)
        self.assertEqual(order["p2"][0], "fezandipiti")

    def test_unresolvable_species_makes_beat_up_refuse(self):
        from fp.battle import Pokemon
        from fp.replay.damage_membership import PsRefusal, _beatup_base_powers

        faints = {"p1": 0, "p2": 0}
        order = {"p1": [], "p2": ["fezandipiti", "clefable"]}
        self.apply(["|switch|p2a: X|Missingno, L80, M|100/100"], faints, order)
        user = Pokemon("fezandipiti", 79)
        with self.assertRaises(PsRefusal) as cm:
            _beatup_base_powers(user, {"fezandipiti": user}, order["p2"])
        self.assertEqual(cm.exception.reason, "ps_beatup_party_order_unresolved")


class TestDisabledMoveSurvivesWorldSampling(unittest.TestCase):
    """fp/search/helpers.py `populate_pkmn_from_set` rebuilt `pkmn.moves` from
    the sampled set, which dropped the per-move `disabled` flag the protocol
    parser set for Disable / Cursed Body (fp/battle_modifier.py:1810-1817), so
    every sampled world forgot the Disable."""

    class _Set:
        def __init__(self, moves):
            self.moves = moves

    class _PkmnSet:
        ability = "levitate"
        item = "leftovers"
        nature = "serious"
        evs = (85,) * 6
        ivs = (31,) * 6
        tera_type = None

    class _Predicted:
        def __init__(self, moves):
            self.pkmn_moveset = TestDisabledMoveSurvivesWorldSampling._Set(moves)
            self.pkmn_set = TestDisabledMoveSurvivesWorldSampling._PkmnSet()

    def _pkmn(self):
        from fp.battle import Pokemon

        pkmn = Pokemon("gengar", 81)
        for mv in ("shadowball", "sludgewave"):
            pkmn.add_move(mv)
        pkmn.moves[0].disabled = True
        pkmn.moves[0].current_pp = 7
        return pkmn

    def test_disabled_flag_survives_the_resample(self):
        from fp.search.helpers import populate_pkmn_from_set

        pkmn = self._pkmn()
        populate_pkmn_from_set(
            pkmn, self._Predicted(["shadowball", "sludgewave", "focusblast", "nastyplot"])
        )
        by_name = {m.name: m for m in pkmn.moves}
        self.assertTrue(by_name["shadowball"].disabled)
        self.assertEqual(7, by_name["shadowball"].current_pp)
        self.assertFalse(by_name["sludgewave"].disabled)
        self.assertFalse(by_name["focusblast"].disabled)

    def test_disabled_move_absent_from_the_sample_is_kept(self):
        from fp.search.helpers import populate_pkmn_from_set

        pkmn = self._pkmn()
        populate_pkmn_from_set(pkmn, self._Predicted(["sludgewave", "focusblast"]))
        by_name = {m.name: m for m in pkmn.moves}
        self.assertIn("shadowball", by_name)
        self.assertTrue(by_name["shadowball"].disabled)


# ---------------------------------------------------------------------------
# Final 21-finding audit: Future Sight immunity (positional), Pain Split /
# lethal-capped-chip discounts, Endeavor exact-HP boundary, mid-turn tera
# retention, stale forme-duplicate pruning
# ---------------------------------------------------------------------------
from fp.replay.checker import (
    _apply_slot_tera,
    _prune_stale_forme_duplicates,
)


class TestFutureSightImmunity(unittest.TestCase):
    """A `|-end|X|move: Future Sight` + `|-immune|X` is asserted POSITIONALLY:
    a landed delayed hit emits Damage-on-target immediately before the caster
    side's DecrementFutureSight; an immune pop emits the decrement alone.  The
    damaged-everywhere test is unusable here because the other side's regular
    move legitimately damaged the immune mon the same turn (synth08141 T10)."""

    def test_fs_immunity_respected_despite_regular_move_damage(self):
        # Ice Beam legitimately damages the immune side with roll/crit variance;
        # the FS pop itself is damage-free -> no finding.  The rule is an
        # EXISTENTIAL positional check: a branch whose last pre-decrement
        # (non-response) instruction is not a Damage on the immune side
        # confirms the modeled immunity.  In synth08141's real branch set the
        # defender's Scale Shot instructions sit between the Ice Beam damage
        # and the decrement -- exactly this shape.
        b = branches(
            [
                "Damage SideTwo: 134",
                "Damage SideOne: 27",
                "Boost SideTwo Defense: -1",
                "DecrementFutureSight SideOne",
            ],
            ["Damage SideTwo: 134", "DecrementFutureSight SideOne"],
        )
        obs = [ObservedEvent("immune", "opp", trigger_move="futuresight")]
        self.assertEqual(compare_turn(ctx(b, obs)), [])

    def test_fs_immunity_breach_fires_when_every_branch_lands_it(self):
        b = branches(
            ["Damage SideTwo: 60", "DecrementFutureSight SideOne"],
            ["Damage SideTwo: 44", "DecrementFutureSight SideOne"],
        )
        # here the Damage sits DIRECTLY before the decrement in every branch:
        # the engine landed the delayed hit everywhere -> HARD
        obs = [ObservedEvent("immune", "opp", trigger_move="futuresight")]
        fs = compare_turn(ctx(b, obs))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].category, "immunity")
        self.assertEqual(fs[0].severity, Severity.HARD)

    def test_fs_hit_with_stamina_response_between_still_counts_as_landed(self):
        # Air Balloon / Stamina responses sit between the FS damage and the
        # decrement (genx FS end-of-turn block); the walk-back skips them.
        b = branches(
            [
                "Damage SideTwo: 60",
                "Boost SideTwo Defense: 1",
                "DecrementFutureSight SideOne",
            ],
        )
        obs = [ObservedEvent("immune", "opp", trigger_move="futuresight")]
        fs = compare_turn(ctx(b, obs))
        self.assertEqual(len(fs), 1)

    def test_fs_immunity_without_any_decrement_still_fires(self):
        # no pending FS modeled at all: the immunity cannot be confirmed
        b = branches(["Damage SideTwo: 134"])
        obs = [ObservedEvent("immune", "opp", trigger_move="futuresight")]
        self.assertEqual(len(compare_turn(ctx(b, obs))), 1)


class TestFutureSightImmuneTrigger(unittest.TestCase):
    def test_end_line_directly_before_immune_yields_futuresight_sentinel(self):
        block = """
|move|p1a: Cryogonal|Ice Beam|p2a: Overqwil
|-damage|p2a: Overqwil|53/100
|-end|p2a: Overqwil|move: Future Sight
|-immune|p2a: Overqwil
"""
        events = _obs(block, "p1", None)
        immunes = [e for e in events if e.kind == "immune"]
        self.assertEqual(len(immunes), 1)
        self.assertEqual(immunes[0].trigger_move, "futuresight")

    def test_plain_move_immune_still_attributed_to_the_move(self):
        block = """
|move|p1a: Luvdisc|Endeavor|p2a: Armarouge
|-immune|p2a: Armarouge
"""
        events = _obs(block, "p1", None)
        immunes = [e for e in events if e.kind == "immune"]
        self.assertEqual(immunes[0].trigger_move, "endeavor")


class TestPainSplitImmunityDiscount(unittest.TestCase):
    def test_pain_split_pair_is_discounted(self):
        # tera-Dark Dusknoir immune to Psyshock; its own Pain Split emits the
        # +d/-d pair, d varying with the other side's roll (synth36693 T23)
        b = branches(
            ["Damage SideOne: 34", "Damage SideTwo: -34"],
            ["Damage SideTwo: 19", "Damage SideOne: 44", "Damage SideTwo: -43"],
        )
        obs = [ObservedEvent("immune", "user")]
        c = TurnContext(turn=1, branches=b, observed=obs, user_move="painsplit")
        self.assertEqual(compare_turn(c), [])

    def test_unpaired_damage_still_fires_for_a_painsplit_user(self):
        # no equal-and-opposite partner: a real breach must still fire
        b = branches(
            ["Damage SideOne: 34"],
            ["Damage SideOne: 44"],
        )
        obs = [ObservedEvent("immune", "user")]
        c = TurnContext(turn=1, branches=b, observed=obs, user_move="painsplit")
        self.assertEqual(len(compare_turn(c)), 1)


class TestLethalCappedChipDiscount(unittest.TestCase):
    def test_lethal_capped_trap_chip_is_discounted(self):
        # Araquanid at 5 hp: drain heals to 23/25, the Whirlpool chip is capped
        # to remaining hp (23 vs 25) and varies with the drain roll
        # (synth43826 T6); nominal chip is maxhp/8 = 30
        b = branches(
            ["Damage SideTwo: 36", "Heal SideOne: 18", "Damage SideOne: 23"],
            ["Damage SideTwo: 40", "Heal SideOne: 20", "Damage SideOne: 25"],
        )
        obs = [ObservedEvent("immune", "user")]
        c = TurnContext(
            turn=1,
            branches=b,
            observed=obs,
            user_move="leechlife",
            side_maxhp={"s1": 246, "s2": 240},
            side_hp={"s1": 5, "s2": 240},
        )
        self.assertEqual(compare_turn(c), [])

    def test_move_scale_damage_is_not_mistaken_for_a_capped_chip(self):
        # same shape but the trailing damage is far above maxhp/8: a real hit
        b = branches(
            ["Damage SideOne: 90"],
            ["Damage SideOne: 110"],
        )
        obs = [ObservedEvent("immune", "user")]
        c = TurnContext(
            turn=1,
            branches=b,
            observed=obs,
            side_maxhp={"s1": 246, "s2": 240},
            side_hp={"s1": 246, "s2": 240},
        )
        self.assertEqual(len(compare_turn(c)), 1)


class TestEndeavorBoundary(unittest.TestCase):
    """APPROXIMATIONS U3 is DELETED and so is its demotion arm.  A landed
    Endeavor now certifies `target.hp == attacker.hp` exactly in the
    reconstruction (fp/hp_certificate.py), so an Endeavor immunity is decided
    like any other immunity -- there is no <= 2 HP tolerance band any more."""

    def test_endeavor_within_two_hp_is_no_longer_demoted(self):
        # the shape that used to be U3's member (synth11861 T12: modelled
        # Endeavor deals 1 in one branch).  With exact HP a branch that damages
        # a mon the protocol showed immune is a real breach, at any magnitude.
        b = branches(
            ["Damage SideOne: 112", "Damage SideTwo: 1"],
            ["Damage SideOne: 169", "Damage SideTwo: 58"],
        )
        obs = [ObservedEvent("immune", "opp", trigger_move="endeavor")]
        fs = compare_turn(ctx(b, obs))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].severity, Severity.HARD)
        self.assertNotIn("U3", fs[0].message)

    def test_endeavor_immunity_modeled_in_some_branch_is_clean(self):
        # the certificate's purpose: with exact HP the engine reproduces the
        # immunity in a branch, and nothing is reported at all
        b = branches(
            ["Damage SideOne: 112"],
            ["Damage SideOne: 169"],
        )
        obs = [ObservedEvent("immune", "opp", trigger_move="endeavor")]
        self.assertEqual(compare_turn(ctx(b, obs)), [])

    def test_endeavor_far_from_boundary_stays_hard(self):
        b = branches(
            ["Damage SideTwo: 80"],
            ["Damage SideTwo: 95"],
        )
        obs = [ObservedEvent("immune", "opp", trigger_move="endeavor")]
        fs = compare_turn(ctx(b, obs))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].severity, Severity.HARD)


class TestMidTurnTeraRetention(unittest.TestCase):
    """A pivot splits the turn into several |t:| chunks with their own requests;
    a snapshot armed AFTER the |-terastallize| of the SAME turn carries live
    tracking that is already correctly tera'd and must NOT be cleared
    (synth49327 T7: un-tera'ing Sneasler restored its Poison typing and made
    Synchronize's reflected psn immune in every branch)."""

    def _reveals(self, during_turn):
        return {
            "occupancies": [
                {
                    "pid": "p2",
                    "slot": "p2a",
                    "species": "sneasler",
                    "start_turn": 6,
                    "end_turn": 99,
                    "entry_tera": None,
                    "tera_during": "dark",
                    "tera_during_turn": during_turn,
                }
            ]
        }

    def test_same_turn_tera_kept_when_live_tracking_saw_it(self):
        mon = _Pkmn("sneasler")
        mon.terastallized = True  # live tracking processed the mid-turn tera
        battler = _Battler(active=mon)
        _apply_slot_tera(battler, "p2", self._reveals(7), 7)
        self.assertTrue(mon.terastallized)
        self.assertEqual(mon.tera_type, "dark")

    def test_same_turn_tera_cleared_for_a_turn_start_snapshot(self):
        mon = _Pkmn("sneasler")  # live tracking has not seen the tera yet
        battler = _Battler(active=mon)
        _apply_slot_tera(battler, "p2", self._reveals(7), 7)
        self.assertFalse(mon.terastallized)
        self.assertEqual(mon.tera_type, "dark")

    def test_earlier_turn_tera_still_applies(self):
        mon = _Pkmn("sneasler")
        battler = _Battler(active=mon)
        _apply_slot_tera(battler, "p2", self._reveals(6), 7)
        self.assertTrue(mon.terastallized)

    def test_later_turn_pre_state_still_untera_d(self):
        mon = _Pkmn("sneasler")
        mon.terastallized = True  # wrongly set for an EARLIER turn's pre-state
        battler = _Battler(active=mon)
        _apply_slot_tera(battler, "p2", self._reveals(8), 7)
        self.assertFalse(mon.terastallized)


class TestPruneStaleFormeDuplicates(unittest.TestCase):
    """The request's party list is the server's authoritative roster; a mon
    tracked under two forme names leaves a stale phantom row that wrongly
    counts as an alive reserve (synth26245: `terapagosterastal` 273/273 next to
    the real fainted `terapagos`, arming Parting Shot's self-switch on a
    benchless side)."""

    @staticmethod
    def _request(names):
        return {
            "side": {
                "pokemon": [
                    {"details": n, "ident": "p1: " + n, "active": False}
                    for n in names
                ]
            }
        }

    def test_stale_forme_duplicate_is_dropped(self):
        active = _Pkmn("pecharunt")
        stale = _Pkmn("terapagosterastal", hp=273)
        real = _Pkmn("terapagos", hp=0)
        others = [_Pkmn("oranguru", hp=0), _Pkmn("phione", hp=0), _Pkmn("carbink", hp=0)]
        battler = _Battler(active=active, reserve=[stale, real] + others)
        req = self._request(
            ["Pecharunt", "Terapagos, L77, M", "Oranguru", "Phione", "Carbink"]
        )
        _prune_stale_forme_duplicates(battler, req)
        self.assertNotIn(stale, battler.reserve)
        self.assertIn(real, battler.reserve)
        self.assertEqual(len(battler.reserve), 4)

    def test_unlisted_mon_with_uncovered_family_is_kept(self):
        # a reserve row the request does not list AND whose family the request
        # does not cover is NOT ours to delete
        active = _Pkmn("pecharunt")
        unknown = _Pkmn("cramorantgulping", hp=100)
        battler = _Battler(active=active, reserve=[unknown])
        req = self._request(["Pecharunt", "Terapagos, L77, M"])
        _prune_stale_forme_duplicates(battler, req)
        self.assertIn(unknown, battler.reserve)

    def test_listed_forme_rows_are_never_touched(self):
        active = _Pkmn("pecharunt")
        listed = _Pkmn("terapagosterastal", hp=273)
        battler = _Battler(active=active, reserve=[listed])
        req = self._request(["Pecharunt", "Terapagos-Terastal, L77, M"])
        _prune_stale_forme_duplicates(battler, req)
        self.assertIn(listed, battler.reserve)


class TestDamageProvenanceParsing(unittest.TestCase):
    """Engines with DamageSource print `Damage <Side>: <amt> [<source>]`; the
    parser must expose the tag and old untagged reprs must keep source=None."""

    def test_tagged_damage(self):
        i = parse_instruction("Damage SideOne: 66 [move]")
        self.assertEqual(i.kind, "Damage")
        self.assertEqual(i.side, "s1")
        self.assertEqual(i.amount(), 66)
        self.assertEqual(i.source, "move")

    def test_tagged_negative_painsplit(self):
        i = parse_instruction("Damage SideTwo: -53 [painsplit]")
        self.assertEqual(i.amount(), -53)
        self.assertEqual(i.source, "painsplit")

    def test_tagged_damage_substitute(self):
        i = parse_instruction("DamageSubstitute SideTwo: 25 [futuresight]")
        self.assertEqual(i.kind, "DamageSubstitute")
        self.assertEqual(i.source, "futuresight")

    def test_untagged_damage_has_no_source(self):
        i = parse_instruction("Damage SideOne: 66")
        self.assertIsNone(i.source)

    def test_non_damage_kinds_never_parse_a_source(self):
        # a bracketed suffix on another kind must stay in the payload
        i = parse_instruction("ChangeStatus SideOne-P0: NONE -> PARALYZE")
        self.assertIsNone(i.source)


class TestImmunityProvenanceExact(unittest.TestCase):
    """With full damage provenance the immunity assertion is decidable exactly
    (retires APPROXIMATIONS.md U1): only `[move]` damage on the immune side can
    contradict a regular-move immunity, only `[futuresight]` the delayed hit."""

    def test_u1_synth45908_painsplit_confounder_clean_when_tagged(self):
        # synth45908 T22: Rotom-Wash (Levitate) vs Earthquake; Rotom's own Pain
        # Split adjusts hp by 54/-53 (NOT equal-and-opposite, so the legacy
        # pairing discount cannot see it).  Tagged, no [move] damage lands on
        # s1 in any branch -> immunity modeled, no finding.
        b = branches(
            [
                "ChangeVolatileStatusDuration SideTwo CONFUSION: 1",
                "Damage SideTwo: 90 [confusion]",
                "SetLastUsedMove SideOne: Switch(P0) -> Move(M3)",
                "Damage SideOne: 54 [painsplit]",
                "Damage SideTwo: -53 [painsplit]",
                "DecrementWeatherTurnsRemaining",
                "Heal SideOne: 13",
            ],
            [
                "ChangeVolatileStatusDuration SideTwo CONFUSION: 1",
                "SetLastUsedMove SideTwo: Move(M3) -> Move(M1)",
                "SetLastUsedMove SideOne: Switch(P0) -> Move(M3)",
                "Damage SideOne: 9 [painsplit]",
                "Damage SideTwo: -8 [painsplit]",
                "DecrementWeatherTurnsRemaining",
                "Heal SideOne: 9",
            ],
        )
        obs = [ObservedEvent("immune", "user", trigger_move="earthquake")]
        c = TurnContext(
            turn=22, branches=b, observed=obs, user_move="painsplit", opp_move="earthquake"
        )
        self.assertEqual(compare_turn(c), [])

    def test_u1_synth45908_untagged_still_takes_legacy_path_and_fires(self):
        # the SAME branches without tags (pre-provenance wheel): 54 vs -53 is
        # off by one, the legacy pairing discount misses it, and the finding
        # fires -- this is exactly why U1 existed.  Pins the legacy fallback.
        b = branches(
            [
                "ChangeVolatileStatusDuration SideTwo CONFUSION: 1",
                "Damage SideTwo: 90",
                "SetLastUsedMove SideOne: Switch(P0) -> Move(M3)",
                "Damage SideOne: 54",
                "Damage SideTwo: -53",
                "DecrementWeatherTurnsRemaining",
                "Heal SideOne: 13",
            ],
            [
                "ChangeVolatileStatusDuration SideTwo CONFUSION: 1",
                "SetLastUsedMove SideTwo: Move(M3) -> Move(M1)",
                "SetLastUsedMove SideOne: Switch(P0) -> Move(M3)",
                "Damage SideOne: 9",
                "Damage SideTwo: -8",
                "DecrementWeatherTurnsRemaining",
                "Heal SideOne: 9",
            ],
        )
        obs = [ObservedEvent("immune", "user", trigger_move="earthquake")]
        c = TurnContext(
            turn=22, branches=b, observed=obs, user_move="painsplit", opp_move="earthquake"
        )
        fs = compare_turn(c)
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].category, "immunity")

    def test_tagged_true_breach_stays_hard(self):
        # control: every branch lands [move] damage on the immune side -> HARD
        b = branches(
            ["Damage SideOne: 195 [move]"],
            ["Damage SideOne: 233 [move]"],
        )
        obs = [ObservedEvent("immune", "user", trigger_move="earthquake")]
        fs = compare_turn(ctx(b, obs))
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].category, "immunity")
        self.assertIs(fs[0].severity, Severity.HARD)

    def test_tagged_chip_only_branches_are_clean_without_heuristics(self):
        # varying chip damage that used to need the magnitude/position
        # heuristics is now excluded by its tag alone
        b = branches(
            ["Damage SideOne: 10 [status]"],
            ["Damage SideOne: 12 [residual]"],
        )
        obs = [ObservedEvent("immune", "user", trigger_move="tackle")]
        self.assertEqual(compare_turn(ctx(b, obs)), [])

    def test_tagged_endeavor_boundary_is_hard_now_that_hp_is_certified(self):
        # the provenance path carried U3's demotion too, and loses it for the
        # same reason: a landed Endeavor certifies `target.hp == attacker.hp`
        # (fp/hp_certificate.py), so 1-2 HP of modelled damage against an
        # observed `-immune` is a breach, not reconstruction noise
        b = branches(
            ["Damage SideTwo: 1 [move]"],
            ["Damage SideTwo: 2 [move]"],
        )
        obs = [ObservedEvent("immune", "opp", trigger_move="endeavor")]
        fs = compare_turn(ctx(b, obs))
        self.assertEqual(len(fs), 1)
        self.assertIs(fs[0].severity, Severity.HARD)
        self.assertNotIn("U3", fs[0].message)

    def test_u1_synth31061_fs_recoil_confounder_clean_when_tagged(self):
        # synth31061 T22: Mew Psyshock vs Mandibuzz (Dark) with Slowbro's
        # Future Sight popping the same turn.  The engine emits Brave Bird's
        # damage/recoil plus the FS decrement WITHOUT any [futuresight] damage
        # -- immunity modeled; the legacy positional arm was confounded by the
        # recoil sitting directly before the decrement.
        b = branches(
            [
                "ChangeActiveMoveActions SideOne-P0: 1",
                "SetLastUsedMove SideOne: Switch(P0) -> Move(M3)",
                "ChangeActiveMoveActions SideTwo-P0: 1",
                "Damage SideOne: 91 [move]",
                "Damage SideTwo: 30 [recoil]",
                "DecrementFutureSight SideOne",
            ],
            [
                "ChangeActiveMoveActions SideOne-P0: 1",
                "SetLastUsedMove SideOne: Switch(P0) -> Move(M3)",
                "ChangeActiveMoveActions SideTwo-P0: 1",
                "Damage SideOne: 138 [move]",
                "Damage SideTwo: 45 [recoil]",
                "DecrementFutureSight SideOne",
            ],
        )
        obs = [ObservedEvent("immune", "opp", trigger_move="futuresight")]
        c = TurnContext(
            turn=22, branches=b, observed=obs, user_move="psyshock", opp_move="bravebird"
        )
        self.assertEqual(compare_turn(c), [])

    def test_u1_synth31061_untagged_still_takes_legacy_path_and_fires(self):
        # the same branches untagged reproduce the U1 false positive (recoil
        # directly before the decrement) -- pins that the fallback is intact
        b = branches(
            [
                "ChangeActiveMoveActions SideOne-P0: 1",
                "SetLastUsedMove SideOne: Switch(P0) -> Move(M3)",
                "ChangeActiveMoveActions SideTwo-P0: 1",
                "Damage SideOne: 91",
                "Damage SideTwo: 30",
                "DecrementFutureSight SideOne",
            ],
            [
                "ChangeActiveMoveActions SideOne-P0: 1",
                "SetLastUsedMove SideOne: Switch(P0) -> Move(M3)",
                "ChangeActiveMoveActions SideTwo-P0: 1",
                "Damage SideOne: 138",
                "Damage SideTwo: 45",
                "DecrementFutureSight SideOne",
            ],
        )
        obs = [ObservedEvent("immune", "opp", trigger_move="futuresight")]
        c = TurnContext(
            turn=22, branches=b, observed=obs, user_move="psyshock", opp_move="bravebird"
        )
        fs = compare_turn(c)
        self.assertEqual(len(fs), 1)
        self.assertEqual(fs[0].category, "immunity")
        self.assertIn("Future Sight", fs[0].message)

    def test_tagged_fs_landed_hit_still_fires(self):
        # control: a [futuresight] Damage on the immune side in every branch
        # (with the decrement present) is a genuine landed delayed hit -> HARD
        b = branches(
            [
                "Damage SideTwo: 60 [futuresight]",
                "DecrementFutureSight SideOne",
            ],
            [
                "Damage SideTwo: 71 [futuresight]",
                "DecrementFutureSight SideOne",
            ],
        )
        obs = [ObservedEvent("immune", "opp", trigger_move="futuresight")]
        fs = compare_turn(ctx(b, obs))
        self.assertEqual(len(fs), 1)
        self.assertIs(fs[0].severity, Severity.HARD)

    def test_tagged_fs_not_modeled_at_all_still_fires(self):
        # no DecrementFutureSight anywhere: the immunity cannot be confirmed
        b = branches(["Damage SideTwo: 60 [move]"])
        obs = [ObservedEvent("immune", "opp", trigger_move="futuresight")]
        fs = compare_turn(ctx(b, obs))
        self.assertEqual(len(fs), 1)

    def test_partial_tags_fall_back_to_legacy(self):
        # a branch set where some Damage is untagged is NOT trusted as
        # provenance (cannot happen from a single wheel; defensive)
        b = branches(
            ["Damage SideTwo: 100 [move]", "Damage SideOne: 10"],
            ["Damage SideTwo: 90 [move]", "Damage SideOne: 12"],
        )
        obs = [ObservedEvent("immune", "user", trigger_move="tackle")]
        fs = compare_turn(ctx(b, obs))
        # legacy chip-scale downgrade applies (12 < 0.25*90) -> SOFT, exactly
        # the pre-provenance behavior
        self.assertEqual(len(fs), 1)
        self.assertIs(fs[0].severity, Severity.SOFT)


# ---------------------------------------------------------------------------
# PHASE 2 -- EXISTENTIAL MEMBERSHIP ASSERT over an unnamable side's legal set
# ---------------------------------------------------------------------------
from fp.replay import checker as _checker
from fp.replay.checker import (
    _legal_action_strings,
    _membership_bound,
    _membership_class,
    _membership_evaluate,
)


class _MembSide:
    def __init__(self, force_switch=False):
        self.force_switch = force_switch


class _MembEngineState:
    def __init__(self, force_switch=False):
        self.side_one = _MembSide(force_switch)
        self.side_two = _MembSide(force_switch)


def _f(sev, category="status", message="m"):
    return Finding(turn=1, severity=sev, category=category, message=message,
                   observed="obs")


class _MembershipHarness:
    """Drives `_membership_evaluate` with a scripted `_evaluate_turn`.

    `script` maps the opponent's candidate string to the finding list that
    candidate's engine call produces, so a test states directly which candidates
    reproduce the observation and which do not."""

    def __init__(self, script, u_named="tackle", legal=None, branches=None):
        # a non-empty instruction list: an engine call that produces NOTHING is
        # degenerate and is refused, which is its own test below
        self.branches = [["some-instruction"]] if branches is None else branches
        self.script = script
        self.u_named = u_named
        self.legal = legal if legal is not None else sorted(script)
        self.calls = []

    def run(self, u_spec=None, o_spec="legal", stats=None, observed=("ev",)):
        stats = {} if stats is None else stats

        def fake_eval(snap, turn, u, o, ua, oa, obs, bl, pid, st):
            self.calls.append((u, o))
            return (list(self.script[o]), self.branches, object())

        with mock.patch.object(_checker, "battle_to_poke_engine_state",
                               lambda snap: _MembEngineState()), \
             mock.patch.object(_checker, "_evaluate_turn", fake_eval), \
             mock.patch.object(_checker, "_legal_action_strings",
                               lambda st, side, tera: list(self.legal)):
            out = _membership_evaluate(
                object(), 1, self.u_named, None, None, None, list(observed),
                [], "p1", stats, u_spec, o_spec, "testclass",
            )
        return out, stats


class TestMembershipClassification(unittest.TestCase):
    def test_class_boundaries(self):
        self.assertEqual(_membership_class(0, 9), "fail")
        self.assertEqual(_membership_class(1, 1), "point")
        self.assertEqual(_membership_class(1, 9), "full")
        self.assertEqual(_membership_class(4, 9), "partial")
        self.assertEqual(_membership_class(9, 9), "vacuous")

    def test_bound_is_counted_and_not_a_prefix(self):
        u = ["u%d" % i for i in range(20)]
        o = ["o%d" % i for i in range(20)]
        nu, no, bounded = _membership_bound(u, o, 40)
        self.assertTrue(bounded)
        self.assertLessEqual(len(nu) * len(no), 40)
        # the subsampled axis must span its range, not take a prefix
        kept = nu if len(nu) < 20 else no
        self.assertIn(kept[-1], (u[-1], o[-1]))

    def test_unbounded_product_is_not_flagged(self):
        nu, no, bounded = _membership_bound(["a", "b"], ["c", "d"], 40)
        self.assertFalse(bounded)


class TestMembershipAssert(unittest.TestCase):
    def test_sharp_turn_exactly_one_candidate_reproduces(self):
        """FULL discrimination: 1 of 3 legal actions reproduces the observation.
        As strong as a point assert, and the one that reproduces is the pair the
        turn is reported under."""
        h = _MembershipHarness({
            "aquajet": [],
            "surf": [_f(Severity.HARD)],
            "pikachu": [_f(Severity.HARD)],
        })
        (ev, cu, co), stats = h.run()
        self.assertEqual(stats["membership_class_full"], 1)
        self.assertEqual(stats["membership_candidates_tried"], 3)
        self.assertEqual(co, "aquajet")
        self.assertEqual(cu, "tackle")
        self.assertEqual(ev[0], [])  # the reproducing candidate is clean

    def test_vacuous_turn_is_reported_as_vacuous(self):
        """Every candidate reproduces -> the check decided NOTHING this turn and
        must be counted apart from real coverage."""
        h = _MembershipHarness({"aquajet": [], "surf": [], "pikachu": []})
        (ev, _cu, _co), stats = h.run()
        self.assertEqual(stats["membership_class_vacuous"], 1)
        self.assertNotIn("membership_class_full", stats)
        self.assertEqual(ev[0], [])

    def test_no_candidate_reproduces_raises_hard(self):
        """The whole point of the existential assert: when the model cannot
        explain the observation under ANY legal action, that is a HARD finding,
        not a refusal."""
        _checker.MEMBERSHIP_FAIL_SAMPLES.clear()
        h = _MembershipHarness({
            "aquajet": [_f(Severity.HARD, "boost")],
            "surf": [_f(Severity.HARD, "boost")],
            "pikachu": [_f(Severity.HARD, "status")],
        })
        (ev, _cu, _co), stats = h.run()
        self.assertEqual(stats["membership_class_fail"], 1)
        self.assertEqual(len(ev[0]), 1)
        self.assertIs(ev[0][0].severity, Severity.HARD)
        self.assertIn("membership 0/3 legal actions reproduce", ev[0][0].message)
        # a fail that is only COUNTED is unadjudicable: it must name itself
        self.assertEqual(len(_checker.MEMBERSHIP_FAIL_SAMPLES), 1)
        _checker.MEMBERSHIP_FAIL_SAMPLES.clear()

    def test_finding_reported_only_if_every_reproducing_candidate_produces_it(self):
        """Same rule as the B1/B3 band re-evaluation (HANDOFF rule 10): a SOFT
        finding absent under some candidate the protocol admits is not decidable,
        so it is not reported."""
        common = _f(Severity.SOFT, "volatile", "shared")
        h = _MembershipHarness({
            "aquajet": [_f(Severity.SOFT, "volatile", "shared"),
                        _f(Severity.SOFT, "heal", "only-here")],
            "surf": [_f(Severity.SOFT, "volatile", "shared")],
        })
        (ev, _cu, _co), stats = h.run()
        self.assertEqual(stats["membership_class_vacuous"], 1)
        cats = [f.category for f in ev[0]]
        self.assertEqual(cats, ["volatile"])
        self.assertIn("[membership 2/2 vacuous]", ev[0][0].message)
        self.assertEqual(common.category, "volatile")  # harness sanity

    def test_single_member_set_is_a_point_assert_not_a_vacuum(self):
        """The split-half case: the other side's action is not unknown, it was
        already observed in the previous block, so its candidate set is the one
        engine option `none`.  Counting that as `vacuous` would understate the
        assert -- it is exact."""
        h = _MembershipHarness({"none": []}, legal=["none"])
        (_ev, _cu, co), stats = h.run(o_spec="none")
        self.assertEqual(stats["membership_class_point"], 1)
        self.assertNotIn("membership_class_vacuous", stats)
        self.assertEqual(co, "none")

    def test_zero_observed_events_is_counted_separately(self):
        h = _MembershipHarness({"aquajet": [], "surf": []})
        _out, stats = h.run(observed=())
        self.assertEqual(stats["membership_zero_observed"], 1)

    def test_all_candidates_unbuildable_refuses_named(self):
        stats = {}

        def fake_eval(*a, **k):
            return None

        with mock.patch.object(_checker, "battle_to_poke_engine_state",
                               lambda snap: _MembEngineState()), \
             mock.patch.object(_checker, "_evaluate_turn", fake_eval), \
             mock.patch.object(_checker, "_legal_action_strings",
                               lambda st, side, tera: ["a", "b"]):
            out = _membership_evaluate(
                object(), 1, "tackle", None, None, None, ["ev"], [], "p1",
                stats, None, "legal", "testclass",
            )
        self.assertIsNone(out)
        self.assertEqual(stats["skipsub_membership_all_unbuildable"], 1)
        self.assertNotIn("membership_turns", stats)


class _LegalMove:
    def __init__(self, mid):
        self.id = mid


class _LegalPkmn:
    def __init__(self, pid, hp, moves=()):
        self.id = pid
        self.hp = hp
        self.moves = [_LegalMove(m) for m in moves]


class _LegalSide:
    def __init__(self, pokemon, active_index=0):
        self.pokemon = pokemon
        self.active_index = "P%d" % active_index


class _LegalState:
    def __init__(self, side_one):
        self.side_one = side_one
        self.side_two = side_one


class TestLegalActionSet(unittest.TestCase):
    def _state(self):
        return _LegalState(_LegalSide([
            _LegalPkmn("PIKACHU", 100, ["thunderbolt", "voltswitch"]),
            _LegalPkmn("LANTURN", 50, ["surf"]),
            _LegalPkmn("ONIX", 0, ["tackle"]),      # fainted: not switchable
        ]))

    def test_moves_plus_alive_switches_only(self):
        out = _legal_action_strings(self._state(), "s1", False)
        self.assertEqual(out, ["lanturn", "thunderbolt", "voltswitch"])

    def test_tera_suffixes_moves_and_drops_switches(self):
        # a side that terastallized cannot also have switched, and the tera is
        # directly observed -- narrowing here is on evidence, not assumption
        out = _legal_action_strings(self._state(), "s1", True)
        self.assertEqual(out, ["thunderbolt-tera", "voltswitch-tera"])

    def test_empty_moveset_falls_back_to_struggle(self):
        st = _LegalState(_LegalSide([_LegalPkmn("PIKACHU", 100, [])]))
        self.assertEqual(_legal_action_strings(st, "s1", False), ["struggle"])


class TestSideThatDidActNeverEntersMembership(unittest.TestCase):
    """A side which DID act is named by `_extract_side_action` and therefore
    never reaches the membership path -- its argument is the move it used, and
    quantifying over its legal set would replace a point assert with a weaker
    one."""

    def test_sleep_talk_while_asleep_is_a_named_action(self):
        block = [
            "|",
            "|t:|1785088142",
            "|cant|p2a: Snorlax|slp",                 # the FOE was prevented
            "|move|p1a: Komala|Sleep Talk|p1a: Komala",
            "|move|p1a: Komala|Body Slam|p2a: Snorlax|[from]move: Sleep Talk",
            "|-damage|p2a: Snorlax|70/100",
            "|upkeep",
        ]
        self.assertEqual(
            _extract_side_action(block, "p1a"), ("move", "sleeptalk")
        )
        # ...and the side that was prevented is the one with no namable action
        self.assertIsNone(_extract_side_action(block, "p2a"))

    def test_a_named_move_side_keeps_a_single_candidate(self):
        h = _MembershipHarness({"aquajet": [], "surf": [_f(Severity.HARD)]})
        (_ev, cu, _co), _stats = h.run()
        # exactly two engine calls: the named user move x each opp candidate
        self.assertEqual([c[0] for c in h.calls], ["tackle", "tackle"])
        self.assertEqual(cu, "tackle")


_CORPUS = "/Users/sallyliu/pokemon-ai/synthetic-corpus"
_E2E_LOG = os.path.join(
    _CORPUS, "battle-gen9randombattle-synth00001_synthopp.log"
)


@unittest.skipUnless(os.path.exists(_E2E_LOG), "synthetic corpus not present")
class TestMembershipEndToEnd(unittest.TestCase):
    """End-to-end on a real corpus game: the membership assert must actually
    remove the blanket refusal, and the negative control must actually restore
    it.  A control that cannot be shown to flip has not been shown to gate
    anything (HANDOFF rules 13/14)."""

    def _run(self, control):
        # DEFAULT OFF as of 2026-07-28 (see `_membership_off`): the "on" arm must
        # opt in explicitly, and the control arm must still win over that opt-in.
        env = {"FP_MEMBERSHIP_REPLAY": "1"}
        if control:
            env["FP_CONTROL_NO_MEMBERSHIP_REPLAY"] = "1"
        with mock.patch.dict(os.environ, env, clear=False):
            if not control:
                os.environ.pop("FP_CONTROL_NO_MEMBERSHIP_REPLAY", None)
            _findings, stats = _checker.check_log(
                _E2E_LOG, teams_dir=_CORPUS, damage_tolerance=0
            )
        return stats

    def test_control_flips_and_denominator_is_conserved(self):
        off = self._run(control=True)
        on = self._run(control=False)
        total_off = off["turns_checked"] + off["turns_skipped"]
        total_on = on["turns_checked"] + on["turns_skipped"]
        # nothing may leave the denominator
        self.assertEqual(total_off, total_on)
        # the control really restores the old blanket refusal
        self.assertGreater(off["turns_skipped"], 0)
        self.assertEqual(off.get("membership_turns", 0), 0)
        self.assertEqual(
            off["skipped_unnamable_action"], off["turns_skipped"]
        )
        # ...and with it off, those very turns are asserted instead
        self.assertGreater(on["turns_checked"], off["turns_checked"])
        self.assertEqual(
            on["membership_turns"],
            on["turns_checked"] - off["turns_checked"],
        )

    def test_strength_is_recorded_for_every_membership_turn(self):
        on = self._run(control=False)
        classes = sum(
            on.get("membership_class_" + c, 0)
            for c in ("point", "full", "partial", "vacuous", "fail")
        )
        # every membership turn carries a strength class -- a turn asserted
        # without a recorded discriminating power would hide inside coverage
        self.assertEqual(classes, on["membership_turns"])
        self.assertGreater(on.get("membership_class_vacuous", 0), 0)


class TestDegenerateCallIsRefused(unittest.TestCase):
    """A candidate set whose every engine call produces no instruction at all
    cannot reproduce ANY observed event, so a `fail` there would measure the
    MODEL, not the engine.  Proven live: `generate_instructions(state, "none",
    "rest")` on a state with `force_switch` returns one branch with an empty
    instruction list (genx/generate_instructions.rs short-circuits the turn)."""

    def test_empty_instruction_lists_with_observations_refuse(self):
        h = _MembershipHarness({"none": [_f(Severity.HARD)]}, legal=["none"],
                               branches=[[]])
        out, stats = h.run(o_spec="none")
        self.assertEqual(out, "degenerate")
        self.assertNotIn("membership_turns", stats)
        self.assertEqual(
            stats["skipsub_membership_degenerate_testclass"], 1
        )

    def test_empty_instruction_lists_with_no_observation_is_vacuous_not_point(self):
        h = _MembershipHarness({"none": []}, legal=["none"], branches=[[]])
        out, stats = h.run(o_spec="none", observed=())
        self.assertNotEqual(out, "degenerate")
        self.assertEqual(stats["membership_class_vacuous"], 1)
        self.assertNotIn("membership_class_point", stats)


class TestNonAuthoritativeLegalSetCannotRaiseHard(unittest.TestCase):
    """An opponent moveset on a log with no full-knowledge sidecar is only what
    the protocol has revealed, i.e. an UNDER-approximation -- and an
    under-approximated set is the one thing that can manufacture a hard finding
    (battle-...-2651908107 T14: Toucannon armed Beak Blast and fainted before its
    |move| line, so its real choice was in no reconstructed slot)."""

    def test_fail_over_a_non_authoritative_set_is_soft_and_named(self):
        script = {"a": [_f(Severity.HARD, "status")],
                  "b": [_f(Severity.HARD, "status")]}
        h = _MembershipHarness(script)
        stats = {}
        with mock.patch.object(_checker, "battle_to_poke_engine_state",
                               lambda snap: _MembEngineState()), \
             mock.patch.object(_checker, "_evaluate_turn",
                               lambda *a, **k: (list(script[a[3]]),
                                                [["i"]], object())), \
             mock.patch.object(_checker, "_legal_action_strings",
                               lambda st, side, tera: ["a", "b"]):
            ev, _cu, _co = _membership_evaluate(
                object(), 1, "tackle", None, None, None, ["ev"], [], "p1",
                stats, None, "legal", "testclass", authoritative=False,
            )
        self.assertEqual(stats["membership_class_fail"], 1)
        self.assertEqual(stats["membership_fail_nonauthoritative_set"], 1)
        self.assertIs(ev[0][0].severity, Severity.SOFT)
        self.assertIn("legal-set NOT authoritative", ev[0][0].message)

    def test_authoritative_set_keeps_the_finding_hard(self):
        script = {"a": [_f(Severity.HARD, "status")],
                  "b": [_f(Severity.HARD, "status")]}
        stats = {}
        with mock.patch.object(_checker, "battle_to_poke_engine_state",
                               lambda snap: _MembEngineState()), \
             mock.patch.object(_checker, "_evaluate_turn",
                               lambda *a, **k: (list(script[a[3]]),
                                                [["i"]], object())), \
             mock.patch.object(_checker, "_legal_action_strings",
                               lambda st, side, tera: ["a", "b"]):
            ev, _cu, _co = _membership_evaluate(
                object(), 1, "tackle", None, None, None, ["ev"], [], "p1",
                stats, None, "legal", "testclass", authoritative=True,
            )
        self.assertIs(ev[0][0].severity, Severity.HARD)
        self.assertNotIn("membership_fail_nonauthoritative_set", stats)
