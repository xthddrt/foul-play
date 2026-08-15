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

Secrets: the launcher sources `<workspace>/.env`, which is NOT in git.
Required vars: `FP_PYTHON` (path to foul-play/.venv/bin/python),
`FP_WEBSOCKET_URI` (wss://sim3.psim.us/showdown/websocket), `FP_BOT_MODE`
(search_ladder), `PS_USERNAME`, `PS_PASSWORD` (+ optional `RG_USERNAME` /
`RG_PASSWORD` per-run account overrides).
