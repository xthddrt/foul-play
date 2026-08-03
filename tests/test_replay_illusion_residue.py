"""Regression tests for the checker-side Illusion / slotless-ident residue.

Each test names the PS source it mirrors and, where one exists, the synthetic
corpus game whose finding it locks down.
"""

import unittest

import constants
from constants import BattleType
from fp import hp_certificate
from fp.battle import Battle, Pokemon
from fp.battle_modifier import (
    _find_bench_pokemon_by_protocol_name,
    _ident_is_slotless,
    curestatus,
    heal_or_damage,
    illusion_end,
    move,
    switch,
    zoroark_inference_allowed,
)
from fp.replay.checker import (
    _apply_illusion,
    _apply_slot_tera,
    _clamp_hp_from_protocol_certificates,
    _harvest_reveals,
    _illusion_switch_target,
    _infer_illusion_spans,
    _move_failed_sides,
    illusion_unresolved_turn,
)


def _sidecar(p1=(), p2=()):
    """{'p1': {species_key: mon}} shaped like damage_membership.load_teams_sidecar."""
    out = {}
    for pid, team in (("p1", p1), ("p2", p2)):
        out[pid] = {
            spec: {"species": spec, "ability": ability, "moves": list(moves)}
            for spec, ability, moves in team
        }
    return out


class TestSlotlessIdentifiers(unittest.TestCase):
    """sim/SIM-PROTOCOL.md:172 'An inactive Pokemon will not have a position
    letter'; sim/pokemon.ts:531-533 toString()."""

    def test_slotted_identifier_is_not_slotless(self):
        self.assertFalse(_ident_is_slotless("p2a: Rotom"))
        self.assertFalse(_ident_is_slotless("p1a: Weedle"))

    def test_bare_player_identifier_is_slotless(self):
        self.assertTrue(_ident_is_slotless("p2: Rotom"))
        self.assertTrue(_ident_is_slotless("p1: Sandaconda"))


class TestBenchLookupByProtocolName(unittest.TestCase):
    """gen9 randbats nicknames are the BASE species
    (data/random-battles/gen9/teams.ts:1598 `name: species.baseSpecies`)."""

    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.opponent.active = Pokemon("chansey", 100)
        self.rotom = Pokemon("rotomfan", 86)
        self.battle.opponent.reserve = [Pokemon("scizor", 100), self.rotom]

    def test_resolves_forme_from_base_species_name(self):
        self.assertIs(
            self.rotom,
            _find_bench_pokemon_by_protocol_name(self.battle.opponent, "Rotom"),
        )

    def test_resolves_exact_name(self):
        self.assertIs(
            self.rotom,
            _find_bench_pokemon_by_protocol_name(self.battle.opponent, "Rotom-Fan"),
        )

    def test_unknown_name_resolves_to_nothing(self):
        self.assertIsNone(
            _find_bench_pokemon_by_protocol_name(self.battle.opponent, "Gyarados")
        )


class TestCureStatusSlotless(unittest.TestCase):
    """Heal Bell cures every party member (data/moves.ts:8252-8268) and prints one
    `-curestatus` per ally against a slotless id (sim/pokemon.ts:1680-1682).
    synth47191 T27 / synth49098 T30."""

    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.active = Pokemon("chansey", 100)
        self.active.status = constants.TOXIC
        self.battle.opponent.active = self.active
        self.rotom = Pokemon("rotomfan", 86)
        self.rotom.status = constants.TOXIC
        self.battle.opponent.reserve = [self.rotom]
        self.battle.user.active = Pokemon("weedle", 100)

    def test_cures_benched_forme_named_by_base_species(self):
        curestatus(self.battle, ["", "-curestatus", "p2: Rotom", "tox", "[msg]"])

        self.assertIsNone(self.rotom.status)
        self.assertEqual(constants.TOXIC, self.active.status)

    def test_benched_cure_does_not_reset_the_actives_toxic_counter(self):
        self.battle.opponent.side_conditions[constants.TOXIC_COUNT] = 4

        curestatus(self.battle, ["", "-curestatus", "p2: Rotom", "tox", "[msg]"])

        self.assertEqual(
            4, self.battle.opponent.side_conditions[constants.TOXIC_COUNT]
        )

    def test_unresolvable_benched_cure_leaves_the_active_alone(self):
        curestatus(self.battle, ["", "-curestatus", "p2: Gyarados", "tox", "[msg]"])

        self.assertEqual(constants.TOXIC, self.active.status)
        self.assertEqual(constants.TOXIC, self.rotom.status)

    def test_slotted_cure_still_hits_the_active(self):
        curestatus(self.battle, ["", "-curestatus", "p2a: Chansey", "tox", "[msg]"])

        self.assertIsNone(self.active.status)
        self.assertEqual(constants.TOXIC, self.rotom.status)


class TestRevivalBlessingClearsStatus(unittest.TestCase):
    """sim/battle.ts:2778-2793: the revived target is un-fainted, `status = ''`
    and healed to half; the only line printed is the `-heal`.
    synth04078 T43 / synth09468 T24 / synth19727 T34."""

    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.opponent.active = Pokemon("rabsca", 91)
        self.fainted = Pokemon("duraludon", 82)
        self.fainted.nickname = "Duraludon"
        self.fainted.hp = 0
        self.fainted.status = constants.PARALYZED
        self.battle.opponent.reserve = [self.fainted]
        self.battle.user.active = Pokemon("weedle", 100)

    def test_revive_clears_status_and_restores_hp(self):
        heal_or_damage(
            self.battle,
            ["", "-heal", "p2: Duraludon", "50/100", "[from] move: Revival Blessing"],
        )

        self.assertIsNone(self.fainted.status)
        # PS revives at `sethp(maxhp / 2)` and `sethp` truncs
        # (sim/battle.ts:2791-2792, sim/pokemon.ts:1663), so an odd max HP
        # revives at floor(maxhp/2) -- 124, not 124.5.  This is an exact-HP
        # certificate, not a percent-derived estimate (fp/hp_certificate.py).
        self.assertEqual(self.fainted.max_hp // 2, self.fainted.hp)
        self.assertTrue(self.fainted.hp_exact)

    def test_revive_clears_sleep_counters(self):
        self.fainted.status = constants.SLEEP
        self.fainted.sleep_turns = 2
        self.fainted.rest_turns = 2

        heal_or_damage(
            self.battle,
            ["", "-heal", "p2: Duraludon", "50/100", "[from] move: Revival Blessing"],
        )

        self.assertIsNone(self.fainted.status)
        self.assertEqual(0, self.fainted.sleep_turns)
        self.assertEqual(0, self.fainted.rest_turns)


class TestSwitchLineStatusIsAuthoritative(unittest.TestCase):
    """sim/pokemon.ts:2104-2107 `getHealth` appends the ENTRANT's status to the
    HP STATUS field, and sim/pokemon.ts:544-552 `getFullDetails` takes that
    health from the real pokemon even when the species half is an illusion.
    synth27501 T38."""

    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.opponent.active = Pokemon("cramorant", 89)
        self.coalossal = Pokemon("coalossal", 89)
        self.coalossal.status = constants.PARALYZED
        self.battle.opponent.reserve = [self.coalossal]
        self.battle.user.active = Pokemon("blissey", 90)

    def test_statusless_switch_line_clears_a_stale_status(self):
        switch(
            self.battle,
            ["", "switch", "p2a: Coalossal", "Coalossal, L89, M", "100/100"],
        )

        self.assertIsNone(self.battle.opponent.active.status)

    def test_switch_line_status_is_applied(self):
        self.coalossal.status = None

        switch(
            self.battle,
            ["", "switch", "p2a: Coalossal", "Coalossal, L89, M", "100/100 brn"],
        )

        self.assertEqual(constants.BURN, self.battle.opponent.active.status)


class TestUserSideIllusionEnd(unittest.TestCase):
    """A |replace| on the USER's side must swap the reconstruction over to the
    real Zoroark too, or every later event lands on the impersonated party
    member (synth31992: the Zoroark's knocked-off Life Orb marked the real
    Salamence `knocked_off`)."""

    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.opponent.active = Pokemon("cyclizar", 84)
        self.salamence = Pokemon("salamence", 77)
        self.salamence.item = "heavydutyboots"
        self.zoroark = Pokemon("zoroark", 83)
        self.zoroark.item = "lifeorb"
        self.battle.user.active = self.salamence
        self.battle.user.reserve = [self.zoroark]

    def test_replace_swaps_the_users_active_to_the_zoroark(self):
        illusion_end(
            self.battle, ["", "replace", "p1a: Zoroark", "Zoroark, L83, M"]
        )

        self.assertEqual("zoroark", self.battle.user.active.name)
        self.assertIn(self.salamence, self.battle.user.reserve)
        self.assertTrue(self.battle.user.active.illusion_broken)


class TestZoroarkInferenceGate(unittest.TestCase):
    """The live-play "invent a Zoroark to explain this move" heuristic must not
    fire when the checker has loaded the exact-teams sidecar
    (synth10251/synth13718/...: Hatterene's Encore is missing from the bundled
    randbats snapshot and conjured a Zoroark that is not on the team)."""

    def setUp(self):
        self.battle = Battle(None)
        self.battle.user.name = "p1"
        self.battle.opponent.name = "p2"
        self.battle.battle_type = BattleType.RANDOM_BATTLE
        self.battle.generation = "gen9"
        self.battle.opponent.active = Pokemon("hatterene", 84)
        self.battle.user.active = Pokemon("mesprit", 80)

    def test_allowed_by_default(self):
        self.assertTrue(zoroark_inference_allowed(self.battle))

    def test_disallowed_when_the_roster_is_known(self):
        self.battle.exact_roster_known = True
        self.assertFalse(zoroark_inference_allowed(self.battle))

    def test_impossible_move_does_not_invent_a_zoroark_in_checker_mode(self):
        self.battle.exact_roster_known = True

        move(self.battle, ["", "move", "p2a: Hatterene", "Encore", "p1a: Mesprit"])

        self.assertEqual("hatterene", self.battle.opponent.active.name)
        self.assertEqual([], self.battle.opponent.reserve)


class TestInferIllusionSpans(unittest.TestCase):
    """data/abilities.ts:2061-2071: |replace| is only printed when the disguise
    is BROKEN by a damaging hit, so a Zoroark that pivots out untouched is never
    announced.  The sidecar movesets settle those stays."""

    OPP_TEAM = (
        ("zoroark", "Illusion", ["darkpulse", "uturn", "focusblast", "sludgebomb"]),
        ("dusknoir", "Frisk", ["shadowsneak", "earthquake", "poltergeist", "painsplit"]),
        ("kilowattrel", "Volt Absorb", ["hurricane", "uturn", "thunderbolt", "roost"]),
    )

    def _reveals(self, chunks, p2=None):
        reveals = _harvest_reveals(chunks)
        _infer_illusion_spans(reveals, _sidecar(p2=p2 if p2 is not None else self.OPP_TEAM))
        return reveals

    def test_impossible_move_proves_a_never_replaced_disguise(self):
        # synth03480 T1: the lead "Dusknoir" U-turns; Dusknoir has no U-turn and
        # Zoroark does, so the whole stay was the Zoroark
        chunks = [
            "|switch|p2a: Dusknoir|Dusknoir, L89, M|100/100\n|turn|1",
            "|move|p2a: Dusknoir|U-turn|p1a: Heatran\n"
            "|switch|p2a: Kilowattrel|Kilowattrel, L83, M|100/100|[from] U-turn\n|turn|2",
        ]
        spans = self._reveals(chunks)["illusions"]

        self.assertEqual(1, len(spans))
        self.assertEqual("dusknoir", spans[0]["disguise"])
        self.assertEqual("zoroark", spans[0]["true_species"])
        self.assertEqual(["uturn"], spans[0]["inferred_from"])
        self.assertEqual(0, spans[0]["start_turn"])
        self.assertEqual(1, spans[0]["end_turn"])

    def test_a_move_only_the_shown_species_has_settles_it_as_genuine(self):
        chunks = [
            "|switch|p2a: Dusknoir|Dusknoir, L89, M|100/100\n|turn|1",
            "|move|p2a: Dusknoir|Poltergeist|p1a: Heatran\n|turn|2",
        ]
        reveals = self._reveals(chunks)

        self.assertEqual([], reveals["illusions"])
        self.assertEqual({}, reveals["illusion_unresolved"])

    def test_an_unprovable_stay_is_recorded_as_unresolved(self):
        # Sludge Bomb is on BOTH sets, so it witnesses nothing either way
        shared = (
            ("zoroark", "Illusion", ["darkpulse", "uturn", "focusblast", "sludgebomb"]),
            ("dusknoir", "Frisk", ["shadowsneak", "earthquake", "poltergeist", "sludgebomb"]),
            ("kilowattrel", "Volt Absorb", ["hurricane", "uturn", "thunderbolt", "roost"]),
        )
        chunks = [
            "|switch|p2a: Dusknoir|Dusknoir, L89, M|100/100\n|turn|1",
            "|move|p2a: Dusknoir|Sludge Bomb|p1a: Heatran\n|turn|2",
        ]
        reveals = self._reveals(chunks, p2=shared)

        self.assertEqual([], reveals["illusions"])
        self.assertEqual({"p2": [(0, 2)]}, reveals["illusion_unresolved"])
        self.assertTrue(illusion_unresolved_turn(reveals, "p2", 1))
        self.assertTrue(illusion_unresolved_turn(reveals, "p2", 2))
        # the ENTRY turn's pre-state still holds the previous occupant
        self.assertFalse(illusion_unresolved_turn(reveals, "p2", 0))
        self.assertFalse(illusion_unresolved_turn(reveals, "p1", 1))

    def test_called_moves_are_not_evidence(self):
        # a |move| line tagged [from] was selected by another effect, not by this
        # pokemon's own moveset
        chunks = [
            "|switch|p2a: Dusknoir|Dusknoir, L89, M|100/100\n|turn|1",
            "|move|p2a: Dusknoir|U-turn|p1a: Heatran|[from]move: Sleep Talk\n|turn|2",
        ]
        reveals = self._reveals(chunks)

        self.assertEqual([], reveals["illusions"])

    def test_replace_span_is_extended_to_the_end_of_the_occupancy(self):
        # |replace| fires at the reveal turn but the mon stays in the slot; on the
        # user side nothing re-identifies the active afterwards
        chunks = [
            "|switch|p2a: Dusknoir|Dusknoir, L89, M|100/100\n|turn|1",
            "|move|p2a: Dusknoir|Dark Pulse|p1a: Heatran\n"
            "|replace|p2a: Zoroark|Zoroark, L83, M\n|turn|2",
            "|move|p2a: Zoroark|Dark Pulse|p1a: Heatran\n|turn|3",
            "|switch|p2a: Kilowattrel|Kilowattrel, L83, M|100/100\n|turn|4",
        ]
        spans = self._reveals(chunks)["illusions"]

        self.assertEqual(1, len(spans))
        self.assertEqual("dusknoir", spans[0]["disguise"])
        self.assertEqual(0, spans[0]["start_turn"])
        self.assertEqual(3, spans[0]["end_turn"])

    def test_no_illusion_bearer_means_no_spans_and_no_refusals(self):
        chunks = [
            "|switch|p2a: Dusknoir|Dusknoir, L89, M|100/100\n|turn|1",
            "|move|p2a: Dusknoir|U-turn|p1a: Heatran\n|turn|2",
        ]
        reveals = self._reveals(chunks, p2=self.OPP_TEAM[1:])

        self.assertEqual([], reveals["illusions"])
        self.assertEqual({}, reveals["illusion_unresolved"])

    def test_the_bearers_own_tera_is_read_off_the_switch_line(self):
        # sim/pokemon.ts:553 appends ", tera:X" for the ENTRANT, so an untera'd
        # Zoroark wearing a terastallized mon's face carries no suffix
        chunks = [
            "|switch|p2a: Dusknoir|Dusknoir, L89, M, tera:Ghost|100/100\n|turn|1",
            "|move|p2a: Dusknoir|U-turn|p1a: Heatran\n|turn|2",
        ]
        spans = self._reveals(chunks)["illusions"]

        self.assertEqual("ghost", spans[0]["bearer_tera"])


class TestApplyIllusion(unittest.TestCase):
    def setUp(self):
        self.battler = Battle(None).user
        self.active = Pokemon("garganacl", 80)
        self.battler.active = self.active

    def test_types_are_replaced_inside_the_span(self):
        reveals = {
            "illusions": [
                {
                    "pid": "p1",
                    "disguise": "garganacl",
                    "true_species": "zoroarkhisui",
                    "start_turn": 7,
                    "end_turn": 12,
                }
            ]
        }

        _apply_illusion(self.battler, "p1", reveals, 9)

        self.assertEqual(["normal", "ghost"], self.active.types)

    def test_types_are_untouched_outside_the_span(self):
        reveals = {
            "illusions": [
                {
                    "pid": "p1",
                    "disguise": "garganacl",
                    "true_species": "zoroarkhisui",
                    "start_turn": 7,
                    "end_turn": 12,
                }
            ]
        }
        original = list(self.active.types)

        _apply_illusion(self.battler, "p1", reveals, 13)
        _apply_illusion(self.battler, "p1", reveals, 7)

        self.assertEqual(original, self.active.types)

    def test_the_impersonated_mons_terastallization_is_stripped(self):
        # synth31657: the real Garganacl teras Dragon on turn 1, so the
        # Zoroark-Hisui wearing its face read as Dragon and Close Combat was not
        # immune
        self.active.terastallized = True
        self.active.tera_type = "dragon"
        reveals = {
            "illusions": [
                {
                    "pid": "p1",
                    "disguise": "garganacl",
                    "true_species": "zoroarkhisui",
                    "start_turn": 7,
                    "end_turn": 12,
                    "bearer_tera": None,
                }
            ]
        }

        _apply_illusion(self.battler, "p1", reveals, 9)

        self.assertFalse(self.active.terastallized)
        self.assertEqual(["normal", "ghost"], self.active.types)

    def test_the_bearers_own_terastallization_is_kept(self):
        reveals = {
            "illusions": [
                {
                    "pid": "p1",
                    "disguise": "garganacl",
                    "true_species": "zoroarkhisui",
                    "start_turn": 7,
                    "end_turn": 12,
                    "bearer_tera": "dark",
                }
            ]
        }

        _apply_illusion(self.battler, "p1", reveals, 9)

        self.assertTrue(self.active.terastallized)
        self.assertEqual("dark", self.active.tera_type)
        # `types` is the BEARER'S BASE pair: the engine reads it as PS's
        # `getTypes(false, true)` for the STAB stage and takes the defensive
        # typing from `terastallized`/`tera_type`
        self.assertEqual(["normal", "ghost"], self.active.types)


if __name__ == "__main__":
    unittest.main()


class TestSlotBasedTeraAttribution(unittest.TestCase):
    """`|-terastallize|` names a SLOT and renders the occupant through toString()
    (sim/pokemon.ts:531), so a name-keyed binding credits the tera to the disguise
    species' real owner (synth22893 / synth27340) or misses it entirely when the
    reconstruction tracks a different forme name (synth42893 Minior)."""

    def _battler(self, species):
        battler = Battle(None).user
        battler.active = Pokemon(species, 80)
        return battler

    # a real log puts the `|turn|N` line BEFORE turn N's resolution block, so the tera
    # that lands during turn 2 is harvested with turn == 2
    TERA_CHUNKS = [
        "|switch|p2a: Minior|Minior-Meteor, L80, F|100/100",
        "|turn|1",
        "|turn|2\n|-terastallize|p2a: Minior|Flying",
        "|turn|3",
    ]

    def test_tera_lands_on_the_slot_occupant_despite_a_forme_rename(self):
        # the protocol says "Minior"; the reconstruction tracks miniormeteor
        reveals = _harvest_reveals(self.TERA_CHUNKS)
        battler = self._battler("miniormeteor")

        _apply_slot_tera(battler, "p2", reveals, 3)

        self.assertEqual("flying", battler.active.tera_type)
        self.assertTrue(battler.active.terastallized)

    def test_pre_tera_turn_knows_the_type_but_is_not_yet_terastallized(self):
        reveals = _harvest_reveals(self.TERA_CHUNKS)
        battler = self._battler("miniormeteor")

        # the tera lands during turn 2's resolution, so turn 2's PRE-state is un-tera'd
        _apply_slot_tera(battler, "p2", reveals, 2)

        self.assertEqual("flying", battler.active.tera_type)
        self.assertFalse(battler.active.terastallized)

    def test_a_bogus_tera_is_cleared_for_a_later_occupant_when_a_bearer_exists(self):
        # the disguised Zoroark tera'd; the REAL mon that comes in afterwards did not
        chunks = [
            "|switch|p2a: Rayquaza|Rayquaza, L74|100/100\n|turn|1",
            "|-terastallize|p2a: Rayquaza|Fighting\n"
            "|replace|p2a: Zoroark|Zoroark-Hisui, L80, M\n"
            "|faint|p2a: Zoroark\n"
            "|switch|p2a: Rayquaza|Rayquaza, L74|100/100\n|turn|2",
            "|turn|3",
        ]
        reveals = _harvest_reveals(chunks)
        reveals["illusion_bearers"] = {"p2": "zoroarkhisui"}
        battler = self._battler("rayquaza")
        battler.active.terastallized = True
        battler.active.tera_type = "fighting"

        _apply_slot_tera(battler, "p2", reveals, 3)

        self.assertFalse(battler.active.terastallized)

    def test_no_illusion_bearer_means_the_live_tracking_is_left_alone(self):
        chunks = ["|switch|p2a: Dragonite|Dragonite, L80, F|100/100\n|turn|1", "|turn|2"]
        reveals = _harvest_reveals(chunks)
        battler = self._battler("dragonite")
        battler.active.terastallized = True
        battler.active.tera_type = "normal"

        _apply_slot_tera(battler, "p2", reveals, 2)

        self.assertTrue(battler.active.terastallized)


class TestAnnouncedSpanIdentity(unittest.TestCase):
    """(pid, start_turn, species) is not an identity: a revealed Zoroark that FAINTS is
    replaced on the same turn, and if the replacement is the genuine mon of the disguise
    species the two occupancies collide.  Only the |replace|-stamped occupancy may extend
    the announced span (synth28469 T17, synth33481 T27)."""

    TEAM = (
        ("zoroarkhisui", "Illusion", ["hypervoice", "uturn", "shadowball", "focusblast"]),
        ("piloswine", "Thick Fat", ["iciclecrash", "earthquake", "iceshard", "roar"]),
    )

    def test_span_is_not_extended_by_the_real_mon_entering_on_the_same_turn(self):
        chunks = [
            "|switch|p2a: Piloswine|Piloswine, L80, F|100/100\n|turn|1",
            "|move|p2a: Piloswine|Hyper Voice|p1a: Heatran\n|turn|2",
            "|replace|p2a: Zoroark|Zoroark-Hisui, L80, M\n"
            "|faint|p2a: Zoroark\n"
            "|switch|p2a: Piloswine|Piloswine, L80, F|100/100\n|turn|3",
            "|move|p2a: Piloswine|Icicle Crash|p1a: Heatran\n|turn|4",
        ]
        reveals = _harvest_reveals(chunks)
        _infer_illusion_spans(reveals, _sidecar(p2=self.TEAM))

        announced = [il for il in reveals["illusions"] if il["start_turn"] == 0]
        self.assertEqual(1, len(announced))
        # the span must stop at the reveal, not run on over the genuine Piloswine
        self.assertLessEqual(announced[0]["end_turn"], 2)


class TestIllusionSwitchTarget(unittest.TestCase):
    """A switch-in the log |replace|s in the same turn brought in the BEARER, so the
    engine must switch in the real mon (synth29327 T4, synth40054 T10)."""

    def test_entry_turn_switch_is_redirected_to_the_true_species(self):
        reveals = {
            "illusions": [
                {
                    "pid": "p2",
                    "disguise": "primarina",
                    "true_species": "zoroark",
                    "start_turn": 4,
                    "end_turn": 4,
                }
            ]
        }

        self.assertEqual(
            "zoroark", _illusion_switch_target(reveals, "p2", 4, "primarina")
        )
        # a LATER turn is not an entry, and the other side is untouched
        self.assertEqual(
            "primarina", _illusion_switch_target(reveals, "p2", 5, "primarina")
        )
        self.assertEqual(
            "primarina", _illusion_switch_target(reveals, "p1", 4, "primarina")
        )


class TestSlotSleepAttempts(unittest.TestCase):
    """PS decrements the sleep counter once per attempted action (conditions.ts slp
    onBeforeMove).  The reconstruction counts it per-POKEMON, which Illusion breaks:
    the disguised sleeper and the genuine mon of that species are one object
    (synth25500 T45/T46 served by a Zoroark wearing Skeledirge's face)."""

    def test_attempts_are_counted_on_the_slot(self):
        chunks = [
            "|switch|p2a: Skeledirge|Skeledirge, L79, F|100/100\n|turn|44",
            "|-status|p2a: Skeledirge|slp|[from] move: Spore\n|turn|45",
            "|cant|p2a: Skeledirge|slp\n|turn|46",
            "|cant|p2a: Skeledirge|slp\n|turn|47",
        ]
        by_turn = _harvest_reveals(chunks)["sleep_attempts_by_turn"]

        self.assertEqual(0, by_turn[("p2a", 45)])
        self.assertEqual(1, by_turn[("p2a", 46)])
        self.assertEqual(2, by_turn[("p2a", 47)])


class TestMoveFailedHarvest(unittest.TestCase):
    """PS `moveThisTurnResult === false`, read as the NEXT turn's `moveLastTurnResult`
    (data/moves.ts temperflare basePowerCallback)."""

    def test_an_absorbed_move_counts_as_failed(self):
        # synth19386 T18: Waterfall into Water Absorb, announced only by the heal
        block = [
            "|move|p2a: Gyarados|Waterfall|p1a: Poliwrath",
            "|-heal|p1a: Poliwrath|302/302|[from] ability: Water Absorb|[of] p2a: Gyarados",
            "|move|p1a: Poliwrath|Bulk Up|p1a: Poliwrath",
            "|-boost|p1a: Poliwrath|atk|1",
        ]

        self.assertEqual({"p1": False, "p2": True}, _move_failed_sides(block))

    def test_a_miss_counts_and_a_landed_move_does_not(self):
        block = [
            "|move|p1a: Uxie|Thunder Wave|p2a: Gogoat|[miss]",
            "|-miss|p1a: Uxie|p2a: Gogoat",
            "|move|p2a: Gogoat|Bulk Up|p2a: Gogoat",
            "|-boost|p2a: Gogoat|atk|1",
        ]

        self.assertEqual({"p1": True, "p2": False}, _move_failed_sides(block))

    def test_a_gen9_protect_block_does_not_count(self):
        block = [
            "|move|p1a: Uxie|U-turn|p2a: Gogoat",
            "|-activate|p2a: Gogoat|move: Protect",
        ]

        self.assertEqual({"p1": False, "p2": False}, _move_failed_sides(block))

    def test_a_side_that_switched_after_failing_does_not_carry_the_flag(self):
        # the engine flag is per-SIDE while PS's is per-POKEMON
        block = [
            "|move|p1a: Uxie|Thunder Wave|p2a: Gogoat|[miss]",
            "|-miss|p1a: Uxie|p2a: Gogoat",
            "|switch|p1a: Clawitzer|Clawitzer, L87, F|265/265",
        ]

        self.assertEqual({"p1": False, "p2": False}, _move_failed_sides(block))


class TestSubstituteWeakHpClamp(unittest.TestCase):
    """`|-fail|SLOT|move: Substitute|[weak]` is PS's substitute onTry bailing on
    `source.hp <= source.maxhp / 4`, so it certifies an HP bound the percent-HP
    reconstruction rounded across (synth02704 T8: 74/295 = 25.08%)."""

    def _snap(self, hp, max_hp, hp_exact=False):
        battle = Battle(None)
        battle.user.active = Pokemon("articuno", 84)
        battle.opponent.active = Pokemon("corviknight", 80)
        battle.user.active.hp = hp
        battle.user.active.max_hp = max_hp
        # the clamp exists for a PERCENT-derived estimate; an exact-HP
        # certificate (fp/hp_certificate.py) is better information than the
        # bound and is handled separately below
        battle.user.active.hp_exact = hp_exact
        return battle

    def test_hp_is_clamped_to_a_quarter(self):
        snap = self._snap(74, 295)

        _clamp_hp_from_protocol_certificates(
            [
                "|move|p1a: Articuno|Substitute|p1a: Articuno",
                "|-fail|p1a: Articuno|move: Substitute|[weak]",
            ],
            snap,
            "p1",
        )

        self.assertEqual(73, snap.user.active.hp)

    def test_a_mid_block_hp_change_before_the_fail_blocks_the_clamp(self):
        snap = self._snap(200, 295)

        _clamp_hp_from_protocol_certificates(
            [
                "|move|p2a: Corviknight|Brave Bird|p1a: Articuno",
                "|-damage|p1a: Articuno|74/295",
                "|move|p1a: Articuno|Substitute|p1a: Articuno",
                "|-fail|p1a: Articuno|move: Substitute|[weak]",
            ],
            snap,
            "p1",
        )

        self.assertEqual(200, snap.user.active.hp)

    def test_a_certificate_that_satisfies_the_bound_is_not_clamped_to_it(self):
        # 60 <= 295 // 4 == 73: the bound is satisfied, and an exactly-stated HP
        # must not be pushed up to the inequality's edge
        snap = self._snap(60, 295, hp_exact=True)

        _clamp_hp_from_protocol_certificates(
            [
                "|move|p1a: Articuno|Substitute|p1a: Articuno",
                "|-fail|p1a: Articuno|move: Substitute|[weak]",
            ],
            snap,
            "p1",
        )

        self.assertEqual(60, snap.user.active.hp)
        self.assertTrue(snap.user.active.hp_exact)

    def test_a_certificate_that_violates_the_bound_is_refused_loudly(self):
        # two protocol statements disagreeing is a broken certification chain,
        # not something to settle silently in either direction
        hp_certificate.reset_refusals()
        snap = self._snap(200, 295, hp_exact=True)

        _clamp_hp_from_protocol_certificates(
            [
                "|move|p1a: Articuno|Substitute|p1a: Articuno",
                "|-fail|p1a: Articuno|move: Substitute|[weak]",
            ],
            snap,
            "p1",
        )

        self.assertEqual(73, snap.user.active.hp)
        self.assertFalse(snap.user.active.hp_exact)
        self.assertEqual(len(hp_certificate.CERTIFICATE_REFUSALS), 1)
