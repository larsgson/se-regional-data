# se-regional-data

Data pipeline for [`se-regional-pwa`](https://github.com/larsgson/bw-se-regional-pwa). Pulls Scripture Earth (`scriptureearth.org`) Proskomma `.pkf` archives, catalogs, fonts, and per-language video/audio manifests for one country at a time, and publishes the result as a single GitHub Release.

Everything runs on your local machine — the pipeline, the packaging, and the `gh release create` call. The consuming PWA pulls the latest release as a build-time fixture; nothing in this repo is served at runtime.

Code is MIT-licensed. Released data is upstream-licensed (CC BY-NC-ND 4.0 per included language); see [`LICENSING.md`](LICENSING.md).

## Requirements

- **Python 3.11+** — drives the whole pipeline. Stdlib only; no `pip install` needed.
- **Node 20+** with `npm install` — only required by `scripts/map_figures.mjs`, which uses `proskomma-core` to parse `.pkf` succinct docsets.
- **Homebrew / apt tools** — `tar`, `zstd`, `gh` (GitHub CLI), `jq` (for ad-hoc inspection, optional).

## Pipeline

```bash
npm install                # one-time; installs proskomma-core for map_figures.mjs only
make pipeline              # fetch + dedupe + map-figures + map-media; ~18 min for COUNTRY=mx
```

Individual stages: `make fetch`, `make dedupe`, `make map-figures`, `make map-media`.

`make classify` probes each ISO's SE deployment, writes `data/pkf/licenses.json` (the audit record), and rewrites the auto-managed block of [`EXCLUDED_ISOS.txt`](EXCLUDED_ISOS.txt) so the release-time pack step automatically excludes any non-CC ISO it found. Uses a per-iso on-disk cache at `data/.license-scan-cache/` keyed on the SW chunk-paths hash, so re-runs are nearly free (SE redeploys auto-invalidate the cache). `make classify-rescan` drops the cache first for a full rescan (~30 min sequential). `make release` invokes the classifier automatically before packing.

Output lives under `data/pkf/` (gitignored, ~170 MB):

- `data/pkf/manifest.json` — top-level summary keyed by ISO with version + pkf bytes.
- `data/pkf/<iso>/` — per-language `info.json`, `*.pkf`, `*.json` catalog, `styles/delta.css`.
- `data/pkf/_fonts/` — deduplicated font pool shared across all languages.

`unclassified.txt` (from `fetch_pkf.py`) lists ISOs discovered for the country that have no SAB app.

## Cutting a release

Requires `gh auth login` against the repo. Driven via `make`:

```bash
make release                           # full publish, country=mx, tag=data-YYYY.MM.DD
make release-dry                       # pack + diff, print what would upload; nothing leaves the machine
make release-draft                     # publish as a hidden draft on GitHub (still uploads)
make release COUNTRY=mx TAG=data-2026.04.23
make pack                              # just the tarball + index.json
make notes                             # just release/release-notes.md
make clean                             # rm -rf release/
make help                              # list targets + current var values
```

`release-dry` is the way to preview without touching GitHub — it prints the paths, sizes, and notes it *would* upload, then exits. Override compression with `ZSTD_LEVEL=15 make pack` (default `19`).

`make release` packs `data/pkf/`, writes release notes (diff vs. the previous release), and calls `gh release create`.

## Release format

Each release has tag `data-YYYY.MM.DD` and three assets:

| Asset                                | Purpose                                                     |
|--------------------------------------|-------------------------------------------------------------|
| `pkf-<country>-<YYYYMMDD>.tar.zst`   | CC-licensed `data/pkf/*` tree, zstd-compressed              |
| `manifest-<country>-<YYYYMMDD>.json` | Sibling copy of `data/pkf/manifest.json` for cheap diffs    |
| `licenses-<country>-<YYYYMMDD>.json` | Per-ISO classification result (`included` / `excluded` + why) |
| `index.json`                         | `{ version, created_at, bytes, sha256, tag, asset, licenses_summary, … }` |

The consuming PWA's build step downloads `index.json` (tiny), verifies `sha256`, then pulls `asset` and untars into its own fixtures directory.

## Licensing

- **Code:** MIT — see [`LICENSE`](LICENSE).
- **Data in releases:** every included language is CC BY-NC-ND 4.0 International. Per-language attribution lives in `info.json` inside the release tarball; the canonical credit is the SE app's "About" / "Copyright" screen for that language.
- **Excluded ISOs:** [`EXCLUDED_ISOS.txt`](EXCLUDED_ISOS.txt) lists languages that are dropped entirely from every release tarball and the published `manifest.json`. The auto-managed block in that file is rewritten by `make classify`; the manual section above it is preserved for ISOs the classifier can't catch. The pack step fails loudly if an excluded ISO ever leaks into the tar.
- **Excluded companion packages:** [`EXCLUDED_PACKAGES.txt`](EXCLUDED_PACKAGES.txt) lists package basenames (e.g. `spa_SPA`) that get stripped from any iso shipping them — keeping the iso, dropping just the non-CC companion. Used for diglot deployments that bundle a Biblica NVI Spanish package alongside the native CC scripture.

Full background and downstream attribution requirements: [`LICENSING.md`](LICENSING.md).

## What lives where

Python (stdlib only):

- `scripts/fetch_pkf.py` — discovers and downloads PKF + catalog + CSS + fonts.
- `scripts/dedupe_assets.py` — consolidates fonts into `_fonts/`, emits per-iso `delta.css`.
- `scripts/map_media.py` — scrapes SE's main JS chunk for video + audio manifests.
- `scripts/classify_licenses.py` — probes SE per ISO, emits `data/pkf/licenses.json`, rewrites the auto block of `EXCLUDED_ISOS.txt`. With `--prune`, also strips non-CC ISO dirs and rewrites `manifest.json`.
- `scripts/pack_release.py` — builds the release tarball + `index.json`, stages manifest + licenses assets, sanity-checks against excluded-iso leaks.
- `scripts/diff_manifest.py` — writes `release-notes.md` by diffing against the previous release's manifest asset. `SKIP_TAG=<tag>` env skips a named release (for regenerating notes for an already-published release).
- `scripts/_lib.py` — shared `load_excluded_isos` / `filter_manifest` helpers.
- `scripts/release.sh` — thin wrapper: classify + pack + diff + `gh release create`.

Node (required for this one only):

- `scripts/map_figures.mjs` — uses `proskomma-core` + `fflate` to parse `.pkf` succinct docsets and populate `figure_urls` in each `info.json`. No Python equivalent of Proskomma exists.

Ad-hoc debugging helpers (not wired into `make`):

- `scripts/probe_*.mjs`, `scripts/scan_media.mjs`.
