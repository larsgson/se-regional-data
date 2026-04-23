# se-regional-data

Private data pipeline for [`se-regional-pwa`](https://github.com/larsgson/bw-se-regional-pwa). Pulls Scripture Earth (`scriptureearth.org`) Proskomma `.pkf` archives, catalogs, fonts, and per-language video/audio manifests for one country at a time, and publishes the result as a single GitHub Release per build.

The consuming PWA pulls the latest release as a build-time fixture; nothing in this repo is served at runtime.

## Pipeline

Run in order; total wall-clock ~18 min for `--country MX`.

```bash
npm install
python3 scripts/fetch_pkf.py --country MX --workers 8   # ~10 min, stdlib only
python3 scripts/dedupe_assets.py                        # ~5 s
node scripts/map_figures.mjs                            # <10 s
CONCURRENCY=6 node scripts/map_media.mjs                # ~8 min
```

Output lives under `data/pkf/` (gitignored, ~170 MB):

- `data/pkf/manifest.json` — top-level summary keyed by ISO with version + pkf bytes.
- `data/pkf/<iso>/` — per-language `info.json`, `*.pkf`, `*.json` catalog, `styles/delta.css`.
- `data/pkf/_fonts/` — deduplicated font pool shared across all languages.

`unclassified.txt` (from `fetch_pkf.py`) lists ISOs discovered for the country that have no SAB app.

## Local dry-run of a release

```bash
node scripts/pack_release.mjs   # builds release/pkf-mx-YYYYMMDD.tar.zst + index.json + manifest copy
node scripts/diff_manifest.mjs  # writes release/release-notes.md (needs `gh auth login` for prior release lookup)
```

## Release format

Each successful build of `.github/workflows/build-data.yml` publishes a tag `data-YYYY.MM.DD` with three assets:

| Asset                            | Purpose                                                     |
|----------------------------------|-------------------------------------------------------------|
| `pkf-<country>-<YYYYMMDD>.tar.zst` | Full `data/pkf/*` tree, zstd-compressed (~50–60 MB)        |
| `manifest-<country>-<YYYYMMDD>.json` | Sibling copy of `data/pkf/manifest.json` for cheap diffs |
| `index.json`                     | `{ version, created_at, bytes, sha256, tag, asset, … }`     |

The consuming PWA's build step downloads `index.json` (tiny), verifies `sha256`, then pulls `asset` and untars into its own fixtures directory.

## Workflow

`.github/workflows/build-data.yml` runs:

- Weekly: `cron: '0 6 * * 1'` (Mon 06:00 UTC).
- On demand: `workflow_dispatch`.
- 40 min job timeout on `ubuntu-latest`.
- Caches `data/pkf/` keyed on script content + `CACHE_VERSION`. Bump `CACHE_VERSION` in the workflow `env:` to force a full re-fetch.

## Licensing

Source content on Scripture Earth is licensed **CC BY-NC-ND** per language. **Keep this repo private.** Releases are consumed only by our own builds via a GitHub token — the redistribution path stays inside the same operator.

## What lives where

- `scripts/fetch_pkf.py` — discovers and downloads PKF + catalog + CSS + fonts.
- `scripts/dedupe_assets.py` — consolidates fonts into `_fonts/`, emits per-iso `delta.css`.
- `scripts/map_figures.mjs` — populates `figure_urls` in each `info.json`.
- `scripts/map_media.mjs` — scrapes SE's main JS chunk for video + audio manifests.
- `scripts/pack_release.mjs` — builds the release tarball + `index.json`.
- `scripts/diff_manifest.mjs` — writes `release-notes.md` by diffing against the previous release's manifest asset.
- `scripts/probe_*.mjs`, `scan_media.mjs` — ad-hoc debugging helpers; not run by the workflow.
