"""Regression tests for the 2026-08-13 battle-start bug family.

That night's post-mortem (battles 2664176268/2664176854 and ghosts
2664147697/2664150396/2664156364):
  1. the search watchdog re-armed /search on the match-found transient
     (searching:[], games:null), double-queuing the account into a "ghost"
     battle nobody played (timer loss);
  2. run.py's blind startup /search raced the login-time games push while a
     ghost was still live, matching a second ghost;
  3. a process that logged in while the account had a live game received the
     battle as a full room-history dump inside |init| — the bootstrap only
     looked at FUTURE messages for |player|/|start, so it wedged forever
     (frozen at turn 1 for 24+ min, then killed by hand), with the substring
     player-match latching onto a timer notice and picking the wrong side.
"""

import asyncio
import json
import time

import pytest

import constants
from config import FoulPlayConfig, BotModes
import fp.run_battle as rb


TAG = "battle-gen9randombattle-2664176268"
USERNAME = "fable foul play"
OPPONENT = "wang_zj"

# The exact rejoin dump the frozen process received on login (init.log,
# 2026-08-13 03:19:56): one |init| message carrying the full room history.
REJOIN_DUMP = (
    ">battle-gen9randombattle-2664176268\n"
    "|init|battle\n"
    "|title|wang_zj vs. fable foul play\n"
    "|j|☆wang_zj\n"
    "|j|☆fable foul play\n"
    "|t:|1786612670\n"
    "|gametype|singles\n"
    "|player|p1|wang_zj|170|2117\n"
    "|player|p2|fable foul play|170|2149\n"
    "|gen|9\n"
    "|tier|[Gen 9] Random Battle\n"
    "|rated|\n"
    "|rule|Sleep Clause Mod: Limit one foe put to sleep\n"
    "|\n"
    "|t:|1786612670\n"
    "|teamsize|p1|6\n"
    "|teamsize|p2|6\n"
    "|start\n"
    "|switch|p1a: Magcargo|Magcargo, L95, M|100/100\n"
    "|switch|p2a: Victreebel|Victreebel, L90, M|290/290\n"
    "|turn|1\n"
    "|l|☆fable foul play\n"
    "|player|p2|\n"
    "|j|☆fable foul play\n"
    "|player|p2|fable foul play|170|\n"
)

# an ordinary fresh-battle |init| (no player lines yet)
PLAIN_INIT = (
    ">battle-gen9randombattle-2664176268\n"
    "|init|battle\n"
    "|title|wang_zj vs. fable foul play\n"
    "|j|☆wang_zj\n"
    "|j|☆fable foul play\n"
)

# the message that fooled the old substring player-match (frozen log line 47):
# contains '|player|' AND the opponent's name, but the player line is OURS
TIMER_NOTICE = (
    ">battle-gen9randombattle-2664176268\n"
    "|player|p2|fable foul play|170|\n"
    "|inactive|Battle timer is ON: inactive players will automatically lose "
    "when time's up. (requested by wang_zj)"
)

REAL_PLAYER_MSG = ">battle-gen9randombattle-2664176268\n|player|p1|wang_zj|170|2117"


def upd(searching, games):
    # PS sends compact JSON: {"searching":[],"games":null}
    return "|updatesearch|" + json.dumps(
        {"searching": searching, "games": games}, separators=(",", ":")
    )


class ScriptedClient:
    """Plays back a scripted websocket. Numeric entries are seconds of
    socket silence (a cancelled wait keeps the unelapsed remainder)."""

    def __init__(self, script=()):
        self.script = list(script)
        self.sent = []

    async def receive_message(self):
        while True:
            if not self.script:
                await asyncio.sleep(3600)  # starve: nothing more scripted
                continue
            item = self.script[0]
            if isinstance(item, (int, float)):
                started = time.monotonic()
                try:
                    await asyncio.sleep(item)
                finally:
                    remaining = item - (time.monotonic() - started)
                    if remaining > 0.005:
                        self.script[0] = remaining
                    else:
                        self.script.pop(0)
                continue
            return self.script.pop(0)

    async def send_message(self, room, message_list):
        self.sent.append((room, list(message_list)))

    async def search_for_match(self, battle_format):
        self.sent.append(("", ["/search {}".format(battle_format)]))

    async def join_room(self, room_name):
        self.sent.append(("", ["/join {}".format(room_name)]))
        # joining an ongoing battle makes the server send the room dump
        self.script.append(REJOIN_DUMP)

    async def leave_room_now(self, battle_tag):
        self.sent.append(("", ["/leave {}".format(battle_tag)]))

    async def relogin(self):
        self.sent.append(("", ["/trn"]))

    def searches(self):
        return [
            m for _, msgs in self.sent for m in msgs if m.startswith("/search")
        ]


@pytest.fixture(autouse=True)
def _config(monkeypatch):
    monkeypatch.setattr(FoulPlayConfig, "username", USERNAME, raising=False)
    monkeypatch.setattr(
        FoulPlayConfig, "bot_mode", BotModes.search_ladder, raising=False
    )
    monkeypatch.setattr(
        FoulPlayConfig, "pokemon_format", "gen9randombattle", raising=False
    )
    monkeypatch.setattr(FoulPlayConfig, "log_to_file", False, raising=False)
    monkeypatch.delenv("FP_CLAIM_DIR", raising=False)
    # compress watchdog time so the tests run in milliseconds
    monkeypatch.setattr(rb, "SEARCH_RECV_TIMEOUT_S", 0.1)
    monkeypatch.setattr(rb, "SEARCH_STALE_S", 0.25)
    monkeypatch.setattr(rb, "SEARCH_STALE_JITTER_S", 0)
    monkeypatch.setattr(rb, "RESCUE_JOIN_S", 0.25)
    monkeypatch.setattr(rb, "BOOTSTRAP_TIMEOUT_S", 0.5)


def run(coro, timeout=5):
    return asyncio.run(asyncio.wait_for(coro, timeout))


# ---------- bootstrap: the 2664176268 freeze ----------


def test_rejoin_dump_initializes_both_sides_with_no_further_messages():
    # the dump already carries the |player| lines: the bootstrap must use
    # them instead of starving on future messages (the 24-minute freeze)
    client = ScriptedClient([])
    battle, msg = run(
        rb.start_battle_common(
            client, "gen9randombattle", TAG, OPPONENT, REJOIN_DUMP
        )
    )
    assert battle.opponent.name == "p1"
    assert battle.user.name == "p2"
    assert battle.opponent.account_rating == "2117"
    # the dump is passed onward so the |start-block scan finds it too
    assert constants.START_STRING in msg


def test_timer_notice_is_not_mistaken_for_the_opponents_player_line():
    # old substring match set opponent.name to 'p2' (our own side) here
    client = ScriptedClient([TIMER_NOTICE, REAL_PLAYER_MSG])
    battle, _ = run(
        rb.start_battle_common(client, "gen9randombattle", TAG, OPPONENT, PLAIN_INIT)
    )
    assert battle.opponent.name == "p1"
    assert battle.opponent.account_rating == "2117"


def test_normal_flow_unchanged_and_foreign_battles_filtered():
    foreign = ">battle-gen9randombattle-9999999\n|player|p1|wang_zj|170|1500"
    interim = ">battle-gen9randombattle-2664176268\n|t:|1786612670\n|gametype|singles"
    client = ScriptedClient([foreign, interim, REAL_PLAYER_MSG])
    battle, _ = run(
        rb.start_battle_common(client, "gen9randombattle", TAG, OPPONENT, PLAIN_INIT)
    )
    assert battle.opponent.name == "p1"
    # 1500 would mean the foreign battle's player line leaked through
    assert battle.opponent.account_rating == "2117"


def test_bootstrap_timeout_forfeits_and_raises():
    # player line never arrives: instead of a silent zombie, the bootstrap
    # must forfeit (freeing the account) and crash out loudly
    client = ScriptedClient([upd([], None), 0.02, PLAIN_INIT])
    with pytest.raises((asyncio.TimeoutError, TimeoutError)):
        run(rb.start_battle(client, "gen9randombattle", None))
    assert (TAG, ["/forfeit"]) in client.sent
    assert ("", ["/leave {}".format(TAG)]) in client.sent


# ---------- watchdog: the double-search ghost family ----------


def test_no_research_on_match_found_transient():
    # THE ghost-maker: searching:[] + games:null is pushed the instant a
    # match forms; re-searching there double-queues the account
    client = ScriptedClient(
        [
            upd(["gen9randombattle"], None),  # our search is alive
            upd([], None),  # match found, room not created yet
            0.05,
            PLAIN_INIT,  # the real battle arrives
        ]
    )
    battle_tag, opponent, msg = run(rb.get_battle_tag_and_opponent(client))
    assert battle_tag == TAG
    assert opponent == OPPONENT
    assert client.searches() == []


def test_idle_account_searches_once_then_claims():
    client = ScriptedClient([upd([], None), 0.05, PLAIN_INIT])
    battle_tag, opponent, msg = run(rb.get_battle_tag_and_opponent(client))
    assert client.searches() == ["/search gen9randombattle"]
    assert battle_tag == TAG
    assert msg == PLAIN_INIT  # the init msg itself is handed to the bootstrap


def test_login_with_live_game_never_searches():
    # the 2664176854 ghost: a startup search while the account already had a
    # live battle queued a second, never-played game
    client = ScriptedClient(
        [upd([], {TAG: "[Gen 9] Random Battle*"}), REJOIN_DUMP]
    )
    battle_tag, opponent, msg = run(rb.get_battle_tag_and_opponent(client))
    assert client.searches() == []
    assert battle_tag == TAG
    assert opponent == OPPONENT
    assert constants.START_STRING in msg


def test_rotting_game_is_joined_not_searched_over():
    # live game, but its |init| never arrives (dead prior session): the
    # watchdog must /join it to force the dump rather than search past it
    client = ScriptedClient([upd([], {TAG: "[Gen 9] Random Battle*"})])
    battle_tag, opponent, _ = run(rb.get_battle_tag_and_opponent(client))
    assert ("", ["/join {}".format(TAG)]) in client.sent
    assert client.searches() == []
    assert battle_tag == TAG
    assert opponent == OPPONENT


def test_truly_dead_search_is_rearmed():
    # a search that stays unconfirmed past the stale window is re-armed
    # (cancelled server-side / push lost — the 2026-08-08 deadlock class)
    client = ScriptedClient(
        [upd(["gen9randombattle"], None), 0.4, upd([], None), 0.05, PLAIN_INIT]
    )
    battle_tag, _, _ = run(rb.get_battle_tag_and_opponent(client))
    assert client.searches() == ["/search gen9randombattle"]
    assert battle_tag == TAG
