"""The checker's output must be byte-identical across PYTHONHASHSEED values.

Gate 7 was denied in part because its own coverage number was not
reproducible: `_backfill_revealed_knowledge` iterated a SET of revealed move
ids under the engine's hard 4-slot cap, so which moves survived the cap -- and
therefore whether the turn's actual move parsed, and therefore the
turns-checked/skipped split -- depended on Python's per-process string hash
seed.  A freeze certificate states corpus counts as re-derivable facts; a
number that moves between runs of an unchanged tree cannot be certified.

Two layers of pinning:

* `TestBackfillMoveOrder` pins the ordering function directly -- it is a
  strict total order (no hash-order tie-break left in it) and it prefers the
  moves a given snapshot may actually need.
* `TestHashSeedByteIdentity` runs the real end-to-end driver
  (`check_replays.py`) over a fixed game under several PYTHONHASHSEED values
  in SUBPROCESSES -- the seed is read at interpreter start, so it cannot be
  exercised in-process -- and asserts byte-identical stdout.

The corpus game used is synth44042, the reproducer recorded in the gate's
`g6_hashseed_nondeterminism.json`: before the fix it reported `checked=18
skipped=16` at seeds 0/1/3 and `checked=19 skipped=15` at seeds 2/4.  That is
the CONTROL for this test -- it is known to have been able to fail.  If the
corpus is not present the end-to-end case skips, so the ordering unit test
below is what always runs.
"""

import os
import subprocess
import sys
import unittest

from fp.replay.checker import _backfill_move_order

_FP_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CORPUS = os.path.join(os.path.dirname(_FP_ROOT), "synthetic-corpus")
_GAME = os.path.join(
    _CORPUS, "battle-gen9randombattle-synth44042_synthopp.log"
)
_SEEDS = ("0", "1", "2", "3", "4")


class TestBackfillMoveOrder(unittest.TestCase):
    def test_empty_and_missing(self):
        self.assertEqual(_backfill_move_order(None, 5), [])
        self.assertEqual(_backfill_move_order({}, 5), [])

    def test_future_first_use_comes_first_nearest_first(self):
        first_use = {"bravebird": 12, "uturn": 30, "willowisp": 20}
        self.assertEqual(
            _backfill_move_order(first_use, 10),
            ["bravebird", "willowisp", "uturn"],
        )

    def test_past_uses_rank_after_future_uses_most_recent_first(self):
        # snapshot 20: 22 and 25 are still ahead of it (nearest first), 18 and 3
        # are behind it and rank last, most-recent first
        first_use = {"a": 3, "b": 25, "c": 18, "d": 22}
        self.assertEqual(_backfill_move_order(first_use, 20), ["d", "b", "c", "a"])

    def test_a_move_first_used_ON_the_snapshot_turn_is_top_priority(self):
        # the backfill exists precisely so a FIRST-USE move parses: the move
        # used on this very turn must never be the one the 4-slot cap drops
        first_use = {"x": 7, "y": 7, "z": 1}
        self.assertEqual(_backfill_move_order(first_use, 7)[:2], ["x", "y"])

    def test_order_is_total_no_hash_tiebreak(self):
        # equal first-use turns are broken by move id, not by set iteration
        first_use = {"zzz": 4, "aaa": 4, "mmm": 4}
        self.assertEqual(
            _backfill_move_order(first_use, 1), ["aaa", "mmm", "zzz"]
        )

    def test_permuting_the_input_dict_cannot_change_the_result(self):
        pairs = [("bravebird", 12), ("uturn", 30), ("willowisp", 20), ("roost", 4)]
        base = _backfill_move_order(dict(pairs), 10)
        for rot in range(1, len(pairs)):
            perm = dict(pairs[rot:] + pairs[:rot])
            self.assertEqual(_backfill_move_order(perm, 10), base)


def _run_checker(seed: str, control: bool = False) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONHASHSEED"] = seed
    env.pop("FP_CONTROL_UNSORTED_BACKFILL", None)
    if control:
        env["FP_CONTROL_UNSORTED_BACKFILL"] = "1"
    return subprocess.run(
        [
            sys.executable,
            "check_replays.py",
            _GAME,
            "--teams-dir",
            _CORPUS,
            "--by-turn",
        ],
        cwd=_FP_ROOT,
        env=env,
        capture_output=True,
    )


@unittest.skipUnless(os.path.exists(_GAME), "synthetic corpus not present")
class TestHashSeedByteIdentity(unittest.TestCase):
    def test_stdout_is_byte_identical_across_hash_seeds(self):
        runs = {s: _run_checker(s) for s in _SEEDS}
        for seed, proc in runs.items():
            self.assertEqual(
                proc.returncode,
                0,
                "checker failed under PYTHONHASHSEED={}: {}".format(
                    seed, proc.stderr.decode()[-2000:]
                ),
            )
        baseline = runs[_SEEDS[0]].stdout
        # the control: this game's line is the one that used to move
        self.assertIn(b"checked=", baseline)
        for seed in _SEEDS[1:]:
            self.assertEqual(
                runs[seed].stdout,
                baseline,
                "checker output differs between PYTHONHASHSEED={} and {}".format(
                    _SEEDS[0], seed
                ),
            )

    def test_control_the_unsorted_traversal_really_does_diverge(self):
        """The probe must be able to fail, or the PASS above means nothing.

        Runs the identical command with the pre-fix hash-order traversal
        restored and asserts the outputs DISAGREE across seeds.  A failure here
        does not mean the ordering is unnecessary -- it means synth44042 is no
        longer a >4-revealed-move reproducer and a fresh one must be found
        before the determinism claim can be certified again."""
        outs = {s: _run_checker(s, control=True).stdout for s in _SEEDS}
        self.assertGreater(
            len(set(outs.values())),
            1,
            "control did not diverge: the reproducer went stale, so the "
            "determinism PASS above is unsupported",
        )


if __name__ == "__main__":
    unittest.main()
