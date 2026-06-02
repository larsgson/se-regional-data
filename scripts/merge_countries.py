#!/usr/bin/env python3
"""
Merge per-country data directories into a single combined directory.

Reads a conf file (country codes or nested .conf references), unions all ISOs
from the listed countries into data/<merged>/ using symlinks, and writes a
merged manifest.json. The result can be packed/released like any single-country
directory.

Usage:
    scripts/merge_countries.py conf/central-america.conf latam
    scripts/merge_countries.py conf/global.conf global
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"


def parse_conf(conf_path: Path) -> list[str]:
    """Parse a conf file into country codes, expanding nested .conf references."""
    countries: list[str] = []
    for raw in conf_path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if line.endswith(".conf"):
            nested = REPO_ROOT / line
            if nested.exists():
                countries.extend(parse_conf(nested))
            else:
                print(f"  warning: {line} not found, skipping", file=sys.stderr)
        else:
            countries.append(line.split()[0].lower())
    return countries


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: merge_countries.py <conf-file> <merged-name>", file=sys.stderr)
        return 1

    conf_path = Path(sys.argv[1])
    if not conf_path.is_absolute():
        conf_path = REPO_ROOT / conf_path
    merged_name = sys.argv[2].lower()
    merged_dir = DATA_DIR / merged_name

    countries = list(dict.fromkeys(parse_conf(conf_path)))
    if not countries:
        print("error: no countries found in conf", file=sys.stderr)
        return 1

    print(f"[merge] {len(countries)} countries: {' '.join(countries)}")

    pool_dir = DATA_DIR / "_pool"
    merged_langs: dict[str, dict] = {}

    for cc in countries:
        cc_dir = DATA_DIR / cc
        manifest_path = cc_dir / "manifest.json"
        if not manifest_path.exists():
            print(f"  {cc}: no manifest.json, skipping (run pipeline first)")
            continue

        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        count = 0
        for lang in manifest.get("languages", []):
            iso = lang["iso"]
            iso_dir = pool_dir / iso
            if not iso_dir.is_dir():
                continue
            if iso not in merged_langs:
                merged_langs[iso] = lang
                count += 1
        print(f"  {cc}: {len(manifest.get('languages', []))} ISOs ({count} new)")

    if not merged_langs:
        print("error: no ISOs found across any country", file=sys.stderr)
        return 1

    merged_dir.mkdir(parents=True, exist_ok=True)

    # Clean existing symlinks in merged dir
    for p in merged_dir.iterdir():
        if p.is_symlink():
            p.unlink()

    # Create symlinks from merged dir to pool
    for iso in sorted(merged_langs):
        link = merged_dir / iso
        if link.is_symlink():
            link.unlink()
        elif link.is_dir():
            continue
        target = os.path.relpath(pool_dir / iso, merged_dir)
        link.symlink_to(target)

    # Symlink _fonts from pool
    fonts_link = merged_dir / "_fonts"
    if fonts_link.is_symlink():
        fonts_link.unlink()
    fonts_src = pool_dir / "_fonts"
    if fonts_src.is_dir():
        fonts_link.symlink_to(os.path.relpath(fonts_src, merged_dir))

    # Write merged manifest
    merged_manifest = {
        "updated_at": max(
            (json.loads((DATA_DIR / cc / "manifest.json").read_text(encoding="utf-8")).get("updated_at", "")
             for cc in countries if (DATA_DIR / cc / "manifest.json").exists()),
            default="",
        ),
        "languages": sorted(merged_langs.values(), key=lambda l: l["iso"]),
    }
    (merged_dir / "manifest.json").write_text(
        json.dumps(merged_manifest, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    print(f"\n[merge] → data/{merged_name}/ : {len(merged_langs)} unique ISOs from {len(countries)} countries")
    return 0


if __name__ == "__main__":
    sys.exit(main())
