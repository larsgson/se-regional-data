#!/usr/bin/env node
/**
 * For each fetched language, detect in-text figure references and build a map
 * from the filename embedded in the pkf to the hashed URL hosted at
 * scriptureearth.org. Writes the map into data/pkf/<iso>/info.json as
 * `figure_urls`. Figures are rendered at runtime by referencing those URLs
 * directly (no image bytes stored locally).
 *
 * Usage: node scripts/map_figures.mjs
 *
 * Idempotent. Re-fetches each illustrated language's service-worker.js to pick
 * up hash changes. Cleans up `figure_urls` when a pkf is updated to contain no
 * figures.
 */
import { readFileSync, writeFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { decompressSync, strFromU8 } from 'fflate';
import { Proskomma } from 'proskomma-core';

const SE = 'https://scriptureearth.org';
const IMG_EXT_RE = /\.(jpg|jpeg|png|tif|tiff|gif|webp)$/i;
const SW_IMG_RE = /\/_app\/immutable\/assets\/([A-Za-z0-9_\-]+)\.([A-Za-z0-9_\-]+)\.(jpg|jpeg|png|tif|tiff|gif|webp)/gi;

class SAB extends Proskomma {
    constructor() {
        super();
        this.selectors = [
            { name: 'lang', type: 'string', regex: '^[A-Za-z0-9-]{2,30}$' },
            { name: 'abbr', type: 'string', regex: '^[A-Za-z0-9 -]+$' }
        ];
        this.validateSelectors();
    }
}

function thaw(pkfPath) {
    const pk = new SAB();
    pk.loadSuccinctDocSet(
        JSON.parse(strFromU8(decompressSync(new Uint8Array(readFileSync(pkfPath)))))
    );
    return pk;
}

/** Return [{bookCode, filename, caption}]. */
function scanFigures(pkfPath) {
    const pk = thaw(pkfPath);
    const dsid = pk.gqlQuerySync('{docSets{id}}').data.docSets[0].id;
    const docs = pk.gqlQuerySync(
        `{docSet(id:"${dsid}"){documents{bc:header(id:"bookCode")}}}`
    ).data.docSet.documents;

    const out = [];
    for (const { bc } of docs) {
        const figSeqs = pk
            .gqlQuerySync(
                `{docSet(id:"${dsid}"){document(bookCode:"${bc}"){sequences{type id}}}}`
            )
            .data.docSet.document.sequences.filter((s) => s.type === 'fig');
        if (figSeqs.length === 0) continue;

        for (const seq of figSeqs) {
            const blocks = pk.gqlQuerySync(
                `{docSet(id:"${dsid}"){document(bookCode:"${bc}"){sequence(id:"${seq.id}"){blocks{scopeLabels items{type payload}}}}}}`
            ).data.docSet.document.sequence.blocks;

            // Filename lives in a scopeLabel with an image extension.
            const allScopes = blocks.flatMap((b) => b.scopeLabels);
            const filenameScope = allScopes.find((l) => IMG_EXT_RE.test(l));
            const filename = filenameScope ? filenameScope.split('/').pop() : null;

            // Caption is the concatenation of token payloads (minus the NO_CAPTION sentinel).
            let caption = blocks
                .flatMap((b) => b.items)
                .filter((it) => it.type === 'token')
                .map((it) => it.payload)
                .join('')
                .trim();
            if (caption === 'NO_CAPTION') caption = '';

            out.push({ bookCode: bc, filename, caption });
        }
    }
    return out;
}

async function fetchServiceWorker(iso) {
    try {
        const r = await fetch(`${SE}/data/${iso}/sab/${iso}/service-worker.js`, {
            headers: { 'User-Agent': 'bw-map-figures/1.0' }
        });
        if (!r.ok) return null;
        return await r.text();
    } catch {
        return null;
    }
}

/** canonical "basename.ext" (no hash) -> full hashed URL on SE */
function extractImageUrlMap(iso, swText) {
    const map = {};
    for (const m of swText.matchAll(SW_IMG_RE)) {
        const [, base, hash, ext] = m;
        const canonical = `${base}.${ext}`;
        map[canonical] = `${SE}/data/${iso}/sab/${iso}/_app/immutable/assets/${base}.${hash}.${ext}`;
    }
    return map;
}

function resolveUrl(filename, imgMap) {
    if (imgMap[filename]) return imgMap[filename];
    const lower = filename.toLowerCase();
    // case-insensitive match on the full filename
    for (const k of Object.keys(imgMap)) if (k.toLowerCase() === lower) return imgMap[k];
    // same basename, any extension (handles .TIF uploaded as .jpg, etc.)
    const base = filename.replace(/\.[^.]+$/, '').toLowerCase();
    for (const k of Object.keys(imgMap)) {
        if (k.replace(/\.[^.]+$/, '').toLowerCase() === base) return imgMap[k];
    }
    return null;
}

async function main() {
    const root = 'data/pkf';
    const isos = readdirSync(root)
        .filter((n) => !n.startsWith('_') && statSync(join(root, n)).isDirectory())
        .sort();

    const results = [];
    for (const iso of isos) {
        const isoDir = join(root, iso);
        const pkfName = readdirSync(isoDir).find((f) => f.endsWith('.pkf'));
        if (!pkfName) continue;
        const infoPath = join(isoDir, 'info.json');
        const info = JSON.parse(readFileSync(infoPath, 'utf8'));

        const figures = scanFigures(join(isoDir, pkfName));

        if (figures.length === 0) {
            let changed = false;
            if ('figure_urls' in info) {
                delete info.figure_urls;
                changed = true;
            }
            if ('figures' in info) {
                delete info.figures;
                changed = true;
            }
            if (changed) writeFileSync(infoPath, JSON.stringify(info, null, 2));
            continue;
        }

        const sw = await fetchServiceWorker(iso);
        if (!sw) {
            console.error(`${iso}: ${figures.length} figure(s) but couldn't fetch service-worker.js`);
            continue;
        }
        const imgMap = extractImageUrlMap(iso, sw);

        const figure_urls = {};
        const missing = [];
        for (const f of figures) {
            if (!f.filename) continue;
            const url = resolveUrl(f.filename, imgMap);
            if (url) figure_urls[f.filename] = url;
            else missing.push(f.filename);
        }

        info.figures = figures;
        info.figure_urls = figure_urls;
        writeFileSync(infoPath, JSON.stringify(info, null, 2));

        const mapped = Object.keys(figure_urls).length;
        const miss = missing.length ? `, ${missing.length} missing (${missing.join(', ')})` : '';
        console.log(`${iso}: ${figures.length} figure(s), ${mapped} mapped${miss}`);
        results.push({ iso, total: figures.length, mapped, missing });
    }

    if (results.length === 0) console.log('No languages with figures found.');
    else console.log(`\nTotal illustrated languages: ${results.length}`);
}

main().catch((e) => {
    console.error(e);
    process.exit(1);
});
