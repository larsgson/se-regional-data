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


def load_excluded_isos(path: Path = EXCLUDED_TXT) -> set[str]:
    """Read EXCLUDED_ISOS.txt and return the set of excluded ISOs.

    File format: one ISO per line. Blank lines and `#` comments are ignored.
    An inline `# …` comment after the ISO is allowed.
    """
    if not path.exists():
        return set()
    out: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        iso = line.split()[0]
        if iso:
            out.add(iso)
    return out


def filter_manifest(manifest: dict, excluded: Iterable[str]) -> dict:
    """Return a copy of the manifest with any language in `excluded` dropped."""
    excluded_set = set(excluded)
    if not excluded_set:
        return manifest
    filtered = dict(manifest)
    filtered["languages"] = [
        l for l in manifest.get("languages", []) if l.get("iso") not in excluded_set
    ]
    return filtered
