#!/usr/bin/env node
/**
 * Sanity-check every multi-collection language's own PKFs against each other:
 * pull a real text sample (Matthew 1, or the first available book) from each
 * collection and flag if any two collections in the SAME language are
 * byte-identical — that would mean a copy/duplicate-content bug (the wrong
 * file reused for two collections). This does not do real language ID; it's
 * a cheap, durable regression check that runs the same way "verify aom/uth
 * aren't mislabeled" did, but automatically, for the handful of languages
 * that ship more than one collection.
 *
 * Usage: node scripts/audit_multicollection.mjs
 */
import { readFileSync } from 'node:fs';
import { decompressSync, strFromU8 } from 'fflate';
import { Proskomma } from 'proskomma-core';

class SABProskomma extends Proskomma {
  constructor() {
    super();
    this.selectors = [
      { name: 'lang', type: 'string', regex: '^[A-Za-z0-9-]{2,30}$' },
      { name: 'abbr', type: 'string', regex: '^[A-Za-z0-9 -]+$' },
    ];
    this.validateSelectors();
  }
}

function sampleText(pkfPath) {
  const pk = new SABProskomma();
  pk.loadSuccinctDocSet(
    JSON.parse(strFromU8(decompressSync(new Uint8Array(readFileSync(pkfPath)))))
  );
  const dsId = pk.gqlQuerySync('{docSets{id}}').data.docSets[0].id;
  const books = pk.gqlQuerySync(
    `{docSet(id:"${dsId}"){documents{bookCode:header(id:"bookCode")}}}`
  ).data.docSet.documents.map((d) => d.bookCode);
  const bookCode = books.includes('MAT') ? 'MAT' : books[0];
  if (!bookCode) return { bookCode: null, text: '' };
  const r = pk.gqlQuerySync(
    `{docSet(id:"${dsId}"){document(bookCode:"${bookCode}"){ mainSequence { blocks { text } } } } }`
  );
  const block = r.data.docSet.document.mainSequence.blocks.find((b) => b.text);
  return { bookCode, text: (block && block.text) || '' };
}

function main() {
  const manifest = JSON.parse(readFileSync('data/global/manifest.json', 'utf-8'));
  const multi = manifest.languages.filter((e) => (e.pkfs || []).length > 1);

  console.log(`[multicollection] ${multi.length} multi-collection language(s) in the source pool`);
  let bad = 0;
  for (const e of multi) {
    const iso = e.iso;
    const samples = e.pkfs.map((pkf) => {
      const path = `data/_pool/${iso}/${pkf}`;
      const { bookCode, text } = sampleText(path);
      return { pkf, bookCode, text };
    });
    console.log(`\n  ${iso}: ${e.pkfs.length} collections`);
    for (const s of samples) {
      const base = s.pkf.split('.')[0];
      console.log(`    ${base} (${s.bookCode}): ${JSON.stringify((s.text || '').slice(0, 80))}`);
    }
    for (let i = 0; i < samples.length; i++) {
      for (let j = i + 1; j < samples.length; j++) {
        if (samples[i].text && samples[i].text === samples[j].text) {
          bad++;
          console.log(`    !! DUPLICATE CONTENT: ${samples[i].pkf} == ${samples[j].pkf}`);
        }
      }
    }
  }
  console.log(`\n[multicollection] ${bad} duplicate-content issue(s) found`);
  process.exit(bad ? 1 : 0);
}

main();
