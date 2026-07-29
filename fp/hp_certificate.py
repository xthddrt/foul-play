"""Exact-HP certificates for the OPPONENT-side HP reconstruction.

WHY THIS EXISTS
---------------
An opponent's HP reaches us only as a percent (`|-damage|p2a: X|25/100`), which
PS derives with `Math.ceil(100 * hp / maxhp)` (sim/pokemon.ts:2079-2086, capped
to 99 whenever `hp < maxhp`).  That is an INTERVAL, not a number, and the
reconstruction used to collapse it to `max_hp * pct` -- a point estimate that can
land OUTSIDE the interval the protocol actually stated (310 * 0.25 = 77.5 -> 78,
while `ceil(100*hp/310) == 25` means `hp in [75, 77]`).

Two separate things are fixed here:

1. **Interval clamping.**  Every percent display is turned into its exact
   integer bound pair and the point estimate is clamped into it, so the
   reconstruction never asserts an HP the protocol has ruled out.  This is
   unconditional and cheap.

2. **Certificates.**  Some protocol events state an opponent's HP EXACTLY, as an
   identity rather than a display.  A landed Endeavor makes `target.hp ==
   user.hp` (PS data/moves.ts:4789-4791 `damageCallback` = `target.
   getUndynamaxedHP() - pokemon.hp`) and our own side's HP is request-exact; a
   Pain Split makes both sides `floor((a+b)/2)`; a 100/100 display means
   `hp == maxhp` exactly (the `percentage === 100 && hp < maxhp -> 99` clamp);
   Revival Blessing revives at `floor(maxhp/2)`.  When one of those fires, the
   exact value REPLACES the display's point estimate for that event, and the
   pokemon is flagged `hp_exact`.

CERTIFICATE LIFETIME
--------------------
A certificate is a statement about the HP at ONE instant.  It stays valid until
something moves that HP by an amount the percent protocol does not tell us --
which is exactly "the next percent display for this pokemon" -- or until the
pokemon leaves the field (`Regenerator` and friends change HP with no event we
observe).  So:

* a certifying event sets `hp_exact = True` and pins `hp`;
* the display carried by that SAME event never overwrites the certificate;
* the NEXT display resets to an interval-clamped estimate (`hp_exact = False`),
  unless that display is itself exact (pct == 100, `0 fnt`, or an interval that
  happens to be a singleton);
* a switch-out clears it.

DISPLAY CONSISTENCY CHECK
-------------------------
A certificate must agree with the percent the same line carried:
`display_pct(certified_hp, max_hp)` must equal the shown pct.  A disagreement
means the chain is broken somewhere (a move model that is not what PS does, an
expectation consumed by the wrong `-damage` line), and the honest response is to
REFUSE the certificate loudly and fall back to the interval estimate -- never to
silently re-derive, and never to pin a value the protocol contradicts.  Refusals
are counted in `CERTIFICATE_REFUSALS` so a corpus sweep surfaces them instead of
burying them.

**The check is only meaningful against an EXACT max HP.**  `display_pct` divides
by max_hp, and for an opponent that number is a randbats-spread GUESS until a
full-knowledge sidecar corrects it.  Checking a certificate against a guess
cannot tell you which of the two is wrong -- and it gets this backwards in
practice: synth43722 T20's Endeavor certificate (186, exactly right) implies
71/100 against the GUESSED max_hp 265 while the protocol showed 69/100, because
the guess, not the certificate, was wrong.  So certificates are accepted and
their shown pct is REMEMBERED (`hp_certificate_pct`), and the verdict is taken
in `verify_against_exact_max` at the moment max HP becomes authoritative.  The
certified value itself never depends on max_hp -- it is an absolute integer read
off an identity -- so it is right even while unverifiable.

DELIBERATELY NOT IMPLEMENTED (see APPROXIMATIONS.md discussion)
---------------------------------------------------------------
* **Exact-delta chip propagation** (burn/poison/hazard/Leftovers arithmetic off a
  certified anchor).  Every one of those amounts is a function of max_hp AND of
  modifiers the reconstruction only guesses (Heatproof, Magic Guard, Poison Heal,
  the toxic counter, hazard effectiveness after a Tera, Air Balloon...).  A wrong
  "exact" delta is worse than an honest interval, and the display check catches
  only the minority of such errors that cross a percent boundary.
* **Super Fang / Nature's Madness / Ruination given an UNKNOWN prior.**  Their
  damage is `max(1, floor(hp/2))` (PS data/moves.ts:18459-18461, 12634-12636,
  15526-15528), so they HALVE the width of the interval but certify nothing on
  their own.  They ARE propagated when the prior is already certified (an exact
  input gives an exact output) -- that arm is implemented below.  When the
  prior is exact only RELATIVE to a guessed max HP (`hp == max_hp` after a
  Rest), the output is propagated as a derivation -- (fraction, halvings) --
  and replayed against the true max when it lands, never frozen as an integer
  computed off the guess (see `hp_certificate_halvings`).
* **Consuming the damage-membership roll identification.**  When a hit's observed
  damage matches exactly one engine roll the true delta is known, which would
  certify the defender.  It is not consumed: membership is computed FROM the
  reconstructed state (defender HP included), so feeding it back would make the
  membership check partly self-confirming.  Keeping the two directions separate
  is what lets membership be evidence at all.
"""

import logging

logger = logging.getLogger(__name__)

# Number of times a certificate disagreed with the percent its own line carried
# and was refused.  Non-zero means a certification chain is broken and the
# reason must be found, not tolerated.
CERTIFICATE_REFUSALS: list = []

# Where the reconstruction currently is (game / turn), stamped onto every
# refusal record.  A corpus-wide count with no address is invisible without
# re-instrumentation -- gate 5's B5 found exactly that -- so the checker sets
# this as it walks the log and each refusal carries its own crime scene.
CONTEXT: dict = {"game": None, "turn": None}


def set_context(game=None, turn=None) -> None:
    if game is not None:
        CONTEXT["game"] = game
    if turn is not None:
        CONTEXT["turn"] = turn


def reset_refusals() -> None:
    CERTIFICATE_REFUSALS.clear()
    CONTEXT["game"] = None
    CONTEXT["turn"] = None


def exact_display_hp(condition: str):
    """`(hp, max_hp)` when this shared health string is an EXACT display, else None.

    PS's `getHealth` (sim/pokemon.ts:2065-2086) emits the secret string
    `${hp}/${maxhp}` as the SHARED display whenever `battle.reportExactHP` is
    set (from `format.debug`, sim/battle.ts:227).  Otherwise gen >= 7 renders
    `pct/100` (with the 99-clamp) and older gens `pixels/48`.  The denominator
    therefore discriminates: `100` -> percent, `48` -> pixels, anything else is
    the mon's TRUE max HP and the numerator its TRUE current HP.  Treating the
    numerator of an exact display as a percent was the dominant real-ladder
    reconstruction defect: every damage display with a numerator >= 100 clamped
    the mon back to FULL (`display_bounds(pct >= 100) == (max, max)`), which is
    why 37 of 40 real-ladder `heal [from] item: Leftovers` soft rows held the
    healed mon at exactly max HP while PS's own heal line proves hp < maxhp
    (sim/battle.ts:2272 short-circuits the heal at full).

    A "champions" display appends a letter to the denominator (`50g`); the int
    parse fails and the caller falls back to the percent path, matching the
    existing champions guards.  `0 fnt` and malformed strings return None."""
    if not condition or "fnt" in condition or "/" not in condition:
        return None
    num_s, _, den_s = condition.partition("/")
    den_parts = den_s.split()
    den_s = den_parts[0] if den_parts else ""
    try:
        num = int(num_s)
        den = int(den_s)
    except ValueError:
        return None
    if den in (100, 48) or den <= 0 or num < 0 or num > den:
        return None
    return num, den


def set_exact_from_display(pkmn, exact) -> None:
    """Fold an exact `(hp, max_hp)` display (see `exact_display_hp`) into the
    reconstruction: both values are server-stated truths, strictly stronger
    than any percent band or pending certificate."""
    hp, max_hp = exact
    pkmn.max_hp = int(max_hp)
    pkmn.max_hp_exact = True
    set_exact(pkmn, hp)


def display_pct(hp, max_hp) -> int:
    """PS's shared health string, numerator only (sim/pokemon.ts:2080-2086).

    `Math.ceil(100 * hp / maxhp)`, forced down to 99 when the mon is not at full
    HP, and 0 for a fainted mon (rendered `0 fnt`)."""
    hp = int(hp)
    max_hp = int(max_hp)
    if max_hp <= 0 or hp <= 0:
        return 0
    pct = -((-100 * hp) // max_hp)  # ceil(100*hp/max_hp) in integer arithmetic
    if pct == 100 and hp < max_hp:
        pct = 99
    return pct


def display_bounds(pct, max_hp) -> tuple[int, int]:
    """Inclusive integer bounds on hp implied by a `pct/100` display.

    Inverts `display_pct`:
      * pct == 100  ->  hp == max_hp exactly (the 99-clamp makes 100 unambiguous)
      * 1 <= pct <= 99 -> ceil(100*hp/max_hp) == pct
                       -> hp in (  (pct-1)*max_hp/100 , pct*max_hp/100 ]
        i.e. [floor((pct-1)*max_hp/100) + 1, floor(pct*max_hp/100)], and for
        pct == 99 the upper bound is max_hp - 1 because the clamp folds the
        whole "rounds to 100 but isn't full" band into 99.
    """
    pct = int(pct)
    max_hp = int(max_hp)
    if max_hp <= 0:
        return (0, 0)
    if pct <= 0:
        return (0, 0)
    if pct >= 100:
        return (max_hp, max_hp)
    lo = ((pct - 1) * max_hp) // 100 + 1
    hi = (pct * max_hp) // 100
    if pct == 99:
        hi = max_hp - 1
    lo = max(1, min(lo, max_hp))
    hi = max(lo, min(hi, max_hp))
    return (lo, hi)


def estimate_from_display(pct, max_hp) -> int:
    """Point estimate for a `pct/100` display: the MIDPOINT of the band.

    The old estimate was the band's TOP edge -- `pkmn.hp = max_hp * pct` kept as
    a float and truncated by `int()` at the engine boundary is exactly
    `floor(pct * max_hp / 100)` -- so its error was one-sided, `[0, band)`, and
    the checker's `round(max_hp * hp_frac)` variant could even land OUTSIDE the
    band (`round(310 * 0.25) = 78` for a Slowking the protocol had pinned to
    [75, 77]: APPROXIMATIONS U3's hard finding, an HP the protocol had ruled
    out).  The midpoint is unbiased and halves the worst-case error to
    +-band/2, and the band is derived from PS's own display formula rather than
    assumed.

    MEASURED, not assumed better (829 corpus games containing Endeavor, wheel
    0.0.55): the top edge leaves 3 of U3's 18 soft members alive (synth11861
    T12, synth32995 T2, synth13267 T17 -- all "estimate is 1 too high, so the
    modelled Endeavor deals 1-2 where PS was immune"), while the midpoint takes
    the class to ZERO.  Its one cost in that set is one NEW soft
    (synth34536 T9, a Disable expiry that reproduces at Barraskewda hp 73 but
    not 72 -- the protocol's `32/100` on a 231 max HP admits both, so that turn
    is unassertable at reconstruction precision either way; the estimator only
    chooses which side of an ambiguity it sits on).
    """
    lo, hi = display_bounds(pct, max_hp)
    if hi <= lo:
        return lo
    return (lo + hi) // 2


# ---------------------------------------------------------------------------
# per-pokemon state
# ---------------------------------------------------------------------------
# `hp_exact`         - hp is the true integer HP (given max_hp is the true max HP)
# `hp_display_pct`   - the last percent display seen for this mon, or None
# `hp_display_hp`    - the hp value that display produced; the pct is only reused
#                      (e.g. when the sidecar corrects max_hp) while hp still
#                      equals it, i.e. while nothing else has moved the HP since.
# `hp_certificate_pending` - name of a fixed-damage move whose landing certifies
#                      this mon's HP, armed by its |move| line and consumed by
#                      the bare `-damage` that move produces.
# `hp_certificate_pct` - the pct the certifying line showed, held until the
#                      consistency check can be run against an exact max HP.
# `hp_certificate_hp`  - the hp that pct described.  The deferred check is only
#                      meaningful while the HP has not moved since; anything that
#                      changes it (a Regenerator heal on switch-out, say) makes
#                      the remembered pct describe a value that no longer exists.
# `hp_certificate_fraction` - (num, den) when the certified value is a FUNCTION of
#                      max HP (`hp == max_hp`, a revive's `max_hp // 2`) rather
#                      than an absolute integer.  Such a certificate has to be
#                      RE-EVALUATED when max HP stops being a guess -- otherwise
#                      the correction to max_hp silently invalidates it.
# `hp_certificate_halvings` - number of Super Fang-family halvings
#                      (`hp -= max(1, hp // 2)`) applied ON TOP of the fraction.
#                      A halving of a max-RELATIVE value is still a function of
#                      max HP -- `ceil(max/2)` after a full-HP Rest is NOT
#                      `(max*1)//2` and not any (num, den) under floor division
#                      -- so the derivation is kept as (fraction, halvings) and
#                      replayed against the true max when it lands.  Freezing
#                      the halved value as an absolute integer computed off the
#                      GUESSED max was gate-5 B5's defect: Terapagos rests to
#                      100/100 (hp := guessed 266), Ruination halves it to 133,
#                      and the deferred check against the true max 273 refuses a
#                      certificate whose honest value was 273 -> 137.
# `max_hp_exact`     - max_hp is the mon's TRUE max HP (our own side, always;
#                      the opponent only once a full-knowledge sidecar lands)
#                      rather than a randbats-spread guess.


def init_pokemon(pkmn) -> None:
    """Fresh pokemon are at full HP, which is exact by construction (and is a
    max-HP-RELATIVE certificate: it survives a correction to max HP).  Max HP
    itself starts as a computed guess -- authoritative sources say so."""
    pkmn.hp_exact = True
    pkmn.hp_display_pct = None
    pkmn.hp_display_hp = None
    pkmn.hp_certificate_pending = None
    pkmn.hp_certificate_pct = None
    pkmn.hp_certificate_hp = pkmn.hp
    pkmn.hp_certificate_fraction = (1, 1)
    pkmn.hp_certificate_halvings = 0
    pkmn.max_hp_exact = False


def is_exact(pkmn) -> bool:
    return bool(getattr(pkmn, "hp_exact", False))


def is_absolute_exact(pkmn) -> bool:
    """Exact as an ABSOLUTE integer: exact, and either checked against a true
    max HP or not a function of max HP at all.  A max-RELATIVE certificate on a
    guessed max (`hp == guessed_max` after a Rest) is exact only in shape --
    its integer value moves when the guess is corrected -- so it must never be
    copied onto another pokemon as a bare number."""
    if not is_exact(pkmn):
        return False
    if bool(getattr(pkmn, "max_hp_exact", False)):
        return True
    return getattr(pkmn, "hp_certificate_fraction", None) is None


def _halve(hp: int) -> int:
    """One Super Fang / Nature's Madness / Ruination application:
    `hp -= max(1, floor(hp/2))` (PS data/moves.ts:18459-18461, 12634-12636,
    15526-15528)."""
    return hp - max(1, hp // 2)


def _from_fraction(max_hp: int, fraction, halvings: int) -> int:
    hp = (max_hp * int(fraction[0])) // int(fraction[1])
    for _ in range(int(halvings or 0)):
        hp = _halve(hp)
    return hp


def _clear_deferred(pkmn) -> None:
    pkmn.hp_certificate_pct = None
    pkmn.hp_certificate_hp = None
    pkmn.hp_certificate_fraction = None
    pkmn.hp_certificate_halvings = 0


def clear(pkmn, reason: str = "") -> None:
    """Drop a certificate (switch-out, or any untracked HP change)."""
    if getattr(pkmn, "hp_exact", False) and reason:
        logger.info(
            "dropping exact-hp certificate on {} ({})".format(
                getattr(pkmn, "name", "?"), reason
            )
        )
    pkmn.hp_exact = False
    _clear_deferred(pkmn)


def set_exact(pkmn, hp) -> None:
    """Pin an exactly-known HP with no display to check it against (user side,
    faints, and the exact `hp/maxhp` condition strings)."""
    pkmn.hp = int(hp)
    pkmn.hp_exact = True
    _clear_deferred(pkmn)


def apply_display(pkmn, pct) -> None:
    """Fold a `pct/100` opponent display into the reconstruction.

    Replaces hp with the interval-clamped point estimate and re-derives
    exactness from the display alone: a display is only exact when it pins a
    single value (pct == 100, a fainted mon, or a max_hp small enough that the
    band is a singleton).  Any certificate the mon was carrying ends here -- a
    new display means the HP moved by an amount the percent protocol does not
    state.  A certificate belonging to THIS event is applied afterwards by
    `certify`, which is why certifying handlers call this first."""
    pct = int(pct)
    max_hp = int(getattr(pkmn, "max_hp", 0) or 0)
    if max_hp <= 0:
        pkmn.hp = 0
        pkmn.hp_exact = False
        _clear_deferred(pkmn)
        return
    lo, hi = display_bounds(pct, max_hp)
    pkmn.hp = estimate_from_display(pct, max_hp)
    pkmn.hp_exact = lo == hi
    _clear_deferred(pkmn)
    if pct >= 100:
        # `100/100` is the one display PS never rounds INTO (sim/pokemon.ts:2083
        # forces 99 whenever hp < maxhp), so it states `hp == max_hp` -- a
        # max-HP-relative identity that must follow a later max-HP correction.
        pkmn.hp_certificate_fraction = (1, 1)
        pkmn.hp_certificate_halvings = 0
        pkmn.hp_certificate_hp = pkmn.hp
    pkmn.hp_display_pct = pct
    pkmn.hp_display_hp = pkmn.hp


def display_contains(pkmn, pct, hp) -> bool:
    lo, hi = display_bounds(pct, getattr(pkmn, "max_hp", 0) or 0)
    return lo <= int(hp) <= hi


def _refuse(pkmn, reason: str, hp: int, shown_pct: int) -> None:
    max_hp = int(getattr(pkmn, "max_hp", 0) or 0)
    CERTIFICATE_REFUSALS.append(
        {
            "pokemon": getattr(pkmn, "name", "?"),
            "reason": reason,
            "certified_hp": hp,
            "max_hp": max_hp,
            "shown_pct": int(shown_pct),
            "implied_pct": display_pct(hp, max_hp),
            "game": CONTEXT.get("game"),
            "turn": CONTEXT.get("turn"),
        }
    )
    logger.error(
        "REFUSING exact-hp certificate on %s (%s): certified hp %d/%d implies "
        "%d/100 but the protocol showed %d/100 -- certification chain broken "
        "(game %s turn %s)",
        getattr(pkmn, "name", "?"),
        reason,
        hp,
        max_hp,
        display_pct(hp, max_hp),
        int(shown_pct),
        CONTEXT.get("game"),
        CONTEXT.get("turn"),
    )


def certify(pkmn, hp, reason: str, shown_pct=None, fraction=None, halvings=0) -> bool:
    """Pin an exact HP stated by a protocol IDENTITY.

    `shown_pct` is the percent the certifying line itself carried.  It is
    checked immediately when this mon's max HP is exact, and otherwise
    remembered for `verify_against_exact_max` -- see the module docstring on why
    checking against a GUESSED max HP would indict the wrong party.

    `fraction` is `(num, den)` when the identity is a FUNCTION of max HP rather
    than an absolute integer (a revive's `max_hp // 2`), and `halvings` the
    number of Super Fang-family applications layered on top of it (see the
    `hp_certificate_halvings` note above).  Such a certificate is re-evaluated
    -- the whole derivation replayed -- not just re-checked, once max HP is
    exact."""
    max_hp = int(getattr(pkmn, "max_hp", 0) or 0)
    if max_hp <= 0:
        return False
    if fraction is not None:
        hp = _from_fraction(max_hp, fraction, halvings)
    hp = max(0, min(int(hp), max_hp))
    max_hp_is_exact = bool(getattr(pkmn, "max_hp_exact", False))
    if shown_pct is not None and max_hp_is_exact:
        if display_pct(hp, max_hp) != int(shown_pct):
            _refuse(pkmn, reason, hp, shown_pct)
            return False
    logger.info(
        "exact-hp certificate on {}: {}/{} ({}{})".format(
            getattr(pkmn, "name", "?"),
            hp,
            max_hp,
            reason,
            "" if max_hp_is_exact else ", unverified: max_hp is a guess",
        )
    )
    pkmn.hp = hp
    pkmn.hp_exact = True
    _clear_deferred(pkmn)
    pkmn.hp_certificate_fraction = fraction
    pkmn.hp_certificate_halvings = int(halvings or 0) if fraction is not None else 0
    pkmn.hp_certificate_hp = hp
    if not max_hp_is_exact:
        pkmn.hp_certificate_pct = None if shown_pct is None else int(shown_pct)
    return True


def refuse_against_bound(pkmn, bound, reason: str) -> None:
    """Drop a certificate that contradicts an INEQUALITY the protocol certified
    outright (e.g. Substitute's `[weak]` fail: `hp <= max_hp // 4`).

    Two protocol statements disagreeing is a broken chain in exactly the sense
    `_refuse` means, so it is counted the same way rather than being settled
    silently in either direction."""
    max_hp = int(getattr(pkmn, "max_hp", 0) or 0)
    CERTIFICATE_REFUSALS.append(
        {
            "pokemon": getattr(pkmn, "name", "?"),
            "reason": reason,
            "certified_hp": int(pkmn.hp),
            "max_hp": max_hp,
            "bound": int(bound),
            "game": CONTEXT.get("game"),
            "turn": CONTEXT.get("turn"),
        }
    )
    logger.error(
        "REFUSING exact-hp certificate on %s: certified hp %d/%d contradicts the "
        "protocol's own bound hp <= %d (%s) -- certification chain broken",
        getattr(pkmn, "name", "?"),
        int(pkmn.hp),
        max_hp,
        int(bound),
        reason,
    )
    clear(pkmn)


def verify_against_exact_max(pkmn) -> bool:
    """Settle a certificate taken while max HP was a guess, now that it is exact.

    Two things happen here, in order:

    1. a max-HP-RELATIVE certificate is RE-EVALUATED against the true max HP
       (a revive certified as `248 // 2 = 124` becomes `247 // 2 = 123`);
    2. the remembered display pct is checked against the result.

    Both steps need the HP to still BE the certified value.  If it has MOVED
    since -- a Regenerator heal on switch-out, or any write that bypassed this
    module -- the certificate describes a value that no longer exists: neither
    re-evaluating the fraction nor checking the pct means anything, and whether
    the current HP is exact is simply unknown.  That is a loss of provenance,
    not a contradiction, so it drops exactness quietly rather than being counted
    as a refusal -- but it does drop it, because keeping an `hp_exact` flag on a
    value nothing vouches for is the failure this module exists to prevent.

    Returns False whenever the certificate did not survive -- on a genuine
    contradiction (counted) or on staleness (not counted) -- so the caller falls
    back to the ordinary estimator either way."""
    frac = getattr(pkmn, "hp_certificate_fraction", None)
    halvings = getattr(pkmn, "hp_certificate_halvings", 0) or 0
    pct = getattr(pkmn, "hp_certificate_pct", None)
    certified_hp = getattr(pkmn, "hp_certificate_hp", None)
    if not is_exact(pkmn):
        _clear_deferred(pkmn)
        return True
    max_hp = int(getattr(pkmn, "max_hp", 0) or 0)
    if certified_hp is not None and int(pkmn.hp) != int(certified_hp):
        logger.info(
            "exact-hp certificate on {} is stale (hp moved {} -> {} since it was "
            "taken); dropping it".format(
                getattr(pkmn, "name", "?"), certified_hp, pkmn.hp
            )
        )
        clear(pkmn)
        return False
    if frac is not None and max_hp > 0:
        pkmn.hp = max(0, min(_from_fraction(max_hp, frac, halvings), max_hp))
    _clear_deferred(pkmn)
    pkmn.hp_certificate_fraction = frac
    pkmn.hp_certificate_halvings = int(halvings) if frac is not None else 0
    if pct is None:
        return True
    if display_pct(pkmn.hp, max_hp) == int(pct):
        return True
    _refuse(pkmn, "deferred check against exact max_hp", int(pkmn.hp), int(pct))
    pkmn.hp_exact = False
    pkmn.hp_certificate_fraction = None
    pkmn.hp_certificate_halvings = 0
    return False


# ---------------------------------------------------------------------------
# fixed-damage certificates armed on a |move| line, consumed by its `-damage`
# ---------------------------------------------------------------------------
# Endeavor:  target.hp := attacker.hp  (PS data/moves.ts:4789-4791).  The
#   identity is symmetric, so whichever side is exact certifies the other.
# Super Fang / Nature's Madness / Ruination:  damage := max(1, floor(hp/2))
#   (PS data/moves.ts:18459-18461, 12634-12636, 15526-15528), so the result is
#   exact only when the PRIOR hp was; with an unknown prior these merely halve
#   the interval and are not propagated (see the module docstring).
CERTIFYING_FIXED_DAMAGE_MOVES = frozenset(
    ("endeavor", "superfang", "naturesmadness", "ruination")
)


def arm(target, move_name: str) -> None:
    if move_name in CERTIFYING_FIXED_DAMAGE_MOVES:
        target.hp_certificate_pending = move_name
    else:
        target.hp_certificate_pending = None


def disarm(pkmn) -> None:
    pkmn.hp_certificate_pending = None


def consume(
    target,
    attacker,
    prior_hp,
    prior_exact: bool,
    shown_pct=None,
    prior_fraction=None,
    prior_halvings=0,
) -> bool:
    """Resolve a pending fixed-damage certificate on the bare `-damage` line the
    move produced.  Returns True when a certificate was pinned.

    `prior_hp`/`prior_exact` describe the TARGET immediately before this line
    (the caller captures them before folding in the display), and
    `prior_fraction`/`prior_halvings` its max-HP-relative derivation when the
    prior exactness was a FUNCTION of a guessed max (see
    `hp_certificate_halvings` above) -- the display fold clears those fields, so
    only the caller can still see them.  `shown_pct` is the percent this line
    carried, or None for the USER side -- whose `-damage` already gives exact
    `hp/maxhp`, and where the interesting direction is the reverse one: our
    exact post-Endeavor HP certifies the OPPONENT attacker's."""
    pending = getattr(target, "hp_certificate_pending", None)
    target.hp_certificate_pending = None
    if not pending:
        return False

    if pending == "endeavor":
        # target.hp == attacker.hp -- symmetric, so knowledge flows either way.
        # Only an ABSOLUTE-exact side may donate: a max-relative certificate's
        # integer is a function of ITS OWN guessed max, and copying it across
        # pokemon severs it from the max it would be re-evaluated against.
        if attacker is None:
            return False
        if is_absolute_exact(attacker):
            return certify(
                target, attacker.hp, "endeavor (target.hp := attacker.hp)", shown_pct
            )
        if is_absolute_exact(target):
            return certify(attacker, target.hp, "endeavor (attacker.hp := target.hp)")
        return False

    # Super Fang family: exact out only if exact in.
    if not prior_exact:
        return False
    prior_hp = int(prior_hp)
    new_hp = _halve(prior_hp)
    if new_hp <= 0:
        return False
    if prior_fraction is not None and not bool(getattr(target, "max_hp_exact", False)):
        # The prior was exact only as a function of a GUESSED max HP (a full-HP
        # Rest, a revive).  The halved value is still such a function --
        # `ceil(max/2)` is not any (num, den) under floor division -- so the
        # derivation is carried as (fraction, halvings) and replayed against
        # the true max in `verify_against_exact_max`, instead of freezing an
        # integer computed off the guess (gate-5 B5: 266//2's 133 refused
        # against the true 273 -> 137).
        return certify(
            target,
            None,
            "{} (hp -= max(1, hp//2), max-relative)".format(pending),
            shown_pct,
            fraction=prior_fraction,
            halvings=int(prior_halvings or 0) + 1,
        )
    return certify(target, new_hp, "{} (hp -= max(1, hp//2))".format(pending), shown_pct)
