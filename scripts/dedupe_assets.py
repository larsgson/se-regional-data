#!/usr/bin/env python3
"""
Consolidate duplicated fonts into a shared pool, then (via build_styles.py)
emit the shared scripture stylesheet and a small per-language delta.css.

Idempotent: safe to re-run after fetch_pkf.py fetches more languages, and safe
to re-run after deltas have already been emitted.

Layout produced:
    data/_pool/_fonts/<name>.<sha8>.ttf            shared font pool
    data/_pool/_fonts/index.json                   sha -> {name, size, isos[]}
    data/_pool/_styles/sab-scripture.css           shared Bible-text rules (build_styles.py)
    data/_pool/<iso>/styles/delta.css              per-iso fonts, colours, overrides (build_styles.py)
    data/_pool/<iso>/styles/raw/                   original SE stylesheets (source of truth)

Deleted:
    data/_pool/<iso>/styles/fonts/                 (content moved to pool)
    data/_pool/<iso>/styles/bundle.css             (superseded by _styles/ + delta.css)
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _lib import POOL_DIR as PKF_ROOT  # noqa: E402
FONTS_POOL = PKF_ROOT / "_fonts"


def sha256_of(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            h.update(chunk)
    return h.hexdigest()


def short(h: str) -> str:
    return h[:8]


def _load_pool_index() -> dict[str, dict]:
    idx_path = FONTS_POOL / "index.json"
    if not idx_path.exists():
        return {}
    try:
        return json.loads(idx_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def consolidate_fonts() -> dict:
    """Move all per-iso fonts into the shared pool, merging with any existing
    pool index (so re-runs don't lose knowledge of previously-fetched langs)."""
    FONTS_POOL.mkdir(parents=True, exist_ok=True)

    pool_index: dict[str, dict] = _load_pool_index()
    iso_to_mapping: dict[str, dict[str, str]] = {}

    for iso_dir in sorted(d for d in PKF_ROOT.iterdir() if d.is_dir() and not d.name.startswith("_")):
        fonts_dir = iso_dir / "styles" / "fonts"
        if not fonts_dir.exists() or not fonts_dir.is_dir():
            continue
        mapping: dict[str, str] = {}
        for font in sorted(fonts_dir.iterdir()):
            if not font.is_file():
                continue
            sha = sha256_of(font)
            parts = font.name.split(".")
            name_stem = parts[0] if len(parts) >= 3 else font.stem
            ext = parts[-1] if len(parts) >= 3 else font.suffix.lstrip(".")
            pool_name = f"{name_stem}.{short(sha)}.{ext}"
            pool_path = FONTS_POOL / pool_name

            if not pool_path.exists():
                shutil.copy2(font, pool_path)
            pool_index.setdefault(sha, {"name": pool_name, "size": pool_path.stat().st_size, "isos": []})
            if iso_dir.name not in pool_index[sha]["isos"]:
                pool_index[sha]["isos"].append(iso_dir.name)
            mapping[font.name] = pool_name
        iso_to_mapping[iso_dir.name] = mapping

    removed_dirs = 0
    for iso in iso_to_mapping:
        fonts_dir = PKF_ROOT / iso / "styles" / "fonts"
        if fonts_dir.exists():
            shutil.rmtree(fonts_dir)
            removed_dirs += 1

    (FONTS_POOL / "index.json").write_text(
        json.dumps(dict(sorted(pool_index.items())), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return {
        "pool_files": len(pool_index),
        "iso_font_dirs_consolidated": len(iso_to_mapping),
        "iso_font_dirs_removed": removed_dirs,
    }


def remove_bundles() -> int:
    n = 0
    for b in PKF_ROOT.glob("*/styles/bundle.css"):
        b.unlink()
        n += 1
    return n


# ---------- CSS delta analysis ----------

URL_HASH_RE = re.compile(r"url\(\s*(?:\.{1,2}/)*(?:fonts/|_fonts/)?([A-Za-z0-9_\-]+)\.[A-Za-z0-9_\-]+\.(ttf|otf|woff2?)\s*\)")


def canonicalize_css(text: str) -> str:
    """Normalize CSS so hash-only differences don't count as real variation."""
    # Drop hashed font filenames: url(./foo.HASH.ttf) -> url(foo.ttf)
    text = URL_HASH_RE.sub(lambda m: f"url({m.group(1)}.{m.group(2)})", text)
    # Collapse whitespace
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def analyze_variants(kind: str, pattern: str) -> list[tuple[str, str, list[str]]]:
    """Return [(canonical_hash, first_filepath, [isos])]."""
    groups: dict[str, dict] = {}
    for f in sorted(PKF_ROOT.glob(pattern)):
        iso = f.parts[-4]
        txt = f.read_text(encoding="utf-8")
        ch = hashlib.sha256(canonicalize_css(txt).encode()).hexdigest()
        g = groups.setdefault(ch, {"path": f, "isos": [], "raw": txt})
        g["isos"].append(iso)
    out = [(ch, g["path"], g["isos"]) for ch, g in groups.items()]
    # largest group first
    out.sort(key=lambda t: -len(t[2]))
    return out


def diff_snippet(a: str, b: str, n_lines: int = 12) -> str:
    # Line-break on `}` so diffs are readable.
    a_lines = a.replace("}", "}\n").splitlines()
    b_lines = b.replace("}", "}\n").splitlines()
    d = difflib.unified_diff(a_lines, b_lines, lineterm="", n=1)
    return "\n".join(list(d)[:n_lines])


def report_css_deltas() -> None:
    print("\n=== CSS VARIATION (after canonicalizing hash-only differences) ===\n")
    for kind, pattern in [
        ("override-dab", "*/styles/raw/override-dab.*.css"),
        ("sab-bc", "*/styles/raw/sab-bc-*.css"),
        ("sab-app", "*/styles/raw/sab-app.*.css"),
        ("sab-annotations", "*/styles/raw/sab-annotations.*.css"),
    ]:
        variants = analyze_variants(kind, pattern)
        total = sum(len(isos) for _, _, isos in variants)
        print(f"--- {kind:<20} {total} files · {len(variants)} true variant(s) ---")
        for i, (_, path, isos) in enumerate(variants):
            size = path.stat().st_size
            head = ", ".join(isos[:8]) + ("…" if len(isos) > 8 else "")
            print(f"  variant {i+1}: {len(isos):>3} lang(s)  {size:>6} B  ({head})")
        # Show diff between variant 1 and 2 (if any)
        if len(variants) >= 2:
            a = canonicalize_css(variants[0][1].read_text())
            b = canonicalize_css(variants[1][1].read_text())
            print(f"  DIFF variant1 -> variant2:")
            snip = diff_snippet(a, b, n_lines=20)
            for line in snip.splitlines()[:20]:
                print(f"    {line}")
        # for sab-bc: also show one variant in full (they're tiny)
        if kind == "sab-bc" and variants:
            print(f"  FULL of variant 1 ({variants[0][1].stat().st_size} B):")
            print("    " + variants[0][1].read_text().strip().replace("\n", "\n    "))
        print()


def summarize_disk() -> None:
    from subprocess import check_output
    total = check_output(["du", "-sh", str(PKF_ROOT)]).split()[0].decode()
    pool = check_output(["du", "-sh", str(FONTS_POOL)]).split()[0].decode()
    print(f"\nDISK: data/pkf/ total: {total} · _fonts pool: {pool}")


def main() -> int:
    if not PKF_ROOT.exists():
        print(f"[dedupe] {PKF_ROOT} does not exist, nothing to do")
        return 0
    stats = consolidate_fonts()
    print(f"[dedupe] fonts: {stats['iso_font_dirs_consolidated']} consolidated, "
          f"{stats['pool_files']} in pool")
    # Always rebuild: delta.css depends on the corpus-wide shared sheet, so a
    # newly fetched language can change what every other delta must override.
    # build_styles only rewrites files whose bytes changed.
    import build_styles
    st = build_styles.build_all()
    print(f"[dedupe] styles: shared sab-scripture.{st.get('shared_hash')} "
          f"({st.get('shared_rules')} rules, {st.get('shared_bytes')} B) · "
          f"{st.get('languages')} delta.css ({st.get('delta_written', 0)} changed)")
    removed = remove_bundles()
    if removed:
        print(f"[dedupe] removed {removed} superseded bundle.css file(s)")
    if "--verbose" in sys.argv:
        report_css_deltas()
        summarize_disk()
    return 0


if __name__ == "__main__":
    sys.exit(main())
