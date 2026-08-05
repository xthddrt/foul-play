import logging
import math
import random

from config import FoulPlayConfig

from poke_engine import MctsResult

# This code moved here from fp.search.main; keep logging to the historical
# logger name so log capture (and the tests asserting on it) is unchanged.
logger = logging.getLogger("fp.search.main")

# Opponent-model ledger: after every search this holds the cross-world
# aggregated prediction of the opponent's next action. The caller stamps
# turn/battle_tag; battle_modifier joins it against the action the opponent
# actually took and appends the pair to opponent_ledger.jsonl. This is the
# ground-truth data for calibrating the opponent model (switch keeps, veto
# gaps, entry intent) - the one parameter family self-play cannot tune.
last_opponent_prediction = {}


def _aggregate_results(mcts_results):
    """Pool the per-world MCTS results into cross-world statistics.

    Returns (pooled_share, blend_sum, agg_score, score_sd, score_min,
    world_scores, pair_stats).
    """
    pooled_share = {}
    blend_sum = {}
    score_weighted_sum = {}
    score_sq_weighted_sum = {}
    score_weight = {}
    world_scores = {}
    pair_stats = {}
    opp_dist = {}
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
                for o in side_two:
                    if o.visits > 0:
                        opp_dist[o.move_choice] = opp_dist.get(o.move_choice, 0.0) + (
                            sample_chance * o.visits / opp_total
                        )
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

    return (
        pooled_share,
        blend_sum,
        agg_score,
        score_sd,
        score_min,
        world_scores,
        pair_stats,
        opp_dist,
    )


def _apply_switch_damping(pooled_share, blend_sum):
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
    return damped_share, damped_blend


def _apply_score_gate(ranked, option_value, agg_score, pooled_share):
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
    return ranked


def _apply_tera_margin_gate(ranked, agg_score, pooled_share):
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
    return ranked


def _apply_losing_upside_tiebreak(ranked, agg_score, pair_stats, revealed_opponent_names):
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
                    # None (not 0.0) when the incumbent has no qualifying pair
                    # data: without evidence about its ceiling, never displace
                    incumbent_upside = dict(scored).get(incumbent)
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
                    displaces = (
                        incumbent_upside is not None
                        and winner != incumbent
                        and (
                            best_upside - incumbent_upside >= margin
                            or (
                                incumbent.startswith("switch ")
                                and not winner.startswith("switch ")
                                and best_upside >= incumbent_upside
                            )
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
    return ranked, upside_fired


def _apply_losing_fallback(ranked, agg_score, pooled_share, upside_fired):
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
    return None


def select_move_from_mcts_results(
    mcts_results: list[(MctsResult, float, int)],
    revealed_opponent_names: set[str] | None = None,
    candidates_margin: float | None = None,
    max_candidates: int = 3,
) -> str:
    """With candidates_margin set, returns (choice, close_candidates) where
    close_candidates are the post-gate options whose agg score sits within
    the margin of the top option (probe-phase finalists)."""
    # revealed_opponent_names: the lowercased species names of the opponent
    # pokemon actually revealed in the real battle (active + revealed reserve).
    # Used by the losing-position upside tiebreak to reject opponent switch
    # replies into SAMPLED unrevealed (phantom) mons - a phantom matchup is a
    # fiction invented by determinization and must not set an option's
    # conditional ceiling. None (legacy callers / unit tests) => no filtering.
    (
        pooled_share,
        blend_sum,
        agg_score,
        score_sd,
        score_min,
        world_scores,
        pair_stats,
        opp_dist,
    ) = _aggregate_results(mcts_results)

    # PAIR TABLE: our arm x their reply -> (chance-weighted visits, total score).
    # This is the ONLY input _apply_losing_upside_tiebreak scores candidates on,
    # and it was previously computed and discarded, leaving post-mortems unable to
    # answer "what would the losing-upside tiebreak have chosen?" -- exactly the
    # question game 4's turn 8 raised (four candidates inside a 0.005 band, all
    # unresolvable from WorldStats/OppWorldStats because those are MARGINALS and
    # the tiebreak needs the JOINT). Logged as one line per decision, mean score
    # per cell, visit-sorted, capped so a wide root cannot flood the log.
    if pair_stats:
        _cells = sorted(
            ((a, b, v, t) for (a, b), (v, t) in pair_stats.items() if v > 0),
            key=lambda c: -c[2],
        )[:60]
        # .format(), NOT lazy %-args: CustomFormatter renders record.msg directly,
        # so logger.info("x %s", y) prints the literal "x %s". That silently
        # emptied 424 StateString lines earlier in this project.
        logger.info(
            "PairTable: {}".format(
                " | ".join(
                    "{} vs {} {:.0f}/{:.4f}".format(a, b, v, t / v)
                    for a, b, v, t in _cells
                )
            )
        )

    # stash the aggregated opponent prediction for the ledger (caller stamps
    # turn/battle_tag; battle_modifier joins with the observed action)
    total_p = sum(opp_dist.values())
    last_opponent_prediction.clear()
    if total_p > 0:
        last_opponent_prediction["dist"] = {
            k: round(v / total_p, 4)
            for k, v in sorted(opp_dist.items(), key=lambda kv: -kv[1])
        }
        # the search's average score for our best arm - NOT a calibrated win
        # probability (it is eval-parameter-dependent); the calibration
        # analysis derives the score->win-rate mapping empirically
        best = max(agg_score, key=agg_score.get) if agg_score else None
        if best is not None:
            last_opponent_prediction["our_mcts_score"] = round(agg_score[best], 4)

    # ARGMAX-ONLY: return the pooled visit-share argmax and skip EVERY gate
    # below (switch damping, variance penalty, negative-regime guard, the
    # opponent switch veto, pooled-share eligibility floors, the score-band and
    # displacement-margin tiebreaks, the losing-position upside fallback).
    #
    # Two reasons this exists. (1) Every Elo number backing the current config
    # was measured by the duel harness, which takes the raw MCTS arm and never
    # calls this module at all — so argmax is the policy we actually validated,
    # and anything else ladders an unmeasured bot. (2) The gates' thresholds
    # (pooled-share 0.005/0.01/0.05, the 0.05-0.15 score band, the 2x
    # displacement margin) were sized for the hand eval's ~0.28 arm-value
    # spread; under the net they are unverified, and a threshold that silently
    # never fires is indistinguishable from one that always does.
    #
    # This is the pooled analogue of the harness's per-world argmax: sum each
    # option's visit share over the sampled worlds and take the max, so a move
    # wins by being endorsed across worlds rather than by one world's search.
    if getattr(FoulPlayConfig, "selection_argmax_only", False):
        if not pooled_share:
            raise ValueError("argmax-only selection: no options in pooled_share")
        choice = max(pooled_share, key=pooled_share.get)
        total = sum(pooled_share.values()) or 1.0
        logger.info(
            "ARGMAX-ONLY selection (all gates bypassed): {} with {}% pooled visit share; "
            "top 3: {}".format(
                choice,
                round(100 * pooled_share[choice] / total, 1),
                [
                    (c, round(100 * s / total, 1))
                    for c, s in sorted(pooled_share.items(), key=lambda kv: -kv[1])[:3]
                ],
            )
        )
        if candidates_margin is not None:
            return choice, [choice]
        return choice

    damped_share, damped_blend = _apply_switch_damping(pooled_share, blend_sum)

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

    ranked = _apply_score_gate(ranked, option_value, agg_score, pooled_share)

    ranked = _apply_tera_margin_gate(ranked, agg_score, pooled_share)

    ranked, upside_fired = _apply_losing_upside_tiebreak(
        ranked, agg_score, pair_stats, revealed_opponent_names
    )

    fallback_choice = _apply_losing_fallback(
        ranked, agg_score, pooled_share, upside_fired
    )
    if fallback_choice is not None:
        if candidates_margin is not None:
            return fallback_choice, [fallback_choice]
        return fallback_choice

    choice = _sample_near_ties(ranked, damped_share, agg_score)
    if candidates_margin is None:
        return choice
    # nominate from the PRE-GATE aggregation: pure phase-1 MCTS scores over
    # every visited arm. The gates (switch gate, tera margin, upside) exist to
    # guard the single-phase argmax against search artifacts - but the
    # committed-root probe IS the honest evaluator of a finalist, so gates
    # must not censor the probe pool (the T12/T19 forensic: within-margin
    # switches were gate-pruned and never probed). Tiny-share arms are still
    # excluded: their scores are too unreliable to nominate on.
    eligible = [c for c in agg_score if pooled_share.get(c, 0.0) >= 0.005]
    best_score = max(agg_score[c] for c in eligible)
    by_score = sorted(
        (c for c in eligible if best_score - agg_score[c] <= candidates_margin),
        key=lambda c: -agg_score[c],
    )
    candidates = by_score[:max_candidates]
    if choice not in candidates:
        candidates = [choice] + candidates[: max_candidates - 1]
    return choice, candidates


def _sample_near_ties(ranked, damped_share, agg_score):
    """Near-tie mixing: a deterministic argmax is readable (within-game
    pattern reads; account-level scouting at top ladder), so among options
    the search itself considers equivalent, sample instead of argmaxing.

    Eligibility (both must hold, over post-gate survivors):
      - visit share >= mix_visit_ratio * the most-visited survivor's share
      - agg score  >= best survivor score - mix_score_tolerance
    Sampling weight: share**2 (sharpened toward the search's preference).
    Never overrides a gate: if the gates elevated an option that is not the
    most-visited survivor, the choice stays deterministic.
    """
    ratio = FoulPlayConfig.mix_visit_ratio
    tol = FoulPlayConfig.mix_score_tolerance
    if ratio <= 0 or tol <= 0 or len(ranked) < 2:
        return ranked[0][0]
    top_choice = ranked[0][0]
    top_share = max(damped_share.get(c, 0.0) for c, _ in ranked)
    if damped_share.get(top_choice, 0.0) < top_share:
        return top_choice  # a gate overrode the search's favorite; respect it
    top_score = max(agg_score.get(c, 0.0) for c, _ in ranked)
    pool = [
        c
        for c, _ in ranked
        if damped_share.get(c, 0.0) >= ratio * top_share
        and agg_score.get(c, 0.0) >= top_score - tol
    ]
    # empty pool, singleton pool, or a pool that excludes the argmax itself
    # (top arm failed the score filter): stay deterministic
    if len(pool) < 2 or top_choice not in pool:
        return top_choice
    weights = [damped_share[c] ** 2 for c in pool]
    chosen = random.choices(pool, weights=weights)[0]
    total_w = sum(weights)
    logger.info(
        "near-tie mix: {} -> {}".format(
            [(c, round(w / total_w, 3)) for c, w in zip(pool, weights)], chosen
        )
    )
    return chosen
