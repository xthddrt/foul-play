import logging
import math
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from copy import deepcopy

from constants import BattleType
from fp.battle import Battle
from config import FoulPlayConfig
from .standard_battles import prepare_battles
from .random_battles import prepare_random_battles

from poke_engine import State as PokeEngineState, monte_carlo_tree_search, MctsResult

from fp.search.poke_engine_helpers import battle_to_poke_engine_state

logger = logging.getLogger(__name__)


def _t_sf(t_stat: float, df: int) -> float:
    """One-tailed survival function of Student's t (numeric integration)."""
    if t_stat < 0:
        return 1.0 - _t_sf(-t_stat, df)
    coeff = math.gamma((df + 1) / 2) / (math.sqrt(df * math.pi) * math.gamma(df / 2))

    def pdf(x):
        return coeff * (1 + x * x / df) ** (-(df + 1) / 2)

    steps, lo, hi = 2000, t_stat, t_stat + 60.0
    h = (hi - lo) / steps
    total = pdf(lo) + pdf(hi)
    for i in range(1, steps):
        total += (4 if i % 2 else 2) * pdf(lo + i * h)
    return total * h / 3


def select_move_from_mcts_results(
    mcts_results: list[(MctsResult, float, int)],
    revealed_opponent_names: set[str] | None = None,
) -> str:
    # revealed_opponent_names: the lowercased species names of the opponent
    # pokemon actually revealed in the real battle (active + revealed reserve).
    # Used by the losing-position upside tiebreak to reject opponent switch
    # replies into SAMPLED unrevealed (phantom) mons - a phantom matchup is a
    # fiction invented by determinization and must not set an option's
    # conditional ceiling. None (legacy callers / unit tests) => no filtering.
    pooled_share = {}
    blend_sum = {}
    score_weighted_sum = {}
    score_sq_weighted_sum = {}
    score_weight = {}
    world_scores = {}
    pair_stats = {}
    for mcts_result, sample_chance, index in mcts_results:
        this_policy = max(mcts_result.side_one, key=lambda x: x.visits)
        logger.info(
            "Policy {}: {} visited {}% avg_score={} sample_chance_multiplier={}".format(
                index,
                this_policy.move_choice,
                round(100 * this_policy.visits / mcts_result.total_visits, 2),
                round(this_policy.total_score / this_policy.visits, 3),
                round(sample_chance, 3),
            )
        )
        world_stats = []
        for s1_option in mcts_result.side_one:
            choice = s1_option.move_choice
            share = s1_option.visits / mcts_result.total_visits
            pooled_share[choice] = pooled_share.get(choice, 0.0) + (
                sample_chance * share
            )
            if s1_option.visits > 0:
                avg = s1_option.total_score / s1_option.visits
                blend_sum[choice] = blend_sum.get(choice, 0.0) + (
                    sample_chance * share * avg
                )
                score_weighted_sum[choice] = (
                    score_weighted_sum.get(choice, 0.0) + sample_chance * avg
                )
                score_sq_weighted_sum[choice] = (
                    score_sq_weighted_sum.get(choice, 0.0) + sample_chance * avg * avg
                )
                score_weight[choice] = score_weight.get(choice, 0.0) + sample_chance
                # within-world score spread: the variance of the reward
                # samples behind this option's average in THIS world.
                # Catastrophic branches (e.g. an opponent move that OHKOs)
                # survive here even when opponent-model mixing launders
                # them out of the world's average. Needs an engine wheel
                # exposing total_score_sq; older wheels skip it.
                total_score_sq = getattr(s1_option, "total_score_sq", None)
                iw_sd = None
                if total_score_sq is not None:
                    iw_var = total_score_sq / s1_option.visits - avg * avg
                    iw_sd = math.sqrt(max(iw_var, 0.0))
                    world_stats.append((s1_option.visits, choice, share, avg, iw_sd))
                world_scores.setdefault(choice, []).append((index, share, avg, iw_sd))
        if world_stats:
            world_stats.sort(reverse=True)
            logger.info(
                "WorldStats {}: {}".format(
                    index,
                    " | ".join(
                        "{} {}%/{}/±{}".format(
                            choice,
                            round(share * 100, 1),
                            round(avg, 3),
                            round(iw_sd, 3),
                        )
                        for _, choice, share, avg, iw_sd in world_stats
                    ),
                )
            )
        # root joint pair table: outcome of each exact (our move, their move)
        # combination. The per-arm averages collapse the opponent's reply mix,
        # erasing conditional structure ("sucker punch WHEN they attack wins");
        # pooled across worlds this feeds the losing-position upside tiebreak.
        root_pairs = getattr(mcts_result, "root_pairs", None)
        side_two_for_pairs = getattr(mcts_result, "side_two", None)
        if root_pairs and side_two_for_pairs:
            for i, s1_opt in enumerate(mcts_result.side_one):
                if i >= len(root_pairs):
                    break
                for j, s2_opt in enumerate(side_two_for_pairs):
                    if j >= len(root_pairs[i]):
                        break
                    visits, total = root_pairs[i][j]
                    if visits > 0:
                        key = (s1_opt.move_choice, s2_opt.move_choice)
                        pv, pt = pair_stats.get(key, (0.0, 0.0))
                        pair_stats[key] = (
                            pv + sample_chance * visits,
                            pt + sample_chance * total,
                        )

        # the OPPONENT's in-tree policy for this world. This is where the
        # opponent model's beliefs live (e.g. the esmolmightbetaken forensic:
        # worlds believed a 70% switch rate while the human stayed 10 straight
        # turns) - without this line those beliefs are computed and discarded.
        side_two = getattr(mcts_result, "side_two", None)
        if side_two:
            opp_total = sum(o.visits for o in side_two)
            if opp_total > 0:
                opp_stats = sorted(side_two, key=lambda o: -o.visits)[:6]
                logger.info(
                    "OppWorldStats {}: {}".format(
                        index,
                        " | ".join(
                            "{} {}%/{}".format(
                                o.move_choice,
                                round(100 * o.visits / opp_total, 1),
                                round(o.total_score / o.visits, 3) if o.visits else 0.0,
                            )
                            for o in opp_stats
                        ),
                    )
                )

    # sample_chance-weighted mean and std of each choice's avg score across
    # the worlds where it was visited at all. The std measures cross-world
    # disagreement: how much this option's quality depends on which hidden
    # opponent set is real.
    agg_score = {
        choice: score_weighted_sum[choice] / score_weight[choice]
        for choice in score_weight
    }
    score_sd = {}
    for choice in score_weight:
        variance = (
            score_sq_weighted_sum[choice] / score_weight[choice]
            - agg_score[choice] ** 2
        )
        score_sd[choice] = math.sqrt(max(variance, 0.0))

    # trusted worst-world score: min per-world avg over the worlds where the
    # option earned at least 1% of that world's visits (abandoned-arm
    # estimates below the floor are junk - systematically depressed).
    # Falls back to the raw min when no world clears the floor.
    score_min = {}
    for choice, entries in world_scores.items():
        trusted = [avg for _, share, avg, _ in entries if share >= 0.01]
        score_min[choice] = (
            min(trusted) if trusted else min(a for _, _, a, _ in entries)
        )

    # Damp voluntary switch options: perfect-information determinization and
    # the variance-free deep search both overvalue switching (see the switch
    # multiplier config option). Turns where every option is a switch
    # (forced switches, revive prompts, team preview) are left untouched.
    damped_share = dict(pooled_share)
    damped_blend = dict(blend_sum)
    if FoulPlayConfig.switch_weight_multiplier != 1.0 and any(
        not k.startswith("switch ") for k in pooled_share
    ):
        for k in damped_share:
            if k.startswith("switch "):
                damped_share[k] *= FoulPlayConfig.switch_weight_multiplier
                if k in damped_blend:
                    damped_blend[k] *= FoulPlayConfig.switch_weight_multiplier

    # Risk-averse per-world blending:
    #   F(c) = sum_w p_w * v_w(c) * s_w(c)  -  lambda * V(c) * sd(s_w(c))
    # The first term blends visit share with avg score INSIDE each sampled
    # world before averaging (an option only gets credit for a world's score
    # in proportion to how hard that world's search endorsed it). The penalty
    # taxes options whose score varies across worlds - gambles on hidden
    # information (the T20 double-shell-smash loss: great in the sampled
    # worlds where Pincurchin had no Discharge, fatal in the ones where it
    # did). The penalty is scaled by the option's own pooled share: a bare
    # -lambda*sd penalty drives all real contenders negative and lets
    # never-searched options (zero mean, zero variance) win the argmax.
    lam = FoulPlayConfig.variance_penalty_lambda
    option_value = {
        choice: damped_blend.get(choice, 0.0)
        - lam * damped_share[choice] * score_sd.get(choice, 0.0)
        for choice in damped_share
    }

    # Negative-regime guard: F(c) ~ share * (score - lambda*sd). For any
    # risk-negative option the share factor flips from a weight into a
    # penalty, so rankings that include one are corrupted - the argmax
    # drifts toward whatever the search liked LEAST (Heavyb00tydoots69 T22
    # Taunt-over-Earthquake; MmoC1 T26 DragonDance-over-Outrage, where a
    # single barely-positive low-sd switch kept the old all-negative guard
    # from firing while 6/6 worlds voted to attack). If ANY option's value
    # is <= 0, suspend the penalty for this decision and rank every option
    # by the risk-neutral per-world blend instead. At lambda=0 there is no
    # penalty to suspend (the values ARE the plain blend already), so the
    # guard is skipped entirely rather than logging a no-op re-ranking.
    if lam > 0 and option_value and min(option_value.values()) <= 0.0:
        logger.info(
            "Variance penalty suspended: risk-negative option present "
            "(min value {})".format(round(min(option_value.values()), 4))
        )
        option_value = {
            choice: damped_blend.get(choice, 0.0) for choice in damped_share
        }

    ranked = sorted(option_value.items(), key=lambda x: x[1], reverse=True)

    # Forensic log: EVERY option, best-value first. Selection eligibility
    # is decided separately below; nothing is filtered out of this log.
    logger.info("Considered Choices:")
    for choice, value in ranked:
        logger.info(
            "\t{}: share={}% score={} sd={} min={} value={}".format(
                choice,
                round(damped_share[choice] * 100, 3),
                round(agg_score.get(choice, 0.0), 3),
                round(score_sd.get(choice, 0.0), 3),
                round(score_min[choice], 3) if choice in score_min else None,
                round(value, 4),
            )
        )

    # Switch/tera score-dominance gate: a voluntary switch or a tera/mega
    # variant is only ELIGIBLE for selection when its aggregated avg score is
    # within the configured tolerance of the best visited plain move (or
    # better). Blended voting can crown a restricted option on visit volume
    # alone (the T14 pattern: a share-leading switch with a clearly worse
    # score); requiring near-parity on score keeps those wins honest. Turns
    # with no visited plain move (forced switch, revive prompt, team preview,
    # all-tera edge) are left ungated.
    def is_restricted(choice: str) -> bool:
        return (
            choice.startswith("switch ")
            or choice.endswith("-tera")
            or choice.endswith("-mega")
        )

    # agg_score only has entries for options visited at least once
    unrestricted = [c for c in option_value if not is_restricted(c) and c in agg_score]
    if unrestricted:
        # baseline = plain moves with at least 5% pooled visit share: a
        # 2-visit plain move's lucky score estimate should not veto sound
        # switches. Falls back to all plain moves if none clear the floor.
        baseline = [c for c in unrestricted if pooled_share.get(c, 0.0) >= 0.05]
        if not baseline:
            baseline = unrestricted
        gate_floor = max(agg_score[c] for c in baseline) - (
            FoulPlayConfig.switch_gate_tolerance
        )
        eligible = set(unrestricted).union(
            c
            for c in option_value
            if is_restricted(c) and agg_score.get(c, 0.0) > gate_floor
        )
        raw_best = ranked[0][0]
        if raw_best not in eligible and is_restricted(raw_best):
            logger.info(
                "Score-gate: disallowed {} (score {} <= gate floor {})".format(
                    raw_best,
                    round(agg_score.get(raw_best, 0.0), 3),
                    round(gate_floor, 3),
                )
            )
        ranked = [i for i in ranked if i[0] in eligible]

    # Tera margin gate: terastallizing (or mega evolving) spends a
    # once-per-battle resource, so beyond the leniency gate above a tera
    # option must BEAT the best non-tera alternative by a real margin -
    # a tie-break-level edge must not spend the resource. Motivating loss
    # (esmolmightbetaken): T1 tera-Fire Flare Blitz won the argmax by
    # 0.006 with five opponent mons unrevealed; the held tera (tera-Steel
    # Haxorus resisting tera-Normal Double-Edge) was the exact endgame
    # answer to the Leafeon that swept us. Baseline = best non-tera option
    # with >=5% pooled share (falls back to all non-tera), mirroring the
    # score-gate's baseline rules. Turns with no non-tera alternative are
    # left ungated.
    tera_margin = FoulPlayConfig.tera_margin_gate
    if tera_margin > 0 and len(ranked) > 1:

        def is_resource_spend(choice: str) -> bool:
            return choice.endswith("-tera") or choice.endswith("-mega")

        non_tera = [c for c, _ in ranked if not is_resource_spend(c) and c in agg_score]
        if non_tera:
            baseline_pool = [
                c for c in non_tera if pooled_share.get(c, 0.0) >= 0.05
            ] or non_tera
            tera_floor = max(agg_score[c] for c in baseline_pool) + tera_margin
            gated = {
                c
                for c, _ in ranked
                if is_resource_spend(c) and agg_score.get(c, 0.0) < tera_floor
            }
            if gated and ranked[0][0] in gated:
                logger.info(
                    "Tera-margin gate: disallowed {} (score {} < floor {})".format(
                        ranked[0][0],
                        round(agg_score.get(ranked[0][0], 0.0), 3),
                        round(tera_floor, 3),
                    )
                )
            filtered = [i for i in ranked if i[0] not in gated]
            if filtered:
                ranked = filtered

    # Significance forfeit: the top pick must be STATISTICALLY better than
    # meaningfully-safer alternatives, or it forfeits to the best of them.
    # Rationale (validated on 9 forensic decisions): the value argmax often
    # wins by noise-level margins over options with visibly smaller
    # within-world outcome spread (iwsd) - e.g. 70%-accuracy Hurricane over
    # guaranteed Boomburst (+0.006, miss = death), tera'd over untera'd
    # kills. A paired t-test across the sampled worlds (world quality
    # cancels in the pairing) asks whether the top pick's per-world score
    # edge is real; if it is not, the safer option is strictly preferable.
    # Legitimately-better aggressive picks pass easily (observed p<0.001).
    # the forfeit prefers the SAFER of two statistically-tied options, which
    # protects leads but is exactly backwards in lost positions (it would veto
    # the swindle) - below the losing-upside threshold it stands down.
    alpha = FoulPlayConfig.significance_forfeit_alpha
    _best_agg_now = max(
        (agg_score.get(c, 0.0) for c, _ in ranked), default=0.0
    )
    if (
        alpha > 0
        and len(ranked) > 1
        and not (
            FoulPlayConfig.losing_upside_threshold > 0
            and _best_agg_now < FoulPlayConfig.losing_upside_threshold
        )
    ):
        best_choice = ranked[0][0]

        def pooled_iwsd(choice):
            # only worlds where the option earned >= 1% of the visits count:
            # a lightly-visited arm's iwsd is biased low (a 1-visit arm has
            # iwsd exactly 0), which would make junk arms look 'safer'.
            # Mirrors the trusted-min floor above; None when no world
            # qualifies, which disqualifies the option from the forfeit.
            sds = [
                e[3]
                for e in world_scores.get(choice, [])
                if e[3] is not None and e[1] >= 0.01
            ]
            return sum(sds) / len(sds) if sds else None

        def beats_at_alpha(top, alt):
            """One-tailed paired t across worlds: True if top > alt at alpha.

            Returns None when there is not enough paired data to test.
            """
            top_by_world = {e[0]: e[2] for e in world_scores.get(top, [])}
            alt_by_world = {e[0]: e[2] for e in world_scores.get(alt, [])}
            common = sorted(set(top_by_world) & set(alt_by_world))
            n = len(common)
            if n < 5:
                return None
            diffs = [top_by_world[w] - alt_by_world[w] for w in common]
            mean_diff = sum(diffs) / n
            sd = math.sqrt(sum((d - mean_diff) ** 2 for d in diffs) / (n - 1))
            if sd == 0.0:
                return mean_diff > 0
            t_stat = mean_diff / (sd / math.sqrt(n))
            return _t_sf(t_stat, n - 1) < alpha

        best_iwsd = pooled_iwsd(best_choice)
        if best_iwsd is not None:
            for alt_choice, _ in ranked[1:]:
                # a challenger needs a real share of the pooled search: junk
                # arms the search abandoned must not qualify as 'safer'
                if pooled_share.get(alt_choice, 0.0) < 0.05:
                    continue
                alt_iwsd = pooled_iwsd(alt_choice)
                if (
                    alt_iwsd is None
                    or alt_iwsd
                    > best_iwsd - FoulPlayConfig.significance_forfeit_iwsd_margin
                ):
                    continue
                verdict = beats_at_alpha(best_choice, alt_choice)
                if verdict is None:
                    continue
                if not verdict:
                    logger.info(
                        "Significance forfeit: {} -> {} (edge not significant "
                        "at alpha={}, iwsd {} vs {})".format(
                            best_choice,
                            alt_choice,
                            alpha,
                            round(best_iwsd, 3),
                            round(alt_iwsd, 3),
                        )
                    )
                    ranked = [i for i in ranked if i[0] == alt_choice] + [
                        i for i in ranked if i[0] != alt_choice
                    ]
                    break

    # Losing-position UPSIDE tiebreak ("swindle preference"): in a lost
    # position the reply-mix averages converge - every option reads ~equally
    # bad against the opponent's best line - and the only thing left to
    # maximize is what happens when the opponent errs. Among near-tied
    # options, re-rank by each option's best explored outcome against any
    # SINGLE opponent reply (the pooled root pair table), with a visit floor
    # so junk cells cannot fake an upside. Forensic: T23 heatwave 0.129 vs
    # suckerpunch 0.128 hid sucker's ~0.5 value against the attack replies
    # the actual human chose (a passed-up ~54% game win).
    upside_fired = False
    upside_threshold = FoulPlayConfig.losing_upside_threshold
    if (
        upside_threshold > 0
        and len(ranked) > 1
        and pair_stats
    ):
        ref_score = max(agg_score.get(c, 0.0) for c, _ in ranked)
        if ref_score < upside_threshold:
            # ONE LINEAR tie band through (dead_zone, dead_zone) and
            # (threshold, 0) - with defaults, (0.05, 0.05) to (0.15, 0), so
            # 0.025 at score 0.10. At and below the dead zone the band equals
            # or exceeds the score itself, so every option competes (this
            # subsumes the old "best-scoring attack" fallback with actual
            # conditional evidence). The worse the position, the less the
            # score differences mean, and the more the conditional ceilings
            # decide.
            dead = FoulPlayConfig.losing_attack_fallback_threshold
            band = dead * (upside_threshold - ref_score) / max(
                upside_threshold - dead, 1e-9
            )
            candidates = [
                c for c, _ in ranked if agg_score.get(c, 0.0) >= ref_score - band
            ]
            if len(candidates) > 1:
                def reply_is_real(their_choice):
                    # An opponent switch into a phantom (sampled unrevealed) mon
                    # is a fictional matchup - the ceiling "if they switch to X
                    # and we crush it" is meaningless when X may not exist and a
                    # good opponent would never walk into it. Keep opponent
                    # MOVES (the active is real) and switches into REVEALED
                    # benched mons; drop switches into unrevealed targets.
                    if revealed_opponent_names is None:
                        return True
                    if their_choice.startswith("switch "):
                        target = their_choice[len("switch ") :].lower()
                        return target in revealed_opponent_names
                    return True

                def upside(choice):
                    row = {
                        b: (v, t)
                        for (a, b), (v, t) in pair_stats.items()
                        if a == choice and reply_is_real(b)
                    }
                    row_visits = sum(v for v, _ in row.values())
                    if row_visits <= 0:
                        return None
                    floor = 0.02 * row_visits
                    vals = [
                        t / v for v, t in row.values() if v >= floor and v > 0
                    ]
                    return max(vals) if vals else None
                scored = [(c, upside(c)) for c in candidates]
                scored = [(c, u) for c, u in scored if u is not None]
                if scored:
                    # exact upside ties (the all-zero dead-position case, where
                    # every pair cell is 0.0) break toward NON-switch options:
                    # the model's zero means "lost inside its rollouts", but
                    # crit/miss/para tails live outside the model and only
                    # attacks harvest them - the one genuine insight of the
                    # retired attack-only fallback, kept.
                    scored.sort(key=lambda x: (-x[1], x[0].startswith("switch ")))
                    winner, best_upside = scored[0]
                    incumbent = ranked[0][0]
                    incumbent_upside = dict(scored).get(incumbent, 0.0)
                    # DISPLACEMENT MARGIN = multiplier x the current band
                    # (defaults 2x: 0.05 at score 0.15, 0.10 at 0.05): the
                    # deeper the position, the bigger the ceiling advantage
                    # demanded to overturn the normal pick - pair-cell
                    # averages are estimates and near-equal ceilings are
                    # noise. A non-switch challenger still displaces a SWITCH
                    # incumbent at equal-or-better upside (the dead-zone
                    # attack preference must survive the margin).
                    margin = (
                        FoulPlayConfig.losing_upside_displacement_multiplier
                        * band
                    )
                    displaces = winner != incumbent and (
                        best_upside - incumbent_upside >= margin
                        or (
                            incumbent.startswith("switch ")
                            and not winner.startswith("switch ")
                            and best_upside >= incumbent_upside
                        )
                    )
                    if displaces:
                        logger.info(
                            "Losing-position upside tiebreak: {} -> {} "
                            "(upside {} vs {})".format(
                                incumbent,
                                winner,
                                round(best_upside, 3),
                                round(incumbent_upside, 3),
                            )
                        )
                        ranked = [i for i in ranked if i[0] == winner] + [
                            i for i in ranked if i[0] != winner
                        ]
                    upside_fired = True

    # Losing-position fallback: when even the best option's aggregated avg
    # score says the game is lost, the visit-share vote tends to favour
    # passive stalling (switches). Real games have crit/miss/para tails that
    # only pay off if you keep attacking, so prefer the best-scoring
    # non-switch option that got a meaningful share of the search.
    # best_choice is the GATED value argmax, so the threshold check runs
    # against the aggregated avg score of the option that would actually be
    # played after the score-dominance gate. Skipped when the upside tiebreak
    # already made the smarter version of this call.
    fallback_threshold = FoulPlayConfig.losing_attack_fallback_threshold
    if upside_fired:
        fallback_threshold = 0
    best_choice = ranked[0][0]
    if fallback_threshold > 0 and agg_score.get(best_choice, 0.0) < fallback_threshold:
        eligible = [
            choice
            for choice, share in pooled_share.items()
            if not choice.startswith("switch ") and share >= 0.05
        ]
        if eligible:
            fallback_choice = max(eligible, key=lambda c: agg_score.get(c, 0.0))
            logger.info(
                "Losing-position fallback fired: best_score={} choosing {} (score {})".format(
                    round(agg_score.get(best_choice, 0.0), 3),
                    fallback_choice,
                    round(agg_score.get(fallback_choice, 0.0), 3),
                )
            )
            return fallback_choice

    # deterministic: always play the single best ELIGIBLE option by
    # variance-penalized per-world blended value
    return ranked[0][0]


# A single ProcessPoolExecutor kept alive across decisions: recreating it
# every decision costs ~parallelism process spawns per move. Workers import
# the poke_engine wheel once when they spawn, so a wheel rebuilt mid-battle
# is NOT silently picked up - the pool is only recreated on breakage and a
# rebuild still requires a bot restart, exactly as documented before.
# Only find_best_move (which runs one-at-a-time in the decision thread)
# touches these globals.
_search_executor: ProcessPoolExecutor | None = None
_search_executor_workers: int | None = None


def _get_search_executor() -> ProcessPoolExecutor:
    global _search_executor, _search_executor_workers
    if (
        _search_executor is None
        or _search_executor_workers != FoulPlayConfig.parallelism
    ):
        if _search_executor is not None:
            _search_executor.shutdown(wait=False, cancel_futures=True)
        logger.info(
            "Spawning search pool with {} workers".format(FoulPlayConfig.parallelism)
        )
        _search_executor = ProcessPoolExecutor(max_workers=FoulPlayConfig.parallelism)
        _search_executor_workers = FoulPlayConfig.parallelism
    return _search_executor


def _discard_search_executor():
    global _search_executor
    if _search_executor is not None:
        _search_executor.shutdown(wait=False, cancel_futures=True)
        _search_executor = None


def gather_mcts_results(
    futures: list,
) -> tuple[list[(MctsResult, float, int)], bool]:
    """Collect per-world (future, sample_chance, index) results.

    One dead worker must not forfeit the battle: worlds whose future raised
    are logged and DROPPED, and the surviving sample chances renormalized so
    the pooled-share floors in selection keep their meaning. Raises only when
    every world died. Returns (results, pool_broken); pool_broken means a
    BrokenProcessPool was seen and the caller must recreate the pool.
    """
    results = []
    pool_broken = False
    last_exc = None
    for fut, chance, index in futures:
        try:
            results.append((fut.result(), chance, index))
        except BrokenProcessPool as e:
            pool_broken = True
            last_exc = e
            logger.error("World {} died (broken process pool): {!r}".format(index, e))
        except Exception as e:
            last_exc = e
            logger.error("World {} died: {!r}".format(index, e))
    if not results:
        raise RuntimeError(
            "all {} search worlds died".format(len(futures))
        ) from last_exc
    if len(results) < len(futures):
        total = sum(chance for _, chance, _ in results)
        if total > 0:
            results = [
                (res, chance / total, index) for res, chance, index in results
            ]
        logger.warning(
            "Dropped {} dead world(s); renormalized {} survivor(s)".format(
                len(futures) - len(results), len(results)
            )
        )
    return results, pool_broken


def get_result_from_mcts(
    state: str, search_time_ms: int, index: int, threads: int
) -> MctsResult:
    logger.debug("Calling with {} state: {}".format(index, state))
    poke_engine_state = PokeEngineState.from_string(state)

    res = monte_carlo_tree_search(poke_engine_state, search_time_ms, threads=threads)
    logger.info("Iterations {}: {}".format(index, res.total_visits))
    return res


def is_first_decision(battle) -> bool:
    """Whether this battle has not had a decision made for it yet.

    `battle.turn` is initialized to `False` in `Battle.__init__` and is only
    set (to an int) once a `|turn|N` protocol message is processed by
    `fp.battle_modifier.turn`. The battle's first decision is one of:
      - a team preview decision, which happens before `|turn|1` arrives, so
        `battle.turn` is still `False` (`battle.team_preview` is also True)
      - the turn-1 move decision in formats without team preview (random
        battles), where the initial message block has already set
        `battle.turn` to 1 by the time the first move is picked
    When the per-battle decision counter is available it takes precedence:
    a turn-1 pivot (e.g. u-turn) creates a SECOND decision inside turn 1
    which must NOT get extended time or the wide early-game sampling.
    """
    decisions = getattr(battle, "decisions_made", None)
    if decisions is not None:
        return decisions <= 1
    return battle.team_preview or not battle.turn or battle.turn <= 1


def base_search_time_ms(battle) -> int:
    if FoulPlayConfig.first_turn_search_time_ms is not None and is_first_decision(
        battle
    ):
        return FoulPlayConfig.first_turn_search_time_ms
    return FoulPlayConfig.search_time_ms


def search_time_num_battles_randombattles(battle):
    revealed_pkmn = len(battle.opponent.reserve)
    if battle.opponent.active is not None:
        revealed_pkmn += 1

    opponent_active_num_moves = len(battle.opponent.active.moves)
    in_time_pressure = battle.time_remaining is not None and battle.time_remaining <= 60
    base_ms = base_search_time_ms(battle)

    # it is still quite early in the battle and the pkmn in front of us
    # hasn't revealed any moves: search a lot of battles shallowly
    if (
        is_first_decision(battle)
        and revealed_pkmn <= 3
        and battle.opponent.active.hp > 0
        and opponent_active_num_moves == 0
    ):
        num_battles_multiplier = 2 if in_time_pressure else 4
        return FoulPlayConfig.parallelism * num_battles_multiplier, int(base_ms // 2)

    else:
        num_battles_multiplier = 1 if in_time_pressure else 2
        return FoulPlayConfig.parallelism * num_battles_multiplier, int(base_ms)


def search_time_num_battles_standard_battle(battle):
    opponent_active_num_moves = len(battle.opponent.active.moves)
    in_time_pressure = battle.time_remaining is not None and battle.time_remaining <= 60
    base_ms = base_search_time_ms(battle)

    if (
        battle.team_preview
        or (battle.opponent.active.hp > 0 and opponent_active_num_moves == 0)
        or opponent_active_num_moves < 3
    ):
        num_battles_multiplier = 1 if in_time_pressure else 2
        return FoulPlayConfig.parallelism * num_battles_multiplier, int(base_ms)
    else:
        return FoulPlayConfig.parallelism, base_ms


def find_best_move(battle: Battle) -> str:
    battle = deepcopy(battle)
    if battle.team_preview:
        battle.user.active = battle.user.reserve.pop(0)
        battle.opponent.active = battle.opponent.reserve.pop(0)

    # snapshot the REVEALED opponent mons before world sampling appends phantom
    # fill-ins - the upside tiebreak uses this to reject fictional phantom-switch
    # replies (a switch reply whose target is not in this set is a phantom).
    revealed_opponent_names = set()
    if battle.opponent.active is not None:
        revealed_opponent_names.add(battle.opponent.active.name.lower())
    for pkmn in battle.opponent.reserve:
        revealed_opponent_names.add(pkmn.name.lower())

    if battle.battle_type == BattleType.RANDOM_BATTLE:
        num_battles, search_time_per_battle = search_time_num_battles_randombattles(
            battle
        )
        battles = prepare_random_battles(battle, num_battles)
    elif battle.battle_type == BattleType.BATTLE_FACTORY:
        num_battles, search_time_per_battle = search_time_num_battles_standard_battle(
            battle
        )
        battles = prepare_random_battles(battle, num_battles)
    elif battle.battle_type == BattleType.STANDARD_BATTLE:
        num_battles, search_time_per_battle = search_time_num_battles_standard_battle(
            battle
        )
        battles = prepare_battles(battle, num_battles)
    else:
        raise ValueError("Unsupported battle type: {}".format(battle.battle_type))

    logger.info("Searching for a move using MCTS...")
    logger.info(
        "Sampling {} battles at {}ms each".format(num_battles, search_time_per_battle)
    )
    revealed_pkmn = len(battle.opponent.reserve) + (
        1 if battle.opponent.active is not None else 0
    )
    logger.info(
        "Decision context: opponent_revealed={} search_ms={} parallelism={} threads={}".format(
            revealed_pkmn,
            search_time_per_battle,
            FoulPlayConfig.parallelism,
            FoulPlayConfig.search_threads,
        )
    )

    def submit_searches(executor):
        futures = []
        for index, (state_string, chance) in enumerate(states):
            fut = executor.submit(
                get_result_from_mcts,
                state_string,
                search_time_per_battle,
                index,
                FoulPlayConfig.search_threads,
            )
            futures.append((fut, chance, index))
        return futures

    states = [
        (battle_to_poke_engine_state(b).to_string(), chance) for b, chance in battles
    ]
    # forensic artifact: the EXACT engine state each world searches, replayable
    # later with State.from_string (DEBUG => file log only). Without this,
    # post-game review has to reconstruct worlds from the sampled-set lines.
    for index, (state_string, _) in enumerate(states):
        logger.debug("WorldState {}: {}".format(index, state_string))
    reuse_pool = getattr(FoulPlayConfig, "reuse_search_pool", True)
    executor = (
        _get_search_executor()
        if reuse_pool
        else ProcessPoolExecutor(max_workers=FoulPlayConfig.parallelism)
    )
    try:
        try:
            futures = submit_searches(executor)
        except BrokenProcessPool:
            # the reused pool broke while idle (e.g. a worker OOM-killed
            # between decisions): recreate once and resubmit
            if not reuse_pool:
                raise
            logger.warning("Search pool broke while idle - recreating")
            _discard_search_executor()
            executor = _get_search_executor()
            futures = submit_searches(executor)
        mcts_results, pool_broken = gather_mcts_results(futures)
    except Exception:
        # total failure: never leave a possibly-broken pool behind
        if reuse_pool:
            _discard_search_executor()
        else:
            executor.shutdown(wait=False, cancel_futures=True)
        raise
    if not reuse_pool:
        executor.shutdown()
    elif pool_broken:
        _discard_search_executor()

    choice = select_move_from_mcts_results(mcts_results, revealed_opponent_names)
    logger.info("Choice: {}".format(choice))
    return choice
