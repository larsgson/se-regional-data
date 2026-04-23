#!/usr/bin/env node
/**
 * Local smoke test: thaw a pkf, list docsets + books, query chapters a few ways.
 *
 *   node scripts/probe_pkf.mjs data/pkf/zai/zai_zai.0HgVnSWZ.pkf JHN 3
 */
import { readFileSync } from 'node:fs';
import { decompressSync, strFromU8 } from 'fflate';
import { Proskomma } from 'proskomma-core';

const [, , pkfPath, bookCode = 'JHN', chapter = '3'] = process.argv;
if (!pkfPath) {
    console.error('usage: node scripts/probe_pkf.mjs <pkf-path> [bookCode] [chapter]');
    process.exit(2);
}

// SABProskomma-equivalent: accept BCP47 lang selectors.
class SABProskomma extends Proskomma {
    constructor() {
        super();
        this.selectors = [
            { name: 'lang', type: 'string', regex: '^[A-Za-z0-9-]{2,30}$' },
            { name: 'abbr', type: 'string', regex: '^[A-Za-z0-9 -]+$' }
        ];
        this.validateSelectors();
    }
}

function thaw(pk, frozen) {
    const json = JSON.parse(strFromU8(decompressSync(frozen)));
    return pk.loadSuccinctDocSet(json);
}

const buf = new Uint8Array(readFileSync(pkfPath));
const pk = new SABProskomma();
thaw(pk, buf);

const ds = pk.gqlQuerySync('{ docSets { id } }');
console.log('docSets:', JSON.stringify(ds.data.docSets));
const docSetId = ds.data.docSets[0].id;

const books = pk.gqlQuerySync(`{
    docSet(id: "${docSetId}") {
        documents {
            bookCode: header(id: "bookCode")
            toc: header(id: "toc")
        }
    }
}`);
console.log(`books (${books.data.docSet.documents.length}):`,
    books.data.docSet.documents.slice(0, 6).map(d => d.bookCode).join(','), '...');

const shapes = [
    ['cv_chapter_string', `{ docSet(id: "${docSetId}") { document(bookCode: "${bookCode}") { cv(chapter: "${chapter}") { text(normalizeSpace: true) } } } }`],
    ['cv_chapterVerses',   `{ docSet(id: "${docSetId}") { document(bookCode: "${bookCode}") { cv(chapterVerses: "${chapter}") { text(normalizeSpace: true) } } } }`],
    ['cvIndex',            `{ docSet(id: "${docSetId}") { document(bookCode: "${bookCode}") { cvIndex(chapter: ${chapter}) { chapter verseNumbers { number range text } } } } }`],
    ['cvIndexes_range',    `{ docSet(id: "${docSetId}") { document(bookCode: "${bookCode}") { cvIndexes { chapter verseNumbers { number range text } } } } }`]
];
for (const [name, q] of shapes) {
    try {
        const r = pk.gqlQuerySync(q);
        const s = JSON.stringify(r);
        console.log(`\n=== ${name} (${s.length} chars) ===`);
        console.log(s.slice(0, 500));
    } catch (e) {
        console.log(`\n=== ${name}: ERROR ${e.message} ===`);
    }
}
