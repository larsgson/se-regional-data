#!/usr/bin/env node
/**
 * Pipeline step: probe every ISO's Scripture Earth SAB deployment and
 * classify the scripture-text license. Emits data/pkf/licenses.json.
 *
 * DECISION LOGIC (corrected 2026-04-23):
 *
 *   1. Extract the Texto: block (the per-work copyright declaration).
 *   2. Negative signals inside the Texto: block → EXCLUDE:
 *        - "Usado con permiso" / "Used with permission"
 *        - "Todos los derechos reservados" / "All rights reserved"
 *        - Biblica NVI bundling ("NUEVA VERSIÓN INTERNACIONAL", "NVI®", "Biblica")
 *        - "Texto en proceso de finalizar" / "in process" / "provisional"
 *   3. Positive signal anywhere in the whole JS bundle → INCLUDE:
 *        - "Creative Commons" near any of:
 *            Atribución-NoComercial-SinDerivadas
 *            Reconocimiento-NoComercial-SinObraDerivada  (knj-style Spanish)
 *            BY-NC-ND / by-nc-nd
 *        - bare "(BY-NC-ND)" token
 *        - creativecommons.org/licenses/by-nc-nd URL literal
 *   4. Otherwise → EXCLUDE as "unclear".
 *
 * The `by-nc-nd.<hash>.png` badge cached by the service worker is recorded
 * as evidence only — it's NOT used in the decision. SAB tooling emits the
 * badge for all Wycliffe-managed texts regardless of actual license state,
 * so it has ~3% false-positive rate (badge but not CC: hch, poi, top, nlv)
 * and ~5% false-negative rate (CC but no badge: chd, cuc, knj, tpt, vmz, zty).
 *
 * Usage:
 *   node scripts/classify_licenses.mjs                 # write licenses.json
 *   node scripts/classify_licenses.mjs --prune         # also rm excluded iso dirs
 *
 * Requires data/pkf/manifest.json (produced by fetch_pkf.py). Run AFTER
 * fetch_pkf.py + dedupe_assets.py but BEFORE packing the release tarball.
 *
 * Disk cache: per-iso concatenated string-literal corpus is cached at
 * data/.license-scan-cache/<iso>.<chunk-paths-sha1>.txt. Cache hits skip
 * all chunk network fetches; SE redeploys auto-invalidate via the chunk
 * hashes. To force a full re-scan, `rm -rf data/.license-scan-cache/`.
 */
import { readFileSync, writeFileSync, rmSync, existsSync, mkdirSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { join } from 'node:path';

const ROOT = process.cwd();
const PKF_DIR = join(ROOT, 'data', 'pkf');
const MANIFEST = join(PKF_DIR, 'manifest.json');
const OUT = join(PKF_DIR, 'licenses.json');
const EXCLUDED_TXT = join(ROOT, 'EXCLUDED_ISOS.txt');
const CACHE_DIR = join(ROOT, 'data', '.license-scan-cache');
const PRUNE = process.argv.includes('--prune');
// SE's Apache rate-limits at ~8 req/s per IP and returns 429 beyond that —
// run sequentially by default. CONCURRENCY=2 is usually still safe; 4+ is not.
const CONCURRENCY = Number(process.env.CONCURRENCY || 1);
const CHUNK_DELAY_MS = Number(process.env.CHUNK_DELAY_MS || 150);

if (!existsSync(MANIFEST)) {
    console.error(`Missing ${MANIFEST}. Run fetch_pkf.py first.`);
    process.exit(1);
}
mkdirSync(CACHE_DIR, { recursive: true });

const manifest = JSON.parse(readFileSync(MANIFEST, 'utf8'));
const isos = manifest.languages.map((l) => l.iso);

// SE's Apache returns truncated/stub bodies on the JS chunks when the
// User-Agent isn't a real browser — even though service-worker.js itself is
// returned fine. A browser-like UA + explicit Accept header is mandatory.
const HEADERS = {
    'User-Agent':
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15',
    Accept: '*/*'
};

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function fetchText(url, tries = 4) {
    for (let i = 0; i < tries; i++) {
        try {
            const r = await fetch(url, { headers: HEADERS });
            if (r.ok) return await r.text();
            if (![429, 403, 502, 503].includes(r.status)) return '';
            // Rate-limited — exponential backoff (1s, 2s, 4s, 8s).
            await sleep(1000 * 2 ** i);
        } catch {
            await sleep(1000 * 2 ** i);
        }
    }
    return '';
}

// Evidence-only: is the badge image cached by the SW? Not used in decisions.
const BADGE = /by-nc-nd\.[A-Za-z0-9_-]+\.(?:png|svg)/i;

// Positive signal — search the WHOLE concatenated JS (Texto: block alone is
// too narrow; CC declaration often lives in a sibling div after Imágenes:).
// Handles:
//   - Atribución-NoComercial-SinDerivadas (most Spanish MX apps)
//   - Reconocimiento-NoComercial-SinObraDerivada (knj-style alt Spanish)
//   - Attribution-Noncommercial-No Derivative Works (English SAB apps, e.g. ood)
//   - BY-NC-ND / by-nc-nd (English code)
//   - Tags/whitespace interleaved between "Creative Commons" and the variant
//   - Bare "(BY-NC-ND)" token
//   - creativecommons.org URL literal
const CC_TEXT =
    /Creative Commons[\s\S]{0,150}?(?:Atribución-NoComercial-SinDerivadas|Reconocimiento-NoComercial-SinObraDerivada|Attribution-?Noncommercial-?No[\s-]*Derivative[\s-]*Works|BY-NC-ND|by-nc-nd)|\(BY-NC-ND\)|creativecommons\.org\/licenses\/by-nc-nd/i;

const JS_URL = /\/_app\/immutable\/(?:chunks|entry|nodes)\/[A-Za-z0-9._-]+\.js/g;
const STRING_LIT = /"([^"\\]{3,4000})"/g;

/** Pull the contents of the Texto: block up to the next <b>…</b> sibling. */
function extractTexto(joined) {
    // Primary: "<b>Texto:</b>" (with optional spaces around the colon)
    const m =
        joined.match(
            /Texto[:\s]*<\/b>([\s\S]{0,2000}?)(?=<\/div>|<b>\s*Audio|<b>\s*Im[áa]genes|<b>\s*Ilustrac|<b>\s*Images|<div)/i
        ) ||
        // Fallback: "Texto:" without the closing <b>
        joined.match(
            />\s*Texto[:\s]*([\s\S]{0,2000}?)(?=<\/div>|Audio:|Im[áa]genes:|Ilustraciones|Images:)/i
        );
    return m ? m[1].slice(0, 1500).trim() : '';
}

/** Short copyright-holder extraction from the Texto: block. */
function extractHolder(texto) {
    const m = texto.match(/©\s*([0-9,\s]+[^<]{2,160})/);
    return m ? m[1].replace(/\s+/g, ' ').trim() : '';
}

/** Scoped negative-signal check — Texto: block only. */
function classifyTexto(texto) {
    const t = texto.toLowerCase();
    if (/usado con permiso|used with permission/.test(t))
        return { ok: false, reason: 'Texto: "Usado con permiso" — permission-only, not CC' };
    if (/todos los derechos reservados|all rights reserved/.test(t))
        return { ok: false, reason: 'Texto: "Todos los derechos reservados" — ARR declaration' };
    if (/nueva versi[óo]n internacional|nvi®|biblica/i.test(texto))
        return {
            ok: false,
            reason: 'Texto: bundles Biblica NVI translation — proprietary, not CC'
        };
    if (/proceso de finalizar|in process|provisional/i.test(texto))
        return { ok: false, reason: 'Texto: provisional / not-final translation' };
    return { ok: true };
}

/**
 * Build and cache the concatenated string-literal corpus for one ISO.
 * Key: sha1(iso + sorted chunk paths) — invalidates automatically when SE
 * redeploys (chunk hashes change).
 */
async function joinedJsForIso(iso) {
    const base = `https://scriptureearth.org/data/${iso}/sab/${iso}`;
    const sw = await fetchText(`${base}/service-worker.js`);
    if (!sw) return { unreachable: true, sw: '', joined: '', jsPaths: [] };

    const jsPaths = [...new Set([...sw.matchAll(JS_URL)].map((m) => m[0]))].sort();
    const key = createHash('sha1').update(`${iso}|${jsPaths.join('|')}`).digest('hex').slice(0, 16);
    const cachePath = join(CACHE_DIR, `${iso}.${key}.txt`);
    if (existsSync(cachePath)) {
        return { sw, joined: readFileSync(cachePath, 'utf8'), jsPaths };
    }

    let joined = '';
    for (const p of jsPaths) {
        // Chunk paths are relative to the per-iso SAB app root, not the
        // domain root — must prefix with `${base}`, not `${SE}`.
        const body = await fetchText(`${base}${p}`);
        if (body) for (const m of body.matchAll(STRING_LIT)) joined += m[1] + '\n';
        await sleep(CHUNK_DELAY_MS);
    }
    writeFileSync(cachePath, joined);
    return { sw, joined, jsPaths };
}

async function probeIso(iso) {
    const { unreachable, sw, joined } = await joinedJsForIso(iso);
    if (unreachable) return { iso, unreachable: true };

    const badge = BADGE.test(sw);
    const texto = extractTexto(joined);
    const holder = extractHolder(texto);
    const ccText = CC_TEXT.test(joined);
    const decision = classifyTexto(texto);
    const evidence = { badge_in_sw: badge, cc_text_in_js: ccText };

    if (!decision.ok) {
        return {
            iso,
            include: false,
            license: 'not-cc',
            reason: decision.reason,
            texto,
            text_holder: holder,
            evidence
        };
    }
    if (ccText) {
        // Badge no longer a deciding factor — recorded in evidence only.
        return {
            iso,
            include: true,
            license: 'CC-BY-NC-ND-4.0',
            texto,
            text_holder: holder,
            evidence
        };
    }
    return {
        iso,
        include: false,
        license: 'unclear',
        reason: 'No CC declaration found in Texto block or JS bundle',
        texto,
        text_holder: holder,
        evidence
    };
}

async function runPool(items, worker, size) {
    const results = new Array(items.length);
    let i = 0;
    await Promise.all(
        Array.from({ length: size }, async () => {
            while (i < items.length) {
                const idx = i++;
                try { results[idx] = await worker(items[idx]); }
                catch (e) { results[idx] = { iso: items[idx], error: String(e) }; }
            }
        })
    );
    return results;
}

console.log(`Classifying ${isos.length} ISOs (${CONCURRENCY} concurrent)...`);
const results = await runPool(isos, probeIso, CONCURRENCY);

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
            texto: r.texto,
            evidence: r.evidence
        };
    }
}

const out = {
    schema_version: 1,
    classifier_version: 3,
    updated_at: new Date().toISOString().slice(0, 10),
    source: 'Scripture Earth SAB app service-worker + JS chunk scan',
    notes: [
        'License applies to the scripture TEXT only (the .pkf data).',
        'Images, audio, and video referenced by SE URL carry their own per-asset licenses and are NOT covered by this classification.',
        'Badge presence is recorded as evidence but not used in the decision — it is unreliable (SAB tooling emits it for all Wycliffe texts regardless of actual license status).'
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
            if (!stripped) continue;
            const iso = stripped.split(/\s+/)[0];
            if (autoIsos.has(iso)) continue;
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
    const m = JSON.parse(readFileSync(MANIFEST, 'utf8'));
    const before = m.languages.length;
    m.languages = m.languages.filter((l) => !(l.iso in excluded));
    writeFileSync(MANIFEST, JSON.stringify(m, null, 2));
    console.log(`  manifest.json: ${before} → ${m.languages.length} languages`);
}
