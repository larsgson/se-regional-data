#!/usr/bin/env python3
"""
Compute an incremental upload plan for scripts/publish-cdn.sh, so a publish
doesn't have to list + diff the entire remote pkf/ tree (tens of thousands of
tiny timing files) on every run — that remote listing, not the actual upload,
is what makes repeat publishes slow.

Maintains a local state file (data/.cdn-published.json) recording the
(size, mtime) of every file included in the last successful publish. Uses the
SAME exclusion rules as pack_release.py / publish-cdn.sh (EXCLUDED_ISOS.txt +
EXCLUDED_PACKAGES.txt), so the plan never includes a non-CC ISO.

Subcommands:
    plan    walk the source tree, diff file-by-file against the state file,
             and write rclone --files-from lists split by cache tier (assets /
             info / timing) into --out-dir. Unchanged files are skipped
             entirely — the "diff" is local stat() calls, not a remote list.

    commit  re-walk the source tree and persist the full current
             (path -> size, mtime) map as the new state. Call this only after
             all rclone passes from the matching `plan` succeeded.

    forget  drop every state entry under a given ISO prefix. Used by
             publish-cdn.sh's self-heal purge step, so a later un-exclusion
             of that ISO is treated as new rather than already-published.

Usage:
    scripts/cdn_delta.py plan   --src data/global --state data/.cdn-published.json --out-dir /tmp/cdn-delta
    scripts/cdn_delta.py commit --src data/global --state data/.cdn-published.json
    scripts/cdn_delta.py forget --state data/.cdn-published.json --iso cya
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import load_excluded_isos, load_excluded_packages  # noqa: E402


def load_state(state_path: Path) -> dict:
    if state_path.exists():
        try:
            return json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            pass
    return {"version": 1, "files": {}}


def save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, separators=(",", ":")), encoding="utf-8")


def iter_included_files(src: Path):
    """Yield (relpath, abspath) for every file publish-cdn.sh would upload:
    same exclusions as pack_release.py (whole-ISO drop + package-basename
    strip), and skipping the on-disk (unfiltered) manifest.json at the root —
    that one is handled separately, from the filtered release asset."""
    excluded_isos = load_excluded_isos()
    excluded_pkgs = load_excluded_packages()

    for entry in sorted(src.iterdir()):
        if entry.name == "manifest.json":
            continue
        if not entry.is_dir():
            continue
        if entry.name in excluded_isos:
            continue
        for root, _dirs, files in os.walk(entry, followlinks=True):
            for fname in files:
                if any(fname.startswith(f"{base}.") for base in excluded_pkgs):
                    continue
                abspath = Path(root) / fname
                relpath = abspath.relative_to(src)
                yield str(relpath), abspath


def classify_tier(relpath: str) -> str:
    """assets | info | timing — mirrors publish-cdn.sh's three upload passes.

    'info' = mutable, short-TTL files that change in place each release:
    per-iso info.json + app-config.json + license-notice.html + styles/delta.css,
    and the shared _app/nav-base.json + _app/nav-<code>.json locales and
    _styles/sab-scripture.css (stable path) + _styles/index.json. 'assets' =
    content-hashed, cache-forever (incl. _styles/sab-scripture.<sha8>.css and
    styles/raw/*). 'timing' = its own moderate TTL."""
    if (relpath.endswith("/info.json")
            or relpath.endswith("/styles/delta.css")
            or relpath in ("_styles/sab-scripture.css", "_styles/index.json")
            or relpath.endswith("/app-config.json")
            or relpath.endswith("/license-notice.html")
            or (relpath.startswith("_app/nav-") and relpath.endswith(".json"))):
        return "info"
    if "/timing/" in relpath:
        return "timing"
    return "assets"


def cmd_plan(args: argparse.Namespace) -> int:
    src = Path(args.src).resolve()
    state_path = Path(args.state)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    known = load_state(state_path).get("files", {})

    lists: dict[str, list[str]] = {"assets": [], "info": [], "timing": []}
    total = 0
    for relpath, abspath in iter_included_files(src):
        total += 1
        st = abspath.stat()
        fp = known.get(relpath)
        if fp and fp[0] == st.st_size and abs(fp[1] - st.st_mtime) < 1.0:
            continue  # unchanged since the last successful commit
        lists[classify_tier(relpath)].append(relpath)

    changed = sum(len(v) for v in lists.values())
    for tier, paths in lists.items():
        text = "\n".join(paths) + ("\n" if paths else "")
        (out_dir / f"{tier}.txt").write_text(text, encoding="utf-8")

    print(f"[cdn-delta] {total} local files, {changed} new/changed "
          f"(assets={len(lists['assets'])} info={len(lists['info'])} "
          f"timing={len(lists['timing'])})", file=sys.stderr)
    return 0


def cmd_commit(args: argparse.Namespace) -> int:
    src = Path(args.src).resolve()
    state_path = Path(args.state)
    files: dict[str, list] = {}
    for relpath, abspath in iter_included_files(src):
        st = abspath.stat()
        files[relpath] = [st.st_size, st.st_mtime]
    save_state(state_path, {"version": 1, "files": files})
    print(f"[cdn-delta] committed {len(files)} file(s) to {state_path}", file=sys.stderr)
    return 0


def cmd_forget(args: argparse.Namespace) -> int:
    state_path = Path(args.state)
    state = load_state(state_path)
    prefix = f"{args.iso}/"
    before = len(state["files"])
    state["files"] = {k: v for k, v in state["files"].items() if not k.startswith(prefix)}
    removed = before - len(state["files"])
    save_state(state_path, state)
    plural = "y" if removed == 1 else "ies"
    print(f"[cdn-delta] forgot {removed} entr{plural} under {args.iso}/", file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_plan = sub.add_parser("plan")
    p_plan.add_argument("--src", required=True)
    p_plan.add_argument("--state", required=True)
    p_plan.add_argument("--out-dir", required=True)
    p_plan.set_defaults(func=cmd_plan)

    p_commit = sub.add_parser("commit")
    p_commit.add_argument("--src", required=True)
    p_commit.add_argument("--state", required=True)
    p_commit.set_defaults(func=cmd_commit)

    p_forget = sub.add_parser("forget")
    p_forget.add_argument("--state", required=True)
    p_forget.add_argument("--iso", required=True)
    p_forget.set_defaults(func=cmd_forget)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
