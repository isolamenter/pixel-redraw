/**
 * Local dev server for the static site.
 *
 * Serves the repository root, so the same absolute paths work as in the nginx
 * image: open http://127.0.0.1:8137/static/index.html and the page finds
 * /pixel_redraw.py, /pixel_palettes.py and /pixel_pipeline.py at the root.
 *
 * It sends `Cache-Control: no-store` for our own files, which plain
 * `python3 -m http.server` does not. That matters more than it sounds: without
 * it a browser will happily keep serving the previous version of a module, and
 * you end up debugging code you already fixed. Requests to the CDN are
 * unaffected, so the Pyodide cache still works.
 *
 * Usage: node tools/serve.mjs [port]     (default 8137)
 */
import { createServer } from 'node:http';
import { readFile, stat } from 'node:fs/promises';
import { extname, join, normalize, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = resolve(fileURLToPath(new URL('..', import.meta.url)));
const PORT = Number(process.argv[2]) || 8137;

const TYPES = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.mjs': 'text/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.py': 'text/x-python; charset=utf-8',
  '.wasm': 'application/wasm',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
};

const server = createServer(async (request, response) => {
  const url = new URL(request.url, 'http://localhost');
  let pathname = decodeURIComponent(url.pathname);
  if (pathname === '/') pathname = '/static/index.html';

  // Refuse anything that escapes the repository root.
  const target = join(ROOT, normalize(pathname).replace(/^(\.\.[/\\])+/, ''));
  if (!target.startsWith(ROOT)) {
    response.writeHead(403).end('forbidden');
    return;
  }

  try {
    const info = await stat(target);
    if (info.isDirectory()) {
      response.writeHead(404).end('not found');
      return;
    }
    const body = await readFile(target);
    response.writeHead(200, {
      'Content-Type': TYPES[extname(target).toLowerCase()] || 'application/octet-stream',
      'Content-Length': body.length,
      'Cache-Control': 'no-store',
    });
    response.end(body);
  } catch (error) {
    response.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' }).end('not found');
  }
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`pixel-redraw dev server: http://127.0.0.1:${PORT}/static/index.html`);
  console.log('serving the repository root; our own assets are no-store so edits always take effect');
});
