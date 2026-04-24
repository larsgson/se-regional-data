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
from _lib import REPO_ROOT, load_excluded_isos, filter_manifest  # noqa: E402

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

    # Stage dir.
    if STAGE.exists():
        shutil.rmtree(STAGE)
    STAGE.mkdir(parents=True)

    # Compute filtered manifest once — used both inside the tarball and as the
    # sibling release asset. The on-disk data/pkf/manifest.json is left alone
    # (that's fetch_pkf.py's output; the pipeline re-uses it across runs).
    full_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    filtered = filter_manifest(full_manifest, excluded)
    filtered_json = json.dumps(filtered, indent=2, ensure_ascii=False)

    tar_path = STAGE / ASSET
    zstd_level = int(os.environ.get("ZSTD_LEVEL", "19"))
    if excluded:
        print(
            f"[pack] excluding {len(excluded)} ISO(s) per EXCLUDED_ISOS.txt: "
            f"{', '.join(sorted(excluded))}"
        )
    print(f"[pack] building {tar_path} from {PKF_ROOT}/ (zstd -{zstd_level}) ...")

    # Swap the on-disk manifest.json with the filtered version for the
    # duration of the tar, then restore. This way the manifest.json *inside*
    # the tarball lists only the 134 included isos — consumers who extract
    # and never look at the sibling asset still see the correct view.
    # Wrapped in try/finally so a crash can't leave the filtered version
    # in place of the 139-iso original.
    exclude_flags = [f"--exclude=./{iso}" for iso in sorted(excluded)]
    tar_cmd = ["tar", *exclude_flags, "-cf", "-", "-C", str(PKF_ROOT), "."]
    zstd_cmd = ["zstd", f"-{zstd_level}", "-T0", "-q", "-o", str(tar_path)]
    original_manifest_bytes = manifest_path.read_bytes()
    try:
        manifest_path.write_text(filtered_json, encoding="utf-8")
        tar_proc = subprocess.Popen(tar_cmd, stdout=subprocess.PIPE)
        zstd_proc = subprocess.Popen(zstd_cmd, stdin=tar_proc.stdout)
        if tar_proc.stdout is not None:
            tar_proc.stdout.close()
        zstd_rc = zstd_proc.wait()
        tar_rc = tar_proc.wait()
    finally:
        manifest_path.write_bytes(original_manifest_bytes)
    if tar_rc != 0 or zstd_rc != 0:
        print(f"[pack] tar|zstd failed (tar={tar_rc}, zstd={zstd_rc})", file=sys.stderr)
        return 1

    # Defensive leak-check: verify no excluded iso made it into the tarball,
    # and that the embedded manifest.json matches the filtered iso count.
    if excluded:
        listing = subprocess.run(
            f"zstd -dc {tar_path} | tar -tf - | head -2000",
            shell=True,
            capture_output=True,
            text=True,
            check=False,
        ).stdout
        leaks = [
            iso for iso in excluded
            if any(p == f"./{iso}/" or p.startswith(f"./{iso}/") for p in listing.splitlines())
        ]
        if leaks:
            print(f"[pack] FATAL: excluded ISO(s) found in tar: {', '.join(leaks)}", file=sys.stderr)
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
        "licenses_summary": {
            "included": licenses_doc.get("included_count"),
            "excluded": licenses_doc.get("excluded_count"),
            "default_license": licenses_doc.get("default_license"),
        },
    }
    (STAGE / "index.json").write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")

    mb = size / (1024 * 1024)
    print(f"[pack] {ASSET}  {mb:.1f} MB  sha256={sha256[:16]}…")
    print(
        f"[pack] tag={TAG}  languages={summary['languages']} "
        f"(excluded {len(excluded)}, classifier-excluded {licenses_doc.get('excluded_count')})"
    )
    print(f"[pack] staged in ./{STAGE.relative_to(REPO_ROOT)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
