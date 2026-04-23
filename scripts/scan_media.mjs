#!/usr/bin/env node
/**
 * Scan every fetched .pkf for in-text figures / videos / audio.
 *
 * USFM pictures come in as `\fig` markers, which Proskomma exposes as:
 *   - a sequence of type "figure" that is grafted from the main text, and/or
 *   - an item with type:"graft", subType:"figure" in a main-sequence block
 *   - a marker:"fig" in the USJ representation, with attrs {src, caption, ...}
 *
 * SAB also supports custom video/audio markers. These tend to appear either as
 * sequences with type "video"/"audio", or as `\vid`/`\aud` custom markers
 * surfacing in USJ.
 */
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { decompressSync, strFromU8 } from 'fflate';
import { Proskomma } from 'proskomma-core';

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

function thawPk(pkfPath) {
    const pk = new SAB();
    pk.loadSuccinctDocSet(
        JSON.parse(strFromU8(decompressSync(new Uint8Array(readFileSync(pkfPath)))))
    );
    return pk;
}

function gatherByIso(root) {
    const out = [];
    for (const name of readdirSync(root)) {
        if (name.startsWith('_')) continue;
        const dir = join(root, name);
        if (!statSync(dir).isDirectory()) continue;
        const pkf = readdirSync(dir).find((f) => f.endsWith('.pkf'));
        if (pkf) out.push({ iso: name, pkfPath: join(dir, pkf) });
    }
    return out.sort((a, b) => a.iso.localeCompare(b.iso));
}

function scanOne(iso, pkfPath) {
    const pk = thawPk(pkfPath);
    const dsid = pk.gqlQuerySync('{docSets{id}}').data.docSets[0].id;
    // 1) Enumerate every sequence type across every book.
    const typeCounts = {};
    const booksWithFigures = [];
    const figureDetails = [];
    const books = pk
        .gqlQuerySync(
            `{docSet(id:"${dsid}"){documents{bc:header(id:"bookCode") nSequences}}}`
        )
        .data.docSet.documents;
    for (const { bc } of books) {
        const r = pk.gqlQuerySync(
            `{docSet(id:"${dsid}"){document(bookCode:"${bc}"){sequences{type id}}}}`
        );
        let hasFigure = false;
        for (const s of r.data.docSet.document.sequences) {
            typeCounts[s.type] = (typeCounts[s.type] || 0) + 1;
            if (s.type === 'fig' || s.type === 'figure' || s.type === 'video' || s.type === 'vid' || s.type === 'audio' || s.type === 'aud') {
                hasFigure = true;
                // Pull figure attributes from the sequence's first block items.
                try {
                    const seqItems = pk.gqlQuerySync(
                        `{docSet(id:"${dsid}"){document(bookCode:"${bc}"){sequence(id:"${s.id}"){blocks{items{type subType payload} scopeLabels}}}}}`
                    );
                    const blocks = seqItems.data.docSet.document.sequence.blocks;
                    const scopes = [...new Set(blocks.flatMap((b) => b.scopeLabels))];
                    const items = blocks.flatMap((b) => b.items);
                    const textDump = items
                        .filter((it) => it.type === 'token')
                        .map((it) => it.payload)
                        .join('');
                    figureDetails.push({
                        bc,
                        type: s.type,
                        id: s.id,
                        text: textDump.slice(0, 240),
                        scopes: scopes.slice(0, 10)
                    });
                } catch {
                    /* shrug */
                }
            }
        }
        if (hasFigure) booksWithFigures.push(bc);
    }
    return { typeCounts, booksWithFigures, figureDetails };
}

const root = 'data/pkf';
const all = gatherByIso(root);

const hits = [];
const summary = {};
for (const { iso, pkfPath } of all) {
    try {
        const r = scanOne(iso, pkfPath);
        for (const k of Object.keys(r.typeCounts))
            summary[k] = (summary[k] || 0) + r.typeCounts[k];
        if (r.booksWithFigures.length > 0) {
            hits.push({ iso, types: r.typeCounts, books: r.booksWithFigures, samples: r.figureDetails.slice(0, 2) });
        }
    } catch (e) {
        process.stderr.write(`${iso}: ERR ${e.message}\n`);
    }
}

console.log(`Scanned ${all.length} languages.\n`);
console.log('Aggregate sequence-type counts across every book of every language:');
for (const [k, v] of Object.entries(summary).sort(([, a], [, b]) => b - a)) {
    console.log(`  ${k.padEnd(12)} ${v}`);
}

console.log(`\nLanguages with figure/video/audio sequences: ${hits.length}`);
for (const h of hits) {
    const figCount = (h.types.fig ?? 0) + (h.types.figure ?? 0);
    const vidCount = (h.types.vid ?? 0) + (h.types.video ?? 0);
    const audCount = (h.types.aud ?? 0) + (h.types.audio ?? 0);
    console.log(
        `  ${h.iso}  figures=${figCount} videos=${vidCount} audio=${audCount}  books=[${h.books.join(',')}]`
    );
    for (const s of h.samples) {
        console.log(`    [${s.type}] ${s.bc}: ${JSON.stringify(s.text)}`);
        if (s.scopes.length) console.log(`        scopes: ${s.scopes.join(' | ')}`);
    }
}
