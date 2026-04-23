#!/usr/bin/env python3
"""
Fetch Proskomma .pkf archives (and companion catalog JSON) from scriptureearth.org
for one or more languages.

Scripture Earth hosts per-language Scripture App Builder PWA deployments at
    https://scriptureearth.org/data/<iso>/sab/<iso>/
Each deployment's `service-worker.js` lists every bundled asset, including the
hashed `.pkf` (frozen Proskomma docset) and matching `.json` (catalog). This
script discovers those paths, downloads the assets, and organises them into a
local tree suitable for a multi-version reader PWA.

Usage
-----
    scripts/fetch_pkf.py zai mxt trc nch
    scripts/fetch_pkf.py --iso-file isos.txt
    scripts/fetch_pkf.py --country MX
    scripts/fetch_pkf.py zai mxt --out data/pkf --workers 8 --force

Output layout (under --out, default ./data/pkf)
-----------------------------------------------
    manifest.json                   # summary of all fetched languages
    <iso>/
        info.json                   # per-language metadata (version, assets)
        <iso>_<collection>.<hash>.pkf
        <iso>_<collection>.<hash>.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SE_BASE = "https://scriptureearth.org"
UA = "Mozilla/5.0 (pkf-fetcher; stdlib)"

PKF_RE = re.compile(r"/_app/immutable/assets/([A-Za-z0-9_\-]+)\.([A-Za-z0-9_\-]+)\.pkf")
JSON_RE = re.compile(r"/_app/immutable/assets/([A-Za-z0-9_\-]+)\.([A-Za-z0-9_\-]+)\.json")
CSS_RE = re.compile(r"/_app/immutable/assets/([A-Za-z0-9_\-]+)\.([A-Za-z0-9_\-]+)\.css")
FONT_RE = re.compile(r"/_app/immutable/assets/([A-Za-z0-9_\-]+)\.([A-Za-z0-9_\-]+)\.(ttf|otf|woff2?)")
CSS_URL_REF_RE = re.compile(r"url\(\s*\.?/?([A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.(?:ttf|otf|woff2?))\s*\)")
ISO_LINK_RE = re.compile(r"iso_code=([a-z]{2,4})")

# Named CSS files that together form the full per-language reader style.
# Numbered page-chunk CSS (0.css, 13.css, …) is intentionally skipped.
STYLE_CSS_NAMES = ("sab-app", "sab-annotations", "override-dab")
# sab-bc-<iso> is picked up dynamically per ISO.


@dataclass
class Asset:
    name: str   # "zai_zai.0HgVnSWZ.pkf"
    base: str   # "zai_zai"
    hash: str   # "0HgVnSWZ"
    kind: str   # "pkf" | "json" | "css" | "font"
    url: str
    ext: str = ""  # for fonts: "ttf" | "otf" | "woff" | "woff2"


def http_get(url: str, timeout: int = 30) -> bytes:
    req = Request(url, headers={"User-Agent": UA, "Accept": "*/*"})
    with urlopen(req, timeout=timeout) as r:
        return r.read()


def http_get_text(url: str, timeout: int = 30) -> str:
    return http_get(url, timeout).decode("utf-8", errors="replace")


def app_root(iso: str) -> str:
    return f"{SE_BASE}/data/{iso}/sab/{iso}"


def discover_assets(iso: str) -> list[Asset]:
    """Read the app's service-worker.js and extract .pkf + companion .json assets.

    Returns [] when the app doesn't exist or has no pkf bundled.
    """
    root = app_root(iso)
    try:
        sw = http_get_text(f"{root}/service-worker.js")
    except (HTTPError, URLError):
        return []

    pkfs: list[Asset] = []
    seen: set[tuple[str, str]] = set()
    for m in PKF_RE.finditer(sw):
        base, h = m.group(1), m.group(2)
        if (base, h) in seen:
            continue
        seen.add((base, h))
        pkfs.append(Asset(
            name=f"{base}.{h}.pkf",
            base=base,
            hash=h,
            kind="pkf",
            url=f"{root}/_app/immutable/assets/{base}.{h}.pkf",
        ))
    if not pkfs:
        return []

    pkf_bases = {p.base for p in pkfs}
    jsons: list[Asset] = []
    for m in JSON_RE.finditer(sw):
        base, h = m.group(1), m.group(2)
        if base not in pkf_bases:
            continue
        jsons.append(Asset(
            name=f"{base}.{h}.json",
            base=base,
            hash=h,
            kind="json",
            url=f"{root}/_app/immutable/assets/{base}.{h}.json",
        ))

    # CSS assets: only the named reader stylesheets, not page-chunk CSS.
    wanted_css = set(STYLE_CSS_NAMES) | {f"sab-bc-{iso}"}
    css_assets: list[Asset] = []
    css_seen: set[tuple[str, str]] = set()
    for m in CSS_RE.finditer(sw):
        base, h = m.group(1), m.group(2)
        if base not in wanted_css or (base, h) in css_seen:
            continue
        css_seen.add((base, h))
        css_assets.append(Asset(
            name=f"{base}.{h}.css",
            base=base,
            hash=h,
            kind="css",
            url=f"{root}/_app/immutable/assets/{base}.{h}.css",
        ))

    # Font assets — pull all that ship with the deployment; CSS @font-face
    # references them by hashed filename.
    font_assets: list[Asset] = []
    font_seen: set[tuple[str, str, str]] = set()
    for m in FONT_RE.finditer(sw):
        base, h, ext = m.group(1), m.group(2), m.group(3)
        if (base, h, ext) in font_seen:
            continue
        font_seen.add((base, h, ext))
        font_assets.append(Asset(
            name=f"{base}.{h}.{ext}",
            base=base,
            hash=h,
            kind="font",
            ext=ext,
            url=f"{root}/_app/immutable/assets/{base}.{h}.{ext}",
        ))

    return pkfs + jsons + css_assets + font_assets


def fetch_version(iso: str) -> str | None:
    try:
        data = http_get_text(f"{app_root(iso)}/_app/version.json")
        return json.loads(data).get("version")
    except (HTTPError, URLError, ValueError):
        return None


def discover_isos_from_country(cc: str) -> list[str]:
    html = http_get_text(f"{SE_BASE}/00eng.php?sortby=country&name={cc}")
    out, seen = [], set()
    for m in ISO_LINK_RE.finditer(html):
        iso = m.group(1)
        if iso not in seen:
            seen.add(iso)
            out.append(iso)
    return out


def load_iso_file(path: Path) -> list[str]:
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#"):
            out.append(s.split()[0])
    return out


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
    """Rewrite `url(./Name.hash.ttf)` refs to `url(./fonts/Name.hash.ttf)`."""
    return CSS_URL_REF_RE.sub(lambda m: f"url(./fonts/{m.group(1)})", css_text)


def build_style_bundle(iso: str, iso_dir: Path, css_assets: list[Asset]) -> Path | None:
    """Concatenate the reader CSS files into a single styles/bundle.css,
    rewriting font url() refs so the bundle is self-contained alongside
    fonts/ in the same directory.

    Ordering: sab-app → sab-annotations → override-dab → sab-bc-<iso>
    (the per-collection tweaks go last so they win the cascade).
    """
    order = ["sab-app", "sab-annotations", "override-dab", f"sab-bc-{iso}"]
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


def fetch_iso(iso: str, out: Path, force: bool, dry: bool) -> dict:
    result = {"iso": iso, "ok": False, "version": None, "assets": [], "error": None}
    try:
        assets = discover_assets(iso)
        if not assets:
            result["error"] = "no SAB app or no .pkf found"
            return result

        result["version"] = fetch_version(iso)
        iso_dir = out / iso

        for a in assets:
            if a.kind == "pkf" or a.kind == "json":
                target = iso_dir / a.name
            elif a.kind == "css":
                target = iso_dir / "styles" / "raw" / a.name
            elif a.kind == "font":
                target = iso_dir / "styles" / "fonts" / a.name
            else:
                target = iso_dir / a.name
            entry = {
                "name": a.name,
                "kind": a.kind,
                "base": a.base,
                "hash": a.hash,
                "url": a.url,
            }
            if dry:
                entry["action"] = "would_fetch"
            else:
                entry["action"] = download_asset(a, target, force)
                entry["size"] = target.stat().st_size
            result["assets"].append(entry)

        if not dry:
            iso_dir.mkdir(parents=True, exist_ok=True)
            css_assets = [a for a in assets if a.kind == "css"]
            bundle = build_style_bundle(iso, iso_dir, css_assets)
            info = {
                "iso": iso,
                "version": result["version"],
                "source": app_root(iso),
                "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "style_bundle": "styles/bundle.css" if bundle else None,
                "assets": result["assets"],
            }
            (iso_dir / "info.json").write_text(
                json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8"
            )

        result["ok"] = True
    except Exception as e:
        result["error"] = f"{type(e).__name__}: {e}"
    return result


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
            "version": r["version"],
            "pkfs": [a["name"] for a in r["assets"] if a["kind"] == "pkf"],
            "catalogs": [a["name"] for a in r["assets"] if a["kind"] == "json"],
            "styles": sum(1 for a in r["assets"] if a["kind"] == "css"),
            "fonts": sum(1 for a in r["assets"] if a["kind"] == "font"),
            "pkf_bytes": sum(
                a.get("size", 0) for a in r["assets"] if a["kind"] == "pkf"
            ),
        }

    manifest = {
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "languages": sorted(existing.values(), key=lambda x: x["iso"]),
    }
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def parse_args(argv: list[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="Fetch Proskomma .pkf archives from scriptureearth.org",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n"
               "  %(prog)s zai mxt trc nch\n"
               "  %(prog)s --iso-file lists/mexico.txt --workers 8\n"
               "  %(prog)s --country MX --out data/pkf\n",
    )
    ap.add_argument("iso", nargs="*", help="ISO codes (e.g. zai mxt trc)")
    ap.add_argument("--iso-file", type=Path, action="append", default=[],
                    help="File with one ISO per line (# comments allowed). Repeatable.")
    ap.add_argument("--country", action="append", default=[],
                    help="Country code (e.g. MX); discovers all ISOs listed for that "
                         "country via scriptureearth.org. Repeatable. Not every ISO has "
                         "a SAB app; missing ones are skipped.")
    ap.add_argument("--out", type=Path, default=Path("data/pkf"),
                    help="Output directory (default: data/pkf)")
    ap.add_argument("--workers", type=int, default=4,
                    help="Parallel downloads (default: 4)")
    ap.add_argument("--force", action="store_true",
                    help="Re-download even if the asset already exists")
    ap.add_argument("--dry-run", action="store_true",
                    help="List what would be fetched; do not download")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    isos: list[str] = list(args.iso)
    for f in args.iso_file:
        isos.extend(load_iso_file(f))
    for cc in args.country:
        print(f"[info] discovering ISOs for country={cc} ...", file=sys.stderr)
        discovered = discover_isos_from_country(cc)
        print(f"[info]   found {len(discovered)} candidate ISOs", file=sys.stderr)
        isos.extend(discovered)

    # dedup, preserve order
    seen: set[str] = set()
    isos = [i for i in isos if not (i in seen or seen.add(i))]

    if not isos:
        print("[error] no ISO codes provided "
              "(use positional args, --iso-file, or --country)", file=sys.stderr)
        return 2

    if not args.dry_run:
        args.out.mkdir(parents=True, exist_ok=True)

    print(f"[info] fetching {len(isos)} ISO(s) -> {args.out} "
          f"(workers={args.workers}, dry_run={args.dry_run})", file=sys.stderr)

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {
            ex.submit(fetch_iso, iso, args.out, args.force, args.dry_run): iso
            for iso in isos
        }
        for fut in as_completed(futures):
            r = fut.result()
            results.append(r)
            iso = r["iso"]
            if r["ok"]:
                pkf_count = sum(1 for a in r["assets"] if a["kind"] == "pkf")
                css_count = sum(1 for a in r["assets"] if a["kind"] == "css")
                font_count = sum(1 for a in r["assets"] if a["kind"] == "font")
                total_size = sum(a.get("size", 0) for a in r["assets"] if a["kind"] == "pkf")
                version = r["version"] or "?"
                size_kb = total_size / 1024
                print(f"  [ok]   {iso:<6} v{version}  "
                      f"{pkf_count} pkf · {css_count} css · {font_count} font  "
                      f"({size_kb:.0f} KB pkf)",
                      file=sys.stderr)
            else:
                print(f"  [skip] {iso:<6} {r['error']}", file=sys.stderr)

    if not args.dry_run:
        write_manifest(args.out, results)

    ok_count = sum(1 for r in results if r["ok"])
    print(f"[done] {ok_count}/{len(results)} OK", file=sys.stderr)
    return 0 if ok_count else 1


if __name__ == "__main__":
    sys.exit(main())
