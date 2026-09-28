/**
 * Prepare the minimal self-hosted Pyodide runtime for local use and Pages.
 * The browser smoke test loads the same files directly from static/pyodide/.
 *
 * Usage:  npm install && npm run dist
 */
import { cpSync, existsSync, mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

const PYODIDE_VERSION = '314.0.7';
const CDN = `https://cdn.jsdelivr.net/pyodide/v${PYODIDE_VERSION}/full/`;
const RUNTIME_FILES = [
  'pyodide.mjs',
  'pyodide.asm.mjs',
  'pyodide.asm.wasm',
  'python_stdlib.zip',
  'pyodide-lock.json',
];
// Only what this app actually imports.  Adding a package here means fetching
// its wheel too; the lock file names the exact file for the running release.
const PACKAGES = ['pillow', 'numpy'];

const here = fileURLToPath(new URL('.', import.meta.url));
const source = join(here, 'node_modules', 'pyodide');
const target = join(here, '..', 'static', 'pyodide');

if (!existsSync(source)) {
  console.error(
    `node_modules/pyodide is missing. Run:  npm install pyodide@${PYODIDE_VERSION}`,
  );
  process.exit(1);
}

rmSync(target, { recursive: true, force: true });
mkdirSync(target, { recursive: true });
for (const file of RUNTIME_FILES) {
  cpSync(join(source, file), join(target, file));
}

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

console.log(`\nstatic/pyodide/ ready for Pyodide ${PYODIDE_VERSION} with ${PACKAGES.join(', ')}`);
