"""Turn a saved foul-play battle log into (a) ordered protocol chunks that can be
replayed through `fp.battle_modifier.update_battle`, and (b) per-turn OBSERVED
events extracted from the resolution block, for the fidelity comparator.

Log format (Python logging, "%(levelname)-8s %(message)s"): the FIRST physical
line of every websocket chunk is written as
`DEBUG    Received message from websocket: <first line>` (usually `>battle-xxx`),
and the remaining protocol lines of that chunk follow raw, each starting with
`|`.  The bot's own INFO/DEBUG logging and `Sending message to websocket:` lines
are dropped.
"""

import re

from fp.replay.comparator import ObservedEvent, _SECONDARY_CAUSE_TAGS

_RECV = re.compile(
    r"^(?:DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+Received message from websocket: (.*)$"
)


def iter_chunks(log_path: str):
    """Yield protocol chunks in order, each a newline-joined string suitable for
    `update_battle(battle, chunk)`.  A chunk starts at a `Received message`
    line and runs until the next one."""
    chunk: list[str] = []
    with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip("\n")
            m = _RECV.match(line)
            if m is not None:
                if chunk:
                    yield "\n".join(chunk)
                chunk = [m.group(1)]
            elif line.startswith("|"):
                chunk.append(line)
            # else: bot's own logging -> drop
    if chunk:
        yield "\n".join(chunk)


# ---------------------------------------------------------------------------
# Observed-event extraction from a resolution block
# ---------------------------------------------------------------------------

# Protocol player-tag -> which player id owns it (e.g. "p2a: Illumise" -> "p2").
_PLAYER_TAG = re.compile(r"^(p[12])[ab]?:")


def _tag_player(token: str) -> str | None:
    m = _PLAYER_TAG.match(token.strip())
    return m.group(1) if m else None


def _side_of(token: str, user_player_id: str) -> str | None:
    pid = _tag_player(token)
    if pid is None:
        return None
    return "user" if pid == user_player_id else "opp"


# Weather protocol strings -> engine weather tokens (substring-matched).
_WEATHER = {
    "RainDance": "RAIN",
    "Rain": "RAIN",
    "SunnyDay": "SUN",
    "Sun": "SUN",
    "Sandstorm": "SAND",
    "Snow": "SNOW",
    "Hail": "HAIL",
    "none": "NONE",
}

_TERRAIN = {
    "Electric Terrain": "ELECTRICTERRAIN",
    "Grassy Terrain": "GRASSYTERRAIN",
    "Psychic Terrain": "PSYCHICTERRAIN",
    "Misty Terrain": "MISTYTERRAIN",
}

# -sidestart move label -> engine side-condition token (as it stringifies).
_SIDE_CONDITION = {
    "Stealth Rock": "stealthrock",
    "move: Stealth Rock": "stealthrock",
    "Spikes": "spikes",
    "move: Spikes": "spikes",
    "Toxic Spikes": "toxicspikes",
    "move: Toxic Spikes": "toxicspikes",
    "Sticky Web": "stickyweb",
    "move: Sticky Web": "stickyweb",
    "Reflect": "reflect",
    "move: Reflect": "reflect",
    "Light Screen": "lightscreen",
    "move: Light Screen": "lightscreen",
    "Aurora Veil": "auroraveil",
    "move: Aurora Veil": "auroraveil",
    "Tailwind": "tailwind",
    "move: Tailwind": "tailwind",
}


def _clean(label: str) -> str:
    # strip a leading "move: " / "ability: " / "item: " qualifier
    for pre in ("move: ", "ability: ", "item: "):
        if label.startswith(pre):
            return label[len(pre) :]
    return label


# "[of] p2a:" source tag on -weather/-fieldstart lines (ability-set field effects).
_OF_SLOT = re.compile(r"\[of\] (p[12][ab]?):")

# Lines that end the same-instant window a berry status-cure must fall inside.
_PHASE_ACTIONS = frozenset(
    ("move", "switch", "drag", "replace", "faint", "cant", "upkeep", "turn")
)


def _berry_cure(block_lines: list[str], start: int, slot: str, status_id: str) -> str:
    """If the -status at block_lines[start] is eaten away the same instant
    (-enditem <berry> [eat] then -curestatus for the same slot+status before any
    other action resolves), return the normalized berry name, else ""."""
    berry = ""
    for line in block_lines[start + 1 :]:
        parts = line.split("|")
        if len(parts) < 2:
            continue
        action = parts[1]
        if action in _PHASE_ACTIONS:
            return ""
        tgt = parts[2].split(":")[0].strip() if len(parts) >= 3 else ""
        if tgt != slot:
            continue
        if action == "-enditem" and len(parts) >= 4 and "[eat]" in line:
            berry = re.sub(r"[^a-z0-9]", "", parts[3].strip().lower())
        elif (
            action == "-curestatus"
            and len(parts) >= 4
            and parts[3].strip() == status_id
        ):
            return berry
    return ""


def _ability_cure(block_lines: list[str], start: int, slot: str, status_id: str) -> str:
    """If the -status at block_lines[start] is cured the SAME INSTANT by the
    target's own ability (`-activate <slot>|ability: X` then `-curestatus
    <slot>|<status>` before any other action resolves), return the normalized
    ability name, else "".

    PS models the status-immunity abilities as apply-then-cure: `setStatus`
    succeeds and the ability's `onUpdate` removes it immediately (Insomnia
    data/abilities.ts, Water Veil, Limber, Immunity, Vital Spirit, Magma
    Armor...), which the protocol prints as three lines in one instant.
    poke-engine models the identical net effect as a straight IMMUNITY -- the
    status is never applied, so no ChangeStatus instruction exists on any branch
    -- and the resulting state is the same.  Asserting the transient status is
    therefore a false positive (synth13836 T7: Spore into Insomnia; synth47976
    T13: Will-O-Wisp into Water Veil)."""
    ability = ""
    for line in block_lines[start + 1 :]:
        parts = line.split("|")
        if len(parts) < 2:
            continue
        action = parts[1]
        if action in _PHASE_ACTIONS:
            return ""
        tgt = parts[2].split(":")[0].strip() if len(parts) >= 3 else ""
        if tgt != slot:
            continue
        if (
            action == "-activate"
            and len(parts) >= 4
            and parts[3].strip().startswith("ability: ")
        ):
            ability = re.sub(
                r"[^a-z0-9]", "", parts[3].strip()[len("ability: ") :].lower()
            )
        elif (
            action == "-curestatus"
            and len(parts) >= 4
            and parts[3].strip() == status_id
        ):
            return ability
    return ""


def _is_cudchew_reeat(block_lines: list[str], idx: int, slot: str) -> bool:
    """True when the -enditem `[eat]` at block_lines[idx] is a Cud Chew end-of-turn
    berry RE-EAT rather than an item loss.  A Cud Chew holder re-consumes the berry
    it ate on the PREVIOUS turn: PS emits `-activate <slot>|ability: Cud Chew` then
    `-enditem <slot>|<berry>|[eat]`, but removes NO item (the berry was already gone
    a turn earlier -- lastItem is re-run for the heal only).  The reconstructed
    pre-turn item is therefore already NONE and the engine correctly emits no
    ChangeItem, so asserting an item loss here is a false positive.  Detected by a
    `-activate ...ability: Cud Chew` for the same slot immediately preceding the
    -enditem within the same instant."""
    if "[eat]" not in block_lines[idx]:
        return False
    for line in reversed(block_lines[:idx]):
        parts = line.split("|")
        if len(parts) < 2:
            continue
        action = parts[1]
        if action in _PHASE_ACTIONS:
            return False  # crossed into a prior instant without seeing Cud Chew
        if (
            action == "-activate"
            and len(parts) >= 4
            and parts[2].split(":")[0].strip() == slot
            and _clean(parts[3].strip()) == "Cud Chew"
        ):
            return True
    return False


def _immune_trigger(block_lines: list[str], idx: int, slot: str) -> str:
    """Attribute the -immune at block_lines[idx] to the move that provoked it:
    the nearest PRECEDING |move| line whose target is the immune slot.  A Magic
    Bounce reflection is a '[from]'-tagged |move| line that still names the
    bounced move and targets the original user, so [from]-tagged lines are NOT
    skipped.  A `-activate ... ability: Synchronize` seen before any qualifying
    move line means the immunity blocked a reflected STATUS (not a move) and
    the sentinel "synchronize" is returned.  "" when nothing attributable.

    A `|-end|<same slot>|move: Future Sight` DIRECTLY before the -immune means
    the immunity blocked the DELAYED Future Sight hit at end of turn (PS
    data/conditions.ts futuremove onEnd -> trySpreadMoveHit), NOT the nearest
    regular |move| line -- attributing it to that move made the matcher demand
    "no damage from a move that really, legitimately hit this turn"
    (synth08141 T10: Overqwil's FS immunity was blamed on Ice Beam).  The
    sentinel "futuresight" routes it to the positional FS check instead."""
    prev = block_lines[idx - 1] if idx > 0 else ""
    pparts = prev.split("|")
    if (
        len(pparts) >= 4
        and pparts[1] == "-end"
        and pparts[2].split(":")[0].strip() == slot
        and pparts[3].strip() == "move: Future Sight"
    ):
        return "futuresight"
    for line in reversed(block_lines[:idx]):
        parts = line.split("|")
        if len(parts) < 2:
            continue
        action = parts[1]
        if (
            action == "-activate"
            and len(parts) >= 4
            and _clean(parts[3].strip()) == "Synchronize"
        ):
            return "synchronize"
        if action == "move" and len(parts) >= 5:
            tgt = parts[4].split(":")[0].strip()
            if tgt == slot:
                return re.sub(r"[^a-z0-9]", "", parts[3].strip().lower())
    return ""


def _cures_secondary_cause_volatile(block_lines: list[str], idx: int, slot: str) -> bool:
    """True when the -enditem at block_lines[idx] is a berry ([eat]) consumed to
    cure a volatile that was itself applied by a secondary cause the reconstructed
    pre-turn state cannot express (a `-start ... [fatigue]`/`[silent]` for the same
    slot earlier in this same instant).  The classic case: Outrage fatigue applies
    confusion, a Lum Berry immediately eats to cure it -- the engine, running the
    faithful pre-turn state with the fixed-duration lock, doesn't realize the
    fatigue this turn, so it never consumes the berry.  That item loss is a
    reconstruction artifact, not an engine defect."""
    if "[eat]" not in block_lines[idx]:
        return False
    for line in reversed(block_lines[:idx]):
        parts = line.split("|")
        if len(parts) < 2:
            continue
        action = parts[1]
        if action in _PHASE_ACTIONS:
            return False  # crossed into a prior instant
        if (
            action == "-start"
            and len(parts) >= 3
            and parts[2].split(":")[0].strip() == slot
            and any(t in line for t in _SECONDARY_CAUSE_TAGS)
        ):
            return True
    return False


def _ends_secondary_cause_volatile(
    block_lines: list[str], idx: int, slot: str, volatile: str
) -> bool:
    """True when the `-end` at block_lines[idx] closes a volatile that was STARTED
    by a secondary cause the reconstructed pre-turn state cannot express, in this
    same instant (a `-start <slot>|<same volatile>|[fatigue]`/`[silent]` earlier).

    The paired half of `_cures_secondary_cause_volatile`.  Outrage's fatigue emits
    `-start <slot>|confusion|[fatigue]`, a Lum Berry eats, and PS closes the loop
    with `-end <slot>|confusion` -- three lines, one instant.  The comparator
    already excuses the `-start` (its own raw carries the tag) and the `-enditem`
    (that back-scan), but the `-end` carries NO tag of its own, so it was asserted
    on its own and reported a missing RemoveVolatileStatus for a volatile the
    engine -- faithfully running the pre-turn state, whose locked-move counter has
    not expired -- never applied in the first place.  Same reconstruction limit,
    same treatment."""
    for line in reversed(block_lines[:idx]):
        parts = line.split("|")
        if len(parts) < 2:
            continue
        action = parts[1]
        if action in _PHASE_ACTIONS:
            return False  # crossed into a prior instant
        if (
            action == "-start"
            and len(parts) >= 4
            and parts[2].split(":")[0].strip() == slot
            and _clean(parts[3].strip()) == volatile
            and any(t in line for t in _SECONDARY_CAUSE_TAGS)
        ):
            return True
    return False


def extract_observed_events(
    block_lines: list[str], user_player_id: str, restrict_slots: dict | None = None
) -> list[ObservedEvent]:
    """Parse one turn's resolution block into ObservedEvents.

    restrict_slots: optional {"user": species, "opp": species} of the actives at
    turn start.  When given, events on a slot whose active species differs (a
    mid-turn faint replacement) are dropped so we only assert on the mons the
    engine actually simulated for this turn.  Slot tracking is done by watching
    `|switch|`/`|drag|`/`|replace|` lines as the block is walked.

    Extraction TRUNCATES at the first mid-turn replacement/pivot switch: a
    |switch|/|drag|/|replace| for a slot that already acted this block (a |move|
    or a first |switch| that was that side's decision), or one that follows a
    |faint|, opens a separate decision phase (post-faint replacement or pivot
    switch-in) the single-turn engine call does not simulate, so nothing from
    that line onward is asserted.
    """
    events: list[ObservedEvent] = []
    # current species occupying each protocol slot (p1a/p2a), by species text
    slot_species: dict[str, str] = {}
    acted_slots: set[str] = set()  # slots that used a |move| this block
    switched_slots: set[str] = set()  # slots whose decision was a |switch|
    faint_seen = False

    def _species_from_details(details: str) -> str:
        # "Illumise, L91, F" -> "illumise" (normalized-ish: lowercase, alnum)
        head = details.split(",")[0].strip().lower()
        return re.sub(r"[^a-z0-9]", "", head)

    def _in_scope(side: str, slot_tag: str) -> bool:
        if restrict_slots is None:
            return True
        cur = slot_species.get(slot_tag)
        want = restrict_slots.get(side)
        if cur is None or want is None:
            return True
        return cur == want

    def _source_in_scope(line: str) -> bool:
        # -weather/-fieldstart from an ability carry "[of] pXa:"; drop the event
        # when that slot's occupant is no longer the turn-start active (the
        # setter entered mid-turn).  Move-set weather/terrain has no source tag.
        m = _OF_SLOT.search(line)
        if m is None:
            return True
        src_slot = m.group(1)
        src_side = _side_of(src_slot + ":", user_player_id)
        return src_side is None or _in_scope(src_side, src_slot)

    def _ev(*args, **kwargs) -> ObservedEvent:
        # every event records the block line it came from: on a DEFERRED (pivot /
        # Revival Blessing) turn the checker splits the block at its second `|t:|`
        # and may only match a pre-split event against phase-1 instructions (see
        # TurnContext.phase_split).
        kwargs.setdefault("line_index", li)
        return ObservedEvent(*args, **kwargs)

    for li, line in enumerate(block_lines):
        parts = line.split("|")
        if len(parts) < 2:
            continue
        action = parts[1]

        if action == "move" and len(parts) >= 3:
            acted_slots.add(parts[2].split(":")[0].strip())
            continue

        if action == "faint":
            faint_seen = True
            continue

        if action in ("switch", "drag", "replace"):
            # parts[2]="p2a: Nick", parts[3]="Species, L.., ..."
            if len(parts) >= 4:
                slot = parts[2].split(":")[0].strip()  # "p2a"
                if faint_seen or slot in acted_slots or slot in switched_slots:
                    # post-faint replacement or pivot switch-in: a separate
                    # decision the engine call does not simulate
                    break
                switched_slots.add(slot)
                slot_species[slot] = _species_from_details(parts[3])
            continue

        # events below carry the target as parts[2] ("p2a: Nick")
        if len(parts) < 3:
            # weather/field have their label at parts[2] too, handled below
            pass

        target = parts[2] if len(parts) >= 3 else ""
        slot = target.split(":")[0].strip()
        side = _side_of(target, user_player_id)

        if action == "-status" and side and len(parts) >= 4:
            if _in_scope(side, slot):
                status_id = parts[3].strip()
                events.append(
                    _ev(
                        "status",
                        side,
                        detail=status_id,
                        raw=line,
                        cure_berry=_berry_cure(block_lines, li, slot, status_id),
                        cure_ability=_ability_cure(
                            block_lines, li, slot, status_id
                        ),
                    )
                )

        elif action in ("-boost", "-unboost") and side and len(parts) >= 5:
            if _in_scope(side, slot):
                sign = 1 if action == "-boost" else -1
                # numeric stage: PS emits a clamped cosmetic |-boost|X|stat|0
                # when the stat is already at the +-6 cap (sim/battle.ts:2030
                # getCappedBoost + :2074-2077); the comparator skips stage 0.
                # Guarded so an unparseable token degrades to None (asserted).
                try:
                    stage = int(parts[4].strip())
                except ValueError:
                    stage = None
                events.append(
                    _ev(
                        "boost",
                        side,
                        detail=parts[3].strip(),
                        sign=sign,
                        value=stage,
                        raw=line,
                    )
                )

        elif action == "-start" and side and len(parts) >= 4:
            if _in_scope(side, slot):
                events.append(
                    _ev(
                        "volatile_start",
                        side,
                        detail=_clean(parts[3].strip()),
                        raw=line,
                    )
                )

        elif action == "-end" and side and len(parts) >= 4:
            if _in_scope(side, slot) and not _ends_secondary_cause_volatile(
                block_lines, li, slot, _clean(parts[3].strip())
            ):
                events.append(
                    _ev(
                        "volatile_end", side, detail=_clean(parts[3].strip()), raw=line
                    )
                )

        elif action == "-immune" and side:
            if _in_scope(side, slot):
                events.append(
                    _ev(
                        "immune",
                        side,
                        raw=line,
                        trigger_move=_immune_trigger(block_lines, li, slot),
                    )
                )

        elif action == "-heal" and side and len(parts) >= 4:
            if _in_scope(side, slot):
                events.append(_ev("heal", side, raw=line))

        elif action == "-enditem" and side and len(parts) >= 4:
            if (
                _in_scope(side, slot)
                and not _cures_secondary_cause_volatile(block_lines, li, slot)
                and not _is_cudchew_reeat(block_lines, li, slot)
            ):
                events.append(
                    _ev(
                        "item_end", side, detail=parts[3].strip(), raw=line
                    )
                )

        elif action == "-weather" and len(parts) >= 3:
            w = _WEATHER.get(parts[2].strip())
            if w is not None and "[upkeep]" not in line and _source_in_scope(line):
                events.append(_ev("weather", "user", detail=w, raw=line))

        elif action == "-fieldstart" and len(parts) >= 3:
            label = _clean(parts[2].strip())
            t = _TERRAIN.get(label)
            if t is not None and _source_in_scope(line):
                events.append(_ev("terrain", "user", detail=t, raw=line))

        elif action in ("-sidestart", "-sideend") and len(parts) >= 4:
            pid = _tag_player(parts[2] + ":") or _tag_player(parts[2])
            # parts[2] here is "p2: name" (side, not slot)
            m = re.match(r"^(p[12])\b", parts[2].strip())
            pid = m.group(1) if m else None
            if pid is not None:
                side = "user" if pid == user_player_id else "opp"
                sc = _SIDE_CONDITION.get(parts[3].strip()) or _SIDE_CONDITION.get(
                    _clean(parts[3].strip())
                )
                if sc is not None:
                    events.append(
                        _ev("side_condition", side, detail=sc, raw=line)
                    )

    return events


def parse_action_flags(block_lines: list[str]):
    """Return {'user': tera_bool, 'opp': tera_bool, ...} plus faint info for a
    block.  Currently reports which side terastallized this turn (needed to form
    the '-tera' move string) and whether each side fainted."""
    tera = {"user": False, "opp": False}
    fainted = {"user": False, "opp": False}
    return tera, fainted  # populated by the checker with user_player_id context
