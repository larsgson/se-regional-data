# se-regional-data

Private data pipeline for [`se-regional-pwa`](https://github.com/larsgson/bw-se-regional-pwa). Pulls Scripture Earth (`scriptureearth.org`) Proskomma `.pkf` archives, catalogs, fonts, and per-language video/audio manifests for one country at a time, and publishes the result as a single GitHub Release.

Everything runs on your local machine — the pipeline, the packaging, and the `gh release create` call. The consuming PWA pulls the latest release as a build-time fixture; nothing in this repo is served at runtime.

## Pipeline

```bash
npm install
make pipeline              # fetch + dedupe + map-figures + map-media; ~18 min for COUNTRY=mx
```

Individual stages: `make fetch`, `make dedupe`, `make map-figures`, `make map-media`.

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
| `pkf-<country>-<YYYYMMDD>.tar.zst`   | Full `data/pkf/*` tree, zstd-compressed (~50–60 MB)         |
| `manifest-<country>-<YYYYMMDD>.json` | Sibling copy of `data/pkf/manifest.json` for cheap diffs    |
| `index.json`                         | `{ version, created_at, bytes, sha256, tag, asset, … }`     |

The consuming PWA's build step downloads `index.json` (tiny), verifies `sha256`, then pulls `asset` and untars into its own fixtures directory.

## Licensing

Source content on Scripture Earth is licensed **CC BY-NC-ND** per language. **Keep this repo private.** Releases are consumed only by our own builds via a GitHub token — the redistribution path stays inside the same operator.

## What lives where

- `scripts/fetch_pkf.py` — discovers and downloads PKF + catalog + CSS + fonts.
- `scripts/dedupe_assets.py` — consolidates fonts into `_fonts/`, emits per-iso `delta.css`.
- `scripts/map_figures.mjs` — populates `figure_urls` in each `info.json`.
- `scripts/map_media.mjs` — scrapes SE's main JS chunk for video + audio manifests.
- `scripts/pack_release.mjs` — builds the release tarball + `index.json`.
- `scripts/diff_manifest.mjs` — writes `release-notes.md` by diffing against the previous release's manifest asset.
- `scripts/release.sh` — thin wrapper: pack + diff + `gh release create`.
- `scripts/probe_*.mjs`, `scan_media.mjs` — ad-hoc debugging helpers.
