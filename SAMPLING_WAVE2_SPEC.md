# Sampling Wave 2 — spec (approved 2026-08-03, scheduled AFTER gate attempt 8)

Six items, all deductive (eliminate impossible sets / use known information) —
no behavioral guessing. Shared constraint for every item: live-only paths stay
behind the existing gates (`zoroark_inference_allowed` / `exact_roster_known`),
one unit test per item, full 168-game conformance regression before merge.
Implementation order = the order below (item 1 is the safety floor for 2–4).

## 1. Crit/hitcount dispatch hygiene [S] — FIRST, prerequisite
Add `-crit` and `-hitcount` handlers to the dispatch table
(fp/battle_modifier.py:~5554-5605). `-crit`: stamp a per-turn flag on the
defender; `-hitcount`: stamp the count. Every damage consumer (items 2-4,
`_substitute_absorbed_damage_interval`) reads the uniform flags instead of
re-scanning lines. Rationale: consumers currently re-derive crit independently
and can disagree; a mis-selected crit arm silently poisons an elimination —
a FALSE elimination (excluding the true set) is the one failure mode this
whole wave must never have.

## 2. Exact inbound roll-set membership (opponent → us) [M] — crown jewel
In `_do_check` / `get_damage_dealt` (fp/inference.py:445-573): when the
defender is OUR mon, the damage delta is an exact integer (our HP is exact).
Replace the ±2.5%+5 acceptance band with PS-exact 16-roll set membership per
candidate set: populate a battle copy per candidate, derive the roll set
(reuse fp/replay/damage_membership.py machinery — already exists and is
gate-proven), eliminate candidates whose roll set cannot produce the observed
integer (respecting the item-1 crit flag; lethal/capped observations use the
lower-bound semantics already shipped in wave 1). Persist eliminations via
`rejected_set_signatures` (producer shipped in wave 1). Runtime: one damage
calc per candidate per opponent hit — microseconds vs the 1.5s budget.
Safety: the contradiction discipline shipped in wave 1 (refusal counters,
drop the inconsistent observation not the filter) backstops this.

## 3. Chip-damage max-HP sieve [M]
Every candidate set fixes max HP deterministically (data/pkmn_sets.py:341-430).
Every residual is exact integer arithmetic on max HP: leftovers/blacksludge
heal max//16, burn max//16, poison max//8, toxic n*max//16, sandstorm max//16,
lifeorb max//10, stealthrock max*mult//8, substitute cost max//4, (add:
spikes tiers, grassy terrain max//16 heal). For each observed opponent
percent transition (a/100 -> b/100) under a known formula c(max), the set of
max values consistent with the display rounding is computable; intersect per
mon across events (running [lo, hi] band on max HP is sufficient — formulas
are monotone in max). Eliminate candidates whose max HP falls outside the
band. Wire into the residual/upkeep handlers in fp/battle_modifier.py that
already parse these events. Display-rounding model must match PS exactly
(sim/pokemon.ts getHealth: /100 with its specific rounding) — write the
rounding helper once, unit-test it against protocol samples from corpora.
Fires from turn 1-2, before any attack lands.

## 4. Absolute-HP display sieve [S] — same machinery as 3
Events that pin the opponent's ABSOLUTE HP: Endeavor (set equal to our exact
HP), Pain Split (average, one side exact), Super Fang (exact halving),
plus full-heal-to-100 markers. Each yields max ∈ [hp/((b+0.5)/100),
hp/((b-0.5)/100)] style bands from the displayed percent (use the exact PS
rounding helper from item 3). Intersect into the same per-mon max-HP band.
One Endeavor event typically eliminates all but one candidate level.

## 5. Stratified (largest-remainder) world allocation [M]
fp/search/random_battles.py:300-310: replace the per-world independent
`random.choices` draw for the opponent's ACTIVE with largest-remainder
allocation of the `num_battles` worlds across candidate sets (normalized
entry_weighted_counts): top-k sets get floor/ceil shares, each world stamped
with the set's true probability as its `sample_chance` (consumed at
fp/search/selection.py:36-65 — verify the weighting flows through). Any set
with posterior >= 1/(2N) is guaranteed representation on every decision.
Non-active revealed mons and fill-ins keep the existing independent draws
(their variance matters far less). Deterministic given the posterior — also
slightly faster than repeated random.choices. Unit test: fixed posterior in,
exact allocation out; plus a chance-sums-to-1 invariant.

## 6. Pivot second-half move forwarding [S]
fp/search/poke_engine_helpers.py:579 hard-codes
`switch_out_move_second_saved_move="NONE"`. When our fast switch-out move
resolves and the opponent's move for this turn has ALREADY EXECUTED (visible
in this turn's protocol before our replacement choice), forward that move
instead of NONE so the engine's remaining half-turn is deterministic. Use the
existing `opponent_switchout_move_stayed_in` computation as the pattern.
Strictly information already in hand; eliminates hedging against moves that
cannot occur this half-turn.

## Explicitly deferred (do NOT implement in this wave)
Reverse us->opp band-intersection tightening; un-refusing Foul Play/Fickle
Beam/Shell Side Arm exact derivations; Substitute back-propagation; joint
team-legality on revealed mons (adds retry cost inside the sampling step);
protocol-only Illusion proofs (subsumed by belief-state search later); all
behavioral reweighting (graded likelihood, move-nonuse, entry intent).
