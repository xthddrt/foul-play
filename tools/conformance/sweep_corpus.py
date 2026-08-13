#!/usr/bin/env python
"""Shard-resumable conformance sweep over a REGISTERED corpus (v7 corpus_1m).

Difference from sweep_conformance.py: that driver globs a locally-unpacked
10k synthetic corpus. This one consumes the register (MANIFEST.tsv.gz) and
streams game artifacts from S3, because the v7 corpus is ~1.05M games spread
over 113 boxes and cannot be tarred into one unit.

IDENTITY: a game is (box, game_id) where game_id is the path under out/.
Filename stems are NOT unique across g-dirs, so every local file is renamed
    <box>~<b>~<g>~<stem>.p1.log.gz
and the finding rows carry that flat name -- reversible, collision-free.

Work split is deterministic: this box handles shards where
    shard_index % NUM_BOXES == BOX_INDEX
and STRAGGLER=1 makes it consider every shard (already-present shards are
skipped via S3 HEAD, which is also the salvage mechanism).

Env: BUCKET PREFIX CORPUS_PREFIX SHARD_SIZE BOX_INDEX NUM_BOXES WORKERS
     PERSPECTIVES STRAGGLER FP_DIR
"""

import concurrent.futures as cf
import gzip
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

BUCKET = os.environ.get("BUCKET", "pokebot-valuenet-389825051723")
PREFIX = os.environ.get("PREFIX", "v7/corpus_1m")
CORPUS_PREFIX = os.environ.get("CORPUS_PREFIX", "v7/farm2/boxes")
SHARD_SIZE = int(os.environ.get("SHARD_SIZE", "500"))
BOX_INDEX = int(os.environ.get("BOX_INDEX", "0"))
NUM_BOXES = int(os.environ.get("NUM_BOXES", "1"))
WORKERS = int(os.environ.get("WORKERS", str(max(1, os.cpu_count() - 2))))
PERSPECTIVES = [p for p in os.environ.get("PERSPECTIVES", "p1").split(",") if p]
STRAGGLER = os.environ.get("STRAGGLER", "0") == "1"
FP_DIR = os.environ.get("FP_DIR", "/opt/foul-play")
LIMIT_SHARDS = int(os.environ.get("LIMIT_SHARDS", "0"))  # canary: stop after N
CONF_DIR = os.environ.get("CONF_DIR", "conformance")  # canary writes elsewhere

_CFG = Config(max_pool_connections=64, retries={"max_attempts": 10, "mode": "adaptive"})


def s3():
    return boto3.client("s3", config=_CFG)


DAMAGE_RE = re.compile(
    r"direct-damage events (\d+) \| in-scope (\d+) \| member (\d+) \| "
    r"lethal/capped-member (\d+) \| diverged (\d+) \| engine-no-damage (\d+)"
)
GAMES_RE = re.compile(
    r"GAMES (\d+) \| turns checked (\d+) \| skipped (\d+) \| reconstruct-errors (\d+)"
)


def load_manifest(path):
    """-> list of (box, game_id) in register order."""
    rows = []
    with gzip.open(path, "rt") as fh:
        header = fh.readline()
        assert header.startswith("box\t"), header[:80]
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 3:
                rows.append((parts[0], parts[2]))
    return rows


def flat_name(box, game_id):
    return "{}~{}".format(box, game_id.replace("/", "~"))


def unflatten(name):
    """flat basename (any suffix) -> (box, game_id)"""
    base = name.split("/")[-1]
    for suf in (".p1.log.gz", ".p2.log.gz", ".p1.log", ".p2.log", ".teams.json"):
        if base.endswith(suf):
            base = base[: -len(suf)]
            break
    bits = base.split("~")
    return bits[0], "/".join(bits[1:])


def fetch_game(args):
    cli, box, game_id, dest = args
    key_base = "{}/{}/out/{}".format(CORPUS_PREFIX, box, game_id)
    flat = flat_name(box, game_id)
    got = []
    for suffix in [".teams.json"] + [".{}.log.gz".format(p) for p in PERSPECTIVES]:
        local = os.path.join(dest, flat + suffix)
        cli.download_file(BUCKET, key_base + suffix, local)
        if suffix != ".teams.json":
            got.append(local)
    return got


def run_shard(idx, games):
    """Sweep one shard; return the record dict (does not upload)."""
    t0 = time.time()
    cli = s3()
    tmp = tempfile.mkdtemp(prefix="shard{}_".format(idx), dir=os.environ.get("SCRATCH", "/opt/scratch"))
    rec = {"shard": idx, "games_registered": len(games)}
    try:
        logs = []
        with cf.ThreadPoolExecutor(max_workers=16) as ex:
            for got in ex.map(fetch_game, [(cli, b, g, tmp) for b, g in games]):
                logs.extend(got)
        t_dl = time.time() - t0

        jf = os.path.join(tmp, "findings.json")
        cmd = [sys.executable, "check_replays.py", *logs, "--teams-dir", tmp,
               "--damage-tolerance", "0", "--quiet", "--json", jf]
        env = dict(os.environ, FP_MEMBERSHIP_REPLAY="1")
        p = subprocess.run(cmd, cwd=FP_DIR, env=env, capture_output=True,
                           text=True, timeout=14400)
        out, err = p.stdout, p.stderr

        findings = []
        if os.path.exists(jf):
            with open(jf) as fh:
                findings = json.load(fh)

        m = GAMES_RE.search(out)
        rec["logs_swept"] = int(m.group(1)) if m else 0
        rec["turns_checked"] = int(m.group(2)) if m else 0
        rec["turns_skipped"] = int(m.group(3)) if m else 0
        rec["reconstruct_errors"] = int(m.group(4)) if m else 0
        # a game counts as SWEPT when every perspective log we asked for replayed
        rec["games_swept"] = rec["logs_swept"] // max(1, len(PERSPECTIVES))
        dmg = {}
        for m in DAMAGE_RE.finditer(out):
            for k, v in zip(("events", "in_scope", "member", "lethal", "diverged",
                             "no_damage"), map(int, m.groups())):
                dmg[k] = dmg.get(k, 0) + v
        rec["damage"] = dmg
        rec["rc"] = p.returncode
        rec["n_findings"] = len(findings)
        rec["hard"] = sum(1 for f in findings if f.get("severity") == "hard")
        rec["soft"] = rec["n_findings"] - rec["hard"]
        errlines = [l for l in err.splitlines()
                    if l.strip() and not l.startswith("logs matched")]
        rec["stderr_lines"] = len(errlines)
        rec["stderr_sample"] = errlines[:20]
        rec["download_s"] = round(t_dl, 1)
        rec["elapsed_s"] = round(time.time() - t0, 1)
        return rec, findings
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def shard_key(idx):
    return "{}/{}/shard_{:04d}.jsonl.gz".format(PREFIX, CONF_DIR, idx)


def exists(cli, key):
    try:
        cli.head_object(Bucket=BUCKET, Key=key)
        return True
    except ClientError:
        return False


def worker(idx_games):
    idx, games = idx_games
    cli = s3()
    key = shard_key(idx)
    if exists(cli, key):
        return idx, "cached", 0
    try:
        rec, findings = run_shard(idx, games)
    except Exception:
        buf = io.BytesIO()
        with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
            gz.write(json.dumps({"shard": idx, "FAILED": True,
                                 "traceback": traceback.format_exc()}).encode())
        cli.put_object(Bucket=BUCKET, Key="{}/{}/FAILED_shard_{:04d}".format(
            PREFIX, CONF_DIR, idx), Body=buf.getvalue())
        return idx, "FAILED", 0
    rec["games"] = ["{}\t{}".format(b, g) for b, g in games]
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb") as gz:
        gz.write((json.dumps(rec) + "\n").encode())
        for f in findings:
            gz.write((json.dumps(f) + "\n").encode())
    cli.put_object(Bucket=BUCKET, Key=key, Body=buf.getvalue())
    return idx, "ok", rec["games_swept"]


def main():
    man = os.environ.get("MANIFEST", "/opt/MANIFEST.tsv.gz")
    rows = load_manifest(man)
    n_shards = (len(rows) + SHARD_SIZE - 1) // SHARD_SIZE
    shards = [(i, rows[i * SHARD_SIZE:(i + 1) * SHARD_SIZE]) for i in range(n_shards)]
    mine = [s for s in shards if STRAGGLER or s[0] % NUM_BOXES == BOX_INDEX]
    if LIMIT_SHARDS:
        mine = mine[:LIMIT_SHARDS]
    print("manifest rows {} | shards {} | mine {} | workers {} | persp {}".format(
        len(rows), n_shards, len(mine), WORKERS, PERSPECTIVES), flush=True)

    os.makedirs(os.environ.get("SCRATCH", "/opt/scratch"), exist_ok=True)
    t0 = time.time()
    stats = {"done": 0, "swept": 0, "failed": 0, "cached": 0, "hb": 0.0}
    cli = s3()

    def run_pass(batch, label):
        with cf.ProcessPoolExecutor(max_workers=WORKERS) as ex:
            for idx, status, n in ex.map(worker, batch):
                stats["done"] += 1
                stats["swept"] += n
                stats["failed"] += status == "FAILED"
                stats["cached"] += status == "cached"
                now = time.time()
                if now - stats["hb"] > 300:
                    el = now - t0
                    msg = ("box {} [{}] shards {} (cached {} failed {}) games {} "
                           "elapsed {:.0f}s rate {:.1f} games/s\n".format(
                               BOX_INDEX, label, stats["done"], stats["cached"],
                               stats["failed"], stats["swept"], el,
                               stats["swept"] / max(1e-9, el)))
                    print(msg, end="", flush=True)
                    try:
                        cli.put_object(
                            Bucket=BUCKET,
                            Key="{}/{}/progress/box_{:03d}.txt".format(
                                PREFIX, CONF_DIR, BOX_INDEX),
                            Body=msg.encode())
                    except Exception:
                        pass
                    stats["hb"] = now

    run_pass(mine, "own")

    # SELF-HEAL. A reclaimed box leaves its modulo class half-swept; rather than
    # needing a second wave, every surviving box sweeps whatever is still missing
    # once its own share is done. Shard presence is an S3 HEAD, so two boxes that
    # pick the same straggler simply write the same object twice -- idempotent.
    if not LIMIT_SHARDS and os.environ.get("SELF_HEAL", "1") == "1":
        import random
        for rnd in range(1, 4):
            present = set()
            p = cli.get_paginator("list_objects_v2")
            for page in p.paginate(Bucket=BUCKET,
                                   Prefix="{}/{}/shard_".format(PREFIX, CONF_DIR)):
                for o in page.get("Contents", ()):
                    m = re.search(r"shard_(\d+)\.jsonl\.gz$", o["Key"])
                    if m:
                        present.add(int(m.group(1)))
            missing = [s for s in shards if s[0] not in present]
            print("self-heal round {}: {} shards still missing".format(
                rnd, len(missing)), flush=True)
            if not missing:
                break
            random.shuffle(missing)   # different boxes start at different ends
            run_pass(missing, "heal{}".format(rnd))

    el = time.time() - t0
    print("SHARDS COMPLETE shards={} games={} failed={} elapsed={:.0f}s "
          "rate={:.2f} games/s".format(stats["done"], stats["swept"],
                                       stats["failed"], el,
                                       stats["swept"] / max(1e-9, el)), flush=True)


if __name__ == "__main__":
    main()
