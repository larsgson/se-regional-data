#!/usr/bin/env python3
"""
Audit: does each published ISO's content plausibly match the language it
claims to be?

We have no independent linguistic verification anywhere in the pipeline —
every language's identity traces back to a single trust point (Scripture
Earth's own URL indexing, scriptureearth.org/data/<iso>/sab/<iso>). This
script adds a second, independent signal: every SAB deployment's own JS
bundle self-reports an app `name:"..."` string (authored by the translation
project itself), which we already cache but never cross-check against our
`nm`/`v` (conf/lang-names.json).

Not full language ID — just a cheap tripwire. Normalizes both strings, drops
common Bible-app stopwords (bible, biblia, nt, testament, ...), and flags any
ISO where the self-reported name shares NO token with our nm/v/ISO-639-3
name. A flag means "needs a human look" (dialect-name conventions, endonym vs
exonym, and multi-collection languages legitimately produce some false
positives), not "confirmed wrong" — see how aom/uth were resolved by hand
despite a real red flag (the "lwm"/"dud" pkf prefixes) that turned out benign
once the self-reported name ("Omie Bible") was checked.

Usage: python3 scripts/audit_language_identity.py [--out PATH]
"""
import argparse
import glob
import json
import re
import sys
import unicodedata
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = REPO_ROOT / "data" / ".media-cache"
LANG_NAMES = REPO_ROOT / "conf" / "lang-names.json"
POOL_DIR = REPO_ROOT / "data" / "_pool"

NAME_RE = re.compile(r'name:"([^"]{2,100})"')
PACKAGE_RE = re.compile(r'package:"([^"]{2,120})"')

STOPWORDS = {
    "bible", "biblia", "biblia", "nt", "new", "testament", "scripture",
    "scriptures", "sagrada", "sagradas", "escrituras", "escritura", "santa",
    "santo", "holy", "el", "la", "the", "de", "del", "en", "und", "et", "le",
    "la", "los", "las", "of", "in", "and", "sab", "app", "version", "bibel",
    "nuevo", "nouveau", "novo", "kitab", "suci",
}


def strip_diacritics(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", s)
                   if not unicodedata.combining(c))


def tokens(s: str) -> set:
    words = re.findall(r"[A-Za-z0-9']+", strip_diacritics(s).lower())
    return {w for w in words if len(w) >= 3 and w not in STOPWORDS}


def load_names() -> dict:
    try:
        return json.loads(LANG_NAMES.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def audit_iso(iso: str, names: dict) -> dict | None:
    files = glob.glob(str(CACHE_DIR / f"{iso}.*.js"))
    if not files:
        return {"iso": iso, "status": "no-cached-chunk"}
    s = Path(files[0]).read_text(encoding="utf-8", errors="replace")
    m = NAME_RE.search(s)
    if not m:
        return {"iso": iso, "status": "no-name-field"}
    app_name = m.group(1)
    pm = PACKAGE_RE.search(s)
    package = pm.group(1) if pm else None

    entry = names.get(iso, {})
    nm, v = entry.get("nm", ""), entry.get("v", "")
    name_tokens = tokens(nm) | tokens(v)
    app_tokens = tokens(app_name) | (tokens(package) if package else set())

    name_overlap = sorted(name_tokens & app_tokens)
    iso_in_package = bool(package and iso in package.lower())
    if name_overlap:
        status = "ok"
    elif iso_in_package:
        # Weaker signal: only the iso code itself appears in the package id
        # (e.g. org.wycliffe.<iso>....) — no actual name-word match. Real for
        # aom (Wycliffe's own package naming convention), but worth surfacing
        # separately since it's one signal short of a genuine cross-check.
        status = "weak-pass"
    else:
        status = "flagged"
    info_path = POOL_DIR / iso / "info.json"
    source = None
    if info_path.exists():
        try:
            source = json.loads(info_path.read_text(encoding="utf-8")).get("source")
        except (OSError, ValueError):
            pass

    return {
        "iso": iso, "nm": nm, "v": v or None, "app_name": app_name,
        "package": package, "source": source,
        "status": status, "name_overlap": name_overlap,
        "iso_in_package": iso_in_package,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(REPO_ROOT / "temp" / "language-identity-audit.json"))
    args = ap.parse_args()

    names = load_names()
    isos = sorted(p.name for p in POOL_DIR.iterdir()
                  if p.is_dir() and not p.name.startswith("_"))

    results = [audit_iso(iso, names) for iso in isos]
    by_status = {}
    for r in results:
        by_status.setdefault(r["status"], []).append(r)

    print(f"[audit] {len(results)} ISOs checked")
    for status in ("ok", "weak-pass", "flagged", "no-name-field", "no-cached-chunk"):
        items = by_status.get(status, [])
        print(f"  {status}: {len(items)}")

    weak = by_status.get("weak-pass", [])
    if weak:
        print(f"\n[audit] weak-pass — only the iso code itself matched, no actual "
              f"name-word overlap ({len(weak)}):")
        for r in weak:
            print(f"  {r['iso']:<6} nm={r['nm']!r} v={r['v']!r}  "
                  f"app_name={r['app_name']!r}  package={r['package']}")

    flagged = by_status.get("flagged", [])
    if flagged:
        print(f"\n[audit] flagged for human review ({len(flagged)}):")
        for r in flagged:
            print(f"  {r['iso']:<6} nm={r['nm']!r} v={r['v']!r}  "
                  f"app_name={r['app_name']!r}  source={r['source']}")

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[audit] full report -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
