#!/usr/bin/env node
/**
 * Pipeline step: probe every ISO's Scripture Earth SAB deployment and
 * classify the scripture-text license. Emits data/pkf/licenses.json.
 *
 * Signals used (in order of trust):
 *   1. "Texto:" block explicitly says "Usado con permiso" / "Todos los derechos
 *      reservados" / "Texto en proceso de finalizar" / bundles Biblica NVI
 *      → NOT CC, exclude from public release.
 *   2. "Creative Commons Atribución-NoComercial-SinDerivadas" or "BY-NC-ND"
 *      appears in the concatenated JS bundle → CC BY-NC-ND 4.0.
 *   3. `by-nc-nd.<hash>.png` badge cached by the app's service worker
 *      → CC BY-NC-ND 4.0 (image-declared).
 *   4. Nothing matches → UNCLEAR, excluded from release with a flag.
 *
 * Usage:
 *   node scripts/classify_licenses.mjs                 # write licenses.json
 *   node scripts/classify_licenses.mjs --prune         # also rm excluded iso dirs
 *
 * Requires data/pkf/manifest.json to exist (produced by fetch_pkf.py). Run AFTER
 * fetch_pkf.py + dedupe_assets.py but BEFORE packing the release tarball.
 */
import { readFileSync, writeFileSync, rmSync, existsSync } from 'node:fs';
import { join } from 'node:path';

const ROOT = process.cwd();
const PKF_DIR = join(ROOT, 'data', 'pkf');
const MANIFEST = join(PKF_DIR, 'manifest.json');
const OUT = join(PKF_DIR, 'licenses.json');
const EXCLUDED_TXT = join(ROOT, 'EXCLUDED_ISOS.txt');
const PRUNE = process.argv.includes('--prune');
const FORCE = process.argv.includes('--force');
const CONCURRENCY = Number(process.env.CONCURRENCY || 8);

if (!existsSync(MANIFEST)) {
    console.error(`Missing ${MANIFEST}. Run fetch_pkf.py first.`);
    process.exit(1);
}

const manifest = JSON.parse(readFileSync(MANIFEST, 'utf8'));
const isos = manifest.languages.map((l) => l.iso);

const UA = 'se-regional-data/classify_licenses';
async function fetchText(url) {
    try {
        const r = await fetch(url, { headers: { 'User-Agent': UA } });
        if (!r.ok) return '';
        return await r.text();
    } catch {
        return '';
    }
}

const BADGE = /by-nc-nd\.[A-Za-z0-9_-]+\.(?:png|svg)/i;
const CC_TEXT =
    /Creative Commons[^<]{0,100}(?:Atribución-NoComercial-SinDerivadas|BY-NC-ND|by-nc-nd)|\(by-nc-nd\)|BY-NC-ND\)/i;
const JS_URL = /\/_app\/immutable\/(?:chunks|entry|nodes)\/[A-Za-z0-9._-]+\.js/g;
const STRING_LIT = /"([^"\\]{3,4000})"/g;

/** Pull the contents of the Texto: block up to the next <b>…</b> sibling. */
function extractTexto(joined) {
    const m =
        joined.match(
            /Texto[:\s]*<\/b>([\s\S]{0,2000}?)(?=<\/div>|<b>\s*Audio|<b>\s*Im[áa]genes|<b>\s*Ilustrac|<b>\s*Images|<div)/i
        ) ||
        joined.match(
            />\s*Texto[:\s]*([\s\S]{0,2000}?)(?=<\/div>|Audio:|Im[áa]genes:|Ilustraciones|Images:)/i
        );
    return m ? m[1].slice(0, 1000).trim() : '';
}

/** Short copyright holder extraction from the Texto: block. */
function extractHolder(texto) {
    // e.g. "© 2016, Wycliffe Bible Translators, Inc." — take up to first </div> / </a> sibling
    const m = texto.match(/©\s*([0-9,\s]+[^<]{2,160})/);
    return m ? m[1].replace(/\s+/g, ' ').trim() : '';
}

function classifyTexto(texto) {
    const t = texto.toLowerCase();
    if (/usado con permiso|used with permission/.test(t))
        return { ok: false, reason: 'Texto: "Usado con permiso" — permission-only, not CC' };
    if (/todos los derechos reservados|all rights reserved/.test(t))
        return {
            ok: false,
            reason: 'Texto: "Todos los derechos reservados" — ARR declaration'
        };
    if (/nueva versi[óo]n internacional|nvi®|biblica/i.test(texto))
        return {
            ok: false,
            reason: 'Texto: bundles Biblica NVI translation — proprietary, not CC'
        };
    if (/proceso de finalizar|in process|provisional/i.test(texto))
        return { ok: false, reason: 'Texto: provisional / not-final translation' };
    return { ok: true };
}

async function probeIso(iso) {
    const base = `https://scriptureearth.org/data/${iso}/sab/${iso}`;
    const sw = await fetchText(`${base}/service-worker.js`);
    if (!sw) return { iso, unreachable: true };

    const badge = BADGE.test(sw);
    const jsPaths = [...new Set([...sw.matchAll(JS_URL)].map((m) => m[0]))];
    let joined = '';
    for (const p of jsPaths) {
        const body = await fetchText(`https://scriptureearth.org${p}`);
        if (body) {
            for (const m of body.matchAll(STRING_LIT)) joined += m[1] + '\n';
        }
    }
    const texto = extractTexto(joined);
    const holder = extractHolder(texto);
    const cc_text = CC_TEXT.test(joined);
    const decision = classifyTexto(texto);

    if (!decision.ok) {
        return {
            iso,
            include: false,
            license: 'not-cc',
            reason: decision.reason,
            texto,
            text_holder: holder
        };
    }
    if (cc_text || badge) {
        return {
            iso,
            include: true,
            license: 'CC-BY-NC-ND-4.0',
            evidence: { badge_in_sw: badge, cc_text_in_js: cc_text },
            texto,
            text_holder: holder
        };
    }
    return {
        iso,
        include: false,
        license: 'unclear',
        reason: 'No CC badge or inline CC declaration found',
        texto,
        text_holder: holder
    };
}

async function runPool(items, worker, size) {
    const results = new Array(items.length);
    let i = 0;
    await Promise.all(
        Array.from({ length: size }, async () => {
            while (i < items.length) {
                const idx = i++;
                try {
                    results[idx] = await worker(items[idx]);
                } catch (e) {
                    results[idx] = { iso: items[idx], error: String(e) };
                }
            }
        })
    );
    return results;
}

// Cache: skip ISOs already present in the prior licenses.json (in either
// included or excluded). Use --force to re-probe everything.
let prior = null;
if (existsSync(OUT)) {
    try {
        prior = JSON.parse(readFileSync(OUT, 'utf8'));
    } catch {}
}
const priorIsos = new Set([
    ...Object.keys(prior?.included || {}),
    ...Object.keys(prior?.excluded || {})
]);

const toProbe = FORCE ? isos : isos.filter((iso) => !priorIsos.has(iso));
const cached = FORCE ? [] : isos.filter((iso) => priorIsos.has(iso));

console.log(
    `Classifying ${isos.length} ISOs ` +
        `(cached=${cached.length}, probe=${toProbe.length}, ${CONCURRENCY} concurrent` +
        `${FORCE ? ', --force' : ''})...`
);

const probedResults = await runPool(toProbe, probeIso, CONCURRENCY);
const cachedResults = cached.map((iso) => {
    if (prior?.included?.[iso]) {
        return { iso, include: true, ...prior.included[iso], cached: true };
    }
    return { iso, include: false, ...prior.excluded[iso], cached: true };
});
const results = [...probedResults, ...cachedResults];

const included = {};
const excluded = {};
for (const r of results) {
    if (r.unreachable || r.error) {
        excluded[r.iso] = { reason: r.error || 'SE unreachable', license: 'unknown' };
        continue;
    }
    if (r.include) {
        included[r.iso] = {
            license: r.license,
            text_holder: r.text_holder || null,
            evidence: r.evidence
        };
    } else {
        excluded[r.iso] = {
            license: r.license,
            reason: r.reason,
            text_holder: r.text_holder || null,
            texto: r.texto
        };
    }
}

const out = {
    schema_version: 1,
    updated_at: new Date().toISOString().slice(0, 10),
    source: 'Scripture Earth SAB app service-worker + JS chunk scan',
    notes: [
        'License applies to the scripture TEXT only (the .pkf data).',
        'Images, audio, and video referenced by SE URL carry their own per-asset licenses and are NOT covered by this classification.'
    ],
    default_license: 'CC-BY-NC-ND-4.0',
    included_count: Object.keys(included).length,
    excluded_count: Object.keys(excluded).length,
    included,
    excluded
};
writeFileSync(OUT, JSON.stringify(out, null, 2));
console.log(`Wrote ${OUT}`);
console.log(`  included: ${out.included_count}`);
console.log(`  excluded: ${out.excluded_count}`);
for (const [iso, e] of Object.entries(excluded)) {
    console.log(`    ${iso}: ${e.license} — ${e.reason}`);
}

updateExcludedTxt(excluded);

function updateExcludedTxt(excludedMap) {
    const BEGIN = '# BEGIN auto-managed by classify_licenses.mjs (do not edit by hand)';
    const END = '# END auto-managed';
    const HEADER = `# ISOs whose source license does not permit redistribution by us.
# These are excluded from release tarballs even when present in data/pkf/.
# Format: one ISO per line. Blank lines and lines starting with # are ignored.
# Inline comments after the ISO are allowed (separated by whitespace).
#
# Two sections:
#   - Manual entries (above the BEGIN marker) are preserved across runs of
#     classify_licenses.mjs. Use this for ISOs the classifier can't catch
#     or that require an out-of-band judgement.
#   - Auto-managed entries (between the BEGIN/END markers) are rewritten on
#     every "make classify" run from data/pkf/licenses.json. Do NOT edit by
#     hand — your changes will be overwritten.
`;

    const autoIsos = new Set(Object.keys(excludedMap));

    // Carry forward any manual entries (everything outside the marker block),
    // dropping ones that have moved into the auto block to avoid duplicates.
    let preservedManual = [];
    if (existsSync(EXCLUDED_TXT)) {
        const lines = readFileSync(EXCLUDED_TXT, 'utf8').split('\n');
        const beginIdx = lines.indexOf(BEGIN);
        const endIdx = lines.indexOf(END);
        const outside =
            beginIdx >= 0 && endIdx > beginIdx
                ? [...lines.slice(0, beginIdx), ...lines.slice(endIdx + 1)]
                : lines;
        for (const raw of outside) {
            const stripped = raw.replace(/#.*$/, '').trim();
            if (!stripped) continue; // skip blanks and pure-comment lines
            const iso = stripped.split(/\s+/)[0];
            if (autoIsos.has(iso)) continue; // moved into auto block
            preservedManual.push(raw.trimEnd());
        }
    }

    const autoLines = [...autoIsos].sort().map((iso) => {
        const e = excludedMap[iso];
        const reason = (e?.reason || e?.license || 'no reason given')
            .replace(/\s+/g, ' ')
            .slice(0, 200);
        return `${iso}  # ${e?.license || 'unknown'} — ${reason}`;
    });

    const manualBlock = preservedManual.length
        ? '# Manual entries:\n' + preservedManual.join('\n') + '\n\n'
        : '';
    const autoBlock = `${BEGIN}\n${autoLines.join('\n')}${autoLines.length ? '\n' : ''}${END}\n`;

    writeFileSync(EXCLUDED_TXT, HEADER + '\n' + manualBlock + autoBlock);
    console.log(
        `Wrote ${EXCLUDED_TXT}  (manual: ${preservedManual.length}, auto: ${autoLines.length})`
    );
}

if (PRUNE) {
    console.log('--prune: removing excluded iso dirs from data/pkf/...');
    for (const iso of Object.keys(excluded)) {
        const d = join(PKF_DIR, iso);
        if (existsSync(d)) {
            rmSync(d, { recursive: true, force: true });
            console.log(`  rm ${d}`);
        }
    }
    // Also rewrite manifest.json to drop excluded ISOs.
    const m = JSON.parse(readFileSync(MANIFEST, 'utf8'));
    const before = m.languages.length;
    m.languages = m.languages.filter((l) => !(l.iso in excluded));
    writeFileSync(MANIFEST, JSON.stringify(m, null, 2));
    console.log(`  manifest.json: ${before} → ${m.languages.length} languages`);
}
