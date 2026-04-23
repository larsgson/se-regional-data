#!/usr/bin/env python3
"""
Consolidate duplicated fonts into a shared pool, then emit a tiny per-language
delta CSS that combines with the app's baseline reader.css.

Idempotent: safe to re-run after fetch_pkf.py fetches more languages, and safe
to re-run after deltas have already been emitted.

Layout produced:
    data/pkf/_fonts/<name>.<sha8>.ttf              shared font pool
    data/pkf/_fonts/index.json                     sha -> {name, size, isos[]}
    data/pkf/<iso>/styles/delta.css                ~500-byte per-iso CSS
    data/pkf/<iso>/styles/raw/                     original SE stylesheets (source of truth)

Deleted:
    data/pkf/<iso>/styles/fonts/                   (content moved to pool)
    data/pkf/<iso>/styles/bundle.css               (superseded by delta.css + reader.css)
"""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

PKF_ROOT = Path("data/pkf")
FONTS_POOL = PKF_ROOT / "_fonts"
FONT_URL_RE = re.compile(r"url\(\s*\./fonts/([A-Za-z0-9_.\-]+\.(?:ttf|otf|woff2?))\s*\)")
FONT_URL_POOL_RE = re.compile(r"url\(\s*\.\./\.\./_fonts/([A-Za-z0-9_.\-]+)\s*\)")


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
        if not fonts_dir.exists():
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


# ---------- Per-iso delta CSS generation ----------

FONT_FACE_RE = re.compile(r"@font-face\s*\{\s*([^}]+?)\s*\}", re.DOTALL)
CONTAINER_RE = re.compile(r"#container\s*\{\s*([^}]+?)\s*\}")
URL_IN_SRC_RE = re.compile(r"url\(\s*\.?/?([^)\s\"']+)\s*\)")


def _parse_css_decls(block: str) -> dict[str, str]:
    """Parse `prop:value;prop:value` style declarations into a dict."""
    out: dict[str, str] = {}
    for part in re.split(r";(?![^(]*\))", block):
        if ":" not in part:
            continue
        k, _, v = part.partition(":")
        out[k.strip().lower()] = v.strip()
    return out


def _build_pool_reverse(pool_index: dict[str, dict]) -> dict[str, dict[str, str]]:
    """iso -> { base-name (e.g. 'CharisSILAm-R') -> pool filename }."""
    rev: dict[str, dict[str, str]] = defaultdict(dict)
    for _sha, info in pool_index.items():
        pool_name = info["name"]
        base = pool_name.split(".")[0]
        for iso in info["isos"]:
            rev[iso][base] = pool_name
    return rev


def generate_delta(iso: str, pool_reverse: dict[str, dict[str, str]]) -> tuple[Path, int] | None:
    raw_dir = PKF_ROOT / iso / "styles" / "raw"
    if not raw_dir.exists():
        return None
    app_files = sorted(raw_dir.glob("sab-app.*.css"))
    if not app_files:
        return None
    app_css = app_files[0].read_text(encoding="utf-8")
    bc_files = sorted(raw_dir.glob(f"sab-bc-{iso}.*.css"))
    bc_css = bc_files[0].read_text(encoding="utf-8") if bc_files else ""

    # @font-face blocks: extract family name + src URL + weight/style.
    # Rename each family to "<iso>-<origfamily>" so multiple langs can coexist.
    faces: list[dict[str, str]] = []
    for m in FONT_FACE_RE.finditer(app_css):
        decls = _parse_css_decls(m.group(1))
        family = decls.get("font-family", "").strip().strip('"').strip("'")
        src = decls.get("src", "")
        url_match = URL_IN_SRC_RE.search(src)
        if not family or not url_match:
            continue
        orig_filename = url_match.group(1).split("/")[-1]
        base = orig_filename.split(".")[0]
        pool_name = pool_reverse.get(iso, {}).get(base)
        if not pool_name:
            continue
        faces.append({
            "family": f"{iso}-{family}",
            "pool": pool_name,
            "weight": decls.get("font-weight", "400"),
            "style": decls.get("font-style", "normal"),
        })

    # #container rule: app_css provides a default, bc_css overrides.
    container: dict[str, str] = {}
    for css in (app_css, bc_css):
        m = CONTAINER_RE.search(css)
        if m:
            container.update(_parse_css_decls(m.group(1)))

    orig_family = container.get("font-family", "font1").strip()
    scoped_family = f"{iso}-{orig_family}"

    lines: list[str] = [
        f"/* Per-language delta stylesheet for {iso}.",
        f" * Pairs with the app's shared reader.css baseline.",
        f" * Source: data/pkf/{iso}/styles/raw/  (do not edit by hand).",
        " */",
    ]
    for f in faces:
        lines.append(
            '@font-face{'
            f'font-family:{f["family"]};'
            f'src:url(../../_fonts/{f["pool"]}) format("truetype");'
            f'font-weight:{f["weight"]};'
            f'font-style:{f["style"]}'
            '}'
        )
    props: list[str] = [f"font-family:{scoped_family}"]
    for css_prop in ("direction", "font-size", "font-weight", "font-style", "color"):
        if css_prop in container:
            props.append(f"{css_prop}:{container[css_prop]}")
    lines.append(f'.reader-root[data-iso="{iso}"]{{{";".join(props)}}}')

    delta_path = PKF_ROOT / iso / "styles" / "delta.css"
    content = "\n".join(lines) + "\n"
    delta_path.write_text(content, encoding="utf-8")
    return delta_path, len(content)


def update_info_json(iso: str, delta_rel: str) -> None:
    info_path = PKF_ROOT / iso / "info.json"
    if not info_path.exists():
        return
    info = json.loads(info_path.read_text(encoding="utf-8"))
    info["style_delta"] = delta_rel
    info.pop("style_bundle", None)
    info_path.write_text(json.dumps(info, indent=2, ensure_ascii=False), encoding="utf-8")


def generate_all_deltas() -> dict:
    pool_index = _load_pool_index()
    pool_reverse = _build_pool_reverse(pool_index)
    counts = {"emitted": 0, "skipped": 0, "total_bytes": 0}
    sizes: list[int] = []
    for iso_dir in sorted(d for d in PKF_ROOT.iterdir() if d.is_dir() and not d.name.startswith("_")):
        result = generate_delta(iso_dir.name, pool_reverse)
        if not result:
            counts["skipped"] += 1
            continue
        delta_path, size = result
        counts["emitted"] += 1
        counts["total_bytes"] += size
        sizes.append(size)
        update_info_json(iso_dir.name, str(delta_path.relative_to(PKF_ROOT / iso_dir.name)))
    if sizes:
        counts["avg_bytes"] = counts["total_bytes"] // len(sizes)
        counts["min_bytes"] = min(sizes)
        counts["max_bytes"] = max(sizes)
    return counts


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
        print(f"error: {PKF_ROOT} does not exist", file=sys.stderr)
        return 2
    stats = consolidate_fonts()
    print("FONT DEDUP:")
    for k, v in stats.items():
        print(f"  {k}: {v}")
    delta_stats = generate_all_deltas()
    print("\nPER-ISO DELTA CSS:")
    for k, v in delta_stats.items():
        print(f"  {k}: {v}")
    removed = remove_bundles()
    print(f"\nREMOVED {removed} superseded bundle.css file(s).")
    report_css_deltas()
    summarize_disk()
    return 0


if __name__ == "__main__":
    sys.exit(main())
