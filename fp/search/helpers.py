import logging

import constants
from data.pkmn_sets import PredictedPokemonSet
from fp.battle import Pokemon

logger = logging.getLogger(__name__)


def log_pkmn_set(pkmn: Pokemon, source=None):
    nature_evs = f"{pkmn.nature},{','.join(str(x) for x in pkmn.evs)}"
    if nature_evs in [
        "serious,85,85,85,85,85,85",
        "serious,252,252,252,252,252,252",
        "serious,11,11,11,11,11,11",
    ]:
        s = "\t{} {} {} {}".format(
            pkmn.name.rjust(15),
            str(pkmn.ability).rjust(12),
            str(pkmn.item).rjust(12),
            pkmn.moves,
        )
    else:
        s = "\t{} {} {} {} {}".format(
            pkmn.name.rjust(15),
            nature_evs.rjust(25),
            str(pkmn.ability).rjust(12),
            str(pkmn.item).rjust(12),
            pkmn.moves,
        )
    if pkmn.tera_type is not None and pkmn.tera_type not in ["nothing", "typeless"]:
        s += " ttype={}".format(pkmn.tera_type)
    if source is not None:
        s += " source={}".format(source)

    logger.info(s)


def populate_pkmn_from_set(
    pkmn: Pokemon, set_: PredictedPokemonSet, source: str = None
):
    known_pokemon_moves = pkmn.moves

    pkmn.moves = []
    for mv in set_.pkmn_moveset.moves:
        pkmn.add_move(mv)
    pkmn.ability = pkmn.ability or set_.pkmn_set.ability
    if pkmn.item == constants.UNKNOWN_ITEM:
        pkmn.item = set_.pkmn_set.item
    pkmn.set_spread(
        set_.pkmn_set.nature,
        ",".join(str(x) for x in set_.pkmn_set.evs),
        ivs=",".join(str(x) for x in getattr(set_.pkmn_set, "ivs", (31,) * 6)),
    )
    if (
        set_.pkmn_set.tera_type is not None
        and not pkmn.terastallized
        and not pkmn.tera_type
    ):
        pkmn.tera_type = set_.pkmn_set.tera_type
    log_pkmn_set(pkmn, source)

    # newly created moves have max PP and are NOT disabled; carry both pieces of
    # live per-move state over from the moves the protocol actually revealed.
    #
    # `disabled` matters as much as `current_pp`: Disable (and Cursed Body) mark
    # one specific opponent move disabled in the protocol parser
    # (fp/battle_modifier.py:1810-1817 `mv.disabled = True`), and re-populating
    # `pkmn.moves` from a sampled set here rebuilt every Move object with
    # `disabled = False` (fp/battle.py:981), so EVERY sampled world silently
    # forgot the Disable and let the search pick a move PS would reject.  The
    # `disable` volatile itself survives the deepcopy, so only this flag was
    # lost -- which also made the volatile's duration meaningless.
    disabled_names = {m.name for m in known_pokemon_moves if m.disabled}
    for known_move in known_pokemon_moves:
        for mv in pkmn.moves:
            if known_move.name.startswith("hiddenpower") and mv.name.startswith(
                "hiddenpower"
            ):
                mv.current_pp = known_move.current_pp
                mv.disabled = mv.disabled or known_move.disabled
                break
            elif mv.name == known_move.name:
                mv.current_pp = known_move.current_pp
                mv.disabled = mv.disabled or known_move.disabled
                break
    # a disabled move the sampled set does not contain would otherwise vanish
    # from the world entirely; keep it, still disabled, so the state stays
    # consistent with the protocol (`add_move` is a no-op past 4 moves is NOT
    # true, so guard the slot count explicitly)
    for name in disabled_names:
        if not any(mv.name == name for mv in pkmn.moves) and len(pkmn.moves) < 4:
            pkmn.add_move(name)
            pkmn.moves[-1].disabled = True
