# HANDOFF — gen9 randombattle bot, state as of 2026-08-02

Written so a fresh session can take over with no context from the previous one.
Read this top to bottom before touching anything.

---

## What this project is

Build the strongest possible gen9 Random Battles (+ Blitz) bot for the Pokémon
Showdown ladder. Only ladder wins matter; losses must be unavoidable.

Two tracks are live right now, running in parallel and independent of each other:

- **Track A — Conformance gate**: prove the engine + replay checker reproduce
  real Pokémon Showdown exactly.
- **Track B — Value network (Step 1)**: replace the hand-crafted `evaluate()`
  with a learned net.

---

## Repos and what they are

| Path | Role | Git |
|---|---|---|
| `/Users/sallyliu/pokemon-fast-bot/poke-engine` | Rust battle engine + MCTS. EDITABLE. | github.com/xthddrt/poke-engine (public fork) |
| `/Users/sallyliu/pokemon-fast-bot/foul-play` | Python bot, battle tracker, replay checker. EDITABLE. | github.com/xthddrt/foul-play (public fork) |
| `/Users/sallyliu/pokemon-fast-bot/valuenet` | Value-net pipeline (generation/encoder/training/cloud). | github.com/xthddrt/valuenet (private) |
| `/Users/sallyliu/pokemon-ai/pokemon-showdown` | **READ-ONLY GROUND TRUTH.** Never edit. | upstream |
| `/Users/sallyliu/pokemon-ai/synthetic-corpus*` | Conformance corpora (see below). | not in git |

Upstream CI workflows were deleted from both forks on purpose (they lint/test
against upstream assumptions and would auto-commit Smogon data nightly).

**`.env` in `pokemon-fast-bot/` holds real Showdown credentials. Never commit it.**

---

## CRITICAL: the wheel-staleness trap

`foul-play/.venv` has a compiled `poke_engine` wheel. **Editing Rust source does
NOT change it.** Sweeping with a stale wheel silently certifies an engine you are
not running — this happened once and nearly invalidated a whole round.

After ANY engine edit:

```bash
export MATURIN_PEP517_ARGS="--features poke-engine/terastallization --no-default-features"
/Users/sallyliu/pokemon-fast-bot/foul-play/.venv/bin/pip3 install --force-reinstall \
  --no-cache-dir /Users/sallyliu/pokemon-fast-bot/poke-engine/poke-engine-py
```

Note `pip3`, not `pip` (the venv has no `pip`). Takes ~5 min. Then verify the
rebuild actually took effect before trusting any result.

---

## TRACK A — Conformance gate

### The acceptance standard (raised by the user 2026-08-02)

**FOUR consecutive 10,000-game sweeps with 0 hard / 0 soft / 0 damage
discrepancies and zero printed warnings**, each on a FRESH corpus (disjoint
seeds), at `--damage-tolerance 0` with `FP_MEMBERSHIP_REPLAY=1`.

**Any fix resets the counter to zero.** All four clean sweeps must come after the
last change. That is 40,000 unseen games consecutively — keep fresh corpora
queued ahead of the loop.

A finding where "the engine is right and the checker over-reports" **still fails
the gate**. The fix is then a CHECKER fix — but only after proving the engine is
right. Never silence a report you cannot prove is false.

### Corpora

| Dir | Seeds | Status |
|---|---|---|
| `synthetic-corpus` | 1–50000 | original training corpus (fixes derived from it; cannot certify) |
| `synthetic-corpus-holdout` | 50001–60000 | old, pre-dates current engine |
| `holdout2`–`holdout16` | 60001–170000 | **all CONSUMED** (rounds 1–7). PRUNED to just the finding games `verify_all.sh` replays — regenerable in full from their seed ranges if ever needed |
| `holdout17`–`holdout20` | 210001–250000 | **gate attempt 3** — fresh, unseen, reserved |

**Prune each corpus right after its findings are diagnosed.** 19 corpora at
~1.5 GB filled a 228 GB disk and silently killed a generation run mid-flight.

Generate more:
```bash
cd /Users/sallyliu/pokemon-ai
tools/node-v22.23.1-darwin-arm64/bin/node tools/gen_corpus.js \
  --count 1250 --start <IDX> --out <DIR> --quiet
```
Resumable; parallelize across 8 disjoint index ranges. ~10 min per 10k games.
**`ls *.log` fails silently at 10k files (argument list too long) — use `find`.**

### Running a sweep

Cloud (preferred, ~15 min, ~$0.50):
```bash
# 1. tar the corpus (*.log + *.teams.json), upload to s3://<bucket>/corpusX.tar.gz
# 2. re-tar+upload code.tar.gz if source changed
# 3. edit + run scratchpad launch_sweepC.sh (change CORPUS_TAR and NAME)
# results land in s3://<bucket>/gate/<NAME>/aggregate.txt
```
Local (~55 min, ties up the Mac):
```bash
cd /Users/sallyliu/pokemon-fast-bot/foul-play
.venv/bin/python sweep_conformance.py <CORPUS_DIR> <OUT_DIR> 6 100
.venv/bin/python aggregate_conformance.py <OUT_DIR>
```

Single game (the authoritative repro):
```bash
cd /Users/sallyliu/pokemon-fast-bot/foul-play
FP_MEMBERSHIP_REPLAY=1 .venv/bin/python check_replays.py \
  <CORPUS>/battle-gen9randombattle-synth<N>_synthopp.log \
  --teams-dir <CORPUS> --damage-tolerance 0 --by-turn
```

### The loop (repeat until 4 clean in a row)

1. Sweep a fresh corpus → aggregate → inventory of findings + warnings.
2. Fan out **Opus** subagents, one per defect cluster, **diagnose-only** (they
   return exact patches; they must not edit — this is not a git repo per-directory
   for isolation purposes and concurrent edits corrupt each other).
3. Apply patches serially (`scratchpad/apply_patches.py` verifies old_string is
   present and unique before writing).
4. `cargo test --features terastallization --no-default-features` — **must be
   1888 passed / 0 failed.**
5. Rebuild the wheel (see trap above).
6. **Verify the round's finding games AND all prior rounds' finding games.** The
   regression guard is mandatory: it caught wave 4 breaking wave 2's Cud Chew fix.
7. Fresh corpus → next gate attempt.

### Progress so far

| Round | Corpus | Findings | Warning lines |
|---|---|---|---|
| 1 | A (holdout2) | 10 | 2,205 |
| 2 | B (holdout3) | 16 | 646 |
| 3 | C (holdout4) | 9 | 107 |
| 4 | D (holdout5) | 12 | 6 |
| 5 | 30k diag (holdout6/7/8) | 15 | 3 |
| 6 | gate9 + 30k diag (holdout10/11/12) | 24 | 3 |
| 7 | gate attempt 2 (holdout13-16, 40k) | 50 | 2 |

**~106 root causes fixed across 11 waves. All 101 finding games verify clean
simultaneously.** Engine suite: 1889 passing.

**Damage has never diverged**: ~2.5M in-scope events across every sweep, one
histogram bucket (+0 HP). The single `diverged` ever recorded was a CHECKER bug
(a mid-battle sidecar dump carrying Minior-Meteor's stats for a Minior-Core).

**Corpus A re-sweeps completely clean** (0 hard / 0 soft / 0 warnings / 0
diverged over 10,000 games) — the first spotless 10k sweep. It does NOT count
toward the gate (its games informed the fixes) but it proves the engine can
produce one.

**Two gate attempts have failed.** Attempt 1 (gate9): 7 findings. Attempt 2
(40k): 50 findings — but roughly half traced to ONE self-inflicted regression
(see below).

### THE TWO PROCESS LESSONS (read these before running another wave)

1. **A fix can break a NEW class, invisibly.** Wave 9 removed Judgment's plate
   typing from `modify_choice`, correctly noting PS types it by the held plate
   — but the plate handlers in `items.rs` run AFTER the defender-ability pass,
   so Judgment stayed NORMAL when Levitate / Sap Sipper / Storm Drain /
   Lightning Rod checked it. 14 immunity findings plus most boost findings, all
   from one deletion. The 75-game targeted regression could not see it, because
   it only re-checks games we had already found.
   **=> After every wave, ALSO re-sweep one previously-clean 10k corpus.**
   Costs ~$0.50 and ~15 min. It would have caught this immediately.

2. **Parallel agents collide on shared code.** Twice, two agents independently
   implemented the same fix in different places: Relic Song's forme flip (the
   two applications CANCELLED, a net no-op) and Judgment's typing arm
   (duplicate match arms). The `old_string`-uniqueness check in
   `scratchpad/apply_patches.py` catches the second application, but the first
   one has already landed.
   **=> Give each agent explicit ownership of a file/function, or serialise
   agents whose clusters touch the same code.**

### DISK

19 corpora at ~1.5 GB each filled the 228 GB disk and silently killed a
generation run. Fixed by: deleting local tarballs (all in S3), deleting the
local 1M-game dataset (in S3), and pruning each consumed corpus to only the
games `verify_all.sh` replays. **Prune each corpus right after its findings
are diagnosed** — every corpus is byte-for-byte regenerable from its seed
range via `gen_corpus.js --start <seed>`.

### IN FLIGHT right now

- Gate attempt 3 corpora generating: holdout17/18/19 done (10k each),
  holdout20 in progress. Seeds 210001-250000, all unseen.
- Nothing else pending on Track A; all known findings are fixed and verified.

### Next actions for Track A

1. Collect wave 5 results → apply patches → `cargo test` → rebuild wheel.
2. Verify corpus C's 9 games + the 26-game regression set
   (`scratchpad/verify_waveB.sh` has the list; add corpus C's games).
3. Sweep corpus D = gate attempt. Then E, F, G for the four-in-a-row.
4. **After the gate passes**: regenerate the 1M-game training dataset on the
   certified engine (~$10, ~2h) so Track B trains on certified physics.

---

## TRACK B — Value network (Step 1)

### Design

Replace the hand-crafted `evaluate()` with a learned net, NNUE-style: small and
fast, because it is called hundreds of thousands of times per second inside MCTS.

- **Inputs**: per-mon learned embeddings (species 32d, item 12d, ability 12d,
  4 moves 16d each, tera type 8d) + 34 numeric features (HP, level, 5 stats,
  status one-hot, boosts, PP, flags). Bench is **sum-pooled** (a set, order does
  not matter); actives get their own slots. Side block (76 feats) carries hazards,
  screens, 34 volatile flags, durations, substitute HP, toxic/protect counters.
  Global block (14) carries weather/terrain/trick room.
- **Architecture**: shared per-mon MLP 2×128 → concat(2 actives + 2 pooled
  benches + 2 side blocks + global) → trunk 2×256 → 1 logit. ~350k params.
- **Target**: `λ·outcome + (1−λ)·search_root_value`. Outcome is unbiased but
  one bit per game (noisy); the search value is per-position but biased. Caches
  store the two SEPARATELY so λ can be ablated without re-encoding.
- **Side-swap augmentation**: half of each batch mirrors sides with label 1−y.
  Kills the measured 53.4% side-one bias and doubles effective data.

### Data

1,000,000 self-play games, 50 shards, 2.4 GB, ~38M positions.
- S3: `s3://pokebot-valuenet-389825051723/shards/` (canonical)
- Local: `valuenet/v1_data/`
- Generated by the CURRENT engine (both sides), 50ms/decision, random
  cross-pairings of 100k corpus teams. **Showdown is not in the loop** — the
  engine adjudicates, so engine defects are baked in. This is why the gate
  matters, and why we regenerate after it passes.

### Pipeline

```bash
valuenet/build_vocab.py            # frozen vocab from engine enums (run once)
valuenet/preprocess_shards.py <dir>   # parallel encode -> cache_v3_*.npz + stats_*.json
valuenet/train.py <dir> <epochs> <out.pt> <lambda>
```
Preprocessing caps workers by **RAM, not cores** (~7 GB peak per worker); one
OOM-killed worker poisons the whole pool as `BrokenProcessPool`.

### IN FLIGHT right now

Three spot instances training λ = 0.65 / 1.0 / 0.5 in parallel (~1.5–2h).
Checkpoints + logs land in `s3://<bucket>/train/`, marker `TRAIN_DONE_<tag>`.

### Next actions for Track B

1. **Calibration gate (kill gate)**: each run prints
   `CALIBRATION vs outcomes: net=X | search_value_baseline=Y` on held-out games.
   The net must beat the baseline. Pick the winning λ. If train ≈ val and both
   plateau high, the net is capacity-limited → widen (MON_HID/TRUNK) and retrain
   on cached tensors (cheap, no regeneration).
2. **M4 — Rust inference port** (long pole): mirror `encoder.py` in Rust with a
   **bit-exactness test** (10k states → identical features, outputs within float
   tolerance); export weights to a flat binary; SIMD forward pass; put it behind
   a flag so the hand eval stays switchable. Benchmark nodes/sec.
3. **M5 — SPRT gate**: net-bot vs current-bot at ladder time control, sequential
   test (H0 = no gain, H1 ≈ +10 Elo, α=β=0.05), a few thousand cloud games (~$15).
   Pass → deploy. Fail → diagnose eval speed vs calibration.
4. **Also SPRT: net-only vs net+probe.** Expectation is the two-phase probe
   system becomes unnecessary once the eval is good (it existed to compensate for
   eval error and was never proven to help — ground-truth playouts on its
   motivating turn came back a statistical tie). Removing it also dissolves the
   turn-1 clock overrun. Decide by test, not argument.

### After v1

- **v1.5 partial-info data** (~$100): two-pool self-play so reveal flags learn the
  value of information. Unlearnable from open-hands data.
- **Step 2 policy head**: learned move priors for search ordering. Largest
  remaining search-efficiency win.
- **Step 3 belief-state search** (encoder already forward-compatible).
- **Step 4 human-exploitation model.**
- **Iteration ratchet**: regenerate with net-guided search at deeper labels →
  retrain → SPRT vs reigning champion. ~$100–150/round, 3–5 rounds.

---

## AWS

Account 389825051723, region **us-east-2**. CLI at
`/Users/sallyliu/.awscli-venv/bin/aws` (credentials already configured).
IAM user `pokebot-cli` has EC2 + S3 full access, **no IAM or Service Quotas
perms** (cannot poll quota status; instance creds are baked into user-data).

- Bucket `pokebot-valuenet-389825051723`: `shards/`, `caches/`, `train/`,
  `gate/<name>/`, `backup/`, plus `code.tar.gz` / corpus tarballs / bootstraps.
- Keypair `~/.ssh/pokebot.pem` (EC2 name `pokebot`), SG `sg-024075dbfb3236454`
  (SSH from the user's IP — **update if their IP changes**), AL2023 AMI
  `ami-028ba4d4ccb4b7b72`.
- **Spot quota 300 vCPUs** (standard families). GPU is a SEPARATE quota, currently
  0 — request "All G and VT Spot Instance Requests" if GPU training is ever wanted.
- Budget: $1,000 approved, **~$30 spent**. Monthly budget alarm set.
- Launchers live in `valuenet/cloud/` (fleet, training, sweep, smoke).

**Before any expensive run**: 10-min canary at the real instance type and worker
count, a cheap full-data pass (parse every line, no encode), and fail-loud
milestone markers. v1 training took 4 attempts; all three failures were
preventable by these.

---

## Working rules (the user's, binding — pass verbatim into every subagent)

* Do exactly what I ask, then stop — no unrequested analysis, diagnosis, or
  fixes, however relevant they seem. Any out-of-scope issue worth flagging gets
  one sentence, no investigation behind it, then wait for my go-ahead.
* Pareto principle, always: ~80% of the result comes from ~20% of the effort —
  find that highest-leverage 20% and do it, never a partial answer. Even
  explicitly requested exhaustive work must justify its cost. Assessment,
  implementation, and verification all count as effort.
* Simplest solution, minimum everything: the least code, files touched,
  dependencies, tool calls, subagents, and words that correctly achieve the
  result. Never improve adjacent code.
* Shortest time to completion, never at correctness's expense (fast-but-wrong is
  done twice). Eliminate sequential waste: do independent work concurrently, run
  the minimum verification that proves correctness — once — and never idle-wait
  on what can run in the background.
* State the assumptions and tradeoffs of the requested work explicitly.
* These Rules bind Claude, its workflows, and every subagent alike: design every
  phase around them and pass them verbatim into each subagent prompt — never
  assume inheritance. Deliberately choose each subagent's model and effort level
  to match its task, and state both in its label (e.g. `[F·h] verify:fix`) —
  never inherited silently.

**Additional standing instruction: every subagent runs on Opus or stronger.**
Never Sonnet or Haiku, however mechanical the task looks. A Sonnet subagent
produced a confident, well-argued Zoroark fix that removed 1 of 25 warnings;
re-running on Opus found the real mechanism and a correctness bug underneath it.
The rework cost more than the model saving.

---

## Method rules learned painfully

1. **Never reason from protocol message text alone.** Reproduce, dump the ACTUAL
   engine instruction branches, then read the governing Showdown source. A prior
   wave prescribed a fix that would have closed zero of the two rows it targeted.
2. **The Python consumer of a mechanic tells you nothing about the Rust producer.**
3. **Verify every claimed fix against the failing game yourself.** A high-confidence
   agent report is a hypothesis, not a result.
4. **Regression-check prior rounds' games after every fix wave.** Two agents
   independently patched the same Cud Chew logic with contradictory half-correct
   rules; only the regression run caught it.
5. **"Engine is right, checker over-reports" is not an acceptable resolution** —
   push back and require proof; two such verdicts turned out to be real bugs.
6. **Fail loud.** A harness that reports COMPLETE with zero outputs costs a whole
   debugging cycle.

---

## Open items not yet fixed

- **Turn-1 probe clock overrun**: 13.5s against the 15s blitz cap. **Blocks ladder
  play.** Likely dissolved by removing the two-phase probe once the net lands.
- Live-play Zoroark move truncation (no sidecar → can still evict a real move from
  the opponent model). Fixable using randbats set data.
- `removed_item` / `knocked_off` latch residue on the Illusion replace path.
- Doom Desire volatile handling (Future Sight was special-cased; Doom Desire not).
- Terapagos forme tracking — two `estimator_error` refusals (`OverlongPartyError`,
  7-mon party). Kept loud deliberately.
- Both forks are public; the user was told and has not asked to change it.
