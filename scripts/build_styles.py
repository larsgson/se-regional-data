#!/usr/bin/env python3
"""
Split SAB's per-language stylesheets into ONE shared scripture stylesheet plus
a tiny per-language delta.

Why: every language ships a ~50 KB `sab-app.<hash>.css`, but only a handful of
things in it are actually per-language. Measured over the corpus (see
`audit_styles.py`): the Bible-text rules (div.p, span.v, div.q, td.tc1 …) are
byte-identical in >99% of languages; what differs is the font, the base text
size/direction, the colour palette and a few rare overrides. The rest of the
file is app chrome (contents-*, plan-*, settings-*, …) that a text renderer
never needs.

Inputs (read-only):
    data/_pool/<iso>/styles/raw/sab-app.<hash>.css      the big one
    data/_pool/<iso>/styles/raw/sab-bc-*.css            per-collection font/dir
    data/_pool/_fonts/index.json                        font pool (dedupe_assets)

Outputs:
    data/_pool/_styles/sab-scripture.css                stable path (short TTL)
    data/_pool/_styles/sab-scripture.<sha8>.css         same bytes, immutable
    data/_pool/_styles/index.json                       what was built, from what
    data/_pool/<iso>/styles/delta.css                   per-language remainder
    data/_pool/<iso>/info.json                          style_delta / style_shared

Client contract (docs/client-data-spec.md §7):
    load  pkf/_styles/sab-scripture.css      once, shared by every language
    load  pkf/<iso>/styles/delta.css         per language, AFTER the shared sheet
    render inside  <div id="container" data-iso="<iso>" data-color-theme="Normal">

Idempotent and deterministic: no timestamps, files only touched when their
bytes change (so the incremental CDN plan stays a no-op on an unchanged tree).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sab_css as S  # noqa: E402
from _lib import POOL_DIR, write_if_changed  # noqa: E402

STYLES_DIR = POOL_DIR / "_styles"
FONTS_POOL = POOL_DIR / "_fonts"
SHARED_NAME = "sab-scripture"
SHARED_REL = f"_styles/{SHARED_NAME}.css"
SHARED_FROM_ISO = f"../{SHARED_REL}"            # relative to <iso>/

# Colour variables every client wants even if no scripture rule references
# them directly (page background, chrome, links, dividers …). The scripture
# sheet's own var() references are added automatically on top of this.
CORE_VARS = (
    "PrimaryColor", "PrimaryDarkColor", "AccentColor", "ControlColor",
    "BackgroundColor", "DialogBackgroundColor", "FootnoteBackgroundColor",
    "TextColor", "TitlesColor", "HintColor", "LinkColor", "VerseNumberColor",
    "TextHighlightColor", "DividerColor",
)

# Scoping. `:is(#container,.reader-root)` serves both the SAB element and the
# reader's own wrapper; the data-iso attribute keeps one language's rules from
# bleeding into another when several deltas are loaded side by side.
#
# Specificity is preserved EXACTLY, because the shared sheet and a language's
# overrides must resolve against each other like the original single sheet did
# (an override that gained an extra id/class would beat shared rules it used to
# lose to). `:where()` contributes zero:
#   scope()       rules about the container itself:  (1,0,0) like `#container`
#                 `:is(#container,.reader-root):where([data-iso="x"])`
#   scope_desc()  rules about descendants:           (0,0,0), adds nothing
#                 `:where(:is(#container,.reader-root)[data-iso="x"]) <selector>`
def scope(iso: str) -> str:
    return f':is(#container,.reader-root):where([data-iso="{iso}"])'


def scope_desc(iso: str) -> str:
    return f':where(:is(#container,.reader-root)[data-iso="{iso}"])'


# ---------------------------------------------------------------- flat rules

Key = tuple[str, str]       # (@media wrapper or "", selector-list prelude)


def flatten(rules: list[S.Rule]) -> list[tuple[Key, list[tuple[str, str]]]]:
    out: list[tuple[Key, list[tuple[str, str]]]] = []
    for r in rules:
        if r.is_group:
            out += [((r.prelude, c.prelude), c.decls) for c in r.children]
        else:
            out.append((("", r.prelude), r.decls))
    return out


def effective(layers: list[list[S.Rule]]) -> "OrderedDict[Key, OrderedDict[str, str]]":
    """Cascade-merge scripture rule layers (sab-app first, then sab-bc …):
    same key -> later declarations win per property; first-seen order kept."""
    eff: OrderedDict[Key, OrderedDict[str, str]] = OrderedDict()
    for layer in layers:
        for key, decls in flatten(layer):
            tgt = eff.setdefault(key, OrderedDict())
            for k, v in decls:
                tgt[k] = v
    return eff


def render_flat(items: list[tuple[Key, "OrderedDict[str, str]"]]) -> str:
    """Rebuild CSS text, regrouping consecutive rules of one @media wrapper."""
    out: list[str] = []
    open_wrap = ""
    for (wrap, sel), decls in items:
        if wrap != open_wrap:
            if open_wrap:
                out.append("}")
            if wrap:
                out.append(wrap + "{")
            open_wrap = wrap
        out.append(sel + "{" + ";".join(f"{k}:{v}" for k, v in decls.items()) + "}")
    if open_wrap:
        out.append("}")
    return "\n".join(out)


# ------------------------------------------------------------- per-language

class Lang:
    """Everything we need from one language's raw stylesheets."""

    def __init__(self, iso: str, iso_dir: Path):
        self.iso = iso
        raw = iso_dir / "styles" / "raw"
        apps = sorted(raw.glob("sab-app.*.css"))
        self.ok = bool(apps)
        if not self.ok:
            return
        # one hashed sab-app per language; if a stale older one is present,
        # the newest by mtime is the one info.json points at.
        app = max(apps, key=lambda p: p.stat().st_mtime)
        # sab-bc-<collection>: per-collection font/dir/footer overrides. Named
        # sab-bc-<iso> usually, but sab-bc-C01 (collection id) for some — so
        # glob, don't guess the suffix.
        bcs = sorted(raw.glob("sab-bc-*.css"))
        self.app_split = S.split(app.read_text(encoding="utf-8"))
        self.bc_splits = [S.split(p.read_text(encoding="utf-8")) for p in bcs]
        # Number of non-theme rules = which SAB build generated this sheet.
        # (info.json "version" is the same 13.3.2 for all of them; the builds
        # are told apart by rule count and, chronologically, by build stamp.)
        self.app_family = len(flatten(self.app_split.ui + self.app_split.scripture))
        self.built_ms = self._build_stamp(iso_dir)
        self.eff = effective(
            [self.app_split.scripture] + [b.scripture for b in self.bc_splits]
        )

    @staticmethod
    def _build_stamp(iso_dir: Path) -> int | None:
        try:
            v = json.loads((iso_dir / "info.json").read_text(encoding="utf-8")).get("version") or ""
        except (OSError, ValueError):
            return None
        tail = v.rsplit("-", 1)[-1]
        return int(tail) if tail.isdigit() else None

    # @font-face + #container base + 3 theme blocks -------------------------
    def base_decls(self) -> "OrderedDict[str, str]":
        d: OrderedDict[str, str] = OrderedDict()
        for sp in [self.app_split, *self.bc_splits]:
            for r in sp.base:
                for k, v in r.decls:
                    d[k] = v
        return d

    def themes(self) -> "OrderedDict[str, OrderedDict[str, str]]":
        t: OrderedDict[str, OrderedDict[str, str]] = OrderedDict()
        for sp in [self.app_split, *self.bc_splits]:
            for r in sp.theme:
                m = re.search(r"data-color-theme=([A-Za-z0-9_-]+)", r.prelude)
                if not m:
                    continue
                name = m.group(1)
                tgt = t.setdefault(name, OrderedDict())
                for k, v in r.decls:
                    tgt[k] = v
        return t

    def font_faces(self) -> list[OrderedDict[str, str]]:
        out = []
        for sp in [self.app_split, *self.bc_splits]:
            out += [OrderedDict(r.decls) for r in sp.font]
        return out


# ------------------------------------------------------------ shared baseline

def build_baseline(langs: list[Lang]) -> "OrderedDict[Key, OrderedDict[str, str]]":
    """Rule-wise majority over every language's effective scripture rules.

    A rule (key) is in the baseline when more than half of the languages have
    it; its declarations are the most common rendering among those languages.
    Order follows the most common whole-sheet rendering so the cascade matches
    what the majority of languages ship today."""
    n = len(langs)
    by_key: dict[Key, Counter] = {}
    for lg in langs:
        for key, decls in lg.eff.items():
            by_key.setdefault(key, Counter())[tuple(decls.items())] += 1

    sheet_counts = Counter(tuple((k, tuple(d.items())) for k, d in lg.eff.items()) for lg in langs)
    modal = next(iter(OrderedDict(sheet_counts.most_common(1))))

    order: list[Key] = [k for k, _ in modal]
    seen = set(order)
    # majority keys the modal sheet lacks (rare): append in first-seen order
    for lg in langs:
        for key in lg.eff:
            if key not in seen:
                seen.add(key)
                order.append(key)

    base: OrderedDict[Key, OrderedDict[str, str]] = OrderedDict()
    for key in order:
        c = by_key[key]
        if sum(c.values()) * 2 <= n:
            continue
        base[key] = OrderedDict(c.most_common(1)[0][0])
    return base


SHARED_HEADER = (
    "/* SAB scripture stylesheet — shared by every language.\n"
    " * Bible-text rules only (paragraphs, verses, headings, poetry, lists, tables,\n"
    " * footnotes, cross-refs, figures, inline video, study-bible blocks).\n"
    " * App chrome (contents/plan/settings/audio/annotations …) is NOT included.\n"
    " * Pair with <iso>/styles/delta.css (fonts, base size, colours, overrides).\n"
    " * Generated by scripts/build_styles.py — do not edit by hand. */\n"
)


# ---------------------------------------------------------------- delta build

VAR_RE = re.compile(r"var\(\s*(--[A-Za-z0-9_-]+)")
FONT_NAME_RE = re.compile(r"\bfont(\d+)\b")


def scope_font(value: str, iso: str) -> str:
    """font1 -> <iso>-font1, matching the renamed @font-face families."""
    return FONT_NAME_RE.sub(lambda m: f"{iso}-font{m.group(1)}", value)


def scope_selector(sel: str, iso: str) -> str:
    sel = sel.strip()
    sc = scope(iso)
    if sel == "#container":
        return sc
    if sel.startswith("#container"):
        return sc + sel[len("#container"):]
    return f"{scope_desc(iso)} {sel}"


def scope_prelude(prelude: str, iso: str) -> str:
    return ",".join(scope_selector(s, iso) for s in S.split_selectors(prelude))


def override_rules(
    iso: str,
    eff: "OrderedDict[Key, OrderedDict[str, str]]",
    base: "OrderedDict[Key, OrderedDict[str, str]]",
) -> list[tuple[Key, "OrderedDict[str, str]"]]:
    """Rules this language needs ON TOP of the shared sheet, already scoped.

    - rule absent from baseline           -> emit as-is
    - rule differs from baseline          -> emit the language's full rule, and
                                             `unset` any property the baseline
                                             has but the language does not
    - baseline rule the language lacks    -> `unset` every baseline property
    """
    out: list[tuple[Key, OrderedDict[str, str]]] = []
    for key, decls in eff.items():
        b = base.get(key)
        if b is not None and b == decls:
            continue
        d = OrderedDict((k, scope_font(v, iso)) for k, v in decls.items())
        if b is not None:
            for k in b:
                d.setdefault(k, "unset")
        out.append(((key[0], scope_prelude(key[1], iso)), d))
    for key, b in base.items():
        if key not in eff:
            out.append(((key[0], scope_prelude(key[1], iso)),
                        OrderedDict((k, "unset") for k in b)))
    return out


def referenced_vars(css: str) -> set[str]:
    return set(VAR_RE.findall(css))


def trim_theme(theme: "OrderedDict[str, str]", wanted: set[str]) -> "OrderedDict[str, str]":
    """Keep the wanted variables plus anything they transitively reference."""
    keep = {f"--{v}" for v in CORE_VARS} | wanted
    changed = True
    while changed:
        changed = False
        for k, v in theme.items():
            if k in keep:
                for r in VAR_RE.findall(v):
                    if r not in keep:
                        keep.add(r)
                        changed = True
    return OrderedDict((k, v) for k, v in theme.items() if k in keep)


def build_delta(
    lg: Lang,
    base: "OrderedDict[Key, OrderedDict[str, str]]",
    shared_vars: set[str],
    shared_hash: str,
    fonts: "FontPool",
) -> tuple[str, dict]:
    iso = lg.iso
    sc = scope(iso)
    lines = [
        f"/* Per-language stylesheet for {iso} (generated by scripts/build_styles.py).",
        f" * Load AFTER {SHARED_FROM_ISO} (sab-scripture.{shared_hash}).",
        f' * Needs data-iso="{iso}" on #container (or .reader-root) and',
        " * data-color-theme=Normal|Sepia|Dark on the same element. */",
    ]

    # fonts: font family renamed <iso>-<family> so languages can coexist
    nfaces = 0
    missing: list[str] = []
    for ff in lg.font_faces():
        family = ff.get("font-family", "").strip().strip("\"'")
        m = re.search(r"url\(\s*\.?/?([^)\s\"']+)\s*\)", ff.get("src", ""))
        if not family or not m:
            continue
        base_name = m.group(1).split("/")[-1].split(".")[0]
        pool_name = fonts.resolve(iso, base_name)
        if not pool_name:
            missing.append(m.group(1).split("/")[-1])
            continue
        lines.append(
            "@font-face{"
            f"font-family:{iso}-{family};"
            f'src:url(../../_fonts/{pool_name}) format("truetype");'
            f'font-weight:{ff.get("font-weight", "400")};'
            f'font-style:{ff.get("font-style", "normal")}}}'
        )
        nfaces += 1

    # base text rule
    bd = lg.base_decls()
    if bd:
        lines.append(sc + "{" + ";".join(f"{k}:{scope_font(v, iso)}" for k, v in bd.items()) + "}")

    # scripture overrides
    ov = override_rules(iso, lg.eff, base)
    ov_css = render_flat(ov)

    # colour themes, trimmed to what the scripture sheet + overrides use
    wanted = shared_vars | referenced_vars(ov_css) | referenced_vars(" ".join(bd.values()))
    for name, theme in lg.themes().items():
        t = trim_theme(theme, wanted)
        lines.append(f'{sc}[data-color-theme={name}]{{' + ";".join(f"{k}:{v}" for k, v in t.items()) + "}")

    if ov_css:
        lines.append(ov_css)

    return "\n".join(lines) + "\n", {"faces": nfaces, "overrides": len(ov), "missing_fonts": missing}


# ------------------------------------------------------------------- driver

class FontPool:
    """Resolves a SAB font reference (`<Name>.<sabhash>.ttf`) to its file in
    the shared pool (`<Name>.<sha8>.ttf`, see dedupe_assets.py).

    Primary route is the pool index's per-iso list. Fallback: a few languages
    were fetched but never attached to the index (their fonts/ dir is gone), so
    match on font name + exact byte size from the language's own info.json —
    unique in practice, and never guessed when ambiguous."""

    def __init__(self) -> None:
        self.by_iso: dict[str, dict[str, str]] = {}
        self.by_base: dict[str, list[tuple[str, int]]] = {}
        idx_path = FONTS_POOL / "index.json"
        if idx_path.exists():
            for info in json.loads(idx_path.read_text(encoding="utf-8")).values():
                base = info["name"].split(".")[0]
                self.by_base.setdefault(base, []).append((info["name"], info.get("size", -1)))
                for iso in info["isos"]:
                    self.by_iso.setdefault(iso, {})[base] = info["name"]
        self.names = {n for v in self.by_base.values() for n, _ in v}

    def _asset_sizes(self, iso: str) -> dict[str, int]:
        p = POOL_DIR / iso / "info.json"
        if not p.exists():
            return {}
        info = json.loads(p.read_text(encoding="utf-8"))
        return {a["name"].split(".")[0]: a.get("size", -1)
                for a in info.get("assets", []) if a.get("kind") == "font"}

    def resolve(self, iso: str, base: str) -> str | None:
        hit = self.by_iso.get(iso, {}).get(base)
        if hit:
            return hit
        cands = self.by_base.get(base, [])
        if len(cands) == 1:
            return cands[0][0]
        size = self._asset_sizes(iso).get(base)
        same = [n for n, sz in cands if sz == size]
        return same[0] if len(same) == 1 else None


def update_info_json(iso: str, shared_hash: str) -> bool:
    p = POOL_DIR / iso / "info.json"
    if not p.exists():
        return False
    info = json.loads(p.read_text(encoding="utf-8"))
    info["style_delta"] = "styles/delta.css"
    info["style_shared"] = SHARED_FROM_ISO
    info["style_shared_hash"] = shared_hash
    info.pop("style_bundle", None)
    return write_if_changed(p, json.dumps(info, indent=2, ensure_ascii=False))


def build_all() -> dict:
    isos = sorted(d.name for d in POOL_DIR.iterdir()
                  if d.is_dir() and not d.name.startswith("_"))
    langs = [lg for lg in (Lang(i, POOL_DIR / i) for i in isos) if lg.ok]
    if not langs:
        return {"languages": 0}

    base = build_baseline(langs)
    shared_css = SHARED_HEADER + render_flat(list(base.items())) + "\n"
    shared_hash = hashlib.sha256(shared_css.encode()).hexdigest()[:8]
    shared_vars = referenced_vars(shared_css)

    STYLES_DIR.mkdir(parents=True, exist_ok=True)
    hashed = f"{SHARED_NAME}.{shared_hash}.css"
    write_if_changed(STYLES_DIR / f"{SHARED_NAME}.css", shared_css)
    write_if_changed(STYLES_DIR / hashed, shared_css)
    for stale in STYLES_DIR.glob(f"{SHARED_NAME}.*.css"):
        if stale.name != hashed:
            stale.unlink()

    fonts = FontPool()
    stats = Counter()
    sizes: list[int] = []
    per_lang: dict[str, dict] = {}
    for lg in langs:
        css, meta = build_delta(lg, base, shared_vars, shared_hash, fonts)
        if write_if_changed(POOL_DIR / lg.iso / "styles" / "delta.css", css):
            stats["delta_written"] += 1
        if update_info_json(lg.iso, shared_hash):
            stats["info_written"] += 1
        sizes.append(len(css.encode()))
        stats["with_overrides"] += 1 if meta["overrides"] else 0
        per_lang[lg.iso] = meta

    fams: dict[int, list[Lang]] = {}
    for lg in langs:
        fams.setdefault(lg.app_family, []).append(lg)

    def day(ms: int) -> str:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d")

    families = []
    for n, members in sorted(fams.items(), key=lambda kv: -len(kv[1])):
        stamps = [m.built_ms for m in members if m.built_ms]
        families.append({
            "rules": n, "languages": len(members),
            **({"built": [day(min(stamps)), day(max(stamps))]} if stamps else {}),
        })

    index = {
        "version": 1,
        "shared": {
            "path": SHARED_REL,
            "hashed": f"_styles/{hashed}",
            "sha8": shared_hash,
            "bytes": len(shared_css.encode()),
            "rules": len(base),
        },
        "languages": len(langs),
        # sab-app.css builds seen (UI rules differ between builds; scripture
        # rules do not — that is what makes one shared sheet possible).
        "sab_app_builds": families,
        "languages_with_scripture_overrides": stats["with_overrides"],
        "delta_bytes": {"min": min(sizes), "max": max(sizes), "avg": sum(sizes) // len(sizes)},
        "overrides": {iso: m["overrides"] for iso, m in sorted(per_lang.items()) if m["overrides"]},
        "missing_fonts": {iso: m["missing_fonts"] for iso, m in sorted(per_lang.items()) if m["missing_fonts"]},
    }
    write_if_changed(STYLES_DIR / "index.json", json.dumps(index, indent=2, sort_keys=False) + "\n")
    return {"languages": len(langs), "shared_bytes": len(shared_css.encode()),
            "shared_rules": len(base), "shared_hash": shared_hash,
            "with_overrides": stats["with_overrides"], **dict(stats),
            "delta_avg": index["delta_bytes"]["avg"], "delta_max": index["delta_bytes"]["max"]}


def main() -> int:
    r = build_all()
    if not r.get("languages"):
        print("[styles] no sab-app stylesheets found; nothing to do")
        return 0
    print(f"[styles] shared {SHARED_REL}: {r['shared_rules']} rules, "
          f"{r['shared_bytes']} B (sab-scripture.{r['shared_hash']})")
    print(f"[styles] {r['languages']} languages · {r['with_overrides']} carry scripture overrides · "
          f"delta.css avg {r['delta_avg']} B / max {r['delta_max']} B · "
          f"{r.get('delta_written', 0)} delta + {r.get('info_written', 0)} info.json rewritten")
    return 0


if __name__ == "__main__":
    sys.exit(main())
