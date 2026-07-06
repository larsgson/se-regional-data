#!/usr/bin/env python3
"""
Probe: is a single common ENGLISH nav-string set usable for all deployments,
or is a per-deployment delta actually needed?

Reads every cached SAB manifest chunk (data/.media-cache/*.js) plus, if present,
/tmp/ind_manifest.js (the direct 'ind' source), extracts the ENGLISH value of
each translationMappings key (en:, else default:), and reports EXACTLY where
deployments disagree.

Output: for every key whose English value is not identical across all
deployments that have it, list each distinct value and how many deployments use
it. Plus key-presence variance and summary stats.

No network, stdlib only.
    python3 scripts/probe_nav_delta.py
    python3 scripts/probe_nav_delta.py --min 2   # only show keys with >= N variants
"""
from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from collections import Counter, defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(REPO, "data", ".media-cache")
IND = "/tmp/ind_manifest.js"

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
    """key -> English string (en:, else default:)."""
    blk = mappings_block(js)
    out = {}
    for m in KEY_ENTRY.finditer(blk):
        key, body = m.group(1), m.group(2)
        v = EN.search(body) or DEFAULT.search(body)
        if v:
            out[key] = v.group(1)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=int, default=2,
                    help="show keys with at least N distinct English values (default 2)")
    args = ap.parse_args()

    sources = sorted(glob.glob(os.path.join(CACHE, "*.js")))
    files = [(os.path.basename(f).split(".")[0], f) for f in sources]
    if os.path.exists(IND):
        files.append(("ind", IND))

    # key -> Counter(english_value -> num deployments)
    values: dict[str, Counter] = defaultdict(Counter)
    key_present: Counter = Counter()      # key -> how many deployments have it
    per_iso: dict[str, dict] = {}
    n = 0
    for iso, path in files:
        js = open(path, encoding="utf-8", errors="replace").read()
        em = english_map(js)
        if not em:
            continue
        n += 1
        per_iso[iso] = em
        for k, v in em.items():
            values[k][v] += 1
            key_present[k] += 1

    print(f"deployments analysed (with nav data): {n}")
    print(f"distinct nav keys seen:               {len(values)}")

    stable = [k for k, c in values.items() if len(c) == 1]
    variant = {k: c for k, c in values.items() if len(c) >= args.min}
    print(f"keys with ONE English value everywhere it appears: {len(stable)}")
    print(f"keys with >= {args.min} distinct English values:        {len(variant)}")

    # key-presence variance
    universal = [k for k, c in key_present.items() if c == n]
    partial = {k: c for k, c in key_present.items() if c < n}
    print(f"keys present in ALL {n} deployments:  {len(universal)}")
    print(f"keys present in only SOME:            {len(partial)}")

    print("\n" + "=" * 78)
    print(f"EXACT DELTAS — every key whose English value is not unanimous")
    print("=" * 78)
    if not variant:
        print("  (none — a single common English set covers every deployment)")
    for k in sorted(variant):
        c = variant[k]
        total = sum(c.values())
        print(f"\n{k}  ({key_present[k]}/{n} deployments have it; {len(c)} distinct values)")
        for val, cnt in c.most_common():
            print(f'    {cnt:>4}x  "{val}"')

    # cost if we adopt the majority value per key as the single canonical set
    overrides = 0
    deviating_deployments = set()
    for k, c in values.items():
        if len(c) == 1:
            continue
        majority, _ = c.most_common(1)[0]
        for iso, em in per_iso.items():
            if k in em and em[k] != majority:
                overrides += 1
                deviating_deployments.add(iso)

    print("\n" + "=" * 78)
    print("IF a single canonical set = the majority English value per key:")
    print(f"  total (deployment,key) overrides needed to be exact: {overrides}")
    print(f"  deployments that deviate on >=1 key:                 "
          f"{len(deviating_deployments)}/{n}")
    print(f"  deployments identical to the canonical set:          "
          f"{n - len(deviating_deployments)}/{n}")
    # worst offenders
    per_dep = Counter()
    for k, c in values.items():
        if len(c) == 1:
            continue
        majority, _ = c.most_common(1)[0]
        for iso, em in per_iso.items():
            if k in em and em[k] != majority:
                per_dep[iso] += 1
    if per_dep:
        print("  worst-deviating deployments (key count):")
        for iso, cnt in per_dep.most_common(8):
            print(f"      {iso}: {cnt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
