#!/usr/bin/env python3
"""Generate the deduplicated, scheme-only .vrs set for every collection.

Each collection's versification is reduced to its **scheme structure** — the
last verse of every chapter. Per-translation merges/exclusions are NOT emitted
here: they already live in the catalog's versesByChapters, so duplicating them
would make every file unique and defeat sharing.

Ownership split (see docs/client-data-spec.md):
  * The NAMED standard scheme files (eng/org/orgw/catm/lxx/vul/rso.vrs) are owned
    and published by the DBT versification service at a central /_vrs/ location.
    We do NOT write them — we classify SE texts against DBT's *published* files
    (fetched from VRS_BASE), so a text is labelled `eng` only if DBT's eng.vrs
    actually reproduces it.
  * Anything that matches no standard gets a content-hashed file, WHICH WE OWN
    and publish (pkf/_vrs/<sha1>.vrs). Identical structures share one file.

Writes to data/_vrs/ (regenerated each run):
  <sha1>.vrs           our custom structures, deduped by content (published)
  index.json           { "<iso>/<collection>": "eng" | "<sha1>", … }
  (the named standard files are NOT written here — DBT owns them.)

Every mapping is verified: the referenced file (DBT's standard or our hash) must
reproduce the collection's own footprint. Run: python3 scripts/gen_vrs.py
"""
import hashlib
import json
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from analyze_versification import extract_schemes, footprint_from_catalog  # noqa: E402

DATA = REPO_ROOT / "data"
OUT = DATA / "_vrs"
# The named standard scheme files are owned/published by the DBT versification
# service at this central location (its index vrs_base). We classify SE texts
# against whatever is authoritative there.
VRS_BASE = "https://cdn.bibel.wiki/_vrs/"
STD_CACHE = DATA / ".vrs-standards"

_OT = "GEN EXO LEV NUM DEU JOS JDG RUT 1SA 2SA 1KI 2KI 1CH 2CH EZR NEH EST JOB " \
      "PSA PRO ECC SNG ISA JER LAM EZK DAN HOS JOL AMO OBA JON MIC NAM HAB ZEP " \
      "HAG ZEC MAL".split()
_NT = "MAT MRK LUK JHN ACT ROM 1CO 2CO GAL EPH PHP COL 1TH 2TH 1TI 2TI TIT PHM " \
      "HEB JAS 1PE 2PE 1JN 2JN 3JN JUD REV".split()
_DC = "TOB JDT ESG WIS SIR BAR LJE S3Y SUS BEL 1MA 2MA 3MA 4MA 1ES 2ES MAN PS2 " \
      "ODA PSS".split()
ORDER = _OT + _NT + _DC
CANON = set(ORDER)
# DBT's named standard schemes, in reference priority — a collection that fits
# several shares the earliest (baselines eng/org first, then the org-western /
# catholic-masoretic variants, then the LXX-numbered family). rsc was retired;
# orgw and catm were added by the DBT service.
PRIORITY = ["eng", "org", "orgw", "catm", "lxx", "vul", "rso"]


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


def parse_vrs(text: str) -> dict:
    """{(book,ch): last} from a .vrs (max-verse lines; comments and = maps skipped)."""
    import re
    fp: dict = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" in line:
            continue
        toks = line.split()
        if not re.fullmatch(r"[A-Z0-9]{3}", toks[0]):
            continue
        for t in toks[1:]:
            m = re.fullmatch(r"(\d+):(\d+)", t)
            if m:
                fp[(toks[0], int(m.group(1)))] = int(m.group(2))
    return fp


def load_standards() -> dict:
    """{code: footprint} for the 6 standard schemes, from DBT's *published* files
    (the authoritative owner) with a local cache and SAB-bundle fallback, so our
    labelling stays consistent with whatever those files actually contain."""
    STD_CACHE.mkdir(parents=True, exist_ok=True)
    sab = None
    out: dict = {}
    fetched = []
    for code in PRIORITY:
        cache = STD_CACHE / f"{code}.vrs"
        text = None
        try:
            req = urllib.request.Request(f"{VRS_BASE}{code}.vrs",
                                         headers={"User-Agent": "se-regional-data/1.0"})
            text = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
            cache.write_text(text, encoding="utf-8")
            fetched.append(code)
        except Exception:                       # noqa: BLE001 — fall back gracefully
            if cache.exists():
                text = cache.read_text(encoding="utf-8")
        if text is not None:
            out[code] = parse_vrs(text)
        else:                                    # never fetched, no cache
            if sab is None:
                sab = extract_schemes()
            if code in sab:                      # SAB bundle covers the classic 6
                out[code] = dict(sab[code])
            # else: DBT-only scheme (orgw/catm) unavailable offline — omit it;
            # texts that would match it fall back to one of our hashed files.
    print(f"[vrs] standard schemes: {len(fetched)}/{len(PRIORITY)} fetched from {VRS_BASE} "
          f"({','.join(fetched) or 'none'}); {sorted(set(PRIORITY) - set(out))} unavailable")
    return out


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
    standards = load_standards()          # {code: footprint} — DBT-owned files
    custom_files: dict = {}               # hash -> footprint (WE own + publish)
    index: dict = {}                      # "iso/base" -> "eng" | "<hash>"
    used_std, nodata = set(), []

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
        # Fully described by a standard scheme? (structure ⊆ that DBT scheme.)
        ref = next((code for code in PRIORITY
                    if code in standards
                    and all(standards[code].get(k) == v for k, v in fp.items())), None)
        if ref:
            used_std.add(ref)            # reference DBT's file; we don't write it
        else:                            # deviator -> our content-hashed custom file
            ref = hashlib.sha1(canonical(fp).encode()).hexdigest()[:16]
            custom_files.setdefault(ref, fp)
        index[f"{iso}/{base}"] = ref

    # Write ONLY our custom hashed files + the index. The named standard files are
    # DBT-owned and deliberately NOT written here (see module docstring).
    if OUT.exists():
        for f in OUT.glob("*"):
            f.unlink()
    OUT.mkdir(parents=True, exist_ok=True)
    for ref, fp in custom_files.items():
        (OUT / f"{ref}.vrs").write_text(render(fp, ref), encoding="utf-8")
    (OUT / "index.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    # Verify: every collection's referenced file reproduces its own footprint —
    # standard refs against DBT's fetched footprints, custom refs against our files.
    bad = []
    for iso, base, cat in collections():
        key = f"{iso}/{base}"
        if key not in index:
            continue
        fp = {k: v for k, v in
              footprint_from_catalog(json.loads((DATA / "global" / iso / cat)
                                                 .read_text(encoding="utf-8"))).items()
              if k[0] in CANON}
        ref = index[key]
        ref_fp = standards[ref] if ref in standards else custom_files[ref]
        if any(ref_fp.get(k) != v for k, v in fp.items()):
            bad.append(key)

    total = len(index)
    print(f"[vrs] {total} collections -> {len(custom_files)} custom files (we publish) + "
          f"standard refs to DBT-owned files in {OUT}/")
    print(f"[vrs] {sum(1 for r in index.values() if r in used_std)} collections reference a "
          f"DBT standard ({','.join(sorted(used_std)) or 'none'}); "
          f"{sum(1 for r in index.values() if r not in used_std)} use our custom file")
    if nodata:
        print(f"[vrs] {len(nodata)} empty/stub collection(s) skipped (no verses): "
              + ", ".join(nodata[:6]) + (" …" if len(nodata) > 6 else ""))
    if bad:
        print(f"[vrs] FAIL: {len(bad)} collection(s) not reproduced by their referenced "
              f"file (standard definition may have changed): "
              + ", ".join(bad[:6]), file=sys.stderr)
        return 1
    print(f"[vrs] verified: all {total} collections reproduced by their referenced file")
    return 0


if __name__ == "__main__":
    sys.exit(main())
