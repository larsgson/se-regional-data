#!/usr/bin/env node
/**
 * Focused test of the inline-video placement algorithm from sofria.ts,
 * re-implemented inline to avoid pulling in the full module dependency chain.
 * Validates that the renderer emits inline thumbnails before/after the
 * requested verse numbers, including the "no pos → defaults to after" case.
 */

function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

function renderInlineVideos(videos, verse, pos) {
    const parts = [];
    for (const v of videos) {
        if (v.placement?.verse !== verse) continue;
        const vpos = v.placement?.pos === 'before' ? 'before' : 'after';
        if (vpos !== pos) continue;
        parts.push(`<span class="reader-inline-video" data-video-id="${esc(v.id)}">${esc(v.title)}</span>`);
    }
    return parts.join('');
}

function renderInline(items, videos) {
    if (!items) return '';
    let out = '';
    for (const it of items) {
        if (typeof it === 'string') { out += esc(it); continue; }
        if (it.type === 'mark') {
            if (it.subtype === 'verses_label') out += `<sup class="v">${esc(it.atts?.number ?? '')}</sup>`;
            continue;
        }
        if (it.type === 'wrapper') {
            const st = it.subtype ?? '';
            if (st === 'chapter') { out += renderInline(it.content, videos); continue; }
            if (st === 'verses') {
                const n = parseInt(String(it.atts?.number ?? ''), 10);
                if (Number.isFinite(n)) {
                    out += renderInlineVideos(videos, n, 'before');
                    out += renderInline(it.content, videos);
                    out += renderInlineVideos(videos, n, 'after');
                } else {
                    out += renderInline(it.content, videos);
                }
                continue;
            }
            out += renderInline(it.content, videos);
        }
    }
    return out;
}

const doc = {
    content: [
        { type: 'wrapper', subtype: 'chapter', content: [
            { type: 'wrapper', subtype: 'verses', atts: { number: '1' }, content: [
                { type: 'mark', subtype: 'verses_label', atts: { number: '1' } }, 'Verse one text. '
            ]},
            { type: 'wrapper', subtype: 'verses', atts: { number: '2' }, content: [
                { type: 'mark', subtype: 'verses_label', atts: { number: '2' } }, 'Verse two text. '
            ]},
            { type: 'wrapper', subtype: 'verses', atts: { number: '3' }, content: [
                { type: 'mark', subtype: 'verses_label', atts: { number: '3' } }, 'Verse three text.'
            ]}
        ]}
    ]
};

const videos = [
    { id: 'v-before-2', title: 'Before v.2', placement: { verse: 2, pos: 'before' } },
    { id: 'v-after-3',  title: 'After v.3',  placement: { verse: 3, pos: 'after' } },
    { id: 'v-default',  title: 'Default v.1', placement: { verse: 1 } } // no pos → after
];

const html = renderInline(doc.content, videos);
console.log('HTML:\n' + html + '\n');

const tests = [
    ['v-before-2 emitted',       html.includes('data-video-id="v-before-2"')],
    ['v-after-3 emitted',        html.includes('data-video-id="v-after-3"')],
    ['v-default emitted',        html.includes('data-video-id="v-default"')],
    ['v-before-2 is BEFORE v.2', html.indexOf('v-before-2') < html.indexOf('Verse two')],
    ['v-after-3 is AFTER v.3',   html.indexOf('v-after-3') > html.indexOf('Verse three')],
    ['v-default after v.1',      html.indexOf('v-default') > html.indexOf('Verse one') &&
                                 html.indexOf('v-default') < html.indexOf('Verse two')]
];
let pass = 0, fail = 0;
for (const [name, ok] of tests) {
    console.log(`  ${ok ? 'OK  ' : 'FAIL'}  ${name}`);
    ok ? pass++ : fail++;
}
console.log(`\n${pass} pass, ${fail} fail`);
process.exit(fail ? 1 : 0);
