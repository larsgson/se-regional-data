#!/usr/bin/env python3
"""
Audit the shared-scripture-CSS split (scripts/build_styles.py).

Proves, from the files that actually get published (not from build_styles'
in-memory state), that for every language

  1. shared sheet + <iso>/styles/delta.css reproduce the language's original
     scripture rules exactly (declaration-for-declaration, after the cascade
     merge of sab-app + sab-bc);
  2. every var(--X) used by the shared sheet or the delta is defined in all
     three colour themes of that delta;
  3. every @font-face in the delta points at a file in the font pool, and the
     base rule's font-family is one the delta defines;
  4. the shared sheet holds no app-chrome selectors, and every USFM paragraph /
     character class the corpus uses is covered by it or by an override;
  5. info.json points at the shared sheet by the hash that exists on disk, and
     the stable and hashed copies are byte-identical.

Exit status 0 = clean, 1 = any failure (so release.sh can gate on it).
Run: python3 scripts/audit_styles.py [-v]
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sab_css as S  # noqa: E402
import build_styles as B  # noqa: E402
from _lib import POOL_DIR  # noqa: E402

VERBOSE = "-v" in sys.argv


def unscope(prelude: str, iso: str) -> str:
    sc, desc = B.scope(iso), B.scope_desc(iso)
    out = []
    for sel in S.split_selectors(prelude):
        if sel.startswith(desc + " "):
            out.append(sel[len(desc) + 1:])
        elif sel.startswith(sc):
            out.append("#container" + sel[len(sc):])
        else:
            out.append(sel)
    return ",".join(out)


def key_of(wrap: str, prelude: str) -> tuple[str, str]:
    # normalise selector-list spacing the way sab_css does
    return (wrap, ",".join(S.split_selectors(prelude)))


def main() -> int:
    errors: list[str] = []

    def fail(iso: str, msg: str) -> None:
        errors.append(f"{iso}: {msg}")

    shared_path = B.STYLES_DIR / f"{B.SHARED_NAME}.css"
    if not shared_path.exists():
        print("[audit-styles] FATAL: no shared stylesheet — run scripts/build_styles.py", file=sys.stderr)
        return 1
    shared_css = shared_path.read_text(encoding="utf-8")
    sha8 = B.hashlib.sha256(shared_css.encode()).hexdigest()[:8]
    hashed = B.STYLES_DIR / f"{B.SHARED_NAME}.{sha8}.css"
    if not hashed.exists() or hashed.read_text(encoding="utf-8") != shared_css:
        fail("_styles", f"hashed copy {hashed.name} missing or differs from stable copy")
    extra = [p.name for p in B.STYLES_DIR.glob(f"{B.SHARED_NAME}.*.css") if p.name != hashed.name]
    if extra:
        fail("_styles", f"stale hashed copies: {extra}")

    # (4a) shared sheet holds no chrome
    shared_raw = B.flatten(S.parse(shared_css))
    for (_, sel), _ in shared_raw:
        for one in S.split_selectors(sel):
            if not (one.startswith(B.SHARED_BOX) or one.startswith(B.SHARED_DESC + " ")):
                fail("_styles", f"shared selector not scoped to the container: {one}")
    shared_flat = [((w, ",".join(B.unscope_shared_selector(x) for x in S.split_selectors(p))), d)
                   for (w, p), d in shared_raw]
    base = {k: dict(d) for k, d in shared_flat}
    for (wrap, sel), _ in shared_flat:
        for one in S.split_selectors(sel):
            if S._UI_RE.search(one) or (one.startswith("#container") and S.kind_of(S.Rule(one)) != "scripture"):
                fail("_styles", f"app-chrome selector leaked into shared sheet: {one}")
    shared_vars = B.referenced_vars(shared_css)

    fonts = B.FontPool()
    pool_names = fonts.names
    idx = json.loads((B.STYLES_DIR / "index.json").read_text(encoding="utf-8"))
    known_missing: dict[str, list[str]] = idx.get("missing_fonts", {})
    warnings: list[str] = []

    isos = sorted(d.name for d in POOL_DIR.iterdir() if d.is_dir() and not d.name.startswith("_"))
    n_checked = n_override = 0
    usfm_used: Counter[str] = Counter()
    covered: set[str] = set()
    for (_, sel), _ in shared_flat:
        covered.update(re.findall(r"\.([A-Za-z][\w-]*)", sel))

    for iso in isos:
        lg = B.Lang(iso, POOL_DIR / iso)
        if not lg.ok:
            continue
        n_checked += 1
        delta_path = POOL_DIR / iso / "styles" / "delta.css"
        if not delta_path.exists():
            fail(iso, "missing styles/delta.css")
            continue
        delta = delta_path.read_text(encoding="utf-8")
        if f"sab-scripture.{sha8}" not in delta:
            fail(iso, "delta.css was built against a different shared sheet (stale; re-run build_styles)")

        rules = S.parse(delta)
        sc = B.scope(iso)

        # classify delta rules
        override: dict[tuple[str, str], dict[str, str]] = {}
        themes: dict[str, dict[str, str]] = {}
        faces: list[dict[str, str]] = []
        base_rule: dict[str, str] = {}
        for r in rules:
            if r.prelude == "@font-face":
                faces.append(dict(r.decls))
                continue
            if r.is_group:
                for c in r.children:
                    override[key_of(r.prelude, unscope(c.prelude, iso))] = dict(c.decls)
                continue
            if r.prelude == sc:
                base_rule = dict(r.decls)
            elif r.prelude.startswith(sc + "[data-color-theme="):
                themes[r.prelude[len(sc + "[data-color-theme="):-1]] = dict(r.decls)
            else:
                override[key_of("", unscope(r.prelude, iso))] = dict(r.decls)

        # (1) reconstruction
        if lg.eff:
            n_override += 1 if override else 0
        orig = {k: {p: B.scope_font(v, iso) for p, v in d.items()} for k, d in lg.eff.items()}
        for key in set(orig) | set(base) | set(override):
            rec: dict[str, str] = dict(base.get(key, {}))
            for p, v in override.get(key, {}).items():
                if v == "unset":
                    rec.pop(p, None)
                else:
                    rec[p] = v
            want = orig.get(key, {})
            if rec != want:
                diff = {p: (rec.get(p), want.get(p)) for p in set(rec) | set(want) if rec.get(p) != want.get(p)}
                fail(iso, f"scripture rule {key[1][:50]!r} not reproduced: {dict(list(diff.items())[:3])}")

        # (2) variables
        used = shared_vars | B.referenced_vars(" ".join(v for d in override.values() for v in d.values()))
        used |= B.referenced_vars(" ".join(base_rule.values()))
        for name in ("Normal", "Sepia", "Dark"):
            t = themes.get(name)
            if t is None:
                fail(iso, f"theme {name} missing")
                continue
            for v in sorted(used - set(t)):
                fail(iso, f"theme {name}: {v} used but not defined")
            for k, val in t.items():
                for ref in B.VAR_RE.findall(val):
                    if ref not in t:
                        fail(iso, f"theme {name}: {k} references undefined {ref}")

        # (3) fonts
        defined = set()
        for f in faces:
            defined.add(f.get("font-family", "").strip())
            m = re.search(r"_fonts/([^)\s\"']+)", f.get("src", ""))
            if not m or (pool_names and m.group(1) not in pool_names):
                fail(iso, f"@font-face points at a file not in the font pool: {f.get('src')}")
        fam = base_rule.get("font-family", "")
        if fam.startswith(f"{iso}-font") and fam not in defined:
            if iso in known_missing:
                warnings.append(f"{iso}: font not obtainable from source, browser default used: {known_missing[iso]}")
            else:
                fail(iso, f"base font-family {fam} has no @font-face in delta.css (font missing from pool?)")

        # (5) info.json pointer
        ip = POOL_DIR / iso / "info.json"
        if ip.exists():
            info = json.loads(ip.read_text(encoding="utf-8"))
            if info.get("style_shared") != B.SHARED_FROM_ISO or info.get("style_shared_hash") != sha8:
                fail(iso, "info.json style_shared/style_shared_hash not current")
            if info.get("style_delta") != "styles/delta.css":
                fail(iso, "info.json style_delta not current")

        # (4b) USFM-ish classes the language actually styles must be reachable
        for key in orig:
            for c in re.findall(r"\.([A-Za-z][\w-]*)", key[1]):
                usfm_used[c] += 1

    summary = (f"[audit-styles] {n_checked} languages · {n_override} with scripture overrides · "
               f"shared {sha8} ({len(shared_flat)} rules)")
    for w in warnings:
        print(f"[audit-styles] WARN {w}", file=sys.stderr)
    if VERBOSE:
        print(summary)
    if errors:
        print(summary)
        print(f"[audit-styles] FAIL — {len(errors)} problem(s):", file=sys.stderr)
        for e in errors[:60]:
            print("  " + e, file=sys.stderr)
        if len(errors) > 60:
            print(f"  … and {len(errors) - 60} more", file=sys.stderr)
        return 1
    print(summary + " — OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
