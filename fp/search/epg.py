"""Endgame Playout Gate (Sally 2026-08-18).

In small-roster positions the value net is combinatorially blind (matchup-swap
probe: roster-ordering at chance) while playout verdicts are cheap and stable
(playout study: >=95% pick-stability at alive<=5, 5k iters; iterconv: depth
beyond ~2.5k buys nothing in this domain). So: when the gate triggers, the
final move is decided by FORCED-ARM PLAYOUTS instead of the search argmax.

Spec (Sally): trigger = total alive <= 6 AND best pooled Q in [0.05, 0.95]
(+ a blitz clock guard). Candidates = top 4 arms by pooled visit share, NO
visit floor, incumbent always included. Playouts: 2.5k iters/move, opp argmax,
our first move overridden with the search left UNCOMMITTED (simultaneous-move
model at every step, matching the tree; Sally 2026-08-18), spread over up to
8 sampled worlds, CRN seeds shared across candidates. Playouts per arm (TOTAL, spread over up to 8 worlds) by total alive:
    2 -> 16, 3 -> 24, 4 -> 32, 5 -> 40, 6 -> 48  (Sally 2026-08-18: doubled,
    all-8-world coverage — the real blitz budget is ~9s/turn over a short
    endgame stretch).
Strict argmax of pooled playout means (no incumbent preference). Any
failure or timeout returns the incumbent unchanged (fail-safe).
"""
import concurrent.futures
import logging
import random
import time

from config import FoulPlayConfig
from fp.search.executor import _get_search_executor

logger = logging.getLogger(__name__)

EPG_ALIVE_MAX = 6
EPG_Q_LO, EPG_Q_HI = 0.05, 0.95
EPG_ITERS = 2500
# screening search: replaces the 4.5s timed search when the gate will run --
# 5k iterations per world just to RANK candidates (playouts decide the move).
EPG_SCREEN_ITERS = 5000
EPG_N_BY_ALIVE = {2: 16, 3: 24, 4: 32, 5: 40, 6: 48}
EPG_CANDIDATES = 4
EPG_MAX_WORLDS = 8
EPG_MAX_STEPS = 300
EPG_WALL_BUDGET_S = 8.0
EPG_MIN_PER_ARM = 4
# clock-aware budget (Sally 2026-08-19, allotment model): the blitz timer is
# quantized -- finish a turn with >=6s of its allotment left and the next turn
# is a fresh 15s; finish with 1-5s left and the next turn is only 10s; run out
# and it is an inactivity forfeit. The caller passes the LOCALLY TRACKED turn
# allotment (fp/search/main.py) as time_remaining, and the margin reserves the
# 6s no-penalty threshold plus ~1s of post-gate overhead (choice send, and the
# pool-backlog spikes observed at 2s on back-to-back activations). Budget =
# min(8s, allotment - spent - margin): a fresh 15s turn funds a ~7.8s wall and
# STILL returns to 15s next turn; a 10s penalty turn funds ~2.8s and recovers.
EPG_CLOCK_MARGIN_S = 7.0
EPG_MIN_BUDGET_S = 2.0
# paired-noise guard bar, in paired standard errors (Sally 2026-08-19: 1.0 ->
# 0.5). At 1.0, a challenger whose ONLY evidence was a single differing CRN
# seed landed exactly ON the bar every time (one nonzero diff among n gives
# md/SE = 1) and always lost the tie -- in dead-lost positions the gate could
# never move to the only arm that ever won a playout. 0.5 lets one clean seed
# of evidence flip the pick while still vetoing sub-half-SE noise.
EPG_GUARD_SE = 0.5

# state string layout (see opp_model_lab): sides are the first two '/'-separated
# segments, each '='-joined with the 6 mon substrings first; mon field 6 = hp.
_MON_HP_FIELD = 6


def _total_alive(state_str: str) -> int:
    alive = 0
    for seg in state_str.split("/")[:2]:
        for mon in seg.split("=")[:6]:
            if float(mon.split(",")[_MON_HP_FIELD]) > 0:
                alive += 1
    return alive


def epg_playout(args):
    """Pool worker: one forced-first-move playout to termination. Engine-only
    (no fp imports) so it runs in the search pool workers as-is."""
    state_str, first_arm, seed, iters, max_steps = args
    from poke_engine import State, generate_instructions, monte_carlo_tree_search

    def to_move(a):
        # generate_instructions mapping (mirrors mine_value.arm_to_move): the
        # engine names a forced-pass arm "No Move", whose move string is
        # "none" -- without this mapping every playout that crossed a
        # KO-replacement turn raised and returned 0.5 (2026-08-18). Switches
        # are stripped to the bare species name, which is what
        # generate_instructions wants.
        a = a.lower()
        if a in ("no move", "nomove"):
            return "none"
        return a[7:] if a.startswith("switch ") else a

    # Step 0 is deliberately UNCOMMITTED (Sally 2026-08-18): the search runs
    # unrestricted and the opponent's reply is its argmax against our MIX --
    # the same simultaneous-move model as every later step and as the tree
    # itself. The forced arm is only OVERRIDDEN into p1 below; the opponent
    # "predicts" it exactly as much as the equilibrium prices it in. (A
    # forced_side_one_move commitment scores the arm against a perfect
    # predictor -- a security value the rest of the system never uses.)
    rng = random.Random(seed)
    state = State.from_string(state_str)
    forced = first_arm
    for step in range(max_steps):
        if not any(p.hp > 0 for p in state.side_one.pokemon):
            return 0.0
        if not any(p.hp > 0 for p in state.side_two.pokemon):
            return 1.0
        try:
            res = monte_carlo_tree_search(
                state, 0, iters, 1, (seed * 7919 + step) & 0x7FFFFFFF
            )
        except BaseException:
            return 0.5
        s1 = [m for m in res.side_one if m.visits > 0]
        s2 = [m for m in res.side_two if m.visits > 0]
        if not s1 or not s2:
            return 0.5
        p1 = forced if forced else max(s1, key=lambda m: m.visits).move_choice
        p2 = max(s2, key=lambda m: m.visits).move_choice
        forced = None
        try:
            branches = [
                b
                for b in generate_instructions(state, to_move(p1), to_move(p2))
                if b.percentage > 0
            ]
        except BaseException:
            return 0.5
        if not branches:
            return 0.5
        pick = rng.choices(branches, weights=[b.percentage for b in branches])[0]
        nxt = state.apply_instructions(pick)
        if not any(p.hp > 0 for p in nxt.side_one.pokemon) and not any(
            p.hp > 0 for p in nxt.side_two.pokemon
        ):
            return 0.5  # simultaneous wipe: ambiguous, split the point
        state = nxt
    a1 = sum(p.hp > 0 for p in state.side_one.pokemon)
    a2 = sum(p.hp > 0 for p in state.side_two.pokemon)
    return 1.0 if (a1 > 0 and a2 == 0) else 0.0 if (a2 > 0 and a1 == 0) else 0.5


def screening_iterations(states, battle):
    """If the gate is armed and the roster trigger holds, return the
    fixed-iteration screening budget for the main search (0 = normal timed
    search). Q-band and clock checks still happen in maybe_playout_gate; a
    screened turn that then skips playouts simply plays the screening argmax
    -- at <=6 alive a 5k-iteration argmax is a fine fallback and the clock
    keeps the savings."""
    if not getattr(FoulPlayConfig, "endgame_playout_gate", 0):
        return 0
    if getattr(battle, "team_preview", False):
        return 0
    try:
        alive = _total_alive(states[0][0])
    except Exception:
        return 0
    return EPG_SCREEN_ITERS if 2 <= alive <= EPG_ALIVE_MAX else 0


def maybe_playout_gate(choice, states, mcts_results, time_remaining, spent_s=0.0):
    """Returns the (possibly replaced) final choice. See module docstring."""
    if not getattr(FoulPlayConfig, "endgame_playout_gate", 0):
        return choice
    try:
        alive = _total_alive(states[0][0])
    except Exception:
        return choice
    if not (2 <= alive <= EPG_ALIVE_MAX):
        return choice
    budget = EPG_WALL_BUDGET_S
    if time_remaining is not None:
        budget = min(budget, time_remaining - spent_s - EPG_CLOCK_MARGIN_S)
    if budget < EPG_MIN_BUDGET_S:
        logger.info(
            "EPG: skipped (clock {}s, spent {:.1f}s -> budget {:.1f}s)".format(
                time_remaining, spent_s, budget
            )
        )
        return choice
    # chance-weighted pooled shares and scores, same aggregation as selection
    share, score, wsum = {}, {}, {}
    for mcts_result, sample_chance, index in mcts_results:
        tv = max(mcts_result.total_visits, 1)
        for o in mcts_result.side_one:
            share[o.move_choice] = share.get(o.move_choice, 0.0) + (
                sample_chance * o.visits / tv
            )
            if o.visits > 0:
                score[o.move_choice] = score.get(o.move_choice, 0.0) + (
                    sample_chance * o.total_score / o.visits
                )
                wsum[o.move_choice] = wsum.get(o.move_choice, 0.0) + sample_chance
    if not share:
        return choice
    best_q = max(
        (score[a] / wsum[a] for a in score if wsum[a] > 0), default=0.5
    )
    if not (EPG_Q_LO <= best_q <= EPG_Q_HI):
        return choice
    # candidates in visit-share order; the incumbent gets a slot but NO
    # preferential ordering (Sally 2026-08-18: playouts are the better model —
    # pure argmax of playout means, exact float ties fall to the higher-visit
    # arm only because it sorts first).
    cands = sorted(share, key=share.get, reverse=True)[:EPG_CANDIDATES]
    if choice not in cands:
        cands = cands[: EPG_CANDIDATES - 1] + [choice]
    if len(cands) < 2:
        return choice
    n_per_arm = EPG_N_BY_ALIVE.get(alive, 24)
    worlds = sorted(states, key=lambda sc: -sc[1])[:EPG_MAX_WORLDS]
    t0 = time.time()
    # k-major job order (arm round-robin) so partial completion stays BALANCED
    # across arms: deciding on whatever finished inside the wall budget is then
    # a CRN prefix per arm -- statistically a smaller n, which the convergence
    # study validated. n_per_arm is the CAP; EPG_WALL_BUDGET_S is the cost.
    uniq = [w[0] for w in worlds]
    jobs = []  # (ci, k, state_idx, arm, seed)
    for k in range(n_per_arm):
        for ci, arm in enumerate(cands):
            jobs.append((ci, k, k % len(uniq), arm, 9500 + k))
    # per-seed outcomes for the paired-noise guard: outcomes[ci][k] is seed
    # k's result for candidate ci (CRN: same seed = same luck across arms)
    outcomes = [{} for _ in cands]
    via = "local"
    from fp.search import remote as _remote

    if getattr(FoulPlayConfig, "search_remote_url", None) and not _remote.remote_disabled():
        # One batched round trip; the server runs the same budget-truncated
        # k-major collection on ITS pool (the whole point: a 32+ core box funds
        # the alive-6 caps that starve on 8 local workers). wall_s leaves the
        # transfer+RTT inside our budget; the timeout bounds the total trip.
        res = _remote.remote_epg_playouts(
            uniq,
            [[si, arm, seed] for _, _, si, arm, seed in jobs],
            EPG_ITERS,
            EPG_MAX_STEPS,
            wall_s=max(0.5, budget - 1.5),
            timeout_s=budget + 2.5,
        )
        if res is None:
            # Transport failure: the timeout already spent this turn's playout
            # budget, so a local re-run would double-spend the clock (the
            # near-timeout lesson). Keep the incumbent; the tripped breaker
            # sends every later turn straight down the local path.
            logger.warning("EPG: remote playouts failed; keeping {}".format(choice))
            return choice
        for (ci, k, _, _, _), r in zip(jobs, res):
            if r is not None:
                outcomes[ci][k] = float(r)
        via = "remote"
    else:
        pool = _get_search_executor()
        futures_meta = {}
        for ci, k, si, arm, seed in jobs:
            fut = pool.submit(
                epg_playout, (uniq[si], arm, seed, EPG_ITERS, EPG_MAX_STEPS)
            )
            futures_meta[fut] = (ci, k)
        pending = set(futures_meta)
        try:
            while pending:
                left = budget - (time.time() - t0)
                if left <= 0:
                    break
                done, pending = concurrent.futures.wait(
                    pending, timeout=min(left, 0.25),
                    return_when=concurrent.futures.FIRST_COMPLETED,
                )
                for fut in done:
                    ci, k = futures_meta[fut]
                    outcomes[ci][k] = fut.result()
        except Exception as e:
            for fut in pending:
                fut.cancel()
            logger.warning(
                "EPG: verification failed ({!r}); keeping {}".format(e, choice)
            )
            return choice
        for fut in pending:
            fut.cancel()
    sums = [sum(o.values()) for o in outcomes]
    counts = [len(o) for o in outcomes]
    if min(counts) < EPG_MIN_PER_ARM:
        logger.info(
            "EPG: budget hit with insufficient samples (counts={}); keeping {}".format(
                counts, choice
            )
        )
        return choice
    means = [s / c for s, c in zip(sums, counts)]
    # PAIRED-NOISE GUARD (Sally 2026-08-18, after the terablast loss): a
    # challenger displaces the incumbent only if its CRN-paired mean advantage
    # exceeds EPG_GUARD_SE paired standard errors -- i.e. only when the
    # playouts have actually MEASURED a difference, not sampled one.
    # Same-seed pairing cancels shared luck, so this is the tightest honest
    # error bar.
    inc_i = cands.index(choice) if choice in cands else 0
    best_i = max(range(len(cands)), key=lambda i: means[i])
    if best_i != inc_i:
        shared = sorted(set(outcomes[best_i]) & set(outcomes[inc_i]))
        if len(shared) >= 2:
            diffs = [outcomes[best_i][k] - outcomes[inc_i][k] for k in shared]
            md = sum(diffs) / len(diffs)
            var = sum((d - md) ** 2 for d in diffs) / (len(diffs) - 1)
            se = (var / len(diffs)) ** 0.5
            if md <= EPG_GUARD_SE * se:
                logger.info(
                    "EPG: challenger {} inside noise (paired d={:+.3f} SE={:.3f}); "
                    "keeping {}".format(cands[best_i], md, se, choice)
                )
                best_i = inc_i
    pick = cands[best_i]
    logger.info(
        "EPG[{}]: alive={} done/arm={} worlds={} wall={:.2f}s  {}  -> {}{}".format(
            via,
            alive,
            "/".join(str(c) for c in counts),
            len(worlds),
            time.time() - t0,
            "  ".join(
                "{}={:.2f}".format(a, m) for a, m in zip(cands, means)
            ),
            pick,
            "" if pick == choice else "  (REPLACED {})".format(choice),
        )
    )
    return pick
