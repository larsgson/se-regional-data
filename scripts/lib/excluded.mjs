/**
 * Shared loader for EXCLUDED_ISOS.txt — the canonical list of ISOs whose
 * upstream license does not permit us to redistribute. Both pack_release.mjs
 * (which strips them from the tarball + manifest) and diff_manifest.mjs
 * (which hides them from release notes) use this.
 *
 * File format: one ISO per line. Blank lines and `#` comments are ignored.
 * An inline `# …` comment after the ISO is allowed.
 */
import { readFileSync, existsSync } from 'node:fs';

const EXCLUDED_PATH = 'EXCLUDED_ISOS.txt';

export function loadExcludedIsos(path = EXCLUDED_PATH) {
    if (!existsSync(path)) return new Set();
    const out = new Set();
    for (const raw of readFileSync(path, 'utf8').split('\n')) {
        const line = raw.replace(/#.*$/, '').trim();
        if (!line) continue;
        const iso = line.split(/\s+/)[0];
        if (iso) out.add(iso);
    }
    return out;
}

export function filterManifest(manifest, excluded) {
    if (!excluded.size) return manifest;
    return {
        ...manifest,
        languages: (manifest.languages || []).filter((l) => !excluded.has(l.iso))
    };
}
