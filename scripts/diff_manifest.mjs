#!/usr/bin/env node
/**
 * Diff this run's data/pkf/manifest.json against the previous release's
 * manifest asset and write release-notes.md.
 *
 * Resolves "previous release" via `gh release list`. The previous release's
 * manifest.json is downloaded as a sibling asset (uploaded by pack_release).
 * If no previous release exists (first run, or asset missing), the notes
 * just report totals.
 *
 * Env:
 *   COUNTRY=mx           filter prior releases by tag/asset prefix
 *   GH_REPO=owner/name   override repo (otherwise gh autodetects)
 *
 * Usage:
 *   node scripts/diff_manifest.mjs                # writes ./release/release-notes.md
 *   node scripts/diff_manifest.mjs --out path     # custom output path
 */
import { execFileSync } from 'node:child_process';
import {
    readFileSync,
    writeFileSync,
    existsSync,
    mkdirSync,
    mkdtempSync,
    rmSync
} from 'node:fs';
import { join, dirname } from 'node:path';
import { tmpdir } from 'node:os';
import { loadExcludedIsos, filterManifest } from './lib/excluded.mjs';

const COUNTRY = (process.env.COUNTRY || 'mx').toLowerCase();
const CURRENT_MANIFEST = 'data/pkf/manifest.json';

function parseArgs() {
    const args = process.argv.slice(2);
    let out = 'release/release-notes.md';
    for (let i = 0; i < args.length; i++) {
        if (args[i] === '--out') out = args[++i];
    }
    return { out };
}

function gh(args) {
    try {
        return execFileSync('gh', args, { stdio: ['ignore', 'pipe', 'pipe'] }).toString();
    } catch (e) {
        const stderr = e.stderr ? e.stderr.toString() : '';
        throw new Error(`gh ${args.join(' ')} failed: ${stderr.trim() || e.message}`);
    }
}

function findPreviousManifest() {
    let listing;
    try {
        listing = gh(['release', 'list', '--limit', '20', '--json', 'tagName,createdAt,isDraft']);
    } catch (e) {
        console.error(`[diff] couldn't list releases: ${e.message}`);
        return null;
    }
    const releases = JSON.parse(listing).filter((r) => !r.isDraft);
    // Newest first; the workflow has not created its own release yet at this point.
    for (const r of releases) {
        let assets;
        try {
            assets = JSON.parse(
                gh(['release', 'view', r.tagName, '--json', 'assets'])
            ).assets;
        } catch {
            continue;
        }
        const manifest = assets.find(
            (a) => a.name.startsWith(`manifest-${COUNTRY}-`) && a.name.endsWith('.json')
        );
        if (!manifest) continue;
        const tmp = mkdtempSync(join(tmpdir(), 'sermd-'));
        try {
            gh(['release', 'download', r.tagName, '-p', manifest.name, '-D', tmp]);
            const text = readFileSync(join(tmp, manifest.name), 'utf8');
            return { tag: r.tagName, manifest: JSON.parse(text) };
        } catch (e) {
            console.error(`[diff] couldn't download ${manifest.name} from ${r.tagName}: ${e.message}`);
        } finally {
            rmSync(tmp, { recursive: true, force: true });
        }
    }
    return null;
}

function indexByIso(manifest) {
    const out = {};
    for (const l of manifest.languages || []) out[l.iso] = l;
    return out;
}

function fmtBytes(n) {
    if (!n) return '0 B';
    if (n < 1024) return `${n} B`;
    if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
    return `${(n / (1024 * 1024)).toFixed(1)} MiB`;
}

function totals(manifest) {
    const langs = manifest.languages || [];
    return {
        count: langs.length,
        bytes: langs.reduce((a, l) => a + (l.pkf_bytes || 0), 0)
    };
}

function buildNotes({ current, previous, previousTag }) {
    const lines = [];
    const tCur = totals(current);
    lines.push(`# Scripture Earth data — ${COUNTRY.toUpperCase()}`);
    lines.push('');
    lines.push(`Updated **${current.updated_at || new Date().toISOString()}**`);
    lines.push('');
    lines.push(`- **Languages:** ${tCur.count}`);
    lines.push(`- **Total .pkf bytes:** ${fmtBytes(tCur.bytes)}`);
    lines.push('');

    if (!previous) {
        lines.push('_First release — no previous manifest to diff against._');
        return lines.join('\n') + '\n';
    }

    const prev = indexByIso(previous);
    const cur = indexByIso(current);
    const allIsos = new Set([...Object.keys(prev), ...Object.keys(cur)]);

    const added = [];
    const removed = [];
    const bumped = [];
    for (const iso of [...allIsos].sort()) {
        const a = prev[iso];
        const b = cur[iso];
        if (!a && b) added.push(b);
        else if (a && !b) removed.push(a);
        else if (a && b && a.version !== b.version) bumped.push({ iso, from: a.version, to: b.version });
    }

    lines.push(`## Changes since \`${previousTag}\``);
    lines.push('');
    lines.push(`- Added: **${added.length}**`);
    lines.push(`- Removed: **${removed.length}**`);
    lines.push(`- Version-bumped: **${bumped.length}**`);
    lines.push('');

    if (added.length) {
        lines.push('### Added languages');
        for (const l of added) lines.push(`- \`${l.iso}\` v${l.version || '?'}`);
        lines.push('');
    }
    if (removed.length) {
        lines.push('### Removed languages');
        for (const l of removed) lines.push(`- \`${l.iso}\``);
        lines.push('');
    }
    if (bumped.length) {
        lines.push('### Version bumps');
        for (const b of bumped) lines.push(`- \`${b.iso}\`: ${b.from || '?'} → ${b.to || '?'}`);
        lines.push('');
    }

    return lines.join('\n') + '\n';
}

function main() {
    const { out } = parseArgs();
    if (!existsSync(CURRENT_MANIFEST)) {
        console.error(`[diff] missing ${CURRENT_MANIFEST}; run fetch_pkf.py first`);
        process.exit(1);
    }
    const excluded = loadExcludedIsos();
    const current = filterManifest(
        JSON.parse(readFileSync(CURRENT_MANIFEST, 'utf8')),
        excluded
    );
    const prevRaw = findPreviousManifest();
    const previous = prevRaw ? filterManifest(prevRaw.manifest, excluded) : null;
    const notes = buildNotes({
        current,
        previous,
        previousTag: prevRaw?.tag ?? null
    });
    mkdirSync(dirname(out), { recursive: true });
    writeFileSync(out, notes);
    console.log(`[diff] wrote ${out}` + (excluded.size ? ` (excluded ${excluded.size} ISO[s])` : ''));
}

main();
