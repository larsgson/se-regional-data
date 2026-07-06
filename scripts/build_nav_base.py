#!/usr/bin/env python3
"""
Regenerate a DRAFT English nav-base set from all cached SAB deployments.

For every nav key seen across data/.media-cache/*.js (+ /tmp/ind_manifest.js if
present), pick the MAJORITY English value. Writes (both gitignored, throwaway):

  data/_pool/_app/nav-base.json          all keys -> majority English string
  data/_pool/_app/nav-base.choices.txt   the keys where deployments disagreed

NOTE: the CURATED, canonical set is conf/nav-base.json (git-tracked, hand-edited
— e.g. sentence-case fixes on a few keys). This script only produces a draft to
diff against; it does NOT write conf/nav-base.json and will never clobber the
curated file. The pipeline ships conf/nav-base.json to the CDN as
pkf/_app/nav-base.json. No network, stdlib only.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from collections import Counter, defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(REPO, "data", ".media-cache")
IND = "/tmp/ind_manifest.js"
OUT_DIR = os.path.join(REPO, "data", "_pool", "_app")

KEY_ENTRY = re.compile(r'([A-Za-z_][A-Za-z0-9_-]*)\s*:\s*\{([^{}]*)\}')
EN = re.compile(r'\ben:"((?:[^"\\]|\\.)*)"')
DEFAULT = re.compile(r'\bdefault:"((?:[^"\\]|\\.)*)"')


def mappings_block(js: str) -> str:
    m = re.search(r'translationMappings:\{', js)
    if not m:
        return ""
    i = js.find("mappings:{", m.start())
    if i < 0:
        return ""
    i = js.find("{", i)
    depth = 0
    for j in range(i, len(js)):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                return js[i:j + 1]
    return ""


def english_map(js: str) -> dict:
    blk = mappings_block(js)
    out = {}
    for m in KEY_ENTRY.finditer(blk):
        key, body = m.group(1), m.group(2)
        v = EN.search(body) or DEFAULT.search(body)
        if v:
            # unescape the JS string literal
            try:
                out[key] = json.loads('"' + v.group(1) + '"')
            except json.JSONDecodeError:
                out[key] = v.group(1)
    return out


def main() -> int:
    files = sorted(glob.glob(os.path.join(CACHE, "*.js")))
    srcs = [(os.path.basename(f).split(".")[0], f) for f in files]
    if os.path.exists(IND):
        srcs.append(("ind", IND))

    values: dict[str, Counter] = defaultdict(Counter)
    present: Counter = Counter()
    n = 0
    for iso, path in srcs:
        em = english_map(open(path, encoding="utf-8", errors="replace").read())
        if not em:
            continue
        n += 1
        for k, v in em.items():
            values[k][v] += 1
            present[k] += 1

    base = {}
    choices = []
    for k in sorted(values):
        c = values[k]
        pick, pick_n = c.most_common(1)[0]
        base[k] = pick
        if len(c) > 1:
            choices.append((k, present[k], c))

    os.makedirs(OUT_DIR, exist_ok=True)
    base_path = os.path.join(OUT_DIR, "nav-base.json")
    with open(base_path, "w", encoding="utf-8") as f:
        json.dump(base, f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")

    choices_path = os.path.join(OUT_DIR, "nav-base.choices.txt")
    with open(choices_path, "w", encoding="utf-8") as f:
        f.write(f"# Keys where deployments disagreed — the pick is the MAJORITY value.\n")
        f.write(f"# Edit data/_pool/_app/nav-base.json directly to override any pick.\n")
        f.write(f"# Built from {n} deployments; {len(choices)} keys had a choice.\n\n")
        for k, seen, c in choices:
            f.write(f"{k}  ({seen}/{n} deployments)\n")
            for val, cnt in c.most_common():
                mark = "  <- PICKED" if val == base[k] else ""
                f.write(f'    {cnt:>4}x  "{val}"{mark}\n')
            f.write("\n")

    print(f"deployments: {n}")
    print(f"keys written to nav-base.json: {len(base)}")
    print(f"keys needing a choice (in nav-base.choices.txt): {len(choices)}")
    print(f"\nwrote:\n  {base_path}\n  {choices_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
