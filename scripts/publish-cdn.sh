#!/usr/bin/env bash
# Publish the per-country/region .pkf tree to Cloudflare R2 (cdn.bibel.wiki/pkf/).
#
# Publishes the LIVE tree under data/<COUNTRY>/ directly — no tarball extraction.
# data/<COUNTRY>/<iso> are symlinks into data/_pool/, so --copy-links uploads the
# real bytes. License filtering is applied on the fly using the SAME sources as
# pack_release.py (EXCLUDED_ISOS.txt + EXCLUDED_PACKAGES.txt), so non-CC languages
# never reach the CDN. The on-disk manifest.json is unfiltered and is skipped;
# the filtered sibling asset release/manifest-<country>-<YMD>.json is uploaded as
# pkf/manifest.json instead.
#
# Upload plan comes from scripts/cdn_delta.py, which diffs the source tree
# against data/.cdn-published.json (a local record of what was published last
# time) and hands rclone only the new/changed files via --files-from. This
# avoids listing+diffing the ~80k-object remote pkf/ tree on every run — the
# state file makes "nothing changed" runs finish in seconds instead of minutes.
#
# Usage:
#   COUNTRY=global scripts/publish-cdn.sh
#   COUNTRY=global DRY_RUN=1 scripts/publish-cdn.sh   # rclone --dry-run, no writes
#
# Required env (R2 credentials — set as CI secrets, do not commit):
#   R2_ACCESS_KEY_ID  R2_SECRET_ACCESS_KEY  R2_ACCOUNT_ID  R2_BUCKET
set -euo pipefail

cd "$(dirname "$0")/.."

# Auto-load .env (gitignored) so R2 creds don't have to be exported by hand.
if [ -f .env ]; then
    set -a; . ./.env; set +a
fi

COUNTRY="$(echo "${COUNTRY:-mx}" | tr A-Z a-z)"
SRC="data/${COUNTRY}"

# --- preconditions ---------------------------------------------------------
[ -d "$SRC" ] || { echo "[cdn] missing $SRC/ — run the pipeline + merge first" >&2; exit 1; }

MANIFEST="$(ls -t "release/manifest-${COUNTRY}-"*.json 2>/dev/null | head -1 || true)"
[ -n "$MANIFEST" ] || { echo "[cdn] no release/manifest-${COUNTRY}-*.json — run 'make pack' first" >&2; exit 1; }

# Credentials: accept either R2_* or CLOUDFLARE_* naming.
R2_ACCESS_KEY_ID="${R2_ACCESS_KEY_ID:-${CLOUDFLARE_ACCESS_KEY_ID:-}}"
R2_SECRET_ACCESS_KEY="${R2_SECRET_ACCESS_KEY:-${CLOUDFLARE_SECRET_ACCESS_KEY:-}}"
R2_ACCOUNT_ID="${R2_ACCOUNT_ID:-${CLOUDFLARE_ACCOUNT_ID:-}}"
R2_BUCKET="${R2_BUCKET:-${CLOUDFLARE_BUCKET:-}}"
PUBLIC_BASE="${PUBLIC_ASSET_BASE:-https://cdn.bibel.wiki}"

for v in R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_ACCOUNT_ID R2_BUCKET; do
    [ -n "${!v:-}" ] || {
        echo "[cdn] missing $v (or its CLOUDFLARE_* equivalent) in .env" >&2
        exit 1
    }
done
command -v rclone >/dev/null || { echo "[cdn] rclone not installed" >&2; exit 1; }

# --- rclone remote from env (no interactive config) ------------------------
export RCLONE_CONFIG_R2_TYPE=s3
export RCLONE_CONFIG_R2_PROVIDER=Cloudflare
export RCLONE_CONFIG_R2_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID"
export RCLONE_CONFIG_R2_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
export RCLONE_CONFIG_R2_ENDPOINT="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"

DEST="r2:${R2_BUCKET}/pkf"
DRY=(); [ "${DRY_RUN:-0}" = "1" ] && DRY=(--dry-run)

# --- license filtering (mirrors pack_release.py) ---------------------------
# rclone forbids combining --files-from with --filter ("should be used alone
# or with --files-from-raw"), so the exclusion logic can't live in rclone flags
# here. Instead scripts/cdn_delta.py's iter_included_files() applies the SAME
# rules (whole-ISO drop from EXCLUDED_ISOS.txt + package-basename strip from
# EXCLUDED_PACKAGES.txt, on-disk manifest.json skipped) when it builds the
# delta lists below — so a non-CC ISO/package never makes it into a
# --files-from list in the first place.
EXCLUDED_ISOS=()
while read -r iso _; do [ -n "$iso" ] && EXCLUDED_ISOS+=("$iso"); done \
    < <(sed 's/#.*//' EXCLUDED_ISOS.txt | awk 'NF')

COMMON=(--copy-links --transfers 16 --checkers 16 ${DRY[@]+"${DRY[@]}"})

echo "[cdn] publishing $SRC -> $DEST (country=$COUNTRY)"
echo "[cdn] manifest: $MANIFEST"
[ "${DRY_RUN:-0}" = "1" ] && echo "[cdn] DRY_RUN=1 — no objects written"

# --- incremental plan (avoids listing+diffing the whole remote pkf/ tree) --
# data/.cdn-published.json records what was uploaded last time; cdn_delta.py
# diffs the source tree against it (local stat() calls, not a remote list)
# and writes --files-from lists of just the new/changed paths per cache tier.
STATE_FILE="data/.cdn-published.json"
DELTA_DIR="$(mktemp -d)"
trap 'rm -rf "$DELTA_DIR"' EXIT

# Pass 0 — self-heal: purge any excluded (non-CC) ISO prefix already on the CDN.
# Catches ISOs that were published before a later classify run excluded them.
# The upload passes never re-create these (cdn_delta.py's iter_included_files
# drops them too), so a one-way purge keeps the CDN in sync with EXCLUDED_ISOS.txt.
# Also forget its local state so a later un-exclusion is treated as new.
purged=0
for iso in "${EXCLUDED_ISOS[@]}"; do
    [ -n "$(rclone lsf "${DEST}/${iso}/" 2>/dev/null)" ] || continue
    if [ "${DRY_RUN:-0}" = "1" ]; then
        echo "[cdn] would purge ${DEST}/${iso}/ (excluded, present on CDN)"
    else
        rclone purge "${DEST}/${iso}" 2>/dev/null && echo "[cdn] purged ${DEST}/${iso}/ (now excluded)"
        python3 scripts/cdn_delta.py forget --state "$STATE_FILE" --iso "$iso"
    fi
    purged=$((purged+1))
done
[ "$purged" -eq 0 ] && echo "[cdn] no stale excluded prefixes on CDN"

python3 scripts/cdn_delta.py plan --src "$SRC" --state "$STATE_FILE" --out-dir "$DELTA_DIR"

# Pass 1 — content-hashed assets (pkf, catalog json, fonts, styles): cache forever.
if [ -s "$DELTA_DIR/assets.txt" ]; then
    rclone copy "$SRC" "$DEST" "${COMMON[@]}" \
        --files-from "$DELTA_DIR/assets.txt" --no-traverse \
        --header-upload "Cache-Control: public, max-age=31536000, immutable"
else
    echo "[cdn] assets: nothing new"
fi

# Pass 2 — mutable per-iso indexes: short TTL so a new release propagates.
if [ -s "$DELTA_DIR/info.txt" ]; then
    rclone copy "$SRC" "$DEST" "${COMMON[@]}" \
        --files-from "$DELTA_DIR/info.txt" --no-traverse \
        --header-upload "Cache-Control: public, max-age=300"
else
    echo "[cdn] info.json: nothing new"
fi

# Pass 3 — timing: moderate TTL. This tier has by far the most files (tens of
# thousands of small per-chapter JSONs) — the one the old remote-diff approach
# was slowest on. The delta plan means this is now just the ISOs that changed.
if [ -s "$DELTA_DIR/timing.txt" ]; then
    rclone copy "$SRC" "$DEST" "${COMMON[@]}" \
        --files-from "$DELTA_DIR/timing.txt" --no-traverse \
        --header-upload "Cache-Control: public, max-age=3600"
else
    echo "[cdn] timing: nothing new"
fi

# Pass 4 — OUR versification specs (data/_vrs/, content-hashed customs only). The
# named standard scheme files (eng.vrs …) are owned/published by the DBT service,
# so gen_vrs.py does not write them and we do not upload them here. These small
# scheme-only .vrs files are each referenced by a collection's manifest `vrs`.
# Not per-iso and not in the SRC tree, so it rides its own pass; --checksum skips
# unchanged files. Moderate TTL: hashed filenames are stable per structure.
if [ -d data/_vrs ]; then
    rclone copy data/_vrs "${DEST}/_vrs" "${COMMON[@]}" --checksum \
        --include "*.vrs" \
        --header-upload "Cache-Control: public, max-age=3600"
else
    echo "[cdn] _vrs: data/_vrs missing (run scripts/gen_vrs.py) — skipping" >&2
fi

# Pass 5 — the FILTERED manifest as pkf/manifest.json (short TTL, in place)
rclone copyto "$MANIFEST" "${DEST}/manifest.json" ${DRY[@]+"${DRY[@]}"} \
    --header-upload "Cache-Control: public, max-age=300"

# Commit the new state only after every pass above has succeeded (set -e would
# have aborted the script earlier on any rclone failure). Skipped on dry runs.
if [ "${DRY_RUN:-0}" != "1" ]; then
    python3 scripts/cdn_delta.py commit --src "$SRC" --state "$STATE_FILE"
fi

echo "[cdn] done."
[ "${DRY_RUN:-0}" = "1" ] || echo "[cdn] verify: curl -sI ${PUBLIC_BASE}/pkf/manifest.json"
