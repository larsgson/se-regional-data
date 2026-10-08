"""
Minimal CSS reader + rule classifier for Scripture App Builder (SAB) stylesheets.

SAB ships a single minified `sab-app.<hash>.css` per language that mixes four
concerns:

    font      @font-face blocks              (per language: which font files)
    base      the bare `#container{…}` rule  (per language: font-family, dir, size)
    theme     `#container[data-color-theme=X]{--Foo:#abc;…}` custom-property blocks
              (per language: the colour palette)
    scripture rules that style Bible text    (div.p, span.v, div.q, td.tc1, …)
    ui        rules that style the app chrome (contents-*, plan-*, settings-*, …)

`split()` separates a stylesheet into those buckets so the scripture rules can
be published once (pkf/_styles/) and only the small per-language remainder
(fonts, base, themes, a few overrides) is published per language.

Stdlib only; pure functions; no I/O.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_COMMENT_RE = re.compile(r"/\*.*?\*/", re.DOTALL)


@dataclass
class Rule:
    """One top-level CSS construct.

    `prelude` is the selector list (or the at-rule header, e.g. `@font-face`
    or `@media(min-width:500px)`); `decls` is the ordered declaration text for
    a plain rule; `children` is non-empty only for grouping at-rules (@media).
    """
    prelude: str
    decls: list[tuple[str, str]] = field(default_factory=list)
    children: list["Rule"] = field(default_factory=list)

    @property
    def is_group(self) -> bool:
        return bool(self.children)

    @property
    def selectors(self) -> list[str]:
        return split_selectors(self.prelude) if not self.prelude.startswith("@") else []

    def render(self) -> str:
        if self.children:
            return self.prelude + "{" + "".join(c.render() for c in self.children) + "}"
        return self.prelude + "{" + ";".join(f"{k}:{v}" for k, v in self.decls) + "}"


def _split_top(text: str, sep: str) -> list[str]:
    """Split on `sep` outside (), [] and quotes."""
    out: list[str] = []
    cur: list[str] = []
    depth, quote = 0, ""
    for ch in text:
        if quote:
            cur.append(ch)
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
        elif ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        elif ch == sep and depth == 0:
            out.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    out.append("".join(cur))
    return out


def split_selectors(prelude: str) -> list[str]:
    return [" ".join(s.split()) for s in _split_top(prelude, ",") if s.strip()]


def parse_decls(body: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for part in _split_top(body, ";"):
        if ":" not in part:
            continue
        k, _, v = part.partition(":")
        k = k.strip()
        if k:
            # custom properties are case-sensitive; others are not
            out.append((k if k.startswith("--") else k.lower(), v.strip()))
    return out


def parse(css: str) -> list[Rule]:
    """Parse a stylesheet into top-level Rules (one level of @media nesting)."""
    css = _COMMENT_RE.sub("", css)
    rules: list[Rule] = []
    i, n = 0, len(css)

    def read_block(start: int) -> tuple[str, int]:
        """css[start] == '{'; return (inner text, index after matching '}')."""
        depth, j, quote = 0, start, ""
        while j < n:
            ch = css[j]
            if quote:
                if ch == quote:
                    quote = ""
            elif ch in "\"'":
                quote = ch
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return css[start + 1:j], j + 1
            j += 1
        return css[start + 1:], n

    while i < n:
        while i < n and css[i].isspace():
            i += 1
        if i >= n:
            break
        if css.startswith("@import", i) or css.startswith("@charset", i):
            j = css.find(";", i)
            i = n if j < 0 else j + 1
            continue
        b = css.find("{", i)
        if b < 0:
            break
        prelude = " ".join(css[i:b].split())
        inner, i = read_block(b)
        if prelude.startswith(("@media", "@supports")):
            rules.append(Rule(prelude, children=parse(inner)))
        else:
            rules.append(Rule(prelude, decls=parse_decls(inner)))
    return rules


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------

# A rule is UI when ANY selector in its list mentions one of these class/id
# stems. The stems are SAB's own naming families for app chrome; scripture
# classes are USFM-derived (p, q, s, v, li, tc1 …) and never collide.
#
# Deliberately NOT UI (they style Bible text or its reader popups):
#   highlighting, a.audio-link/web-link/…, .collapsible-*, .study-bible-*,
#   .resource-block/-content/-title, .video-*, div.footer/span.ref, the
#   `#container.{single,verse-by-verse,scrpopup,story,glossary,footnote,
#   crossref}` page modes, and the generic basics (`*`, img, #content, .txs).
_UI_STEMS = (
    "contents", "layout", "plan", "assistant", "settings", "message",
    "onboarding", "user-account", "annotation", "history", "download",
    "feed", "search-result", "quiz", "resource-config", "about",
    "icon-", "float-",
)
_UI_RE = re.compile(r"[.#](?:" + "|".join(re.escape(s) for s in _UI_STEMS) + r")|^ui\.")
_THEME_RE = re.compile(r"^#container\[data-color-theme=[A-Za-z0-9_-]+\]$")


def kind_of(rule: Rule) -> str:
    """font | base | theme | ui | scripture"""
    if rule.prelude == "@font-face":
        return "font"
    if rule.is_group:
        kids = {kind_of(c) for c in rule.children}
        return "ui" if kids == {"ui"} else "scripture" if kids == {"scripture"} else "mixed"
    sels = rule.selectors
    if sels == ["#container"]:
        return "base"
    if sels and all(_THEME_RE.match(s) for s in sels):
        return "theme"
    if any(_UI_RE.search(s) for s in sels):
        return "ui"
    return "scripture"


@dataclass
class Split:
    font: list[Rule] = field(default_factory=list)
    base: list[Rule] = field(default_factory=list)
    theme: list[Rule] = field(default_factory=list)
    scripture: list[Rule] = field(default_factory=list)
    ui: list[Rule] = field(default_factory=list)


def split(css: str) -> Split:
    out = Split()
    for r in parse(css):
        k = kind_of(r)
        if k == "mixed":
            # a grouping rule with mixed children: split by child, keep wrapper
            for kind in ("scripture", "ui"):
                kids = [c for c in r.children if kind_of(c) == kind]
                if kids:
                    getattr(out, kind).append(Rule(r.prelude, children=kids))
            continue
        getattr(out, k).append(r)
    return out


def render(rules: list[Rule]) -> str:
    return "".join(r.render() for r in rules)
