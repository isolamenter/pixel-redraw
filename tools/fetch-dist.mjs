/**
 * Build tools/pyodide-dist/: a self-contained Pyodide runtime for local testing.
 *
 * The deployed page loads Pyodide from jsDelivr, so this is not needed to run
 * the app.  It exists because Node cannot resolve a remote indexURL for its own
 * module imports, so the Node smoke test needs the runtime on disk.  It doubles
 * as the answer to "how big would self-hosting be, and does it work offline":
 * the total is ~17MB, most of it the interpreter itself.
 *
 * Usage:  npm install && npm run dist
 */
import { cpSync, existsSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

const PYODIDE_VERSION = '314.0.7';
const CDN = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;
// Only what this app actually imports.  Adding a package here means fetching
// its wheel too; the lock file names the exact file for the running release.
const PACKAGES = ['pillow', 'numpy'];

const here = fileURLToPath(new URL('.', import.meta.url));
const source = join(here, 'node_modules', 'pyodide');
const target = join(here, '..', 'static', 'pyodide');
const testDist = join(here, 'pyodide-dist');

if (!existsSync(source)) {
  console.error(
    `node_modules/pyodide is missing. Run:  npm install pyodide@${PYODIDE_VERSION}`,
  );
  process.exit(1);
}

rmSync(target, { recursive: true, force: true });
cpSync(source, target, { recursive: true });

const lock = JSON.parse(
  await (await import('node:fs/promises')).readFile(join(target, 'pyodide-lock.json'), 'utf8'),
);

for (const name of PACKAGES) {
  const entry = lock.packages[name];
  if (!entry) {
    console.error(`package ${name} is not in the ${PYODIDE_VERSION} lock file`);
    process.exit(1);
  }
  const file = entry.file_name;
  const response = await fetch(CDN + file);
  if (!response.ok) {
    console.error(`failed to fetch ${file}: HTTP ${response.status}`);
    process.exit(1);
  }
  const bytes = new Uint8Array(await response.arrayBuffer());
  mkdirSync(target, { recursive: true });
  writeFileSync(join(target, file), bytes);
  console.log(`fetched ${file} (${(bytes.length / 1024 / 1024).toFixed(1)} MB)`);
}

rmSync(testDist, { recursive: true, force: true });
cpSync(target, testDist, { recursive: true });

console.log(`\nstatic/pyodide/ and tools/pyodide-dist/ ready for Pyodide ${PYODIDE_VERSION} with ${PACKAGES.join(', ')}`);
