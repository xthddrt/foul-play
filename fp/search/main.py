import logging
import math
import os
import time
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

# re-exports: the worker-pool lifecycle lives in fp.search.executor, but
# fp.search.main remains the public import path
from fp.search.executor import (  # noqa: F401
    _discard_search_executor,
    _get_search_executor,
    gather_mcts_results,
    get_result_from_mcts,
    run_mcts_searches,
)

# re-export: move selection lives in fp.search.selection, but fp.search.main
# remains the public import path
from fp.search import selection
from fp.search.selection import select_move_from_mcts_results  # noqa: F401

logger = logging.getLogger(__name__)


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
        ms = FoulPlayConfig.first_turn_search_time_ms
    else:
        ms = FoulPlayConfig.search_time_ms
    # never search past the actual clock: leave margin for world sampling,
    # serialization, aggregation, and the network round-trip
    if battle.time_remaining is not None:
        clamped = min(ms, max(300, battle.time_remaining * 1000 - 3000))
        # Log when the CLOCK, not the config, is deciding our search depth. Without
        # this a too-generous per-turn budget is invisible: the opening searches
        # deep, drains the bank, and the late-game turns that actually decide the
        # match silently collapse toward the 300ms floor.
        if clamped < ms:
            logger.info(
                "SEARCH CLAMPED by clock: {}ms -> {}ms (time_remaining={}s)".format(
                    ms, clamped, battle.time_remaining
                )
            )
        ms = clamped
    return ms


def _per_world_search_ms(wall_ms: int, num_battles: int) -> int:
    # worlds run in ceil(num_battles / pool_workers) sequential waves on the
    # pool: divide the wall budget by the wave count so wall-clock time stays
    # the same no matter how many worlds share the pool
    pool = (
        getattr(FoulPlayConfig, "search_pool_workers", None) or FoulPlayConfig.parallelism
    )
    waves = max(1, math.ceil(num_battles / pool))
    return int(wall_ms // waves)


def _wave_count(num_battles: int) -> int:
    pool = (
        getattr(FoulPlayConfig, "search_pool_workers", None) or FoulPlayConfig.parallelism
    )
    return max(1, math.ceil(num_battles / pool))


def dedupe_states(states, max_replicas=2):
    """Collapse identical sampled worlds, summing their chance.

    Late-game inference narrows the opponent to 1-3 candidate sets, so 16-32
    MCTS searches can run over 1-3 DISTINCT engine states - up to ~90%
    duplicate compute. `_probe_and_choose` already does this for phase 2 for
    exactly this reason. Up to `max_replicas` copies of each unique state are
    kept because duplicate worlds are the only source of RNG diversity in the
    search; the summed chance is split evenly across the kept replicas so the
    downstream weighting is unchanged.
    """
    order = []
    merged = {}
    for state_string, chance in states:
        if state_string not in merged:
            merged[state_string] = [0.0, 0]
            order.append(state_string)
        merged[state_string][0] += chance
        merged[state_string][1] += 1

    reduced = []
    for state_string in order:
        total_chance, multiplicity = merged[state_string]
        replicas = min(multiplicity, max_replicas)
        for _ in range(replicas):
            reduced.append((state_string, total_chance / replicas))
    return reduced


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
        # The x4 fan-out was sized for parallelism=8 (32 worlds). It scales with
        # parallelism, so a wider pool multiplies into far more worlds than intended;
        # first_turn_world_multiplier pins it when the world count matters more than
        # the historical default.
        num_battles_multiplier = getattr(
            FoulPlayConfig, "first_turn_world_multiplier", None
        ) or (2 if in_time_pressure else 4)
        num_battles = FoulPlayConfig.parallelism * num_battles_multiplier
        # base_ms is the WALL budget for the whole first decision
        return num_battles, _per_world_search_ms(base_ms, num_battles)

    else:
        num_battles_multiplier = 1 if in_time_pressure else 2
        num_battles = FoulPlayConfig.parallelism * num_battles_multiplier
        # historical wall budget: one base_ms wave per multiplier
        return num_battles, _per_world_search_ms(
            base_ms * num_battles_multiplier, num_battles
        )


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
        num_battles = FoulPlayConfig.parallelism * num_battles_multiplier
        return num_battles, _per_world_search_ms(
            base_ms * num_battles_multiplier, num_battles
        )
    else:
        num_battles = FoulPlayConfig.parallelism
        return num_battles, _per_world_search_ms(base_ms, num_battles)


def _probe_and_choose(states, candidates, phase1_choice, mcts_results=None) -> str:
    """Phase 2: committed-root probes. Each finalist is measured by dedicated
    forced-root searches across all sampled worlds; the opponent's root reply
    is then a best response to the commitment, so the simultaneous-move
    equilibrium cannot donate implausible replies to any candidate's value.
    Pick the best sample-weighted mean; near-equal means fall to the higher
    worst-world floor."""
    from fp.search.executor import run_probe_searches

    # dedupe: identical world states are the sampler's frequency weighting -
    # probing one twice is wasted compute. Probe each unique state once,
    # weighted by its summed chance (multiplicity), capped at probe_worlds
    # unique states ranked by weight.
    merged = {}
    for state_string, chance in states:
        merged[state_string] = merged.get(state_string, 0.0) + chance
    probe_states = sorted(merged.items(), key=lambda sc: -sc[1])[
        : FoulPlayConfig.probe_worlds
    ]
    # adaptive depth: spend the whole phase-2 budget across however many
    # pool waves the probe grid needs - fewer unique worlds or fewer
    # finalists automatically buy deeper probes
    pool = getattr(FoulPlayConfig, "search_pool_workers", None) or FoulPlayConfig.parallelism
    tasks = len(probe_states) * len(candidates)
    waves = max(1, math.ceil(tasks / pool))
    probe_ms = max(
        FoulPlayConfig.probe_ms_min,
        min(FoulPlayConfig.probe_ms_max, FoulPlayConfig.probe_phase2_budget_ms // waves),
    )
    logger.info(
        "ProbePlan: {} candidates x {} unique worlds ({} tasks, {} waves) at {}ms".format(
            len(candidates), len(probe_states), tasks, waves, probe_ms
        )
    )
    # their modal reply per world (vs our CURRENT active) for blind switch
    # probes: a switch is invisible until it resolves, so the opponent's
    # reply must be what they'd play against the mon they can actually see
    their_replies = {}
    if mcts_results is not None:
        for res, _chance, index in mcts_results:
            if index >= len(states):
                continue
            state_string = states[index][0]
            side_two = getattr(res, "side_two", None)
            if not side_two or state_string in their_replies:
                continue
            live = [m for m in side_two if m.visits > 0]
            if not live:
                continue
            top = max(live, key=lambda m: m.visits).move_choice.lower()
            if top.endswith("-tera"):
                top = top[: -len("-tera")]
            their_replies[state_string] = top
    try:
        results = run_probe_searches(
            probe_states,
            candidates,
            probe_ms,
            FoulPlayConfig.search_threads,
            their_replies=their_replies,
        )
    except Exception:
        logger.warning("probe phase failed; keeping phase-1 choice", exc_info=True)
        return phase1_choice
    scored = []
    for cand, rows in results.items():
        if not rows:
            continue
        wsum = sum(ch for _, ch, _ in rows)
        mean = sum(v * ch for v, ch, _ in rows) / wsum if wsum > 0 else 0.0
        floor = min(v for v, _, _ in rows)
        scored.append((cand, mean, floor))
        logger.info(
            "ProbeStats {}: mean={} floor={} worlds={}".format(
                cand, round(mean, 4), round(floor, 4), len(rows)
            )
        )
    if not scored:
        return phase1_choice
    scored.sort(key=lambda x: -x[1])
    best = scored[0]
    for cand, mean, floor in scored[1:]:
        mean_loss = best[1] - mean
        floor_gain = floor - best[2]
        # the safer option must buy MORE worst-case than it costs on average
        if mean_loss <= FoulPlayConfig.probe_floor_margin and floor_gain > mean_loss:
            best = (cand, mean, floor)
    logger.info("ProbeChoice: {} (phase1 was {})".format(best[0], phase1_choice))
    return best[0]


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
    # time_remaining comes from Showdown's `|inactive|Time left: N sec` messages,
    # which are only logged at DEBUG -- so the single number that decides BOTH how
    # much search we may spend AND whether we run 8 worlds or 16 (in_time_pressure
    # flips the multiplier at <=60s) was invisible at INFO. Surface it here.
    # Remote search needs the clock: a remote attempt that times out costs its
    # FULL timeout and is then followed by a local search. With 15s left that is
    # unaffordable (measured 16.39s on w64_r6 and it nearly timed the game out).
    # Stash it so fp.search.remote can decline to even try when the clock is short.
    FoulPlayConfig.last_time_remaining = battle.time_remaining
    logger.info(
        "Decision context: opponent_revealed={} search_ms={} parallelism={} threads={} "
        "time_remaining={} worlds={} in_time_pressure={}".format(
            revealed_pkmn,
            search_time_per_battle,
            FoulPlayConfig.parallelism,
            FoulPlayConfig.search_threads,
            battle.time_remaining,
            num_battles,
            battle.time_remaining is not None and battle.time_remaining <= 60,
        )
    )
    _t_search_start = time.time()

    states = [
        (battle_to_poke_engine_state(b).to_string(), chance) for b, chance in battles
    ]
    num_sampled = len(states)
    deduped_states = dedupe_states(states)
    # Dump the exact engine state of every sampled world. These strings are the
    # ONLY way to re-run a decision later -- at a different search budget, with a
    # different net, or with different constants. Without them a post-mortem can
    # only read the choice the bot made, never re-ask the question: the protocol
    # log is not enough (it lacks our private request data) and a Showdown replay
    # is not enough (same gap). One line here makes every turn reproducible.
    for _i, (_s, _c) in enumerate(deduped_states):
        logger.info("StateString {} chance={:.4f} {}".format(_i, _c, _s))
    if len(deduped_states) < len(states):
        # give the freed wall time back as DEPTH on the states that exist
        wall_ms = search_time_per_battle * _wave_count(len(states))
        search_time_per_battle = _per_world_search_ms(wall_ms, len(deduped_states))
        logger.info(
            "World dedupe: {} sampled -> {} unique-with-replicas, {}ms each".format(
                len(states), len(deduped_states), search_time_per_battle
            )
        )
        states = deduped_states

    # ONE budget per decision, and it is the budget we actually spend.
    #
    # search_time_per_battle was sized by dividing the wall budget across the
    # waves the SAMPLED world count would need. run_mcts_searches then trims the
    # world list down to a single local wave -- and nothing re-sized the budget
    # afterwards, so the freed waves were simply thrown away. Measured on the
    # 2026-08-08 ladder game: turn 1 asked for 10000ms, sampled 32 worlds, was
    # trimmed to 8, and searched them at 10000//4 = 2500ms for a 2.5s turn.
    #
    # Apply the same cap here, BEFORE sizing, and then divide the wall budget by
    # the waves the surviving worlds actually need. What is passed on the command
    # line is then what the turn really costs.
    _cap = min(
        (getattr(FoulPlayConfig, "search_pool_workers", None) or FoulPlayConfig.parallelism),
        os.cpu_count() or 8,
    )
    if len(states) > _cap:
        _trimmed = sorted(states, key=lambda sc: -sc[1])[:_cap]
        _tot = sum(c for _, c in _trimmed) or 1.0
        states[:] = [(s, c / _tot) for s, c in _trimmed]
        logger.info(
            "World cap: {} sampled -> {} searchable in one wave".format(
                num_sampled, len(states)
            )
        )
    wall_budget_ms = base_search_time_ms(battle)
    search_time_per_battle = _per_world_search_ms(wall_budget_ms, len(states))
    logger.info(
        "Budget: wall={}ms worlds={} waves={} -> {}ms per world".format(
            wall_budget_ms,
            len(states),
            _wave_count(len(states)),
            search_time_per_battle,
        )
    )
    # forensic artifact: the EXACT engine state each world searches, replayable
    # later with State.from_string (DEBUG => file log only). Without this,
    # post-game review has to reconstruct worlds from the sampled-set lines.
    for index, (state_string, _) in enumerate(states):
        logger.debug("WorldState {}: {}".format(index, state_string))
    probe_eligible = (
        FoulPlayConfig.probe_phase1_ms > 0
        and battle.battle_type == BattleType.RANDOM_BATTLE
        and (battle.time_remaining is None or battle.time_remaining > 9)
    )
    if probe_eligible and not is_first_decision(battle):
        # probe_phase1_ms is a PER-WORLD budget sized for the UNDEDUPED wave
        # structure. Late game, dedupe collapses 16-48 worlds into a handful of
        # unique states and frees whole waves; overwriting with the flat value
        # would throw that time away and make the decisive turns the shallowest
        # of the game. Conserve the wall instead (waves x probe_phase1_ms), with
        # the clock -- less the phase-2 probe budget that still has to run after
        # phase 1 -- as the ceiling. Never below probe_phase1_ms, so the
        # no-dedupe path is exactly the previous behavior.
        phase1_wall_ms = FoulPlayConfig.probe_phase1_ms * _wave_count(num_sampled)
        if battle.time_remaining is not None:
            phase1_wall_ms = min(
                phase1_wall_ms,
                max(300, battle.time_remaining * 1000 - 3000)
                - FoulPlayConfig.probe_phase2_budget_ms,
            )
        search_time_per_battle = max(
            FoulPlayConfig.probe_phase1_ms,
            phase1_wall_ms // _wave_count(len(states)),
        )

    mcts_results = run_mcts_searches(
        states, search_time_per_battle, FoulPlayConfig.search_threads
    )

    if probe_eligible:
        choice, candidates = select_move_from_mcts_results(
            mcts_results,
            revealed_opponent_names,
            candidates_margin=FoulPlayConfig.probe_margin,
            max_candidates=FoulPlayConfig.probe_max_candidates,
        )
        if len(candidates) > 1:
            choice = _probe_and_choose(states, candidates, choice, mcts_results)
    else:
        choice = select_move_from_mcts_results(mcts_results, revealed_opponent_names)
    _elapsed = time.time() - _t_search_start
    logger.info(
        "TurnTiming: elapsed={:.2f}s budget_per_world={}ms worlds={} waves={} "
        "expected_wall={:.2f}s time_remaining_at_start={}".format(
            # `states` (not deduped_states) is what was actually searched: the
            # local-fallback trim in run_mcts_searches shortens it in place.
            _elapsed, search_time_per_battle, len(states),
            _wave_count(len(states)),
            _wave_count(len(states)) * search_time_per_battle / 1000.0,
            battle.time_remaining,
        )
    )
    logger.info("Choice: {}".format(choice))

    # stamp the opponent-prediction stash for the ledger join (see selection.py)
    if selection.last_opponent_prediction.get("dist"):
        selection.last_opponent_prediction.update(
            turn=battle.turn,
            battle_tag=battle.battle_tag,
            our_active=getattr(battle.user.active, "name", None),
            opp_active=getattr(battle.opponent.active, "name", None),
            our_choice=choice,
        )
    return choice
