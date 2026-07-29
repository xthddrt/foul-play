#!/usr/bin/env python
"""Run the post-game replay fidelity checker over one or more saved battle logs.

    python check_replays.py logs/battle-gen9randombattleblitz-*.log
    python check_replays.py --hard-only logs/*.log
    python check_replays.py --by-turn logs/battle-....log

For each log it reconstructs every turn from the protocol and checks that the
poke-engine reproduces the observed categorical outcome; it prints the findings
(engine-vs-reality mismatches) grouped by category, plus coverage stats.
"""

import argparse
import glob
import os
import sys
from collections import Counter

from fp import hp_certificate
from fp.replay import check_log
from fp.replay import checker
from fp.replay.comparator import Severity

# which `skipsub_` family belongs under which top-level `skipped_` reason.
# A reason absent here has no sub-classification (it is already atomic).
SKIP_SUB_PREFIX = {
    "skipped_no_active": "skipsub_no_active_",
    "skipped_unnamable_action": "skipsub_unnamable_",
    "skipped_engine_build_failed": "skipsub_build_",
}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("logs", nargs="+", help="log file paths or globs")
    ap.add_argument(
        "--hard-only",
        action="store_true",
        help="show only HARD (stat-independent) findings",
    )
    ap.add_argument(
        "--by-turn",
        action="store_true",
        help="print every finding with its turn/game instead of grouping",
    )
    ap.add_argument(
        "--quiet",
        action="store_true",
        help="suppress per-game coverage lines; only the summary",
    )
    ap.add_argument(
        "--json",
        metavar="OUT",
        help="dump all findings (with per-turn context) to a JSON file for triage",
    )
    ap.add_argument(
        "--teams-dir",
        metavar="DIR",
        help="directory with <game>.teams.json full-knowledge sidecars "
        "(synthetic corpus): overrides both sides with exact sets and enables "
        "the exact-damage membership check",
    )
    ap.add_argument(
        "--damage-tolerance",
        type=int,
        default=0,
        metavar="N",
        help="HP tolerance around the nearest engine roll before a damage "
        "membership miss becomes a HARD finding (default 0)",
    )
    ap.add_argument(
        "--damage-json",
        metavar="OUT",
        help="dump every in-scope damage membership record to a JSON file",
    )
    args = ap.parse_args()

    # Every argument MUST resolve to at least one file.  This used to be checked
    # only in aggregate ("did anything at all match?"), which let a run silently
    # shrink its own denominator: the freeze gate's real-corpus guard invoked this
    # script through a whitespace-splitting shell expansion, so the 14 log files
    # whose names contain spaces ("battle-...-2651480974_fable foul play.log",
    # "..._Soul Dew Latias.log", ...) arrived as fragments that matched nothing --
    # and the run reported a clean "GAMES 147" instead of 161, with no error.  A
    # guard is only worth its denominator, so an argument that matches nothing is
    # now fatal and named.
    paths: list[str] = []
    unmatched: list[str] = []
    for pat in args.logs:
        matched = sorted(glob.glob(pat))
        if not matched and os.path.exists(pat):
            # a literal path containing glob metacharacters ([, ], ?) still works
            matched = [pat]
        if matched:
            paths.extend(matched)
        else:
            unmatched.append(pat)
    if unmatched:
        print(
            "no logs matched {} argument(s): {}".format(
                len(unmatched), ", ".join(repr(u) for u in unmatched)
            ),
            file=sys.stderr,
        )
        print(
            "(a path containing spaces must be quoted -- an unquoted one is split "
            "into fragments that match nothing)",
            file=sys.stderr,
        )
        return 1
    if not paths:
        print("no logs matched", file=sys.stderr)
        return 1
    print("logs matched: {}".format(len(paths)), file=sys.stderr)

    all_findings = []
    all_refusals = []
    damage_records = []
    totals = Counter()
    games = 0
    for path in paths:
        per_game_records = [] if args.teams_dir else None
        try:
            findings, stats = check_log(
                path,
                teams_dir=args.teams_dir,
                damage_collector=per_game_records,
                damage_tolerance=args.damage_tolerance,
            )
        except BaseException as e:  # incl. pyo3 PanicException; never kill the run
            print("ERROR {}: {}".format(path, e), file=sys.stderr)
            continue
        # check_log resets this list per log; drain it so every refusal is
        # reported WITH its game/turn instead of surviving only as a count
        all_refusals.extend(dict(r) for r in hp_certificate.CERTIFICATE_REFUSALS)
        if per_game_records:
            for r in per_game_records:
                r.game = path.split("/")[-1]
            damage_records.extend(per_game_records)
        games += 1
        for k, v in stats.items():
            totals[k] += v
        if args.hard_only:
            findings = [f for f in findings if f.severity is Severity.HARD]
        for f in findings:
            f.game = path  # tag for reporting
        all_findings.extend(findings)
        if not args.quiet:
            hard = sum(1 for f in findings if f.severity is Severity.HARD)
            soft = len(findings) - hard
            print(
                "{:<70} checked={:<4} skipped={:<4} findings={} (hard {}, soft {})".format(
                    path.split("/")[-1],
                    stats["turns_checked"],
                    stats["turns_skipped"],
                    len(findings),
                    hard,
                    soft,
                )
            )

    if args.json:
        import json

        payload = []
        for f in all_findings:
            payload.append(
                {
                    "game": getattr(f, "game", "").split("/")[-1],
                    "turn": f.turn,
                    "severity": f.severity.value,
                    "category": f.category,
                    "message": f.message,
                    "observed": f.observed,
                    "user_move": getattr(f, "user_move", ""),
                    "opp_move": getattr(f, "opp_move", ""),
                    "user_active": getattr(f, "user_active", ""),
                    "opp_active": getattr(f, "opp_active", ""),
                    "branches": getattr(f, "branches", []),
                    "block": getattr(f, "block", []),
                    "state_string": getattr(f, "state_string", ""),
                }
            )
        with open(args.json, "w") as fh:
            json.dump(payload, fh, indent=1)
        print("wrote {} findings to {}".format(len(payload), args.json))

    print("\n" + "=" * 78)
    print(
        "GAMES {} | turns checked {} | skipped {} | reconstruct-errors {}".format(
            games,
            totals["turns_checked"],
            totals["turns_skipped"],
            totals["reconstruct_errors"],
        )
    )
    # named sub-counts of the aggregate above (turns the checker declined to
    # assert on for a specific, attributable reason; and findings the engine's
    # folded damage rolls cannot decide -- see fp.replay.checker)
    for key, label in (
        (
            "hp_certificate_refusals",
            "  exact-HP certificates REFUSED (broken certification chain)",
        ),
        (
            "band_eval_turns",
            "  finding turns re-evaluated over the certified HP band",
        ),
        (
            "band_eval_engine_calls",
            "  extra engine calls spent on band re-evaluation",
        ),
        (
            "band_findings_dropped",
            "  findings dropped (reproduced at another in-band HP)",
        ),
        (
            "unburden_hypothesis_turns",
            "  turns widened with an UNBURDEN ability hypothesis",
        ),
        ("ko_margin_demotions", "  findings softened (KO-boundary margin)"),
        ("phase2_turns", "  deferred turns continued into a phase-2 replay"),
        (
            "phase2_no_observed_switch",
            "  deferred branches NOT continued (no observed switch/revive)",
        ),
        ("phase2_apply_errors", "  phase-2 degraded (state apply failed)"),
        ("phase2_call_errors", "  phase-2 degraded (engine call failed)"),
        ("phase2_errors", "  phase-2 degraded (unexpected error)"),
        (
            "substitute_absorb_refusals",
            "  absorbed-Substitute-hit derivations REFUSED (HP interval widened)",
        ),
    ):
        if totals.get(key):
            print("{}: {}".format(label, totals[key]))
    # ...and the NAMED buckets behind that total, so a refusal is attributable
    # rather than merely countable
    for key in sorted(totals):
        if key.startswith("substitute_absorb_refused_"):
            print(
                "    of which {}: {}".format(
                    key[len("substitute_absorb_refused_") :], totals[key]
                )
            )
    # MEMBERSHIP ASSERT STRENGTH.  A turn whose unnamable side was asserted by
    # membership of its legal action set is NOT the same evidence as a turn with
    # both actions named, and folding the two into one coverage percentage is the
    # exact failure HANDOFF rule 20 names.  So the strength distribution is
    # printed as its own block, and the VACUOUS count -- turns where EVERY
    # candidate reproduced the observation, i.e. the check decided nothing -- is
    # printed on its own line and subtracted in the headline below.
    if totals.get("membership_turns"):
        mt = totals["membership_turns"]
        checked = totals["turns_checked"]
        print("\nMEMBERSHIP ASSERT (turns whose unnamable action was quantified):")
        print(
            "  membership turns {} of {} checked ({:.2f}%) | engine calls spent {}".format(
                mt, checked, 100.0 * mt / max(1, checked),
                totals.get("membership_candidates_tried", 0),
            )
        )
        order = ["point", "full", "partial", "vacuous", "fail"]
        legend = {
            "point": "exact point assert (legal set has ONE member)",
            "full": "full discrimination (exactly 1 of N reproduces)",
            "partial": "partial (>1 but not all reproduce)",
            "vacuous": "VACUOUS -- every candidate reproduces; decides NOTHING",
            "fail": "NO candidate reproduces -> HARD finding",
        }
        seen = 0
        for cls in order:
            n = totals.get("membership_class_" + cls, 0)
            seen += n
            print(
                "    {:<8} {:>7}  ({:5.2f}% of membership turns)  {}".format(
                    cls, n, 100.0 * n / max(1, mt), legend[cls]
                )
            )
        if seen != mt:
            print("    {:<8} {:>7}   <-- classes do NOT sum to membership turns".format(
                "MISMATCH", mt - seen))
        # per-class breakdown by the skip family the turn came from
        for cls in order:
            subs = sorted(
                (k for k in totals if k.startswith("membershipby_" + cls + "_")),
                key=lambda k: -totals[k],
            )
            for sk in subs[:12]:
                print("      {:<8} {:<44} {:>6}".format(
                    cls, sk[len("membershipby_" + cls + "_"):], totals[sk]))
        for key, label in (
            ("membership_fail_nonauthoritative_set",
             "  FAIL turns demoted to SOFT (opponent legal set under-approximated)"),
            ("nonmembership_zero_observed",
             "  ORDINARY (non-membership) checked turns with NO categorical event "
             "observed either -- same emptiness, counted both ways"),
            ("membership_eot_dropped_force_switch",
             "  end-of-turn events dropped (force-switch call has no residual "
             "phase in the engine; NOT asserted)"),
            ("membership_noop_move_residue_dropped",
             "  move-sourced events dropped from no-action blocks "
             "(previous half's residue; NOT asserted)"),
            ("membership_zero_observed",
             "  membership turns with NO categorical event observed at all "
             "(nothing could have failed)"),
            ("membership_bounded_turns",
             "  BOUNDED turns (candidate product capped; NOT exhaustive)"),
            ("membership_candidates_unbuildable",
             "  candidates the engine could not build (excluded from denominator)"),
            ("illusion_soft_capped_turns",
             "  illusion-unresolved turns asserted with findings CAPPED TO SOFT"),
            ("illusion_hard_capped_findings",
             "  findings demoted HARD->SOFT by that cap"),
        ):
            if totals.get(key):
                print("{}: {}".format(label, totals[key]))
        if checker.MEMBERSHIP_FAIL_SAMPLES:
            print("  FAIL turns (no legal action reproduces -- adjudicate per row):")
            for g, t, cls, n, pair, cats in checker.MEMBERSHIP_FAIL_SAMPLES[:40]:
                print("    {} T{} class={} tried={} best_pair={} categories={}".format(
                    g, t, cls, n, pair, cats))
        vac = totals.get("membership_class_vacuous", 0)
        ord_empty = totals.get("nonmembership_zero_observed", 0)
        tot_turns = checked + totals["turns_skipped"]
        print(
            "  HEADLINE: {} of {} resolution blocks asserted ({:.2f}%); "
            "{} ({:.2f}% of checked) are VACUOUS membership checks -> "
            "NON-VACUOUS coverage {:.2f}%".format(
                checked, tot_turns, 100.0 * checked / max(1, tot_turns),
                vac, 100.0 * vac / max(1, checked),
                100.0 * (checked - vac) / max(1, tot_turns),
            )
        )
        # ...and the SAME standard applied to the turns that were already
        # checked before this existed.  The line above subtracts emptiness only
        # from the membership turns, which flatters the pre-existing coverage by
        # holding it to a weaker standard -- exactly the asymmetry rule 20 warns
        # about.  An ordinary checked turn with no categorical event asserted
        # nothing either.
        prev_checked = checked - mt
        print(
            "  LIKE-FOR-LIKE (empty-observation turns removed from BOTH sides): "
            "before {}/{} = {:.2f}%  ->  after {}/{} = {:.2f}%".format(
                prev_checked - ord_empty, tot_turns,
                100.0 * (prev_checked - ord_empty) / max(1, tot_turns),
                (prev_checked - ord_empty) + (mt - vac), tot_turns,
                100.0 * ((prev_checked - ord_empty) + (mt - vac))
                / max(1, tot_turns),
            )
        )

    # SKIP ATTRIBUTION.  Every turns_skipped increment carries a named reason
    # (`skipped_*`, summing to the total) and, where that reason is a family, a
    # sub-bucket (`skipsub_*`, summing to its parent).  An unnamed or unbalanced
    # total is itself a defect and is printed as one.
    skip_reasons = sorted(
        (k for k in totals if k.startswith("skipped_")), key=lambda k: -totals[k]
    )
    if skip_reasons or totals.get("turns_skipped"):
        print("SKIP ATTRIBUTION (turns_skipped {}):".format(totals["turns_skipped"]))
        named = 0
        for key in skip_reasons:
            named += totals[key]
            print(
                "  {:<34} {:>7}  ({:5.2f}% of skips)".format(
                    key[len("skipped_") :],
                    totals[key],
                    100.0 * totals[key] / max(1, totals["turns_skipped"]),
                )
            )
            sub_prefix = SKIP_SUB_PREFIX.get(key)
            subs = (
                sorted(
                    (k for k in totals if k.startswith(sub_prefix)),
                    key=lambda k: -totals[k],
                )
                if sub_prefix
                else []
            )
            sub_named = 0
            for sk in subs:
                sub_named += totals[sk]
                print(
                    "      {:<36} {:>7}".format(sk[len("skipsub_") :], totals[sk])
                )
            if subs and sub_named != totals[key]:
                print(
                    "      {:<36} {:>7}   <-- sub-buckets do NOT sum to parent".format(
                        "MISMATCH", totals[key] - sub_named
                    )
                )
        unnamed = totals["turns_skipped"] - named
        print(
            "  {:<34} {:>7}   <-- MUST BE 0 (an unattributed skip is a taxonomy defect)".format(
                "UNATTRIBUTED", unnamed
            )
        )
        if checker.SKIP_RESIDUAL_SAMPLES:
            print(
                "  residual samples (first {} collected this process, <=3 shown per kind):".format(
                    len(checker.SKIP_RESIDUAL_SAMPLES)
                )
            )
            shown = Counter()
            for kind, detail in checker.SKIP_RESIDUAL_SAMPLES:
                if shown[kind] >= 3:
                    continue
                shown[kind] += 1
                print("      [{}] {}".format(kind, detail[:150]))

    for r in all_refusals:
        print(
            "    REFUSED {} in {} T{}: {} (certified {}/{}{})".format(
                r.get("pokemon"),
                r.get("game"),
                r.get("turn"),
                r.get("reason"),
                r.get("certified_hp"),
                r.get("max_hp"),
                ", shown {}/100 implied {}/100".format(
                    r["shown_pct"], r["implied_pct"]
                )
                if "shown_pct" in r
                else ", bound {}".format(r.get("bound")),
            )
        )
    hard = [f for f in all_findings if f.severity is Severity.HARD]
    soft = [f for f in all_findings if f.severity is Severity.SOFT]
    print("FINDINGS: {} hard, {} soft".format(len(hard), len(soft)))

    if args.by_turn:
        for f in sorted(all_findings, key=lambda x: (x.severity.value, x.category)):
            print(
                "  [{}] {} T{} {}: {}".format(
                    f.severity.value,
                    getattr(f, "game", "?").split("/")[-1][:40],
                    f.turn,
                    f.category,
                    f.message,
                )
            )
    else:
        by_cat = Counter((f.severity.value, f.category) for f in all_findings)
        for (sev, cat), n in sorted(by_cat.items(), key=lambda x: -x[1]):
            print("  {:<6} {:<18} {}".format(sev, cat, n))
        # a few concrete examples of the most common hard category
        if hard:
            top_cat = Counter(f.category for f in hard).most_common(1)[0][0]
            print("\n  examples of hard/{}:".format(top_cat))
            for f in [f for f in hard if f.category == top_cat][:5]:
                print(
                    "    {} T{}: {} | obs: {}".format(
                        getattr(f, "game", "?").split("/")[-1][:36],
                        f.turn,
                        f.message,
                        f.observed[:80],
                    )
                )

    if args.teams_dir:
        _print_damage_report(damage_records, totals, args)
    return 0


def _print_damage_report(records, totals, args):
    print("\n" + "=" * 78)
    print("EXACT-DAMAGE MEMBERSHIP (synthetic full-knowledge corpus)")
    in_scope = totals.get("damage_in_scope", 0)
    print(
        "direct-damage events {} | in-scope {} | member {} | lethal/capped-member {}"
        " | diverged {} | engine-no-damage {}".format(
            totals.get("damage_direct_events", 0),
            in_scope,
            totals.get("damage_member", 0),
            totals.get("damage_lethal_member", 0),
            totals.get("damage_diverged", 0),
            totals.get("damage_engine_no_damage", 0),
        )
    )
    excl = sorted(
        (k, v) for k, v in totals.items() if k.startswith("damage_excluded_")
    )
    if excl:
        print("  excluded (counted, not asserted):")
        for k, v in excl:
            print("    {:<40} {}".format(k[len("damage_excluded_") :], v))
    for k in ("damage_substitute", "damage_unattributed",
              "damage_delayed_move", "damage_self_damage", "damage_target_mismatch",
              "damage_fickle_base_arm", "damage_fickle_doubled_arm",
              "damage_fickle_arm_ambiguous", "damage_fixed_exact_asserted",
              "damage_calc_errors", "damage_state_errors", "damage_turn_errors"):
        if totals.get(k):
            print("    {:<40} {}".format(k[len("damage_") :], totals[k]))

    nonlethal = [r for r in records if not r.lethal and r.status != "engine_no_damage"]
    exact = sum(1 for r in nonlethal if r.member)
    if nonlethal:
        print(
            "\n  non-lethal in-scope: {} | exact-member: {} ({:.2%})".format(
                len(nonlethal), exact, exact / len(nonlethal)
            )
        )
        hist = Counter(r.nearest_delta for r in nonlethal)
        print("  observed-minus-nearest-roll histogram:")
        for d in sorted(hist):
            print("    {:+3d} HP: {:>6}  {}".format(d, hist[d], "#" * min(60, hist[d])))
    lethal = [r for r in records if r.lethal]
    lethal_bad = [r for r in lethal if not r.member]
    if lethal:
        print(
            "  lethal/capped in-scope: {} | lower-bound satisfied: {}".format(
                len(lethal), len(lethal) - len(lethal_bad)
            )
        )

    divergent = sorted(
        (r for r in records if not r.member),
        key=lambda r: -abs(r.nearest_delta if r.nearest_delta is not None else 0),
    )
    if divergent:
        print("\n  top divergences (> nearest roll):")
        for r in divergent[:10]:
            print(
                "    {} T{}: {} {} -> {} crit={} obs_delta={} nearest{:+d} max_roll={}".format(
                    r.game[:36],
                    r.turn,
                    r.attacker,
                    r.move,
                    r.defender,
                    r.crit,
                    r.observed_delta,
                    r.nearest_delta,
                    r.max_roll,
                )
            )

    if args.damage_json:
        import json as _json
        from dataclasses import asdict

        with open(args.damage_json, "w") as fh:
            _json.dump([asdict(r) for r in records], fh, indent=1)
        print("\n  wrote {} damage records to {}".format(len(records), args.damage_json))


if __name__ == "__main__":
    sys.exit(main())
