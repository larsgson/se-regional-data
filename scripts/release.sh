#!/usr/bin/env bash
# Pack data/pkf/ and publish it as a GitHub Release from the local machine.
# Assumes `gh auth status` is OK and the current repo has a remote.
#
# Usage:
#   scripts/release.sh                # country=mx, tag=data-YYYY.MM.DD
#   COUNTRY=mx scripts/release.sh
#   TAG=data-2026.04.23 scripts/release.sh
#   DRAFT=1 scripts/release.sh        # publish as draft
set -euo pipefail

cd "$(dirname "$0")/.."

COUNTRY="${COUNTRY:-mx}"
YMD="$(date -u +%Y%m%d)"
TAG="${TAG:-data-$(date -u +%Y.%m.%d)}"
export COUNTRY TAG

if [ ! -f data/pkf/manifest.json ]; then
    echo "[release] missing data/pkf/manifest.json — run the fetch pipeline first" >&2
    exit 1
fi

echo "[release] packing ${COUNTRY} → ${TAG}"
node scripts/pack_release.mjs

echo "[release] diffing against previous release"
node scripts/diff_manifest.mjs --out release/release-notes.md

TAR="release/pkf-${COUNTRY}-${YMD}.tar.zst"
MANIFEST="release/manifest-${COUNTRY}-${YMD}.json"
INDEX="release/index.json"
NOTES="release/release-notes.md"

for f in "$TAR" "$MANIFEST" "$INDEX" "$NOTES"; do
    [ -f "$f" ] || { echo "[release] missing $f" >&2; exit 1; }
done

draft_flag=()
[ "${DRAFT:-0}" = "1" ] && draft_flag=(--draft)

echo "[release] publishing ${TAG}"
gh release create "$TAG" \
    "$TAR" "$MANIFEST" "$INDEX" \
    --title "$TAG" \
    --notes-file "$NOTES" \
    "${draft_flag[@]}"

gh release view "$TAG" --web || true
