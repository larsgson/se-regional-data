#!/usr/bin/env python3
"""
Diff this run's data/pkf/manifest.json against the previous release's
manifest asset and write release-notes.md.

Resolves "previous release" via `gh release list`. The previous release's
manifest.json is downloaded as a sibling asset (uploaded by pack_release).
If no previous release exists (first run, or asset missing), the notes
just report totals.

Env:
    COUNTRY=mx           filter prior releases by tag/asset prefix
    SKIP_TAG=<tag>       skip the named release (for regenerating notes
                         for an already-published release; prevents the
                         release from being diffed against itself)

Usage:
    scripts/diff_manifest.py                  # writes ./release/release-notes.md
    scripts/diff_manifest.py --out path
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import REPO_ROOT, load_excluded_isos, filter_manifest  # noqa: E402

COUNTRY = os.environ.get("COUNTRY", "global").lower()
CURRENT_MANIFEST = REPO_ROOT / "data" / COUNTRY / "manifest.json"


def gh(args: list[str]) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} failed: {r.stderr.strip() or r.stdout.strip()}")
    return r.stdout


def find_previous_manifest() -> dict | None:
    """Return {'tag': str, 'manifest': dict} of the most-recent non-draft
    release with a manifest asset (skipping SKIP_TAG), or None."""
    skip_tag = os.environ.get("SKIP_TAG")
    try:
        listing = gh(["release", "list", "--limit", "20", "--json", "tagName,createdAt,isDraft"])
    except RuntimeError as e:
        print(f"[diff] couldn't list releases: {e}", file=sys.stderr)
        return None
    releases = [
        r for r in json.loads(listing)
        if not r["isDraft"] and r["tagName"] != skip_tag
    ]
    for r in releases:
        try:
            assets = json.loads(gh(["release", "view", r["tagName"], "--json", "assets"]))["assets"]
        except RuntimeError:
            continue
        match = next(
            (
                a for a in assets
                if a["name"].startswith(f"manifest-{COUNTRY}-") and a["name"].endswith(".json")
            ),
            None,
        )
        if not match:
            continue
        with tempfile.TemporaryDirectory(prefix="sermd-") as tmp:
            try:
                gh(["release", "download", r["tagName"], "-p", match["name"], "-D", tmp])
                text = (Path(tmp) / match["name"]).read_text(encoding="utf-8")
                return {"tag": r["tagName"], "manifest": json.loads(text)}
            except RuntimeError as e:
                print(f"[diff] couldn't download {match['name']} from {r['tagName']}: {e}", file=sys.stderr)
    return None


def index_by_iso(manifest: dict) -> dict[str, dict]:
    # `languages` is an iso-keyed map in the new compact manifest, a list of
    # {iso, ...} entries in the older form. Normalise both to {iso: entry}.
    langs = manifest.get("languages", {})
    if isinstance(langs, dict):
        return dict(langs)
    return {l["iso"]: l for l in langs}


def _pkfs(entry: dict) -> set:
    """Set of .pkf filenames for an entry, from either manifest form."""
    if "collections" in entry:
        return {c.get("pkf") for c in entry["collections"] if c.get("pkf")}
    return set(entry.get("pkfs", []))


def _entry_bytes(entry: dict) -> int:
    if "collections" in entry:
        return sum(c.get("pkf_bytes", 0) for c in entry["collections"])
    return entry.get("pkf_bytes", 0)


def fmt_bytes(n: int) -> str:
    if not n:
        return "0 B"
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KiB"
    return f"{n / (1024 * 1024):.1f} MiB"


def totals(manifest: dict) -> dict:
    idx = index_by_iso(manifest)
    return {"count": len(idx), "bytes": sum(_entry_bytes(e) for e in idx.values())}


def build_notes(current: dict, previous: dict | None, previous_tag: str | None) -> str:
    t_cur = totals(current)
    lines = [
        f"# Scripture Earth data — {COUNTRY.upper()}",
        "",
        f"Updated **{current.get('updated_at', datetime.now(timezone.utc).isoformat())}**",
        "",
        f"- **Languages:** {t_cur['count']}",
        f"- **Total .pkf bytes:** {fmt_bytes(t_cur['bytes'])}",
        "",
    ]
    if not previous:
        lines.append("_First release — no previous manifest to diff against._")
        return "\n".join(lines) + "\n"

    prev_idx = index_by_iso(previous)
    cur_idx = index_by_iso(current)
    all_isos = sorted(set(prev_idx) | set(cur_idx))

    added: list[str] = []
    removed: list[str] = []
    changed: list[str] = []
    for iso in all_isos:
        a = prev_idx.get(iso)
        b = cur_idx.get(iso)
        if not a and b:
            added.append(iso)
        elif a and not b:
            removed.append(iso)
        elif a and b and _pkfs(a) != _pkfs(b):
            # .pkf filenames are content-hashed, so a differing set means the
            # shipped text changed (the compact manifest carries no version).
            changed.append(iso)

    lines.extend(
        [
            f"## Changes since `{previous_tag}`",
            "",
            f"- Added: **{len(added)}**",
            f"- Removed: **{len(removed)}**",
            f"- Updated: **{len(changed)}**",
            "",
        ]
    )
    if added:
        lines.append("### Added languages")
        for iso in added:
            lines.append(f"- `{iso}`")
        lines.append("")
    if removed:
        lines.append("### Removed languages")
        for iso in removed:
            lines.append(f"- `{iso}`")
        lines.append("")
    if changed:
        lines.append("### Updated languages")
        for iso in changed:
            lines.append(f"- `{iso}`")
        lines.append("")
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(REPO_ROOT / "release" / "release-notes.md"))
    args = ap.parse_args()

    if not CURRENT_MANIFEST.exists():
        print(f"[diff] missing {CURRENT_MANIFEST}; run fetch_pkf.py first", file=sys.stderr)
        return 1
    excluded = load_excluded_isos()
    current = filter_manifest(json.loads(CURRENT_MANIFEST.read_text(encoding="utf-8")), excluded)

    # Previous manifest is the already-shipped asset — pack_release.py applied
    # the *then-current* exclusion list at pack time. Do NOT re-filter it with
    # the *now-current* list, or newly-excluded ISOs would be stripped from
    # both sides of the diff and falsely appear unchanged.
    prev = find_previous_manifest()
    previous = prev["manifest"] if prev else None
    notes = build_notes(current, previous, prev["tag"] if prev else None)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(notes, encoding="utf-8")
    suffix = f" (excluded {len(excluded)} ISO[s])" if excluded else ""
    print(f"[diff] wrote {out}{suffix}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
