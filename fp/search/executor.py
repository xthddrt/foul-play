import logging
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

from config import FoulPlayConfig

from poke_engine import State as PokeEngineState, monte_carlo_tree_search, MctsResult

# This code moved here from fp.search.main; keep logging to the historical
# logger name so log capture (and the tests asserting on it) is unchanged.
logger = logging.getLogger("fp.search.main")


# A single ProcessPoolExecutor kept alive across decisions: recreating it
# every decision costs ~parallelism process spawns per move. Workers import
# the poke_engine wheel once when they spawn, so a wheel rebuilt mid-battle
# is NOT silently picked up - the pool is only recreated on breakage and a
# rebuild still requires a bot restart, exactly as documented before.
# Only find_best_move (which runs one-at-a-time in the decision thread)
# touches these globals.
_search_executor: ProcessPoolExecutor | None = None
_search_executor_workers: int | None = None


def pool_workers() -> int:
    return (
        getattr(FoulPlayConfig, "search_pool_workers", None) or FoulPlayConfig.parallelism
    )


def _get_search_executor() -> ProcessPoolExecutor:
    global _search_executor, _search_executor_workers
    workers = pool_workers()
    if _search_executor is None or _search_executor_workers != workers:
        if _search_executor is not None:
            _search_executor.shutdown(wait=False, cancel_futures=True)
        logger.info("Spawning search pool with {} workers".format(workers))
        _search_executor = ProcessPoolExecutor(max_workers=workers)
        _search_executor_workers = workers
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


def get_probe_result(
    state: str, search_time_ms: int, forced_move: str, threads: int
) -> MctsResult:
    """Committed-root probe: side_one restricted to forced_move, so the
    opponent's root reply is a best response to a known commitment - no
    simultaneous-move mixing at the measured ply. Correct for MOVES, whose
    threat is visible; switches use the blind probe instead."""
    poke_engine_state = PokeEngineState.from_string(state)
    return monte_carlo_tree_search(
        poke_engine_state,
        search_time_ms,
        threads=threads,
        forced_side_one_move=forced_move,
    )


def get_blind_switch_probe(
    state: str, search_time_ms: int, switch_move: str, their_reply: str, threads: int
) -> float:
    """Blind-switch probe: the opponent cannot see a switch coming, so their
    root reply is FORCED to what they would play against our current active
    (their phase-1 modal action) while our root is forced to the switch.
    Both moves forced on the ORIGINAL state keeps every probe anchored to
    the same root baseline - a committed-root switch probe instead grants
    clairvoyant punishment (measured cost ~0.2 on the T12 forensic), and a
    post-exchange search would grade each candidate on its own baseline."""
    poke_engine_state = PokeEngineState.from_string(state)
    res = monte_carlo_tree_search(
        poke_engine_state,
        search_time_ms,
        threads=threads,
        forced_side_one_move=switch_move,
        forced_side_two_move=their_reply,
    )
    arms = [m for m in res.side_one if m.visits > 0]
    if not arms:
        raise ValueError("empty probe result")
    best = max(arms, key=lambda m: m.visits)
    return best.total_score / best.visits


def run_probe_searches(
    states: list[(str, float)],
    candidates: list[str],
    search_time_ms: int,
    threads: int,
    their_replies: dict | None = None,
) -> dict:
    """One probe per (candidate, world): committed-root for moves, blind for
    switches (their_replies maps state_string -> their modal reply vs our
    current active). Returns {candidate: [(value, chance, index), ...]}."""
    executor = _get_search_executor()
    futures = {}
    for cand in candidates:
        for index, (state_string, chance) in enumerate(states):
            reply = (their_replies or {}).get(state_string)
            if cand.startswith("switch ") and reply is not None:
                fut = executor.submit(
                    get_blind_switch_probe,
                    state_string,
                    search_time_ms,
                    cand,
                    reply,
                    threads,
                )
                futures[fut] = (cand, chance, index, True)
            else:
                fut = executor.submit(
                    get_probe_result, state_string, search_time_ms, cand, threads
                )
                futures[fut] = (cand, chance, index, False)
    out = {c: [] for c in candidates}
    for fut, (cand, chance, index, blind) in futures.items():
        try:
            res = fut.result()
        except Exception:
            logger.debug("probe failed for %s world %s", cand, index, exc_info=True)
            continue
        if blind:
            out[cand].append((res, chance, index))
        else:
            arms = [m for m in res.side_one if m.visits > 0]
            if len(arms) != 1:
                continue
            out[cand].append((arms[0].total_score / arms[0].visits, chance, index))
    return out


def run_mcts_searches(
    states: list[(str, float)], search_time_ms: int, threads: int
) -> list[(MctsResult, float, int)]:
    """Run one MCTS search per (state_string, sample_chance) world.

    Owns the pool lifecycle: reuses the long-lived pool (recreating it once
    and resubmitting if it broke while idle), drops dead worlds and
    renormalizes the surviving sample chances via gather_mcts_results, and
    never leaves a possibly-broken pool behind on total failure.
    """

    # Remote first when configured. Showdown refuses authenticated logins from
    # datacenter IPs, so the websocket is pinned to the local box -- but the
    # search is a pure function of the state strings and can live anywhere.
    # A miss returns None (never raises) and we fall through to the local pool,
    # so a dead worker box costs strength, never the game.
    from fp.search.remote import remote_mcts_searches

    remote = remote_mcts_searches(states, search_time_ms, threads)
    if remote is not None:
        return remote

    # The remote world count can be far larger than the LOCAL box can run in one
    # wave. Falling back verbatim is what turned a 4.5s budget into 52.98s on
    # 2026-08-05: 64 worlds through an 8-process pool is 8 sequential waves.
    # Trim to one local wave, keeping the highest-probability worlds, and
    # renormalize -- a shallower-but-on-time decision beats a timeout loss.
    # os.cpu_count(), NOT pool_workers(): pool_workers() is the CONFIGURED
    # parallelism (64 when driving a remote worker), which is not what this
    # machine can actually run. Falling back to 64 local processes also pays a
    # cold-pool spawn of 64 interpreters, which is most of the 16.39s on w64_r6.
    import os as _os
    _cap = min(pool_workers(), _os.cpu_count() or 8)
    if len(states) > _cap:
        _trimmed = sorted(states, key=lambda sc: -sc[1])[:_cap]
        _tot = sum(c for _, c in _trimmed)
        if _tot > 0:
            _trimmed = [(s, c / _tot) for s, c in _trimmed]
        # Trim IN PLACE, do not rebind: the caller keeps this same list and
        # indexes it with the world indices returned below to build the probe
        # phase's their_replies map and world set. Rebinding would leave the
        # caller indexing a DIFFERENT list, so index i would name another
        # world and blind-switch probes would be forced against a reply
        # computed for a state they never searched.
        states[:] = _trimmed
        logger.warning(
            "Local fallback capped to {} worlds (one wave) to stay inside the clock".format(_cap)
        )

    def submit_searches(executor):
        futures = []
        for index, (state_string, chance) in enumerate(states):
            fut = executor.submit(
                get_result_from_mcts,
                state_string,
                search_time_ms,
                index,
                threads,
            )
            futures.append((fut, chance, index))
        return futures

    reuse_pool = getattr(FoulPlayConfig, "reuse_search_pool", True)
    executor = (
        _get_search_executor()
        if reuse_pool
        else ProcessPoolExecutor(max_workers=pool_workers())
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
    return mcts_results
