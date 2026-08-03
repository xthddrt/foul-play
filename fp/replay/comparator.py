"""Compare a turn's OBSERVED protocol outcome against the poke-engine's
predicted transition branches, and emit fidelity findings.

The engine's `generate_instructions(state, s1_move, s2_move)` returns a list of
probability-weighted branches; each branch is a `StateInstructions` whose
`instruction_list` holds `Instruction` objects that stringify (via `repr`) to
the engine's Display format, e.g.::

    Damage SideOne: 66 [move]
    ChangeStatus SideOne-P0: NONE -> PARALYZE
    Boost SideTwo Attack: -1
    ApplyVolatileStatus SideOne: SUBSTITUTE
    ChangeWeather: NONE,0 -> RAIN,5
    ChangeSideCondition SideTwo Stealthrock: 1

One real turn corresponds to ONE branch (the one whose RNG rolls actually
happened), so a fidelity check asks: is the observed outcome jointly
representable by at least one predicted branch?  A HARD observed event that no
branch can produce is a real mismatch.  Damage magnitude and anything that
depends on the opponent's exact (unknown) randbats spread is reported SOFT.

Side convention: the engine's SideOne is always the USER (the bot) and SideTwo
the OPPONENT (see the battle->state conversion).  Observed events carry a
`side` of "user"/"opp"; the checker passes `user_is_side_one=True`.

DAMAGE PROVENANCE (2026-07-27, retires APPROXIMATIONS.md U1): engines with
`DamageSource` print a trailing ` [source]` tag on every `Damage` /
`DamageSubstitute` repr (`[move]`, `[recoil]`, `[painsplit]`, `[futuresight]`,
`[status]`, ...).  When every Damage in the turn's branch set carries a tag,
the immunity check uses the tag EXACTLY (the trigger move's damage is
`[move]` — or `[futuresight]` for the delayed hit — and nothing else can
contradict the immunity).  A pre-provenance wheel prints no tag, and the check
falls back to the positional/arithmetic heuristics unchanged.
"""

import re
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum

try:
    from data import all_move_json
except Exception:  # pragma: no cover
    all_move_json = {}


class Severity(Enum):
    HARD = "hard"  # stat-independent, high-confidence fidelity claim
    SOFT = "soft"  # damage/stat-dependent or reveal-only; informational


# ---------------------------------------------------------------------------
# Instruction repr parsing
# ---------------------------------------------------------------------------

# "<Name> <SideRef>[-<Idx>][ <sub>]: <payload>"  or bare "<Name> <SideRef>"
_SIDE = {"SideOne": "s1", "SideTwo": "s2"}

# Trailing provenance tag on Damage/DamageSubstitute payloads, e.g.
# "91 [move]" / "30 [recoil]" (engines with DamageSource; see module docstring).
_SOURCE_SUFFIX = re.compile(r"^(?P<body>.*?)\s*\[(?P<source>[a-z]+)\]$")


@dataclass
class Instr:
    kind: str
    side: str | None = None  # "s1" | "s2" | None
    idx: str | None = None  # "P0".."P5" for per-pokemon instructions
    sub: str | None = None  # secondary key (stat name, side-condition, volatile)
    payload: str = ""  # everything after the colon, trimmed
    raw: str = ""
    # provenance tag ("move" | "recoil" | "painsplit" | ...) for Damage /
    # DamageSubstitute from a DamageSource-aware engine; None on older wheels
    source: str | None = None

    def amount(self) -> int | None:
        m = re.search(r"-?\d+", self.payload)
        return int(m.group()) if m else None


def parse_instruction(rep: str) -> Instr:
    rep = rep.strip()
    head, _, payload = rep.partition(":")
    payload = payload.strip()
    tokens = head.split()
    kind = tokens[0] if tokens else rep
    side = idx = sub = None
    source = None
    if kind in ("Damage", "DamageSubstitute"):
        m = _SOURCE_SUFFIX.match(payload)
        if m:
            payload = m.group("body")
            source = m.group("source")
    for tok in tokens[1:]:
        base, _, i = tok.partition("-")
        if base in _SIDE:
            side = _SIDE[base]
            if i:
                idx = i
        else:
            # stat name (Attack/Speed/...), side-condition, or volatile label
            sub = tok
    return Instr(
        kind=kind, side=side, idx=idx, sub=sub, payload=payload, raw=rep, source=source
    )


def _branches_damage_tagged(branches) -> bool:
    """True iff the branch set carries usable damage provenance: at least one
    `Damage` instruction exists and EVERY `Damage` instruction has a source
    tag.  A pre-provenance wheel tags nothing (-> False, legacy heuristics);
    a DamageSource engine tags everything, so a mixed set cannot occur."""
    seen = False
    for b in branches:
        for i in b:
            if i.kind == "Damage":
                if i.source is None:
                    return False
                seen = True
    return seen


def parse_branches(branches) -> list[list[Instr]]:
    """branches: list of poke_engine StateInstructions -> parsed instruction lists."""
    out = []
    for b in branches:
        out.append([parse_instruction(repr(i)) for i in b.instruction_list])
    return out


# ---------------------------------------------------------------------------
# Observed events (produced by protocol.py) and the finding model
# ---------------------------------------------------------------------------


@dataclass
class ObservedEvent:
    kind: str  # "status" | "boost" | "volatile_start" | "weather" | ...
    side: str  # "user" | "opp"
    detail: str = ""  # status name, stat, volatile id, weather, side-condition
    sign: int = 0  # for boosts: +/-; for side conditions: +1/-1
    value: int | None = None  # observed magnitude (e.g. exact damage to user)
    raw: str = ""
    # for "status": berry that ate the status the same instant (normalized,
    # e.g. "chestoberry"), "" when the status stuck
    cure_berry: str = ""
    # for "status": the target's OWN ability that cured the status the same
    # instant (normalized, e.g. "insomnia"), "" when nothing cured it
    cure_ability: str = ""
    # for "immune": the move that provoked the -immune (normalized, e.g.
    # "thunderwave"), or the sentinel "synchronize" when the immunity blocked a
    # Synchronize status reflection; "" when no trigger could be attributed
    trigger_move: str = ""
    # index of the block line this event was read from; used only to attribute
    # the event to a DEFERRED turn's phase (see TurnContext.phase_split)
    line_index: int | None = None


@dataclass
class Finding:
    turn: int
    severity: Severity
    category: str
    message: str
    observed: str = ""
    predicted: str = ""


@dataclass
class TurnContext:
    """Everything the matcher needs about one reconstructed turn."""

    turn: int
    branches: list[list[Instr]]
    observed: list[ObservedEvent]
    # move strings actually chosen (for messages)
    user_move: str = ""
    opp_move: str = ""
    notes: list[str] = field(default_factory=list)
    # normalized volatile names each engine side ALREADY holds at turn start,
    # keyed "s1"/"s2".  A protocol `-start` for one of these is a PS condition
    # `onRestart` re-emission (e.g. Charge re-announced by Electromorphosis on a
    # re-hit) with no engine state change, so the engine correctly produces no
    # ApplyVolatileStatus and it must not be flagged as a miss.
    pre_volatiles: dict = field(default_factory=dict)
    # turn-start max HP of each engine side's active, keyed "s1"/"s2".  Used
    # only to size an end-of-turn burn/poison chip out of the immunity check
    # (see _self_inflicted_damage_indices); absent = that rule is skipped.
    side_maxhp: dict = field(default_factory=dict)
    # turn-start CURRENT HP of each engine side's active, keyed "s1"/"s2".
    # Used only by the immunity check's lethal-capped-chip discount
    # (_lethal_capped_chip_index); absent = that rule is skipped.
    side_hp: dict = field(default_factory=dict)
    # DEFERRED-TURN (pivot / Revival Blessing) PHASE ATTRIBUTION.
    # `branches` on such a turn is the union `phase-1 ++ phase-2` per branch
    # (checker._replay_deferred_phase).  Without attribution an observed event
    # from the pre-switch part of the turn could be satisfied by a phase-2
    # instruction -- e.g. a deferred END-OF-TURN berry eat silently satisfying an
    # observed REACTIVE mid-turn eat, hiding a genuine engine gap.
    #   phase_split[k]      = number of PHASE-1 instructions in branches[k]
    #                         (instructions at index >= it are phase 2)
    #   phase2_line_index   = block-line index of the turn's SECOND `|t:|`, the
    #                         protocol boundary between the two phases
    # Both must be set (and phase_split aligned with `branches`) for the guard to
    # engage; otherwise matching is unrestricted, exactly as before.
    phase_split: list | None = None
    phase2_line_index: int | None = None


def _obs_side_to_engine(side: str, user_is_side_one: bool) -> str:
    user = "s1" if user_is_side_one else "s2"
    opp = "s2" if user_is_side_one else "s1"
    return user if side == "user" else opp


def _any_branch(branches, pred) -> bool:
    return any(any(pred(i) for i in br) for br in branches)


def _all_branches(branches, pred) -> bool:
    return all(any(pred(i) for i in br) for br in branches)


def _norm(s: str) -> str:
    """Uppercase, strip everything non-alphanumeric: 'Heal Block' -> 'HEALBLOCK',
    'move: Leech Seed' handled by _clean upstream -> 'LEECHSEED'."""
    return re.sub(r"[^A-Z0-9]", "", s.upper())


# Volatiles the engine actually tracks as ApplyVolatileStatus (PokemonVolatileStatus
# in genx/state.rs). A protocol `-start` whose name is NOT here is represented by a
# different instruction (Future Sight -> SetFutureSight, Wish -> ChangeWish, a type
# change -> ChangeType) or is display-only, so it must not be asserted as a missing
# volatile.  (TYPECHANGE is deliberately absent: see the volatile_start handler.)
ENGINE_VOLATILES = frozenset(
    _norm(v)
    for v in (
        "AQUARING ATTRACT AUTOTOMIZE BANEFULBUNKER BIDE BOUNCE BURNINGBULWARK CHARGE "
        "CONFUSION CURSE DEFENSECURL DESTINYBOND DIG DISABLE DIVE ELECTRIFY ELECTROSHOT "
        "EMBARGO ENCORE ENDURE FLASHFIRE FLINCH FLY FOCUSENERGY FOLLOWME FORESIGHT "
        "FREEZESHOCK GASTROACID GEOMANCY GLAIVERUSH GRUDGE HEALBLOCK HELPINGHAND ICEBURN "
        "IMPRISON INGRAIN KINGSSHIELD LASERFOCUS LEECHSEED LIGHTSCREEN LOCKEDMOVE MAGICCOAT "
        "MAGNETRISE MAXGUARD METEORBEAM MINIMIZE MIRACLEEYE MUSTRECHARGE NIGHTMARE NORETREAT "
        "OCTOLOCK PARTIALLYTRAPPED PERISH4 PERISH3 PERISH2 PERISH1 PHANTOMFORCE POWDER "
        "POWERSHIFT POWERTRICK PROTECT RAGE RAGEPOWDER REFLECT ROOST SALTCURE SHADOWFORCE "
        "SKULLBASH SKYATTACK SKYDROP SILKTRAP SLOWSTART SMACKDOWN SNATCH SOLARBEAM SOLARBLADE "
        "SPARKLINGARIA SPIKYSHIELD SPOTLIGHT STOCKPILE SUBSTITUTE SYRUPBOMB TARSHOT TAUNT "
        "TELEKINESIS THROATCHOP TRUANT TORMENT UNBURDEN UPROAR YAWN TRAPPED CUDCHEW "
        "TRANSFORM"
    ).split()
)

# `-start`/`-end` tags whose cause is a residual or announcement the reconstructed
# pre-turn state cannot express (e.g. locked-move fatigue confusion), so a missing
# branch is a reconstruction limit, not an engine defect.
_SECONDARY_CAUSE_TAGS = ("[fatigue]", "[silent]", "[upkeep]")


# Map Showdown status ids -> engine ChangeStatus new_status tokens.
_STATUS_MAP = {
    "brn": "BURN",
    "par": "PARALYZE",
    "psn": "POISON",
    "tox": "TOXIC",
    "slp": "SLEEP",
    "frz": "FREEZE",
}

# Recoil moves and the engine's recoil fraction (poke-engine src/choices.rs
# `recoil: Some(..)`, applied as trunc(damage_dealt * fraction) in
# genx/generate_instructions.rs:2937).  Fractions verified against PS
# data/moves.ts `recoil: [33,100]` / `[1,4]` / `[1,2]` respectively (PS rounds
# damage*33/100 etc.; over move-scale damage the two conventions differ by <2
# HP, inside the +-2 matching tolerance used below).
_RECOIL_MOVES = {
    "bravebird": 0.33,
    "doubleedge": 0.33,
    "flareblitz": 0.33,
    "woodhammer": 0.33,
    "wavecrash": 0.33,
    "volttackle": 0.33,
    "wildcharge": 0.25,
    "takedown": 0.25,
    "submission": 0.25,
    "headsmash": 0.5,
}

# immune-event trigger sentinels that are status-only by construction (not
# move names): a Synchronize reflection carries the status, never damage.
_STATUS_ONLY_TRIGGERS = frozenset(("synchronize",))


def _self_inflicted_damage_indices(branch, side: str, maxhp: int | None) -> set[int]:
    """Positions in `branch` of Damage instructions on `side` that are NOT the
    opposing move's damage: a confusion self-hit, or an end-of-turn status chip.

    Both are structurally identifiable in the engine's emission order:

      * CONFUSION self-hit -- the self-hit branch pushes
        `ChangeVolatileStatusDuration <side> CONFUSION: 1` and then IMMEDIATELY
        the self-damage (genx/generate_instructions.rs:4063-4098), so the
        Damage sits directly after that duration change.  (The move-continuation
        sibling pushes the same duration change with NO damage after it, which
        is exactly why the self-hit's damage varies across the branch set and
        survives the uniform-background subtraction below.)
      * TOXIC chip -- the residual pushes the tick damage and then
        `ChangeSideCondition <side> ToxicCount: 1` (:6606-6660), so the Damage
        sits directly before that counter increment.
      * BURN / POISON chip -- no adjacent marker, so it is identified by
        magnitude against the side's turn-start max HP: trunc(maxhp * 1/16) for
        burn (gen7+; halved again under Heatproof) and trunc(maxhp * 1/8) for
        poison (:6541-6600).  Only ONE such Damage is discounted, searched from
        the END of the branch (residuals are emitted after the moves).

    Every rule discards AT MOST ONE instruction, so a real move-scale immunity
    breach -- damage on the immune side in every branch that is none of these --
    still survives to fire HARD."""
    out: set[int] = set()
    n = len(branch)

    def _dmg(i: int) -> bool:
        return (
            0 <= i < n
            and branch[i].kind == "Damage"
            and branch[i].side == side
            and (branch[i].amount() or 0) > 0
        )

    for i, instr in enumerate(branch):
        if (
            instr.kind == "ChangeVolatileStatusDuration"
            and instr.side == side
            and _norm(instr.sub or "") == "CONFUSION"
            and _dmg(i + 1)
        ):
            out.add(i + 1)
            break
    for i, instr in enumerate(branch):
        if (
            instr.kind == "ChangeSideCondition"
            and instr.side == side
            and _norm(instr.sub or "") == "TOXICCOUNT"
            and _dmg(i - 1)
            and (i - 1) not in out
        ):
            out.add(i - 1)
            break
    if maxhp and maxhp > 0:
        chips = {
            max(1, int(maxhp * 0.0625)),  # burn, gen7+
            max(1, int(maxhp * 0.03125)),  # burn under Heatproof
            max(1, int(maxhp * 0.125)),  # poison / partial-trap (gen6+ 1/8)
        }
        for i in range(n - 1, -1, -1):
            if i in out or not _dmg(i):
                continue
            if branch[i].amount() in chips:
                out.add(i)
                break
    return out


def _lethal_capped_chip_index(
    branch, side: str, maxhp: int | None, start_hp: int | None, already: set[int]
) -> set[int]:
    """Position of at most ONE trailing Damage on `side` that is a LETHAL,
    HP-CAPPED end-of-turn chip: the engine clamps every residual chip (burn /
    poison / partial-trap) to the holder's remaining hp (`cmp::min(chip, hp)`),
    so on a branch where the mon dies to the chip its amount is the remaining hp
    -- NOT the nominal maxhp/8-16 the magnitude rule in
    `_self_inflicted_damage_indices` recognizes -- and it varies with the same
    branch's earlier roll-dependent damage/heals (synth43826 T6: Araquanid's
    Whirlpool chip reads 23 on one Leech-Life-drain branch and 25 on the other,
    defeating the uniform background of its Endeavor immunity).  Identified by
    exact running-hp arithmetic: the LAST Damage on `side` whose amount equals
    the hp remaining after every earlier Damage/Heal on that side, is <= the
    largest nominal chip (maxhp/8), and is not already discounted."""
    out: set[int] = set()
    if not maxhp or maxhp <= 0 or start_hp is None or start_hp <= 0:
        return out
    last = None
    for i, instr in enumerate(branch):
        if instr.kind == "Damage" and instr.side == side and (instr.amount() or 0) > 0:
            last = i
    if last is None or last in already:
        return out
    hp = start_hp
    for i, instr in enumerate(branch[:last]):
        amt = instr.amount() or 0
        if instr.kind == "Damage" and instr.side == side:
            hp -= max(0, amt)
        elif instr.kind == "Heal" and instr.side == side:
            hp = min(maxhp, hp + amt)
    hp = max(0, hp)
    amount = branch[last].amount() or 0
    if amount == hp and amount <= max(1, int(maxhp * 0.125)):
        out.add(last)
    return out

# Showdown -boost stat ids -> engine Boost stat tokens.
_STAT_MAP = {
    "atk": "Attack",
    "def": "Defense",
    "spa": "SpecialAttack",
    "spd": "SpecialDefense",
    "spe": "Speed",
    "accuracy": "Accuracy",
    "evasion": "Evasion",
}


def compare_turn(ctx: TurnContext, user_is_side_one: bool = True) -> list[Finding]:
    """Return fidelity findings for one turn.  Empty == engine reproduced every
    hard observed effect in at least one branch."""
    findings: list[Finding] = []
    br = ctx.branches
    if not br:
        return findings

    def eng_side(side):
        return _obs_side_to_engine(side, user_is_side_one)

    def phase_view(ev):
        """The branch slices this event is ALLOWED to be matched against.

        On a deferred turn the branch list is `phase-1 ++ phase-2`; an event read
        from before the block's second `|t:|` happened in phase 1 and may only be
        satisfied by a phase-1 instruction, and vice versa.  Any missing piece of
        attribution information (non-deferred turn, un-indexed event, mismatched
        split list) falls back to the whole branch, i.e. the pre-guard behavior."""
        splits = ctx.phase_split
        if (
            not splits
            or ctx.phase2_line_index is None
            or ev.line_index is None
            or len(splits) != len(ctx.branches)
        ):
            return ctx.branches
        if ev.line_index >= ctx.phase2_line_index:
            return [b[k:] for b, k in zip(ctx.branches, splits)]
        return [b[:k] for b, k in zip(ctx.branches, splits)]

    for ev in ctx.observed:
        s = eng_side(ev.side)
        br = phase_view(ev)

        if ev.kind == "status":
            want = _STATUS_MAP.get(ev.detail.lower())
            if want is None:
                continue
            ok = _any_branch(
                br,
                lambda i: i.kind == "ChangeStatus"
                and i.side == s
                and want in i.payload,
            )
            if ev.cure_ability:
                # a status-immunity ability (Insomnia / Water Veil / Limber /
                # Immunity / Vital Spirit / Magma Armor ...) applied-and-cured
                # the status in one instant.  PS prints the transient
                # -status/-activate/-curestatus triple; poke-engine models the
                # same ability as a plain immunity and never applies the status,
                # so NO branch carries a ChangeStatus and the post-turn state is
                # nevertheless identical.  Not a fidelity breach -- see
                # protocol._ability_cure.
                continue
            if not ok and ev.cure_berry:
                # the status was eaten away the same instant (-status /
                # -enditem [eat] / -curestatus): the engine collapses
                # apply+cure into the bare berry consumption, so a branch
                # eating that berry models the observed net
                berry = _norm(ev.cure_berry)
                ok = _any_branch(
                    br,
                    lambda i: i.kind == "ChangeItem"
                    and i.side == s
                    and berry in _norm(i.payload),
                )
            if not ok:
                findings.append(
                    Finding(
                        ctx.turn,
                        Severity.HARD,
                        "status",
                        "observed {} on {} but no branch applies it".format(
                            ev.detail, ev.side
                        ),
                        observed=ev.raw,
                        predicted="no ChangeStatus->{} on {}".format(want, s),
                    )
                )

        elif ev.kind == "boost":
            stat = _STAT_MAP.get(ev.detail.lower())
            if stat is None:
                continue
            if ev.value == 0:
                # A 0-stage |-boost|X|stat|0 is PS's clamped COSMETIC line: the
                # stat is already at the +-6 cap, getCappedBoost zeroes the
                # delta (sim/battle.ts:2030) and `if (boostBy)` skips the real
                # arm (sim/battle.ts:2045), but the ability/self else-arms
                # still `add` the line with boostBy 0 (sim/battle.ts:2074-2077).
                # No state changes, and the engine deliberately emits no
                # 0-delta Boost instruction -- not a miss.  (value None means
                # the stage could not be parsed: fall through and assert.)
                continue
            ok = _any_branch(
                br,
                lambda i: i.kind == "Boost"
                and i.side == s
                and i.sub == stat
                and (i.amount() or 0) * ev.sign > 0,
            )
            if not ok:
                findings.append(
                    Finding(
                        ctx.turn,
                        Severity.HARD,
                        "boost",
                        "observed {} {} on {} but no branch produces it".format(
                            "+" if ev.sign > 0 else "-", ev.detail, ev.side
                        ),
                        observed=ev.raw,
                    )
                )

        elif ev.kind == "volatile_start":
            vol = _norm(ev.detail)
            if any(t in ev.raw for t in _SECONDARY_CAUSE_TAGS):
                continue
            if vol == "TYPECHANGE":
                # A type change (Protean/Libero/Color Change/Soak/Reflect Type/
                # ...) surfaces as `-start ... typechange`, but poke-engine
                # represents it with a ChangeType instruction, or folds the new
                # type into the mon's base types -- never as an ApplyVolatileStatus.
                # A ChangeType on this side satisfies it, and when the engine folds
                # the type in it emits none; either way the -start is a display-only
                # announcement whose mechanical consequences (STAB/immunity/resist/
                # damage) are asserted by the OTHER observed-event kinds, so it is
                # never a standalone HARD volatile miss.
                continue
            if vol in ctx.pre_volatiles.get(s, ()):
                # target already holds this volatile at turn start: PS's condition
                # onRestart re-emits an identical `-start` (Charge re-announced by
                # Electromorphosis/Wind Power on a re-hit, ...) with no engine state
                # change, so no ApplyVolatileStatus is (or should be) produced.
                continue
            if vol not in ENGINE_VOLATILES:
                continue  # engine represents it via a non-volatile instruction
            ok = _any_branch(
                br,
                lambda i: i.kind == "ApplyVolatileStatus"
                and i.side == s
                and _norm(i.payload) == vol,
            )
            if not ok:
                findings.append(
                    Finding(
                        ctx.turn,
                        Severity.HARD,
                        "volatile",
                        "observed volatile {} start on {} but no branch applies it".format(
                            ev.detail, ev.side
                        ),
                        observed=ev.raw,
                    )
                )

        elif ev.kind == "volatile_end":
            vol = _norm(ev.detail)
            if any(t in ev.raw for t in _SECONDARY_CAUSE_TAGS):
                continue
            if vol not in ENGINE_VOLATILES:
                continue
            ok = _any_branch(
                br,
                lambda i: i.kind == "RemoveVolatileStatus"
                and i.side == s
                and _norm(i.payload) == vol,
            )
            if not ok and vol == "SUBSTITUTE":
                # BRANCH-FOLD RESIDUAL, not a mechanic miss.  A Substitute breaks
                # exactly when the hit's damage reaches its stored HP, but the
                # engine's Branch model folds the 16 exact rolls into a kill arm and
                # a survivor MEAN - it fans at the defender-KO threshold only, never
                # at the substitute-break threshold - so a roll spread that STRADDLES
                # the sub's HP is presented as the single folded value and no branch
                # carries the break.  synth132960 T45: Sleep Talk -> Wave Crash, sub
                # health 59, folded damage 58, while the engine's OWN exact roll set
                # for that hit is [54,54,54,55,56,57,57,58,59,59,60,60,61,62,63,63] -
                # 8 of the 16 rolls do break it, i.e. PS's `|-end|Substitute` is
                # inside the engine's damage model and only the fold misses it.
                # A branch that damaged the substitute on this side proves the
                # mechanic ran; the break is then a roll selection the fold cannot
                # express, the same class as the residual suppressions in the other
                # direction.  Without that evidence the report still stands.
                ok = _any_branch(
                    br, lambda i: i.kind == "DamageSubstitute" and i.side == s
                )
            if not ok:
                findings.append(
                    Finding(
                        ctx.turn,
                        Severity.SOFT,
                        "volatile",
                        "observed volatile {} end on {} but no branch removes it".format(
                            ev.detail, ev.side
                        ),
                        observed=ev.raw,
                    )
                )

        elif ev.kind == "weather":
            ok = _any_branch(
                br,
                lambda i: i.kind == "ChangeWeather"
                and _norm(ev.detail) in _norm(i.payload),
            )
            if not ok:
                findings.append(
                    Finding(
                        ctx.turn,
                        Severity.HARD,
                        "weather",
                        "observed weather {} but no branch sets it".format(ev.detail),
                        observed=ev.raw,
                    )
                )

        elif ev.kind == "terrain":
            ok = _any_branch(
                br,
                lambda i: i.kind == "ChangeTerrain"
                and _norm(ev.detail) in _norm(i.payload),
            )
            if not ok:
                findings.append(
                    Finding(
                        ctx.turn,
                        Severity.HARD,
                        "terrain",
                        "observed terrain {} but no branch sets it".format(ev.detail),
                        observed=ev.raw,
                    )
                )

        elif ev.kind == "side_condition":
            ok = _any_branch(
                br,
                lambda i: i.kind == "ChangeSideCondition"
                and i.side == s
                and _norm(i.sub or "") == _norm(ev.detail),
            )
            if not ok:
                findings.append(
                    Finding(
                        ctx.turn,
                        Severity.HARD,
                        "side_condition",
                        "observed side-condition {} change on {} but no branch produces it".format(
                            ev.detail, ev.side
                        ),
                        observed=ev.raw,
                    )
                )

        elif ev.kind == "item_end":
            ok = _any_branch(
                br,
                lambda i: i.kind == "ChangeItem" and i.side == s,
            )
            if not ok:
                # A berry self-consumed on an HP threshold (`-enditem ... [eat]`,
                # e.g. Sitrus at <=50% max HP) fires only when HP crosses the gate,
                # which turns on the exact damage/HP magnitude -- and for the
                # opponent on its unknown randbats spread.  Like damage magnitude
                # that is reported SOFT, not a HARD fidelity breach.  A FORCED
                # removal (Knock Off / Trick / Thief / Incinerate, tagged `[from]
                # move:`/`[from] ability:`) does not depend on HP, so it stays HARD.
                eaten = "[eat]" in ev.raw
                findings.append(
                    Finding(
                        ctx.turn,
                        Severity.SOFT if eaten else Severity.HARD,
                        "item",
                        "observed item loss ({}) on {} but no branch removes it".format(
                            ev.detail, ev.side
                        ),
                        observed=ev.raw,
                    )
                )

        elif ev.kind == "immune":
            # a deterministic immunity: some branch must leave the target
            # undamaged by the ATTACKER's move. Damage identical across every
            # branch is background (Leech Seed / burn residual, recoil from the
            # immune side's own deterministic move) -- the move's own damage
            # varies with its hit/miss/proc branching -- so it cannot
            # contradict the immunity.
            trig = ev.trigger_move
            if trig and (
                trig in _STATUS_ONLY_TRIGGERS
                or all_move_json.get(trig, {}).get("category") == "status"
            ):
                # the immunity blocked a STATUS-category trigger (Thunder Wave
                # vs a Ground type, a Synchronize status reflection): the
                # trigger deals no damage, so Damage on the immune side
                # (end-of-turn chip, its own move's recoil) is causally
                # unrelated and cannot contradict the immunity.
                continue
            o = "s2" if s == "s1" else "s1"
            tagged = _branches_damage_tagged(br)
            if trig == "futuresight":
                # DELAYED-HIT immunity (`|-end|X|move: Future Sight` + |-immune|).
                #
                # PROVENANCE PATH (DamageSource engines): a landed delayed hit
                # is exactly a `Damage <immune side> [futuresight]`; an immune
                # pop emits `DecrementFutureSight <caster side>` with no such
                # Damage.  Decidable directly -- no positional scanning, and
                # same-turn recoil/move damage on the immune side cannot
                # confound it (synth31061 T22: Brave Bird recoil is `[recoil]`).
                if tagged:
                    def _fs_immune_modeled_tagged(branch) -> bool:
                        if not any(
                            i.kind == "DecrementFutureSight" and i.side == o
                            for i in branch
                        ):
                            # no pending FS modeled: cannot confirm the immunity
                            return False
                        return not any(
                            i.kind == "Damage"
                            and i.side == s
                            and i.source == "futuresight"
                            and (i.amount() or 0) > 0
                            for i in branch
                        )

                    if not any(_fs_immune_modeled_tagged(b) for b in br):
                        findings.append(
                            Finding(
                                ctx.turn,
                                Severity.HARD,
                                "immunity",
                                "protocol shows {} immune to the delayed Future Sight "
                                "but every branch lands it".format(ev.side),
                                observed=ev.raw,
                            )
                        )
                    continue
                # LEGACY PATH (no provenance tags): the damaged-everywhere test
                # below is unusable here -- the other
                # side's REGULAR move legitimately damaged the immune mon this
                # same turn and its rolls vary across branches, so the immune
                # side is "damaged everywhere" even when the engine handled the
                # FS immunity perfectly (synth08141 T10 / synth23711 T20 /
                # synth41461 T47).  Use the engine's positional signature
                # instead: a LANDED delayed hit emits `Damage <immune side>`
                # (+ the Air Balloon / Stamina responses) immediately BEFORE
                # `DecrementFutureSight <caster side>` (genx
                # generate_instructions.rs FS end-of-turn block); an immune pop
                # emits the decrement alone.
                fs_interleaved = {
                    "ChangeItem",
                    "Boost",
                    "RemoveVolatileStatus",
                    "DamageSubstitute",
                }

                def _fs_immune_modeled(branch) -> bool:
                    for k, ins in enumerate(branch):
                        if ins.kind == "DecrementFutureSight" and ins.side == o:
                            j = k - 1
                            while j >= 0 and branch[j].kind in fs_interleaved:
                                j -= 1
                            return not (
                                j >= 0
                                and branch[j].kind == "Damage"
                                and branch[j].side == s
                            )
                    # no pending FS modeled at all: cannot confirm the immunity
                    return False

                if not any(_fs_immune_modeled(b) for b in br):
                    findings.append(
                        Finding(
                            ctx.turn,
                            Severity.HARD,
                            "immunity",
                            "protocol shows {} immune to the delayed Future Sight "
                            "but every branch lands it".format(ev.side),
                            observed=ev.raw,
                        )
                    )
                continue
            # PROVENANCE PATH (DamageSource engines): the trigger move's direct
            # damage is tagged `[move]` and NOTHING else on the immune side is
            # (recoil `[recoil]`, Pain Split `[painsplit]`, chips `[status]` /
            # `[residual]` / `[weather]` / `[item]` / `[ability]`, a same-turn
            # delayed hit `[futuresight]`), so the assertion is decidable
            # directly: some branch must carry NO positive `[move]` Damage on
            # the immune side.  This retires APPROXIMATIONS.md U1 -- both of
            # its members (synth45908 T22 Pain Split, synth31061 T22 recoil)
            # are exactly the confounders the tag disambiguates.
            #
            # The Endeavor exact-HP-boundary demotion that used to live here
            # (APPROXIMATIONS U3) is GONE, with its entry, per that entry's own
            # revisit trigger: the protocol's HP identities are now propagated
            # into the reconstruction (fp/hp_certificate.py), so a landed
            # Endeavor pins `target.hp == attacker.hp` exactly instead of the
            # comparator having to tolerate a 1-2 HP modelled Endeavor as
            # "the same state at reconstruction precision".  An Endeavor
            # immunity is now decided like any other.
            if tagged:
                move_dmg = [
                    [
                        i.amount()
                        for i in b
                        if i.kind == "Damage"
                        and i.side == s
                        and i.source == "move"
                        and (i.amount() or 0) > 0
                    ]
                    for b in br
                ]
                if any(not dmgs for dmgs in move_dmg):
                    continue  # some branch models the immunity exactly
                findings.append(
                    Finding(
                        ctx.turn,
                        Severity.HARD,
                        "immunity",
                        "protocol shows {} immune but every branch damages it "
                        "with the move (damage provenance)".format(ev.side),
                        observed=ev.raw,
                    )
                )
                continue
            # LEGACY PATH (no provenance tags -- pre-DamageSource wheel):
            # Damage the immune side inflicted on ITSELF (confusion self-hit) or
            # took from its own end-of-turn status chip is causally unrelated to
            # the immunity, and -- unlike a uniform background -- it VARIES
            # across the branch set (the self-hit exists on one branch only; the
            # chip is capped by the HP the self-hit left).  It has to be dropped
            # BEFORE the damaged-everywhere test, otherwise a turn where BOTH
            # moves are immune (no move damage anywhere to form a background)
            # can never satisfy the test: synth02141 T40, Shadow Ball into a
            # Normal type while its own Boomburst is blocked by a Ghost, where
            # branch A carries only the confusion self-hit (10) and branch B
            # only the toxic tick (31), and the empty background left both
            # residuals standing.  See _self_inflicted_damage_indices.
            maxhp = (ctx.side_maxhp or {}).get(s)
            start_hp = (ctx.side_hp or {}).get(s)
            profiles = []
            for b in br:
                drop = _self_inflicted_damage_indices(b, s, maxhp)
                drop |= _lethal_capped_chip_index(b, s, maxhp, start_hp, drop)
                profiles.append(
                    Counter(
                        i.amount()
                        for k, i in enumerate(b)
                        if k not in drop
                        and i.kind == "Damage"
                        and i.side == s
                        and (i.amount() or 0) > 0
                    )
                )
            # per-branch OTHER-side move damage (used by the recoil discount
            # and as the move-damage scale for the chip-size downgrade)
            other_dmg = [
                [
                    i.amount()
                    for i in b
                    if i.kind == "Damage" and i.side == o and (i.amount() or 0) > 0
                ]
                for b in br
            ]
            # the immune side's own move was a RECOIL move: its recoil Damage
            # tracks the roll of its own hit (engine: trunc(damage * r),
            # genx/generate_instructions.rs:2937), so it varies across branches
            # and would defeat the uniform-background subtraction.  Discard at
            # most ONE immune-side Damage `a` per branch that matches the
            # recoil relation |a - r*D| <= 2 against a same-branch other-side
            # Damage D, then run the normal check on the residuals.
            own_move = (ctx.user_move if ev.side == "user" else ctx.opp_move) or ""
            if own_move.endswith("-tera"):
                own_move = own_move[: -len("-tera")]
            r = _RECOIL_MOVES.get(own_move)
            if r is not None:
                for p, dmgs in zip(profiles, other_dmg):
                    for a in sorted(p):
                        if any(abs(a - r * d) <= 2 for d in dmgs):
                            p[a] -= 1
                            if p[a] <= 0:
                                del p[a]
                            break
            if own_move == "painsplit":
                # the immune side's own Pain Split re-averages BOTH actives' hp:
                # the engine emits the pair `Damage <immune side> +d` /
                # `Damage <other side> -d` (or mirrored), and d varies with the
                # other side's roll-dependent hp -- so it defeats the uniform
                # background exactly like recoil does (synth36693 T23:
                # tera-Dark Dusknoir's Psyshock immunity was contradicted only
                # by its own Pain Split's hp adjustment).  Discard at most ONE
                # immune-side Damage per branch that is PAIRED with an
                # equal-and-opposite Damage on the other side in that branch.
                for p, b in zip(profiles, br):
                    negs = {
                        -(i.amount() or 0)
                        for i in b
                        if i.kind == "Damage"
                        and i.side == o
                        and (i.amount() or 0) < 0
                    }
                    for a in sorted(p):
                        if a in negs:
                            p[a] -= 1
                            if p[a] <= 0:
                                del p[a]
                            break
            background = profiles[0].copy()
            for p in profiles[1:]:
                background &= p
            damaged_everywhere = all(p - background for p in profiles)
            if damaged_everywhere:
                # chip-scale residuals (every branch's non-background damage
                # < 25% of that branch's smallest move damage on the other
                # side) are end-of-turn-chip variance the reconstructed state
                # can misalign (Leftovers/status timing) -- informational, not
                # a hard breach.  Move-scale residuals, or branches with no
                # other-side damage to set the scale, stay HARD.
                chip_scale = True
                for p, dmgs in zip(profiles, other_dmg):
                    residual = p - background
                    if not residual:
                        continue
                    if not dmgs:
                        chip_scale = False
                        break
                    threshold = 0.25 * min(dmgs)
                    if any(a >= threshold for a in residual):
                        chip_scale = False
                        break
                # The ENDEAVOR EXACT-HP BOUNDARY demotion (APPROXIMATIONS U3)
                # used to sit here, softening any branch whose residual Endeavor
                # damage was <= 2 on the grounds that the observed `-immune` and
                # a modelled 1-2 damage Endeavor were "the same state at
                # reconstruction precision".  It is DELETED with its registry
                # entry, per that entry's own revisit trigger: Endeavor's
                # immunity is `pokemon.hp < target.hp` (PS data/moves.ts
                # endeavor onTryImmunity), and a landed Endeavor now pins
                # `target.hp == attacker.hp` EXACTLY in the reconstruction
                # (fp/hp_certificate.py), so the comparison is decidable rather
                # than tolerance-bound.  Verified by execution over all 829
                # corpus games containing Endeavor: zero findings of any
                # severity in the `immunity` category, where gate 4 had 1 hard
                # and 18 soft.
                findings.append(
                    Finding(
                        ctx.turn,
                        Severity.SOFT if chip_scale else Severity.HARD,
                        "immunity",
                        "protocol shows {} immune but every branch damages it".format(
                            ev.side
                        ),
                        observed=ev.raw,
                    )
                )

        elif ev.kind == "heal":
            # Revival Blessing's half-HP restore is announced as a `-heal` on the
            # revived RESERVE (`|-heal|p2: Vigoroth|50/100|[from] move: Revival
            # Blessing`) but the engine models it with its own instruction --
            # `Revive SideTwo-P1: 126` (genx/generate_instructions.rs:236-247),
            # never a Heal -- so a Revive on that side satisfies the event.
            #
            # A max-HP-raising forme change is announced the same way: PS's
            # Pokemon#setType/formeChange path emits a SILENT `-heal`
            # (`|-heal|p1a: Terapagos|311/373|[silent]`) for the HP the higher
            # base-HP forme gains -- Terapagos-Terastal -> Terapagos-Stellar on
            # terastallizing (sim/battle-actions.ts:1956-1958 formeChange +
            # sim/pokemon.ts setMaxHP heal).  poke-engine models that with
            # `ChangeMaxHP <side>: maxhp N hp N`, which raises hp and maxhp
            # together and is emitted INSTEAD of a Heal, so it satisfies the
            # event exactly like Revive does.
            ok = _any_branch(
                br, lambda i: i.kind in ("Heal", "Revive", "ChangeMaxHP") and i.side == s
            )
            if not ok:
                findings.append(
                    Finding(
                        ctx.turn,
                        Severity.SOFT,
                        "heal",
                        "observed heal on {} but no branch heals it".format(ev.side),
                        observed=ev.raw,
                    )
                )

    return findings
