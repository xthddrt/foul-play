"""Pre-build a symbol index over pokemon-showdown data/ and poke-engine/src/.

Audit agents were spending ~50 sequential greps each just LOCATING code
(measured: 893 of 972 tool calls in the 9-mechanic wave were Bash, 52% into
engine source, 24% into PS source). One index lookup replaces that hunt.

Output: analysis/symbol_index.json
  ps[<id>]        -> {"file":..., "start":L, "end":L}   whole handler block
  engine[<sym>]   -> ["src/genx/x.rs:123", ...]          every mention site

Regenerate after pulling either repo:
  python3 foul-play/tools/conformance/build_symbol_index.py
"""
import json
import os
import re

ROOT = "/Users/sallyliu/pokemon-fast-bot"
PS_FILES = [
    "pokemon-showdown/data/moves.ts",
    "pokemon-showdown/data/abilities.ts",
    "pokemon-showdown/data/items.ts",
    "pokemon-showdown/data/conditions.ts",
]
ENGINE_DIR = "poke-engine/src"
OUT = os.path.join(ROOT, "ladder-games/analysis/symbol_index.json")

# a top-level data entry: exactly one tab of indent, `id: {`
ENTRY = re.compile(r"^\t(\w+): \{\s*$")
# engine symbols worth indexing
SYM = re.compile(r"\b(?:Choices|Abilities|Items|PokemonVolatileStatus|PokemonStatus"
                 r"|PokemonType|Terrain|Weather)::([A-Z][A-Z0-9_]*)\b")
FN = re.compile(r"^\s*(?:pub(?:\(crate\))?\s+)?fn\s+(\w+)")


def index_ps():
    out = {}
    for rel in PS_FILES:
        path = os.path.join(ROOT, rel)
        if not os.path.exists(path):
            continue
        lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
        i = 0
        while i < len(lines):
            m = ENTRY.match(lines[i])
            if m:
                start = i + 1
                depth = 0
                j = i
                while j < len(lines):
                    depth += lines[j].count("{") - lines[j].count("}")
                    if depth <= 0 and j > i:
                        break
                    j += 1
                out.setdefault(m.group(1), []).append(
                    {"file": rel, "start": start, "end": j + 1}
                )
                i = j
            i += 1
    return out


def index_engine():
    syms, fns = {}, {}
    base = os.path.join(ROOT, ENGINE_DIR)
    for dirpath, _, files in os.walk(base):
        for f in files:
            if not f.endswith(".rs"):
                continue
            path = os.path.join(dirpath, f)
            rel = os.path.relpath(path, ROOT)
            for n, line in enumerate(
                open(path, encoding="utf-8", errors="replace"), 1
            ):
                for s in set(SYM.findall(line)):
                    syms.setdefault(s, []).append(f"{rel}:{n}")
                fm = FN.match(line)
                if fm:
                    fns.setdefault(fm.group(1), []).append(f"{rel}:{n}")
    # cap runaway lists (a few enum names appear thousands of times)
    for d in (syms, fns):
        for k, v in d.items():
            if len(v) > 60:
                d[k] = v[:60] + [f"...({len(v)} total)"]
    return syms, fns


def main():
    ps = index_ps()
    syms, fns = index_engine()
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump({"ps": ps, "engine_symbols": syms, "engine_fns": fns},
              open(OUT, "w"), indent=0)
    print(f"PS entries indexed:      {len(ps)}")
    print(f"engine symbols indexed:  {len(syms)}")
    print(f"engine functions:        {len(fns)}")
    print(f"-> {OUT} ({os.path.getsize(OUT)/1024:.0f} KB)")


if __name__ == "__main__":
    main()
