# Final-exam sweep verdict — bot side (engine 0a372f3)

**VERDICT: NOT CLEAN. Leg 1 of the two-consecutive-clean conformance gate does NOT pass.**

Zero damage divergences, but **2 hard findings and 2 soft findings** across 2 games,
against a no-exceptions bar of zero/zero/zero. Both are real divergences on per-event
evidence; neither is dismissible as an artifact.

| | result |
|---|---|
| games registered → **games swept** | 100,372 → **100,372** (exact manifest match) |
| shards | 201 / 201, **0 failed** |
| perspective logs replayed | 200,744 (p1 + p2 for every game) |
| resolution blocks | 7,077,325 checked / 17,848 skipped (**99.75% asserted**) |
| reconstruct errors | **0** |
| **hard findings** | **2** (1 distinct event) |
| **soft findings** | **2** (1 distinct event) |
| **damage divergences** | **0** |
| exact-damage membership | 1,433,703 in-scope, **87.7332%** exact-member |
| tracker warning lines | 16 |
| clean games | 100,370 / 100,372 = **99.9980%** |

## Engine attestation

- `poke-engine` verified locally at **0a372f3** with a **clean working tree** before packing.
- `RUN_CONFIG.json` / `DONE` attest `engine_sha 0a372f3c8b37b221e13e5dffed74c2d53e8a4a9a`,
  `engine_dirty_files 0`, **30/30 boxes published `ENGINE_SHA.json = 0a372f3`**,
  30 DRAINED / 0 FAILED / 0 WEDGED / 0 FAILRATE / 0 PAIR_COLLISION, 0 incomplete dropped.
- Sweep boxes built the engine **from the shipped 0a372f3 source on-box**; the wheel
  prefix `v7/exam_bot100k/sweep/wheel/` was **verified empty** beforehand, so no
  previously-published wheel was reachable.
- Pre-launch validation: 5-game local spot-check, **378/381 blocks asserted, 0 hard /
  0 soft / 0 diverged**.

## Finding 1 — HARD, engine defect: Double Shock's type removal is not applied

    game   i-0c75ab2481b9c0d3f / b001/g1/20260813T002914-l2-000073   (both perspectives)
    turn   13
    msg    observed par on opp but no branch applies it
    obs    |-status|p2a: Pawmot|par

Protocol: Pawmot uses Double Shock, which strips its Electric type, then Regigigas'
Body Slam paralyses it.

    |move|p2a: Pawmot|Double Shock|p1a: Regigigas
    |-start|p2a: Pawmot|typechange|???/Fighting|[from] move: Double Shock
    |move|p1a: Regigigas|Body Slam|p2a: Pawmot
    |-damage|p2a: Pawmot|25/243
    |-status|p2a: Pawmot|par

**Evidence.** The reconstructed state has Pawmot as **ELECTRIC/FIGHTING**, and neither
engine branch contains any typechange instruction — the branches carry only the
Substitute break, damage (166 / 203) and Leftovers. Pawmot therefore stays Electric in
every branch, Electric types are **immune to paralysis**, and so no branch can apply
`par`.

**Not a roll boundary.** Body Slam rolls **153..181** against Pawmot's 203 HP — no roll
KOs, and the observed damage is consistent with the branch set. The *only* divergence
is the missing status.

**Disposition: engine defect, routed.** Note this is the same mechanic as the corpus
campaign's "Double Shock → `par` not applied" cluster reported fixed in wave 1 (3→0).
Either that fix does not cover this path — Double Shock used from behind a Substitute,
with the typechange landing before the opponent's move — or it regressed. **Worth
treating as a gate-blocking regression candidate rather than a fresh singleton.**

## Finding 2 — SOFT, engine: multi-hit loop does not break when the attacker faints

    game   i-0175b245d8cb16985 / b000/g6/20260813T001450-l6-000006   (both perspectives)
    turn   35
    msg    observed side-condition spikes change but no branch produces it [ko-margin]
    obs    |-sidestart|p2: l6b07fdf2|Spikes

Protocol: Maushold-Four (18/238 HP) uses Population Bomb into Skarmory, which holds
**Rocky Helmet**. The Rocky Helmet chip kills Maushold **after hit 1** — PS prints
`|-hitcount|p1a: Skarmory|1` — and Skarmory then sets Spikes.

    |move|p2a: Maushold|Population Bomb|p1a: Skarmory
    |-damage|p2a: Maushold|0 fnt|[from] item: Rocky Helmet|[of] p1a: Skarmory
    |faint|p2a: Maushold
    |-hitcount|p1a: Skarmory|1
    |move|p1a: Skarmory|Spikes|p2: Maushold
    |-sidestart|p2: l6b07fdf2|Spikes

**Evidence.** The engine's branch runs **four** hits (`Damage SideOne: 31, 31, 31, 20`)
plus the Rocky Helmet chip — it keeps swinging after the attacker is dead. With the
turn diverging that early, Skarmory's Spikes appears in no branch.

**The `[ko-margin]` tag does not justify dismissal here.** Rocky Helmet is a
deterministic 1/6-max-HP chip (39 vs Maushold's 18 remaining), so the break happens on
hit 1 **regardless of roll**; Population Bomb's own rolls (29..34) never KO Skarmory
(113/235). Nothing is roll-dependent.

**Disposition: the documented DEFERRED multi-hit-break class**, reached by a new route.
`verify_all_findings.sh` records it for Effect Spore sleeping the attacker on hit 1 of
Surging Strikes — PS stops the move (`battle-actions.ts:890`) while the engine folds
secondary rolls post-loop (`gi.rs:5423`), so the break cannot be expressed without
branching the hit loop. This is the same shape with the attacker **killed by Rocky
Helmet** instead of slept. Routed as a decision — extend the defer to cover
attacker-death breaks, or fix the hit loop — rather than silently carried.

## Tracker warnings (16)

- **15×** `Switch rejected as trapped but magnezone has no trapping ability that could
  have caused it (candidates: ['analytic', 'magnetpull', ...])` — same tracker
  inference gap seen in the fresh100k sweep. Not an engine mismatch.
- **1×** `REFUSING to derive an absorbed Substitute hit [survival_contradiction]
  suicune: rolls >= 74 vs tracked sub <= 74` — the checker declining to derive rather
  than asserting wrongly. Correct conservative behaviour, not a divergence.

## Gate status

The conformance gate requires **two consecutive clean sweeps**. This sweep is **not
clean**, so the streak is **0** — leg 1 does not pass and no streak is carried. The
gate cannot be re-attempted meaningfully until Finding 1 is fixed and Finding 2 is
adjudicated, since both would otherwise recur.

Neither finding touches damage: **0 damage divergences over 1,433,703 in-scope
exact-damage events at tolerance 0** — the damage lane's wave-2 work holds up
corpus-wide on a fresh corpus.

## Caveats

1. **Deletion-blindness — counts are a floor.** The checker asserts each observed event
   is reproducible by the engine, not the converse (measured 0/66 on deleted `-heal`
   events on farm data).
2. **Assertion coverage 99.75%** of blocks (7,077,325 of 7,095,173); the unasserted
   remainder is small but not zero.
3. **The Ogerpon tera-forme roster-folding gap** found in the fresh100k sweep is still
   open on the harness side and would silently mis-reconstruct rosters here too; it
   surfaces only when a forced switch drags in the evicted mon.
4. **Damage-membership scope still excludes transformed attackers and defenders.**

## Sweep cost and wall

6 × m7a.8xlarge spot, us-east-2, ~10 min wall end-to-end (boot + on-box engine build +
sweep), ≈ **$1.7**. Teardown tag-scoped to `v7sweep-*` and verified 0 instances /
0 open spot requests in all three regions; the generator fleet (`v7-exambot100k-*`)
was never in scope.
