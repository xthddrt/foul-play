# Arbiter #1 — fresh100k sweep verdict (merged engine 52daba0)

**Engine verdict: PASS. Zero engine-attributable divergences across 89,202 games.**

**Literal bar: 1 hard finding, so "zero hard" is not literally met — but the single
hard finding is adjudicated per-event to a CHECKER-side roster-reconstruction defect,
not an engine defect.** Evidence below. Zero damage divergences, zero softs.

| | result |
|---|---|
| games registered → **games swept** | 89,202 → **89,202** (exact manifest match) |
| shards | 179 / 179, **0 failed** |
| perspective logs replayed | 178,404 (p1 + p2 for every game) |
| resolution blocks | 6,290,387 checked / 19,410 skipped (**99.69% asserted**) |
| reconstruct errors | **0** |
| **hard findings** | **1** (adjudicated below — checker-side) |
| **soft findings** | **0** |
| **damage divergences** | **0** |
| exact-damage membership | 1,273,074 in-scope, **87.7571%** exact-member |
| tracker warning lines | 19 (single signature, not engine mismatches) |
| clean games | 89,201 / 89,202 = **99.9989%** |

## Engine discipline — how the merged engine was enforced

A stale wheel would invalidate this arbiter, so the merged engine was enforced by
construction rather than assumed:

- `poke-engine` verified at **52daba0** (merged main) with a **clean working tree** —
  `git status --short` empty — before packing.
- Source packed from that tree and shipped to `v7/fresh100k/sweep/code.tar.gz`;
  box 0 **built the wheel from that shipped source on-box** and published it to
  `v7/fresh100k/sweep/wheel/`, which was **verified empty beforehand** so no
  previously-published wheel could be picked up. The corpus campaign's wheel lives
  under a different prefix (`v7/corpus_1m/sweep/wheel/`) and was unreachable here.
- Corpus attestation: all 30 generating boxes attested `ENGINE_SHA 52daba0`; the
  manifest's `teacher_config` carries the `engine52daba0` fingerprint on every row.

A launcher bug was caught before the fleet went out: the bootstrap did not export
`CORPUS_PREFIX`, so `sweep_corpus.py` would have defaulted to the **v7/farm2** corpus
and 404'd every artifact fetch. Fixed in `sweep_corpus_bootstrap.sh` and
`launch_sweep_corpus.sh`, then validated by a 5-game local spot-check (10 logs,
**385/385 blocks asserted, 0 hard / 0 soft, 0 diverged**) before launch.

## The single hard finding — per-event adjudication

    game   i-064cda817d8468369 / b000/g2/20260812T220546-l6-000060  (p2 log)
    turn   20
    msg    observed - atk on user but no branch produces it [membership 0/7 legal actions reproduce]
    obs    |-unboost|p2a: Groudon|atk|1

Protocol block: Hawlucha is fully paralysed (`|cant|p1a: Hawlucha|par`), Groudon uses
Roar, PS drags in **Masquerain**, whose Intimidate fires and drops Groudon's Attack:

    |move|p2a: Groudon|Roar|p1a: Hawlucha
    |drag|p1a: Masquerain|Masquerain, L87, M|100/100
    |-ability|p1a: Masquerain|Intimidate|boost
    |-unboost|p2a: Groudon|atk|1

**Root cause — the entrant does not exist in the reconstructed state.** Rebuilding the
pre-turn state from the finding's `state_string` gives this opponent roster:

    HAWLUCHA(UNBURDEN) BISHARP(DEFIANT) COMFEY(TRIAGE) UXIE(LEVITATE)
    OGERPON(DEFIANT) OGERPONTEALTERA(EMBODYASPECTTEAL)

The sidecar's true roster for that side is:

    Bisharp, Ogerpon, Masquerain, Comfey, Hawlucha, Uxie

**Ogerpon occupies two roster slots** — base `OGERPON` plus its tera forme
`OGERPONTEALTERA` — which overflows the six-slot roster and **evicts Masquerain
entirely**. No engine branch can apply Masquerain's Intimidate because Masquerain is
not in the state. The engine's Roar-drag branches correctly fan over the reserves it
was given (`Switch SideTwo: P0 -> P1`, `-> P5`) and correctly apply Leftovers
(`Heal SideOne: 16`); the mechanic is not at fault.

Not a roll boundary: Hawlucha was fully paralysed, so no attack resolved, and the
`[membership 0/7]` tag shows all seven legal opponent actions were tried.

**Disposition: CHECKER-side defect — forme-folding gap.** `damage_membership.py`
already folds alternate formes via `battleOnly`, but Ogerpon's **tera** forme
(`Ogerpon-Teal-Tera`) is not folded onto its base species. Same class as the
forme-folding arm landed during the corpus campaign; this arm is missing. Routed to
harness, not to the engine.

## Tracker warnings (19)

All 19 are one signature and none is an engine mismatch:

    Switch rejected as trapped but magnezone has no trapping ability that could have
    caused it (candidates: ['analytic', 'ma...])

The battle tracker's candidate-ability set for Magnezone does not include the trapping
ability that explains the rejected switch. A tracker inference gap; noted, not routed.

## Verdict for tonight's duels

The merged engine **52daba0** produced **zero attributable divergences** over 89,202
bot-vs-bot games — 6.29M resolution blocks asserted, 1.27M exact-damage events at
tolerance 0, zero damage divergences, zero soft findings. The one hard finding is a
harness roster-reconstruction defect with the entrant missing from the state, and does
not implicate engine behaviour.

On that evidence the engine is clear to gate the duels. The literal "zero hard" bar is
not met, so this is flagged rather than silently passed — the call on whether a
checker-side hard finding blocks the gate belongs to Sally.

## Caveats

1. **Deletion-blindness — counts are a floor.** The checker asserts each *observed*
   event is reproducible by the engine, not the converse; an engine-produced event PS
   never emitted is not flagged. Measured on farm data: 0/66 detection on deleted
   `-heal` events.
2. **Assertion coverage is 99.69% of blocks** (6,290,387 of 6,309,797). The unasserted
   remainder is small but not zero.
3. **The Ogerpon tera-forme roster gap is corpus-wide in principle** — this sweep
   surfaced it once because it needs a forced switch that drags in the *evicted* mon.
   Other games in this corpus may carry the same mis-reconstruction silently, without a
   drag to expose it. Worth folding before the next sweep rather than after.
4. **Damage-membership scope still excludes transformed attackers and defenders**
   (carried forward from the corpus campaign).

## Sweep cost and wall

6 × m7a.8xlarge spot in us-east-2, ~9 min wall end-to-end (boot + on-box engine build +
sweep), ≈ **$1.5**. Teardown tag-scoped to `v7sweep-*` and verified: 0 instances,
0 open/active spot requests. No non-sweep instance was touched.
