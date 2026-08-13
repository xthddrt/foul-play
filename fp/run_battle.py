import json
import asyncio
import concurrent.futures
import os
import random
import time
from copy import deepcopy
import logging

from data.pkmn_sets import RandomBattleTeamDatasets, TeamDatasets
from data.pkmn_sets import SmogonSets
import constants
from constants import BattleType
from config import BotModes, FoulPlayConfig, SaveReplay
from fp.battle import LastUsedMove, Pokemon, Battle
from fp.battle_modifier import async_update_battle, process_battle_updates
from fp.helpers import normalize_name
from fp.infostate import emit_row
from fp.search.main import find_best_move
from fp.team_dump import dump_team_from_request_json

from fp.websocket_client import PSWebsocketClient

logger = logging.getLogger(__name__)

# --- battle-start tunables (module-level so tests can compress time) ---
SEARCH_RECV_TIMEOUT_S = 30  # watchdog wakes at least this often
SEARCH_STALE_S = 45  # in-flight search unconfirmed this long => assume dead
SEARCH_STALE_JITTER_S = 45  # spread fleet re-searches (shared IP budget)
RESCUE_JOIN_S = 60  # single-process: live game but no |init| => /join it
BOOTSTRAP_TIMEOUT_S = 240  # claim -> first choice sent must finish within this


def format_decision(battle, decision):
    # Formats a decision for communication with Pokemon-Showdown
    # If the move can be used as a Z-Move, it will be

    # A recharge turn is the one move request where the engine's only legal
    # action is `MoveChoice::None`: the `mustrecharge` volatile short-circuits
    # `add_available_moves` (poke-engine src/genx/state.rs:1276-1281).  PS expects
    # the `recharge` pseudo-move it invented for that slot (sim/pokemon.ts:973-977),
    # which owns no moveset entry - so this must be answered before the
    # `battle.user.active.get_move(decision)` lookup below.
    # The arm serializes as "No Move" (poke-engine-py movechoice_to_string), so
    # match every spelling; answer with the request's forced move rather than a
    # hardcoded `recharge` so an unexpected placeholder can never produce an
    # illegal command (2661937105: `/choose move No Move` -> inactivity loss).
    if decision.lower().replace(" ", "") in ("none", "nomove"):
        request = battle.request_json or {}
        active = (request.get(constants.ACTIVE) or [{}])[0]
        moves = [m for m in active.get(constants.MOVES, []) if not m.get("disabled")]
        if len(moves) == 1:
            return ["/choose move {}".format(moves[0]["id"]), str(battle.rqid)]
        return ["/choose default", str(battle.rqid)]

    if decision.startswith(constants.SWITCH_STRING + " "):
        switch_pokemon = decision.split("switch ")[-1]
        for pkmn in battle.user.reserve:
            if pkmn.name == switch_pokemon:
                message = "/switch {}".format(pkmn.index)
                break
        else:
            raise ValueError("Tried to switch to: {}".format(switch_pokemon))
    else:
        tera = False
        mega = False
        if decision.endswith("-tera"):
            decision = decision.replace("-tera", "")
            tera = True
        elif decision.endswith("-mega"):
            decision = decision.replace("-mega", "")
            mega = True
        message = "/choose move {}".format(decision)

        if battle.user.active.can_mega_evo and mega:
            message = "{} {}".format(message, constants.MEGA)
        elif battle.user.active.can_ultra_burst:
            message = "{} {}".format(message, constants.ULTRA_BURST)

        # only dynamax on last pokemon
        if battle.user.active.can_dynamax and all(
            p.hp == 0 for p in battle.user.reserve
        ):
            message = "{} {}".format(message, constants.DYNAMAX)

        if tera:
            message = "{} {}".format(message, constants.TERASTALLIZE)

        # get_move returns None when our move-tracking desynced from PS
        move = battle.user.active.get_move(decision)
        if move is not None and move.can_z:
            message = "{} {}".format(message, constants.ZMOVE)

    return [message, str(battle.rqid)]


def battle_is_finished(battle_tag, msg):
    return (
        msg.startswith(">{}".format(battle_tag))
        and (constants.WIN_STRING in msg or constants.TIE_STRING in msg)
        and constants.CHAT_STRING not in msg
    )


def extract_battle_factory_tier_from_msg(msg):
    start = msg.find("Battle Factory Tier: ") + len("Battle Factory Tier: ")
    end = msg.find("</b>", start)
    tier_name = msg[start:end]

    return normalize_name(tier_name)


def fallback_choice(battle):
    # last-resort legal choice when the search dies: any legal move beats
    # forfeiting on time
    request = battle.request_json or {}
    if not request.get(constants.FORCE_SWITCH):
        active = request.get(constants.ACTIVE) or [{}]
        for m in active[0].get(constants.MOVES, []):
            if not m.get("disabled"):
                return m["id"]
    for p in battle.user.reserve:
        if p.hp > 0:
            return "switch {}".format(p.name)
    return battle.user.active.moves[0].name


async def async_pick_move(battle):
    # increment on the REAL battle (find_best_move only ever sees a copy)
    # so the first-decision extended search applies exactly once per battle
    battle.decisions_made = getattr(battle, "decisions_made", 0) + 1
    battle_copy = deepcopy(battle)

    loop = asyncio.get_event_loop()
    with concurrent.futures.ThreadPoolExecutor() as pool:
        try:
            # the request-json update lives inside the try as well: a desync
            # there must degrade to a legal move, not forfeit the game
            if not battle_copy.team_preview:
                battle_copy.user.update_from_request_json(battle_copy.request_json)
            # TRUE-STATE NET: one forward pass over the real information state,
            # no search and no world sampling -- that is the whole premise of
            # the model. It runs INSIDE this try, and returning None falls
            # through to the normal search, so a decode miss degrades to the
            # champion bot rather than to an illegal command.
            best_move = None
            if FoulPlayConfig.truestate_net and not battle_copy.team_preview:
                try:
                    from fp.truestate_policy import pick_move as _ts_pick

                    best_move = await loop.run_in_executor(
                        pool, _ts_pick, battle_copy, FoulPlayConfig.truestate_net,
                        FoulPlayConfig.truestate_temperature,
                    )
                    if best_move is not None:
                        logger.info("truestate net chose: {}".format(best_move))
                except Exception as e:
                    logger.error("truestate net failed ({!r}); using search".format(e))
                    best_move = None
            if best_move is None:
                best_move = await loop.run_in_executor(pool, find_best_move, battle_copy)
        except asyncio.CancelledError:
            raise
        # BaseException: pyo3 panics escape `except Exception`
        except BaseException as e:
            logger.error(
                "find_best_move failed ({!r}); using fallback choice "
                "(turn={} rqid={} active={})".format(
                    e, battle.turn, battle.rqid, battle.user.active
                ),
                exc_info=True,
            )
            best_move = fallback_choice(battle_copy)

    # info-state row for this decision point (normal / forced switch / revival).
    # `battle_copy` is the state the search actually saw: the request JSON has
    # been folded in, so its move `disabled` flags and party indexes are the
    # ones the legal mask must be built from. Never raises (see fp.infostate).
    emit_row(battle_copy, best_move, FoulPlayConfig.emit_infostate_rows)

    try:
        battle.user.last_selected_move = LastUsedMove(
            battle.user.active.name,
            best_move.removesuffix("-tera").removesuffix("-mega"),
            battle.turn,
        )
        return format_decision(battle_copy, best_move)
    except asyncio.CancelledError:
        raise
    except BaseException as e:
        # formatting the choice failed (unknown switch target, missing move, ...)
        # - let PS pick a legal action rather than letting this kill the battle
        logger.error(
            "format_decision failed for {!r} ({!r}); sending default choice "
            "(turn={} rqid={})".format(best_move, e, battle.turn, battle.rqid),
            exc_info=True,
        )
        return ["/choose default", str(battle.rqid)]


async def handle_team_preview(battle, ps_websocket_client):
    battle_copy = deepcopy(battle)
    battle_copy.user.active = Pokemon.get_dummy()
    battle_copy.opponent.active = Pokemon.get_dummy()
    battle_copy.team_preview = True

    best_move = await async_pick_move(battle_copy)

    # because we copied the battle before sending it in, we need to update the last selected move here
    pkmn_name = battle.user.reserve[int(best_move[0].split()[1]) - 1].name
    battle.user.last_selected_move = LastUsedMove(
        "teampreview", "switch {}".format(pkmn_name), battle.turn
    )

    size_of_team = len(battle.user.reserve) + 1
    team_list_indexes = list(range(1, size_of_team))
    choice_digit = int(best_move[0].split()[-1])

    team_list_indexes.remove(choice_digit)
    message = [
        "/team {}{}|{}".format(
            choice_digit, "".join(str(x) for x in team_list_indexes), battle.rqid
        )
    ]

    await ps_websocket_client.send_message(battle.battle_tag, message)


def claim_battle(battle_tag):
    """Multi-battle containment: at most ONE local process owns each battle.

    Showdown joins every one of an account's connections to any new battle it
    starts (users.ts joinRoom fans out to all connections), so N processes on
    one account all see every battle begin. The first to atomically create
    FP_CLAIM_DIR/<tag>.claim owns it; everyone else leaves the room and keeps
    waiting. FP_CLAIM_DIR unset => single-process mode, always own (unchanged
    behavior). Battle tags are globally unique, so stale claim files are inert.
    """
    claim_dir = os.environ.get("FP_CLAIM_DIR")
    if not claim_dir:
        return True
    os.makedirs(claim_dir, exist_ok=True)
    try:
        fd = os.open(
            os.path.join(claim_dir, battle_tag + ".claim"),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
        )
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        return False


def _first_game_tag(updatesearch_payload, pokemon_format):
    # first live battle of OUR format from an |updatesearch| payload
    try:
        games = json.loads(updatesearch_payload.strip()).get("games") or {}
    except ValueError:
        return None
    prefix = "battle-{}-".format(pokemon_format)
    for tag in games:
        if tag.startswith(prefix):
            return tag
    return None


async def get_battle_tag_and_opponent(ps_websocket_client: PSWebsocketClient):
    """Wait for the next battle |init| and claim it; in ladder mode this loop
    also OWNS every /search (run.py no longer fires one at startup — that send
    raced the login-time |updatesearch| push and double-queued the account
    when it already had a live game: the 2664176854 ghost loss, 2026-08-13).

    The server's |updatesearch| pushes ({searching, games}) are the source of
    truth; they arrive on login, after every game, and on every state change:
      - our format in `searching` -> our search is alive, do nothing
      - idle (no search, games:null) and nothing of ours in flight -> search
      - idle but OUR search was just in flight -> MATCH FORMING: do NOT
        re-search. This transient is pushed the instant a match is found,
        before the battle room exists. The old watchdog re-armed right here
        whenever the queue had taken >20s, double-queuing the account; the
        second search matched a "ghost" battle nobody ever played (timer
        losses 2664147697/2664150396/2664156364/2664176268/2664176854 on
        2026-08-13 — each ghost id sits just above the real game's).
        Only if the in-flight search stays unconfirmed for SEARCH_STALE_S
        (cancelled server-side, push lost — the 2026-08-08 endodontist
        deadlock class) is it re-armed, jittered across fleet slots
        (2026-08-09: synchronized retries saturated the shared IP budget).
      - games active with no search (single-process): OUR battle — its
        |init|/rejoin dump normally follows within seconds; if it never comes
        (a rotting game from a dead session), /join it to force the dump.
    A guest demotion (sibling login or a browser logout) re-auths and lets
    the server's next push decide.
    """
    armed = False  # a /search of ours is believed in-flight
    armed_mono = 0.0
    entry_mono = time.monotonic()
    live_game_tag = None
    rescue_attempted = False
    fleet = bool(os.environ.get("FP_CLAIM_DIR"))

    async def _search():
        nonlocal armed, armed_mono
        await ps_websocket_client.search_for_match(FoulPlayConfig.pokemon_format)
        armed = True
        armed_mono = time.monotonic()

    while True:
        try:
            msg = await asyncio.wait_for(
                ps_websocket_client.receive_message(), timeout=SEARCH_RECV_TIMEOUT_S
            )
        except asyncio.TimeoutError:
            msg = None

        if FoulPlayConfig.bot_mode == BotModes.search_ladder:
            if msg is not None and "|updateuser|" in msg:
                fields = msg.split("|updateuser|")[1].split("|")
                named = fields[1].strip() if len(fields) > 1 else "1"
                if named == "0":
                    # the demotion also cancelled the account's searches
                    await ps_websocket_client.relogin()
                    armed = False
                    continue

            state = None
            if msg is not None and "|updatesearch|" in msg:
                payload = msg.split("|updatesearch|", 1)[1]
                # Only OUR format counts: with mixed-format slots on one
                # account (blitz + regular), a sibling's blitz search must
                # not convince a regular slot its own search is alive.
                searching_part = payload.split('"games"')[0]
                if '"{}"'.format(FoulPlayConfig.pokemon_format) in searching_part:
                    state = "searching"
                    armed = True  # adopt: it is our account's search
                    armed_mono = time.monotonic()
                elif '"games":null' in payload:
                    state = "idle"
                    live_game_tag = None
                else:
                    state = "in_game"
                    if not fleet:
                        # single-process: a live game with no search is OURS
                        # to play (fleet slots see sibling games here and must
                        # keep searching; the claim layer sorts ownership out)
                        live_game_tag = (
                            _first_game_tag(payload, FoulPlayConfig.pokemon_format)
                            or live_game_tag
                        )

            now = time.monotonic()
            if state == "searching":
                pass
            elif (
                state == "idle" or (fleet and state == "in_game")
            ) and not armed:
                await _search()
            elif (
                armed
                and live_game_tag is None
                and now - armed_mono
                > SEARCH_STALE_S + random.uniform(0, SEARCH_STALE_JITTER_S)
            ):
                # in-flight search unconfirmed for too long: assume it died
                # (an "already searching" popup is harmless if it did not)
                await _search()
            elif (
                not armed
                and state is None
                and live_game_tag is None
                and now - entry_mono > SEARCH_RECV_TIMEOUT_S
            ):
                # never saw an |updatesearch| at all: fail open, don't idle out
                await _search()

            if (
                live_game_tag is not None
                and not rescue_attempted
                and now - entry_mono > RESCUE_JOIN_S
            ):
                logger.warning(
                    "Live unplayed game {} — joining to force the room dump".format(
                        live_game_tag
                    )
                )
                await ps_websocket_client.join_room(live_game_tag)
                rescue_attempted = True

        if msg is None:
            continue

        split_msg = msg.split("|")
        first_msg = split_msg[0]
        # a NEW battle is the |init| block (">battle-...\n|init|battle...");
        # deinits and stray room lines also carry the room prefix and must not
        # be parsed as battles (split_msg[4] is the "X vs. Y" title, only
        # present in the init block)
        if "battle" in first_msg and "|init|" in msg and len(split_msg) > 4:
            battle_tag = first_msg.replace(">", "").strip()
            if not claim_battle(battle_tag):
                # another process owns it: detach this connection and make sure
                # a search is pending again (the match consumed the account's
                # search; "already searching" popups are harmless and ignored)
                logger.info("Claimed elsewhere, skipping: {}".format(battle_tag))
                await ps_websocket_client.leave_room_now(battle_tag)
                if FoulPlayConfig.bot_mode == BotModes.search_ladder:
                    await _search()
                continue
            user_name = FoulPlayConfig.username
            opponent_name = (
                split_msg[4].replace(user_name, "").replace("vs.", "").strip()
            )
            # Claiming a battle must leave ZERO pending searches: if this
            # battle came from a sibling's search, our own re-search is still
            # queued server-side and would later match into a battle no
            # process is free to play (observed: the orphaned d-dog2357 game,
            # 2026-08-08). Siblings that still want a search re-arm via the
            # watchdog above.
            if os.environ.get("FP_CLAIM_DIR"):
                await ps_websocket_client.send_message("", ["/cancelsearch"])
            logger.info("Initialized {} against: {}".format(battle_tag, opponent_name))
            # the msg is returned too: when this connection was joined to an
            # ALREADY-RUNNING battle (login while the account had a live
            # game), this |init| is the full room-history dump and carries
            # the |player|/|start block the bootstrap needs
            return battle_tag, opponent_name, msg


async def start_battle_common(
    ps_websocket_client: PSWebsocketClient,
    pokemon_battle_type,
    battle_tag,
    opponent_name,
    msg,
):
    battle = Battle(battle_tag)
    battle.opponent.account_name = opponent_name
    battle.pokemon_format = pokemon_battle_type
    battle.generation = pokemon_battle_type[:4]

    # Find the opponent's |player| line ('p1' or 'p2'), e.g.
    # '|player|p1|OpponentName|2|'. It is NOT always a future message: when
    # this connection was joined to an already-running battle, the |init| in
    # `msg` is the room-history dump and already CONTAINS the player lines
    # (and the |start block) — waiting for fresh messages then starves
    # forever (2664176268, 2026-08-13: 24 min wedged at turn 1, timer loss).
    # So the current msg is examined first, and the parse is line-based with
    # an exact name match: substring matching once latched onto
    # '|player|p2|<us>|...\n|inactive|... (requested by <opponent>)' and
    # assigned us the wrong side.
    # Tag-filtered: with multiple battles on one account, another battle's
    # |player| line must not initialize this one.
    while True:
        if not (msg.startswith(">battle") and battle.battle_tag not in msg):
            player_line = None
            for line in msg.split("\n"):
                parts = line.split("|")
                if (
                    len(parts) > 3
                    and parts[1] == "player"
                    and parts[2].strip() in constants.ID_LOOKUP
                    and parts[3].strip() == battle.opponent.account_name
                ):
                    player_line = parts
                    break
            if player_line is not None:
                battle.opponent.name = player_line[2].strip()
                battle.user.name = constants.ID_LOOKUP[battle.opponent.name]
                # |player|p1|Name|avatar|rating - rating present on ladder games
                battle.opponent.account_rating = (
                    player_line[5].strip()
                    if len(player_line) > 5 and player_line[5].strip()
                    else None
                )
                break
        msg = await ps_websocket_client.receive_message()

    return battle, msg


async def get_first_request_json(
    ps_websocket_client: PSWebsocketClient, battle: Battle
):
    while True:
        msg = await ps_websocket_client.receive_message()
        if msg.startswith(">battle") and battle.battle_tag not in msg:
            continue
        msg_split = msg.split("|")
        if len(msg_split) < 3:
            continue
        if msg_split[1].strip() == "request" and msg_split[2].strip():
            user_json = json.loads(msg_split[2].strip("'"))
            battle.request_json = user_json
            battle.user.initialize_first_turn_user_from_json(user_json)
            battle.rqid = user_json[constants.RQID]
            return


async def start_random_battle(
    ps_websocket_client: PSWebsocketClient,
    pokemon_battle_type,
    battle_tag,
    opponent_name,
    first_msg,
):
    battle, msg = await start_battle_common(
        ps_websocket_client, pokemon_battle_type, battle_tag, opponent_name, first_msg
    )
    battle.battle_type = BattleType.RANDOM_BATTLE
    RandomBattleTeamDatasets.initialize(pokemon_battle_type)

    while True:
        if msg.startswith(">battle") and battle.battle_tag not in msg:
            msg = await ps_websocket_client.receive_message()
            continue
        if constants.START_STRING in msg:
            battle.started = True

            # hold onto some messages to apply after we get the request JSON
            # omit the bot's switch-in message because we won't need that
            # parsing the request JSON will set the bot's active pkmn
            battle.msg_list = [
                m
                for m in msg.split(constants.START_STRING)[1].strip().split("\n")
                if not (m.startswith("|switch|{}".format(battle.user.name)))
            ]
            break
        msg = await ps_websocket_client.receive_message()

    await get_first_request_json(ps_websocket_client, battle)

    if FoulPlayConfig.dump_team_dir is not None:
        try:
            dump_team_from_request_json(
                battle.request_json, pokemon_battle_type, battle.battle_tag
            )
        except Exception as e:
            logger.error("Could not dump team: {}".format(e))

    # apply the messages that were held onto
    process_battle_updates(battle)

    # timer BEFORE the first search: the ~4s of search puts a natural gap
    # between this send and the choice, so neither can be dropped by the
    # server's chat throttle (start_battle's later /timer on is a no-op)
    if not FoulPlayConfig.never_start_timer:
        await ps_websocket_client.send_message(battle.battle_tag, ["/timer on"])

    best_move = await async_pick_move(battle)
    # arm the resend safety net for turn 1 too — the 2662166594 loss was a
    # dropped FIRST choice, and this path used to leave last_choice_sent
    # unset, so all eight timer warnings failed the resend guard
    battle.last_choice_sent = best_move
    await ps_websocket_client.send_message(battle.battle_tag, best_move)

    return battle


async def start_standard_battle(
    ps_websocket_client: PSWebsocketClient,
    pokemon_battle_type,
    team_dict,
    battle_tag,
    opponent_name,
    first_msg,
):
    battle, msg = await start_battle_common(
        ps_websocket_client, pokemon_battle_type, battle_tag, opponent_name, first_msg
    )
    battle.user.team_dict = team_dict
    if "battlefactory" in pokemon_battle_type:
        battle.battle_type = BattleType.BATTLE_FACTORY
    else:
        battle.battle_type = BattleType.STANDARD_BATTLE

    if battle.generation in constants.NO_TEAM_PREVIEW_GENS:
        while True:
            if constants.START_STRING in msg:
                battle.started = True

                # hold onto some messages to apply after we get the request JSON
                # omit the bot's switch-in message because we won't need that
                # parsing the request JSON will set the bot's active pkmn
                battle.msg_list = [
                    m
                    for m in msg.split(constants.START_STRING)[1].strip().split("\n")
                    if not (m.startswith("|switch|{}".format(battle.user.name)))
                ]
                break
            msg = await ps_websocket_client.receive_message()

        await get_first_request_json(ps_websocket_client, battle)

        unique_pkmn_names = set(
            [p.name for p in battle.user.reserve] + [battle.user.active.name]
        )
        SmogonSets.initialize(
            FoulPlayConfig.smogon_stats or pokemon_battle_type, unique_pkmn_names
        )
        TeamDatasets.initialize(pokemon_battle_type, unique_pkmn_names)

        # apply the messages that were held onto
        process_battle_updates(battle)

        best_move = await async_pick_move(battle)
        await ps_websocket_client.send_message(battle.battle_tag, best_move)

    else:
        while constants.START_TEAM_PREVIEW not in msg:
            msg = await ps_websocket_client.receive_message()

        preview_string_lines = msg.split(constants.START_TEAM_PREVIEW)[-1].split("\n")

        opponent_pokemon = []
        for line in preview_string_lines:
            if not line:
                continue

            split_line = line.split("|")
            if (
                split_line[1] == constants.TEAM_PREVIEW_POKE
                and split_line[2].strip() == battle.opponent.name
            ):
                opponent_pokemon.append(split_line[3])

        await get_first_request_json(ps_websocket_client, battle)
        battle.initialize_team_preview(opponent_pokemon, pokemon_battle_type)
        battle.during_team_preview()

        unique_pkmn_names = set(
            p.name for p in battle.opponent.reserve + battle.user.reserve
        )

        if battle.battle_type == BattleType.BATTLE_FACTORY:
            battle.battle_type = BattleType.BATTLE_FACTORY
            tier_name = extract_battle_factory_tier_from_msg(msg)
            logger.info("Battle Factory Tier: {}".format(tier_name))
            TeamDatasets.initialize(
                pokemon_battle_type,
                unique_pkmn_names,
                battle_factory_tier_name=tier_name,
            )
        else:
            battle.battle_type = BattleType.STANDARD_BATTLE
            SmogonSets.initialize(
                FoulPlayConfig.smogon_stats or pokemon_battle_type, unique_pkmn_names
            )
            TeamDatasets.initialize(pokemon_battle_type, unique_pkmn_names)

        await handle_team_preview(battle, ps_websocket_client)

    return battle


async def start_battle(ps_websocket_client, pokemon_battle_type, team_dict):
    battle_tag, opponent_name, first_msg = await get_battle_tag_and_opponent(
        ps_websocket_client
    )
    if FoulPlayConfig.log_to_file:
        FoulPlayConfig.file_log_handler.do_rollover(
            "{}_{}.log".format(battle_tag, opponent_name)
        )

    # Everything between claiming the battle and sending the first choice is
    # message-driven with no other watchdog: a broken assumption here used to
    # wedge the process SILENTLY until the timer bled the game out and the
    # zombie had to be killed by hand (2664176268). Bound it: on expiry,
    # forfeit — a fast visible loss that frees the account instead of a slow
    # one that poisons the next session — and crash out loudly so the
    # launcher starts clean.
    try:
        async with asyncio.timeout(BOOTSTRAP_TIMEOUT_S):
            if "random" in pokemon_battle_type:
                battle = await start_random_battle(
                    ps_websocket_client,
                    pokemon_battle_type,
                    battle_tag,
                    opponent_name,
                    first_msg,
                )
            else:
                battle = await start_standard_battle(
                    ps_websocket_client,
                    pokemon_battle_type,
                    team_dict,
                    battle_tag,
                    opponent_name,
                    first_msg,
                )
    except (asyncio.TimeoutError, TimeoutError):
        logger.critical(
            "Battle bootstrap wedged for >{}s in {} — forfeiting".format(
                BOOTSTRAP_TIMEOUT_S, battle_tag
            )
        )
        await ps_websocket_client.send_message(battle_tag, ["/forfeit"])
        await ps_websocket_client.leave_room_now(battle_tag)
        raise

    # random battles already sent /timer on before their first search; don't
    # spend a second message on the account's shared throttle budget
    if not FoulPlayConfig.never_start_timer and "random" not in pokemon_battle_type:
        await ps_websocket_client.send_message(battle.battle_tag, ["/timer on"])

    return battle


async def pokemon_battle(ps_websocket_client, pokemon_battle_type, team_dict):
    battle = await start_battle(ps_websocket_client, pokemon_battle_type, team_dict)
    while True:
        msg = await ps_websocket_client.receive_message()
        # Multi-battle containment: never feed another battle's protocol into
        # this battle's state. If a NEW battle starts while we are mid-game
        # (the server joins every connection to it), detach — an idle process
        # claims and plays it. If NOBODY claims it within 3s (a stray search
        # matched while every slot was busy), forfeit it immediately instead
        # of letting the timer bleed it out; the claim file is the mutex so
        # exactly one process forfeits.
        if msg.startswith(">battle") and battle.battle_tag not in msg:
            if "|init|" in msg:
                foreign_tag = msg.split("|")[0].replace(">", "").strip()

                async def _sweep_orphan(tag):
                    await asyncio.sleep(3)
                    if os.environ.get("FP_CLAIM_DIR") and claim_battle(tag):
                        logger.warning("Orphaned battle {} — forfeiting".format(tag))
                        await ps_websocket_client.send_message(tag, ["/forfeit"])
                    await ps_websocket_client.leave_room_now(tag)

                asyncio.ensure_future(_sweep_orphan(foreign_tag))
            continue
        # A timer warning naming US after we already answered the current
        # request is the server saying it never received the choice (observed
        # 2026-08-09: two fleet games sent correct-rqid choices that were
        # silently blackholed in transit — no error, no action, timer death,
        # inbound still flowing). The warning doubles as a NAK: resend the
        # last choice. Duplicate delivery is harmless (PS rejects an
        # already-answered rqid with a no-op error line).
        # The server tells us outright when it dropped our message for chat
        # throttling ("typing too quickly") — treat it as a NAK and resend the
        # last choice (paced by send_message, so the resend can't be dropped
        # the same way; a stale rqid resend is a harmless no-op error).
        if "message-throttle-notice" in msg and getattr(
            battle, "last_choice_sent", None
        ):
            logger.warning(
                "Server throttle dropped a message — resending: {}".format(
                    battle.last_choice_sent
                )
            )
            await ps_websocket_client.send_message(
                battle.battle_tag, battle.last_choice_sent
            )
        if (
            "|inactive|" in msg
            and "seconds left" in msg
            and FoulPlayConfig.username in msg
            and getattr(battle, "last_choice_sent", None)
        ):
            logger.warning(
                "Timer warning after choice was sent — resending: {}".format(
                    battle.last_choice_sent
                )
            )
            await ps_websocket_client.send_message(
                battle.battle_tag, battle.last_choice_sent
            )
        if battle_is_finished(battle.battle_tag, msg):
            winner = (
                msg.split(constants.WIN_STRING)[-1].split("\n")[0].strip()
                if constants.WIN_STRING in msg
                else None
            )
            logger.info("Winner: {}".format(winner))
            try:
                from fp.battle_modifier import record_opponent_action

                record_opponent_action(
                    battle,
                    "game_end",
                    "win" if winner == FoulPlayConfig.username else "loss",
                )
            except Exception:
                logger.debug("game_end ledger row failed", exc_info=True)
            if (
                FoulPlayConfig.save_replay == SaveReplay.always
                or (
                    FoulPlayConfig.save_replay == SaveReplay.on_loss
                    and winner != FoulPlayConfig.username
                )
                or (
                    FoulPlayConfig.save_replay == SaveReplay.on_win
                    and winner == FoulPlayConfig.username
                )
            ):
                await ps_websocket_client.save_replay(battle.battle_tag)
            await ps_websocket_client.leave_battle(battle.battle_tag)
            return winner
        else:
            try:
                action_required = await async_update_battle(battle, msg)
            except asyncio.CancelledError:
                raise
            except BaseException as e:
                # a parse failure must not forfeit the game: if the message that
                # blew up contained a request, still answer it with a legal choice
                logger.error(
                    "async_update_battle failed ({!r}) on msg: {}".format(e, msg),
                    exc_info=True,
                )
                action_required = "|request|" in msg

            if action_required and not battle.wait:
                try:
                    best_move = await async_pick_move(battle)
                except asyncio.CancelledError:
                    raise
                except BaseException as e:
                    logger.error(
                        "async_pick_move failed ({!r}); sending default choice "
                        "(turn={} rqid={})".format(e, battle.turn, battle.rqid),
                        exc_info=True,
                    )
                    best_move = ["/choose default", str(battle.rqid)]

                # deliberately outside the handlers above: a dead socket should
                # still end the battle
                battle.last_choice_sent = best_move
                await ps_websocket_client.send_message(battle.battle_tag, best_move)
