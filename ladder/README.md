# Ladder bot — everything needed to run the v10 champion

Backup copies (refreshed 2026-08-21) of the pieces that live OUTSIDE the git
repos in Sally's workspace. Together with this repo (foul-play @ main) and
github.com/xthddrt/poke-engine @ main (build the `poke_engine` wheel from
`poke-engine-py` with `--features poke-engine/terastallization
--no-default-features`), this directory is sufficient to reconstruct the
working ladder bot.

| file | live location | what |
|---|---|---|
| `run_game.sh` | `<workspace>/ladder-games/` | plays ONE ranked gen9randombattleblitz game with the champion config and archives it |
| `run_parallel.sh` | `<workspace>/ladder-games/` | N simultaneous games on one account (atomic battle claims) |
| `archive_game.py` | `<workspace>/ladder-games/` | archiver run_game.sh calls after the bot exits |
| `nets/v10c0.bin` | `<workspace>/valuenet/nets_v10/` | PKNN v8-format champion (370,645 params, sha256 8259a84a…) — promoted 2026-08-20 |
| `nets/v10c0.constants.json` | beside the .bin | **mandatory sidecar** — engine constants (absolute reward, tau 1.0, UCB c 0.02 both sides). Without it foul-play silently runs the previous champion's constants |
| `nets/v10c0.meta.json` | beside the .bin | provenance + full measurement record |
| `nets/v8b_*.bin`, `v8c_*` | — | previous champions, kept for regression duels |

## The exact champion spec (what `run_game.sh` runs)

- **Net**: `v10c0.bin` (sha256 `8259a84a0c1b24fc650257a925de4a29dd4aa08e1ab6960a1f60feb56a51e2ab`).
  Trained by `corrections/v10_train.py` on the 3,995,769-row / 1,367,256-game
  fresh v10 corpus, full-coverage corpus+mirror epochs, seed 1, best-holdout
  checkpoint (Brier 0.023935). Exported via `evallab/export_v8.py`.
- **Sidecar constants** (auto-applied by foul-play when `--nn-weights` points
  at the bin): `PE_NN_REWARD=absolute`, `PE_NN_TAU=1.0`,
  `PE_TUNE_UCB_EXPLORATION=0.02`, `PE_TUNE_OPPONENT_UCB_EXPLORATION=0.02`
  (12k-iteration plateau [0.019,0.042] mapped to ladder budget via lnN).
- **Search**: 4500 ms/turn, 14000 ms first turn, 8 sampled worlds on an
  8-process pool, `--search-threads 1`.
- **Opponent model (phantom)**: `PE_PHANTOM_MODE=soft`, `PE_PHANTOM_ALPHA=0`
  (sampled never-revealed opponent mons get no exploration bonus),
  `PE_PHANTOM_SELF_AS_SEEN=0.2`.
- **Move selection**: mixing ON by default (`RG_MIX=0` reverts to pure
  argmax): sample among candidates with visit ≥ 25% of the argmax AND avg
  score ≥ the argmax's, weighted by visit×score (anti-readability).
- **Gates**: tera gate `--tera-gate-q-margin 0.01 --tera-gate-visit-frac
  0.25`; endgame playout gate OFF by default (`RG_EPG=1` enables).
- **Format**: `gen9randombattleblitz` (override `RG_FORMAT`).
- **Measured strength**: round-robin 39,312 games @ 12k iters — **+12.1 Elo
  vs v9c0, +45.8 vs v8c_s1**, transitivity checked. Evaluator: 0.023935
  shared-holdout Brier (v9c0 0.024546, v8c_s1 0.033267); neutral bench_v1
  0.02709 (v9c0 0.02942). Full record in `nets/v10c0.meta.json`.

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
FP_ROOT="$(dirname "$PWD")" RG_NN_WEIGHTS="$PWD/ladder/nets/v10c0.bin" \
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
