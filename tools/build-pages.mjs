import { cp, mkdir, readdir, rm, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { dirname, resolve } from 'node:path';

const ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const OUTPUT = resolve(ROOT, 'dist');
const PYTHON_FILES = [
  'pixel_redraw.py',
  'pixel_palettes.py',
  'pixel_pipeline.py',
  'pixel_color.py',
  'pixel_reduce.py',
];

await rm(OUTPUT, { recursive: true, force: true });
await mkdir(OUTPUT, { recursive: true });
for (const entry of await readdir(resolve(ROOT, 'static'))) {
  await cp(resolve(ROOT, 'static', entry), resolve(OUTPUT, entry), { recursive: true });
}
for (const file of PYTHON_FILES) {
  await cp(resolve(ROOT, file), resolve(OUTPUT, file));
}
await writeFile(resolve(OUTPUT, '_routes.json'), JSON.stringify({
  version: 1,
  include: ['/api/*'],
  exclude: [],
}, null, 2) + '\n');

console.log(`Cloudflare Pages assets prepared at ${OUTPUT}`);
