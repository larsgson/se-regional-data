#!/usr/bin/env node
/**
 * Probe: thaw a pkf, fetch sofria for a chapter, and print a condensed render.
 *   node scripts/probe_render.mjs data/pkf/nch ISA 1
 */
import { readFileSync, readdirSync } from 'node:fs';
import { join } from 'node:path';
import { decompressSync, strFromU8 } from 'fflate';
import { Proskomma } from 'proskomma-core';

const [, , isoDir, bookCode = 'GEN', chapter = '1'] = process.argv;
const pkfName = readdirSync(isoDir).find((f) => f.endsWith('.pkf'));
if (!pkfName) throw new Error(`no pkf in ${isoDir}`);

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

const pk = new SAB();
pk.loadSuccinctDocSet(
    JSON.parse(strFromU8(decompressSync(new Uint8Array(readFileSync(join(isoDir, pkfName))))))
);
const dsid = pk.gqlQuerySync('{docSets{id}}').data.docSets[0].id;
const raw = pk.gqlQuerySync(
    `{docSet(id:"${dsid}"){document(bookCode:"${bookCode}"){sofria(chapter:${chapter})}}}`
).data.docSet.document.sofria;
const doc = JSON.parse(raw);

// tiny subset of the full renderer, just enough to spot-check output
function esc(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
const usfm = (s) => (s?.startsWith('usfm:') ? s.slice(5) : s ?? '');

const fns = [];
function ri(items) {
    let o = '';
    for (const it of items ?? []) {
        if (typeof it === 'string') { o += esc(it); continue; }
        if (it.type === 'mark' && it.subtype === 'verses_label') { o += `<sup class="v">${esc(it.atts?.number ?? '')}</sup>`; continue; }
        if (it.type === 'wrapper' && (it.subtype === 'chapter' || it.subtype === 'verses')) { o += ri(it.content); continue; }
        if (it.type === 'wrapper') { o += `<span class="char ${esc(usfm(it.subtype))}">${ri(it.content)}</span>`; continue; }
        if (it.type === 'graft' && it.subtype === 'footnote') {
            let caller = '';
            const parts = [];
            for (const b of it.sequence.blocks ?? []) for (const x of b.content ?? []) {
                if (typeof x === 'object' && x.type === 'graft' && x.subtype === 'note_caller') {
                    caller = (x.sequence.blocks?.[0]?.content ?? []).filter(c => typeof c === 'string').join('');
                } else parts.push(ri([x]));
            }
            fns.push({ caller, body: parts.join('') });
            o += `<sup class="note-caller">[${esc(caller || fns.length)}]</sup>`;
        }
    }
    return o;
}

const html = [];
for (const b of doc.sequence.blocks ?? []) {
    if (b.type === 'graft' && (b.subtype === 'title' || b.subtype === 'heading')) {
        for (const p of b.sequence.blocks ?? []) if (p.type === 'paragraph')
            html.push(`<${b.subtype === 'title' ? 'h2' : 'h3'}>${ri(p.content)}</${b.subtype === 'title' ? 'h2' : 'h3'}>`);
    }
    if (b.type === 'paragraph') html.push(`<p class="${esc(usfm(b.subtype)) || 'p'}">${ri(b.content)}</p>`);
}

console.log(html.join('\n'));
console.log('\nFOOTNOTES:', fns.length);
fns.forEach((f, i) => console.log(`  [${f.caller || i + 1}] ${f.body.slice(0, 140)}…`));
