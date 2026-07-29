"""Pins for the three real-ladder adjudication fixes (gate-8 blocker D).

The 46-row real-ladder soft residue decomposed into exactly three located
reconstruction defects (adjudicated per row, by execution, gate 9 wave):

1. `reportExactHP` displays parsed as percents — every opponent condition
   string was fed to the pct/100 machinery, so an exact display with a
   numerator >= 100 clamped the mon back to FULL HP.  PS ground truth:
   sim/pokemon.ts:2065-2086 (`getHealth` shares the secret `hp/maxhp` string
   whenever `battle.reportExactHP` is set, sim/battle.ts:227).
   8 rows, all in local customgame logs.

2. `_harvest_reveals` had no arm for residual `-heal`/`-damage` lines tagged
   `[from] item: X`, which for Leftovers / Black Sludge / Life Orb are the ONLY
   protocol evidence the item exists — so the FIRST such heal of a game was
   unreproducible in its own turn's pre-state.  33 rows, every one a
   first-reveal `heal [from] item: Leftovers` turn.

3. A `|replace|`-proven Illusion bearer makes EARLIER same-species occupancies
   protocol-undecidable, but only the announced occupancy was covered; the
   checker asserted the shown species on the earlier stay (a "Zacian" that is
   Psychic-immune and carries Dark Pulse — Sassyflygon2 T13).  Fixed by
   refusal, not assertion: `_mark_protocol_illusion_ambiguity`, gated to
   no-sidecar logs so sidecar-proven spans keep their coverage.
"""

from fp import hp_certificate
from fp.replay.checker import (
    _harvest_reveals,
    _mark_protocol_illusion_ambiguity,
)


class TestExactDisplayHp:
    def test_exact_display_is_recognised(self):
        assert hp_certificate.exact_display_hp("162/238") == (162, 238)

    def test_numerator_at_or_above_100_is_not_clamped(self):
        # the defect shape: "179/238" fed to the percent path clamps to FULL
        assert hp_certificate.exact_display_hp("179/238") == (179, 238)

    def test_percent_display_is_not_exact(self):
        assert hp_certificate.exact_display_hp("76/100") is None

    def test_pixel_display_is_not_exact(self):
        assert hp_certificate.exact_display_hp("24/48") is None

    def test_fnt_and_malformed_are_not_exact(self):
        assert hp_certificate.exact_display_hp("0 fnt") is None
        assert hp_certificate.exact_display_hp("") is None
        # champions letter suffix ("50g") fails the int parse -> percent path
        assert hp_certificate.exact_display_hp("50/100g") is None

    def test_status_suffix_is_tolerated(self):
        assert hp_certificate.exact_display_hp("162/238 brn") == (162, 238)

    def test_set_exact_from_display_pins_both_values(self):
        class P:
            pass

        p = P()
        p.hp = 238
        p.max_hp = 238
        hp_certificate.init_pokemon(p)
        hp_certificate.set_exact_from_display(p, (162, 238))
        assert p.hp == 162
        assert p.max_hp == 238
        assert p.max_hp_exact is True
        assert hp_certificate.is_exact(p)


def _chunks(lines):
    return ["\n".join(lines)]


class TestResidualItemHarvest:
    def test_leftovers_heal_reveals_a_start_of_battle_item(self):
        reveals = _harvest_reveals(
            _chunks(
                [
                    "|turn|1",
                    "|switch|p1a: Keldeo|Keldeo-Resolute, L79|100/100",
                    "|turn|15",
                    "|-heal|p1a: Keldeo|82/100|[from] item: Leftovers",
                ]
            )
        )
        assert reveals["items"][("p1", "keldeoresolute")] == ("leftovers", None)

    def test_life_orb_damage_reveals_the_item(self):
        reveals = _harvest_reveals(
            _chunks(
                [
                    "|turn|1",
                    "|switch|p1a: Zacian|Zacian, L69|100/100",
                    "|-damage|p1a: Zacian|91/100|[from] item: Life Orb",
                ]
            )
        )
        assert reveals["items"][("p1", "zacian")] == ("lifeorb", None)

    def test_of_slot_owns_the_item(self):
        reveals = _harvest_reveals(
            _chunks(
                [
                    "|turn|1",
                    "|switch|p1a: Corviknight|Corviknight, L80|100/100",
                    "|switch|p2a: Garchomp|Garchomp, L78|100/100",
                    "|-damage|p1a: Corviknight|84/100|[from] item: Rocky Helmet|[of] p2a: Garchomp",
                ]
            )
        )
        assert reveals["items"][("p2", "garchomp")] == ("rockyhelmet", None)
        assert ("p1", "corviknight") not in reveals["items"]

    def test_acquired_item_is_not_written_back_as_a_start_item(self):
        # a Tricked-on Leftovers heals later; the heal must not claim the
        # receiver STARTED with Leftovers
        reveals = _harvest_reveals(
            _chunks(
                [
                    "|turn|1",
                    "|switch|p1a: Slowking|Slowking, L85|100/100",
                    "|turn|3",
                    "|-item|p1a: Slowking|Leftovers|[from] move: Trick",
                    "|turn|4",
                    "|-heal|p1a: Slowking|90/100|[from] item: Leftovers",
                ]
            )
        )
        assert ("p1", "slowking") not in reveals["items"]
        gains = reveals["item_gains"][("p1", "slowking")]
        assert gains and gains[0][1] == "leftovers"


class TestProtocolIllusionAmbiguity:
    @staticmethod
    def _reveals():
        # the Sassyflygon2 shape: an early "Zacian" occupancy, the
        # |replace|-announced Zoroark occupancy, the bearer's faint, and the
        # REAL Zacian entering afterwards
        return {
            "illusions": [
                {
                    "pid": "p1",
                    "disguise": "zacian",
                    "true_species": "zoroark",
                    "start_turn": 16,
                    "end_turn": 18,
                }
            ],
            "occupancies": [
                {"pid": "p1", "species": "zacian", "start_turn": 12,
                 "end_turn": 14, "transformed": False},
                {"pid": "p1", "species": "zacian", "start_turn": 16,
                 "end_turn": 18, "transformed": False},
                {"pid": "p1", "species": "zacian", "start_turn": 18,
                 "end_turn": 25, "transformed": False},
            ],
            "faint_turns": {("p1", "zoroark"): 18},
        }

    def test_pre_replace_same_species_occupancy_is_marked(self):
        reveals = self._reveals()
        _mark_protocol_illusion_ambiguity(reveals)
        assert (12, 14) in reveals["illusion_unresolved"]["p1"]

    def test_the_announced_occupancy_itself_is_not_marked(self):
        reveals = self._reveals()
        _mark_protocol_illusion_ambiguity(reveals)
        assert (16, 18) not in reveals["illusion_unresolved"]["p1"]

    def test_post_faint_occupancy_is_genuine(self):
        reveals = self._reveals()
        _mark_protocol_illusion_ambiguity(reveals)
        assert (18, 25) not in reveals["illusion_unresolved"]["p1"]

    def test_other_species_are_left_alone(self):
        reveals = self._reveals()
        reveals["occupancies"].append(
            {"pid": "p1", "species": "rhydon", "start_turn": 5,
             "end_turn": 8, "transformed": False}
        )
        _mark_protocol_illusion_ambiguity(reveals)
        assert (5, 8) not in reveals["illusion_unresolved"].get("p1", [])
