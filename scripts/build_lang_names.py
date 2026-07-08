#!/usr/bin/env python3
"""Build conf/lang-names.json — the {iso: {nm, v?}} language-name registry that
pack_release.py embeds into manifest.json.

Field names / types / rules mirror the consumer's dbt/_app/media-index.json:
  * nm  English name, ALWAYS present. May carry a ':'/','-qualified variant
        (e.g. "English: USA", "Chinantec, Comaltepec").
  * v   vernacular name in native script. Included ONLY when it differs from nm;
        omitted otherwise.

Sources, in priority order per ISO:
  1. The DBT media-index (cdn.bibel.wiki/dbt/_app/media-index.json) — the
     canonical registry; copying nm/v from it keeps our names identical to the
     data the client merges against. Carries v (native script) for many langs.
  2. SIL ISO 639-3 Ref_Name — English fallback for langs the DBT index doesn't
     list (SE ships a few DBT doesn't). These get nm only; we have no vernacular
     source for them.

Scope: every language in the shared pool (data/_pool/<iso>/), so any COUNTRY
subset a release packs is fully covered.

This snapshots two upstream sources into a committed, reviewable file so the pack
step needs no network. Re-run when the DBT index gains/updates names:
    python3 scripts/build_lang_names.py            # refresh from network
    python3 scripts/build_lang_names.py --offline  # rebuild from cached copies

Raw downloads are cached (gitignored) under data/ so --offline / reruns work.
"""
import argparse
import csv
import json
import os
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
POOL = REPO_ROOT / "data" / "_pool"
OUT = REPO_ROOT / "conf" / "lang-names.json"

ASSET_BASE = os.environ.get("PUBLIC_ASSET_BASE", "https://cdn.bibel.wiki").rstrip("/")
DBT_URL = f"{ASSET_BASE}/dbt/_app/media-index.json"
DBT_CACHE = REPO_ROOT / "data" / ".dbt-media-index.json"
ISO_URL = "https://iso639-3.sil.org/sites/iso639-3/files/downloads/iso-639-3.tab"
ISO_CACHE = REPO_ROOT / "data" / ".iso639-3.tab"


def fetch(url: str, cache: Path, offline: bool) -> bytes:
    if offline:
        if not cache.exists():
            sys.exit(f"[names] --offline but no cache at {cache}; run once online first")
        return cache.read_bytes()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "se-regional-data/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        cache.write_bytes(data)
        return data
    except Exception as e:  # noqa: BLE001 — fall back to cache on any network error
        if cache.exists():
            print(f"[names] fetch {url} failed ({e}); using cached {cache}", file=sys.stderr)
            return cache.read_bytes()
        sys.exit(f"[names] fetch {url} failed and no cache: {e}")


def pool_isos() -> list:
    return sorted(
        p.name for p in POOL.iterdir()
        if p.is_dir() and not p.name.startswith("_")
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--offline", action="store_true",
                    help="rebuild from cached downloads, no network")
    args = ap.parse_args()

    if not POOL.is_dir():
        sys.exit(f"[names] missing {POOL}")

    dbt = json.loads(fetch(DBT_URL, DBT_CACHE, args.offline))["l"]
    iso_names = {}
    for row in csv.DictReader(
        fetch(ISO_URL, ISO_CACHE, args.offline).decode("utf-8").splitlines(),
        delimiter="\t",
    ):
        iso_names[row["Id"]] = row["Ref_Name"]

    registry: dict[str, dict] = {}
    from_dbt = from_iso = no_name = 0
    missing: list[str] = []
    for iso in pool_isos():
        e = dbt.get(iso)
        if e and e.get("nm"):
            entry = {"nm": e["nm"]}
            v = e.get("v")
            if v and v != e["nm"]:      # v only when it differs from nm
                entry["v"] = v
            from_dbt += 1
        elif iso in iso_names:
            entry = {"nm": iso_names[iso]}
            from_iso += 1
        else:
            entry = {"nm": iso.upper()}  # last resort so nm is always present
            no_name += 1
            missing.append(iso)
        registry[iso] = entry

    OUT.write_text(
        json.dumps(registry, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"[names] wrote {OUT.relative_to(REPO_ROOT)}  "
          f"({len(registry)} isos: {from_dbt} from DBT, {from_iso} from ISO 639-3)")
    if missing:
        print(f"[names] WARNING: no name source for {no_name} iso(s), used code "
              f"as nm: {', '.join(missing)}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
