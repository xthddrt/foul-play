"""Aggregate the conformance sweep: checker findings AND stderr warnings.

The findings JSON holds engine-vs-PS mismatches. The .err files hold
battle-tracker warnings (5-move Pokemon, missing volatiles, ...) which are not
engine mismatches but ARE "noted discrepancies", so they are reported too.
"""

import collections
import glob
import json
import re
import sys

out_dir = sys.argv[1]

rows = []
for f in sorted(glob.glob(f"{out_dir}/unit_*.findings.json")):
    try:
        d = json.load(open(f))
    except Exception as e:
        print(f"UNREADABLE {f}: {e}")
        continue
    rows.extend(d if isinstance(d, list) else d.get("findings", []))

hard = [r for r in rows if r.get("hard") or r.get("severity") == "hard"]
soft = [r for r in rows if r not in hard]
print("=" * 70)
print(f"ENGINE FINDINGS: {len(hard)} hard, {len(soft)} soft  (total {len(rows)})")
print("=" * 70)
if rows:
    for k, v in collections.Counter(r.get("category", "?") for r in rows).most_common():
        print(f"  {k}: {v}")
    print("\nrows:")
    for r in (hard + soft)[:80]:
        g = r.get("game") or r.get("log", "?")
        detail = str(r.get("detail") or r.get("message", ""))[:140]
        print(f"  [{r.get('category','?')}] {g} t{r.get('turn','?')}: {detail}")

# ---- damage membership: parse the per-unit stdout summaries ----
tot = collections.Counter()
hist = collections.Counter()
for f in sorted(glob.glob(f"{out_dir}/unit_*.out")):
    txt = open(f, errors="replace").read()
    for m in re.finditer(
        r"direct-damage events (\d+) \| in-scope (\d+) \| member (\d+) \| "
        r"lethal/capped-member (\d+) \| diverged (\d+) \| engine-no-damage (\d+)", txt):
        for k, v in zip(("events", "in_scope", "member", "lethal", "diverged", "no_damage"),
                        map(int, m.groups())):
            tot[k] += v
    for m in re.finditer(r"([+-]\d+) HP:\s+(\d+)", txt):
        hist[int(m.group(1))] += int(m.group(2))

print("\n" + "=" * 70)
print("EXACT-DAMAGE MEMBERSHIP (tolerance 0)")
print("=" * 70)
for k in ("events", "in_scope", "member", "lethal", "diverged", "no_damage"):
    print(f"  {k}: {tot[k]}")
if tot["in_scope"]:
    print(f"  exact-member rate: {100.0 * tot['member'] / tot['in_scope']:.4f}%")
print("  observed-minus-nearest-roll histogram:")
for off in sorted(hist):
    flag = "   <-- NONZERO OFFSET" if off != 0 else ""
    print(f"    {off:+d} HP: {hist[off]}{flag}")

# ---- tracker warnings from stderr ----
warn = collections.Counter()
examples = {}
for f in sorted(glob.glob(f"{out_dir}/unit_*.err")):
    for line in open(f, errors="replace"):
        line = line.strip()
        if not line or line.startswith("logs matched"):
            continue
        key = re.sub(r"\b\d+\b", "N", line)
        key = re.sub(r"'[^']*'", "'X'", key)
        key = re.sub(r"\[[^\]]*\]", "[...]", key)
        key = key[:110]
        warn[key] += 1
        examples.setdefault(key, line[:180])

print("\n" + "=" * 70)
print(f"TRACKER WARNINGS (stderr): {sum(warn.values())} lines, {len(warn)} classes")
print("=" * 70)
for k, v in warn.most_common(25):
    print(f"  {v:6d}x  {examples[k]}")
