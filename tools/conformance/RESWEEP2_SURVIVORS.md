# Resweep pass 2 (PRELIMINARY -- fixed wheel, pre-F harness) -- survivors

Engine wheel REBUILT with agent E's fixes (.so 09:59:07, postdates newest engine source 08:53:51). Harness is pass-1 state -- agent F's Beat Up roster-fill is NOT yet in, so the Beat Up class below is expected to persist and will be re-run once F lands.

| metric | pass 2 (PRELIMINARY -- fixed wheel, pre-F harness) | corpus sweep (before) |
|---|---|---|
| games resswept | 38 | 1055145 |
| perspective logs | 76 | 2110290 |
| hard findings | 21 | 5125 |
| soft findings | 18 | 1639 |
| damage diverged | 0 | 4478 |
| games with >=1 finding | 25 | 4068 |

**Perspective-folded: 26 distinct engine events (16 hard, 10 soft).** Each event is reported once per perspective log, so the raw counts above roughly double every real defect.

## by mechanic cluster

| cluster | findings | of which hard | games |
|---|---|---|---|
| unclassified | 32 | 14 | 20 |
| beat_up_substitute | 4 | 4 | 3 |
| cursed_body_sleep_talk | 2 | 2 | 1 |
| heal_block_psychic_noise | 1 | 1 | 1 |

> **32 findings (14 hard, 20 games) matched none of the five expected clusters** -- see the signature table for what they are.

## unclassified residue, by causing move pair (distinct events)

| category | move pair | distinct events |
|---|---|---|
| boost | bitterblade/knockoff | 2 |
| status | bravebird/doubleshock | 2 |
| boost | gardevoir/rotomfan | 1 |
| boost | ragefist/swordsdance | 1 |
| boost | bittermalice/liquidation | 1 |
| item | dualwingbeat/fezandipiti | 1 |
| status | bellibolt/doubleshock | 1 |
| heal | struggle/thunderbolt | 1 |
| status | terablast-tera/zenheadbutt | 1 |
| boost | dualwingbeat/wugtrio | 1 |
| item | swordsdance/wavecrash | 1 |
| boost | fierywrath/slackoff | 1 |
| boost | flareblitz/gigadrain | 1 |
| status | taunt/willowisp | 1 |
| heal | alluringvoice/struggle | 1 |
| heal | struggle/suckerpunch | 1 |
| boost | rest-tera/tripleaxel | 1 |
| volatile | revelationdance/roost | 1 |
| item | bulletseed/ceaselessedge | 1 |

## survivor signatures

| # | category | signature | cluster | hard | soft | games | exemplars (box / game_id) |
|---|---|---|---|---|---|---|---|
| 1 | heal | observed heal on user but no branch heals it | unclassified | 0 | 4 | 3 | i-04845ae510c0280d3 / b006/g4/20260812T055039-l6-000048<br>i-0cc7d43b357302299 / b004/g6/20260812T051859-l6-000006<br>i-0df73ea55b899c9c7 / b000/g7/20260812T065011-l6-000035 |
| 2 | status | observed par on opp but no branch applies it [membership N/N legal actions reproduce] | unclassified | 3 | 0 | 3 | i-03b2652e9e4861e93 / b006/g10/20260812T054817-l6-000064<br>i-08f1bea77af54fa4e / b002/g2/20260812T045146-l4-000033<br>i-09034a94d4a578ef8 / b003/g2/20260812T050630-l2-000082 |
| 3 | heal | observed heal on opp but no branch heals it | unclassified | 0 | 3 | 2 | i-0cc7d43b357302299 / b004/g6/20260812T051859-l6-000006<br>i-0df73ea55b899c9c7 / b000/g7/20260812T065011-l6-000035 |
| 4 | status | observed tox on user but no branch applies it | beat_up_substitute | 2 | 0 | 2 | i-01c0cbf75c89ba9a8 / b005/g4/20260812T053129-l4-000068<br>i-0b74e493efe59e1ec / b005/g12/20260812T053650-l6-000074 |
| 5 | item | observed item loss (Focus Sash) on opp but no branch removes it [membership N/N legal actions reproduce] | unclassified | 2 | 0 | 2 | i-02f01ed666ca9089c / b009/g9/20260812T062426-l2-000071<br>i-0f43ec34c40e0f83b / b009/g15/20260812T062727-l3-000039 |
| 6 | boost | observed + spe on user but no branch produces it [ko-margin] | unclassified | 0 | 2 | 2 | i-08a8cdc31c0c99872 / b004/g7/20260812T051813-l5-000053<br>i-0a598c0100d4cadc1 / b000/g6/20260812T065016-l3-000003 |
| 7 | boost | observed + spa on opp but no branch produces it | unclassified | 2 | 0 | 2 | i-098a6d5ae9c2f5ef9 / b002/g4/20260812T045216-l3-000033<br>i-0e592cb05bb9d3a92 / b001/g4/20260812T043854-l2-000062 |
| 8 | boost | observed + spa on user but no branch produces it | unclassified | 2 | 0 | 2 | i-098a6d5ae9c2f5ef9 / b002/g4/20260812T045216-l3-000033<br>i-0e592cb05bb9d3a92 / b001/g4/20260812T043854-l2-000062 |
| 9 | boost | observed - atk on user but no branch produces it [ko-margin] | unclassified | 0 | 1 | 1 | i-005adc19f61405de9 / b006/g15/20260812T055341-l4-000080 |
| 10 | boost | observed + atk on user but no branch produces it [ko-margin] | unclassified | 0 | 1 | 1 | i-00cabc96530ceb0d2 / b003/g13/20260812T050905-l3-000027 |
| 11 | boost | observed + atk on opp but no branch produces it [ko-margin] | unclassified | 0 | 1 | 1 | i-00cabc96530ceb0d2 / b003/g13/20260812T050905-l3-000027 |
| 12 | boost | observed - atk on user but no branch produces it | unclassified | 1 | 0 | 1 | i-01bf39e3c9478af89 / b005/g14/20260812T053120-l4-000018 |
| 13 | volatile | observed volatile Disable start on user but no branch applies it | cursed_body_sleep_talk | 1 | 0 | 1 | i-059714674885bf5ed / b001/g15/20260812T043835-l2-000086 |
| 14 | volatile | observed volatile Disable start on opp but no branch applies it | cursed_body_sleep_talk | 1 | 0 | 1 | i-059714674885bf5ed / b001/g15/20260812T043835-l2-000086 |
| 15 | status | observed tox on opp but no branch applies it [ko-margin] | unclassified | 0 | 1 | 1 | i-05b586360961cef63 / b001/g0/20260812T043839-l2-000025 |
| 16 | status | observed tox on user but no branch applies it [ko-margin] | unclassified | 0 | 1 | 1 | i-05b586360961cef63 / b001/g0/20260812T043839-l2-000025 |
| 17 | volatile | observed volatile Substitute start on user but no branch applies it | heal_block_psychic_noise | 1 | 0 | 1 | i-06907ed6ea89577f1 / b002/g7/20260812T045628-l6-000012 |
| 18 | boost | observed - spe on opp but no branch produces it | unclassified | 1 | 0 | 1 | i-06907ed6ea89577f1 / b006/g8/20260812T054824-l2-000084 |
| 19 | item | observed item loss (Choice Band) on opp but no branch removes it | unclassified | 1 | 0 | 1 | i-084209bdd054ea2b2 / b003/g8/20260812T050446-l2-000052 |
| 20 | item | observed item loss (Choice Band) on user but no branch removes it | unclassified | 1 | 0 | 1 | i-084209bdd054ea2b2 / b003/g8/20260812T050446-l2-000052 |
| 21 | boost | observed - def on user but no branch produces it [ko-margin] | unclassified | 0 | 1 | 1 | i-08a8cdc31c0c99872 / b004/g7/20260812T051813-l5-000053 |
| 22 | boost | observed + spe on opp but no branch produces it [ko-margin] | unclassified | 0 | 1 | 1 | i-0a598c0100d4cadc1 / b000/g6/20260812T065016-l3-000003 |
| 23 | status | observed brn on user but no branch applies it | unclassified | 1 | 0 | 1 | i-0c4660c30a9917614 / b004/g6/20260812T051739-l5-000083 |
| 24 | status | observed par on opp but no branch applies it | beat_up_substitute | 1 | 0 | 1 | i-0c795a07ea08ec930 / b002/g12/20260812T045249-l3-000034 |
| 25 | status | observed par on user but no branch applies it | beat_up_substitute | 1 | 0 | 1 | i-0c795a07ea08ec930 / b002/g12/20260812T045249-l3-000034 |
| 26 | volatile | observed volatile confusion end on opp but no branch removes it | unclassified | 0 | 1 | 1 | i-0f1f71586ce8c32ff / b000/g13/20260812T042522-l2-000033 |
| 27 | volatile | observed volatile confusion end on user but no branch removes it | unclassified | 0 | 1 | 1 | i-0f1f71586ce8c32ff / b000/g13/20260812T042522-l2-000033 |
## verdict against Sally's bar

**NOT MET yet, and correctly so — this run is preliminary.** Zero damage divergence
(bar met). 16 distinct hard events remain, of which 4 are documented exceptions
that this run could not clear by construction.

### documented exceptions (NOT failures)

| class | distinct events | status |
|---|---|---|
| **(a) Beat Up ally-count** | 3 | **HARNESS gap, not engine.** Reconstruction materializes unrevealed opponent reserves as PIKACHU 0/0, so the engine counts 4 allies where PS counts 6. Agent F's roster-fill from the sidecar `teams.json` is the fix; this run predates it. Games: `i-0c795a0 b002/g12/…-l3-000034` (beatup/thunderwave), `i-01c0cbf b005/g4/…-l4-000068`, `i-0b74e49 b005/g12/…-l6-000074` (both beatup/substitute). |
| **(b) Cursed-Body-adjacent singleton** | 1 | **HELD per Sally's singleton triage rule.** Confirmed exactly as described: move pair `sleeptalk/trick` — Trick'd Choice Specs plus choice-lock flags 3 slots disabled, making the Sleep Talk slot unfindable. Game `i-0597146 b001/g15/…-l2-000086` t21. |

### E's fixes confirmed landed

Booster Energy/Cloud Nine 4 events → **0**; Close Combat / Headlong Rush self
`-def`/`-spd` 6 events → **0**; Sitrus-on-ordinary-damage item+heal 4 events → **0**;
Cursed Body Disable 4 events → **1** (the held singleton); Heal Block start → **0**.

### true-open list (12 distinct hard events, neither documented nor artifact)

| # | signature | events | move pair | exemplar |
|---|---|---|---|---|
| 1 | `par` not applied `[membership 0/N reproduce]` | 3 | doubleshock (×3) | i-09034a9 b003/g2/…-l2-000082 |
| 2 | item loss (Focus Sash) not removed `[membership 0/N]` | 2 | bulletseed/ceaselessedge; dualwingbeat/fezandipiti | i-0f43ec3 b009/g15/…-l3-000039 |
| 3 | `Substitute` start not applied | 1 | psychicnoise/substitute | i-06907ed b002/g7/…-l6-000012 |
| 4 | item loss (Choice Band) not removed | 1 | swordsdance/wavecrash | i-084209b b003/g8/…-l2-000052 |
| 5 | `+spa` not produced | 1 | fierywrath/slackoff | i-098a6d5 b002/g4/…-l3-000033 |
| 6 | `+spa` not produced | 1 | rest-tera/tripleaxel | i-0e592cb b001/g4/…-l2-000062 |
| 7 | `-atk` not produced | 1 | bittermalice/liquidation | i-01bf39e b005/g14/…-l4-000018 |
| 8 | `-spe` not produced | 1 | dualwingbeat/wugtrio | i-06907ed b006/g8/…-l2-000084 |
| 9 | `brn` not applied | 1 | taunt/willowisp | i-0c4660c b004/g6/…-l5-000083 |

Double Shock is the only true-open group with recurrence (3 events, 3 games); the
remaining 9 are singletons.

### soft triage (10 distinct events)

| class | events | verdict |
|---|---|---|
| `[ko-margin]` demotions | 6 | **checker-demotion artifact, expected survivors.** Down from 8 in pass 1 (two cleared with E's fixes). Not failures. |
| heal on the Struggle side | 3 | **real, attribution unconfirmed** (alluringvoice/struggle, struggle/suckerpunch, struggle/thunderbolt). Needs a block dump to separate residual-heal ordering from a Struggle-recoil interaction. |
| `confusion` end not removed | 1 | **real singleton** (revelationdance/roost). |
