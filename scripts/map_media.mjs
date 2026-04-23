#!/usr/bin/env node
/**
 * Extract per-language video and audio manifests from each SE deployment's
 * large "contents" JS chunk, and write them into data/pkf/<iso>/info.json
 * under `media`. Nothing is stored locally — all references are absolute URLs
 * to scriptureearth.org / youtube.com / 4.dbt.io, rendered on demand.
 *
 * Structure stored:
 *   media.audio = {
 *     base_url: "https://www.scriptureearth.org/data/<iso>/audio",
 *     items: [ { bookCode, chapter, filename, url, len, size, timingFile } ]
 *   }
 *   media.videos = [
 *     { id, title, width, height, thumbnail, thumbnailUrl, onlineUrl,
 *       kind: 'youtube' | 'hls' | 'other',
 *       placement: { bookCode, chapter, verse, pos } }
 *   ]
 *
 * Idempotent. Safe to re-run after SE redeploys (JS-chunk hashes change, so
 * we always re-discover the manifest chunk via grep).
 */
import { readFileSync, writeFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';

const SE = 'https://scriptureearth.org';
const PKF_ROOT = 'data/pkf';
const IMG_EXT_RE = /\.(jpg|jpeg|png|webp)$/i;
const SW_IMG_RE = /\/_app\/immutable\/assets\/([A-Za-z0-9_\-]+)\.([A-Za-z0-9_\-]+)\.(jpg|jpeg|png|webp)/gi;

// --- low-level utilities ----------------------------------------------------

async function fetchText(url) {
    const r = await fetch(url, { headers: { 'User-Agent': 'bw-map-media/1.0' } });
    if (!r.ok) return null;
    return await r.text();
}

async function headSize(url) {
    try {
        const r = await fetch(url, {
            method: 'HEAD',
            headers: { 'User-Agent': 'bw-map-media/1.0' }
        });
        const cl = r.headers.get('content-length');
        return cl ? parseInt(cl, 10) : 0;
    } catch {
        return 0;
    }
}

/**
 * Walk string from startIdx (which must be '{') and return the index of the
 * matching '}', respecting JS strings so braces inside strings aren't counted.
 */
function findMatchingBrace(text, startIdx) {
    let depth = 0;
    let inStr = false;
    let stringChar = null;
    for (let i = startIdx; i < text.length; i++) {
        const c = text[i];
        if (inStr) {
            if (c === '\\') {
                i++;
                continue;
            }
            if (c === stringChar) inStr = false;
            continue;
        }
        if (c === '"' || c === "'" || c === '`') {
            inStr = true;
            stringChar = c;
            continue;
        }
        if (c === '{') depth++;
        else if (c === '}') {
            depth--;
            if (depth === 0) return i;
        }
    }
    return -1;
}

/** For the given minified JS text and a regex whose first match inside the
 * enclosing object is taken as a handle, return each enclosing {...} object
 * substring (including the outer braces). */
function collectObjectsByHandle(text, handleRe) {
    const out = [];
    handleRe.lastIndex = 0;
    let m;
    while ((m = handleRe.exec(text)) !== null) {
        // Walk backward to find the '{' that starts the enclosing object.
        let idx = m.index;
        let depth = 0;
        while (idx > 0) {
            const c = text[idx];
            if (c === '}') depth++;
            else if (c === '{') {
                if (depth === 0) break;
                depth--;
            }
            idx--;
        }
        if (text[idx] !== '{') continue;
        const end = findMatchingBrace(text, idx);
        if (end > idx) out.push(text.slice(idx, end + 1));
    }
    return out;
}

function getField(objText, key) {
    // Minified JS: keys are usually bare (id:) but sometimes quoted ("id":).
    // Value: either a JS string "…", a number, or a nested object/array — we
    // only extract strings and numbers here.
    const re = new RegExp(
        `(?:^|[\\{,])\\s*["']?${key}["']?\\s*:\\s*("(?:[^"\\\\]|\\\\.)*"|'(?:[^'\\\\]|\\\\.)*'|-?\\d+(?:\\.\\d+)?)`
    );
    const m = objText.match(re);
    if (!m) return null;
    const raw = m[1];
    if (raw.startsWith('"') || raw.startsWith("'")) {
        return JSON.parse(raw.replace(/\\'/g, "'"));
    }
    return parseFloat(raw);
}

/** Find a nested object value for a given key (e.g. "placement"). Returns the
 * substring (including braces) or null. */
function getNestedObject(objText, key) {
    const re = new RegExp(`(?:^|[\\{,])\\s*["']?${key}["']?\\s*:\\s*\\{`);
    const m = objText.match(re);
    if (!m) return null;
    const openIdx = m.index + m[0].length - 1;
    const end = findMatchingBrace(objText, openIdx);
    if (end < 0) return null;
    return objText.slice(openIdx, end + 1);
}

// --- video extraction -------------------------------------------------------

function detectVideoKind(url) {
    if (!url) return 'other';
    if (url.includes('youtube.com') || url.includes('youtu.be')) return 'youtube';
    if (url.includes('.m3u8')) return 'hls';
    if (url.includes('arclight.org')) return 'arclight';
    if (url.includes('vimeo.com')) return 'vimeo';
    if (/\.(mp4|webm|ogv|m4v|mov)(\?|$)/i.test(url)) return 'file';
    return 'other';
}

/** SAB stores some URLs pre-HTML-encoded (e.g. `&amp;` instead of `&`).
 *  Decode the handful of entities we actually see so the URL works verbatim
 *  as an iframe src or a fetch target. */
function decodeHtmlEntities(s) {
    if (!s) return s;
    return s
        .replace(/&amp;/g, '&')
        .replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>')
        .replace(/&quot;/g, '"')
        .replace(/&#39;/g, "'");
}

function parsePlacementRef(refStr) {
    if (!refStr) return {};
    // Accepts "MRK 1:1", "MRK.1.1", "JHN.1.2", etc.
    const m = refStr.match(/^([A-Z1-3]{2,4})[\s.:](\d+)(?:[.:](\d+))?/);
    if (!m) return { rawRef: refStr };
    return {
        bookCode: m[1],
        chapter: parseInt(m[2], 10),
        verse: m[3] != null ? parseInt(m[3], 10) : null
    };
}

function extractVideos(chunkText, imageUrlMap) {
    const videoObjs = collectObjectsByHandle(chunkText, /\bonlineUrl\s*:/g);
    const out = [];
    const seen = new Set();
    for (const obj of videoObjs) {
        const id = getField(obj, 'id');
        const onlineUrl = getField(obj, 'onlineUrl');
        if (!id || !onlineUrl) continue;
        const key = `${id}|${onlineUrl}`;
        if (seen.has(key)) continue;
        seen.add(key);

        const thumbnail = getField(obj, 'thumbnail');
        const placementText = getNestedObject(obj, 'placement');
        const placement = placementText
            ? {
                  ...parsePlacementRef(getField(placementText, 'ref')),
                  pos: getField(placementText, 'pos') ?? null,
                  collection: getField(placementText, 'collection') ?? null
              }
            : {};

        const decodedUrl = decodeHtmlEntities(onlineUrl);
        out.push({
            id,
            title: getField(obj, 'title') ?? '',
            width: getField(obj, 'width') ?? null,
            height: getField(obj, 'height') ?? null,
            thumbnail: thumbnail ?? null,
            thumbnailUrl: thumbnail ? imageUrlMap[thumbnail] ?? null : null,
            onlineUrl: decodedUrl,
            kind: detectVideoKind(decodedUrl),
            placement
        });
    }
    return out;
}

// --- audio extraction -------------------------------------------------------

function extractAudioSources(chunkText) {
    // SE's SAB apps expose these under `sources:{d1:{type:"download",...,address:"http://..."}}`.
    // We anchor on the inner shape rather than the parent key name (which has
    // been observed as `sources`, sometimes `audioSources`) for robustness.
    const re = /\b(?:audio)?[Ss]ources\s*:\s*\{\s*[A-Za-z][A-Za-z0-9_]*\s*:\s*\{\s*type\s*:\s*"(?:download|streaming)"/g;
    const m = re.exec(chunkText);
    if (!m) return {};
    const openIdx = chunkText.indexOf('{', m.index);
    const end = findMatchingBrace(chunkText, openIdx);
    if (end < 0) return {};
    const inner = chunkText.slice(openIdx, end + 1);

    const sources = {};
    const keyRe = /([A-Za-z][A-Za-z0-9_]*)\s*:\s*\{/g;
    let km;
    while ((km = keyRe.exec(inner)) !== null) {
        const k = km[1];
        const start = keyRe.lastIndex - 1;
        const e = findMatchingBrace(inner, start);
        if (e < 0) continue;
        const body = inner.slice(start, e + 1);
        const address = getField(body, 'address');
        if (address) sources[k] = address.replace(/^http:\/\//, 'https://');
        keyRe.lastIndex = e + 1;
    }
    return sources;
}

/** Parse USFM bookCode+chapter out of the canonical filename pattern
 *  "<iso>-NN-BKC-CC.mp3"  →  { bookCode: "BKC", chapter: CC }. */
function parseAudioFilename(filename) {
    const m = filename.match(/-(\d{2})-([A-Z0-9]{3})-(\d+)\.mp3$/i);
    if (!m) return {};
    return { bookCode: m[2].toUpperCase(), chapter: parseInt(m[3], 10) };
}

function extractAudioItems(chunkText, sources) {
    const objs = collectObjectsByHandle(
        chunkText,
        /\bnum\s*:\s*\d+\s*,\s*filename\s*:\s*"[^"]+\.mp3"/g
    );
    const seen = new Set();
    const out = [];
    for (const obj of objs) {
        const filename = getField(obj, 'filename');
        if (!filename || !filename.endsWith('.mp3')) continue;
        if (seen.has(filename)) continue;
        seen.add(filename);
        const src = getField(obj, 'src') ?? '';
        const baseUrl = sources[src] ?? null;
        const url = baseUrl ? `${baseUrl}/${filename}` : null;
        const { bookCode, chapter } = parseAudioFilename(filename);
        out.push({
            filename,
            url,
            bookCode: bookCode ?? null,
            chapter: chapter ?? null,
            num: getField(obj, 'num') ?? null,
            len: getField(obj, 'len') ?? null,
            size: getField(obj, 'size') ?? null,
            timingFile: getField(obj, 'timingFile') ?? null,
            src
        });
    }
    return out;
}

// --- service-worker helpers -------------------------------------------------

function imageUrlMapFrom(iso, swText) {
    const map = {};
    for (const m of swText.matchAll(SW_IMG_RE)) {
        const [, base, hash, ext] = m;
        map[`${base}.${ext}`] = `${SE}/data/${iso}/sab/${iso}/_app/immutable/assets/${base}.${hash}.${ext}`;
    }
    return map;
}

function chunkUrlsFrom(swText) {
    const out = [];
    const seen = new Set();
    for (const m of swText.matchAll(/s\+"(\/_app\/immutable\/(?:chunks|nodes)\/[A-Za-z0-9_.\-]+\.js)"/g)) {
        if (seen.has(m[1])) continue;
        seen.add(m[1]);
        out.push(m[1]);
    }
    return out;
}

// --- main manifest-finder ---------------------------------------------------

async function findManifestChunk(iso, chunkPaths) {
    // HEAD each chunk to get content-length, then try largest-first. The
    // contents manifest is typically the largest chunk by a wide margin.
    const withSizes = await Promise.all(
        chunkPaths.map(async (p) => ({
            url: `${SE}/data/${iso}/sab/${iso}${p}`,
            size: await headSize(`${SE}/data/${iso}/sab/${iso}${p}`)
        }))
    );
    withSizes.sort((a, b) => b.size - a.size);

    // Try chunks in descending size until we find one with onlineUrl or audioSources.
    // Stop after a max of 12 chunks; the manifest is always near the top.
    for (let i = 0; i < Math.min(12, withSizes.length); i++) {
        const body = await fetchText(withSizes[i].url);
        if (!body) continue;
        if (body.includes('onlineUrl:') || body.includes('audioSources:')) {
            return body;
        }
    }
    return null;
}

async function processIso(iso) {
    const isoDir = join(PKF_ROOT, iso);
    const infoPath = join(isoDir, 'info.json');
    if (!statSync(isoDir, { throwIfNoEntry: false })) return null;

    let info;
    try {
        info = JSON.parse(readFileSync(infoPath, 'utf8'));
    } catch {
        return null;
    }

    const sw = await fetchText(`${SE}/data/${iso}/sab/${iso}/service-worker.js`);
    if (!sw) return { iso, error: 'no SW' };

    const imageMap = imageUrlMapFrom(iso, sw);
    const chunks = chunkUrlsFrom(sw);
    if (chunks.length === 0) return { iso, videos: 0, audio: 0 };

    const manifestText = await findManifestChunk(iso, chunks);
    if (!manifestText) return { iso, videos: 0, audio: 0 };

    const videos = extractVideos(manifestText, imageMap);
    const sources = extractAudioSources(manifestText);
    const audioItems = extractAudioItems(manifestText, sources);

    if (videos.length === 0 && audioItems.length === 0) {
        // Clean stale entries
        if ('media' in info) {
            delete info.media;
            writeFileSync(infoPath, JSON.stringify(info, null, 2));
        }
        return { iso, videos: 0, audio: 0 };
    }

    const base_url = sources.d1 ?? Object.values(sources)[0] ?? null;
    info.media = {
        videos,
        audio: {
            base_url,
            items: audioItems
        }
    };
    writeFileSync(infoPath, JSON.stringify(info, null, 2));

    return {
        iso,
        videos: videos.length,
        video_kinds: videos.reduce((acc, v) => {
            acc[v.kind] = (acc[v.kind] || 0) + 1;
            return acc;
        }, {}),
        audio: audioItems.length,
        base_url
    };
}

async function main() {
    const argv = process.argv.slice(2);
    const filter = argv.length ? new Set(argv) : null;
    const concurrency = parseInt(process.env.CONCURRENCY || '6', 10);
    const isos = readdirSync(PKF_ROOT)
        .filter((n) => !n.startsWith('_') && statSync(join(PKF_ROOT, n)).isDirectory())
        .filter((n) => (filter ? filter.has(n) : true))
        .sort();

    let withVideo = 0;
    let withAudio = 0;
    let done = 0;
    const videoKindTotals = {};

    // Worker-pool pattern: N workers pull from a shared queue.
    const queue = isos.slice();
    async function worker() {
        while (queue.length > 0) {
            const iso = queue.shift();
            if (!iso) break;
            try {
                const r = await processIso(iso);
                done++;
                if (!r) continue;
                if (r.error) {
                    console.log(`  ${iso}: ${r.error}  (${done}/${isos.length})`);
                    continue;
                }
                if (r.videos > 0 || r.audio > 0) {
                    if (r.videos) {
                        withVideo++;
                        for (const [k, v] of Object.entries(r.video_kinds || {})) {
                            videoKindTotals[k] = (videoKindTotals[k] || 0) + v;
                        }
                    }
                    if (r.audio) withAudio++;
                    const kinds = Object.entries(r.video_kinds || {})
                        .map(([k, v]) => `${k}=${v}`)
                        .join(',');
                    console.log(
                        `  ${iso.padEnd(6)} videos=${String(r.videos).padStart(3)} [${kinds}]  audio=${String(r.audio).padStart(3)}  (${done}/${isos.length})`
                    );
                } else {
                    // Quiet for no-media langs, but count.
                }
            } catch (e) {
                console.error(`${iso}: ${e.message}`);
            }
        }
    }
    await Promise.all(Array.from({ length: concurrency }, () => worker()));

    console.log(
        `\nDone. ${withVideo} languages with videos, ${withAudio} with audio. Totals by kind: ${JSON.stringify(videoKindTotals)}.`
    );
}

main().catch((e) => {
    console.error(e);
    process.exit(1);
});
