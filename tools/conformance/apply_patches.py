"""Apply diagnosed patches serially with exact-string replacement.

Refuses to apply if old_string is absent or non-unique — a silent mismatch
would corrupt a file, and these patches were authored against a snapshot.
"""

import json
import sys

patches_file = sys.argv[1]
diagnoses = json.load(open(patches_file))

applied, failed = 0, []
for d in diagnoses:
    for p in d["patch"]:
        path, old, new = p["file"], p["old_string"], p["new_string"]
        try:
            src = open(path).read()
        except Exception as e:
            failed.append((path, f"unreadable: {e}"))
            continue
        n = src.count(old)
        if n != 1:
            failed.append((path, f"old_string occurs {n}x (need exactly 1): {old[:70]!r}"))
            continue
        open(path, "w").write(src.replace(old, new, 1))
        applied += 1
        print(f"APPLIED  {path.split('/')[-1]}: {p['rationale'][:90]}")

print(f"\napplied={applied} failed={len(failed)}")
for path, why in failed:
    print(f"FAILED   {path.split('/')[-1]}: {why}")
sys.exit(1 if failed else 0)
