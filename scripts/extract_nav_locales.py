#!/usr/bin/env python3
"""
Extract per-locale UI-string sets (the same Menu_*/Settings_*/... namespace as
conf/nav-base.json) from every cached SAB manifest chunk, and write one
reconciled conf/nav-<code>.json per non-English locale.

SAB ships a small library of pre-built UI translations; a deployment opts into
whichever ones its community also reads (app-config.json interfaceLanguages).
Different, unrelated deployments offering the same locale carry near-identical
string sets (same principle already used to build conf/nav-base.json for
English — verified here too: 3 independent Spanish deployments were 376/376
byte-identical), so this reconciles across ALL contributing deployments per
locale (majority vote per key) rather than picking one arbitrarily.

Output: conf/nav-<code>.json for every code that clears MIN_DEPLOYMENTS or
MIN_KEYS (a single deployment offering its own from-scratch UI translation,
e.g. a language with only 3 of 459 keys translated, is too sparse to publish
as a full locale and is skipped — reported, not silently dropped).

Disagreement across contributing deployments is common and mostly NOT
cosmetic (measured: ~85% of es disagreements, ~93% of id's, are genuine
different wording — different SAB tooling versions over time, not
case/whitespace drift). A key is only published when the majority clears
--min-agreement (default 0.6); below that, we'd be publishing what's really a
coin flip, so the key is left out and the client falls back to English.

Scanning all ~626 cached chunks takes ~5-6 min, so results are cached to
data/.nav-locales-cache.json. Re-running reconciliation (e.g. with a
different --min-agreement) reads the cache and is near-instant; pass
--rescan to force a fresh corpus scan (only needed after data/.media-cache/
changes, e.g. a new fetch_pkf.py run).

Usage: python3 scripts/extract_nav_locales.py [--rescan] [--min-agreement F]
                                              [--min-deployments N] [--min-keys N]
"""
import argparse
import glob
import json
import re
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "data" / ".media-cache"
NAV_BASE = REPO_ROOT / "conf" / "nav-base.json"
OUT_DIR = REPO_ROOT / "conf"
SCAN_CACHE = REPO_ROOT / "data" / ".nav-locales-cache.json"

# A single alternation of only our known nav-base keys, so the engine tries
# these exact literal prefixes instead of a generic identifier pattern at
# every position in a ~1.5MB minified chunk (that naive approach took ~1hr
# across the corpus; this is ~5.5 min).
PAIR_RE = re.compile(r'([A-Za-z_][A-Za-z0-9_]*):"((?:[^"\\]|\\.)*)"')


def build_key_alt(nav_keys: list) -> re.Pattern:
    ordered = sorted(nav_keys, key=len, reverse=True)  # longest-first: no prefix shadowing
    return re.compile(r'(' + '|'.join(re.escape(k) for k in ordered) + r'):\{([^{}]*)\}')


def scan_corpus(nav_keys: set) -> tuple[dict, dict]:
    """{code: {key: Counter(value -> count)}}, {code: set(contributing isos)}."""
    key_alt = build_key_alt(list(nav_keys))
    by_code = defaultdict(lambda: defaultdict(Counter))
    contributors = defaultdict(set)

    files = sorted(glob.glob(str(CACHE_DIR / "*.js")))
    t0 = time.time()
    for n, f in enumerate(files):
        iso = Path(f).name.split(".")[0]
        s = Path(f).read_text(encoding="utf-8", errors="replace")
        for om in key_alt.finditer(s):
            key, inner = om.group(1), om.group(2)
            for code, val in PAIR_RE.findall(inner):
                if code == "en":
                    continue
                by_code[code][key][val] += 1
                contributors[code].add(iso)
        if n % 100 == 0:
            print(f"  {n}/{len(files)} chunks, {time.time()-t0:.0f}s", file=sys.stderr)
    print(f"[nav-locales] {len(files)} chunks scanned in {time.time()-t0:.0f}s", file=sys.stderr)
    return by_code, contributors


def save_cache(by_code: dict, contributors: dict) -> None:
    payload = {
        "by_code": {c: {k: dict(v) for k, v in keys.items()} for c, keys in by_code.items()},
        "contributors": {c: sorted(s) for c, s in contributors.items()},
    }
    SCAN_CACHE.write_text(json.dumps(payload), encoding="utf-8")


def load_cache() -> tuple[dict, dict]:
    payload = json.loads(SCAN_CACHE.read_text(encoding="utf-8"))
    by_code = {c: {k: Counter(v) for k, v in keys.items()}
               for c, keys in payload["by_code"].items()}
    contributors = {c: set(s) for c, s in payload["contributors"].items()}
    return by_code, contributors


def reconcile(by_code: dict, contributors: dict, nav_keys: set,
              min_deployments: int, min_keys: int, min_agreement: float):
    published, skipped, disagreements, omitted = {}, [], {}, {}
    for code in sorted(by_code):
        n_dep = len(contributors[code])
        keys = by_code[code]
        if n_dep < min_deployments or len(keys) < min_keys:
            skipped.append((code, n_dep, len(keys)))
            continue
        final, dis, weak = {}, [], []
        for key, counts in keys.items():
            (top_val, top_n), *rest = counts.most_common()
            total = sum(counts.values())
            agreement = top_n / total
            if agreement < min_agreement:
                # A weak majority is really a coin flip between different SAB
                # tooling versions' wording — publishing one would misrepresent
                # it as settled. Omit; the client falls back to English.
                weak.append((key, top_val, top_n, total))
                continue
            final[key] = top_val
            if top_n < total:  # published but not unanimous
                dis.append((key, top_val, top_n, total))
        published[code] = {"strings": final, "deployments": n_dep,
                            "keys": len(final), "of": len(nav_keys)}
        disagreements[code] = dis
        omitted[code] = weak
    return published, skipped, disagreements, omitted


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--rescan", action="store_true",
                    help="force a fresh corpus scan instead of using the cache")
    ap.add_argument("--min-agreement", type=float, default=0.6,
                    help="omit a key if the majority value has less than this "
                         "fraction of agreement across contributing deployments (default 0.6)")
    ap.add_argument("--min-deployments", type=int, default=2,
                    help="skip a locale with fewer contributing deployments (default 2)")
    ap.add_argument("--min-keys", type=int, default=50,
                    help="skip a locale with fewer recovered keys (default 50)")
    args = ap.parse_args()

    nav_base = json.loads(NAV_BASE.read_text(encoding="utf-8"))
    nav_keys = set(nav_base.keys())

    if args.rescan or not SCAN_CACHE.exists():
        by_code, contributors = scan_corpus(nav_keys)
        save_cache(by_code, contributors)
    else:
        print(f"[nav-locales] using cached scan ({SCAN_CACHE.relative_to(REPO_ROOT)}); "
              f"pass --rescan to force a fresh corpus scan", file=sys.stderr)
        by_code, contributors = load_cache()

    published, skipped, disagreements, omitted = reconcile(
        by_code, contributors, nav_keys, args.min_deployments, args.min_keys, args.min_agreement)

    for code, info in sorted(published.items(), key=lambda kv: -kv[1]["deployments"]):
        out = OUT_DIR / f"nav-{code}.json"
        out.write_text(json.dumps(info["strings"], indent=2, sort_keys=True, ensure_ascii=False) + "\n",
                       encoding="utf-8")
        dis, weak = disagreements[code], omitted[code]
        print(f"[nav-locales] {code}: {info['deployments']} deployments, "
              f"{info['keys']}/{info['of']} keys -> {out.relative_to(REPO_ROOT)} "
              f"({len(dis)} published-but-contested, {len(weak)} omitted as too weak)")
        for key, val, top_n, total in dis[:3]:
            print(f"    contested (published): {key}: {top_n}/{total} -> {val!r}")
        for key, val, top_n, total in weak[:3]:
            print(f"    omitted (weak {top_n}/{total}): {key}: {val!r}")

    if skipped:
        print(f"\n[nav-locales] skipped (below --min-deployments={args.min_deployments} "
              f"or --min-keys={args.min_keys}):")
        for code, n_dep, n_keys in skipped:
            print(f"    {code}: {n_dep} deployment(s), {n_keys} key(s)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
