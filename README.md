# se-regional-data

Data pipeline for [`se-regional-pwa`](https://github.com/larsgson/bw-se-regional-pwa). Pulls Scripture Earth (`scriptureearth.org`) Proskomma `.pkf` archives, catalogs, fonts, audio timing data, and per-language video/audio manifests, then publishes the result as GitHub Releases.

Supports per-country, per-region, and global releases. Everything runs on your local machine — the pipeline, the packaging, and the `gh release create` call. The consuming PWA pulls the latest release as a build-time fixture; nothing in this repo is served at runtime.

Code is MIT-licensed. Released data is upstream-licensed (CC BY-NC-ND 4.0 per included language); see [`LICENSING.md`](LICENSING.md).

## Requirements

- **Python 3.11+** — drives the whole pipeline. Stdlib only; no `pip install` needed.
- **Node 20+** with `npm install` — only required by `scripts/map_figures.mjs`, which uses `proskomma-core` to parse `.pkf` succinct docsets.
- **Homebrew / apt tools** — `tar`, `zstd`, `gh` (GitHub CLI), `jq` (for ad-hoc inspection, optional).

## Quick start

```bash
npm install                        # one-time; installs proskomma-core

# Single country
COUNTRY=mx make pipeline           # fetch + dedupe + map-figures + map-media
COUNTRY=mx make release            # classify + pack + publish

# Entire region
make region-pipeline REGION=conf/central-america.conf
make region-release  REGION=conf/central-america.conf

# Combined global release (after per-country pipelines)
make merge-global CONF=conf/global.conf MERGED=global
COUNTRY=global make release
```

## Data layout

Each country gets its own directory under `data/` (gitignored):

```
data/mx/                           per-country data (one per COUNTRY)
  manifest.json                    top-level summary keyed by ISO
  <iso>/                           per-language data
    info.json                      metadata, audio/video manifests
    *.pkf                          Proskomma docset
    *.json                         catalog
    styles/delta.css               compact stylesheet
    timing/<BOOK>-<chapter>.json   verse-level audio timing
  _fonts/                          deduplicated font pool

data/latam/                        merged directory (symlinks to per-country ISOs)
data/global/                       global merge (symlinks to all ISOs)
data/.media-cache/                 shared across countries (ISO-keyed)
data/.license-scan-cache/          shared across countries (ISO-keyed)
```

## Pipeline stages

```bash
make pipeline                      # runs all four stages below
make fetch                         # download PKF + catalog + CSS + fonts from SE
make dedupe                        # consolidate fonts into _fonts/, emit delta.css
make map-figures                   # parse PKF files for figure URLs (requires Node)
make map-media                     # extract video/audio manifests + timing data from SE JS chunks
```

All stages respect `COUNTRY` (default: `mx`). Individual stages: `COUNTRY=gt make fetch`, etc.

`make classify` probes each ISO's SE deployment, writes `data/<country>/licenses.json`, and rewrites the auto-managed block of [`EXCLUDED_ISOS.txt`](EXCLUDED_ISOS.txt). Uses a per-ISO on-disk cache at `data/.license-scan-cache/`, so re-runs are nearly instant. `make classify-rescan` drops the cache for a full rescan.

## Multi-country workflow

### Region config files

Region definitions live in `conf/`:

```
conf/central-america.conf         MX, GT, BZ, HN, SV, NI, CR, PA
conf/south-america.conf           CO, VE, EC, PE, BO, PY, CL, AR, UY, BR, GY, SR, GF
conf/global.conf                  references the above (expands recursively)
```

Format: one country code per line, `#` comments. Lines ending in `.conf` are expanded recursively.

### Step by step

```bash
# 1. Run the pipeline for every country in a region
make region-pipeline REGION=conf/central-america.conf
make region-pipeline REGION=conf/south-america.conf

# 2a. Release per country (one GitHub release each, tagged data-<cc>-YYYY.MM.DD)
make region-release REGION=conf/central-america.conf

# 2b. Merge into a combined directory and release
make merge-region REGION=conf/central-america.conf MERGED=ca
COUNTRY=ca make release

# 3. Global merge across all regions
make merge-global CONF=conf/global.conf MERGED=global
COUNTRY=global make release
```

The merge step creates `data/<merged>/` with symlinks to the per-country ISO directories. ISOs appearing in multiple countries are deduplicated (first country wins; data is identical). The shared caches (`.media-cache/`, `.license-scan-cache/`) are ISO-keyed, so languages fetched for one country don't need re-scanning when they appear in another.

## Cutting a release

Requires `gh auth login` against the repo.

```bash
make release                       # full publish, country=mx, tag=data-YYYY.MM.DD
make release-dry                   # pack + diff, print what would upload; no network
make release-draft                 # publish as hidden draft on GitHub
COUNTRY=gt make release            # release a specific country
COUNTRY=latam make release         # release a merged region
make pack                          # just the tarball + index.json
make notes                         # just release/release-notes.md
make clean                         # rm -rf release/
make help                          # list targets + current var values
```

Override tag: `TAG=data-mx-2026.06.01 COUNTRY=mx make release`. Override compression: `ZSTD_LEVEL=15 make pack`.

## Release format

Each release has three assets plus metadata:

| Asset                                | Purpose                                                     |
|--------------------------------------|-------------------------------------------------------------|
| `pkf-<country>-<YYYYMMDD>.tar.zst`  | CC-licensed data tree, zstd-compressed                      |
| `manifest-<country>-<YYYYMMDD>.json` | Manifest copy for cheap diffs                               |
| `licenses-<country>-<YYYYMMDD>.json` | Per-ISO classification (`included` / `excluded` + reason)   |
| `index.json`                         | `{ version, created_at, bytes, sha256, tag, … }`           |

The consuming PWA's build step downloads `index.json`, verifies `sha256`, then pulls the tarball.

## Disk caches

Both caches are shared across countries (ISO-keyed, not country-keyed):

- **`data/.media-cache/`** — cached service-worker + manifest chunk per ISO. Warm-cache `map_media` runs ~70x faster. Auto-invalidates on SE redeploys.
- **`data/.license-scan-cache/`** — cached JS corpus per ISO. Warm-cache `classify` runs in ~2s for 139 ISOs. Auto-invalidates on SE redeploys.

To force a full re-fetch: `rm -rf data/.media-cache/ data/.license-scan-cache/`.

## Licensing

- **Code:** MIT — see [`LICENSE`](LICENSE).
- **Data in releases:** every included language is CC BY-NC-ND 4.0 International. Per-language attribution lives in `info.json` inside the release tarball.
- **Excluded ISOs:** [`EXCLUDED_ISOS.txt`](EXCLUDED_ISOS.txt) lists languages dropped from releases. The auto-managed block is rewritten by `make classify`.
- **Excluded companion packages:** [`EXCLUDED_PACKAGES.txt`](EXCLUDED_PACKAGES.txt) lists package basenames stripped from ISOs that ship them (e.g. Biblica NVI Spanish companions).

Full background and downstream attribution requirements: [`LICENSING.md`](LICENSING.md).

## What lives where

Python (stdlib only):

- `scripts/fetch_pkf.py` — discovers and downloads PKF + catalog + CSS + fonts.
- `scripts/dedupe_assets.py` — consolidates fonts into `_fonts/`, emits per-ISO `delta.css`.
- `scripts/map_media.py` — extracts video/audio manifests + verse-level timing data from SE JS chunks.
- `scripts/classify_licenses.py` — probes SE per ISO, emits `licenses.json`, rewrites `EXCLUDED_ISOS.txt`.
- `scripts/pack_release.py` — builds the release tarball + `index.json`.
- `scripts/diff_manifest.py` — writes `release-notes.md` by diffing against the previous release.
- `scripts/merge_countries.py` — merges per-country directories into a combined directory via symlinks.
- `scripts/_lib.py` — shared helpers (`PKF_ROOT`, `load_excluded_isos`, `filter_manifest`).
- `scripts/release.sh` — thin wrapper: classify + pack + diff + `gh release create`.

Node (required for this one only):

- `scripts/map_figures.mjs` — uses `proskomma-core` to parse `.pkf` and populate `figure_urls` in `info.json`.

Config:

- `conf/central-america.conf` — Central America country codes.
- `conf/south-america.conf` — South America country codes.
- `conf/global.conf` — references all region confs (expands recursively).
