# Licensing

The repository contains two distinct kinds of artifact, governed by different licenses.

## Code — MIT

The pipeline scripts (`scripts/`, `Makefile`, configuration) are licensed under the [MIT License](LICENSE). Use them freely.

## Data — upstream Scripture rights holders

The `.pkf` archives, catalogs, fonts, figure URLs, and audio/video manifests fetched from [`scriptureearth.org`](https://scriptureearth.org/) are not ours to relicense. We redistribute them in transformed-but-functionally-equivalent form (`.tar.zst` of the SE deployment trees) under the rights granted by their upstream owners.

For every ISO included in a release of this repository:

> **Creative Commons Attribution-NonCommercial-NoDerivatives 4.0 International** — [CC BY-NC-ND 4.0](https://creativecommons.org/licenses/by-nc-nd/4.0/).

Copyright lines (text / audio / images) vary per language; the canonical credit lives in the SE-rendered "About" / "Copyright" screen for each language. Per-language ownership and version metadata is preserved in the release `manifest.json`.

### Attribution

When using a release artifact downstream:

1. Credit the upstream rights holder for each language consumed (Wycliffe Bible Translators, Inc. is the most common; others appear per language).
2. Link back to `https://scriptureearth.org/` as the canonical source.
3. Do not modify the Scripture text content (NoDerivatives). Format conversion (e.g. `.pkf` → on-screen rendering) is permitted and is what SE itself ships.
4. Non-commercial use only.

## Excluded ISOs

`EXCLUDED_ISOS.txt` lists languages whose upstream license does **not** grant us redistribution rights. They are present in the local pipeline output for completeness but are stripped from every release tarball and from the published `manifest.json`.

Currently excluded:

| ISO | Language | Reason |
|-----|----------|--------|
| `cya` | Chatino de Nopala | Text © 2013 David Neil Nellis (used by SE with permission). No Creative Commons license was granted; treat as all rights reserved. |

If you find an additional language that lacks a CC license on its SE "About" screen, add it to `EXCLUDED_ISOS.txt` *before* the next release.

## Why this repo can be public

`CC BY-NC-ND 4.0` permits unmodified, attributed, non-commercial redistribution. Hosting the tarballs as GitHub Releases on a public repo qualifies, **provided** the excluded-ISO list above is honored. The exclusion is enforced at pack time by `scripts/pack_release.mjs`, which both filters the tar and sanity-checks the staged `manifest.json` — the release will fail loudly if an excluded ISO slips through.
