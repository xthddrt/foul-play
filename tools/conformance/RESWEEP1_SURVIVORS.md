# Resweep pass 1 -- survivors (corrected harness, PRE-fix engine wheel)

Engine wheel unchanged from the corpus sweep; only `fp/replay/damage_membership.py` moved. So every row below is what the corrected checker STILL attributes to the engine.

| metric | pass 1 | corpus sweep (before) |
|---|---|---|
| games resswept | 4068 | 1055145 |
| perspective logs | 8136 | 2110290 |
| hard findings | 46 | 5125 |
| soft findings | 26 | 1639 |
| damage diverged | 0 | 4478 |
| games with >=1 finding | 38 | 4068 |

**Perspective-folded: 44 distinct engine events (30 hard, 14 soft).** Each event is reported once per perspective log, so the raw counts above roughly double every real defect.

## by mechanic cluster

| cluster | findings | of which hard | games |
|---|---|---|---|
| unclassified | 52 | 26 | 25 |
| booster_energy_cloud_nine | 8 | 8 | 4 |
| cursed_body_sleep_talk | 5 | 5 | 4 |
| beat_up_substitute | 4 | 4 | 3 |
| heal_block_psychic_noise | 3 | 3 | 2 |

> **52 findings (26 hard, 25 games) matched none of the five expected clusters** -- see the signature table for what they are.

## unclassified residue, by causing move pair (distinct events)

| category | move pair | distinct events |
|---|---|---|
| boost | closecombat/exeggutor | 4 |
| boost | headlongrush/taurospaldeablaze | 2 |
| boost | bitterblade/knockoff | 2 |
| status | bravebird/doubleshock | 2 |
| boost | gardevoir/rotomfan | 1 |
| boost | ragefist/swordsdance | 1 |
| boost | bittermalice/liquidation | 1 |
| item | dualwingbeat/fezandipiti | 1 |
| status | bellibolt/doubleshock | 1 |
| heal | struggle/thunderbolt | 1 |
| status | terablast-tera/zenheadbutt | 1 |
| item | flamethrower/pyroar | 1 |
| heal | flamethrower/pyroar | 1 |
| item | calyrexice/hypervoice | 1 |
| heal | calyrexice/hypervoice | 1 |
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
| 1 | heal | observed heal on user but no branch heals it | unclassified | 0 | 6 | 5 | i-04845ae510c0280d3 / b006/g4/20260812T055039-l6-000048<br>i-05c3503b2c9659367 / b007/g1/20260812T060033-l3-000022<br>i-0649cc4359c805377 / b001/g4/20260812T044206-l2-000060 |
| 2 | heal | observed heal on opp but no branch heals it | unclassified | 0 | 5 | 4 | i-05c3503b2c9659367 / b007/g1/20260812T060033-l3-000022<br>i-0649cc4359c805377 / b001/g4/20260812T044206-l2-000060<br>i-0cc7d43b357302299 / b004/g6/20260812T051859-l6-000006 |
| 3 | volatile | observed volatile Disable start on user but no branch applies it | cursed_body_sleep_talk | 4 | 0 | 4 | i-04845ae510c0280d3 / b007/g4/20260812T060324-l6-000072<br>i-059714674885bf5ed / b001/g15/20260812T043835-l2-000086<br>i-0cc7d43b357302299 / b001/g10/20260812T043837-l6-000068 |
| 4 | item | observed item loss (Booster Energy) on user but no branch removes it | booster_energy_cloud_nine | 4 | 0 | 4 | i-0dc172ad87fd30d1a / b001/g15/20260812T043819-l3-000009<br>i-0dcec37c240ba5671 / b003/g6/20260812T050939-l5-000075<br>i-0f1f71586ce8c32ff / b004/g11/20260812T052323-l6-000006 |
| 5 | item | observed item loss (Booster Energy) on opp but no branch removes it | booster_energy_cloud_nine | 4 | 0 | 4 | i-0dc172ad87fd30d1a / b001/g15/20260812T043819-l3-000009<br>i-0dcec37c240ba5671 / b003/g6/20260812T050939-l5-000075<br>i-0f1f71586ce8c32ff / b004/g11/20260812T052323-l6-000006 |
| 6 | status | observed par on opp but no branch applies it [membership N/N legal actions reproduce] | unclassified | 3 | 0 | 3 | i-03b2652e9e4861e93 / b006/g10/20260812T054817-l6-000064<br>i-08f1bea77af54fa4e / b002/g2/20260812T045146-l4-000033<br>i-09034a94d4a578ef8 / b003/g2/20260812T050630-l2-000082 |
| 7 | boost | observed - def on opp but no branch produces it | unclassified | 3 | 0 | 3 | i-041d165646def21ce / b000/g1/20260812T065019-l5-000017<br>i-08472e297c47a3fb7 / b001/g5/20260812T043916-l2-000086<br>i-0bd7329a3180ca64d / b008/g4/20260812T061647-l3-000003 |
| 8 | boost | observed - spd on opp but no branch produces it | unclassified | 3 | 0 | 3 | i-041d165646def21ce / b000/g1/20260812T065019-l5-000017<br>i-08472e297c47a3fb7 / b001/g5/20260812T043916-l2-000086<br>i-0bd7329a3180ca64d / b008/g4/20260812T061647-l3-000003 |
| 9 | boost | observed - def on user but no branch produces it | unclassified | 3 | 0 | 3 | i-041d165646def21ce / b000/g1/20260812T065019-l5-000017<br>i-08472e297c47a3fb7 / b001/g5/20260812T043916-l2-000086<br>i-0bd7329a3180ca64d / b008/g4/20260812T061647-l3-000003 |
| 10 | boost | observed - spd on user but no branch produces it | unclassified | 3 | 0 | 3 | i-041d165646def21ce / b000/g1/20260812T065019-l5-000017<br>i-08472e297c47a3fb7 / b001/g5/20260812T043916-l2-000086<br>i-0bd7329a3180ca64d / b008/g4/20260812T061647-l3-000003 |
| 11 | status | observed tox on user but no branch applies it | beat_up_substitute | 2 | 0 | 2 | i-01c0cbf75c89ba9a8 / b005/g4/20260812T053129-l4-000068<br>i-0b74e493efe59e1ec / b005/g12/20260812T053650-l6-000074 |
| 12 | item | observed item loss (Focus Sash) on opp but no branch removes it [membership N/N legal actions reproduce] | unclassified | 2 | 0 | 2 | i-02f01ed666ca9089c / b009/g9/20260812T062426-l2-000071<br>i-0f43ec34c40e0f83b / b009/g15/20260812T062727-l3-000039 |
| 13 | item | observed item loss (Sitrus Berry) on opp but no branch removes it | unclassified | 0 | 2 | 2 | i-05c3503b2c9659367 / b007/g1/20260812T060033-l3-000022<br>i-0649cc4359c805377 / b001/g4/20260812T044206-l2-000060 |
| 14 | item | observed item loss (Sitrus Berry) on user but no branch removes it | unclassified | 0 | 2 | 2 | i-05c3503b2c9659367 / b007/g1/20260812T060033-l3-000022<br>i-0649cc4359c805377 / b001/g4/20260812T044206-l2-000060 |
| 15 | boost | observed + spe on user but no branch produces it [ko-margin] | unclassified | 0 | 2 | 2 | i-08a8cdc31c0c99872 / b004/g7/20260812T051813-l5-000053<br>i-0a598c0100d4cadc1 / b000/g6/20260812T065016-l3-000003 |
| 16 | boost | observed + spa on opp but no branch produces it | unclassified | 2 | 0 | 2 | i-098a6d5ae9c2f5ef9 / b002/g4/20260812T045216-l3-000033<br>i-0e592cb05bb9d3a92 / b001/g4/20260812T043854-l2-000062 |
| 17 | boost | observed + spa on user but no branch produces it | unclassified | 2 | 0 | 2 | i-098a6d5ae9c2f5ef9 / b002/g4/20260812T045216-l3-000033<br>i-0e592cb05bb9d3a92 / b001/g4/20260812T043854-l2-000062 |
| 18 | boost | observed - atk on user but no branch produces it [ko-margin] | unclassified | 0 | 1 | 1 | i-005adc19f61405de9 / b006/g15/20260812T055341-l4-000080 |
| 19 | boost | observed + atk on user but no branch produces it [ko-margin] | unclassified | 0 | 1 | 1 | i-00cabc96530ceb0d2 / b003/g13/20260812T050905-l3-000027 |
| 20 | boost | observed + atk on opp but no branch produces it [ko-margin] | unclassified | 0 | 1 | 1 | i-00cabc96530ceb0d2 / b003/g13/20260812T050905-l3-000027 |
| 21 | boost | observed - atk on user but no branch produces it | unclassified | 1 | 0 | 1 | i-01bf39e3c9478af89 / b005/g14/20260812T053120-l4-000018 |
| 22 | volatile | observed volatile Disable start on opp but no branch applies it | cursed_body_sleep_talk | 1 | 0 | 1 | i-059714674885bf5ed / b001/g15/20260812T043835-l2-000086 |
| 23 | status | observed tox on opp but no branch applies it [ko-margin] | unclassified | 0 | 1 | 1 | i-05b586360961cef63 / b001/g0/20260812T043839-l2-000025 |
| 24 | status | observed tox on user but no branch applies it [ko-margin] | unclassified | 0 | 1 | 1 | i-05b586360961cef63 / b001/g0/20260812T043839-l2-000025 |
| 25 | volatile | observed volatile Heal Block start on user but no branch applies it | heal_block_psychic_noise | 1 | 0 | 1 | i-0649cc4359c805377 / b000/g8/20260812T042515-l2-000002 |
| 26 | volatile | observed volatile Heal Block start on opp but no branch applies it | heal_block_psychic_noise | 1 | 0 | 1 | i-0649cc4359c805377 / b000/g8/20260812T042515-l2-000002 |
| 27 | volatile | observed volatile Substitute start on user but no branch applies it | heal_block_psychic_noise | 1 | 0 | 1 | i-06907ed6ea89577f1 / b002/g7/20260812T045628-l6-000012 |
| 28 | boost | observed - spe on opp but no branch produces it | unclassified | 1 | 0 | 1 | i-06907ed6ea89577f1 / b006/g8/20260812T054824-l2-000084 |
| 29 | item | observed item loss (Choice Band) on opp but no branch removes it | unclassified | 1 | 0 | 1 | i-084209bdd054ea2b2 / b003/g8/20260812T050446-l2-000052 |
| 30 | item | observed item loss (Choice Band) on user but no branch removes it | unclassified | 1 | 0 | 1 | i-084209bdd054ea2b2 / b003/g8/20260812T050446-l2-000052 |
| 31 | boost | observed - def on user but no branch produces it [ko-margin] | unclassified | 0 | 1 | 1 | i-08a8cdc31c0c99872 / b004/g7/20260812T051813-l5-000053 |
| 32 | boost | observed + spe on opp but no branch produces it [ko-margin] | unclassified | 0 | 1 | 1 | i-0a598c0100d4cadc1 / b000/g6/20260812T065016-l3-000003 |
| 33 | status | observed brn on user but no branch applies it | unclassified | 1 | 0 | 1 | i-0c4660c30a9917614 / b004/g6/20260812T051739-l5-000083 |
| 34 | status | observed par on opp but no branch applies it | beat_up_substitute | 1 | 0 | 1 | i-0c795a07ea08ec930 / b002/g12/20260812T045249-l3-000034 |
| 35 | status | observed par on user but no branch applies it | beat_up_substitute | 1 | 0 | 1 | i-0c795a07ea08ec930 / b002/g12/20260812T045249-l3-000034 |
| 36 | volatile | observed volatile confusion end on opp but no branch removes it | unclassified | 0 | 1 | 1 | i-0f1f71586ce8c32ff / b000/g13/20260812T042522-l2-000033 |
| 37 | volatile | observed volatile confusion end on user but no branch removes it | unclassified | 0 | 1 | 1 | i-0f1f71586ce8c32ff / b000/g13/20260812T042522-l2-000033 |
## per-soft triage verdicts (14 distinct soft events)

| soft signature | events | verdict |
|---|---|---|
| any signature carrying `[ko-margin]` (`-atk` rotomfan/gardevoir, `+atk` swordsdance/ragefist, `-def`+`+spe` bitterblade/knockoff, `+spe` flareblitz/gigadrain, `tox` terablast-tera/zenheadbutt) | 8 | **checker-demotion artifact.** These are the pre-existing KO-boundary margin machinery: the checker itself demoted them because the observation sits on a KO boundary where the engine's folded damage rolls cannot decide. Not engine defects; expected to survive any engine fix. |
| Sitrus Berry item-loss + paired heal, ordinary damage (pyroar/flamethrower, calyrexice/hypervoice) | 4 | **real**, same root cause as the item-application family the fix wave is already addressing — damage crosses the 50% threshold, PS eats the berry, engine removes neither item nor applies the heal. Only the trigger differs (ordinary damage, not Belly Drum). Should clear with E's item fixes; if it does not, it is a distinct threshold bug. |
| heal on the Struggle side (struggle/alluringvoice, struggle/thunderbolt, struggle/suckerpunch) | 1 | **real but attribution unconfirmed.** A heal PS applies that no engine branch produces, on turns involving Struggle. Needs the block dump to separate a residual-heal ordering issue from a Struggle-recoil interaction. Low confidence, single distinct event. |
| volatile confusion end (revelationdance/roost) | 1 | **real, singleton.** PS ends confusion, no engine branch removes it. |

## flag: what sits outside the five expected clusters

The four clusters that matched account for **13 of 44 distinct events** (all hard).
`switchin_update_timing` matched **zero** on my keyword heuristic -- I do not know
that cluster's message signature, so some of the residue below plausibly belongs
to it rather than being new.

Nothing outside the known clusters appears in **volume**. The residue is 31 distinct
events, and its two largest coherent groups are:

1. **Close Combat / Headlong Rush self `-def`/`-spd` not produced** -- 6 distinct
   events (4 closecombat, 2 headlongrush) across 3 games. Both moves share the
   same self-lowering secondary, so this is one mechanic and is the only residue
   group large enough to deserve a cluster name. It is conditional, not general:
   Close Combat is ubiquitous and a blanket defect would have produced thousands
   of corpus findings, not four.
2. **Double Shock -> par not applied** -- 3 distinct events across 3 games
   (`[membership 0/N legal actions reproduce]`).

Everything else is singletons or p1/p2 pairs: Focus Sash x2, Choice Band x1,
`+spa` slackoff/fierywrath, `+spa` rest-tera/tripleaxel, `brn` taunt/willowisp,
`-atk` liquidation/bittermalice, `-spe` wugtrio/dualwingbeat.
