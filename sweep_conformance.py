"""Parallel conformance sweep of the fresh 10k holdout corpus.

Runs check_replays.py at --damage-tolerance 0 with FP_MEMBERSHIP_REPLAY=1
(exact-damage membership check) over units of games, one process per unit,
and aggregates every finding into one JSON.

Usage: sweep_holdout2.py <corpus_dir> <out_dir> [n_workers] [unit_size]
"""

import concurrent.futures as cf
import glob
import json
import os
import subprocess
import sys

FP = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable


def run_unit(args):
    idx, games, corpus, out_dir = args
    jf = os.path.join(out_dir, f"unit_{idx:04d}.findings.json")
    done = os.path.join(out_dir, f"unit_{idx:04d}.done")
    if os.path.exists(done):
        return idx, "cached"
    env = dict(os.environ, FP_MEMBERSHIP_REPLAY="1")
    cmd = [PY, "check_replays.py", *games, "--teams-dir", corpus,
           "--damage-tolerance", "0", "--quiet", "--json", jf]
    p = subprocess.run(cmd, cwd=FP, env=env, capture_output=True, text=True, timeout=7200)
    with open(os.path.join(out_dir, f"unit_{idx:04d}.out"), "w") as fh:
        fh.write(p.stdout[-20000:])
    if p.returncode != 0 or p.stderr:
        with open(os.path.join(out_dir, f"unit_{idx:04d}.err"), "w") as fh:
            fh.write(p.stderr[-20000:])
    open(done, "w").close()
    return idx, f"rc={p.returncode}"


def main():
    corpus = sys.argv[1]
    out_dir = sys.argv[2]
    workers = int(sys.argv[3]) if len(sys.argv) > 3 else 8
    unit = int(sys.argv[4]) if len(sys.argv) > 4 else 100
    os.makedirs(out_dir, exist_ok=True)
    logs = sorted(glob.glob(os.path.join(corpus, "battle-gen9randombattle-synth*.log")))
    print(f"{len(logs)} games, unit={unit}, workers={workers}", flush=True)
    tasks = [(i, logs[i * unit:(i + 1) * unit], corpus, out_dir)
             for i in range((len(logs) + unit - 1) // unit)]
    n = 0
    with cf.ProcessPoolExecutor(max_workers=workers) as ex:
        for idx, status in ex.map(run_unit, tasks):
            n += 1
            if n % 10 == 0:
                print(f"units: {n}/{len(tasks)}", flush=True)
    print("SWEEP COMPLETE", flush=True)


if __name__ == "__main__":
    main()
