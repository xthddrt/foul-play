#!/usr/bin/env python
"""Build the issue-game manifest from the v7 corpus_1m sweep shard records.

Every game carrying >=1 finding of any kind gets one row. Damage divergences
need no separate treatment: the shard-level `damage.diverged` total (4478)
equals the count of damage-category findings, so every divergence is already an
attributable per-game finding rather than a counter-only event.

Outputs (local paths given on argv, both also uploaded by the caller):
  ISSUE_GAMES.tsv        box, game_id, families, n_hard, n_soft, n_damage,
                         n_findings, perspectives, sample_turn, sample_message
  FAMILY_EXEMPLARS.tsv   up to N games per family for the fix agents to repro
"""
import collections
import concurrent.futures as cf
import gzip
import io
import json
import os
import sys

import boto3
from botocore.config import Config

BUCKET = os.environ.get("BUCKET", "pokebot-valuenet-389825051723")
PREFIX = os.environ.get("PREFIX", "v7/corpus_1m")
CONF_DIR = os.environ.get("CONF_DIR", "conformance")
PER_FAMILY = int(os.environ.get("PER_FAMILY", "25"))
OUT_ISSUES = sys.argv[1]
OUT_FAMILY = sys.argv[2]

cli = boto3.client("s3", config=Config(max_pool_connections=64,
                                       retries={"max_attempts": 10, "mode": "adaptive"}))


def family(f):
    msg = f.get("message", "")
    mv = "{} {}".format(f.get("user_move", "") or "", f.get("opp_move", "") or "")
    if "struggle" in msg:
        return "struggle_damage"
    if "bellydrum" in mv:
        return "bellydrum_sitrus"
    if "revivalblessing" in mv:
        return "revival_blessing"
    if "[ko-margin]" in msg:
        return "ko_margin_soft"
    if f.get("category") == "damage":
        return "other_damage_rollset"
    return "other_categorical"


def identity(f):
    """flat log basename -> (box, game_id, perspective)"""
    g = f.get("game", "")
    persp = ""
    for suf in (".p1.log.gz", ".p2.log.gz", ".p1.log", ".p2.log"):
        if g.endswith(suf):
            persp = "p1" if ".p1" in suf else "p2"
            g = g[: -len(suf)]
            break
    bits = g.split("~")
    return bits[0], "/".join(bits[1:]), persp


def main():
    keys = []
    p = cli.get_paginator("list_objects_v2")
    for page in p.paginate(Bucket=BUCKET,
                           Prefix="{}/{}/shard_".format(PREFIX, CONF_DIR)):
        keys += [o["Key"] for o in page.get("Contents", ())
                 if o["Key"].endswith(".jsonl.gz")]

    def get(k):
        body = cli.get_object(Bucket=BUCKET, Key=k)["Body"].read()
        with gzip.GzipFile(fileobj=io.BytesIO(body)) as gz:
            return [json.loads(l) for l in gz.read().decode().splitlines() if l.strip()]

    findings = []
    with cf.ThreadPoolExecutor(max_workers=48) as ex:
        for recs in ex.map(get, keys):
            findings.extend(recs[1:])
    print("shards {} | findings {}".format(len(keys), len(findings)), file=sys.stderr)

    games = collections.OrderedDict()
    for f in findings:
        box, gid, persp = identity(f)
        key = (box, gid)
        g = games.setdefault(key, {"fam": collections.Counter(), "hard": 0,
                                   "soft": 0, "damage": 0, "n": 0,
                                   "persp": set(), "sample": None})
        g["fam"][family(f)] += 1
        g["n"] += 1
        if f.get("severity") == "hard":
            g["hard"] += 1
        else:
            g["soft"] += 1
        if f.get("category") == "damage":
            g["damage"] += 1
        if persp:
            g["persp"].add(persp)
        if g["sample"] is None:
            g["sample"] = (f.get("turn", ""), (f.get("message", "") or "")[:160])

    with open(OUT_ISSUES, "w") as fh:
        fh.write("box\tgame_id\tfamilies\tn_hard\tn_soft\tn_damage\tn_findings\t"
                 "perspectives\tsample_turn\tsample_message\n")
        for (box, gid), g in games.items():
            fams = ",".join(k for k, _ in g["fam"].most_common())
            t, m = g["sample"]
            fh.write("{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\n".format(
                box, gid, fams, g["hard"], g["soft"], g["damage"], g["n"],
                "+".join(sorted(g["persp"])) or "-", t, m.replace("\t", " ")))
    print("issue games: {}".format(len(games)))

    # per-family exemplars, worst-first so a fix agent gets the densest repro
    by_fam = collections.defaultdict(list)
    for (box, gid), g in games.items():
        for fam in g["fam"]:
            by_fam[fam].append(((box, gid), g))
    with open(OUT_FAMILY, "w") as fh:
        fh.write("family\tbox\tgame_id\tfamily_findings\tn_hard\tn_soft\t"
                 "perspectives\tsample_turn\tsample_message\n")
        for fam in sorted(by_fam, key=lambda k: -len(by_fam[k])):
            rows = sorted(by_fam[fam], key=lambda kv: -kv[1]["fam"][fam])
            for (box, gid), g in rows[:PER_FAMILY]:
                t, m = g["sample"]
                fh.write("{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\t{}\n".format(
                    fam, box, gid, g["fam"][fam], g["hard"], g["soft"],
                    "+".join(sorted(g["persp"])) or "-", t,
                    m.replace("\t", " ")))
            print("  {:<24} {} games (exemplars {})".format(
                fam, len(rows), min(PER_FAMILY, len(rows))))


if __name__ == "__main__":
    main()
