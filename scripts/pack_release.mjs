#!/usr/bin/env node
/**
 * Package data/pkf/ into a release artifact:
 *   - <country>-YYYYMMDD.tar.zst of data/pkf/*
 *   - index.json: { version, created_at, bytes, sha256, tag, asset, manifest_asset, country }
 *   - manifest.json (copied as a sibling release asset for cheap diffing)
 *
 * Usage:
 *   node scripts/pack_release.mjs               # defaults: country=mx
 *   COUNTRY=mx node scripts/pack_release.mjs
 *   TAG=data-2026.04.22 COUNTRY=mx node scripts/pack_release.mjs
 *
 * Writes into ./release/ (gitignored). Safe to re-run; overwrites the staging
 * directory each time.
 */
import { createHash } from 'node:crypto';
import { execSync } from 'node:child_process';
import {
    mkdirSync,
    readFileSync,
    writeFileSync,
    statSync,
    rmSync,
    existsSync,
    copyFileSync
} from 'node:fs';
import { join } from 'node:path';
import { loadExcludedIsos, filterManifest } from './lib/excluded.mjs';

const COUNTRY = (process.env.COUNTRY || 'mx').toLowerCase();
const PKF_ROOT = 'data/pkf';
const STAGE = 'release';
const CREATED_AT = new Date().toISOString();
const YMD = CREATED_AT.slice(0, 10).replace(/-/g, '');
const TAG = process.env.TAG || `data-${CREATED_AT.slice(0, 10).replace(/-/g, '.')}`;

const ASSET = `pkf-${COUNTRY}-${YMD}.tar.zst`;
const MANIFEST_ASSET = `manifest-${COUNTRY}-${YMD}.json`;
const LICENSES_ASSET = `licenses-${COUNTRY}-${YMD}.json`;

function ensurePkfRoot() {
    try {
        if (!statSync(PKF_ROOT).isDirectory()) throw new Error();
    } catch {
        console.error(`[pack] missing ${PKF_ROOT}; run the fetch pipeline first`);
        process.exit(1);
    }
    const manifestPath = join(PKF_ROOT, 'manifest.json');
    try {
        statSync(manifestPath);
    } catch {
        console.error(`[pack] missing ${manifestPath}; fetch_pkf.py didn't complete`);
        process.exit(1);
    }
    return manifestPath;
}

function sha256OfFile(path) {
    const h = createHash('sha256');
    h.update(readFileSync(path));
    return h.digest('hex');
}

function summarizeManifest(manifest) {
    const langs = (manifest.languages || []).length;
    const bytes = (manifest.languages || []).reduce((a, l) => a + (l.pkf_bytes || 0), 0);
    return { languages: langs, pkf_bytes_total: bytes, updated_at: manifest.updated_at };
}

function main() {
    const manifestPath = ensurePkfRoot();
    const excluded = loadExcludedIsos();

    rmSync(STAGE, { recursive: true, force: true });
    mkdirSync(STAGE, { recursive: true });

    const tarPath = join(STAGE, ASSET);
    const zstdLevel = parseInt(process.env.ZSTD_LEVEL || '19', 10);
    const excludeFlags = [...excluded].map((iso) => `--exclude=./${iso}`).join(' ');
    if (excluded.size) {
        console.log(`[pack] excluding ${excluded.size} ISO(s) per EXCLUDED_ISOS.txt: ${[...excluded].join(', ')}`);
    }
    console.log(`[pack] building ${tarPath} from ${PKF_ROOT}/ (zstd -${zstdLevel}) ...`);
    // bsdtar on macOS and GNU tar on Linux disagree on how -I word-splits its
    // argument, so pipe through zstd explicitly via the shell instead.
    execSync(
        `tar ${excludeFlags} -cf - -C ${PKF_ROOT} . | zstd -${zstdLevel} -T0 -q -o ${tarPath}`,
        { stdio: 'inherit', shell: '/bin/bash' }
    );

    // Verify no excluded iso slipped into the tar (defensive — bsdtar/GNU tar
    // can disagree on glob anchoring).
    if (excluded.size) {
        const listing = execSync(
            `zstd -dc ${tarPath} | tar -tf - | head -2000`,
            { shell: '/bin/bash' }
        ).toString();
        const leaks = [...excluded].filter((iso) =>
            listing.split('\n').some((p) => p === `./${iso}/` || p.startsWith(`./${iso}/`))
        );
        if (leaks.length) {
            console.error(`[pack] FATAL: excluded ISO(s) found in tar: ${leaks.join(', ')}`);
            process.exit(1);
        }
    }

    // Stage a filtered manifest.json (no excluded ISOs).
    const fullManifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
    const filtered = filterManifest(fullManifest, excluded);
    const manifestStaged = join(STAGE, MANIFEST_ASSET);
    writeFileSync(manifestStaged, JSON.stringify(filtered, null, 2));

    // Stage licenses.json — emitted by classify_licenses.mjs (audit record of
    // what's in / out and why). Required as a release asset.
    const licensesPath = join(PKF_ROOT, 'licenses.json');
    if (!existsSync(licensesPath)) {
        console.error(`[pack] missing ${licensesPath}; run classify_licenses.mjs (or make classify) first`);
        process.exit(1);
    }
    const licensesStaged = join(STAGE, LICENSES_ASSET);
    copyFileSync(licensesPath, licensesStaged);
    const licensesDoc = JSON.parse(readFileSync(licensesPath, 'utf8'));

    const bytes = statSync(tarPath).size;
    const sha256 = sha256OfFile(tarPath);
    const manifestSummary = summarizeManifest(filtered);

    const index = {
        version: TAG,
        tag: TAG,
        country: COUNTRY,
        created_at: CREATED_AT,
        asset: ASSET,
        manifest_asset: MANIFEST_ASSET,
        licenses_asset: LICENSES_ASSET,
        bytes,
        sha256,
        summary: manifestSummary,
        excluded_isos: [...excluded],
        licenses_summary: {
            included: licensesDoc.included_count,
            excluded: licensesDoc.excluded_count,
            default_license: licensesDoc.default_license
        }
    };
    writeFileSync(join(STAGE, 'index.json'), JSON.stringify(index, null, 2));

    const mb = (bytes / (1024 * 1024)).toFixed(1);
    console.log(`[pack] ${ASSET}  ${mb} MB  sha256=${sha256.slice(0, 16)}…`);
    console.log(`[pack] tag=${TAG}  languages=${manifestSummary.languages} (excluded ${excluded.size}, classifier-excluded ${licensesDoc.excluded_count})`);
    console.log(`[pack] staged in ./${STAGE}/`);
}

main();
