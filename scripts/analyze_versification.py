#!/usr/bin/env python3
"""Fingerprint each ISO's actual verse structure against the 6 bundled
versification schemes and report how confidently each resolves.

The chosen scheme is NOT stored in our data (every catalog has hasMapping=False
and the app-definition carries no versification field). But two things make it
derivable:

  * The 6 scheme definitions ship as .vrs template-string constants inside every
    app chunk (data/.media-cache/<iso>*.js) — we extract them once. Each gives a
    book -> chapter -> last-verse footprint.
  * Each ISO's real per-book verse structure is in its catalog's
    versesByChapters — we reduce it to the same footprint (max verse NUMBER per
    chapter, which survives local verse merges like "14-15").

Matching the ISO footprint against the 6 schemes over their shared (book,chapter)
set gives, per collection:
  * scheme      best-fitting scheme, or None
  * confidence  exact | best-fit | undetermined
        exact        0 disagreements, a unique scheme, ≥1 discriminating chapter
        best-fit     unique nearest scheme but the text deviates (merges/edits)
        undetermined coverage can't separate the candidates (e.g. NT-only), or
                     several schemes tie
  * dis/cov     disagreements over the shared-chapter count
  * disc        how many of the ISO's chapters are discriminating (the schemes
                don't all agree there) — 0 means the scheme is unknowable here

Usage:  python3 scripts/analyze_versification.py [--json temp/versification.json]
"""
import argparse
import glob
import json
import re
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "data"

# SAB/Proskomma scheme label (from the .vrs header) -> standard code.
LABEL_CODE = {
    "Original": "org",
    "English": "eng",
    "Russian Orthodox": "rso",
    "Russian Protestant": "rsc",
    "Septuagint": "lxx",
    "Vulgate": "vul",
}

_OT = "GEN EXO LEV NUM DEU JOS JDG RUT 1SA 2SA 1KI 2KI 1CH 2CH EZR NEH EST JOB " \
      "PSA PRO ECC SNG ISA JER LAM EZK DAN HOS JOL AMO OBA JON MIC NAM HAB ZEP " \
      "HAG ZEC MAL".split()
_NT = "MAT MRK LUK JHN ACT ROM 1CO 2CO GAL EPH PHP COL 1TH 2TH 1TI 2TI TIT PHM " \
      "HEB JAS 1PE 2PE 1JN 2JN 3JN JUD REV".split()
_DC = "TOB JDT ESG WIS SIR BAR LJE S3Y SUS BEL 1MA 2MA 3MA 4MA 1ES 2ES MAN PS2 " \
      "ODA PSS".split()
_OTS, _NTS, _DCS = set(_OT), set(_NT), set(_DC)
_CANON = _OTS | _NTS | _DCS


def ranges(nums: set) -> str:
    """{1,2,3,5,6} -> '1-3,5-6'."""
    out, s = [], sorted(nums)
    i = 0
    while i < len(s):
        j = i
        while j + 1 < len(s) and s[j + 1] == s[j] + 1:
            j += 1
        out.append(f"{s[i]}-{s[j]}" if j > i else f"{s[i]}")
        i = j + 1
    return ",".join(out)


def extract_schemes() -> dict:
    """{code: {(book, chapter): last_verse}} parsed from the .vrs constants that
    ship inside any app chunk."""
    src = None
    for p in sorted(glob.glob(str(DATA / ".media-cache" / "*.js"))):
        t = Path(p).read_text(encoding="utf-8", errors="replace")
        if "# Versification" in t:
            src = t
            break
    if src is None:
        sys.exit("[vrs] no .media-cache chunk with versification definitions found")

    schemes: dict[str, dict] = {}
    # Each definition is a template literal: `# Versification "Name"\n<lines>`.
    for m in re.finditer(r'`(# Versification\s+"([^"]+)"[\s\S]*?)`', src):
        body, label = m.group(1), m.group(2)
        code = LABEL_CODE.get(label)
        if not code:
            continue
        fp: dict = {}
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" in line:
                continue  # skip comments and verse-mapping lines
            toks = line.split()
            book = toks[0]
            if not re.fullmatch(r"[A-Z0-9]{3}", book):
                continue
            for tok in toks[1:]:
                cm = re.fullmatch(r"(\d+):(\d+)", tok)
                if cm:
                    fp[(book, int(cm.group(1)))] = int(cm.group(2))
        if fp:
            schemes[code] = fp
    missing = set(LABEL_CODE.values()) - set(schemes)
    if missing:
        print(f"[vrs] WARNING: schemes not extracted: {sorted(missing)}", file=sys.stderr)
    return schemes


def footprint_from_catalog(cat: dict) -> dict:
    """{(book, chapter): max verse number} for a collection's catalog."""
    fp: dict = {}
    for d in cat.get("documents", []):
        book = d.get("bookCode")
        for ch, verses in (d.get("versesByChapters") or {}).items():
            try:
                ci = int(ch)
            except (TypeError, ValueError):
                continue
            mx = 0
            for pos, label in verses.items():
                for tok in re.findall(r"\d+", str(pos)) + re.findall(r"\d+", str(label)):
                    v = int(tok)
                    if v > mx:
                        mx = v
            if mx:
                fp[(book, ci)] = mx
    return fp


def scheme_book_counts(schemes: dict) -> dict:
    """{book: {valid chapter counts across schemes}} — a book is 'complete' if its
    top chapter matches any scheme's count (handles Joel 3 vs 4, Ps 150 vs 151)."""
    counts: dict = {}
    for s in schemes.values():
        per: dict = {}
        for (b, ch) in s:
            per[b] = max(per.get(b, 0), ch)
        for b, c in per.items():
            counts.setdefault(b, set()).add(c)
    return counts


def coverage(fp: dict, book_counts: dict) -> dict:
    """Full vs partial coverage, plus a compact per-testament map when partial.
    'Full' = every testament the text touches (OT / NT) is present in its entirety
    with every book complete; deuterocanon is treated as bonus, not required."""
    bychap: dict = {}
    for (b, ch) in fp:
        bychap.setdefault(b, set()).add(ch)

    def complete(b: str) -> bool:
        chs = bychap[b]
        n = max(chs)
        return chs == set(range(1, n + 1)) and n in book_counts.get(b, {n})

    pres_ot = [b for b in _OT if b in bychap]
    pres_nt = [b for b in _NT if b in bychap]
    pres_dc = [b for b in _DC if b in bychap]
    ot_full = len(pres_ot) == len(_OT) and all(complete(b) for b in pres_ot)
    nt_full = len(pres_nt) == len(_NT) and all(complete(b) for b in pres_nt)

    claim_o, claim_n = bool(pres_ot), bool(pres_nt)
    # Deuterocanon is bonus — it never forces 'partial'. Only the completeness of
    # the OT / NT the text actually contains decides full vs partial.
    full = (not claim_o or ot_full) and (not claim_n or nt_full)

    cov: dict = {}
    if claim_o:
        cov["o"] = "full" if ot_full else {b: ranges(bychap[b]) for b in pres_ot}
    if claim_n:
        cov["n"] = "full" if nt_full else {b: ranges(bychap[b]) for b in pres_nt}
    if pres_dc:
        cov["d"] = {b: ranges(bychap[b]) for b in pres_dc}
    return {"full": full, "map": cov}


def classify(fp: dict, schemes: dict) -> dict:
    """Match one collection footprint against every scheme."""
    if not fp:
        return {"scheme": None, "confidence": "no-data", "dis": 0, "cov": 0, "disc": 0}

    # Which of this text's chapters actually discriminate between schemes?
    disc = 0
    for k in fp:
        vals = {s[k] for s in schemes.values() if k in s}
        if len(vals) > 1:
            disc += 1

    scored = []
    for code, s in schemes.items():
        shared = [k for k in fp if k in s]
        agree = sum(1 for k in shared if fp[k] == s[k])
        scored.append({"code": code, "cov": len(shared),
                       "agree": agree, "dis": len(shared) - agree})
    # Best = fewest disagreements, then most coverage, then most agreement.
    best_dis = min(x["dis"] for x in scored)
    viable = [x for x in scored if x["dis"] == best_dis]
    viable.sort(key=lambda x: (-x["cov"], -x["agree"]))
    best = viable[0]

    determined = len(viable) == 1 and disc > 0
    if determined:
        conf = "exact" if best_dis == 0 else "best-fit"
        scheme = best["code"]
    else:
        conf = "undetermined"
        scheme = None
    return {"scheme": scheme, "confidence": conf, "dis": best_dis,
            "cov": best["cov"], "disc": disc,
            "viable": sorted(x["code"] for x in viable)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", default="temp/versification.json",
                    help="where to write the full per-collection report")
    args = ap.parse_args()

    schemes = extract_schemes()
    book_counts = scheme_book_counts(schemes)
    print(f"[vrs] extracted {len(schemes)} schemes: "
          + ", ".join(f"{c}({len(f)} ch)" for c, f in sorted(schemes.items())))

    man_path = sorted(glob.glob(str(REPO_ROOT / "release" / "manifest-global-*.json")))
    if not man_path:
        sys.exit("[vrs] no release/manifest-global-*.json — run 'make pack' first")
    langs = json.loads(Path(man_path[-1]).read_text(encoding="utf-8"))["languages"]

    rows = []
    for iso in sorted(langs):
        for col in langs[iso]["collections"]:
            cat_path = DATA / "global" / iso / col["catalog"]
            if not cat_path.exists():
                continue
            try:
                cat = json.loads(cat_path.read_text(encoding="utf-8"))
            except ValueError:
                continue
            fp = footprint_from_catalog(cat)
            res = classify(fp, schemes)
            cov = coverage(fp, book_counts)
            res.update({"iso": iso, "collection": col["pkf"].split(".")[0],
                        "books": len({b for b, _ in fp}),
                        "coverage": "full" if cov["full"] else "partial",
                        "map": cov["map"]})
            rows.append(res)

    # --- coverage x scheme cross-tab --------------------------------------
    full = [r for r in rows if r["coverage"] == "full"]
    part = [r for r in rows if r["coverage"] == "partial"]
    print(f"\n[vrs] {len(rows)} collections — {len(full)} full, {len(part)} partial\n")

    def tab(group, name):
        by = Counter(r["scheme"] or "undetermined" for r in group)
        print(f"{name} ({len(group)}):")
        for code, n in by.most_common():
            print(f"  {code:<13} {n:>4}")
    tab(full, "FULL coverage — versification")
    print()
    tab(part, "PARTIAL coverage — likely versification")

    # Full-but-undetermined = complete text a scheme can't tell apart (NT-only).
    fu = [r for r in full if not r["scheme"]]
    print(f"\nFull-but-undetermined: {len(fu)} "
          f"(complete canon, but coverage doesn't discriminate — mostly full NT-only)")

    # Sample partial coverage maps.
    print("\nSample PARTIAL coverage maps:")
    for r in sorted(part, key=lambda r: r["books"])[:4] + sorted(part, key=lambda r: -r["books"])[:3]:
        print(f"  {r['iso']}/{r['collection']:<9} books={r['books']:<3} "
              f"likely={r['scheme'] or '?':<4} viable={','.join(r['viable'])}")
        print(f"      {json.dumps(r['map'], ensure_ascii=False)[:200]}")

    print("\nExamples:")
    for iso in ("ind", "cco", "niy"):
        for r in rows:
            if r["iso"] == iso:
                m = "" if r["coverage"] == "full" else f"  map={json.dumps(r['map'],ensure_ascii=False)[:90]}"
                print(f"  {iso}/{r['collection']:<9} {r['coverage']:<7} "
                      f"{r['scheme'] or 'undetermined':<12} ({r['confidence']}, "
                      f"dis={r['dis']}/{r['cov']}){m}")

    out = Path(args.json)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(rows, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[vrs] full report -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
