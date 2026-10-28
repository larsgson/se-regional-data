# Client data spec — `cdn.bibel.wiki/pkf/`

Everything a client fetches from the PKF source at runtime, and how to use
each part. This is the "PKF" source documented in
[`bcv-commons/bibles`](https://github.com/bcv-commons/bibles) (the discovery
layer indexing Bible content across PKF, DBT, and helloAO) — see its
[`doc/sources.md`](https://github.com/bcv-commons/bibles/blob/main/doc/sources.md)
for how PKF fits alongside the other sources. All paths here are served from
Cloudflare R2 behind the CDN base URL and are CORS-enabled for `GET`/`HEAD`.

**Base URL:** `https://cdn.bibel.wiki/pkf/`
(configured client-side as `PUBLIC_ASSET_BASE` + `/pkf/`)

Data is licence-filtered before publication: only CC / public-domain languages
are present. A language absent from the CDN was excluded on purpose — treat a
`404` as "not available", never as an error to retry.

---

## 1. Delivery model at a glance

```
_vrs/<code>.vrs                         # named standard versification schemes — DBT-owned (§3)
pkf/
  manifest.json                         # index of every published language
  _vrs/<hash>.vrs                       # content-hashed versification specs — ours (§3)
  _styles/
    sab-scripture.css                   # shared Bible-text rules for EVERY language (§7)
    sab-scripture.<sha8>.css            # same bytes, content-hashed (immutable)
    index.json                          # what the shared sheet was built from
  _fonts/<name>.<sha8>.ttf              # shared font pool, referenced by delta.css (§7)
  _app/
    nav-base.json                       # shared English UI-string set (§6.5)
    nav-<code>.json                     # optional shared localized UI-string sets (es/pt/fr/nl/id) (§6.5)
  <iso>/
    info.json                           # per-language metadata + media manifest
    license-notice.html                 # full copyright/attribution page, verbatim (§6.7)
    <collection>.<hash>.pkf             # scripture text (Proskomma docset)
    <collection>.<hash>.json            # Proskomma catalog (book list, structure)
    app-config.json                     # UI config: navigation strings, features
    sofria/<BOOK>/<chapter>.sofria.json # one chapter as Sofria — read this instead of unpacking the .pkf (§7a)
    glossary.json                       # {term: definition}, only where the language has a glossary
    styles/
      delta.css                         # this language's fonts, base text, colour themes (§7)
      raw/*.css                         # original SAB stylesheets — reference only
    timing/<BOOK>-<chapter>.json        # verse audio timing (only where audio exists)
```

Each collection's manifest `vrs` field names a versification file. **A scheme
code** (`eng`, `org`, `orgw`, `catm`, `lxx`, `vul`, `rso`) resolves to the
DBT-owned `/_vrs/<code>.vrs` (root, so every English version shares one file); **a
hash** resolves to our `pkf/_vrs/<hash>.vrs` (§3).

Three things to internalise before the details:

- **Filenames of `.pkf` and catalog are content-hashed.** Never hardcode them —
  resolve them from `manifest.json` (or the language's `info.json`). A new
  release changes the hash (and thus the filename), which is how cache-busting
  works.
- **The filename *prefix* (before the hash) is NOT a language identifier —
  never infer language from it.** It's an arbitrary internal project label
  from the source tooling, and can differ entirely from the `<iso>` it's
  published under. Real, currently-published examples: `aom` (Ömie) ships as
  `lwm_C01.<hash>.pkf`; `uth` (ut-Hun/Dukawa) ships as `dud_C01.<hash>.pkf`.
  Neither `lwm` nor `dud` mean anything about the content — they're Wycliffe's
  own build-time labels, verified against the source deployments' own
  self-reported names and package IDs (`"Omie Bible"` / `org.wycliffe.aom.…`
  and `"ut-Hun (Dukawa) Bible"` / `org.wycliffe.dud.uthun.dukawa.…`
  respectively). **The `<iso>` key in `manifest.json` — not any filename
  substring — is the authoritative language identifier**, and it's the only
  thing to key off of. We run a corpus-wide audit cross-checking every
  published `<iso>` against its source deployment's own self-reported name (an
  independent signal from the source project itself, not just our own
  indexing) specifically to back this guarantee — see
  `scripts/audit_language_identity.py` if you want the methodology.
- **The theme/colour system ships inside the CSS, not as JSON.** All three
  colour themes live in `styles/delta.css` (see §7). `app-config.json` deliberately
  does *not* re-deliver colours — it only names the themes so you can build the
  switcher.

---

## 2. Cache semantics (what you may cache, for how long)

The CDN sets these `Cache-Control` headers. They tell you what is safe to cache
hard vs. what to re-check.

| Path | `Cache-Control` | Meaning for the client |
|---|---|---|
| `<iso>/<collection>.<hash>.pkf`, catalog, `<iso>/styles/raw/**`, `_fonts/**`, `_styles/sab-scripture.<sha8>.css` | `immutable, max-age=31536000` | Content-hashed → cache forever. A change arrives as a new filename. |
| `manifest.json`, `<iso>/info.json`, `<iso>/app-config.json`, `<iso>/license-notice.html`, `<iso>/styles/delta.css`, `_styles/sab-scripture.css`, `_styles/index.json`, `_app/nav-base.json`, `_app/nav-<code>.json` | `max-age=300` | Mutable in place. Re-validate every ~5 min; safe to hold briefly. |
| `<iso>/timing/*.json` | `max-age=3600` | Rarely changes; hold up to an hour. |
| `pkf/_vrs/<hash>.vrs` | `max-age=3600` | Our content-hashed versification specs (§3). Filenames stable per structure. |
| `/_vrs/<code>.vrs` | (DBT-set) | Named standard schemes, served by the DBT service (§3); cache per its headers. |

`_app/nav-base.json` (the shared English UI-string set, §6.5) is fetched
**once** and reused for every language — it is not per-ISO. The optional
`_app/nav-<code>.json` locales (§6.5, §6.2) are likewise shared across every
language that lists that `code`, not per-ISO — fetch each at most once.

---

## 3. `manifest.json` — the index

The first thing to fetch. An **iso-keyed map** of every available language, with
the hashed filenames of its text assets and compact **discoverability flags** so
a picker over all languages needs this one fetch — not one `info.json` per
language.

```json
{
  "updated_at": "2026-07-07T…",
  "languages": {
    "ind": {
      "nm": "Indonesian",
      "v": "Bahasa Indonesia",
      "media": "",
      "codex": "on",
      "collections": [
        {
          "pkf": "ind_C01.CN8xM8h_.pkf",
          "catalog": "ind_C01.CQGdoNr3.json",
          "pkf_bytes": 3486576,
          "media": "",
          "books": 66,
          "codex": "on",
          "vrs": "443615e816a7e382"
        }
      ]
    },
    "cco": {
      "nm": "Chinantec, Comaltepec", "media": "avt", "codex": "n",
      "collections": [
        { "pkf": "cco_cco.OQKl-t7N.pkf", "catalog": "cco_cco.BpLcyqvW.json",
          "pkf_bytes": 1017185, "media": "avt", "books": 27, "codex": "n",
          "vrs": "eng" }
      ]
    },
    "niy": {
      "nm": "Ndruna",
      "v": "Ndrǔna",
      "media": "avt",
      "codex": "on",
      "collections": [
        { "pkf": "fra_C02.Dtd78lh9.pkf", "catalog": "fra_C02.BAIxHq8g.json",
          "pkf_bytes": 4184789, "media": "",    "books": 66, "codex": "on",
          "vrs": "938e92a081140e82" },
        { "pkf": "niy_C01.NAjnnqlC.pkf", "catalog": "niy_C01.DFlMbbeS.json",
          "pkf_bytes": 2009477, "media": "avt", "books": 33, "codex": "on",
          "vrs": "7f12b3c97397ce74",
          "coverage": { "o": { "GEN": "1-50", "PSA": "1-150" }, "n": "full" } }
      ]
    }
  }
}
```

**How to use it**

- `languages` is an **object keyed by ISO** — `Object.keys(languages)` is your
  picker list; `languages["ind"]` is direct lookup, no array scan.
- The **top-level** `media` and `codex` on each entry are aggregates across its
  `collections[]` (see below) — use them to filter/badge the picker by ISO
  without walking `collections[]`.
- To load a language, take a collection's `pkf` / `catalog` and fetch
  `pkf/<iso>/<that-filename>`. That resolves the content-hashed names; the hash
  is the cache-buster (there is no `version` field).

**`nm` / `v`** — language display names, **same field names, types, and rules as
the DBT `dbt/_app/media-index.json`** (they are sourced from it, so an ISO's name
is identical across both indexes for the client-side merge):

- **`nm`** — English name, **always present**. May carry a `:` / `,`-qualified
  variant (`"English: USA"`, `"Chinantec, Comaltepec"`).
- **`v`** — vernacular name in its native script; present **only when it differs
  from `nm`** (e.g. `ind` → `"Bahasa Indonesia"`, `niy` → `"Ndrǔna"`), omitted
  otherwise. Render `v` as the primary label with `nm` as the secondary/search
  key when present; fall back to `nm` alone when it isn't.
- The few SE languages the DBT index doesn't list take their `nm` from the SIL
  ISO 639-3 reference name and carry no `v`.

**`media`** — a compact string, letters in the fixed order **`a` `v` `t`**
(audio, video, timing), present letter = available. `""` = none. Always present.

- **Scripture-Earth scope only.** These reflect media SE supplies (the
  `info.json` `media` block and the CDN `timing/` files, §8–§9). A language can
  be `media:""` here yet still have audio from **DBT / Bible Brain**, delivered
  by a *separate* repo and merged on the client side — treat these as "SE has
  it," and OR them with the DBT availability source for the full picture.
- `t` (timing) implies `a` (audio); timing without audio never occurs.

**`codex`** — a compact string of testament initials in canon order **`o` `n`
`d`** (Old Testament, New Testament, Deuterocanon), e.g. `"on"`, `"n"`, `"ond"`.
Lets the picker show "Full Bible" vs "NT only" etc. without reading the catalog.

**`collections[]`** — one entry per collection. A few languages ship more than
one; the data is **flat** (no per-ISO hierarchy) — each collection is just
another `{ pkf, catalog, … }` object. Per-collection fields:

- **`pkf` / `catalog`** — the content-hashed filenames for this collection.
- **`pkf_bytes`** — size of this collection's `.pkf` (per-collection, so you can
  budget the exact download).
- **`media`** — as above, but scoped to *this* collection. In multi-collection
  languages a flag can differ per collection (e.g. `niy` above: the French
  `fra_C02` has no audio, the Nyokon `niy_C01` has audio+timing). The top-level
  `media` is the OR across collections.
- **`books`** — bare integer: scripture book count (glossary/peripherals
  excluded).
- **`codex`** — as above, scoped to this collection; the top-level `codex` is
  the union.
- **`vrs`** — versification reference: names a `.vrs` file that reproduces this
  collection's verse structure (the last verse of every chapter) *exactly*.
  Present for every collection that has a catalog. Two forms, which resolve to
  **two different locations**:
  - a **standard scheme code** — one of `eng` (English), `org` (Original/Hebrew),
    `orgw` (org-western: org Psalms, Western Malachi), `catm` (Catholic-Masoretic),
    `lxx` (Septuagint), `vul` (Vulgate), `rso` (Russian Orthodox) — asserted only
    when that standard scheme *fully and truthfully* describes the text, so every
    English version shares one file. **Resolve at `https://cdn.bibel.wiki/_vrs/<code>.vrs`.**
  - a **content hash** (e.g. `443615e816a7e382`) when the text matches no standard
    scheme and needs its own file; identical structures share one file. **Resolve
    at `https://cdn.bibel.wiki/pkf/_vrs/<hash>.vrs`.**

  Routing rule: if `vrs` is one of the seven scheme codes above → root `/_vrs/`;
  otherwise (a hash) → `pkf/_vrs/`. The files are **scheme-only** — per-translation
  merges and excluded/renumbered verses are *not* duplicated here; those live in
  the catalog's `versesByChapters` (§5). So `vrs` tells you the numbering system;
  `versesByChapters` tells you exactly which verses this text contains.

  **Ownership.** The named standard schemes at `/_vrs/` are **owned and published
  by the DBT versification service** (the authority — it holds the reference
  Septuagint/Vulgate exemplars, and its scheme set may grow). This repository
  publishes **only the content-hashed `pkf/_vrs/<hash>.vrs` files** and references
  the standard files by code. A collection gets a standard code only when *DBT's*
  file provably reproduces its structure, so the reference is always valid. `vrs`
  is always a resolvable file — never a "no scheme" sentinel.
- **`coverage`** — present **only when the collection is *not* full** (i.e. it
  doesn't contain, in their entirety, the testaments its `codex` marks; the
  common cases are book portions or scattered-chapter selections). Its **absence
  means full coverage** — every OT/NT book the `codex` claims is complete, so no
  map is needed. When present, it maps present chapters per testament:

  ```jsonc
  "coverage": {
    "o": { "GEN": "1-50", "EXO": "1-20,24,31-34,40" },  // partial OT: per-book chapter ranges
    "n": "full",                                         // NT complete
    "d": { "TOB": "1-14" }                               // deuterocanon present (always mapped)
  }
  ```
  A testament key is `"full"` when that whole testament is present, or an
  object of `{ BOOK: "chapter-ranges" }` when partial. Deuterocanon (`d`) is
  bonus — it never by itself makes a collection "partial", but it is listed in
  the map when the collection is partial for another reason.

Currently the only published multi-collection language is **`niy`** (ships 2:
a French `fra_C02` + the Nyokon `niy_C01`); every other language has exactly one
collection. (`hch` and `poi` also *source* a second collection each, but its
companion is a proprietary Spanish text stripped at publish time — see
[`EXCLUDED_PACKAGES.txt`](../EXCLUDED_PACKAGES.txt) — so they ship 1 in what's
actually delivered.) Always read the array length; don't hardcode this list.

---

## 4. `<iso>/<collection>.<hash>.pkf` — the scripture text

A frozen Proskomma **succinct docset** (gzip-compressed JSON). Contains the
actual verses, chapter/verse structure, footnotes, cross-references, and
per-book headers.

**How to use it**

- Load with `proskomma-core` (same library the pipeline uses): decompress →
  `loadSuccinctDocSet`. Query text via GraphQL against the docset.
- One `.pkf` per collection; `niy` (§3) ships two. Use every entry in
  `manifest.languages[<iso>].collections[]` (each carries its own `pkf` and
  `catalog`) — never assume a fixed count.

The `.pkf` is the source of truth for **text**; it is *not* where UI strings,
navigation labels, or styling live — those are §6 and §7.

---

## 5. `<iso>/<collection>.<hash>.json` — the catalog

Companion to the `.pkf`. Lists the documents (books) in the docset and their
structure. Each document carries the **localised book name**:

```json
{
  "id": "C01",
  "documents": [
    { "bookCode": "GEN", "h": "Kejadian", "toc": "Kejadian",
      "toc2": "Kejadian", "toc3": "Kej", "versesByChapters": { … } }
  ]
}
```

**How to use it**

- `bookCode` = USFM code (join key everywhere). `h`/`toc` = full localised name,
  `toc3` = short abbreviation.
- `versesByChapters` tells you the chapter/verse counts for navigation bounds.
- You *can* read book names straight from here. `app-config.books` (§6.4)
  exposes the same names in canonical order with grouping — use whichever is
  more convenient; they agree.

**Glossary:** if the collection has a glossary, it appears here as an extra
document with `bookCode: "GLO"` (alongside the scripture books) — i.e. it is
**already inside the `.pkf`/catalog you ship**, not a separate download. Render
it like any other document in the docset. Its UI is gated by the feature flags
`book-show-glossary` and `show-glossary-words` (§6.3); glossary word links in
the text are controlled by `settings-glossary-links`.

---

## 6. `<iso>/app-config.json` — UI configuration  *(NEW)*

The reader's **behaviour and per-collection identity**. Contains only data that
has no counterpart elsewhere in the delivery — colours/styling are in the CSS
(§7), and the UI strings are the single shared `_app/nav-base.json` (§6.5), not
here.

```json
{
  "collection":         { … },   // §6.1  identity
  "interfaceLanguages": { … },   // §6.2  UI-language options (informational)
  "features":           { … },   // §6.3  behaviour flags
  "books":              [ … ],   // §6.4  localised book nav labels (ordered)
  "themeNames":         [ … ],   // §6.6  theme switcher entries (colours come from CSS)
  "copyright":          { … }    // §6.7  licence + attribution
}
```

### 6.1 `collection` — identity

```json
"collection": {
  "id": "C01",
  "versionId": "ind_C01",
  "name": "Indonesian",
  "abbreviation": "IND",
  "languageCode": "ind",
  "textDirection": "ltr"
}
```

**Use:** app/title-bar label; set `<html lang>` from `languageCode` and the root
`dir` from `textDirection`. `id` is the docset id inside the `.pkf`/catalog (not
unique across languages). `versionId` **is** the stable, unique key for *this
specific text edition* — it's the `.pkf`/catalog filename stem (e.g. `ind_C01`,
`cco_cco`, `tpl_tcf`). Use it for downstream cross-referencing / de-duplicating
against other resources that carry several editions per language. For the rare
multi-collection languages this is the **primary** collection's key; the other
editions' `versionId`s are the remaining `manifest` `collections[].pkf` stems
(see §3–§4).
Read these synchronously at load — everything else can be deferred.

### 6.2 `interfaceLanguages` — UI language options

```json
"interfaceLanguages": {
  "default": "id",
  "available": [
    { "code": "en", "displayName": "Inggris",   "textDirection": "ltr" },
    { "code": "id", "displayName": "Indonesia",  "textDirection": "ltr" }
  ]
}
```

**Use:** the interface languages the *original* deployment offered — and,
**for any `code` other than `en`, also a real, fetchable UI-string set**: try
`GET _app/nav-<code>.json` (§6.5). Not every `code` has one (a handful of
sources offer a from-scratch translation into their own single-digit-deployment
language that wasn't broad enough to reconcile — see §6.5), so treat a 404 on
`nav-<code>.json` as "fall back to English," not an error. `default` was the
source app's initial selection — a reasonable default for which locale to try
first, alongside `en`.

### 6.3 `features` — behaviour flags

SAB's own feature keys, passed through verbatim as a single flat map. The source
splits these across **two** objects — global app features (`start-at-reference`,
`search-*`, `book-select`, annotations, history) and collection-level features
(`show-scripture-refs`, the `ref-*-separator` group, `show-footnotes`,
`show-cross-refs`). **They are merged here into one map** so the client has a
single lookup; collection-level keys win on the (rare) key collision. There are
~60 keys in total; representative subset:

```json
"features": {
  "start-at-reference": "JHN.1",
  "book-select": "grid",
  "book-group-titles": true,
  "search": true,
  "show-footnotes": true,
  "show-cross-refs": true,
  "app-layout-direction": "ltr",
  "ref-chapter-verse-separator": ".",
  "ref-verse-range-separator": "-",
  "ref-verse-list-separator": ","
}
```

**Use:**

- `start-at-reference` — where the reader opens (here: John 1, not Genesis 1).
- `book-select` — `"grid"` vs `"list"` book-picker layout.
- `book-group-titles` — show testament/section headings in the picker.
- `show-footnotes` / `show-cross-refs` — gate rendering of those markers.
- `ref-*-separator` — how to **format** a reference (`John 3.16`) and **parse**
  user-typed references in search. These vary by language convention, so read
  them from here rather than hardcoding.

Treat unknown keys as forward-compatible: ignore any you don't handle; new SAB
flags will appear here over time.

### 6.4 `books` — localised navigation labels (ordered)

```json
"books": [
  { "id": "GEN", "name": "Kejadian", "abbreviation": "Kej",
    "testament": "OT", "section": "Pentateuch", "chapters": 50 },
  { "id": "JHN", "name": "Yohanes",  "abbreviation": "Yoh",
    "testament": "NT", "section": "Gospels", "chapters": 21 }
]
```

**Use:** the book picker, reference display, running head, next/previous-book
navigation. Array order **is** canonical book order (so "next book" = index+1).
`name` for the picker, `abbreviation` for tight spots (breadcrumbs, chips).
Group by `testament`/`section` when `features.book-group-titles` is true.
`id` (USFM) addresses the book inside the `.pkf`. `chapters` is the chapter
count — use it to build the chapter picker and bound chapter navigation
**without loading the `.pkf`** (the catalog's `versesByChapters` gives the same
counts if you need per-chapter verse totals too).

### 6.5 UI strings — `_app/nav-base.json` + optional `_app/nav-<code>.json` locales

All UI chrome (menus, annotations panel, search, settings, plans, onboarding —
459 keys) has a **shared English set at `pkf/_app/nav-base.json`, used by
every language** — plus, for about half of languages, one or more **shared
localized sets** at `pkf/_app/nav-<code>.json` (`nav-es.json`, `nav-pt.json`,
`nav-fr.json`, `nav-nl.json`, `nav-id.json`). None of these are per-collection
and none are part of `app-config.json` — fetch `nav-base.json` **once** and
reuse it for the whole app; additionally fetch `nav-<code>.json` for any
`code` in a language's `interfaceLanguages.available` (§6.2) that isn't `en`.

```json
{
  "Menu_Bible": "Bible",
  "Menu_Contents": "Contents",
  "Menu_Search": "Search",
  "Menu_Settings": "Settings",
  "Settings_Book_Selection": "Book selection",
  "Annotation_Bookmarks": "Bookmarks"
}
```

**Use:** render any label as `navBase[key]`, or `navLocale[key] ?? navBase[key]`
once a locale set is loaded — **a locale file never has every key** (see
below), so always fall back to English per-key, not per-file. Keys are stable
SAB identifiers (`Menu_*`, `Annotation_*`, `Settings_*`, …) — use them
directly, no remap.

**Why `nav-base.json` is one shared English set:** across all 466 source
deployments the English strings were 96–98% identical (differences were only
SAB-version wording drift), so the set was reconciled once, by hand, into a
single canonical file — no per-language nav, no delta, every client uses this
one English set for the English case.

**Why the `nav-<code>.json` locales exist, and why they're partial:** SAB
ships a small library of pre-built UI translations (not one per target
scripture language — that's a separate, much bigger undertaking most of the
~600 minority-language projects never do). A deployment just opts into
whichever of these its community also reads, which is why `interfaceLanguages`
often lists a *regional* language (Spanish, Portuguese, French, Dutch,
Indonesian) unrelated to the collection's own target language. Unlike English,
these are reconciled from far fewer independent deployments (`es`: 255,
`pt`: 42, `fr`: 17, `nl`: 6, `id`: 3), and disagreement between them is
**mostly genuine** (different SAB tooling versions over time — measured, not
assumed: ~85% of `es` disagreements and ~93% of `id`'s are real wording
differences, not case drift). So each `nav-<code>.json` only includes a key
when a **confident majority** (≥60% of contributing deployments) agrees;
contested-but-published keys keep the majority wording, and keys with no
confident majority are **omitted** rather than publishing a coin flip —
expect `nav-<code>.json` to cover roughly 60–95% of `nav-base.json`'s keys,
not all 459. A source offering its *own* from-scratch translation to only
itself (fewer than 2 contributing deployments) isn't published as a locale at
all — there's nothing to reconcile against, and one sample isn't a shared,
verified set.

### 6.6 `themeNames` — theme switcher entries

```json
"themeNames": [
  { "id": "Normal", "default": true },
  { "id": "Sepia" },
  { "id": "Dark" }
]
```

**Use:** build the theme-switcher menu. The **colours are not here** — applying
a theme is a CSS operation (§7). `id` is the value you set on
`data-color-theme`. `default` marks the initial theme.

### 6.7 `copyright` — licence + attribution

```json
"copyright": {
  "license": "CC-BY-NC-ND-4.0",
  "holder": "1993, 2003, 2009, 2011, Wycliffe Bible Translators, Inc., …",
  "year": 2011,
  "years": [1993, 2003, 2009, 2011],
  "source": "https://scriptureearth.org/…",
  "notice_url": "<iso>/license-notice.html"
}
```

**Use:** the About / copyright screen, attribution, and edition dating.
`license` is present for **every** language (`CC-BY-NC-ND-4.0`, `Public Domain`,
…). `holder` is the copyright line where one was detected (present for ~83% of
languages; **may be absent**, render the licence alone then). `year` is the
most recent copyright year — the effective **edition
date**, a useful disambiguator against other resources' editions; present only
when a `holder` was found. `years` is the full ascending list and appears **only
when there was more than one** (i.e. a revision history — original + revisions).
`source` is the deployment the text came from.

This is authoritative for attribution — every published language is
licence-cleared before delivery, and this block is the machine-readable record
of under what terms. Do not omit it from a public reading view where the licence
requires attribution (CC-BY-NC-ND does).

**`notice_url`** — present when the source carried a fuller copyright/attribution
page beyond what `holder`/`year` distill (edition numbers, translation-status
caveats, contact info, related-work credits, external links — whatever the
source actually published). **Present for every included language** (source
carries no such page for a handful of hand-curated/direct sources; `holder`/
`year`/`license` are the reliable machine-readable fields regardless — this is
a bonus, richer, less structured supplement). A relative path resolving to
`pkf/<that-path>` — fetch
it for the **raw source HTML, verbatim** (small, e.g. `<div>`/`<b>`/`<a href>`
fragments — not a full HTML document; render as a fragment or strip tags for
plain text, your choice). We republish it as our own static asset specifically
so it doesn't depend on the original deployment staying reachable — the
deployment itself may be a private/staging link (see `source`) with no
durability guarantee. Cache per §2 (`max-age=300`, same tier as `info.json` /
`app-config.json`).

---

## 7. Styles — look & feel, incl. themes

A language's complete appearance is **two small files**, loaded in this order:

| File | Scope | Holds |
|---|---|---|
| `_styles/sab-scripture.css` | shared by **every** language (~21 KB; every selector is scoped to `#container` / `.reader-root`, descendant rules via `:where()` so they add no specificity — safe to load into a host page) | The Bible-text rules in SAB's own class names: paragraphs (`div.p`, `div.m`, `div.pi`…), poetry (`div.q`, `div.q2`…), headings (`div.s`, `div.ms`, `div.mt`…), verse/chapter (`span.v`, `div.c`), lists, tables (`td.tc1`…), footnotes & cross-refs, figures/captions, inline video, study-bible blocks, intro material, plus the page modes `#container.footnote`, `.crossref`, `.scrpopup`, `.glossary`, `.story`, `.single`, `.verse-by-verse` |
| `<iso>/styles/delta.css` (`info.json` → `style_delta`) | one language (~4 KB) | `@font-face` (files in `_fonts/`), base font/size/direction, the three colour themes, and — for the ~20% of languages that have any — the few scripture rules that differ from the shared sheet |

What is deliberately **not** in either file: SAB's app chrome (contents/home
screen, plans, settings, quiz, assistant, onboarding, audio bar, annotations UI,
downloads, history). A text renderer does not need it; it remains in
`<iso>/styles/raw/sab-app.<hash>.css` if you build those screens.
`raw/sab-annotations.*` and `raw/override-dab.*` are app-only / empty
respectively and can be ignored.

```html
<link rel="stylesheet" href="https://cdn.bibel.wiki/pkf/_styles/sab-scripture.css">
<link rel="stylesheet" href="https://cdn.bibel.wiki/pkf/ind/styles/delta.css">   <!-- info.json: style_delta -->
<div id="container" data-iso="ind" data-color-theme="Normal"> … rendered Sofria … </div>
```

**Rules of use**

1. Load the shared sheet first, then `delta.css`. `info.json` names both
   (`style_shared` = `../_styles/sab-scripture.css`, relative to `<iso>/`;
   `style_delta` = `styles/delta.css`). `style_shared_hash` is the sha8 of the
   shared sheet this delta was built against — the delta's header comment says
   the same. To pin an immutable URL use `_styles/sab-scripture.<sha8>.css`.
2. Put `data-iso="<iso>"` on the element that carries `id="container"` (or
   `.reader-root`). Every delta rule is scoped to that attribute, so deltas of
   several languages can be loaded in one page without interfering.
3. **Colours** are attribute-scoped custom properties in `delta.css`:

   ```css
   :is(#container,.reader-root)[data-iso="ind"][data-color-theme=Normal] { --BackgroundColor:#F2F2F2; --TextColor:#2A2A2A; … }
   :is(#container,.reader-root)[data-iso="ind"][data-color-theme=Sepia]  { … }
   :is(#container,.reader-root)[data-iso="ind"][data-color-theme=Dark]   { … }
   ```
   Switch theme by setting `data-color-theme` (a value from `themeNames`, §6.6);
   everything re-colours with no JS colour maths. `delta.css` carries every
   variable the shared sheet uses (verse numbers, links, block backgrounds,
   highlighter pens, footers …) plus the core chrome colours (`--PrimaryColor`,
   `--BackgroundColor`, `--DialogBackgroundColor`, `--LinkColor`, `--DividerColor` …).
   The full ~150-variable SAB palette (audio bar, plans, quiz …) is only in
   `raw/sab-app.*.css`.
4. **Fonts**: `@font-face` families are named `<iso>-font1` etc. and point at
   `../../_fonts/<name>.<sha8>.ttf`, so serving the tree as published resolves
   them. The few dozen distinct font files are shared across languages and cached once.
   A handful of languages reference a font SAB hosts under a name we cannot
   fetch (e.g. `eky`); their `delta.css` then has no `@font-face` and the browser
   default applies. `_styles/index.json` → `missing_fonts` lists them.
5. The shared sheet and `delta.css` are **mutable at a stable path** (5-minute
   TTL, §2) because they are regenerated together whenever a new language
   changes the shared baseline. Do not cache them for longer than that; the
   hashed `sab-scripture.<sha8>.css` is the immutable form.

How the split was derived and verified: `scripts/build_styles.py` takes, per
rule, the majority rendering across all languages as the shared baseline, and
emits everything else per language. `scripts/audit_styles.py` re-parses the
published files and checks that shared + delta reproduce every language's
original scripture rules declaration-for-declaration, that every `var()` is
defined in all three themes, and that the shared sheet contains no app chrome.

---

## 7a. Per-chapter Sofria and glossary

So a reader doesn't have to download and unpack the whole `.pkf` before showing a
chapter, the build also writes every chapter out as Sofria, the same layout as
`cdn.bibel.wiki/openbible/…`:

- `<iso>/sofria/<BOOK>/<chapter>.sofria.json` — exactly what Proskomma's
  `sofria(chapter:N)` returns. Chapter 1 carries the book title and introduction.
  Where that query throws (e.g. a table running across a chapter break) the chapter
  is cut from the whole-book Sofria with `sliceChapter` (as in bcv-commons/bibles
  `tools/sofria-render`) and the file has `"fallback": "whole-book"`.
- `info.json` → `formats.sofria.path` is the path template; resolve it against `<iso>/`.
- A companion collection (e.g. `niy`'s `fra_C02`) is under `sofria/<base>/<BOOK>/…` and
  listed in `formats.sofria.collections`; the language's own collection is at `sofria/`.
- `<iso>/glossary.json` (`{term: definition}`, from the `GLO` book) and
  `info.json` → `glossary`. Present only where a glossary exists.
- Figures and media are unchanged (`figure_urls`, `media` in `info.json`).
- Cache: mutable paths, `max-age=3600` (`glossary.json`: 300). Packages in
  `EXCLUDED_PACKAGES.txt` are not exported.

## 8. `<iso>/info.json` — metadata + media manifest

Per-language metadata plus the audio/video manifest (produced by the media
extraction step). Present for every language; the `media` block is present only
where the deployment has audio/video.

```json
{
  "iso": "ind",
  "source": "https://…",
  "assets": [ … ],          // the hashed files that make up this language
  "style_delta": "styles/delta.css",            // per-language stylesheet (§7)
  "style_shared": "../_styles/sab-scripture.css", // shared scripture stylesheet, relative to <iso>/ (§7)
  "style_shared_hash": "ac5de0fc",              // sha8 of the shared sheet this delta was built against
  "formats": { "sofria": { "path": "sofria/<BOOK>/<chapter>.sofria.json", "granularity": "chapter" } },
  "glossary": "glossary.json",                  // present only when the language has a glossary
  "media": {
    "audio": {
      "base_url": "…",
      "sources": { "h1": { "type": "fcbh", "name": "FCBH", "damId": "ACUTBLN2DA", "folder": "Achuar audio" } },
      "items": [ { "bookCode","chapter","src","damId","url","len","size","hasTiming" } ]
    },
    "videos": [ … ]
  }
}
```

**How to use it**

- `media.audio.items[]` — each chapter's streaming `url` (fetched from the
  source, not the CDN) plus `hasTiming: true` when verse-timing exists.
- Every item has `bookCode` and `chapter` (no need to parse the file name) and
  `src`, a key into `media.audio.sources`. A `fcbh` source is DBT: `damId` is the
  DBT fileset id and `filename` the file in it. Those files are **not** on our
  CDN, so `url` is null. Every non-DBT item (`d*` download, `a1` app-bundled)
  has its own percent-encoded `url`, except the few `a1` files whose location the
  deployed app doesn't reveal. Pair the timing with the DBT file `filename` in fileset
  `damId`. `len` (ms) and `size` (bytes) are copied from the app manifest as it
  reports them; they are informational, not a reliable integrity check (some items
  have no `size`, a few carry a wrong `len` or `size` for an unchanged recording).
  The source's API key is never published.
- When `hasTiming`, fetch `pkf/<iso>/timing/<BOOK>-<chapter>.json` (§9) to drive
  verse-synced playback.
- `media` absent (or empty) → the language is text-only.

---

## 9. `<iso>/timing/<BOOK>-<chapter>.json` — audio timing

Verse-level timing for synced audio highlight. Delivered only for chapters that
have it (`info.json` → `hasTiming`).

```json
[[5.14,7.82,"s1"],[7.82,12.66,"1"],[12.66,18.98,"2"],[18.98,23.52,"3a"],[23.52,27.32,"3b"]]
```

Each row is `[startSeconds, endSeconds, verseLabel]`, in playback order.

**How to use it:** as audio time advances, find the row whose
`[start,end)` contains `currentTime` and highlight `verseLabel`. Path is
predictable: `<BOOK>` is the USFM code, `<chapter>` the number
(e.g. `timing/JHN-3.json`).

**`verseLabel` is not always a plain integer** — treat it as an opaque string,
not a number to parse:
- `"s1"`, `"s2"`, … — a **section/heading marker**, not a verse. Typically
  narration before the first verse; don't map it to a `versesByChapters` entry,
  just don't highlight a verse for it.
- `"3a"`, `"3b"`, … — a **sub-verse split** (the audio narrates a merged/long
  verse in parts). Match against the corresponding merged label in the
  catalog's `versesByChapters` (§5) if you need to resolve it to a verse number;
  for simple highlighting, matching the label string as-is is enough.

---

## 10. Putting it together — a load sequence

1. `GET manifest.json` → pick language (`languages[<iso>]`) → read its
   `collections[].pkf` / `collections[].catalog` hashed filenames.
2. `GET _app/nav-base.json` **once** (cache it) → the shared English UI-string
   set; render labels as `navBase[key]`.
3. `GET <iso>/app-config.json` → set `lang`/`dir` from `collection`; keep
   `features` for behaviour, `books` for the picker, `themeNames` for the
   switcher, `copyright` for the About/attribution view. (No `nav` here — UI
   strings come from step 2.) Optionally, for any non-`en` code in
   `interfaceLanguages.available`, `GET _app/nav-<code>.json` **once** (cache
   it, reuse across every language offering that code) and merge over
   `navBase` per-key for a localized UI.
4. `GET _styles/sab-scripture.css` **once** (cache ≤ 5 min), then
   `GET <iso>/styles/delta.css` → apply both; put `data-iso="<iso>"` and
   `data-color-theme` (default from `themeNames`) on the `#container` element (§7).
5. `GET <iso>/<collection>.<hash>.pkf` (+ catalog) → render text (the `GLO`
   document, if present, is the glossary).
6. If audio: read `info.json` → `media`; per chapter with `hasTiming`, `GET` the
   matching `timing/*.json`.

`manifest.json`, `info.json`, `app-config.json`, `license-notice.html`,
`styles/delta.css`, `_styles/sab-scripture.css`, `_styles/index.json`,
`_app/nav-base.json`, `_app/nav-<code>.json` are the only files that change in
place (short TTL); everything else is content-hashed and cacheable forever.

---

## 11. Forward-compatibility contract

Two guarantees the client can rely on, so today's integration keeps working:

- **Stable resource URLs.** Each concern is fetched from a stable path
  (`<iso>/app-config.json`, `_app/nav-base.json`, `_app/nav-<code>.json`,
  `<iso>/styles/delta.css`, `_styles/sab-scripture.css`, `<iso>/timing/…`). A future server-side optimisation may
  share more data across languages behind the same URLs; the paths you fetch
  will not move.
- **Additive data.** New sections only *add* previously-unavailable data
  (behaviour flags, copyright, the UI-string sets). Existing files (`.pkf`,
  catalog, CSS, fonts) are never deprecated or reshaped; adopting `app-config.json`
  never requires abandoning anything you consume today. UI strings are flat
  key→string sets (`_app/nav-base.json` + optional `_app/nav-<code>.json`,
  §6.5) — no per-language *files per ISO*, just a small shared library; still
  no complex merge logic beyond a per-key fallback to English.
