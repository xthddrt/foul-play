"""Play from the true-state net: one forward pass, no search, no world sampling.

This is the inference half of `fp/infostate.py`. That module already turns a
LIVE battle into the exact row the net was trained on (it is what emitted the
training corpus), so serialization is proven against real games. What was
missing is the inverse: turning the net's chosen slot back into the decision
string `format_decision` expects, which is the same contract `find_best_move`
satisfies:

    "<move>"            a move             (slots 0-3)
    "<move>-tera"       terastallise       (slots 4-7)
    "switch <name>"     a switch           (slots 8-12)
    "struggle"          struggle           (slot 13)
    "No Move"           recharge           (slot 14)

The whole point of the model is that it consumes the TRUE information state --
unrevealed opponents stay unknown -- so there is no determinization here and
nothing to search. `pick_move` is a single forward pass.

Enable with `--truestate-net <ckpt.pt>`; without it nothing in this file runs.
"""

import os
import sys

from fp import infostate

_NET = None
_VOCAB = None
_TORCH = None
_ENCODE = None


def _lazy_import():
    """Import torch + the truestate package only when the flag is used.

    foul-play's normal MCTS path must not pay a torch import, and the ladder
    fleet's venv is not guaranteed to have it.
    """
    global _TORCH, _ENCODE, _VOCAB
    if _TORCH is not None:
        return
    import torch

    ts_dir = os.environ.get(
        "TRUESTATE_DIR", "/Users/sallyliu/pokemon-fast-bot/truestate"
    )
    if ts_dir not in sys.path:
        sys.path.insert(0, ts_dir)
    from tensorize import encode_row, load_vocab

    _TORCH, _ENCODE, _VOCAB = torch, encode_row, load_vocab()


def load(ckpt_path):
    """Load the checkpoint once; subsequent calls are free."""
    global _NET
    if _NET is not None:
        return _NET
    _lazy_import()
    from model import TrueStateNet

    blob = _TORCH.load(ckpt_path, map_location="cpu", weights_only=False)
    state = blob.get("model", blob.get("state_dict", blob))
    net = TrueStateNet()
    net.load_state_dict(state)
    net.eval()
    # one thread: a 3.4M-param net over 13 tokens is dispatch-bound, and at
    # batch size 1 extra threads only add synchronisation (measured: 4->8
    # threads changed throughput by 0.8%)
    _TORCH.set_num_threads(1)
    _NET = net
    return _NET


def _slot_to_decision(battle, slot, info):
    """Inverse of infostate.action_to_slot -> the decision string."""
    if slot == infostate.STRUGGLE_SLOT:
        return "struggle"
    if slot == infostate.RECHARGE_SLOT:
        # format_decision answers this from the request's forced move
        return "No Move"

    if 0 <= slot < 8:
        base = slot % 4
        tera = slot >= 4
        # move_slots is name -> index; invert for this active only
        for name, idx in (info.get("move_slots") or {}).items():
            if idx == base:
                return "{}-tera".format(name) if tera else name
        return None

    for party_index, s in (info.get("switch_slots") or {}).items():
        if s != slot:
            continue
        for pkmn in battle.user.reserve:
            if getattr(pkmn, "index", None) == party_index:
                return "{} {}".format(infostate.constants.SWITCH_STRING, pkmn.name)
        return None
    return None


def pick_move(battle, ckpt_path, temperature=0.0):
    """Return a decision string chosen by the net, or None to fall back.

    Returning None (rather than raising or guessing) is deliberate: every
    caller already has the search path available, and a bad decode should
    degrade to the champion bot, never to an illegal command.

    `temperature` > 0 samples from softmax(masked logits / T) instead of taking
    the argmax. Deploy stays at 0; RL generation runs at 1.0, because a policy
    gradient can only learn from actions the policy actually varies over -- an
    argmax corpus has zero exploration and the update degenerates.
    """
    net = load(ckpt_path)
    info = infostate.build_legal_mask(battle)
    mask = info["mask"]
    if not any(mask):
        return None

    row = infostate.serialize_row(
        battle, infostate.request_type(battle), None, info
    )
    sample = _ENCODE(row, _VOCAB)
    batch = {k: v.unsqueeze(0) for k, v in sample.items()}

    with _TORCH.no_grad():
        out = net(
            batch["ids"], batch["num"], batch["ttype"],
            batch["active_us"], batch["legal"],
        )
        logits = out["policy"][0]

    # trust the request-derived mask over the row's copy of it
    neg = _TORCH.full_like(logits, float("-inf"))
    legal = _TORCH.tensor(mask, dtype=_TORCH.bool)
    masked = _TORCH.where(legal, logits, neg)
    if temperature and temperature > 0:
        probs = _TORCH.softmax(masked / temperature, dim=-1)
        slot = int(_TORCH.multinomial(probs, 1))
    else:
        slot = int(masked.argmax())

    if not mask[slot]:
        return None
    return _slot_to_decision(battle, slot, info)


def value_estimate(battle):
    """P(win) for the current true state -- for logging/analysis, not play."""
    if _NET is None:
        return None
    info = infostate.build_legal_mask(battle)
    row = infostate.serialize_row(
        battle, infostate.request_type(battle), None, info
    )
    sample = _ENCODE(row, _VOCAB)
    batch = {k: v.unsqueeze(0) for k, v in sample.items()}
    with _TORCH.no_grad():
        out = _NET(
            batch["ids"], batch["num"], batch["ttype"],
            batch["active_us"], batch["legal"],
        )
        return float(_TORCH.sigmoid(out["value"][0]))
