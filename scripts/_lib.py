"""
Shared helpers for the release pipeline.

Keep this module stdlib-only. Both pack_release.py and diff_manifest.py depend
on the EXCLUDED_ISOS.txt loader; classify_licenses.py writes that file.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parent.parent
EXCLUDED_TXT = REPO_ROOT / "EXCLUDED_ISOS.txt"
EXCLUDED_PACKAGES_TXT = REPO_ROOT / "EXCLUDED_PACKAGES.txt"


def _load_first_token_set(path: Path) -> set[str]:
    """Parse a file with one token per line, # comments allowed, blanks skipped."""
    if not path.exists():
        return set()
    out: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        token = line.split()[0]
        if token:
            out.add(token)
    return out


def load_excluded_isos(path: Path = EXCLUDED_TXT) -> set[str]:
    """Read EXCLUDED_ISOS.txt and return the set of excluded ISOs."""
    return _load_first_token_set(path)


def load_excluded_packages(path: Path = EXCLUDED_PACKAGES_TXT) -> set[str]:
    """Read EXCLUDED_PACKAGES.txt and return the set of package basenames to strip."""
    return _load_first_token_set(path)


def package_base_from_filename(name: str) -> str:
    """Extract the package basename from a hashed asset filename.

    Examples:
        hch_hch.Bsy8WhX5.pkf       -> hch_hch
        spa_SPA.DRiEscWa.pkf       -> spa_SPA
        spa_SPA.CKQk4ozT.json      -> spa_SPA
    """
    return name.split(".", 1)[0]


def strippable_packages_for_iso(lang_entry: dict, excluded_packages: set[str]) -> set[str]:
    """For one manifest.languages[i] entry, return the set of strippable package
    basenames that the iso actually ships."""
    if not excluded_packages:
        return set()
    bases: set[str] = set()
    for name in lang_entry.get("pkfs", []) + lang_entry.get("catalogs", []):
        bases.add(package_base_from_filename(name))
    return bases & excluded_packages


def filter_manifest(manifest: dict, excluded: Iterable[str], excluded_packages: Iterable[str] = ()) -> dict:
    """Return a copy of the manifest with:
       - any language in `excluded` dropped entirely
       - any package basename in `excluded_packages` removed from the surviving
         languages' pkfs[] and catalogs[] arrays
    """
    excluded_isos = set(excluded)
    excluded_pkgs = set(excluded_packages)
    out_langs: list[dict] = []
    for lang in manifest.get("languages", []):
        if lang.get("iso") in excluded_isos:
            continue
        if not excluded_pkgs:
            out_langs.append(lang)
            continue
        copy = dict(lang)
        copy["pkfs"] = [n for n in lang.get("pkfs", []) if package_base_from_filename(n) not in excluded_pkgs]
        copy["catalogs"] = [n for n in lang.get("catalogs", []) if package_base_from_filename(n) not in excluded_pkgs]
        out_langs.append(copy)
    filtered = dict(manifest)
    filtered["languages"] = out_langs
    return filtered
