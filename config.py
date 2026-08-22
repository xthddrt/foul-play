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
        # FP_LOG_SUBDIR isolates concurrent processes' logs (multi-battle same
        # account): two handlers sharing logs/init.log would cross-contaminate
        # on rollover (rename + still-open fd = one bot's lines in the other's
        # battle log).
        self.base_dir = "logs"
        sub = os.environ.get("FP_LOG_SUBDIR")
        if sub:
            self.base_dir = os.path.join("logs", sub)
        os.makedirs(self.base_dir, exist_ok=True)

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
    emit_infostate_rows: str = None
    truestate_net: str = None
    truestate_temperature: float = 0.0
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
    # OFF by default. It overrode --search-time-ms on every non-first decision,
    # so a 4500ms budget silently ran at 1500ms. It was never proven to help
    # (HANDOFF.md: "ground-truth playouts on its motivating turn came back a
    # statistical tie"), it existed to compensate for eval error that the v6 net
    # now handles, it is dead code under --selection-argmax-only (which returns a
    # single candidate, so phase 2 never fires), and its turn-1 clock overrun is
    # listed as a ladder blocker. Pass --probe-phase1-ms N to re-enable.
    probe_phase1_ms: int = 0
    probe_phase2_budget_ms: int = 3000
    probe_ms_min: int = 500
    probe_ms_max: int = 2250
    probe_worlds: int = 8
    probe_margin: float = 0.04
    probe_floor_margin: float = 0.01
    probe_max_candidates: int = 3
    tera_margin_gate: float = 0.015
    # Argmax-path tera gate (selection.py _apply_argmax_tera_gate). 0 = OFF, so
    # the champion's measured configuration is unchanged unless asked for.
    # score margin = tera_gate_score_per_mon x (opp_alive + opp_unrevealed)
    # Per-turn clock increment of the format (Showdown Blitz: +5s/turn, bank
    # capped at 15s — data/rulesets.ts:764-777). Spending more than this per
    # turn IN TOTAL drains the bank until the game is lost on time, with every
    # turn looking individually fine. Auto-set for blitz formats in
    # configure(); 0 disables the cap.
    turn_increment_ms: int = 0
    turn_overhead_margin_ms: int = 1500
    tera_gate_score_per_mon: float = 0.0
    tera_gate_visit_frac: float = 0.5
    # RB-SWITCH GATE (Sally 2026-08-19). Switching into a still-unrevealed
    # Revival Blessing carrier must beat the best alternative holding >=
    # rb_switch_gate_visit_frac (0.15) pooled visit share by
    # rb_switch_gate_per_alive x (5 - our_dead) on agg_score, else that
    # alternative is played instead. See selection._apply_argmax_rb_switch_gate.
    #
    # 0.003 -> max margin 0.015 at a full team, 0 when the carrier is our last
    # mon (switching to it is then forced). For scale: measured argmax-vs-
    # runner-up Q gaps run ~0.003 median, and the DUEL-TUNED tera gate peaks at
    # 0.010 (--tera-gate-q-margin 0.01). So this is deliberately 1.5x the tera
    # gate even though RB is the cheaper action -- switching the carrier in
    # REVEALS it but does NOT spend Revival Blessing. Chosen for
    # restrictiveness, not parity: over 33 probe games it gates 14/23
    # opportunities vs 12/23 at 0.002, the two extra being +0.0065/+0.0069 gaps
    # at 4 alive -- it overrides modest preferences, not only dead heats. An
    # earlier 0.005 spec was a near-total ban (max margin 0.025, above nearly
    # every observed gap) and was walked back. 0.0 disables the gate.
    #
    # visit_frac 0.15: at 0.25 several allows were accidents of the threshold
    # (no alternative cleared the bar, so the score test never ran). 0.15 makes
    # them decide on the margin instead, and matched 0.10 exactly on 20 games.
    rb_switch_gate_per_alive: float = 0.003
    rb_switch_gate_visit_frac: float = 0.15
    # Extra score margin demanded while the OPPONENT still holds their own
    # tera: their comeback potential is higher, so ours is worth more held.
    tera_gate_opp_tera_bonus: float = 0.002
    # Q-scaled argmax-path tera gate (Sally 2026-08-17, replaces the per-mon
    # margin when set): margin = tera_gate_q_margin x (4Q(1-Q))^2 with Q = the
    # best considered non-tera's score, so the knob IS the margin demanded in
    # a level game (production 0.01 = 1%). Squared: sigmoid compression AND
    # the held option's remaining relevance both scale with Q(1-Q), so the
    # rent decays fast once a game tilts either way. Consideration set
    # unchanged (visit-frac bar).
    tera_gate_q_margin: float = 0.0
    # Endgame playout gate (fp/search/epg.py). 0 = off.
    endgame_playout_gate: int = 0
    losing_upside_threshold: float = 0.15
    losing_upside_displacement_multiplier: float = 2.0
    selection_argmax_only: bool = False
    # sample among near-equal argmax candidates (visit >= 25% of argmax, score
    # >= argmax's) weighted by visit_share * avg_score -- anti-readability mix
    selection_mix: bool = False
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
            default=2000,
            help="Added to the per-world search time to form the remote HTTP "
            "timeout. Covers transfer plus the worker's own fan-out.",
        )
        parser.add_argument(
            "--opponent-switch-keep",
            type=float,
            default=None,
            help="Probability a VOLUNTARY opponent switch survives the in-tree veto "
            "(1.0 = veto off, the engine default). The veto models HUMAN inertia -- "
            "real ladder players switch on ~15-25%% of free turns while the in-tree "
            "best-responder switches 58-70%%. It applies to side_two ONLY, so it must "
            "be 1.0 for self-play generation or the two sides play different policies "
            "(measured: side_one won 56.53%% of 2,065 r2 games, z=+5.9). Pass 0.8 to "
            "restore the historical ladder behaviour.",
        )
        parser.add_argument(
            "--opponent-phantom-switch-keep",
            type=float,
            default=None,
            help="Multiplier on --opponent-switch-keep when the switch target is an "
            "UNREVEALED sampled mon (1.0 = off). Models existence-uncertainty: a "
            "determinized world treats its guess as a certainty and deploys it "
            "(measured 44%% of side_two root policy switching into fill-ins that did "
            "not exist). Only meaningful when worlds contain unknowns, so it is inert "
            "during generation. Pass 0.6 to restore the historical ladder behaviour.",
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
            "--tera-gate-per-mon",
            type=float,
            default=None,
            help="Enable the argmax-path tera gate. A tera/mega is only spent if "
            "it beats every CONSIDERED non-tera option's ave_score by this value "
            "x (opponent mons alive + opponent mons unrevealed). 'Considered' "
            "means pooled visit share >= --tera-gate-visit-frac of the tera's, so "
            "a barely-searched move cannot veto a tera the search overwhelmingly "
            "wants. If nothing is considered, the tera is allowed. If the gate "
            "blocks, the pooled-visit argmax of the considered non-tera options is "
            "played instead. 0 (default) disables.",
        )
        parser.add_argument(
            "--tera-gate-visit-frac",
            type=float,
            default=None,
            help="Fraction of the SUMMED pooled visit share of all tera arms a "
            "non-tera option must reach to be considered by --tera-gate-per-mon. "
            "Default 0.5.",
        )
        parser.add_argument(
            "--tera-gate-opp-tera-bonus",
            type=float,
            default=None,
            help="Extra margin added to the tera gate floor while the opponent "
            "still holds THEIR tera. Default 0.002.",
        )
        parser.add_argument(
            "--tera-gate-q-margin",
            type=float,
            default=None,
            help="Q-scaled argmax-path tera gate; when set (>0) it REPLACES the "
            "per-mon margin: a tera/mega is spent only if it beats the best "
            "CONSIDERED non-tera's ave_score by this value x (4Q(1-Q))^2, Q "
            "being that non-tera's score — i.e. this IS the margin at Q=0.5. "
            "Consideration still uses --tera-gate-visit-frac. 0 (default) "
            "keeps the per-mon formula.",
        )
        parser.add_argument(
            "--endgame-playout-gate",
            type=int,
            default=None,
            help="1 = in small-roster positions (total alive <= 6, live Q) the "
            "final move is decided by forced-arm playouts over the top-4 arms "
            "(fp/search/epg.py; Sally 2026-08-18 spec: 2.5k iters, n by alive "
            "2:16/3:24/4:32/5:40/6:48 total, up to 8 worlds, CRN, top-4 worlds). 0 (default) off.",
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
            "--truestate-net",
            default=None,
            help="Path to a true-state net checkpoint (truestate/train.py). When "
            "set, moves are chosen by ONE forward pass over the true information "
            "state -- no MCTS, no world sampling. Falls back to the normal search "
            "for any decision the net cannot decode, so this can never produce an "
            "illegal command.",
        )
        parser.add_argument(
            "--truestate-temperature",
            type=float,
            default=0.0,
            help="Sampling temperature over the masked policy logits when "
            "--truestate-net is set. 0 (default) is argmax, i.e. exactly the "
            "deploy-time behaviour; RL data generation uses 1.0 so the corpus "
            "contains the exploration the policy gradient needs.",
        )
        parser.add_argument(
            "--emit-infostate-rows",
            default=None,
            help="If set, one info-state JSON row per decision (normal move pick, "
            "forced switch, revival) is appended to "
            "<dir>/<battle-tag>_<side>.rows.jsonl. See truestate/SCHEMA.md for the "
            "v1 row schema. Serialization failures are logged and skipped, never "
            "propagated into the game.",
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
            "--selection-mix",
            action="store_true",
            help="With --selection-argmax-only: sample among near-equal candidates "
            "(pooled visit share >= 25%% of the argmax's AND average score >= the "
            "argmax's) weighted by visit_share * avg_score, instead of the "
            "deterministic argmax. Anti-readability: two 2026-08-21 ladder losses "
            "were multi-turn single-move loops into opponent lines the model itself "
            "predicted at 80%%+. Gates (tera, rb-switch) still vet the mixed pick.",
        )
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
        # ---- PHANTOM opponent-model knobs (engine-side; documented here so the
        # config is the one place that knows they exist and what they do).
        # All read by poke-engine through Rust LazyLocks (once per process) and
        # only ACTIVE when fp/search/main.py passes per-world phantom masks,
        # which it does iff PE_PHANTOM_MODE is "soft" or "cut".
        #
        #   PE_PHANTOM_MODE        off | cut | soft
        #     soft (production): arms switching into a masked never-revealed
        #       slot keep full Q but get a reduced exploration bonus.
        #     cut: any node where a masked mon is ACTIVE becomes a leaf
        #       (net eval, no expansion). Duel-measured ~+7 Elo vs off.
        #   PE_PHANTOM_ALPHA       (0)   exploration mult for THEIR phantom
        #       switches — how hard we discount lines about mons we invented.
        #   PE_PHANTOM_SELF_AS_SEEN (0.2) the weight the MODELED OPPONENT puts
        #       on lines using our not-yet-revealed switches: their tree
        #       statistics skip those samples with probability 1 - as_seen, so
        #       their replies stop bracing for arrivals they cannot see and
        #       surprise value backs up into our lines. Root pair table stays
        #       true. 1.0 = off. Independent of every other knob.
        #   (PE_PHANTOM_ALPHA_SELF was REMOVED 2026-08-17 — we know our own
        #    team, so discounting our own switches had no epistemic basis.)
        #
        # Masks-off / knobs-off is bit-exact with the pre-feature engine
        # (parity-gated 2026-08-17). Launchers (run_game.sh / run_parallel.sh)
        # export the production values; RG_PHANTOM_* overrides per run.
        self.phantom_note = " ".join(
            "{}={}".format(k, os.environ.get(k, "<unset>"))
            for k in (
                "PE_PHANTOM_MODE",
                "PE_PHANTOM_ALPHA",
                "PE_PHANTOM_SELF_AS_SEEN",
            )
        )
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
        # Set BEFORE any poke_engine search runs: the engine reads these once
        # via LazyLock, so a later assignment is silently ignored.
        for flag, env in (
            (args.opponent_switch_keep, "PE_TUNE_OPPONENT_VOLUNTARY_SWITCH_KEEP"),
            (args.opponent_phantom_switch_keep, "PE_TUNE_OPPONENT_PHANTOM_SWITCH_KEEP"),
        ):
            if flag is not None:
                os.environ[env] = str(flag)
        self.opponent_switch_keep = args.opponent_switch_keep
        self.opponent_phantom_switch_keep = args.opponent_phantom_switch_keep
        self.nn_weights = os.environ.get("PE_NN_WEIGHTS")
        self.selection_argmax_only = args.selection_argmax_only
        self.selection_mix = args.selection_mix
        if args.tera_gate_per_mon is not None:
            self.tera_gate_score_per_mon = args.tera_gate_per_mon
        if args.tera_gate_visit_frac is not None:
            self.tera_gate_visit_frac = args.tera_gate_visit_frac
        if args.tera_gate_opp_tera_bonus is not None:
            self.tera_gate_opp_tera_bonus = args.tera_gate_opp_tera_bonus
        if args.tera_gate_q_margin is not None:
            self.tera_gate_q_margin = args.tera_gate_q_margin
        if args.endgame_playout_gate is not None:
            self.endgame_playout_gate = args.endgame_playout_gate
        self.websocket_uri = args.websocket_uri
        self.username = args.ps_username
        self.password = args.ps_password
        self.avatar = args.ps_avatar
        self.bot_mode = BotModes[args.bot_mode]
        self.pokemon_format = args.pokemon_format
        # Blitz is an increment clock: +10s/turn effective, 15s bank cap
        # (rulesets.ts:764-777 — "Add Per Turn IS 5, TRANSLATING TO AN
        # INCREMENT OF 10"). Cap per-turn search so a config can never spend
        # past the increment and bleed the bank. At the default margin this
        # yields 8500ms sustainable — no effect on the standard 4000-4500ms
        # configs, pure guard rail against overambitious ones.
        if self.turn_increment_ms == 0 and "blitz" in (args.pokemon_format or ""):
            self.turn_increment_ms = 10000
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
        self.emit_infostate_rows = args.emit_infostate_rows
        self.truestate_net = args.truestate_net
        self.truestate_temperature = args.truestate_temperature
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
