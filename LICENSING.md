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

## How exclusion is enforced

Two layers with different cadences:

1. **Audit — `scripts/classify_licenses.py`.** Probes every ISO's SE deployment, parses the in-app "Texto:" copyright block plus the JS bundle for a Creative Commons declaration, and writes `data/pkf/licenses.json`. Uses a per-iso on-disk cache at `data/.license-scan-cache/` keyed on the SW chunk-paths hash, so steady-state runs are nearly free and SE redeploys auto-invalidate. `make release` invokes the classifier automatically before packing. To force a full rescan (e.g. when you suspect SE has updated a license upstream), use `make classify-rescan`. The full classification (included + excluded with the reason for each) is published as `licenses-<country>-<YYYYMMDD>.json` next to the tarball, so downstream consumers (and auditors) can see exactly what was decided and when.

2. **Enforcement — `EXCLUDED_ISOS.txt` (read at every release).** The list of ISOs that must be excluded. Read by `scripts/pack_release.py`, which uses it to filter the tar and sanity-check the staged `manifest.json` — the release will fail loudly if an excluded ISO slips through. The file has two sections: an **auto-managed block** (between the `# BEGIN auto-managed` / `# END auto-managed` markers) that `classify_licenses.py` rewrites on every run from `licenses.json`, and a **manual section** above that block which is preserved verbatim. Hand-edit the manual section for ISOs the classifier can't catch; never edit the auto block.

Currently in `EXCLUDED_ISOS.txt`:

| ISO | Language | Reason |
|-----|----------|--------|
| `cya` | Chatino de Nopala | Text © 2013 David Neil Nellis (used by SE with permission). No Creative Commons license was granted; treat as all rights reserved. |

The classifier independently flags `cya` based on its "Usado con permiso" copy and writes it into the auto-managed block of `EXCLUDED_ISOS.txt`; that's what keeps it out of the tarball. `licenses.json` is the published audit record of how each ISO was classified, including per-iso `evidence.badge_in_sw` / `evidence.cc_text_in_js` flags so you can trace the decision.

## Why this repo can be public

`CC BY-NC-ND 4.0` permits unmodified, attributed, non-commercial redistribution. Hosting the tarballs as GitHub Releases on a public repo qualifies, provided `EXCLUDED_ISOS.txt` is kept current and `pack_release.mjs`'s sanity check passes. The audit file (`licenses.json`) serves as the publishable record of how each ISO was classified at the time the file was last refreshed.

**Operational reminder:** if you suspect SE has updated a per-language license (e.g. a previously-excluded ISO now has a CC declaration, or vice versa), the disk cache won't pick it up until SE changes the chunk hashes. Run `make classify-rescan` to drop the cache and re-probe everything.
