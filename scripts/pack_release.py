#!/usr/bin/env python3
"""
Package data/pkf/ into a release artifact:
  - pkf-<country>-<YYYYMMDD>.tar.zst of data/pkf/*
  - manifest-<country>-<YYYYMMDD>.json (sibling copy of data/pkf/manifest.json, filtered)
  - licenses-<country>-<YYYYMMDD>.json (sibling copy of data/pkf/licenses.json)
  - index.json: { version, created_at, bytes, sha256, tag, asset, manifest_asset, licenses_asset, … }

Usage:
    scripts/pack_release.py                    # defaults: country=mx
    COUNTRY=mx TAG=data-2026.04.23 scripts/pack_release.py

Writes into ./release/ (gitignored). Safe to re-run; overwrites the staging
directory each time.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import (  # noqa: E402
    REPO_ROOT,
    filter_manifest,
    load_excluded_isos,
    load_excluded_packages,
    package_base_from_filename,
    strippable_packages_for_iso,
)

COUNTRY = os.environ.get("COUNTRY", "mx").lower()
PKF_ROOT = REPO_ROOT / "data" / "pkf"
STAGE = REPO_ROOT / "release"
CREATED_AT = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
YMD = CREATED_AT[:10].replace("-", "")
TAG = os.environ.get("TAG", f"data-{CREATED_AT[:10].replace('-', '.')}")

ASSET = f"pkf-{COUNTRY}-{YMD}.tar.zst"
MANIFEST_ASSET = f"manifest-{COUNTRY}-{YMD}.json"
LICENSES_ASSET = f"licenses-{COUNTRY}-{YMD}.json"


def sha256_of_file(path: Path) -> str:
    h = hashlib.sha256()
    h.update(path.read_bytes())
    return h.hexdigest()


def summarize_manifest(manifest: dict) -> dict:
    langs = manifest.get("languages", [])
    return {
        "languages": len(langs),
        "pkf_bytes_total": sum(l.get("pkf_bytes", 0) for l in langs),
        "updated_at": manifest.get("updated_at"),
    }


def main() -> int:
    if not PKF_ROOT.is_dir():
        print(f"[pack] missing {PKF_ROOT}; run the fetch pipeline first", file=sys.stderr)
        return 1
    manifest_path = PKF_ROOT / "manifest.json"
    if not manifest_path.exists():
        print(f"[pack] missing {manifest_path}; fetch_pkf.py didn't complete", file=sys.stderr)
        return 1

    excluded = load_excluded_isos()
    excluded_packages = load_excluded_packages()

    # Stage dir.
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)

    # Per-iso package strip plan: { iso: {"spa_SPA", ...} }. Computed from the
    # manifest's per-iso pkfs/catalogs lists. Only isos that actually ship a
    # listed package land in this map.
    full_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    strippable_per_iso: dict[str, set[str]] = {}
    for lang in full_manifest.get("languages", []):
        if lang.get("iso") in excluded:
            continue  # iso is fully dropped; per-package filtering is moot
        s = strippable_packages_for_iso(lang, excluded_packages)
        if s:
            strippable_per_iso[lang["iso"]] = s

    # Compute filtered manifest once — used both inside the tarball and as the
    # sibling release asset. The on-disk data/pkf/manifest.json is left alone
    # (that's fetch_pkf.py's output; the pipeline re-uses it across runs).
    filtered = filter_manifest(full_manifest, excluded, excluded_packages)
    filtered_json = json.dumps(filtered, indent=2, ensure_ascii=False)

    tar_path = STAGE / ASSET
    zstd_level = int(os.environ.get("ZSTD_LEVEL", "19"))
    if excluded:
        print(
            f"[pack] excluding {len(excluded)} ISO(s) per EXCLUDED_ISOS.txt: "
            f"{', '.join(sorted(excluded))}"
        )
    if strippable_per_iso:
        listing = ", ".join(
            f"{iso}/[{','.join(sorted(bases))}]" for iso, bases in sorted(strippable_per_iso.items())
        )
        print(f"[pack] stripping companion packages per EXCLUDED_PACKAGES.txt: {listing}")
    print(f"[pack] building {tar_path} from {PKF_ROOT}/ (zstd -{zstd_level}) ...")

    # Build tar exclude list:
    #   - whole iso dirs for fully-excluded isos
    #   - per-iso package globs for strippable companion packages
    exclude_flags = [f"--exclude=./{iso}" for iso in sorted(excluded)]
    for iso, bases in sorted(strippable_per_iso.items()):
        for base in sorted(bases):
            exclude_flags.append(f"--exclude=./{iso}/{base}.*")

    # Swap-and-restore plan for filtered files: each (path, original_bytes,
    # filtered_bytes) gets written to disk for the duration of the tar, then
    # restored in finally. Wrap manifest.json + each affected info.json.
    swaps: list[tuple[Path, bytes, bytes]] = [
        (manifest_path, manifest_path.read_bytes(), filtered_json.encode("utf-8"))
    ]
    for iso, bases in strippable_per_iso.items():
        info_path = PKF_ROOT / iso / "info.json"
        if not info_path.exists():
            continue
        original = info_path.read_bytes()
        info = json.loads(original.decode("utf-8"))
        info["assets"] = [
            a for a in info.get("assets", [])
            if package_base_from_filename(a.get("name", "")) not in bases
            and a.get("base") not in bases
        ]
        swaps.append(
            (info_path, original, json.dumps(info, indent=2, ensure_ascii=False).encode("utf-8"))
        )

    tar_cmd = ["tar", *exclude_flags, "-cf", "-", "-C", str(PKF_ROOT), "."]
    zstd_cmd = ["zstd", f"-{zstd_level}", "-T0", "-q", "-o", str(tar_path)]
    try:
        for path, _orig, filtered_bytes in swaps:
            path.write_bytes(filtered_bytes)
        tar_proc = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE)
        zstd_proc = subprocess.Popen(zstd_cmd, stdin=tar_proc.stdout)
        if tar_proc.stdout is not None:
            tar_proc.stdout.close()
        zstd_rc = zstd_proc.wait()
        tar_rc = tar_proc.wait()
    finally:
        for path, original, _filtered in swaps:
            path.write_bytes(original)
    if tar_rc != 0 or zstd_rc != 0:
        print(f"[pack] tar|zstd failed (tar={tar_rc}, zstd={zstd_rc})", file=sys.stderr)
        return 1

    # Defensive leak-check: verify no excluded iso AND no strippable package
    # leaked into the tarball.
    listing = subprocess.run(
        f"zstd -dc {tar_path} | tar -tf -",
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    ).stdout.splitlines()
    if excluded:
        leaks = [
            iso for iso in excluded
            if any(p == f"./{iso}/" or p.startswith(f"./{iso}/") for p in listing)
        ]
        if leaks:
            print(f"[pack] FATAL: excluded ISO(s) found in tar: {', '.join(leaks)}", file=sys.stderr)
            return 1
    pkg_leaks: list[str] = []
    for iso, bases in strippable_per_iso.items():
        for base in bases:
            prefix = f"./{iso}/{base}."
            for p in listing:
                if p.startswith(prefix):
                    pkg_leaks.append(p)
    if pkg_leaks:
        print(
            f"[pack] FATAL: strippable companion packages found in tar: {', '.join(pkg_leaks[:10])}",
            file=sys.stderr,
        )
        return 1
    embedded = subprocess.run(
        f"zstd -dc {tar_path} | tar -xOf - ./manifest.json",
        shell=True,
        capture_output=True,
        text=True,
        check=False,
    ).stdout
    try:
        embedded_manifest = json.loads(embedded)
    except json.JSONDecodeError:
        embedded_manifest = None
    embedded_count = len(embedded_manifest.get("languages", [])) if embedded_manifest else -1
    expected_count = len(filtered.get("languages", []))
    if embedded_count != expected_count:
        print(
            f"[pack] FATAL: embedded manifest.json has {embedded_count} languages, "
            f"expected {expected_count}",
            file=sys.stderr,
        )
        return 1

    # Stage the sibling manifest release asset from the same filtered bytes.
    (STAGE / MANIFEST_ASSET).write_text(filtered_json, encoding="utf-8")

    # Stage licenses.json (required — the classifier writes it).
    licenses_src = PKF_ROOT / "licenses.json"
    if not licenses_src.exists():
        print(f"[pack] missing {licenses_src}; run classify_licenses.py first", file=sys.stderr)
        return 1
    licenses_dst = STAGE / LICENSES_ASSET
    shutil.copyfile(licenses_src, licenses_dst)
    licenses_doc = json.loads(licenses_src.read_text(encoding="utf-8"))

    size = tar_path.stat().st_size
    sha256 = sha256_of_file(tar_path)
    summary = summarize_manifest(filtered)

    index = {
        "version": TAG,
        "tag": TAG,
        "country": COUNTRY,
        "created_at": CREATED_AT,
        "asset": ASSET,
        "manifest_asset": MANIFEST_ASSET,
        "licenses_asset": LICENSES_ASSET,
        "bytes": size,
        "sha256": sha256,
        "summary": summary,
        "excluded_isos": sorted(excluded),
        "stripped_packages": {
            iso: sorted(bases) for iso, bases in sorted(strippable_per_iso.items())
        },
        "licenses_summary": {
            "included": licenses_doc.get("included_count"),
            "excluded": licenses_doc.get("excluded_count"),
            "default_license": licenses_doc.get("default_license"),
        },
    }
    (STAGE / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")

    mb = size / (1024 * 1024)
    print(f"[pack] {ASSET}  {mb:.1f} MB  sha256={sha256[:16]}…")
    pkg_count = sum(len(b) for b in strippable_per_iso.values())
    print(
        f"[pack] tag={TAG}  languages={summary['languages']} "
        f"(iso-excluded {len(excluded)}, classifier-excluded {licenses_doc.get('excluded_count')}, "
        f"package-stripped {pkg_count} from {len(strippable_per_iso)} iso(s))"
    )
    print(f"[pack] staged in ./{STAGE.relative_to(REPO_ROOT)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
