#!/usr/bin/env bash
# Pack data/pkf/ and publish it as a GitHub Release from the local machine.
# Assumes `gh auth status` is OK and the current repo has a remote.
#
# Usage:
#   scripts/release.sh                # country=mx, tag=data-YYYY.MM.DD
#   COUNTRY=mx scripts/release.sh
#   TAG=data-2026.04.23 scripts/release.sh
#   DRAFT=1 scripts/release.sh        # publish as draft (still uploads to GitHub)
#   DRY_RUN=1 scripts/release.sh      # pack + diff only; skip the gh release create call
#
# Network behavior: classify_licenses.mjs reuses its disk cache
# (data/.license-scan-cache/) so steady-state release runs are fast. To force
# a full rescan, run `make classify-rescan` first.
set -euo pipefail

cd "$(dirname "$0")/.."

COUNTRY="${COUNTRY:-mx}"
YMD="$(date -u +%Y%m%d)"
TAG="${TAG:-data-$(date -u +%Y.%m.%d)}"
export COUNTRY TAG

CC="$(echo "${COUNTRY}" | tr A-Z a-z)"
if [ ! -f "data/${CC}/manifest.json" ]; then
    echo "[release] missing data/${CC}/manifest.json — run the fetch pipeline first" >&2
    exit 1
fi

echo "[release] classifying licenses (disk-cached; rm data/.license-scan-cache to force rescan)"
python3 scripts/classify_licenses.py

echo "[release] reconciling localized UI-string sets (cached; --rescan to force)"
python3 scripts/extract_nav_locales.py

echo "[release] rebuilding shared scripture CSS + per-language delta.css, then auditing the split"
python3 scripts/build_styles.py
python3 scripts/audit_styles.py

echo "[release] regenerating per-language app-config.json (incl. notice_url) + staging nav files"
CONCURRENCY=8 python3 scripts/map_app_config.py

echo "[release] generating deduplicated versification (.vrs) index"
python3 scripts/gen_vrs.py

echo "[release] packing ${COUNTRY} → ${TAG}"
python3 scripts/pack_release.py

echo "[release] diffing against previous release"
python3 scripts/diff_manifest.py --out release/release-notes.md

TAR="release/pkf-${COUNTRY}-${YMD}.tar.zst"
MANIFEST="release/manifest-${COUNTRY}-${YMD}.json"
LICENSES="release/licenses-${COUNTRY}-${YMD}.json"
INDEX="release/index.json"
NOTES="release/release-notes.md"

# The bulk tarball (~700 MB) is a producer-side BACKUP snapshot — no client needs
# it (clients read the CDN, which already gets the incremental per-file delta).
# So by default routine releases upload only the small metadata (a few hundred KB)
# and skip re-uploading the whole tarball. Set RELEASE_TARBALL=1 to attach it
# (e.g. a periodic backup snapshot).
ASSETS=("$MANIFEST" "$LICENSES" "$INDEX")
[ "${RELEASE_TARBALL:-0}" = "1" ] && ASSETS=("$TAR" "${ASSETS[@]}")

req=("$MANIFEST" "$LICENSES" "$INDEX" "$NOTES")
[ "${RELEASE_TARBALL:-0}" = "1" ] && req+=("$TAR")
for f in "${req[@]}"; do
    [ -f "$f" ] || { echo "[release] missing $f" >&2; exit 1; }
done

if [ "${DRY_RUN:-0}" = "1" ]; then
    echo "[release] DRY_RUN=1 — skipping gh release create"
    echo "[release] would upload to tag ${TAG} (RELEASE_TARBALL=${RELEASE_TARBALL:-0}):"
    for f in "${ASSETS[@]}"; do
        printf '         %s (%s bytes)\n' "$f" "$(wc -c < "$f" | tr -d ' ')"
    done
    [ "${RELEASE_TARBALL:-0}" = "1" ] || echo "         (tarball skipped — set RELEASE_TARBALL=1 for a backup snapshot)"
    echo "[release] notes preview:"
    sed 's/^/         /' "$NOTES"
    exit 0
fi

draft_flag=()
[ "${DRAFT:-0}" = "1" ] && draft_flag=(--draft)

echo "[release] publishing ${TAG} (tarball: ${RELEASE_TARBALL:-0})"
if gh release view "$TAG" >/dev/null 2>&1; then
    # Same-day re-release (the tag is date-based): update the existing release's
    # assets and notes in place rather than failing on the duplicate tag.
    echo "[release] ${TAG} exists — updating assets in place (--clobber)"
    gh release upload "$TAG" "${ASSETS[@]}" --clobber
    gh release edit "$TAG" --title "$TAG" --notes-file "$NOTES"
else
    gh release create "$TAG" \
        "${ASSETS[@]}" \
        --title "$TAG" \
        --notes-file "$NOTES" \
        ${draft_flag[@]+"${draft_flag[@]}"}
fi

# Prune old releases: keep only the RELEASE_KEEP most recent (default 2 = latest
# + previous). The "previous" one must survive — diff_manifest.py diffs the next
# release against it for change-notes. Set RELEASE_KEEP=0 to disable pruning.
scripts/prune_releases.sh || true

gh release view "$TAG" --web || true
