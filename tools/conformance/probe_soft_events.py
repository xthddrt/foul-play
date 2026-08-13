#!/usr/bin/env python
"""Per-event adjudication probe for surviving SOFT findings.

Sally's bar: a soft finding may only be dismissed as a checker-side artifact if,
FOR THAT EVENT, the engine's branch set actually contains the observed outcome at
the correct weight -- and the demotion is purely the checker declining to assert
at a roll-dependent KO boundary. Class-level dismissal is not sufficient.

That evidence cannot come from the finding record: a finding exists precisely
BECAUSE no branch matched. So this probe goes back to the engine.

`checker.py` documents why the ko-margin class exists: the python binding's
`generate_instructions` folds every hit to a single 0.925*max_damage roll and
exposes no damage-branching knob, so a real roll that landed a hair either side
of the defender's HP makes every faint-gated companion effect unreachable in
EVERY branch. But `calculate_damage_rolls_full` DOES expose all 16 Showdown-exact
rolls. So for each event we can ask the decisive question directly:

    does the defender's HP fall INSIDE the engine's own roll set?

- STRADDLE -> some rolls KO and some do not. The observed outcome is reachable
  in the engine's true branch set at weight (#surviving rolls)/16; only the
  0.925 fold hid it. Artifact, quantified.
- NO STRADDLE -> every roll agrees on the KO outcome. The KO boundary does NOT
  explain the finding, so the ko-margin tag is not a licence to dismiss it.
  Route for a fix.
"""
import json
import os
import sys

import poke_engine as pe

FINDINGS = sys.argv[1]


def rolls_for(f):
    """-> (s1_rolls, s2_rolls) non-crit, or (None, None) if the state won't build."""
    ss = f.get("state_string") or ""
    if not ss:
        return None, None, "no state_string"
    try:
        st = pe.State.from_string(ss)
    except Exception as e:
        return None, None, "state parse failed: {}".format(e)
    um = f.get("user_move") or ""
    om = f.get("opp_move") or ""
    out = []
    for first in (True, False):
        try:
            r = pe.calculate_damage_rolls_full(st, um, om, first)
            out.append(r)
        except Exception as e:
            return None, None, "engine call failed: {}".format(e)
    return st, out, ""


def main():
    F = json.load(open(FINDINGS))
    soft = [f for f in F if f.get("severity") != "hard"]
    print("adjudicating {} soft findings\n".format(len(soft)))
    for i, f in enumerate(soft, 1):
        print("=" * 100)
        print("[{}] {} t{} {}".format(i, f.get("game"), f.get("turn"),
                                      f.get("category")))
        print("    {}".format(f.get("message")))
        print("    observed: {}".format(f.get("observed")))
        print("    moves: user={} opp={}".format(f.get("user_move"), f.get("opp_move")))
        st, out, err = rolls_for(f)
        if err:
            print("    PROBE: {}".format(err))
            continue
        # defender HP for each side, from the reconstructed state
        try:
            def act(side):
                # active_index is a PokemonIndex enum (P0..P5); side.pokemon is a
                # plain list, so the enum has to become an integer offset
                idx = str(side.active_index).rsplit(".", 1)[-1].lstrip("Pp")
                return side.pokemon[int(idx)]
            s1, s2 = act(st.side_one), act(st.side_two)
            hp = {"s1": (s1.hp, s1.maxhp, s1.id), "s2": (s2.hp, s2.maxhp, s2.id)}
        except Exception as e:
            print("    PROBE: state introspection failed: {}".format(e))
            continue
        print("    state: side_one {} {}/{} | side_two {} {}/{}".format(
            hp["s1"][2], hp["s1"][0], hp["s1"][1],
            hp["s2"][2], hp["s2"][0], hp["s2"][1]))
        for label, res in zip(("s1_first", "s2_first"), out):
            if res is None:
                continue
            for side_idx, side_name, target in ((0, "side_one_attacks", "s2"),
                                                (1, "side_two_attacks", "s1")):
                pack = res[side_idx] if res and len(res) > side_idx else None
                if not pack:
                    continue
                noncrit = pack[0] if pack else []
                if not noncrit:
                    continue
                thp = hp[target][0]
                ko = sum(1 for r in noncrit if r >= thp)
                surv = len(noncrit) - ko
                straddle = ko > 0 and surv > 0
                print("      [{}] {}: rolls {}..{} (n={}) vs target HP {} -> "
                      "KO {} / survive {}  {}".format(
                          label, side_name, min(noncrit), max(noncrit),
                          len(noncrit), thp, ko, surv,
                          "STRADDLE (artifact, weight {}/{})".format(surv, len(noncrit))
                          if straddle else "no straddle"))


if __name__ == "__main__":
    main()
