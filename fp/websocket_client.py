import asyncio
import fcntl
import os
import websockets
import requests
import json
import time

import logging

logger = logging.getLogger(__name__)


def _pace_across_processes(path, min_gap):
    # The server's chat-throttle queue is per-USER (users.ts chatQueue), so
    # per-connection pacing cannot prevent drops when sibling slot processes
    # of the same account send simultaneously. Serialize sends account-wide
    # through a box-local lock file: take the lock, wait out the gap since the
    # last send by ANY process, stamp, release.
    with open(path, "a+") as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        try:
            last = float(f.read().strip() or "0")
        except ValueError:
            last = 0.0
        wait = last + min_gap - time.time()
        if wait > 0:
            time.sleep(wait)
        f.seek(0)
        f.truncate()
        f.write(repr(time.time()))
        f.flush()


def _is_local_server(address: str) -> bool:
    """True only for a websocket on this machine.

    The send pacing and the post-login settle below exist because Showdown
    THROTTLES and a dropped /choose is an inactivity loss (battle 2662166594,
    2026-08-09). They are pure cost against a local --no-security server, which
    has no throttle at all -- and they dominate a short validation game: 0.62s
    per outbound message plus a 3s settle turned a 2.5s-of-search game into 18s.
    Speeding that up must never be able to touch a real ladder connection, so
    the opt-out is gated on the ADDRESS, not just on an env var.
    """
    a = (address or "").lower()
    host = a.split("://", 1)[-1].split("/", 1)[0].split(":", 1)[0]
    return host in ("localhost", "127.0.0.1", "::1", "[::1]")


def _fast_local(address: str) -> bool:
    return (
        os.environ.get("FP_LOCAL_NO_PACE") == "1" and _is_local_server(address)
    )


class LoginError(Exception):
    pass


class SaveReplayError(Exception):
    pass


class PSWebsocketClient:
    websocket = None
    address = None
    login_uri = None
    local_login = False
    username = None
    password = None
    last_message = None
    last_challenge_time = 0

    @classmethod
    async def create(cls, username, password, address):
        self = PSWebsocketClient()
        self.username = username
        self.password = password
        self.address = address
        self.websocket = await websockets.connect(self.address)
        # FP_LOCAL_LOGIN=1 skips the public login service entirely. A server
        # started with --no-security sets noguestsecurity, so `/trn name,0,`
        # with an EMPTY assertion is accepted -- no HTTP round-trip at all.
        #
        # This matters at fleet scale, not on one box. The farm's games run
        # wholly on local servers, but every bot process still authenticated
        # against play.pokemonshowdown.com: 65 boxes x 96 lanes made ~360
        # req/s (624k POSTs in 2.3h), the endpoint began returning Cloudflare
        # 520/522s, and 19.9% of 312,126 login attempts failed. Because a
        # challenger that dies at login leaves its acceptor waiting out the
        # full 900 s timeout, that single dependency burned 62% of all fleet
        # lane-time and produced the end-of-run collapse (see
        # truestate/farm/POSTMORTEM_RUN1.md).
        self.local_login = os.environ.get("FP_LOCAL_LOGIN") == "1"
        self.login_uri = os.environ.get("FP_LOGIN_URI") or (
            "https://play.pokemonshowdown.com/api/login"
            if password
            else "https://play.pokemonshowdown.com/action.php?"
        )
        return self

    async def join_room(self, room_name):
        message = "/join {}".format(room_name)
        await self.send_message("", [message])
        logger.debug("Joined room '{}'".format(room_name))

    async def receive_message(self):
        message = await self.websocket.recv()
        logger.debug("Received message from websocket: {}".format(message))
        return message

    async def send_message(self, room, message_list):
        # The server enforces a per-connection chat throttle (600ms between
        # messages, ~6-message buffer) and SILENTLY DROPS the overflow with
        # only an in-room throttle notice. A dropped /choose is an inactivity
        # loss (battle 2662166594, 2026-08-09: the turn-1 choice was dropped
        # during the boot burst and the game timed out). Pace sends just above
        # the server's throttle; a lone in-battle choice is never delayed
        # because the preceding search dwarfs the gap.
        if not hasattr(self, "_send_lock"):
            self._send_lock = asyncio.Lock()
            self._last_send_mono = 0.0
            self._min_send_gap = 0.0 if _fast_local(self.address) else 0.62
            pace_dir = os.environ.get("FP_CLAIM_DIR")
            self._pace_file = (
                os.path.join(
                    pace_dir,
                    ".send_pace_"
                    + "".join(c if c.isalnum() else "_" for c in self.username or ""),
                )
                if pace_dir
                else None
            )
        async with self._send_lock:
            if self._pace_file:
                # account-wide pacing across sibling slot processes
                await asyncio.to_thread(
                    _pace_across_processes, self._pace_file, self._min_send_gap
                )
            else:
                gap = self._last_send_mono + self._min_send_gap - time.monotonic()
                if gap > 0:
                    await asyncio.sleep(gap)
            message = room + "|" + "|".join(message_list)
            logger.debug("Sending message to websocket: {}".format(message))
            await self.websocket.send(message)
            self._last_send_mono = time.monotonic()
            self.last_message = message

    async def avatar(self, avatar):
        await self.send_message("", ["/avatar {}".format(avatar)])
        await self.send_message("", ["/cmd userdetails {}".format(self.username)])
        while True:
            # Wait for the query response and check the avatar
            # |queryresponse|QUERYTYPE|JSON
            msg = await self.receive_message()
            msg_split = msg.split("|")
            if msg_split[1] == "queryresponse":
                user_details = json.loads(msg_split[3])
                if user_details["avatar"] == avatar:
                    logger.info("Avatar set to {}".format(avatar))
                else:
                    logger.warning(
                        "Could not set avatar to {}, avatar is {}".format(
                            avatar, user_details["avatar"]
                        )
                    )
                break

    async def close(self):
        await self.websocket.close()

    async def get_id_and_challstr(self):
        while True:
            message = await self.receive_message()
            split_message = message.split("|")
            if split_message[1] == "challstr":
                return split_message[2], split_message[3]

    def _get_assertion(self, client_id, challstr):
        # empty string and None both mean "no password" — create() already
        # picks the guest login URI for both; keep this check consistent
        guest_login = not self.password

        if self.local_login:
            # --no-security server: empty assertion, no HTTP call. Returning
            # early here is what makes the farm independent of any external
            # service; see the note in create().
            return "", self.username

        if guest_login:
            response = requests.post(
                self.login_uri,
                data={
                    "act": "getassertion",
                    "userid": self.username,
                    "challstr": "|".join([client_id, challstr]),
                },
            )
        else:
            response = requests.post(
                self.login_uri,
                data={
                    "name": self.username,
                    "pass": self.password,
                    "challstr": "|".join([client_id, challstr]),
                },
            )

        if response.status_code != 200:
            logger.error(
                "Could not get assertion\nDetails:\n{}".format(response.content)
            )
            raise LoginError("Could not get assertion")

        if guest_login:
            return response.text, self.username
        response_json = json.loads(response.text[1:])
        if "actionsuccess" not in response_json:
            logger.error("Login Unsuccessful: {}".format(response_json))
            raise LoginError("Could not log-in: {}".format(response_json))
        return response_json.get("assertion"), response_json["curuser"]["userid"]

    async def login(self):
        logger.info("Logging in...")
        client_id, challstr = await self.get_id_and_challstr()
        # cached for relogin(): the challstr stays valid for this connection's
        # lifetime, and reading a fresh one mid-wait would eat the stream
        self.client_id, self.challstr = client_id, challstr

        assertion, userid = self._get_assertion(client_id, challstr)
        message = ["/trn " + self.username + ",0," + assertion]
        logger.info("Successfully logged in")
        await self.send_message("", message)
        # NOT zero even locally: /trn is processed asynchronously, and
        # challenging before the rename lands makes the server drop the
        # challenge with "cancelled because they changed their username".
        # 0.5s is empirically enough on loopback; 3s is the remote setting.
        await asyncio.sleep(0.5 if _fast_local(self.address) else 3)
        return userid

    async def relogin(self):
        """Re-authenticate THIS connection using the cached challstr.

        A sibling process logging into the same account registers as a name
        change: Showdown cancels the account's pending searches and can demote
        this connection back to guest. Fresh assertion + /trn restores it
        without touching the message stream."""
        logger.info("Re-authenticating after guest demotion...")
        assertion, _ = self._get_assertion(self.client_id, self.challstr)
        await self.send_message("", ["/trn " + self.username + ",0," + assertion])
        await asyncio.sleep(1)

    async def update_team(self, team):
        await self.send_message("", ["/utm {}".format(team)])

    async def challenge_user(self, user_to_challenge, battle_format):
        logger.info("Challenging {}...".format(user_to_challenge))
        message = ["/challenge {},{}".format(user_to_challenge, battle_format)]
        await self.send_message("", message)
        self.last_challenge_time = time.time()

    async def accept_challenge(self, battle_format, room_name):
        if room_name is not None:
            await self.join_room(room_name)

        logger.info("Waiting for a {} challenge".format(battle_format))
        username = None
        while username is None:
            msg = await self.receive_message()
            split_msg = msg.split("|")
            if (
                len(split_msg) == 9
                and split_msg[1] == "pm"
                and split_msg[3].strip().replace("!", "").replace("‽", "")
                == self.username
                and split_msg[4].startswith("/challenge")
                and split_msg[5] == battle_format
            ):
                username = split_msg[2].strip()

        message = ["/accept " + username]
        await self.send_message("", message)

    async def search_for_match(self, battle_format):
        logger.info("Searching for ranked {} match".format(battle_format))
        message = ["/search {}".format(battle_format)]
        await self.send_message("", message)

    async def leave_room_now(self, battle_tag):
        """Fire-and-forget /leave: detaches THIS connection from the room
        without consuming the message stream waiting for |deinit|. Used by the
        multi-battle claim layer to drop battles another process owns."""
        await self.send_message("", ["/leave {}".format(battle_tag)])

    async def leave_battle(self, battle_tag):
        message = ["/leave {}".format(battle_tag)]
        await self.send_message("", message)

        # Bounded: the /leave or its |deinit| can be lost in transit (same
        # send-blackhole class as the inactivity losses), and this loop used
        # to wait forever — finished processes hung and a fleet box never
        # completed draining (bobfamilyrules, 2026-08-09). The game is over;
        # 10s of best-effort confirmation is plenty.
        try:
            async with asyncio.timeout(10):
                while True:
                    msg = await self.receive_message()
                    if battle_tag in msg and "deinit" in msg:
                        return
        except (asyncio.TimeoutError, TimeoutError):
            logger.warning(
                "No deinit for {} within 10s — leaving anyway".format(battle_tag)
            )

    async def save_replay(self, battle_tag):
        message = ["/savereplay"]
        await self.send_message(battle_tag, message)
