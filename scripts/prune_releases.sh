#!/usr/bin/env bash
# Prune GitHub releases, keeping a bounded set — deletes older ones and their tags.
#
# Two independent caps, because releases come in two flavours:
#   * routine releases carry only small metadata (manifest/licenses/index);
#   * backup releases (RELEASE_TARBALL=1) also carry the ~700 MB .tar.zst.
#
# Keeps the union of:
#   RELEASE_KEEP (default 2)          most-recent releases overall — latest +
#                                     previous; the previous MUST survive because
#                                     diff_manifest.py diffs the next release
#                                     against it for change notes.
#   RELEASE_KEEP_TARBALL (default 2)  most-recent releases that have a .tar.zst
#                                     asset — your backup snapshots, so they are
#                                     NOT pruned away by later metadata-only runs.
# Everything else is deleted.
#
# Called at the end of scripts/release.sh; also runnable directly:
#   scripts/prune_releases.sh
#   RELEASE_KEEP=2 RELEASE_KEEP_TARBALL=3 scripts/prune_releases.sh
#   DRY_RUN=1 scripts/prune_releases.sh        # list what would be deleted
#   RELEASE_KEEP=0 RELEASE_KEEP_TARBALL=0 scripts/prune_releases.sh   # disable
set -euo pipefail

cd "$(dirname "$0")/.."

KEEP="${RELEASE_KEEP:-2}"
KEEP_TAR="${RELEASE_KEEP_TARBALL:-2}"
command -v gh >/dev/null || { echo "[prune] gh not installed; skipping" >&2; exit 0; }

if [ "$KEEP" -le 0 ] && [ "$KEEP_TAR" -le 0 ] 2>/dev/null; then
    echo "[prune] RELEASE_KEEP=$KEEP RELEASE_KEEP_TARBALL=$KEEP_TAR — pruning disabled"
    exit 0
fi

# All tags, newest-first (bash 3.2: no mapfile — read into an array).
tags=()
while IFS= read -r t; do
    [ -n "$t" ] && tags+=("$t")
done < <(gh release list --limit 200 --json tagName,createdAt \
             -q "sort_by(.createdAt) | reverse | .[].tagName")

[ "${#tags[@]}" -eq 0 ] && { echo "[prune] no releases"; exit 0; }

keep=" "          # space-delimited set of tags to keep
# 1) the KEEP most-recent overall
i=0
for t in "${tags[@]}"; do
    [ "$i" -ge "$KEEP" ] && break
    keep="${keep}${t} "
    i=$((i + 1))
done
# 2) the KEEP_TAR most-recent releases that carry a .tar.zst asset
tcount=0
for t in "${tags[@]}"; do
    [ "$tcount" -ge "$KEEP_TAR" ] && break
    if gh release view "$t" --json assets -q '.assets[].name' 2>/dev/null \
            | grep -q '\.tar\.zst$'; then
        case "$keep" in *" $t "*) ;; *) keep="${keep}${t} ";; esac
        tcount=$((tcount + 1))
    fi
done

echo "[prune] keeping (overall<=$KEEP, tarball<=$KEEP_TAR):${keep}"
deleted=0
for t in "${tags[@]}"; do
    case "$keep" in *" $t "*) continue ;; esac
    if [ "${DRY_RUN:-0}" = "1" ]; then
        echo "  would delete $t"
    else
        echo "  deleting $t"
        gh release delete "$t" --cleanup-tag --yes
    fi
    deleted=$((deleted + 1))
done
if [ "$deleted" -eq 0 ]; then
    echo "[prune] nothing to delete"
fi
