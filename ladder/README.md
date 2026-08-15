# Ladder bot — everything needed to run the v8b champion

Backup copies (2026-08-15) of the pieces that live OUTSIDE the git repos in
Sally's workspace. Together with this repo (foul-play @ main) and
github.com/xthddrt/poke-engine @ main (build the `poke_engine` wheel from
`poke-engine-py` with `--features poke-engine/terastallization
--no-default-features`), this directory is sufficient to reconstruct the
working ladder bot.

| file | live location | what |
|---|---|---|
| `run_game.sh` | `<workspace>/ladder-games/` | plays ONE ranked gen9randombattleblitz game with the champion config (v8b net, argmax selection, tera gate) and archives it |
| `run_parallel.sh` | `<workspace>/ladder-games/` | N simultaneous games on one account (atomic battle claims) |
| `archive_game.py` | `<workspace>/ladder-games/` | archiver run_game.sh calls after the bot exits |
| `nets/v8b_s1.bin` | `<workspace>/valuenet/nets_v8b/` | PKNN v8 champion (370,645 params, sha256 af5df24b…) — promoted 2026-08-15 |
| `nets/v8b_s1.constants.json` | beside the .bin | **mandatory sidecar** — engine constants (absolute reward, tau 1.0, c 0.0345). Without it foul-play silently runs the previous champion's constants |
| `nets/v8b_s1.meta.json` / `.config.json` | beside the .bin | provenance |

Layout expectations: `run_game.sh` assumes a workspace root containing
`foul-play/` (with `.venv` and the poke_engine wheel installed), the net at
`../valuenet/nets_v8b/v8b_s1.bin` relative to foul-play (or set
`RG_NN_WEIGHTS`), and a `ladder-games/` dir for archives.

## Fresh-machine quickstart (nothing but the two repos)

```bash
mkdir workspace && cd workspace
git clone https://github.com/xthddrt/foul-play
git clone https://github.com/xthddrt/poke-engine
cd foul-play
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt   # builds poke_engine from ../poke-engine (needs Rust; NEVER from PyPI)
mkdir ../ladder-games && cp ladder/run_game.sh ladder/run_parallel.sh ladder/archive_game.py ../ladder-games/
cat > ../.env <<'ENV'
FP_PYTHON="$PWD/foul-play/.venv/bin/python"
FP_WEBSOCKET_URI="wss://sim3.psim.us/showdown/websocket"
FP_BOT_MODE="search_ladder"
PS_USERNAME="your-account"
PS_PASSWORD="your-password"
ENV
FP_ROOT="$(dirname "$PWD")" RG_NN_WEIGHTS="$PWD/ladder/nets/v8b_s1.bin" \
  bash ../ladder-games/run_game.sh
```

Notes: `FP_ROOT` points the launcher at your workspace (defaults to Sally's
path); `RG_NN_WEIGHTS` points at the net vendored here (the default path
expects a `valuenet/` dir that only exists in the original workspace); the
PS-exact team sampler uses the `sets.json` vendored at
`data/ps/gen9randombattle_sets.json` when no `pokemon-showdown` checkout is
present (env `PS_SETS_JSON` overrides). Randbats set statistics are fetched
by foul-play at battle start (network required). The engine wheel needs a
Rust toolchain (`rustup`) and builds in ~5-10 min.

Secrets: the launcher sources `<workspace>/.env`, which is NOT in git.
Required vars: `FP_PYTHON` (path to foul-play/.venv/bin/python),
`FP_WEBSOCKET_URI` (wss://sim3.psim.us/showdown/websocket), `FP_BOT_MODE`
(search_ladder), `PS_USERNAME`, `PS_PASSWORD` (+ optional `RG_USERNAME` /
`RG_PASSWORD` per-run account overrides).
