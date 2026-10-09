#!/usr/bin/env node
/**
 * Export each pool language's Proskomma .pkf as per-chapter Sofria, so a reader
 * can fetch one chapter instead of downloading and unpacking the whole .pkf.
 *
 *   data/_pool/<iso>/sofria/<BOOK>/<chapter>.sofria.json
 *   data/_pool/<iso>/glossary.json            {term: definition}
 *
 * and in <iso>/info.json:
 *   formats.sofria = { path: "sofria/<BOOK>/<chapter>.sofria.json",
 *                      granularity: "chapter", [collections: {<base>: path}] }
 *   glossary       = "glossary.json"          (only when the language has one)
 *
 * Same layout as cdn.bibel.wiki/openbible/…  Each file is what Proskomma's
 * `sofria(chapter:N)` returns (chapter 1 also carries the book title and
 * introduction). Where that query throws (e.g. a table running across a chapter
 * break) the chapter is cut from the whole-book Sofria with sliceChapter — a copy
 * of bcv-commons/bibles tools/sofria-render/src/sources/pkf.js — and the file
 * carries `"fallback": "whole-book"`.
 *
 * Collections: the language's own collection (<iso>_*) is written at sofria/;
 * a companion collection (e.g. niy's fra_C02) goes to sofria/<base>/ and is
 * listed under formats.sofria.collections. Packages in EXCLUDED_PACKAGES.txt
 * and ISOs in EXCLUDED_ISOS.txt are never exported.
 *
 * Usage: node scripts/export_sofria.mjs [iso …] [--force] [--jobs=N]
 *
 * Idempotent: a language is skipped while sofria/.source (the list of .pkf
 * names it was built from) matches; files are rewritten only when their bytes
 * change.
 */
import {
  readFileSync, writeFileSync, readdirSync, existsSync, mkdirSync, rmSync,
} from 'node:fs';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { spawn } from 'node:child_process';
import { decompressSync, strFromU8 } from 'fflate';
import { Proskomma } from 'proskomma-core';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const POOL = join(ROOT, 'data', '_pool');
const SOFRIA_PATH = 'sofria/<BOOK>/<chapter>.sofria.json';

class SAB extends Proskomma {
  constructor() {
    super();
    this.selectors = [
      { name: 'lang', type: 'string', regex: '^[A-Za-z0-9-]{2,30}$' },
      { name: 'abbr', type: 'string', regex: '^[A-Za-z0-9 -]+$' },
    ];
    this.validateSelectors();
  }
}

function readSet(file) {
  if (!existsSync(file)) return new Set();
  return new Set(
    readFileSync(file, 'utf8').split('\n')
      .map((l) => l.replace(/#.*/, '').trim().split(/\s+/)[0])
      .filter(Boolean),
  );
}

function writeIfChanged(path, text) {
  if (existsSync(path) && readFileSync(path, 'utf8') === text) return false;
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, text);
  return true;
}

/** Cut one chapter out of a whole-book Sofria document (bibles' sliceChapter). */
export function sliceChapter(bookDoc, chapter) {
  const want = String(chapter);
  const blocks = bookDoc.sequence?.blocks || [];
  const out = [];
  let pending = [];
  let current = null;
  for (const b of blocks) {
    if (b.type === 'graft') { pending.push(b); continue; }
    const kept = [];
    let firstChapter = null;
    for (const item of b.content || []) {
      if (item && item.type === 'wrapper' && item.subtype === 'chapter') {
        current = String(Array.isArray(item.atts?.number) ? item.atts.number[0] : item.atts?.number);
        if (firstChapter == null) firstChapter = current;
      }
      if ((current ?? '1') === want) kept.push(item);
    }
    const graftChapter = firstChapter ?? current ?? '1';
    if (graftChapter === want) out.push(...pending);
    pending = [];
    if (kept.length) out.push({ ...b, content: kept });
  }
  if ((current ?? '1') === want) out.push(...pending);
  return { ...bookDoc, sequence: { ...bookDoc.sequence, blocks: out } };
}

/** All text under a Sofria node (grafts such as notes excluded). */
function plain(items) {
  let s = '';
  for (const it of items || []) {
    if (typeof it === 'string') s += it;
    else if (it && it.type === 'wrapper') s += plain(it.content);
  }
  return s;
}

/**
 * GLO book -> {term: definition}. Each entry is `\k term\k* definition`; a
 * paragraph without a \k continues the previous entry.
 */
export function glossaryFrom(doc) {
  const out = {};
  let last = null;
  const walk = (items) => {
    // Find the leading \k wrapper (it may sit inside the chapter wrapper).
    let term = '';
    let rest = '';
    for (const it of items || []) {
      if (it && it.type === 'wrapper' && it.subtype === 'chapter') {
        const r = walk(it.content);
        term = term || r.term;
        rest += r.rest;
      } else if (it && it.type === 'wrapper' && it.subtype === 'usfm:k' && !term) {
        term = plain(it.content).trim();
      } else if (typeof it === 'string') rest += it;
      else if (it && it.type === 'wrapper') rest += plain(it.content);
    }
    return { term, rest };
  };
  for (const b of doc.sequence?.blocks || []) {
    if (b.type !== 'paragraph') continue;
    const { term, rest } = walk(b.content);
    const def = rest.replace(/\s+/g, ' ').trim();
    if (term) {
      out[term] = def;
      last = term;
    } else if (last && def) {
      out[last] = out[last] ? `${out[last]} ${def}` : def;
    }
  }
  return out;
}

function thaw(file) {
  const pk = new SAB();
  pk.loadSuccinctDocSet(JSON.parse(strFromU8(decompressSync(new Uint8Array(readFileSync(file))))));
  return pk;
}

/** Export one pkf under outDir. Returns {books, chapters, fallback, written, glossary}. */
function exportPkf(file, outDir) {
  const pk = thaw(file);
  const ds = pk.gqlQuerySync('{docSets{id}}').data.docSets[0].id;
  const docs = pk.gqlQuerySync(
    `{docSet(id:"${ds}"){documents{bookCode:header(id:"bookCode") cvIndexes{chapter}}}}`,
  ).data.docSet.documents;
  const res = { books: 0, chapters: 0, fallback: 0, written: 0, glossary: null };
  const sofriaOf = (book, ch) => {
    const arg = ch == null ? '' : `(chapter:${ch})`;
    const err = console.error;
    console.error = () => {};   // proskomma logs a stack for every chapter it can't render
    try {
      const raw = pk.gqlQuerySync(
        `{docSet(id:"${ds}"){document(bookCode:"${book}"){sofria${arg}}}}`,
      ).data?.docSet?.document?.sofria;
      return raw || null;
    } catch {
      return null;
    } finally {
      console.error = err;
    }
  };
  for (const d of docs) {
    const book = d.bookCode;
    if (!/^[0-9A-Z]{3}$/.test(book)) continue;
    const chapters = [...new Set(d.cvIndexes.map((c) => c.chapter))].filter((c) => c > 0).sort((a, b) => a - b);
    let whole = null;
    const wholeDoc = () => {
      if (!whole) {
        const raw = sofriaOf(book, null);
        if (!raw) throw new Error(`no Sofria for ${book}`);
        whole = JSON.parse(raw);
      }
      return whole;
    };
    res.books++;
    // A book with no chapters (peripheral/front matter) is published as chapter 1.
    for (const ch of chapters.length ? chapters : [1]) {
      let text;
      const raw = chapters.length ? sofriaOf(book, ch) : null;
      if (raw) {
        text = JSON.stringify(JSON.parse(raw));
      } else if (chapters.length) {
        const doc = sliceChapter(wholeDoc(), ch);
        if (!doc.sequence.blocks.length) continue;
        doc.fallback = 'whole-book';
        text = JSON.stringify(doc);
        res.fallback++;
      } else {
        text = JSON.stringify(wholeDoc());
      }
      if (writeIfChanged(join(outDir, book, `${ch}.sofria.json`), text)) res.written++;
      res.chapters++;
    }
    if (book === 'GLO') {
      const g = glossaryFrom(wholeDoc());
      if (Object.keys(g).length) res.glossary = g;
    }
  }
  return res;
}

function parallel(isos, jobs, force) {
  const lanes = Array.from({ length: jobs }, () => []);
  isos.forEach((iso, i) => lanes[i % jobs].push(iso));
  let bad = 0, open = 0;
  for (const lane of lanes.filter((l) => l.length)) {
    open++;
    const c = spawn(process.execPath, [fileURLToPath(import.meta.url), ...(force ? ['--force'] : []), ...lane], { stdio: 'inherit' });
    c.on('exit', (code) => { if (code) bad++; if (--open === 0) process.exit(bad ? 1 : 0); });
  }
}

function main() {
  const args = process.argv.slice(2);
  const force = args.includes('--force');
  const jobsArg = args.find((a) => a.startsWith('--jobs='));
  const wanted = args.filter((a) => !a.startsWith('--'));
  const excludedIsos = readSet(join(ROOT, 'EXCLUDED_ISOS.txt'));
  const excludedPkgs = readSet(join(ROOT, 'EXCLUDED_PACKAGES.txt'));
  const isos = (wanted.length ? wanted : readdirSync(POOL).filter((n) => !n.startsWith('_')))
    .filter((n) => existsSync(join(POOL, n, 'info.json')))
    .sort();
  const jobs = jobsArg ? Number(jobsArg.slice(7)) : 1;
  if (jobs > 1 && isos.length > 1) return parallel(isos, Math.min(jobs, isos.length), force);
  let done = 0, skipped = 0, chapters = 0, fallback = 0, failed = 0;
  for (const iso of isos) {
    const dir = join(POOL, iso);
    const infoPath = join(dir, 'info.json');
    const info = JSON.parse(readFileSync(infoPath, 'utf8'));
    const pkfs = (info.assets || [])
      .filter((a) => a.kind === 'pkf' && existsSync(join(dir, a.name)))
      .filter((a) => !excludedPkgs.has(a.base || a.name.split('.')[0]));
    const sofriaDir = join(dir, 'sofria');
    if (excludedIsos.has(iso) || !pkfs.length) {
      // never leave an exported tree behind for an ISO that must not ship
      if (existsSync(sofriaDir)) rmSync(sofriaDir, { recursive: true });
      if (existsSync(join(dir, 'glossary.json'))) rmSync(join(dir, 'glossary.json'));
      if (info.formats?.sofria || info.glossary) {
        delete info.formats?.sofria;
        if (info.formats && !Object.keys(info.formats).length) delete info.formats;
        delete info.glossary;
        writeFileSync(infoPath, JSON.stringify(info, null, 2));
      }
      continue;
    }
    const stamp = pkfs.map((a) => a.name).sort().join('\n');
    const stampPath = join(sofriaDir, '.source');
    if (!force && existsSync(stampPath) && readFileSync(stampPath, 'utf8') === stamp
        && info.formats?.sofria?.path === SOFRIA_PATH) {
      skipped++;
      continue;
    }
    // own collection first: <iso>_*; companions (e.g. fra_C02) get their own subdir
    const own = pkfs.find((a) => (a.base || a.name.split('.')[0]).startsWith(`${iso}_`)) || pkfs[0];
    if (existsSync(sofriaDir)) rmSync(sofriaDir, { recursive: true });
    const companions = {};
    let glossary = null;
    try {
      for (const a of pkfs) {
        const base = a.base || a.name.split('.')[0];
        const isOwn = a === own;
        const out = isOwn ? sofriaDir : join(sofriaDir, base);
        const r = exportPkf(join(dir, a.name), out);
        chapters += r.chapters;
        fallback += r.fallback;
        if (!isOwn) companions[base] = `sofria/${base}/<BOOK>/<chapter>.sofria.json`;
        if (isOwn && r.glossary) glossary = r.glossary;
      }
    } catch (e) {
      failed++;
      console.error(`[sofria] ${iso}: ${e.message}`);
      if (existsSync(sofriaDir)) rmSync(sofriaDir, { recursive: true });
      continue;
    }
    writeFileSync(stampPath, stamp);
    const gPath = join(dir, 'glossary.json');
    if (glossary) writeIfChanged(gPath, JSON.stringify(glossary));
    else if (existsSync(gPath)) rmSync(gPath);
    info.formats = {
      ...(info.formats || {}),
      sofria: {
        path: SOFRIA_PATH,
        granularity: 'chapter',
        ...(Object.keys(companions).length ? { collections: companions } : {}),
      },
    };
    if (glossary) info.glossary = 'glossary.json';
    else delete info.glossary;
    writeIfChanged(infoPath, JSON.stringify(info, null, 2));
    done++;
    console.log(`[sofria] ${iso}: ok${glossary ? ` · glossary ${Object.keys(glossary).length}` : ''}`);
  }
  console.log(`[sofria] ${done} exported · ${skipped} up to date · ${chapters} chapters · ${fallback} via sliceChapter · ${failed} failed`);
  if (failed) process.exit(1);
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) main();
