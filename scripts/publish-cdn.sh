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

# --- license-filter rules (mirrors pack_release.py) ------------------------
# Use --filter (NOT --include/--exclude): rules are evaluated top-to-bottom,
# first match wins, so excluded ISOs are dropped BEFORE any include can match.
# Mixing --include and --exclude has indeterminate order and could leak a
# non-CC ISO's info.json/timing. These BASE rules go first in every pass.
EXCLUDED_ISOS=()
while read -r iso _; do [ -n "$iso" ] && EXCLUDED_ISOS+=("$iso"); done \
    < <(sed 's/#.*//' EXCLUDED_ISOS.txt | awk 'NF')

BASE=()
BASE+=(--filter "- /manifest.json")     # on-disk manifest is UNFILTERED — skip it
for iso in "${EXCLUDED_ISOS[@]}"; do
    BASE+=(--filter "- /${iso}/**")
done
while read -r base _; do
    [ -n "$base" ] && BASE+=(--filter "- /*/${base}.*")
done < <(sed 's/#.*//' EXCLUDED_PACKAGES.txt | awk 'NF')

COMMON=(--copy-links --transfers 16 --checkers 16 --fast-list ${DRY[@]+"${DRY[@]}"})

echo "[cdn] publishing $SRC -> $DEST (country=$COUNTRY)"
echo "[cdn] manifest: $MANIFEST"
[ "${DRY_RUN:-0}" = "1" ] && echo "[cdn] DRY_RUN=1 — no objects written"

# Pass 0 — self-heal: purge any excluded (non-CC) ISO prefix already on the CDN.
# Catches ISOs that were published before a later classify run excluded them.
# The upload passes never re-create these (BASE filters drop them), so a one-way
# purge keeps the CDN in sync with the current EXCLUDED_ISOS.txt.
purged=0
for iso in "${EXCLUDED_ISOS[@]}"; do
    [ -n "$(rclone lsf "${DEST}/${iso}/" 2>/dev/null)" ] || continue
    if [ "${DRY_RUN:-0}" = "1" ]; then
        echo "[cdn] would purge ${DEST}/${iso}/ (excluded, present on CDN)"
    else
        rclone purge "${DEST}/${iso}" 2>/dev/null && echo "[cdn] purged ${DEST}/${iso}/ (now excluded)"
    fi
    purged=$((purged+1))
done
[ "$purged" -eq 0 ] && echo "[cdn] no stale excluded prefixes on CDN"

# Pass 1 — content-hashed assets (pkf, catalog json, fonts, styles): cache forever.
# Excluded ISOs dropped first; info.json + timing handled in later passes; rest included.
rclone copy "$SRC" "$DEST" "${COMMON[@]}" "${BASE[@]}" \
    --header-upload "Cache-Control: public, max-age=31536000, immutable" \
    --filter "- /*/info.json" \
    --filter "- /*/timing/**"

# Pass 2 — mutable per-iso indexes: short TTL so a new release propagates.
rclone copy "$SRC" "$DEST" "${COMMON[@]}" "${BASE[@]}" \
    --header-upload "Cache-Control: public, max-age=300" \
    --filter "+ /*/info.json" \
    --filter "- **"

# Pass 3 — timing: moderate TTL.
rclone copy "$SRC" "$DEST" "${COMMON[@]}" "${BASE[@]}" \
    --header-upload "Cache-Control: public, max-age=3600" \
    --filter "+ /*/timing/**" \
    --filter "- **"

# Pass 4 — the FILTERED manifest as pkf/manifest.json (short TTL, in place)
rclone copyto "$MANIFEST" "${DEST}/manifest.json" ${DRY[@]+"${DRY[@]}"} \
    --header-upload "Cache-Control: public, max-age=300"

echo "[cdn] done."
[ "${DRY_RUN:-0}" = "1" ] || echo "[cdn] verify: curl -sI ${PUBLIC_BASE}/pkf/manifest.json"
