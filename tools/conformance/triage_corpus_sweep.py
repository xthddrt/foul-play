#!/usr/bin/env python
"""Aggregate the shard results of the v7 corpus_1m sweep and dedupe findings
into root-cause signatures.

    python triage_corpus_sweep.py <CONF_DIR> [out.md]

Counts come from the shard objects themselves (never a running counter):
games_swept is summed over every shard record present in S3.
"""
import collections
import gzip
import io
import json
import os
import re
import sys

import boto3
from botocore.config import Config

BUCKET = os.environ.get("BUCKET", "pokebot-valuenet-389825051723")
PREFIX = os.environ.get("PREFIX", "v7/corpus_1m")
CONF_DIR = sys.argv[1] if len(sys.argv) > 1 else "conformance"
OUT = sys.argv[2] if len(sys.argv) > 2 else None

cli = boto3.client("s3", config=Config(max_pool_connections=64,
                                       retries={"max_attempts": 10, "mode": "adaptive"}))


def iter_shards():
    p = cli.get_paginator("list_objects_v2")
    keys = []
    for page in p.paginate(Bucket=BUCKET, Prefix="{}/{}/shard_".format(PREFIX, CONF_DIR)):
        for o in page.get("Contents", ()):
            if o["Key"].endswith(".jsonl.gz"):
                keys.append(o["Key"])
    import concurrent.futures as cf

    def get(k):
        body = cli.get_object(Bucket=BUCKET, Key=k)["Body"].read()
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as gz:
            return [json.loads(l) for l in gz.read().decode().splitlines() if l.strip()]

    with cf.ThreadPoolExecutor(max_workers=48) as ex:
        for recs in ex.map(get, keys):
            yield recs
    print("shards read: {}".format(len(keys)), file=sys.stderr)


# --- signature derivation ---------------------------------------------------
_HP = re.compile(r"\d+/\d+")
_NUM = re.compile(r"\b\d+\b")


_DMG_MONS = re.compile(r"\bon \S+ from \S+ (\S+) \(crit=")


def signature(f):
    """Collapse a finding to a MECHANIC-level signature.

    The species names are the accidental part and the move is the essential one:
    keying on the raw message split 'Struggle damage' into ~40 rows named for
    whichever pair of mons happened to be on the field. So species are erased
    (the attacker's move survives inside the damage template) and the move pair
    is NOT part of the key -- it is reported per group as context instead, since
    for Belly Drum / Revival Blessing the partner move is the varying half."""
    msg = f.get("message", "")
    m = _DMG_MONS.search(msg)
    if m:
        msg = _DMG_MONS.sub("on <mon> from <mon> {} (crit=".format(m.group(1)), msg)
    msg = _HP.sub("N/N", msg)
    msg = _NUM.sub("N", msg)
    msg = re.sub(r"\s+", " ", msg).strip()
    return (f.get("category", "?"), msg[:160])


def main():
    tot = collections.Counter()
    dmg = collections.Counter()
    findings = []
    shard_ids = set()
    for recs in iter_shards():
        head = recs[0]
        if head.get("FAILED"):
            tot["failed_shards"] += 1
            continue
        shard_ids.add(head["shard"])
        for k in ("games_registered", "games_swept", "logs_swept", "turns_checked",
                  "turns_skipped", "reconstruct_errors", "n_findings", "hard", "soft",
                  "stderr_lines"):
            tot[k] += head.get(k, 0)
        for k, v in (head.get("damage") or {}).items():
            dmg[k] += v
        for f in recs[1:]:
            findings.append(f)

    sigs = collections.defaultdict(list)
    for f in findings:
        sigs[signature(f)].append(f)

    lines = []
    A = lines.append
    A("# v7 corpus_1m conformance sweep -- findings\n")
    A("| metric | value |")
    A("|---|---|")
    A("| shards present | {} |".format(len(shard_ids)))
    A("| games registered (shard records) | {} |".format(tot["games_registered"]))
    A("| games swept | {} |".format(tot["games_swept"]))
    A("| perspective logs replayed | {} |".format(tot["logs_swept"]))
    A("| resolution blocks checked | {} |".format(tot["turns_checked"]))
    A("| blocks skipped | {} |".format(tot["turns_skipped"]))
    A("| reconstruct errors | {} |".format(tot["reconstruct_errors"]))
    A("| findings (hard) | {} |".format(tot["hard"]))
    A("| findings (soft) | {} |".format(tot["soft"]))
    A("| tracker warning lines | {} |".format(tot["stderr_lines"]))
    A("| failed shards | {} |".format(tot["failed_shards"]))
    A("")
    A("## exact-damage membership (tolerance 0)\n")
    A("| metric | value |")
    A("|---|---|")
    for k in ("events", "in_scope", "member", "lethal", "diverged", "no_damage"):
        A("| {} | {} |".format(k, dmg[k]))
    if dmg["in_scope"]:
        A("| exact-member rate | {:.4f}% |".format(
            100.0 * dmg["member"] / dmg["in_scope"]))
    A("")
    # a game is ONE game even when both perspective logs flag it: strip the
    # .p1/.p2 suffix before counting, or the dirty count double-counts and the
    # clean percentage reads low
    def game_identity(f):
        g = f.get("game", "")
        for suf in (".p1.log.gz", ".p2.log.gz", ".p1.log", ".p2.log"):
            if g.endswith(suf):
                return g[: -len(suf)]
        return g

    dirty_games = {game_identity(f) for f in findings}
    clean = tot["games_swept"] - len(dirty_games)
    A("**clean games: {} of {} ({:.4f}%)**\n".format(
        clean, tot["games_swept"],
        100.0 * clean / max(1, tot["games_swept"])))

    # ---- root-cause families -------------------------------------------
    def family(f):
        msg = f.get("message", "")
        mv = "{} {}".format(f.get("user_move", "") or "", f.get("opp_move", "") or "")
        if "struggle" in msg:
            return "Struggle damage"
        if "bellydrum" in mv:
            return "Belly Drum / Sitrus"
        if "revivalblessing" in mv:
            return "Revival Blessing"
        if "[ko-margin]" in msg:
            return "KO-margin (soft demotion)"
        if f.get("category") == "damage":
            return "Other damage roll-set"
        return "Other categorical"

    fam = collections.Counter()
    fam_hard = collections.Counter()
    fam_games = collections.defaultdict(set)
    for f in findings:
        k = family(f)
        fam[k] += 1
        fam_hard[k] += f.get("severity") == "hard"
        fam_games[k].add(game_identity(f))
    A("## root-cause families\n")
    A("| family | findings | of which hard | games affected |")
    A("|---|---|---|---|")
    for k, c in fam.most_common():
        A("| {} | {} | {} | {} |".format(k, c, fam_hard[k], len(fam_games[k])))
    A("")

    A("## findings by signature\n")
    if not sigs:
        A("**The corpus is 100% clean: 0 hard, 0 soft, 0 damage divergences.**")
    else:
        A("| # | category | signature | count | games | move context (top) | exemplars (box / game_id) |")
        A("|---|---|---|---|---|---|---|")
        for i, (sig, fs) in enumerate(
                sorted(sigs.items(), key=lambda kv: -len(kv[1])), 1):
            cat, msg = sig
            # exemplars: distinct GAMES, not distinct findings (three hits in one
            # game is one thing to look at, not three)
            ex, seen = [], set()
            for f in fs:
                g = f.get("game", "")
                bits = g.split("~")
                ident = ("{} / {}".format(bits[0], "/".join(bits[1:]).rsplit(".p", 1)[0])
                         if len(bits) >= 2 else g)
                if ident not in seen:
                    seen.add(ident)
                    if len(ex) < 3:
                        ex.append(ident)
            mv = collections.Counter()
            for f in fs:
                for k in ("user_move", "opp_move"):
                    if f.get(k):
                        mv[f[k]] += 1
            ctx = ", ".join("{} ({})".format(m, c) for m, c in mv.most_common(3))
            A("| {} | {} | {} | {} | {} | {} | {} |".format(
                i, cat, msg, len(fs), len(seen), ctx or "-", "<br>".join(ex)))
    text = "\n".join(lines)
    print(text)
    if OUT:
        open(OUT, "w").write(text)


if __name__ == "__main__":
    main()
