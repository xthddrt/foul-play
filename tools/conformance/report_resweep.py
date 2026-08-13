#!/usr/bin/env python
"""Turn a resweep's findings into (a) the survivor report and (b) the manifest
for the NEXT pass.

    python report_resweep.py <findings.json> <resweep.log> <OUT.md> <PASS_N.tsv>

The manifest carries exactly the games with >=1 surviving finding of any kind,
in the same column shape as ISSUE_GAMES.tsv, so the next pass is a drop-in
(`ISSUES=<PASS_N.tsv> python resweep_issue_games.py`).

Findings are also bucketed into the five mechanic clusters the fix wave expects.
Anything that matches none of them lands in `unclassified` and is reported
FIRST and loudly: unclassified volume is the signal that the corrected harness
still attributes something the ground-truth method did not predict.
"""
import collections
import json
import os
import re
import sys

FINDINGS, LOG, OUT_MD, OUT_TSV = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
LABEL = os.environ.get("PASS_LABEL", "pass 1")
HEADER = os.environ.get("PASS_HEADER",
    "Engine wheel unchanged from the corpus sweep; only "
    "`fp/replay/damage_membership.py` moved. So every row below is what the "
    "corrected checker STILL attributes to the engine.")

# (cluster, predicate keywords) -- matched against message + both move slots
CLUSTERS = [
    ("booster_energy_cloud_nine", ("boosterenergy", "cloudnine", "airlock",
                                   "protosynthesis", "quarkdrive")),
    ("cursed_body_sleep_talk", ("cursedbody", "sleeptalk")),
    ("beat_up_substitute", ("beatup",)),
    ("heal_block_psychic_noise", ("healblock", "psychicnoise")),
    ("switchin_update_timing", ("switchin", "onswitch", "updatetiming",
                                "regenerator", "naturalcure")),
]
_HP = re.compile(r"\d+/\d+")
_NUM = re.compile(r"\b\d+\b")
_DMG_MONS = re.compile(r"\bon \S+ from \S+ (\S+) \(crit=")


def blob(f):
    """Message + both move slots, lowercased AND stripped of non-alphanumerics.

    The strip matters: the protocol spells the item "Booster Energy" with a
    space while the move/ability keys are spaceless (`boosterenergy`), so a raw
    substring test silently missed every Booster Energy item finding and parked
    them in `unclassified` -- i.e. it manufactured exactly the 'outside the
    known clusters' signal this report exists to detect."""
    raw = "{} {} {}".format(f.get("message", "") or "",
                            f.get("user_move", "") or "",
                            f.get("opp_move", "") or "").lower()
    return re.sub(r"[^a-z0-9]", "", raw)


def cluster(f):
    b = blob(f)
    for name, keys in CLUSTERS:
        if any(k in b for k in keys):
            return name
    return "unclassified"


def signature(f):
    msg = f.get("message", "") or ""
    m = _DMG_MONS.search(msg)
    if m:
        msg = _DMG_MONS.sub("on <mon> from <mon> {} (crit=".format(m.group(1)), msg)
    msg = _NUM.sub("N", _HP.sub("N/N", msg))
    return (f.get("category", "?"), re.sub(r"\s+", " ", msg).strip()[:160])


def identity(f):
    g = f.get("game", "") or ""
    persp = ""
    for suf in (".p1.log.gz", ".p2.log.gz", ".p1.log", ".p2.log"):
        if g.endswith(suf):
            persp = "p1" if ".p1" in suf else "p2"
            g = g[: -len(suf)]
            break
    bits = g.split("~")
    return bits[0], "/".join(bits[1:]), persp


def main():
    findings = json.load(open(FINDINGS))
    log = open(LOG, errors="replace").read()
    swept = re.search(r"RESWEEP: (\d+) games, (\d+) logs replayed, (\d+)s", log)
    dmg = collections.Counter()
    for m in re.finditer(r"in-scope damage\s+(\d+) \(member (\d+)\)", log):
        dmg["in_scope"] += int(m.group(1))
        dmg["member"] += int(m.group(2))
    dv = re.search(r"damage diverged\s+(\d+)", log)
    diverged = int(dv.group(1)) if dv else 0

    hard = [f for f in findings if f.get("severity") == "hard"]
    soft = [f for f in findings if f.get("severity") != "hard"]

    # ---- per-game rollup (the next pass's manifest) ----------------------
    games = collections.OrderedDict()
    for f in findings:
        box, gid, persp = identity(f)
        g = games.setdefault((box, gid), {"fam": collections.Counter(), "hard": 0,
                                          "soft": 0, "damage": 0, "n": 0,
                                          "persp": set(), "sample": None})
        g["fam"][cluster(f)] += 1
        g["n"] += 1
        g["hard" if f.get("severity") == "hard" else "soft"] += 1
        if f.get("category") == "damage":
            g["damage"] += 1
        if persp:
            g["persp"].add(persp)
        if g["sample"] is None:
            g["sample"] = (f.get("turn", ""), (f.get("message", "") or "")[:160])
    with open(OUT_TSV, "w") as fh:
        fh.write("box\tgame_id\tfamilies\tn_hard\tn_soft\tn_damage\tn_findings\t"
                 "perspectives\tsample_turn\tsample_message\n")
        for (box, gid), g in games.items():
            t, m = g["sample"]
            fh.write("{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\n".format(
                box, gid, ",".join(k for k, _ in g["fam"].most_common()),
                g["hard"], g["soft"], g["damage"], g["n"],
                "+".join(sorted(g["persp"])) or "-", t, m.replace("\t", " ")))

    # ---- survivor report --------------------------------------------------
    cl = collections.Counter()
    cl_hard = collections.Counter()
    cl_games = collections.defaultdict(set)
    for f in findings:
        c = cluster(f)
        cl[c] += 1
        cl_hard[c] += f.get("severity") == "hard"
        cl_games[c].add(identity(f)[:2])

    L = []
    A = L.append
    A("# Resweep {} -- survivors\n".format(LABEL))
    A(HEADER + "\n")
    A("| metric | {} | corpus sweep (before) |".format(LABEL))
    A("|---|---|---|")
    A("| games resswept | {} | 1055145 |".format(swept.group(1) if swept else "?"))
    A("| perspective logs | {} | 2110290 |".format(swept.group(2) if swept else "?"))
    A("| hard findings | {} | 5125 |".format(len(hard)))
    A("| soft findings | {} | 1639 |".format(len(soft)))
    A("| damage diverged | {} | 4478 |".format(diverged))
    A("| games with >=1 finding | {} | 4068 |".format(len(games)))
    A("")
    # Fold p1/p2 mirrors: the SAME engine event is reported once per perspective
    # log, so the raw finding count roughly doubles every real defect. The
    # distinct-event count is the number a fix agent actually has to chase.
    def event_key(f):
        g = identity(f)
        msg = re.sub(r"\bon (user|opp)\b", "on <side>",
                     (f.get("message") or ""))
        mv = tuple(sorted([f.get("user_move", "") or "",
                           f.get("opp_move", "") or ""]))
        return (g[0], g[1], f.get("turn"), f.get("category"),
                _NUM.sub("N", msg), mv)

    ev = {}
    for f in findings:
        ev.setdefault(event_key(f), f)
    ev_hard = sum(1 for v in ev.values() if v.get("severity") == "hard")
    A("**Perspective-folded: {} distinct engine events ({} hard, {} soft).** "
      "Each event is reported once per perspective log, so the raw counts above "
      "roughly double every real defect.\n".format(
          len(ev), ev_hard, len(ev) - ev_hard))
    A("## by mechanic cluster\n")
    A("| cluster | findings | of which hard | games |")
    A("|---|---|---|---|")
    for c, n in cl.most_common():
        A("| {} | {} | {} | {} |".format(c, n, cl_hard[c], len(cl_games[c])))
    A("")
    if cl["unclassified"]:
        A("> **{} findings ({} hard, {} games) matched none of the five expected "
          "clusters** -- see the signature table for what they are.\n".format(
              cl["unclassified"], cl_hard["unclassified"],
              len(cl_games["unclassified"])))

    # what the unclassified residue actually is, grouped by the move pair that
    # should have caused it -- this is the "is anything outside the known
    # clusters showing up in VOLUME?" question, answered on distinct events
    sub = collections.Counter()
    for k, f in ev.items():
        if cluster(f) != "unclassified":
            continue
        mv = [m for m in (f.get("user_move", ""), f.get("opp_move", "")) if m]
        sub[(f.get("category"), "/".join(sorted(mv)) or "-")] += 1
    if sub:
        A("## unclassified residue, by causing move pair (distinct events)\n")
        A("| category | move pair | distinct events |")
        A("|---|---|---|")
        for (cat, mv), n in sub.most_common():
            A("| {} | {} | {} |".format(cat, mv, n))
        A("")

    sigs = collections.defaultdict(list)
    for f in findings:
        sigs[signature(f)].append(f)
    A("## survivor signatures\n")
    if not sigs:
        A("**Zero surviving findings.**")
    else:
        A("| # | category | signature | cluster | hard | soft | games | exemplars (box / game_id) |")
        A("|---|---|---|---|---|---|---|---|")
        for i, (sig, fs) in enumerate(sorted(sigs.items(), key=lambda kv: -len(kv[1])), 1):
            cat, msg = sig
            ex, seen = [], []
            for f in fs:
                b, gid, _ = identity(f)
                k = "{} / {}".format(b, gid)
                if k not in seen:
                    seen.append(k)
            ex = seen[:3]
            h = sum(1 for f in fs if f.get("severity") == "hard")
            A("| {} | {} | {} | {} | {} | {} | {} | {} |".format(
                i, cat, msg, cluster(fs[0]), h, len(fs) - h, len(seen),
                "<br>".join(ex)))
    open(OUT_MD, "w").write("\n".join(L))
    print("\n".join(L[:40]))
    print("\n[wrote {} and {} ({} games)]".format(OUT_MD, OUT_TSV, len(games)))


if __name__ == "__main__":
    main()
