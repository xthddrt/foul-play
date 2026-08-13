#!/usr/bin/env python
"""STAGED resweep of exactly the ISSUE_GAMES set, both perspectives.

Verifies a fix wave. Runs LOCALLY (see the choice note in the header comment of
the report): 4,068 games x 2 logs is ~25 min of single-core work, so half the
Mac's cores clear it in ~6-7 min and no cloud box, code pack or wheel publish is
in the loop. The corpus download is ~2.2 GB, cached on disk between runs.

    PYTHON=/path/to/venv/bin/python \
    bash -c '.venv/bin/python tools/conformance/resweep_issue_games.py'

Env:
  ISSUES     path to ISSUE_GAMES.tsv (default: alongside this script)
  GAMEDIR    where to cache downloaded artifacts (default scratch/resweep_games)
  WORKERS    parallel checker processes (default 4 = half of 8 cores)
  PYTHON     interpreter whose venv carries the NEW wheel (default foul-play/.venv)
  CHUNK      games per checker invocation (default 60)

ACCEPTANCE (printed as a verdict): zero hard findings, zero damage divergence.
Every surviving soft finding is listed individually for triage.
"""
import collections
import concurrent.futures as cf
import json
import os
import re
import subprocess
import sys
import time

import boto3
from botocore.config import Config

HERE = os.path.dirname(os.path.abspath(__file__))
FP = os.path.dirname(os.path.dirname(HERE))          # .../foul-play
BUCKET = os.environ.get("BUCKET", "pokebot-valuenet-389825051723")
CORPUS_PREFIX = os.environ.get("CORPUS_PREFIX", "v7/farm2/boxes")
ISSUES = os.environ.get("ISSUES", os.path.join(HERE, "ISSUE_GAMES.tsv"))
SCRATCH = os.environ.get(
    "SCRATCH",
    "/private/tmp/claude-501/-Users-sallyliu-pokemon-fast-bot/"
    "410e0c58-8931-45a0-8e25-e3a8ec37baef/scratchpad")
GAMEDIR = os.environ.get("GAMEDIR", os.path.join(SCRATCH, "resweep_games"))
WORKERS = int(os.environ.get("WORKERS", "4"))
CHUNK = int(os.environ.get("CHUNK", "60"))
PY = os.environ.get("PYTHON", os.path.join(FP, ".venv", "bin", "python"))

DAMAGE_RE = re.compile(
    r"direct-damage events (\d+) \| in-scope (\d+) \| member (\d+) \| "
    r"lethal/capped-member (\d+) \| diverged (\d+) \| engine-no-damage (\d+)")
GAMES_RE = re.compile(r"GAMES (\d+) \| turns checked (\d+) \| skipped (\d+)")

_CFG = Config(max_pool_connections=64,
              retries={"max_attempts": 10, "mode": "adaptive"})


def flat(box, gid):
    return "{}~{}".format(box, gid.replace("/", "~"))


def load_issues():
    rows = []
    with open(ISSUES) as fh:
        head = fh.readline().split("\t")
        assert head[0] == "box", head[:2]
        for line in fh:
            p = line.rstrip("\n").split("\t")
            if len(p) >= 2:
                rows.append((p[0], p[1]))
    return rows


def fetch(args):
    box, gid = args
    cli = boto3.client("s3", config=_CFG)
    base = "{}/{}/out/{}".format(CORPUS_PREFIX, box, gid)
    name = flat(box, gid)
    out = []
    for suf in (".teams.json", ".p1.log.gz", ".p2.log.gz"):
        dst = os.path.join(GAMEDIR, name + suf)
        if not os.path.exists(dst):          # cached across runs
            cli.download_file(BUCKET, base + suf, dst)
        if suf != ".teams.json":
            out.append(dst)
    return out


def run_chunk(logs):
    jf = os.path.join(GAMEDIR, ".findings_{}.json".format(os.getpid()))
    p = subprocess.run(
        [PY, "check_replays.py", *logs, "--teams-dir", GAMEDIR,
         "--damage-tolerance", "0", "--quiet", "--json", jf],
        cwd=FP, env=dict(os.environ, FP_MEMBERSHIP_REPLAY="1"),
        capture_output=True, text=True, timeout=7200)
    fs = []
    if os.path.exists(jf):
        fs = json.load(open(jf))
        os.remove(jf)
    dmg = collections.Counter()
    for m in DAMAGE_RE.finditer(p.stdout):
        for k, v in zip(("events", "in_scope", "member", "lethal", "diverged",
                         "no_damage"), map(int, m.groups())):
            dmg[k] += v
    g = GAMES_RE.search(p.stdout)
    return fs, dmg, (int(g.group(1)) if g else 0), p.stderr


def main():
    rows = load_issues()
    os.makedirs(GAMEDIR, exist_ok=True)
    print("resweeping {} issue games (both perspectives) with {}".format(
        len(rows), PY), flush=True)
    t0 = time.time()

    logs = []
    with cf.ThreadPoolExecutor(max_workers=24) as ex:
        for i, got in enumerate(ex.map(fetch, rows), 1):
            logs.extend(got)
            if i % 500 == 0:
                print("  fetched {}/{}".format(i, len(rows)), flush=True)
    print("fetched {} logs in {:.0f}s".format(len(logs), time.time() - t0), flush=True)

    chunks = [logs[i:i + CHUNK * 2] for i in range(0, len(logs), CHUNK * 2)]
    findings, dmg, swept, errs = [], collections.Counter(), 0, []
    with cf.ProcessPoolExecutor(max_workers=WORKERS) as ex:
        for n, (fs, d, g, err) in enumerate(ex.map(run_chunk, chunks), 1):
            findings.extend(fs)
            dmg.update(d)
            swept += g
            errs += [l for l in err.splitlines()
                     if l.strip() and not l.startswith("logs matched")]
            if n % 10 == 0:
                print("  chunks {}/{}".format(n, len(chunks)), flush=True)

    hard = [f for f in findings if f.get("severity") == "hard"]
    soft = [f for f in findings if f.get("severity") != "hard"]
    el = time.time() - t0
    print("\n" + "=" * 78)
    print("RESWEEP: {} games, {} logs replayed, {:.0f}s".format(
        len(rows), swept, el))
    print("  hard findings      {}".format(len(hard)))
    print("  soft findings      {}".format(len(soft)))
    print("  damage diverged    {}".format(dmg["diverged"]))
    print("  in-scope damage    {} (member {})".format(dmg["in_scope"], dmg["member"]))
    print("  stderr lines       {}".format(len(errs)))
    ok = not hard and not dmg["diverged"]
    print("  VERDICT: {}".format(
        "PASS -- zero hard, zero damage divergence"
        if ok else "FAIL -- fixes incomplete or regressed"))
    if hard:
        print("\nHARD (must be zero):")
        for f in hard[:60]:
            print("  [{}] {} t{}: {}".format(f.get("category"), f.get("game"),
                                             f.get("turn"), f.get("message", "")[:150]))
    if soft:
        print("\nSOFT surviving -- triage each (checker-demotion artifact vs real):")
        by = collections.Counter()
        for f in soft:
            by[(f.get("category"), (f.get("message") or "")[:110])] += 1
        for (cat, msg), c in by.most_common():
            print("  {:>5}x [{}] {}".format(c, cat, msg))
    json.dump(findings, open(os.path.join(SCRATCH, "resweep_findings.json"), "w"),
              indent=1)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
