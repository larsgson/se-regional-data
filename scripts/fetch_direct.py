#!/usr/bin/env python3
"""
Fetch specific language collections from custom (non-scriptureearth.org) SAB
deployments — direct links received outside SE's country/ISO discovery.

These deployments use the same SvelteKit/SAB app shell as scriptureearth.org
(same service-worker.js + hashed /_app/immutable/assets/ layout), just hosted
on an arbitrary domain. A single deployment can bundle several language
collections (e.g. eng_C02 + grc_C03 + ind_C01) — this script fetches only the
collections you list in the config, ignoring the rest.

Config: conf/direct-sources.txt
    base_url  collection_base  target_iso  license
(see that file's header comment for column details)

Usage:
    scripts/fetch_direct.py                      # process every entry
    scripts/fetch_direct.py --conf conf/foo.txt
    scripts/fetch_direct.py --dry-run
    scripts/fetch_direct.py --force               # re-download even if cached

Output — written straight into the shared pool, same layout as fetch_pkf.py:
    data/_pool/<target_iso>/
        info.json                 # source_type="direct" + manual_license block
        <collection_base>.<hash>.pkf
        <collection_base>.<hash>.json
        styles/raw/*.css, styles/fonts/*, styles/bundle.css

Each fetched ISO also gets a symlinked view + manifest.json entry under
data/direct/ (a pseudo-"country" bucket), so it flows through the existing
merge/classify/pack/release pipeline unchanged. Add a bare "direct" line to
any region .conf (e.g. conf/global.conf) to fold it into that merge.

classify_licenses.py reads the manual_license block and skips its normal
scriptureearth.org network probe for these ISOs — the license here is taken
on faith from whoever filled in conf/direct-sources.txt, not auto-detected.

Safety: refuses to overwrite a pool ISO that already exists from a NON-direct
source (i.e. a real scriptureearth.org fetch), and vice versa — fetch_pkf.py
refuses to overwrite an ISO marked source_type="direct" here. This guards
against an ISO-code collision silently clobbering the other source.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import REPO_ROOT  # noqa: E402

UA = "Mozilla/5.0 (pkf-fetcher; stdlib)"
DEFAULT_CONF = REPO_ROOT / "conf" / "direct-sources.txt"
POOL_DIR = REPO_ROOT / "data" / "_pool"
DIRECT_DIR = REPO_ROOT / "data" / "direct"

PKF_RE = re.compile(r"/_app/immutable/assets/([A-Za-z0-9_\-]+)\.([A-Za-z0-9_\-]+)\.pkf")
JSON_RE = re.compile(r"/_app/immutable/assets/([A-Za-z0-9_\-]+)\.([A-Za-z0-9_\-]+)\.json")
CSS_RE = re.compile(r"/_app/immutable/assets/([A-Za-z0-9_\-]+)\.([A-Za-z0-9_\-]+)\.css")
FONT_RE = re.compile(r"/_app/immutable/assets/([A-Za-z0-9_\-]+)\.([A-Za-z0-9_\-]+)\.(ttf|otf|woff2?)")
CSS_URL_REF_RE = re.compile(r"url\(\s*\.?/?([A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.(?:ttf|otf|woff2?))\s*\)")

# Named CSS files that together form the full reader style (same as fetch_pkf.py).
STYLE_CSS_NAMES = ("sab-app", "sab-annotations", "override-dab")

LICENSE_LABELS = {
    "public-domain": "Public Domain",
    "cc-by-nc-nd": "CC-BY-NC-ND-4.0",
    "cc-by": "CC-BY-4.0",
    "proprietary": "proprietary",
    "unknown": "unknown",
}


@dataclass
class Asset:
    name: str
    base: str
    hash: str
    kind: str
    url: str
    ext: str = ""


@dataclass
class SourceEntry:
    base_url: str
    collection_base: str
    target_iso: str
    license: str


def http_get(url: str, timeout: int = 30) -> bytes:
    req = Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def http_get_text(url: str, timeout: int = 30) -> str:
    return http_get(url, timeout).decode("utf-8", errors="replace")


def parse_conf(path: Path) -> list[SourceEntry]:
    out: list[SourceEntry] = []
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split()
        if len(parts) != 4:
            print(f"[direct] {path}:{lineno}: expected 4 columns, got {len(parts)} — skipping",
                  file=sys.stderr)
            continue
        base_url, collection_base, target_iso, license_tag = parts
        out.append(SourceEntry(base_url.rstrip("/"), collection_base, target_iso, license_tag))
    return out


def discover_assets(base_url: str, collection_base: str) -> list[Asset] | None:
    """Fetch service-worker.js at base_url and extract assets for one collection.

    Returns None if the deployment is unreachable or the collection isn't found.
    """
    try:
        sw = http_get_text(f"{base_url}/service-worker.js")
    except (HTTPError, URLError) as e:
        print(f"[direct] {base_url}: cannot fetch service-worker.js ({e})", file=sys.stderr)
        return None

    pkfs: list[Asset] = []
    for m in PKF_RE.finditer(sw):
        base, h = m.group(1), m.group(2)
        if base != collection_base:
            continue
        pkfs.append(Asset(f"{base}.{h}.pkf", base, h, "pkf",
                           f"{base_url}/_app/immutable/assets/{base}.{h}.pkf"))
    if not pkfs:
        print(f"[direct] {base_url}: no .pkf found for collection '{collection_base}' "
              f"(check the name against that deployment's service-worker.js)", file=sys.stderr)
        return None

    jsons: list[Asset] = []
    for m in JSON_RE.finditer(sw):
        base, h = m.group(1), m.group(2)
        if base != collection_base:
            continue
        jsons.append(Asset(f"{base}.{h}.json", base, h, "json",
                            f"{base_url}/_app/immutable/assets/{base}.{h}.json"))

    # Collection suffix, e.g. "ind_C01" -> "C01". Unlike scriptureearth.org apps
    # (where the per-collection CSS is sab-bc-<iso>), these custom deployments
    # name it sab-bc-<collection-suffix> instead.
    suffix = collection_base.split("_")[-1]
    wanted_css = set(STYLE_CSS_NAMES) | {f"sab-bc-{suffix}"}
    css_assets: list[Asset] = []
    css_seen: set[tuple[str, str]] = set()
    for m in CSS_RE.finditer(sw):
        base, h = m.group(1), m.group(2)
        if base not in wanted_css or (base, h) in css_seen:
            continue
        css_seen.add((base, h))
        css_assets.append(Asset(f"{base}.{h}.css", base, h, "css",
                                 f"{base_url}/_app/immutable/assets/{base}.{h}.css"))

    # Fonts are shared across all collections in the deployment; grab them all
    # (matches fetch_pkf.py's behavior — harmless if a font is unused by this
    # collection's CSS).
    font_assets: list[Asset] = []
    font_seen: set[tuple[str, str, str]] = set()
    for m in FONT_RE.finditer(sw):
        base, h, ext = m.group(1), m.group(2), m.group(3)
        if (base, h, ext) in font_seen:
            continue
        font_seen.add((base, h, ext))
        font_assets.append(Asset(f"{base}.{h}.{ext}", base, h, "font",
                                  f"{base_url}/_app/immutable/assets/{base}.{h}.{ext}", ext))

    return pkfs + jsons + css_assets + font_assets


def download_asset(asset: Asset, dest: Path, force: bool) -> str:
    """Return 'fetched' | 'cached'."""
    if dest.exists() and not force:
        return "cached"
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(http_get(asset.url))
    tmp.replace(dest)
    return "fetched"


def write_font_aware_css(css_text: str) -> str:
    return CSS_URL_REF_RE.sub(lambda m: f"url(./fonts/{m.group(1)})", css_text)


def build_style_bundle(suffix: str, iso_dir: Path, css_assets: list[Asset]) -> Path | None:
    """Same ordering rule as fetch_pkf.py, but keyed on the collection suffix
    (e.g. "C01") instead of the ISO code — see discover_assets() note above."""
    order = ["sab-app", "sab-annotations", "override-dab", f"sab-bc-{suffix}"]
    by_base = {a.base: a for a in css_assets}
    parts: list[str] = []
    for base in order:
        a = by_base.get(base)
        if not a:
            continue
        raw_path = iso_dir / "styles" / "raw" / a.name
        if not raw_path.exists():
            continue
        parts.append(f"/* ===== {a.name} ===== */")
        parts.append(write_font_aware_css(raw_path.read_text(encoding="utf-8")))
    if not parts:
        return None
    bundle = iso_dir / "styles" / "bundle.css"
    bundle.parent.mkdir(parents=True, exist_ok=True)
    bundle.write_text("\n".join(parts), encoding="utf-8")
    return bundle


def fetch_entry(entry: SourceEntry, force: bool, dry: bool) -> dict:
    result = {"iso": entry.target_iso, "ok": False, "error": None, "assets": []}

    pool_iso = POOL_DIR / entry.target_iso
    info_path = pool_iso / "info.json"
    if info_path.exists():
        try:
            existing = json.loads(info_path.read_text(encoding="utf-8"))
        except Exception:
            existing = None
        if existing and existing.get("source_type") != "direct":
            result["error"] = (
                f"data/_pool/{entry.target_iso}/ already exists from a different "
                f"source ({existing.get('source')!r}) — refusing to overwrite; "
                f"pick a different target_iso in conf/direct-sources.txt"
            )
            return result

    assets = discover_assets(entry.base_url, entry.collection_base)
    if assets is None:
        result["error"] = "no matching assets found (see warning above)"
        return result

    for a in assets:
        if a.kind in ("pkf", "json"):
            target = pool_iso / a.name
        elif a.kind == "css":
            target = pool_iso / "styles" / "raw" / a.name
        elif a.kind == "font":
            target = pool_iso / "styles" / "fonts" / a.name
        else:
            target = pool_iso / a.name
        rec = {"name": a.name, "kind": a.kind, "base": a.base, "hash": a.hash, "url": a.url}
        if dry:
            rec["action"] = "would_fetch"
        else:
            rec["action"] = download_asset(a, target, force)
            rec["size"] = target.stat().st_size
        result["assets"].append(rec)

    if not dry:
        pool_iso.mkdir(parents=True, exist_ok=True)
        css_assets = [a for a in assets if a.kind == "css"]
        suffix = entry.collection_base.split("_")[-1]
        bundle = build_style_bundle(suffix, pool_iso, css_assets)

        license_label = LICENSE_LABELS.get(entry.license, entry.license)
        info = {
            "iso": entry.target_iso,
            "version": None,
            "source": entry.base_url,
            "source_type": "direct",
            "collection": entry.collection_base,
            "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "style_bundle": "styles/bundle.css" if bundle else None,
            "assets": result["assets"],
            "manual_license": {
                "include": entry.license not in ("proprietary", "unknown"),
                "license": license_label,
                "reason": (
                    f"Manually verified by repo maintainer for direct source "
                    f"{entry.base_url} (collection {entry.collection_base}) — "
                    f"not a scriptureearth.org deployment; classify_licenses.py "
                    f"skips its normal network probe and trusts this tag."
                ),
                "text_holder": None,
            },
        }
        info_path.write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")

    result["ok"] = True
    return result


def _symlink_iso(pool: Path, out: Path, iso: str) -> None:
    link = out / iso
    if link.is_symlink():
        link.unlink()
    elif link.is_dir():
        return
    target = os.path.relpath(pool / iso, out)
    link.symlink_to(target)


def write_manifest(out: Path, results: list[dict]) -> None:
    existing: dict[str, dict] = {}
    manifest_path = out / "manifest.json"
    if manifest_path.exists():
        try:
            prev = json.loads(manifest_path.read_text(encoding="utf-8"))
            for e in prev.get("languages", []):
                existing[e["iso"]] = e
        except Exception:
            pass
    for r in results:
        if not r.get("ok"):
            continue
        existing[r["iso"]] = {
            "iso": r["iso"],
            "version": None,
            "pkfs": [a["name"] for a in r["assets"] if a["kind"] == "pkf"],
            "catalogs": [a["name"] for a in r["assets"] if a["kind"] == "json"],
            "styles": sum(1 for a in r["assets"] if a["kind"] == "css"),
            "fonts": sum(1 for a in r["assets"] if a["kind"] == "font"),
            "pkf_bytes": sum(a.get("size", 0) for a in r["assets"] if a["kind"] == "pkf"),
        }
    manifest = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "languages": sorted(existing.values(), key=lambda x: x["iso"]),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Fetch specific collections from direct (non-SE) SAB deployments",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--conf", type=Path, default=DEFAULT_CONF,
                    help=f"Config file (default: {DEFAULT_CONF.relative_to(REPO_ROOT)})")
    ap.add_argument("--force", action="store_true",
                    help="Re-download even if the asset already exists")
    ap.add_argument("--dry-run", action="store_true",
                    help="List what would be fetched; do not download")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if not args.conf.exists():
        print(f"[direct] missing {args.conf}", file=sys.stderr)
        return 1

    entries = parse_conf(args.conf)
    if not entries:
        print(f"[direct] no entries in {args.conf}", file=sys.stderr)
        return 0

    plural = "y" if len(entries) == 1 else "ies"
    print(f"[direct] processing {len(entries)} source entr{plural} from {args.conf}")

    if not args.dry_run:
        POOL_DIR.mkdir(parents=True, exist_ok=True)
        DIRECT_DIR.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    for e in entries:
        r = fetch_entry(e, args.force, args.dry_run)
        results.append(r)
        if r["ok"]:
            pkf_n = sum(1 for a in r["assets"] if a["kind"] == "pkf")
            print(f"  [ok]   {r['iso']:<8} <- {e.base_url} ({e.collection_base})  "
                  f"{pkf_n} pkf  license={e.license}")
        else:
            print(f"  [skip] {r['iso']:<8} {r['error']}")

    if not args.dry_run:
        for r in results:
            if r["ok"]:
                _symlink_iso(POOL_DIR, DIRECT_DIR, r["iso"])
        write_manifest(DIRECT_DIR, results)

    ok = sum(1 for r in results if r["ok"])
    print(f"[done] {ok}/{len(results)} OK -> data/direct/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
