#!/usr/bin/env python3
"""
Pipeline step: probe every ISO's Scripture Earth SAB deployment and
classify the scripture-text license. Emits data/pkf/licenses.json.

DECISION LOGIC (classifier_version 3):

  1. Extract the Texto: block (the per-work copyright declaration).
  2. Negative signals inside the Texto: block → EXCLUDE:
       - "Usado con permiso" / "Used with permission"
       - "Todos los derechos reservados" / "All rights reserved"
       - Biblica NVI bundling ("NUEVA VERSIÓN INTERNACIONAL", "NVI®", "Biblica")
       - "Texto en proceso de finalizar" / "in process" / "provisional"
  3. Positive signal anywhere in the whole JS bundle → INCLUDE:
       - "Creative Commons" near any of:
           Atribución-NoComercial-SinDerivadas
           Reconocimiento-NoComercial-SinObraDerivada (knj-style alt Spanish)
           Attribution-Noncommercial-No Derivative Works (English SAB apps)
           BY-NC-ND / by-nc-nd
       - bare "(BY-NC-ND)" token
       - creativecommons.org/licenses/by-nc-nd URL literal
  4. Otherwise → EXCLUDE as "unclear".

The `by-nc-nd.<hash>.png` badge cached by the service worker is recorded
as evidence only — it's NOT used in the decision. SAB tooling emits the
badge for all Wycliffe-managed texts regardless of actual license state.

Usage:
    scripts/classify_licenses.py                 # write licenses.json
    scripts/classify_licenses.py --prune         # also rm excluded iso dirs

Requires data/pkf/manifest.json (produced by fetch_pkf.py). Run AFTER
fetch_pkf.py + dedupe_assets.py but BEFORE packing the release tarball.

Disk cache: per-iso concatenated string-literal corpus is cached at
data/.license-scan-cache/<iso>.<chunk-paths-sha1>.txt. Cache hits skip
all chunk network fetches; SE redeploys auto-invalidate via the chunk
hashes. To force a full re-scan, `rm -rf data/.license-scan-cache/`.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import REPO_ROOT  # noqa: E402

PKF_DIR = REPO_ROOT / "data" / "pkf"
MANIFEST = PKF_DIR / "manifest.json"
OUT = PKF_DIR / "licenses.json"
EXCLUDED_TXT = REPO_ROOT / "EXCLUDED_ISOS.txt"
CACHE_DIR = REPO_ROOT / "data" / ".license-scan-cache"

# SE's Apache returns truncated/stub bodies on the JS chunks when the
# User-Agent isn't a real browser. Mandatory.
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15"
    ),
    "Accept": "*/*",
}

# Evidence-only: is the badge image cached by the SW? Not used in decisions.
BADGE = re.compile(r"by-nc-nd\.[A-Za-z0-9_-]+\.(?:png|svg)", re.I)

# Positive signal — search the WHOLE concatenated JS.
CC_TEXT = re.compile(
    r"Creative Commons[\s\S]{0,150}?(?:"
    r"Atribución-NoComercial-SinDerivadas"
    r"|Reconocimiento-NoComercial-SinObraDerivada"
    r"|Attribution-?Noncommercial-?No[\s-]*Derivative[\s-]*Works"
    r"|BY-NC-ND"
    r"|by-nc-nd"
    r")"
    r"|\(BY-NC-ND\)"
    r"|creativecommons\.org/licenses/by-nc-nd",
    re.I,
)

JS_URL = re.compile(
    r"/_app/immutable/(?:chunks|entry|nodes)/[A-Za-z0-9._-]+\.js"
)
STRING_LIT = re.compile(r'"([^"\\]{3,4000})"')

TEXTO_PRIMARY = re.compile(
    r"Texto[:\s]*</b>([\s\S]{0,2000}?)"
    r"(?=</div>|<b>\s*Audio|<b>\s*Im[áa]genes|<b>\s*Ilustrac|<b>\s*Images|<div)",
    re.I,
)
TEXTO_FALLBACK = re.compile(
    r">\s*Texto[:\s]*([\s\S]{0,2000}?)"
    r"(?=</div>|Audio:|Im[áa]genes:|Ilustraciones|Images:)",
    re.I,
)
HOLDER_RE = re.compile(r"©\s*([0-9,\s]+[^<]{2,160})")

NEG_USADO = re.compile(r"usado con permiso|used with permission", re.I)
NEG_ARR = re.compile(r"todos los derechos reservados|all rights reserved", re.I)
NEG_NVI = re.compile(r"nueva versi[óo]n internacional|nvi®|biblica", re.I)
NEG_PROV = re.compile(r"proceso de finalizar|in process|provisional", re.I)


def fetch_text(url: str, tries: int = 4) -> str:
    """GET with browser UA; exponential backoff on 429/403/5xx. Returns '' on failure."""
    for attempt in range(tries):
        try:
            req = Request(url, headers=HEADERS)
            with urlopen(req, timeout=30) as r:
                return r.read().decode("utf-8", errors="replace")
        except HTTPError as e:
            if e.code not in (429, 403, 502, 503):
                return ""
            time.sleep(2**attempt)
        except (URLError, TimeoutError):
            time.sleep(2**attempt)
    return ""


def extract_texto(joined: str) -> str:
    m = TEXTO_PRIMARY.search(joined) or TEXTO_FALLBACK.search(joined)
    if not m:
        return ""
    return m.group(1)[:1500].strip()


def extract_holder(texto: str) -> str:
    m = HOLDER_RE.search(texto)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()


def classify_texto(texto: str) -> dict:
    """Return {'ok': True} or {'ok': False, 'reason': '…'} based on Texto:-block content."""
    if NEG_USADO.search(texto):
        return {"ok": False, "reason": 'Texto: "Usado con permiso" — permission-only, not CC'}
    if NEG_ARR.search(texto):
        return {"ok": False, "reason": 'Texto: "Todos los derechos reservados" — ARR declaration'}
    if NEG_NVI.search(texto):
        return {"ok": False, "reason": "Texto: bundles Biblica NVI translation — proprietary, not CC"}
    if NEG_PROV.search(texto):
        return {"ok": False, "reason": "Texto: provisional / not-final translation"}
    return {"ok": True}


def joined_js_for_iso(iso: str, chunk_delay_s: float) -> dict:
    """Fetch + cache the concatenated string-literal corpus for one ISO."""
    base = f"https://scriptureearth.org/data/{iso}/sab/{iso}"
    sw = fetch_text(f"{base}/service-worker.js")
    if not sw:
        return {"unreachable": True, "sw": "", "joined": ""}

    paths = sorted(set(JS_URL.findall(sw)))
    key = hashlib.sha1(f"{iso}|{'|'.join(paths)}".encode()).hexdigest()[:16]
    cache_path = CACHE_DIR / f"{iso}.{key}.txt"
    if cache_path.exists():
        return {"unreachable": False, "sw": sw, "joined": cache_path.read_text(encoding="utf-8")}

    parts: list[str] = []
    for p in paths:
        body = fetch_text(f"{base}{p}")
        if body:
            parts.extend(m.group(1) + "\n" for m in STRING_LIT.finditer(body))
        time.sleep(chunk_delay_s)
    joined = "".join(parts)
    cache_path.write_text(joined, encoding="utf-8")
    return {"unreachable": False, "sw": sw, "joined": joined}


def probe_iso(iso: str, chunk_delay_s: float) -> dict:
    result = joined_js_for_iso(iso, chunk_delay_s)
    if result["unreachable"]:
        return {"iso": iso, "unreachable": True}

    sw = result["sw"]
    joined = result["joined"]
    badge = bool(BADGE.search(sw))
    texto = extract_texto(joined)
    holder = extract_holder(texto)
    cc_text = bool(CC_TEXT.search(joined))
    decision = classify_texto(texto)
    evidence = {"badge_in_sw": badge, "cc_text_in_js": cc_text}

    if not decision["ok"]:
        return {
            "iso": iso,
            "include": False,
            "license": "not-cc",
            "reason": decision["reason"],
            "texto": texto,
            "text_holder": holder,
            "evidence": evidence,
        }
    if cc_text:
        return {
            "iso": iso,
            "include": True,
            "license": "CC-BY-NC-ND-4.0",
            "texto": texto,
            "text_holder": holder,
            "evidence": evidence,
        }
    return {
        "iso": iso,
        "include": False,
        "license": "unclear",
        "reason": "No CC declaration found in Texto block or JS bundle",
        "texto": texto,
        "text_holder": holder,
        "evidence": evidence,
    }


def update_excluded_txt(excluded_map: dict) -> None:
    """Rewrite the auto-managed block of EXCLUDED_ISOS.txt. Preserve manual entries."""
    BEGIN = "# BEGIN auto-managed by classify_licenses.py (do not edit by hand)"
    END = "# END auto-managed"
    HEADER = (
        "# ISOs whose source license does not permit redistribution by us.\n"
        "# These are excluded from release tarballs even when present in data/pkf/.\n"
        "# Format: one ISO per line. Blank lines and lines starting with # are ignored.\n"
        "# Inline comments after the ISO are allowed (separated by whitespace).\n"
        "#\n"
        "# Two sections:\n"
        "#   - Manual entries (above the BEGIN marker) are preserved across runs of\n"
        "#     classify_licenses.py. Use this for ISOs the classifier can't catch\n"
        "#     or that require an out-of-band judgement.\n"
        "#   - Auto-managed entries (between the BEGIN/END markers) are rewritten on\n"
        "#     every \"make classify\" run from data/pkf/licenses.json. Do NOT edit by\n"
        "#     hand — your changes will be overwritten.\n"
    )

    auto_isos = set(excluded_map.keys())

    preserved_manual: list[str] = []
    if EXCLUDED_TXT.exists():
        lines = EXCLUDED_TXT.read_text(encoding="utf-8").splitlines()
        try:
            begin_idx = lines.index(BEGIN)
        except ValueError:
            begin_idx = -1
        try:
            end_idx = lines.index(END)
        except ValueError:
            end_idx = -1
        # Also tolerate the old ".mjs" marker while migrating.
        if begin_idx < 0:
            for i, l in enumerate(lines):
                if "BEGIN auto-managed" in l:
                    begin_idx = i
                    break
        if end_idx < 0:
            for i, l in enumerate(lines):
                if "END auto-managed" in l:
                    end_idx = i
                    break
        if 0 <= begin_idx < end_idx:
            outside = lines[:begin_idx] + lines[end_idx + 1 :]
        else:
            outside = lines
        for raw in outside:
            stripped = raw.split("#", 1)[0].strip()
            if not stripped:
                continue
            iso = stripped.split()[0]
            if iso in auto_isos:
                continue
            preserved_manual.append(raw.rstrip())

    auto_lines: list[str] = []
    for iso in sorted(auto_isos):
        e = excluded_map[iso]
        reason = re.sub(r"\s+", " ", (e.get("reason") or e.get("license") or "no reason given"))[:200]
        license_ = e.get("license", "unknown")
        auto_lines.append(f"{iso}  # {license_} — {reason}")

    manual_block = ""
    if preserved_manual:
        manual_block = "# Manual entries:\n" + "\n".join(preserved_manual) + "\n\n"
    auto_block = BEGIN + "\n" + ("\n".join(auto_lines) + "\n" if auto_lines else "") + END + "\n"
    EXCLUDED_TXT.write_text(HEADER + "\n" + manual_block + auto_block, encoding="utf-8")
    print(
        f"Wrote {EXCLUDED_TXT}  (manual: {len(preserved_manual)}, auto: {len(auto_lines)})"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--prune", action="store_true", help="Also rm data/pkf/<iso>/ for excluded ISOs and rewrite manifest.json.")
    args = ap.parse_args()

    if not MANIFEST.exists():
        print(f"Missing {MANIFEST}. Run fetch_pkf.py first.", file=sys.stderr)
        return 1
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    isos = [l["iso"] for l in manifest["languages"]]

    # SE's Apache rate-limits at ~8 req/s per IP — sequential by default.
    chunk_delay_s = float(os.environ.get("CHUNK_DELAY_MS", "150")) / 1000.0
    print(f"Classifying {len(isos)} ISOs (sequential, {chunk_delay_s*1000:.0f}ms between chunks)...")

    results: list[dict] = []
    for iso in isos:
        try:
            results.append(probe_iso(iso, chunk_delay_s))
        except Exception as e:
            results.append({"iso": iso, "error": str(e)})

    included: dict[str, dict] = {}
    excluded: dict[str, dict] = {}
    for r in results:
        if r.get("unreachable") or r.get("error"):
            excluded[r["iso"]] = {
                "reason": r.get("error") or "SE unreachable",
                "license": "unknown",
            }
            continue
        if r["include"]:
            included[r["iso"]] = {
                "license": r["license"],
                "text_holder": r["text_holder"] or None,
                "evidence": r["evidence"],
            }
        else:
            excluded[r["iso"]] = {
                "license": r["license"],
                "reason": r["reason"],
                "text_holder": r["text_holder"] or None,
                "texto": r["texto"],
                "evidence": r["evidence"],
            }

    out = {
        "schema_version": 1,
        "classifier_version": 3,
        "updated_at": time.strftime("%Y-%m-%d", time.gmtime()),
        "source": "Scripture Earth SAB app service-worker + JS chunk scan",
        "notes": [
            "License applies to the scripture TEXT only (the .pkf data).",
            "Images, audio, and video referenced by SE URL carry their own per-asset licenses and are NOT covered by this classification.",
            "Badge presence is recorded as evidence but not used in the decision — it is unreliable (SAB tooling emits it for all Wycliffe texts regardless of actual license status).",
        ],
        "default_license": "CC-BY-NC-ND-4.0",
        "included_count": len(included),
        "excluded_count": len(excluded),
        "included": included,
        "excluded": excluded,
    }
    OUT.write_text(json.dumps(out, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Wrote {OUT}")
    print(f"  included: {out['included_count']}")
    print(f"  excluded: {out['excluded_count']}")
    for iso, e in excluded.items():
        print(f"    {iso}: {e.get('license', 'unknown')} — {e.get('reason', '')}")

    update_excluded_txt(excluded)

    if args.prune:
        print("--prune: removing excluded iso dirs from data/pkf/...")
        for iso in excluded:
            d = PKF_DIR / iso
            if d.exists():
                shutil.rmtree(d)
                print(f"  rm {d}")
        before = len(manifest["languages"])
        manifest["languages"] = [l for l in manifest["languages"] if l["iso"] not in excluded]
        MANIFEST.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  manifest.json: {before} → {len(manifest['languages'])} languages")

    return 0


if __name__ == "__main__":
    sys.exit(main())
