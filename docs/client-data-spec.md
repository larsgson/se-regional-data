# Client data spec — `cdn.bibel.wiki/pkf/`

Everything the `se-regional-pwa` client fetches at runtime, and how to use each
part. All paths are served from Cloudflare R2 behind the CDN base URL and are
CORS-enabled for `GET`/`HEAD`.

**Base URL:** `https://cdn.bibel.wiki/pkf/`
(configured client-side as `PUBLIC_ASSET_BASE` + `/pkf/`)

Data is licence-filtered before publication: only CC / public-domain languages
are present. A language absent from the CDN was excluded on purpose — treat a
`404` as "not available", never as an error to retry.

---

## 1. Delivery model at a glance

```
pkf/
  manifest.json                         # index of every published language
  _vrs/<vrs>.vrs                        # versification specs, deduplicated + shared   ← NEW
  <iso>/
    info.json                           # per-language metadata + media manifest
    <collection>.<hash>.pkf             # scripture text (Proskomma docset)
    <collection>.<hash>.json            # Proskomma catalog (book list, structure)
    app-config.json                     # UI config: navigation strings, features
    styles/
      bundle.css                        # ready-to-use reader stylesheet (incl. themes)
      raw/*.css                         # source stylesheets (bundle is built from these)
      fonts/*.ttf                       # the fonts this language uses
    timing/<BOOK>-<chapter>.json        # verse audio timing (only where audio exists)
```

`_vrs/` is shared across languages: each collection's manifest `vrs` field names
its file there (a standard scheme like `eng.vrs`, or a content hash), so every
English version points at the same `_vrs/eng.vrs` (§3).

Two things to internalise before the details:

- **Filenames of `.pkf` and catalog are content-hashed.** Never hardcode them —
  resolve them from `manifest.json` (or the language's `info.json`). A new
  release changes the hash (and thus the filename), which is how cache-busting
  works.
- **The theme/colour system ships inside the CSS, not as JSON.** All three
  colour themes are baked into `styles/` (see §7). `app-config.json` deliberately
  does *not* re-deliver colours — it only names the themes so you can build the
  switcher.

---

## 2. Cache semantics (what you may cache, for how long)

The CDN sets these `Cache-Control` headers. They tell you what is safe to cache
hard vs. what to re-check.

| Path | `Cache-Control` | Meaning for the client |
|---|---|---|
| `<iso>/<collection>.<hash>.pkf`, catalog, `styles/**`, `fonts/**` | `immutable, max-age=31536000` | Content-hashed → cache forever. A change arrives as a new filename. |
| `manifest.json`, `<iso>/info.json`, `<iso>/app-config.json`, `_app/nav-base.json` | `max-age=300` | Mutable in place. Re-validate every ~5 min; safe to hold briefly. |
| `<iso>/timing/*.json` | `max-age=3600` | Rarely changes; hold up to an hour. |
| `_vrs/*.vrs` | `max-age=3600` | Versification specs (§3). Filenames are stable per structure; hold up to an hour. |

`_app/nav-base.json` (the single shared English UI-string set, §6.5) is fetched
**once** and reused for every language — it is not per-ISO.

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
- **`vrs`** — versification reference: the **basename of this collection's file
  at `_vrs/<vrs>.vrs`**, which reproduces its verse structure (the last verse of
  every chapter) *exactly*. Present for every collection that has a catalog. Two
  forms:
  - a **standard Paratext scheme code** — `eng` (English), `org`
    (Original/Hebrew), `lxx` (Septuagint), `vul` (Vulgate), `rso` (Russian
    Orthodox), `rsc` (Russian Protestant) — when that standard scheme *fully and
    truthfully* describes the text. This is only asserted when true, so every
    English version shares the one `_vrs/eng.vrs` (no per-collection copy);
  - a **content hash** (e.g. `443615e816a7e382`) when the text deviates from
    every standard scheme and needs its own file. Identical structures share one
    hashed file.

  Resolve it as `_vrs/<vrs>.vrs`. The files are **scheme-only** — per-translation
  merges and excluded/renumbered verses are *not* duplicated here; those live in
  the catalog's `versesByChapters` (§5). So `vrs` tells you the numbering system;
  `versesByChapters` tells you exactly which verses this text contains.

  **All six standard scheme files are always published** (`_vrs/eng.vrs` …
  `_vrs/vul.vrs`), even ones no SE text currently uses — so external or merged
  data (e.g. Septuagint/Vulgate-versified sources) can reference any of them.
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

Currently the only multi-collection languages are **`hch`, `niy`, `poi`** (each
ships 2); every other language has exactly one collection.

---

## 4. `<iso>/<collection>.<hash>.pkf` — the scripture text

A frozen Proskomma **succinct docset** (gzip-compressed JSON). Contains the
actual verses, chapter/verse structure, footnotes, cross-references, and
per-book headers.

**How to use it**

- Load with `proskomma-core` (same library the pipeline uses): decompress →
  `loadSuccinctDocSet`. Query text via GraphQL against the docset.
- One `.pkf` per collection; the three languages named in §3 ship two. Use every
  entry in `manifest.languages[<iso>].collections[]` (each carries its own `pkf`
  and `catalog`).

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

**Use:** informational — the interface languages the *original* deployment
offered. The delivered UI strings are the single shared English set (§6.5), so
there is no per-language UI payload to switch to; treat this as metadata (e.g.
to show what the source app supported), not as a set of fetchable UI variants.
`default` was the source app's initial selection.

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

### 6.5 UI strings — the single shared `_app/nav-base.json`

All UI chrome (menus, annotations panel, search, settings, plans, onboarding —
459 keys) is **one shared English set at `pkf/_app/nav-base.json`, used by every
language.** It is *not* per-collection and is **not** part of `app-config.json`
— fetch it **once** and reuse it for the whole app.

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

**Use:** render any label as `navBase[key]`. Keys are stable SAB identifiers
(`Menu_*`, `Annotation_*`, `Settings_*`, …) — use them directly, no remap. Flat
`key → string`; no per-language file, no merge, no fallback logic.

**Why one shared set:** across all 466 source deployments the English strings
were 96–98% identical (differences were only SAB-version wording drift), so the
set was reconciled once, by hand, into a single canonical file. There is
deliberately **no per-language nav and no delta** — every client uses this one
English set. (The source deployments' own translations are not delivered; if the
PWA localises its UI, it does so with its own translation layer keyed on these
same stable identifiers.)

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
  "source": "https://scriptureearth.org/…"
}
```

**Use:** the About / copyright screen, attribution, and edition dating.
`license` is present for **every** language (`CC-BY-NC-ND-4.0`, `Public Domain`,
…). `holder` is the copyright line where one was detected (present for ~42% of
languages — mostly Wycliffe-sourced texts; **may be absent**, render the licence
alone then). `year` is the most recent copyright year — the effective **edition
date**, a useful disambiguator against other resources' editions; present only
when a `holder` was found. `years` is the full ascending list and appears **only
when there was more than one** (i.e. a revision history — original + revisions).
`source` is the deployment the text came from.

This is authoritative for attribution — every published language is
licence-cleared before delivery, and this block is the machine-readable record
of under what terms. Do not omit it from a public reading view where the licence
requires attribution (CC-BY-NC-ND does).

---

## 7. `<iso>/styles/` — look & feel, incl. themes

The complete visual appearance. **This is where colours live** — all three
themes are baked into the CSS as attribute-scoped custom-property blocks:

```css
#container[data-color-theme=Normal] { --BackgroundColor:#F2F2F2; --TextColor:#2A2A2A; … }
#container[data-color-theme=Sepia]  { --BackgroundColor:#E9D8BA; --TextColor:#5A4129; … }
#container[data-color-theme=Dark]   { --BackgroundColor:#000000; --TextColor:#E0E0E0; … }
```

**How to use it**

1. Load `styles/bundle.css` (ready-to-use; it already `@import`s / inlines the
   reader styles). You do **not** need `styles/raw/*` — those are the source the
   bundle is built from, delivered for reference only.
2. Render the reader inside an element with `id="container"` (or matching the
   selector the CSS expects).
3. **Switch theme** by setting `data-color-theme="Dark"` (a value from
   `themeNames`, §6.6). The CSS custom properties cascade and everything
   re-colours instantly — no JS colour math, no re-fetch.
4. **Fonts:** `styles/fonts/*.ttf` are referenced by the CSS `@font-face` rules
   with relative URLs, so serving the `styles/` tree together makes fonts resolve
   automatically. No separate font wiring needed.

Because colours/styling are fully contained here, treat the CSS as the single
source of visual truth and let `app-config.json` handle only strings/behaviour.

---

## 8. `<iso>/info.json` — metadata + media manifest

Per-language metadata plus the audio/video manifest (produced by the media
extraction step). Present for every language; the `media` block is present only
where the deployment has audio/video.

```json
{
  "iso": "ind",
  "source": "https://…",
  "assets": [ … ],          // the hashed files that make up this language
  "media": {
    "audio": { "base_url": "…", "items": [ { "bookCode","chapter","url","hasTiming" } ] },
    "videos": [ … ]
  }
}
```

**How to use it**

- `media.audio.items[]` — each chapter's streaming `url` (fetched from the
  source, not the CDN) plus `hasTiming: true` when verse-timing exists.
- When `hasTiming`, fetch `pkf/<iso>/timing/<BOOK>-<chapter>.json` (§9) to drive
  verse-synced playback.
- `media` absent (or empty) → the language is text-only.

---

## 9. `<iso>/timing/<BOOK>-<chapter>.json` — audio timing

Verse-level timing for synced audio highlight. Delivered only for chapters that
have it (`info.json` → `hasTiming`).

```json
[ [0.0, 4.32, "1"], [4.32, 9.10, "2"], [9.10, 13.7, "3"] ]
```

Each row is `[startSeconds, endSeconds, verseLabel]`, in playback order.

**How to use it:** as audio time advances, find the row whose
`[start,end)` contains `currentTime` and highlight `verseLabel`. Path is
predictable: `<BOOK>` is the USFM code, `<chapter>` the number
(e.g. `timing/JHN-3.json`).

---

## 10. Putting it together — a load sequence

1. `GET manifest.json` → pick language (`languages[<iso>]`) → read its
   `collections[].pkf` / `collections[].catalog` hashed filenames.
2. `GET _app/nav-base.json` **once** (cache it) → the single shared English
   UI-string set; render labels as `navBase[key]`.
3. `GET <iso>/app-config.json` → set `lang`/`dir` from `collection`; keep
   `features` for behaviour, `books` for the picker, `themeNames` for the
   switcher, `copyright` for the About/attribution view. (No `nav` here — UI
   strings come from step 2.)
4. `GET <iso>/styles/bundle.css` → apply; set `data-color-theme` from the default
   in `themeNames`.
5. `GET <iso>/<collection>.<hash>.pkf` (+ catalog) → render text (the `GLO`
   document, if present, is the glossary).
6. If audio: read `info.json` → `media`; per chapter with `hasTiming`, `GET` the
   matching `timing/*.json`.

`manifest.json`, `info.json`, `app-config.json`, `_app/nav-base.json` are the
only files that change in place (short TTL); everything else is content-hashed
and cacheable forever.

---

## 11. Forward-compatibility contract

Two guarantees the client can rely on, so today's integration keeps working:

- **Stable resource URLs.** Each concern is fetched from a stable path
  (`<iso>/app-config.json`, `_app/nav-base.json`, `<iso>/styles/…`,
  `<iso>/timing/…`). A future server-side optimisation may share more data across
  languages behind the same URLs; the paths you fetch will not move.
- **Additive data.** New sections only *add* previously-unavailable data
  (behaviour flags, copyright, the shared UI-string set). Existing files (`.pkf`,
  catalog, CSS, fonts) are never deprecated or reshaped; adopting `app-config.json`
  never requires abandoning anything you consume today. UI strings are one flat
  shared set (`_app/nav-base.json`, §6.5) — no per-language files, no merge.
