"""Audit how often each selection-override layer actually changes a decision.

The layers in fp/search/main.py sit above the search and override its argmax.
Each is a hand-tuned constant justified by a small number of forensic cases, so
before tuning one, measure it: how often does its activation window open, and
how often does opening it actually change the pick?

Reads INFO-level battle logs (foul-play/logs/*.log).

    python audit_selection.py 'logs/*.log'
    python audit_selection.py 'logs/*.log' --upside-threshold 0.15

--upside-threshold replays the upside tiebreak's gate at a different value and
reports which recorded firings it would suppress. It cannot predict firings the
old threshold never reached (a wider gate may open on decisions whose pair
tables were never logged), so it is a lower bound in that direction.
"""

import argparse
import glob
import os
import re
from collections import Counter

CHOICE_ROW = re.compile(r"^\s*(\S.*?): share=([\d.]+)% score=([\d.]+)")

# One entry per override layer in fp/search/main.py, newest-first in the stack.
LAYERS = {
    "score-gate": "Score-gate: disallowed",
    "tera-margin-gate": "Tera-margin gate: disallowed",
    "significance-forfeit": "Significance forfeit:",
    "upside-tiebreak": "Losing-position upside tiebreak:",
    "attack-fallback": "Losing-position fallback fired:",
    "variance-suspended": "Variance penalty suspended:",
}

UPSIDE_FIRING = re.compile(
    r"Losing-position upside tiebreak: (.+?) -> (.+?) \(upside ([\d.]+) vs ([\d.]+)\)"
)


def parse_log(path):
    """Yield one dict per logged decision."""
    decisions = []
    pending = None
    for line in open(path, errors="ignore"):
        line = line.rstrip("\n")
        body = line.split(None, 1)[1] if line.split(None, 1)[1:] else ""

        if "Considered Choices:" in line:
            pending = {"options": [], "layers": [], "chosen": None, "log": path}
            continue
        if pending is None:
            continue

        m = CHOICE_ROW.match(body)
        if m:
            pending["options"].append(
                (m.group(1), float(m.group(2)) / 100.0, float(m.group(3)))
            )
            continue

        for name, needle in LAYERS.items():
            if needle in line:
                pending["layers"].append(name)
                if name == "upside-tiebreak":
                    f = UPSIDE_FIRING.search(line)
                    if f:
                        pending["upside"] = {
                            "from": f.group(1),
                            "to": f.group(2),
                            "win_upside": float(f.group(3)),
                            "lose_upside": float(f.group(4)),
                        }

        if "Choice: " in line:
            pending["chosen"] = line.split("Choice: ", 1)[1].strip()
            if pending["options"]:
                decisions.append(pending)
            pending = None
    return decisions


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pattern", nargs="?", default="logs/*.log")
    ap.add_argument("--upside-threshold", type=float, default=None)
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.expanduser(args.pattern)))
    decisions = [d for f in files for d in parse_log(f)]
    if not decisions:
        print("no decisions parsed — are these INFO-level logs?")
        return

    print("logs scanned:      {}".format(len(files)))
    print("decisions logged:  {}".format(len(decisions)))

    fired = Counter(n for d in decisions for n in d["layers"])
    changed = Counter()
    for d in decisions:
        argmax = max(d["options"], key=lambda o: o[2])[0]
        if d["chosen"] != argmax:
            for n in d["layers"]:
                changed[n] += 1

    print("\nlayer                  fired   decisions-where-pick != search argmax")
    for name in LAYERS:
        print("  {:<20} {:>5}   {:>5}".format(name, fired.get(name, 0), changed.get(name, 0)))

    scores = sorted(max(o[2] for o in d["options"]) for d in decisions)
    print("\nbest-score distribution (gates key off this):")
    for t in (0.05, 0.10, 0.15, 0.20, 0.25, 0.35):
        n = sum(1 for s in scores if s < t)
        print("  best < {:.2f}:  {:>5}  ({:.1f}%)".format(t, n, 100 * n / len(scores)))

    if args.upside_threshold is not None:
        t = args.upside_threshold
        firings = [d for d in decisions if "upside" in d]
        keep = [d for d in firings if max(o[2] for o in d["options"]) < t]
        print("\nupside tiebreak replayed at threshold {}:".format(t))
        print("  recorded firings:  {}".format(len(firings)))
        print("  still fire:        {}".format(len(keep)))
        print("  suppressed:        {}".format(len(firings) - len(keep)))
        for d in firings:
            best = max(o[2] for o in d["options"])
            print(
                "    {:<12} best={:.3f}  {} -> {}  (upside {} vs {})  [{}]".format(
                    "SUPPRESSED" if best >= t else "fires",
                    best,
                    d["upside"]["from"],
                    d["upside"]["to"],
                    d["upside"]["win_upside"],
                    d["upside"]["lose_upside"],
                    os.path.basename(d["log"])[:40],
                )
            )


if __name__ == "__main__":
    main()
