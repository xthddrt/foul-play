# v7 corpus_1m conformance — FINAL VERDICT (closing record)

**Bar MET, including Sally's tightened bar: zero hard, zero soft, zero damage
divergences, zero tracker warnings.**

Formal run: the full `PASS2_GAMES.tsv` set — all **38** games that survived pass 1,
not the reduced pass-2/3 subsets — against the wave-2 tree.

| | result |
|---|---|
| games re-swept | 38 (76 perspective logs) |
| **hard findings** | **0** |
| **soft findings** | **0** |
| **damage divergences** | **0** |
| checker stderr / tracker warnings | **0** |
| in-scope damage asserted | 640 (member 586) |
| wall | 13 s |

Independently reproduces the mechanics lane's closing resweep.

Artifact under test, verified by inspection rather than assertion:

- engine extension `poke-engine/poke-engine-py/python/poke_engine/poke_engine.cpython-312-darwin.so`,
  built **13:11:05**, postdating the newest engine source (`generate_instructions.rs`, 13:08:13).
  The package is an **editable install**; `site-packages/poke_engine/` does not exist,
  so a freshness check pointed there silently misses the real binary — resolve the
  extension through `importlib.util.find_spec`, not the site-packages path.
- harness `fp/replay/checker.py` **13:09:38**, `fp/replay/damage_membership.py` **13:07:10**,
  `fp/replay/protocol.py` 13:07:10.

## Coverage chain — why this is a corpus-wide result

Corpus-wide **by construction**, not by sampling:

1. **Full sweep** — all **1,055,145** registered games (2,110,290 perspective logs,
   73,632,218 resolution blocks asserted of 73,854,188) at damage-tolerance 0.
   Swept count equals the register exactly; 2,111/2,111 shards, 0 failed. Every game
   with ≥1 finding enumerated: **4,068** → `ISSUE_GAMES.tsv`, column sums reconciling
   to 5,125 hard / 1,639 soft / 4,478 damage / 6,764 findings.
2. **Pass 1** (corrected harness, pre-fix engine) — re-swept all 4,068. **4,030 went
   clean**; 38 survived.
3. **Passes 2–3, the wave-1 verdict, and this wave-2 verdict** — re-swept **every** one
   of the 38 survivors. Now 0/0/0/0.

Every one of the 1,055,145 games is either clean at the full sweep, clean at pass 1,
or individually re-verified here. No game carrying a finding was retired unswept.

## The 16 former softs — per-event dispositions

**All 16 are fixed. None was dismissed as an artifact.**

This is the most important entry in this record. The earlier draft of this document
classified 6 of these as "ko-margin demotion artifacts" and 3 as "attributed and
documented", on **class-level** reasoning. Sally's per-event bar overturned that
classification completely.

The method that broke it: a finding exists *because* no branch matched, so the finding
record can never prove artifact status from itself. But `calculate_damage_rolls_full`
exposes all 16 Showdown-exact rolls and each finding carries its exact `state_string`,
so `tools/conformance/probe_soft_events.py` rebuilds each pre-turn state and asks
whether the defender's HP falls **inside** the engine's own roll set. A straddle would
prove the observed outcome reachable at weight (#surviving)/16, hidden only by the
binding's `0.925 × max` fold. **All 16 came back `no straddle`** — the KO boundary
explained none of them.

| # | cluster | findings | probe evidence that broke the artifact reading | disposition |
|---|---|---|---|---|
| S1 | Rage Fist vs transformed Ditto | 2 | rolls **276..325** vs Haxorus **244** HP → all 16 KO; PS shows 186 damage, **below the engine's minimum roll** | **fixed engine-side** — `times_attacked` transform copy |
| S2 | residual heals on Struggle turns | 7 | state was **correct** (Wigglytuff `wish=(1, 212)`, all carried `item=LEFTOVERS`) yet no healing branch existed | **fixed engine-side** — Struggle recoil in fold thresholds; drain heal in the recoil cap |
| S3 | Weak Armor + Knock Off | 2 | Knock Off rolls **162..192** vs Ceruledge **81** HP → all KO, yet PS shows it alive with `-def 1`/`+spe 2` | **fixed** — threshold-fan modified-BP read (engine) with entrant reserve-HP seeding (checker) |
| S4 | Speed Boost after a KO | 2 | Flare Blitz **328..385** vs Lilligant **210** → all KO; PS still emits `+spe 1` at end of turn | **fixed engine-side** — fainted-tracer gate |
| S5 | Traced Intimidate on switch-in | 1 | **double-switch turn with no attacking move at all** — no KO boundary existed to justify the ko-margin tag | **fixed engine-side** — fainted-tracer gate |
| S6 | Toxic Chain secondary | 2 | Zen Headbutt 108..128 vs 265 → no KO; no branch applied `tox` | **fixed engine-side** |

Two of my own earlier attributions were also overturned by the probe: the claim that a
pending Wish "is not expressible in the reconstructed pre-turn state" was false
(`wish=(1, 212)` was present), and the six ko-margin events were not artifacts on any
evidence obtainable.

**Had these been triaged at class level, 9 real engine fixes and 2 checker fixes would
have been buried behind a documentation pass.** The per-event bar is what surfaced them.

Reproducing probe (evidence published as `SOFT_PROBE_EVIDENCE.txt`, inputs as
`final_findings.json`) — but read caveat 2 first:

    .venv/bin/python tools/conformance/probe_soft_events.py final_findings.json

## Fix ledger

**Wave 1 — 14 engine fixes** (12:17 build). Clearances observed directly across my own
passes, same games and same harness with only the wheel changing: Booster Energy /
Cloud Nine 4→0, Close Combat / Headlong Rush self `-def`/`-spd` 6→0, Sitrus on ordinary
damage 4→0, Cursed Body Disable 4→0, Heal Block / Psychic Noise 2→0, Beat Up ally-count
3→0, Double Shock `par` 3→0, Focus Sash / Choice Band 3→0, remaining hard singletons 6→0.

**Wave 2 — 9 engine fixes + 2 checker fixes.** Damage lane: `times_attacked` transform
copy (in foul-play), threshold-fan modified-BP read. Mechanics lane: Libero typeless
gate, Struggle recoil in fold thresholds, drain heal in the recoil cap, Tera Blast flip
before ability gates, fainted-tracer gate. Checker: illusion item-gain backfill, entrant
reserve-HP seeding from `|switch|` lines. Cargo fully green (45/45 in the round-11 file).

The fix→finding attribution in the S1–S6 table above is the lanes'; what this document
verifies independently is the **end state** — all 16 findings gone, on the 38-game set,
on the current tree.

**Harness arms** (throughout): `damage_membership.py` — atk-zero, spe-zero and HP-shave
arms, forme-folding via `battleOnly` / `max_hp_exact`, Beat Up roster-fill from the
sidecar. The harness correction alone removed **4,386 Struggle damage findings and all
4,478 damage divergences** from the corpus sweep — those were never engine defects, and
no engine change would have cleared them.

## Caveats carried forward

1. **Deletion-blindness — every count here is a floor.** The checker asserts that each
   *observed* event is reproducible by the engine; it does not assert the converse, so
   an engine that produces an event PS never emitted is not flagged. Measured directly:
   deleting an observed `-heal` was detected **0/66** times on farm data and 6/67 on the
   synthetic reference corpora.
2. **Stale-stored-state probe discrepancy on S1/S3 — recorded, with its resolution.**
   The mechanics lane's probe replayed the *stored* `state_string` captured in the pass-1
   findings, which predates the wave-2 fixes, and so continued to show a divergence after
   the fix had landed. Resolution: the damage lane's direct rechecks against **current**
   state returned PS-exact members, **30/30**. The stale reading is a property of the
   probe *input*, not of the engine. Anyone re-running `probe_soft_events.py` against the
   archived `final_findings.json` will reproduce it and must not read it as a live
   divergence.
3. **The late checker fixes have not been held to the corpus bar.** Both are verified
   only on this 38-game set plus the unit suite. The upcoming **fresh-100k sweep** is
   what qualifies them; a harness regression affecting games outside this set would not
   have been caught here.
4. **gen6–8 Libero arm shares the missing typeless gate.** Same defect class as the
   gen9 Libero fix, not fixed. Out of scope for this campaign — flagged, not addressed.
5. **Damage-membership scope still excludes transformed attackers and defenders.** The
   exact-damage check declines those events rather than asserting them, which is why S1
   surfaced as a categorical boost finding rather than a damage divergence. Flagged for
   the next sweep's checker work.
6. **The checker asserts only on nameable/decidable turns** — corpus-wide 99.70% of
   resolution blocks (73,632,218 of 73,854,188). The unasserted remainder is small but
   not zero.

## Published artifacts

`s3://pokebot-valuenet-389825051723/v7/corpus_1m/conformance/` — `DONE`, `FINDINGS.md`,
`ISSUE_GAMES.tsv`, `FAMILY_EXEMPLARS.tsv`, `RESWEEP1_SURVIVORS.md`,
`RESWEEP2_SURVIVORS.md`, `PASS2_GAMES.tsv`, `PASS3_GAMES.tsv`, `SOFT_PROBE_EVIDENCE.txt`,
`final_findings.json`, and this document.
