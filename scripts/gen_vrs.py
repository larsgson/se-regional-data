#!/usr/bin/env python3
"""Generate the deduplicated, scheme-only .vrs set for every collection.

Each collection's versification is reduced to its **scheme structure** — the
last verse of every chapter. Per-translation merges/exclusions are NOT emitted
here: they already live in the catalog's versesByChapters, so duplicating them
would make every file unique and defeat sharing.

Deduplication:
  * A collection whose structure is fully contained in one of the 6 standard
    Paratext schemes references that shared file — so every English version
    points at the same eng.vrs, no per-collection copy.
  * Everything else (deviates from all standards) gets a content-hashed file, so
    two collections with identical structure share one file.

Writes to data/_vrs/ (regenerated each run):
  eng.vrs org.vrs …    the standard schemes actually referenced
  <sha1>.vrs           custom structures, deduped by content
  index.json           { "<iso>/<collection>": "eng" | "<sha1>", … }

Every mapping is verified: the referenced file must reproduce the collection's
own footprint. Run with no arguments:  python3 scripts/gen_vrs.py
"""
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_versification import extract_schemes, footprint_from_catalog  # noqa: E402

DATA = REPO_ROOT / "data"
OUT = DATA / "_vrs"

_OT = "GEN EXO LEV NUM DEU JOS JDG RUT 1SA 2SA 1KI 2KI 1CH 2CH EZR NEH EST JOB " \
      "PSA PRO ECC SNG ISA JER LAM EZK DAN HOS JOL AMO OBA JON MIC NAM HAB ZEP " \
      "HAG ZEC MAL".split()
_NT = "MAT MRK LUK JHN ACT ROM 1CO 2CO GAL EPH PHP COL 1TH 2TH 1TI 2TI TIT PHM " \
      "HEB JAS 1PE 2PE 1JN 2JN 3JN JUD REV".split()
_DC = "TOB JDT ESG WIS SIR BAR LJE S3Y SUS BEL 1MA 2MA 3MA 4MA 1ES 2ES MAN PS2 " \
      "ODA PSS".split()
ORDER = _OT + _NT + _DC
CANON = set(ORDER)
# Standard schemes in reference priority — a collection that fits several shares
# the earliest (English first, since it is by far the most common).
PRIORITY = ["eng", "org", "lxx", "vul", "rso", "rsc"]


def render(fp: dict, name: str) -> str:
    """Footprint {(book,ch): last} -> .vrs text, canonical book order."""
    bybook: dict = {}
    for (b, c), v in fp.items():
        bybook.setdefault(b, {})[c] = v
    lines = [f'# Versification  "{name}"',
             "# Scheme structure only (last verse per chapter); per-translation",
             "# merges/exclusions live in the catalog's versesByChapters.", "#"]
    for book in ORDER:
        if book in bybook:
            chs = bybook[book]
            lines.append(f"{book} " + " ".join(f"{c}:{chs[c]}" for c in sorted(chs)))
    return "\n".join(lines) + "\n"


def canonical(fp: dict) -> str:
    return json.dumps(sorted((b, c, v) for (b, c), v in fp.items()))


def collections():
    # Read the source manifest (data/global/manifest.json, list form) so this runs
    # BEFORE pack — pack then consumes the index we write. Covers every pool
    # collection; pack only looks up the ones it actually publishes.
    man = DATA / "global" / "manifest.json"
    if not man.exists():
        sys.exit(f"[vrs] no {man} — run the pipeline first")
    for e in json.loads(man.read_text(encoding="utf-8")).get("languages", []):
        iso = e["iso"]
        for pkf, cat in zip(e.get("pkfs", []), e.get("catalogs", [])):
            yield iso, pkf.split(".")[0], cat


def main() -> int:
    schemes = extract_schemes()
    # Footprints we emit files for, keyed by ref (scheme code or content hash).
    files: dict = {}          # ref -> footprint
    index: dict = {}          # "iso/base" -> ref
    used_std, custom, nodata = set(), set(), []

    for iso, base, cat in collections():
        cat_path = DATA / "global" / iso / cat
        if not cat_path.exists():
            continue
        fp = {k: v for k, v in
              footprint_from_catalog(json.loads(cat_path.read_text(encoding="utf-8"))).items()
              if k[0] in CANON}
        if not fp:                       # empty/stub collection (no verses)
            nodata.append(f"{iso}/{base}")
            continue
        # Fully described by a standard scheme? (structure ⊆ that scheme.)
        ref = next((code for code in PRIORITY
                    if all(schemes[code].get(k) == v for k, v in fp.items())), None)
        if ref:
            used_std.add(ref)
            files.setdefault(ref, {k: v for k, v in schemes[ref].items()})
        else:                            # deviator -> content-hashed custom file
            ref = hashlib.sha1(canonical(fp).encode()).hexdigest()[:16]
            custom.add(ref)
            files.setdefault(ref, fp)
        index[f"{iso}/{base}"] = ref

    # Publish the COMPLETE standard scheme library — all six — even schemes no SE
    # collection references (lxx, vul …). Clients, or merged non-SE data, may
    # point at any standard scheme, so _vrs/<code>.vrs must always be resolvable.
    for code in PRIORITY:
        files.setdefault(code, dict(schemes[code]))

    # Write the deduped set + index.
    if OUT.exists():
        for f in OUT.glob("*"):
            f.unlink()
    OUT.mkdir(parents=True, exist_ok=True)
    for ref, fp in files.items():
        (OUT / f"{ref}.vrs").write_text(render(fp, ref), encoding="utf-8")
    (OUT / "index.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # Verify: every collection's referenced file reproduces its own footprint.
    bad = []
    for iso, base, cat in collections():
        key = f"{iso}/{base}"
        if key not in index:
            continue
        fp = {k: v for k, v in
              footprint_from_catalog(json.loads((DATA / "global" / iso / cat)
                                                 .read_text(encoding="utf-8"))).items()
              if k[0] in CANON}
        ref_fp = files[index[key]]
        if any(ref_fp.get(k) != v for k, v in fp.items()):
            bad.append(key)

    total = len(index)
    n_std = sum(1 for r in files if r in PRIORITY)
    print(f"[vrs] {total} collections -> {len(files)} files "
          f"({n_std} standard schemes [all published]; {len(custom)} custom) in {OUT}/")
    print(f"[vrs] standards referenced by SE collections: "
          f"{','.join(sorted(used_std)) or 'none'} "
          f"(lxx/vul/etc. published for cross-versification even if unreferenced)")
    print(f"[vrs] {sum(1 for r in index.values() if r in used_std)} collections share a "
          f"standard scheme; {sum(1 for r in index.values() if r in custom)} use a custom file")
    if nodata:
        print(f"[vrs] {len(nodata)} empty/stub collection(s) skipped (no verses): "
              + ", ".join(nodata[:6]) + (" …" if len(nodata) > 6 else ""))
    if bad:
        print(f"[vrs] FAIL: {len(bad)} collection(s) not reproduced by their file: "
              + ", ".join(bad[:6]), file=sys.stderr)
        return 1
    print(f"[vrs] verified: all {total} collections reproduced by their referenced file")
    return 0


if __name__ == "__main__":
    sys.exit(main())
