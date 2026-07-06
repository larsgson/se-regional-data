#!/usr/bin/env python3
"""
Extract per-language UI configuration from each SAB deployment's manifest JS
chunk and write data/_pool/<iso>/app-config.json. This is the reader's
behaviour + identity config (NOT the scripture text, colours, or UI strings):

    { collection, interfaceLanguages, features, books, themeNames, copyright }

Per the settled contract (docs/client-data-spec.md §6), there is deliberately
NO `nav` here — UI strings are the single shared conf/nav-base.json, published
separately as pkf/_app/nav-base.json.

Chunk source, in order:
  1. the cached manifest chunk data/.media-cache/<iso>.<hash>.js (map_media.py
     already downloaded these for most ISOs — reused, no network);
  2. else fetch the deployment's service-worker, find the chunk carrying
     `bookCollections:`, and cache it (SE ISOs, and direct sources via their
     info.json `source` URL).

Copyright is read from data/global/licenses.json (the classifier output);
falls back to the pool info.json `manual_license` block for direct sources.

Idempotent; safe to re-run. Env: CONCURRENCY=6.
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import POOL_DIR, REPO_ROOT  # noqa: E402
from map_media import (  # noqa: E402
    CACHE_DIR, SE, fetch_text, get_field, head_size,
)

LICENSES = REPO_ROOT / "data" / "global" / "licenses.json"

# Prefix-agnostic: SAB builds reference chunks as `<var>+"/_app/immutable/…js"`
# where <var> is s, a, … depending on the build. Match the path regardless.
CHUNK_PATH_RE = re.compile(
    r'/_app/immutable/(?:chunks|nodes|entry)/[A-Za-z0-9_.\-]+\.js'
)


def chunk_paths(sw: str) -> list:
    seen, out = set(), []
    for m in CHUNK_PATH_RE.finditer(sw):
        if m.group(0) not in seen:
            seen.add(m.group(0))
            out.append(m.group(0))
    return out


# --- generic minified-JS helpers -------------------------------------------


def find_match(text: str, start: int) -> int:
    """Match the bracket/brace at text[start] ({ or [), respecting JS strings."""
    open_ch = text[start]
    close_ch = "}" if open_ch == "{" else "]"
    depth = 0
    in_str = False
    q = ""
    i = start
    while i < len(text):
        c = text[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == q:
                in_str = False
        elif c in ('"', "'", "`"):
            in_str = True
            q = c
        elif c == open_ch:
            depth += 1
        elif c == close_ch:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def slice_block(text: str, pat: str, opener: str) -> str | None:
    """Return the {...} or [...] block whose start matches regex `pat`."""
    m = re.search(pat, text)
    if not m:
        return None
    i = text.find(opener, m.start())
    if i < 0:
        return None
    end = find_match(text, i)
    return text[i:end + 1] if end > i else None


def iter_objects(arr_text: str):
    i = arr_text.find("{")
    while 0 <= i < len(arr_text):
        end = find_match(arr_text, i)
        if end < 0:
            break
        yield arr_text[i:end + 1]
        i = arr_text.find("{", end + 1)


SCALAR = re.compile(
    r'"?([A-Za-z_][\w-]*)"?\s*:\s*'
    r'("(?:[^"\\]|\\.)*"|!0|!1|-?\d+(?:\.\d+)?)'
)


def parse_flat(block: str) -> dict:
    """Top-level scalar entries of a minified object; skips nested obj/arrays.

    Walks the block once tracking brace/bracket depth and string state, and
    keeps only `key: scalar` pairs that sit at depth 1 (immediate children)."""
    out: dict = {}
    depth = 0
    in_str = False
    q = ""
    i = 0
    n = len(block)
    while i < n:
        c = block[i]
        if in_str:
            if c == "\\":
                i += 2
                continue
            if c == q:
                in_str = False
            i += 1
            continue
        # immediate child (depth 1): try to grab a `key: scalar` pair first, so
        # a quoted key isn't swallowed by the string handler below.
        if depth == 1:
            m = SCALAR.match(block, i)
            if m:
                out[m.group(1)] = _scalar(m.group(2))
                i = m.end()
                continue
        if c in ('"', "'", "`"):
            in_str = True
            q = c
            i += 1
            continue
        if c in "{[":
            depth += 1
            if depth == 2:  # a nested value under the outer object — skip it whole
                j = find_match(block, i)
                if j > i:
                    i = j + 1
                    depth -= 1
                    continue
            i += 1
            continue
        if c in "}]":
            depth -= 1
            i += 1
            continue
        i += 1
    return out


def _scalar(raw: str):
    if raw == "!0":
        return True
    if raw == "!1":
        return False
    if raw.startswith('"'):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw.strip('"')
    return float(raw) if "." in raw else int(raw)


# --- section extractors -----------------------------------------------------


def extract_collection(coll: str, iso: str) -> dict:
    td = (get_field(coll, "textDirection") or "LTR")
    abbr = get_field(coll, "collectionAbbreviation")
    # collectionName is only set by newer SAB builds; fall back so name is never null
    name = get_field(coll, "collectionName") or abbr or iso
    return {
        "id": get_field(coll, "id"),
        "name": name,
        "abbreviation": abbr,
        "languageCode": get_field(coll, "languageCode") or iso,
        "textDirection": str(td).lower(),
    }


def extract_features(js: str, coll: str) -> dict:
    main = slice_block(js, r"mainFeatures:\{", "{") or "{}"
    cfeat = slice_block(coll, r"\bfeatures:\{", "{") or "{}"
    merged = {**parse_flat(main), **parse_flat(cfeat)}  # collection wins
    return merged


def extract_books(coll: str) -> list:
    arr = slice_block(coll, r"\bbooks:\[", "[")
    if not arr:
        return []
    out = []
    for obj in iter_objects(arr):
        bid = get_field(obj, "id")
        if not bid:
            continue
        entry = {
            "id": bid,
            "name": get_field(obj, "name"),
            "abbreviation": get_field(obj, "abbreviation"),
            "testament": get_field(obj, "testament"),
            "section": get_field(obj, "section"),
        }
        ch = get_field(obj, "chapters")
        if ch is not None:
            entry["chapters"] = ch
        out.append(entry)
    return out


def extract_theme_names(js: str) -> list:
    arr = slice_block(js, r"\bthemes:\[", "[") or ""
    names = re.findall(r'\{name:"([^"]+)",enabled:', arr)
    default = get_field(js, "defaultTheme")
    return [{"id": n, **({"default": True} if n == default else {})} for n in names]


def extract_interface_languages(js: str) -> dict:
    ws = slice_block(js, r"interfaceLanguages:\{", "{")
    available = []
    if ws:
        block = slice_block(ws, r"writingSystems:\{", "{") or ""
        for m in re.finditer(r'([A-Za-z][\w-]*):\{type:"interface"([^{}]*(?:\{[^{}]*\}[^{}]*)*)\}', block):
            code = m.group(1)
            body = m.group(2)
            td = re.search(r'textDirection:"(\w+)"', body)
            dn = re.search(r'displayNames:\{[^}]*\b' + re.escape(code) + r':"([^"]*)"', body)
            dn_en = re.search(r'displayNames:\{[^}]*\ben:"([^"]*)"', body)
            available.append({
                "code": code,
                "displayName": (dn or dn_en).group(1) if (dn or dn_en) else code,
                "textDirection": (td.group(1).lower() if td else "ltr"),
            })
    default = get_field(js, "defaultLang")
    return {"default": default, "available": available}


def load_licenses() -> dict:
    if LICENSES.exists():
        try:
            return json.loads(LICENSES.read_text(encoding="utf-8")).get("included", {})
        except Exception:
            pass
    return {}


def extract_copyright(iso: str, info: dict, lic: dict) -> dict | None:
    entry = lic.get(iso)
    if entry:
        out = {"license": entry.get("license")}
        if entry.get("text_holder"):
            out["holder"] = entry["text_holder"]
        out["source"] = info.get("source")
        return out
    ml = info.get("manual_license")
    if ml:
        out = {"license": ml.get("license")}
        if ml.get("text_holder"):
            out["holder"] = ml["text_holder"]
        out["source"] = info.get("source")
        return out
    return None


# --- chunk acquisition ------------------------------------------------------


def get_chunk(iso: str, info: dict) -> str | None:
    # 1) cached manifest chunk from map_media
    for f in glob.glob(str(CACHE_DIR / f"{iso}.*.js")):
        body = Path(f).read_text(encoding="utf-8", errors="replace")
        if "bookCollections:" in body:
            return body
    # 2) fetch: direct source uses its own base_url; SE uses the app path
    if info.get("source_type") == "direct" and info.get("source"):
        base = info["source"].rstrip("/")
    else:
        base = f"{SE}/data/{iso}/sab/{iso}"
    sw = fetch_text(f"{base}/service-worker.js")
    if not sw:
        return None
    # HEAD each chunk, try largest-first, stop at the one carrying bookCollections
    paths = chunk_paths(sw)
    paths.sort(key=lambda p: -head_size(f"{base}{p}"))
    for p in paths[:12]:
        b = fetch_text(f"{base}{p}")
        if b and "bookCollections:" in b:
            CACHE_DIR.mkdir(parents=True, exist_ok=True)
            (CACHE_DIR / f"{iso}.appcfg.js").write_text(b, encoding="utf-8")
            return b
    return None


def process_iso(iso: str, lic: dict) -> dict:
    iso_dir = POOL_DIR / iso
    info_path = iso_dir / "info.json"
    try:
        info = json.loads(info_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"iso": iso, "error": "no info.json"}

    js = get_chunk(iso, info)
    if not js:
        return {"iso": iso, "error": "no manifest chunk"}

    coll = slice_block(js, r"bookCollections:\[\{", "{")
    if not coll:
        return {"iso": iso, "error": "no bookCollections"}

    cfg = {
        "collection": extract_collection(coll, iso),
        "interfaceLanguages": extract_interface_languages(js),
        "features": extract_features(js, coll),
        "books": extract_books(coll),
        "themeNames": extract_theme_names(js),
    }
    cr = extract_copyright(iso, info, lic)
    if cr:
        cfg["copyright"] = cr

    (iso_dir / "app-config.json").write_text(
        json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return {"iso": iso, "books": len(cfg["books"]),
            "features": len(cfg["features"]), "themes": len(cfg["themeNames"]),
            "copyright": bool(cr)}


def main() -> int:
    argv = sys.argv[1:]
    only = set(argv) if argv else None
    conc = int(os.environ.get("CONCURRENCY", "6"))
    lic = load_licenses()

    isos = [p.name for p in sorted(POOL_DIR.iterdir())
            if p.is_dir() and not p.name.startswith("_")
            and (not only or p.name in only)]

    # Stage the curated shared UI-string set into the pool for delivery
    # (published as pkf/_app/nav-base.json). Canonical source is conf/nav-base.json.
    nav_src = REPO_ROOT / "conf" / "nav-base.json"
    if nav_src.exists() and not only:
        app_dir = POOL_DIR / "_app"
        app_dir.mkdir(parents=True, exist_ok=True)
        (app_dir / "nav-base.json").write_text(
            nav_src.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[nav-base] staged conf/nav-base.json -> {app_dir}/nav-base.json")

    ok = err = 0
    with ThreadPoolExecutor(max_workers=conc) as ex:
        futs = {ex.submit(process_iso, iso, lic): iso for iso in isos}
        for fut in as_completed(futs):
            r = fut.result()
            if r.get("error"):
                err += 1
                print(f"  [skip] {r['iso']:<8} {r['error']}", file=sys.stderr)
            else:
                ok += 1
                print(f"  [ok]   {r['iso']:<8} books={r['books']:>3} "
                      f"features={r['features']:>2} themes={r['themes']} "
                      f"copyright={'Y' if r['copyright'] else '-'}", file=sys.stderr)
    print(f"\n[done] {ok} app-config.json written, {err} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
