import argparse
import logging
import os
import sys
from enum import Enum, auto
from logging.handlers import RotatingFileHandler
from typing import Optional


class CustomFormatter(logging.Formatter):
    def format(self, record):
        lvl = "{}".format(record.levelname)
        return "{} {}".format(lvl.ljust(8), record.msg)


class CustomRotatingFileHandler(RotatingFileHandler):
    def __init__(self, file_name, **kwargs):
        self.base_dir = "logs"
        if not os.path.exists(self.base_dir):
            os.mkdir(self.base_dir)

        super().__init__("{}/{}".format(self.base_dir, file_name), **kwargs)

    def do_rollover(self, new_file_name):
        new_file_name = new_file_name.replace("/", "_")
        self.baseFilename = "{}/{}".format(self.base_dir, new_file_name)
        self.doRollover()


def init_logging(level, log_to_file):
    websockets_logger = logging.getLogger("websockets")
    websockets_logger.setLevel(logging.INFO)
    requests_logger = logging.getLogger("urllib3")
    requests_logger.setLevel(logging.INFO)

    # Gets the root logger to set handlers/formatters
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(level)
    stdout_handler.setFormatter(CustomFormatter())
    logger.addHandler(stdout_handler)
    FoulPlayConfig.stdout_log_handler = stdout_handler

    if log_to_file:
        file_handler = CustomRotatingFileHandler("init.log")
        file_handler.setLevel(logging.DEBUG)  # file logs are always debug
        file_handler.setFormatter(CustomFormatter())
        logger.addHandler(file_handler)
        FoulPlayConfig.file_log_handler = file_handler


class SaveReplay(Enum):
    always = auto()
    never = auto()
    on_loss = auto()
    on_win = auto()


class BotModes(Enum):
    challenge_user = auto()
    accept_challenge = auto()
    search_ladder = auto()


class _FoulPlayConfig:
    websocket_uri: str
    username: str
    password: str | None
    user_id: str
    avatar: str
    bot_mode: BotModes
    pokemon_format: str = ""
    smogon_stats: str = None
    search_time_ms: int
    first_turn_search_time_ms: int | None = None
    search_threads: int
    search_pool_workers: int | None = None
    parallelism: int
    run_count: int
    team_name: str
    team_list: str = None
    dump_team_dir: str = None
    switch_weight_multiplier: float = 1.0
    losing_attack_fallback_threshold: float = 0.05
    variance_penalty_lambda: float = 0.0
    switch_gate_tolerance: float = 0.01
    # near-tie mixing (selection.py _sample_near_ties); either 0 disables
    mix_visit_ratio: float = 0.8
    mix_score_tolerance: float = 0.01
    # two-phase committed-root probes (main.py): phase 1 nominates finalists,
    # phase 2 measures each with a dedicated forced-root search per world.
    # probe_phase1_ms=0 disables (single-phase search, today's behavior)
    probe_phase1_ms: int = 1500
    probe_phase2_budget_ms: int = 3000
    probe_ms_min: int = 500
    probe_ms_max: int = 2250
    probe_worlds: int = 8
    probe_margin: float = 0.04
    probe_floor_margin: float = 0.01
    probe_max_candidates: int = 3
    tera_margin_gate: float = 0.015
    losing_upside_threshold: float = 0.15
    losing_upside_displacement_multiplier: float = 2.0
    selection_argmax_only: bool = False
    reuse_search_pool: bool = True
    never_start_timer: bool = False
    user_to_challenge: str
    save_replay: SaveReplay
    room_name: str
    log_level: str
    log_to_file: bool
    stdout_log_handler: logging.StreamHandler
    file_log_handler: Optional[CustomRotatingFileHandler]

    def configure(self):
        parser = argparse.ArgumentParser()
        parser.add_argument(
            "--websocket-uri",
            required=True,
            help="The PokemonShowdown websocket URI, e.g. wss://sim3.psim.us/showdown/websocket",
        )
        parser.add_argument("--ps-username", required=True)
        parser.add_argument("--ps-password", default=None)
        parser.add_argument("--ps-avatar", default=None)
        parser.add_argument(
            "--bot-mode", required=True, choices=[e.name for e in BotModes]
        )
        parser.add_argument(
            "--user-to-challenge",
            default=None,
            help="If bot_mode is `challenge_user`, this is required",
        )
        parser.add_argument(
            "--pokemon-format", required=True, help="e.g. gen9randombattle"
        )
        parser.add_argument(
            "--smogon-stats-format",
            default=None,
            help="Overwrite which smogon stats are used to infer unknowns. If not set, defaults to the --pokemon-format value.",
        )
        parser.add_argument(
            "--search-time-ms",
            type=int,
            default=100,
            help="Time to search per battle in milliseconds",
        )
        parser.add_argument(
            "--first-turn-search-time-ms",
            type=int,
            default=None,
            help="Search time in milliseconds used for the battle's FIRST decision, "
            "falling back to --search-time-ms when unset — blitz grants ~40s on turn 1",
        )
        parser.add_argument(
            "--first-turn-world-multiplier",
            type=int,
            default=None,
            help="Worlds on the first decision = parallelism x this (default 4, or 2 "
            "under time pressure). Pin it when widening --search-parallelism, which "
            "otherwise multiplies the first-turn fan-out.",
        )
        parser.add_argument(
            "--search-remote-url",
            default=None,
            help="Base URL of a search_server.py worker (e.g. http://1.2.3.4:8000). "
            "When set, sampled worlds are searched THERE instead of in the local "
            "process pool; the websocket stays local because Showdown refuses "
            "authenticated logins from datacenter IPs. Any remote failure falls "
            "back to the local pool, so this can only cost strength, not games.",
        )
        parser.add_argument(
            "--search-remote-timeout-margin-ms",
            type=int,
            default=4000,
            help="Added to the per-world search time to form the remote HTTP "
            "timeout. Covers transfer plus the worker's own fan-out.",
        )
        parser.add_argument(
            "--probe-phase1-ms",
            type=int,
            default=None,
            help="Phase-1 budget per world for the two-phase committed-root probe. "
            "This OVERRIDES --search-time-ms on every non-first decision (see "
            "fp/search/main.py), so leaving it at the 1500ms class default silently "
            "caps normal turns regardless of --search-time-ms. Pass 0 to disable "
            "probing entirely and run a single-phase search at the full budget.",
        )
        parser.add_argument(
            "--search-parallelism",
            type=int,
            default=1,
            help="Number of states to search in parallel",
        )
        parser.add_argument(
            "--search-pool-workers",
            type=int,
            default=None,
            help="Worker processes in the search pool. Defaults to "
            "--search-parallelism. Set to the physical core count to run "
            "more worlds than cores in sequential full-speed waves instead "
            "of oversubscribing.",
        )
        parser.add_argument(
            "--search-threads",
            type=int,
            default=1,
            help="Number of threads to use per state",
        )
        parser.add_argument(
            "--run-count",
            type=int,
            default=1,
            help="Number of PokemonShowdown battles to run",
        )
        parser.add_argument(
            "--team-name",
            default=None,
            help="Which team to use. Can be a filename or a foldername relative to ./teams/teams/. "
            "If a foldername, a random team from that folder will be chosen each battle. "
            "If not set, defaults to the --pokemon-format value.",
        )
        parser.add_argument(
            "--team-list",
            default=None,
            help="A path to a text file containing a list of team names to choose from in order. Takes precedence over --team-name.",
        )
        parser.add_argument(
            "--switch-weight-multiplier",
            type=float,
            default=1.0,
            help="Multiplier applied to voluntary switch options' aggregated weights "
            "before final move selection. Values below 1.0 reduce how often the bot "
            "switches; 1.0 disables the correction. Forced switches are unaffected.",
        )
        parser.add_argument(
            "--losing-attack-fallback-threshold",
            type=float,
            default=0.05,
            help="When the best aggregated avg score falls below this, prefer the "
            "best-scoring non-switch option - real games have crit/miss/para tails "
            "that only pay off if you keep attacking; 0 disables.",
        )
        parser.add_argument(
            "--variance-penalty-lambda",
            type=float,
            default=0.0,
            help="Risk-aversion knob for move selection: each option's value is "
            "its sample-weighted mean of per-world (visit_share x avg_score), "
            "minus lambda x pooled_share x the std of its avg_score across "
            "sampled worlds. Penalizes moves whose quality depends on which "
            "hidden opponent set is real; 0 = risk-neutral per-world blend.",
        )
        parser.add_argument(
            "--switch-gate-tolerance",
            type=float,
            default=0.01,
            help="A voluntary switch or tera/mega option is only eligible for "
            "selection when its aggregated avg score is within this tolerance "
            "of the best plain move with >=5%% pooled visit share (or better). "
            "0 requires strict score dominance.",
        )
        parser.add_argument(
            "--losing-upside-threshold",
            type=float,
            default=0.15,
            help="Upper activation of the losing-position upside re-rank: "
            "below this score, options within a sliding tie band of the best "
            "are re-ranked by their best explored outcome vs any single "
            "opponent reply (root pair table). One LINEAR band: 0 at this "
            "threshold, equal to the dead-zone bound at the dead-zone bound "
            "(with defaults: (0.15,0) to (0.05,0.05), so 0.025 at 0.10) - "
            "at and below the dead zone the band covers every option. "
            "0 disables.",
        )
        parser.add_argument(
            "--losing-upside-displacement-multiplier",
            type=float,
            default=2.0,
            help="The upside challenger must exceed the incumbent pick's "
            "upside by (this x the current tie band) to overturn it - the "
            "deeper the position, the bigger the ceiling advantage demanded "
            "(defaults: 0.05 at score 0.10, 0.10 at 0.05). A non-switch "
            "challenger still displaces a switch incumbent at "
            "equal-or-better upside.",
        )
        parser.add_argument(
            "--tera-margin-gate",
            type=float,
            default=0.015,
            help="A tera/mega option (a once-per-battle resource spend) is only "
            "eligible for selection when its aggregated avg score BEATS the "
            "best non-tera alternative by at least this margin. Stops "
            "tie-break-level edges from spending tera (the T1 tera-Fire "
            "Flare Blitz that won the argmax by 0.006 with five opponent "
            "mons unrevealed). 0 disables.",
        )
        parser.add_argument(
            "--no-search-pool-reuse",
            action="store_true",
            help="Recreate the search worker pool for every decision instead of "
            "reusing it across decisions. Reuse skips ~parallelism process "
            "spawns per move; either way a rebuilt poke_engine wheel is only "
            "picked up after a bot restart.",
        )
        parser.add_argument(
            "--dump-team-dir",
            default=None,
            help="If set, the bot's team in random battle formats is written to "
            "<dump-team-dir>/<pokemon-format>/<battle-tag>.txt in Showdown export format "
            "once it is revealed at battle start. Each team is also mirrored into "
            "./teams/teams/<gen>/sampled/ so it can be re-used via --team-name.",
        )
        parser.add_argument(
            "--never-start-timer",
            action="store_true",
            help="When enabled, the bot will not send '/timer on' at battle start",
        )
        parser.add_argument(
            "--save-replay",
            default="never",
            choices=[e.name for e in SaveReplay],
            help="When to save replays",
        )
        parser.add_argument(
            "--room-name",
            default=None,
            help="If bot_mode is `accept_challenge`, the room to join while waiting",
        )
        parser.add_argument(
            "--nn-weights",
            default=None,
            help="Path to valuenet weights (.bin). Sets PE_NN_WEIGHTS for the engine, "
            "switching leaf evaluation from the hand eval to the neural net. Every other "
            "net-mode constant (reward form, tau, both UCB exploration factors, FPU) is "
            "already the engine DEFAULT, so this is the only flag needed to run the net bot. "
            "Refuses to start if the file is missing — a silent fallback to the hand eval "
            "is the failure this flag exists to prevent.",
        )
        parser.add_argument(
            "--selection-argmax-only",
            action="store_true",
            help="Pick the pooled visit-share argmax and bypass EVERY selection gate "
            "(switch damping, variance penalty, opponent switch veto, pooled-share "
            "floors, score-band and displacement tiebreaks, losing-position fallback). "
            "This is the policy every Elo measurement was actually taken under — the "
            "duel harness reads the raw MCTS arm and never calls selection.py — and the "
            "gates' thresholds were sized for the hand eval's value scale, so they are "
            "unverified under the net.",
        )
        parser.add_argument("--log-level", default="DEBUG", help="Python logging level")
        parser.add_argument(
            "--log-to-file",
            action="store_true",
            help="When enabled, DEBUG logs will be written to a file in the logs/ directory",
        )

        args = parser.parse_args()
        # Set PE_NN_WEIGHTS before ANY engine call and before the search pool is
        # created: poke-engine reads every PE_* var through a Rust LazyLock, i.e.
        # once per process, then caches forever. A worker that has already
        # resolved that LazyLock would keep the hand eval no matter what we set
        # later — silently, with no error.
        self.nn_constants_note = None
        if args.nn_weights:
            weights = os.path.abspath(os.path.expanduser(args.nn_weights))
            if not os.path.isfile(weights):
                raise SystemExit(
                    "--nn-weights: no such file: %s\nRefusing to start: without it the "
                    "engine silently falls back to the hand eval." % weights
                )
            os.environ["PE_NN_WEIGHTS"] = weights
            # A net's MCTS constants are PROPERTIES OF THAT NET, not global
            # defaults: tau = 1/(mean per-move log-odds swing) and c = r*tau^2
            # are measured from the net's own trajectories, so every retrain
            # moves them (champion tau 2.5 -> v4 baseline 2.851). Carrying them
            # in a sidecar next to the .bin makes them travel with the weights,
            # so a net cannot be run on another net's search settings. That
            # mismatch is the worst failure mode we have: it does not crash, does
            # not warn, and does not show up in validation loss -- it just looks
            # like "the new net didn't help".
            # Explicit PE_TUNE_* in the environment still wins, for experiments.
            sidecar = os.path.splitext(weights)[0] + ".constants.json"
            if os.path.isfile(sidecar):
                import json

                applied, skipped = [], []
                for k, v in json.load(open(sidecar)).items():
                    if not k.startswith("PE_"):
                        continue  # provenance fields (tau, sigma, derived_from)
                    if k in os.environ:
                        skipped.append(k)
                    else:
                        os.environ[k] = str(v)
                        applied.append("%s=%s" % (k, v))
                self.nn_constants_note = "from %s: %s%s" % (
                    os.path.basename(sidecar),
                    " ".join(applied) or "(none)",
                    " | env override kept: %s" % ",".join(skipped) if skipped else "",
                )
            else:
                self.nn_constants_note = (
                    "NO SIDECAR at %s -- engine defaults will be used, which are "
                    "the CHAMPION's constants, not this net's" % os.path.basename(sidecar)
                )
        self.nn_weights = os.environ.get("PE_NN_WEIGHTS")
        self.selection_argmax_only = args.selection_argmax_only
        self.websocket_uri = args.websocket_uri
        self.username = args.ps_username
        self.password = args.ps_password
        self.avatar = args.ps_avatar
        self.bot_mode = BotModes[args.bot_mode]
        self.pokemon_format = args.pokemon_format
        self.smogon_stats = args.smogon_stats_format
        self.search_time_ms = args.search_time_ms
        self.first_turn_search_time_ms = args.first_turn_search_time_ms
        if args.probe_phase1_ms is not None:
            self.probe_phase1_ms = args.probe_phase1_ms
        self.first_turn_world_multiplier = args.first_turn_world_multiplier
        self.search_remote_url = args.search_remote_url
        self.search_remote_timeout_margin_ms = args.search_remote_timeout_margin_ms
        self.parallelism = args.search_parallelism
        self.search_pool_workers = args.search_pool_workers
        self.search_threads = args.search_threads
        self.run_count = args.run_count
        self.team_name = args.team_name or self.pokemon_format
        self.team_list = args.team_list
        self.dump_team_dir = args.dump_team_dir
        self.switch_weight_multiplier = args.switch_weight_multiplier
        self.losing_attack_fallback_threshold = args.losing_attack_fallback_threshold
        self.variance_penalty_lambda = args.variance_penalty_lambda
        self.switch_gate_tolerance = args.switch_gate_tolerance
        self.tera_margin_gate = args.tera_margin_gate
        self.losing_upside_threshold = args.losing_upside_threshold
        self.losing_upside_displacement_multiplier = (
            args.losing_upside_displacement_multiplier
        )
        self.reuse_search_pool = not args.no_search_pool_reuse
        self.never_start_timer = args.never_start_timer
        self.user_to_challenge = args.user_to_challenge
        self.save_replay = SaveReplay[args.save_replay]
        self.room_name = args.room_name
        self.log_level = args.log_level
        self.log_to_file = args.log_to_file

        self.validate_config()

    def requires_team(self) -> bool:
        return not (
            "random" in self.pokemon_format or "battlefactory" in self.pokemon_format
        )

    def validate_config(self):
        if self.bot_mode == BotModes.challenge_user:
            assert (
                self.user_to_challenge is not None
            ), "If bot_mode is `CHALLENGE_USER`, you must declare USER_TO_CHALLENGE"


FoulPlayConfig = _FoulPlayConfig()
