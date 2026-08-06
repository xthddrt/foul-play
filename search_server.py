#!/usr/bin/env python3
"""Remote MCTS worker.

Runs on a big box; the bot runs wherever Showdown will accept its login and
POSTs sampled worlds here. See fp/search/remote.py for why the split exists.

    PE_NN_WEIGHTS=/opt/valuenet_weights.bin python search_server.py --port 8000

Request   POST /search
          {"states": [str, ...], "search_time_ms": int, "threads": int}
Response  {"results": [ {...} | null, ... ]}   -- order matches the request;
          null marks a world that died, which the client drops and
          renormalizes rather than failing the decision.

Health    GET /health -> {"ok": true, "workers": N}
"""

import argparse
import gzip
import json
import logging
import os
import sys
from concurrent.futures import ProcessPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
)
logger = logging.getLogger("search_server")

# Apply the weights' constants sidecar the same way config.py does locally.
# Without it the engine silently uses the CHAMPION's constants (tau=2.5)
# instead of the net's, and every search here would be subtly wrong.
_weights = os.environ.get("PE_NN_WEIGHTS")
if _weights:
    _sidecar = os.path.splitext(_weights)[0] + ".constants.json"
    if os.path.isfile(_sidecar):
        for _k, _v in json.load(open(_sidecar)).items():
            if _k.startswith("PE_") and _k not in os.environ:
                os.environ[_k] = str(_v)
        logger.info("applied constants sidecar %s", _sidecar)
    else:
        logger.warning("NO SIDECAR at %s - engine defaults will be used", _sidecar)

from poke_engine import State, monte_carlo_tree_search, engine_config  # noqa: E402

# FAIL LOUD on a netless worker. With PE_NN_WEIGHTS unset the engine silently
# falls back to the classic hand eval AND -- because the matchup skip is gated on
# nn_active() -- rebuilds the 6x6 1v1 MatchupMatrix on every decision, spending
# ~10% of the move budget on 36 mini-searches whose result the net would never
# have read. Both failures are invisible in the logs, and this worker exists
# precisely to run the net, so a missing net is a misconfiguration, not a mode.
_cfg = engine_config()
logger.info("engine: %s", _cfg)
if "nn_active=true" not in _cfg:
    if os.environ.get("FP_ALLOW_NO_NN") == "1":
        logger.error("NO NET LOADED - classic eval + matchup rebuild per decision "
                     "(FP_ALLOW_NO_NN=1 set, continuing anyway)")
    else:
        raise SystemExit(
            "search_server refusing to start with no net: PE_NN_WEIGHTS is unset or "
            "failed to load, so every search would use the classic eval and burn ~10% "
            "of the budget rebuilding the matchup matrix. Set PE_NN_WEIGHTS=<...>.bin "
            "(and keep its .constants.json beside it), or set FP_ALLOW_NO_NN=1 to "
            "override deliberately."
        )

_POOL = None


def _search(args):
    state_string, search_time_ms, threads = args
    res = monte_carlo_tree_search(
        State.from_string(state_string), search_time_ms, threads=threads
    )
    side = lambda rows: [  # noqa: E731
        [m.move_choice, m.visits, m.total_score, getattr(m, "total_score_sq", 0.0)]
        for m in rows
    ]
    return {
        "side_one": side(res.side_one),
        "side_two": side(res.side_two),
        "total_visits": res.total_visits,
        "root_pairs": [[[v, t] for (v, t) in row] for row in (res.root_pairs or [])],
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):  # quieter than the default per-request line
        pass

    def _send(self, code, payload):
        body = json.dumps(payload).encode()
        hdrs = {}
        if "gzip" in (self.headers.get("Accept-Encoding") or ""):
            body = gzip.compress(body, 1)
            hdrs["Content-Encoding"] = "gzip"
        self.send_response(code)
        for k, v in hdrs.items():
            self.send_header(k, v)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.rstrip("/") == "/health":
            self._send(200, {"ok": True, "workers": _POOL._max_workers})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        if self.path.rstrip("/") != "/search":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(n)
            if self.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            req = json.loads(raw.decode())
            states = req["states"]
            ms = int(req["search_time_ms"])
            threads = int(req.get("threads", 1))
        except Exception as e:
            return self._send(400, {"error": "bad request: {!r}".format(e)})

        import time

        t0 = time.time()
        if len(states) > _POOL._max_workers:
            # never let this be silent again: it manifests as a client-side
            # timeout with no server-side error at all
            logger.warning(
                "REQUEST OF %d WORLDS EXCEEDS POOL OF %d -- will run in %d waves, "
                "wall time multiplied accordingly",
                len(states), _POOL._max_workers,
                -(-len(states) // _POOL._max_workers),
            )
        futures = [_POOL.submit(_search, (s, ms, threads)) for s in states]
        results = []
        dead = 0
        for i, fut in enumerate(futures):
            try:
                # generous per-future ceiling: the pool runs them concurrently,
                # so this bounds a hung worker, not the batch
                results.append(fut.result(timeout=ms / 1000.0 + 60))
            except Exception as e:
                logger.error("world %d died: %r", i, e)
                results.append(None)
                dead += 1
        logger.info(
            "search: %d worlds x %dms -> %.2fs (%d dead)",
            len(states),
            ms,
            time.time() - t0,
            dead,
        )
        self._send(200, {"results": results})


def main():
    global _POOL
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--bind", default="0.0.0.0")
    # MUST be >= the largest world count a client will send. The pool size is
    # the concurrency limit: 64 worlds through a 32-process pool runs as TWO
    # sequential waves, doubling wall time and blowing the client's timeout.
    # That is exactly how the first 64-world game died (turn 1: 52.98s).
    # Oversubscription is correct here -- it matches what the local pool does,
    # and each world is time-bounded, so more processes means less CPU each,
    # not more wall time.
    ap.add_argument("--workers", type=int, default=max(os.cpu_count() or 8, 64))
    args = ap.parse_args()

    _POOL = ProcessPoolExecutor(max_workers=args.workers)
    # warm the pool so the first real decision does not pay process spawn
    list(_POOL.map(int, range(args.workers)))
    logger.info("serving on %s:%d with %d workers", args.bind, args.port, args.workers)
    ThreadingHTTPServer((args.bind, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
