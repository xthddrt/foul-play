"""Remote MCTS transport.

Pokemon Showdown refuses authenticated logins from AWS IP ranges (verified
2026-08-05: guest socket + challstr are issued fine, then the server closes
with 1000 OK the instant `/trn` is sent; the identical account/code/moment
works from a residential IP). So the websocket must stay on the local box.

The search does NOT have to. `get_result_from_mcts` is a pure function of a
serialized state string, which makes the world fan-out trivially remotable:
the local process samples worlds and picks the move, a remote box does the
MCTS. One round trip per decision.

Everything here is best-effort: any failure returns None and the caller
falls back to the local process pool. A network stall must never cost a
game.
"""

import gzip
import json
import logging
import time
import urllib.error
import urllib.request

from config import FoulPlayConfig

logger = logging.getLogger("fp.search.main")


class RemoteSideResult:
    """Mirrors poke_engine.MctsSideResult's read surface."""

    __slots__ = ("move_choice", "visits", "total_score", "total_score_sq")

    def __init__(self, move_choice, visits, total_score, total_score_sq):
        self.move_choice = move_choice
        self.visits = visits
        self.total_score = total_score
        self.total_score_sq = total_score_sq


class RemoteMctsResult:
    """Mirrors poke_engine.MctsResult's read surface.

    selection.py only ever READS side_one/side_two/total_visits/root_pairs,
    so a plain object with the same attributes is indistinguishable from the
    engine's native type downstream.
    """

    __slots__ = ("side_one", "side_two", "total_visits", "root_pairs")

    def __init__(self, side_one, side_two, total_visits, root_pairs):
        self.side_one = side_one
        self.side_two = side_two
        self.total_visits = total_visits
        self.root_pairs = root_pairs


def _decode(payload) -> RemoteMctsResult:
    def side(rows):
        return [RemoteSideResult(r[0], r[1], r[2], r[3]) for r in rows]

    return RemoteMctsResult(
        side(payload["side_one"]),
        side(payload["side_two"]),
        payload["total_visits"],
        [[tuple(cell) for cell in row] for row in payload.get("root_pairs") or []],
    )


# Circuit breaker. A remote that times out once will almost certainly time out
# again -- it is undersized, unreachable, or overloaded, none of which fix
# themselves mid-battle. Retrying every decision costs the FULL timeout every
# turn on top of the local search. On 2026-08-05 that turned a 4.5s budget into
# 52.98s on turn 1 and made the bot unplayable for the whole game. One failure
# disables remote for the rest of the process; the bot then runs at normal local
# speed instead of dragging a corpse behind it.
_REMOTE_DISABLED = False


def remote_disabled() -> bool:
    return _REMOTE_DISABLED


def _trip_breaker(why: str):
    global _REMOTE_DISABLED
    _REMOTE_DISABLED = True
    logger.error(
        "REMOTE SEARCH DISABLED for the rest of this process ({}). "
        "All further decisions use the local pool at full speed.".format(why)
    )


def remote_mcts_searches(states, search_time_ms: int, threads: int):
    """Return [(RemoteMctsResult, chance, index)] or None to fall back.

    Returning None (not raising) is deliberate: the caller treats a remote
    miss as "use the local pool", so a dead worker box degrades the bot to
    its normal local strength instead of forfeiting.
    """
    url = getattr(FoulPlayConfig, "search_remote_url", None)
    if not url or _REMOTE_DISABLED:
        return None

    # The remote runs every world concurrently, so the wall floor is one
    # world's search time; the margin covers transfer plus its fan-out.
    # 4000 -> 2000: this is dead time paid out of the turn budget on failure.
    margin_ms = getattr(FoulPlayConfig, "search_remote_timeout_margin_ms", 2000)
    timeout_s = (search_time_ms + margin_ms) / 1000.0

    # CLOCK GUARD. On a miss we pay timeout_s AND then a full local search. Only
    # attempt remote if the worst case still leaves headroom, otherwise go
    # straight local at full speed. Without this, w64_r6 spent 16.39s on a turn
    # that had 15s left.
    remaining = getattr(FoulPlayConfig, "last_time_remaining", None)
    if remaining is not None:
        worst_case = timeout_s + search_time_ms / 1000.0 + 3.0
        if worst_case > remaining:
            logger.warning(
                "Skipping remote: worst case {:.1f}s exceeds {}s left on the clock".format(
                    worst_case, remaining
                )
            )
            return None

    # gzip: the world states are highly repetitive text -- 217KB -> 19KB (11.6x)
    # for 64 worlds. At the ~410KB/s upload measured from here that is 0.53s of
    # dead transfer per request, which is most of the variance that tripped the
    # 7.5s timeout on w64_r6 while the worker itself took a steady 4.8s.
    raw = json.dumps(
        {
            "states": [s for s, _ in states],
            "search_time_ms": search_time_ms,
            "threads": threads,
        }
    ).encode()
    body = gzip.compress(raw, 1)
    req = urllib.request.Request(
        url.rstrip("/") + "/search",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Content-Encoding": "gzip",
            "Accept-Encoding": "gzip",
        },
    )

    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding") == "gzip":
                data = gzip.decompress(data)
            payload = json.loads(data.decode())
    except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
        logger.warning(
            "Remote search failed after {:.2f}s ({!r}) - falling back to local pool".format(
                time.time() - t0, e
            )
        )
        _trip_breaker(repr(e)[:120])
        return None

    raw = payload.get("results")
    if not isinstance(raw, list) or len(raw) != len(states):
        logger.warning(
            "Remote search returned {} results for {} worlds - falling back".format(
                len(raw) if isinstance(raw, list) else "?", len(states)
            )
        )
        return None

    results = []
    for index, (entry, (_state, chance)) in enumerate(zip(raw, states)):
        if entry is None:
            logger.error("Remote world {} died".format(index))
            continue
        try:
            results.append((_decode(entry), chance, index))
        except (KeyError, TypeError, IndexError) as e:
            logger.error("Remote world {} malformed: {!r}".format(index, e))

    if not results:
        logger.warning("Every remote world died - falling back to local pool")
        return None

    # Same contract as gather_mcts_results: dropped worlds must not silently
    # change what the pooled-share floors in selection.py mean.
    if len(results) < len(states):
        total = sum(c for _, c, _ in results)
        if total > 0:
            results = [(r, c / total, i) for r, c, i in results]
        logger.warning(
            "Dropped {} remote world(s); renormalized {} survivor(s)".format(
                len(states) - len(results), len(results)
            )
        )

    logger.info(
        "RemoteSearch: {} worlds, {}ms each, round trip {:.2f}s".format(
            len(states), search_time_ms, time.time() - t0
        )
    )
    return results
