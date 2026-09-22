/**
 * Link and evaluate the frontend module graph without a browser.
 *
 * The UI was split from one 2000-line IIFE into ten ES modules whose imports are
 * explicit. A name imported from the wrong module — or not exported at all — is
 * a hard failure in the browser with a message most people never see, and it
 * takes the whole page down. Node reports it precisely, and does so at *link*
 * time, before any of the DOM-dependent code runs.
 *
 * The DOM is stubbed rather than emulated on purpose: this checks the module
 * graph, not the behaviour. Behaviour is checked in a real browser (see README).
 *
 * Usage: node tools/linkcheck.mjs
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { join } from 'node:path';

const root = fileURLToPath(new URL('..', import.meta.url));

function elementStub() {
  const node = {
    style: {}, dataset: {}, hidden: false, disabled: false, value: '', type: '',
    textContent: '', className: '', firstChild: null,
    classList: { add() {}, remove() {}, contains() { return false; }, toggle() {} },
    appendChild() {}, removeChild() {}, insertBefore() {}, remove() {},
    setAttribute() {}, removeAttribute() {}, addEventListener() {},
    querySelector: () => null, querySelectorAll: () => [],
    getBoundingClientRect: () => ({ top: 0, bottom: 0, left: 0, right: 0, width: 0, height: 0 }),
    scrollIntoView() {}, focus() {}, click() {},
    getContext: () => null, toBlob() {},
  };
  return node;
}

globalThis.document = {
  querySelector: () => elementStub(),
  createElement: () => elementStub(),
  createTextNode: () => ({}),
  body: elementStub(),
};
globalThis.window = { addEventListener() {}, isSecureContext: false, innerHeight: 900, devicePixelRatio: 1 };
globalThis.localStorage = { getItem: () => null, setItem() {}, removeItem() {} };
globalThis.sessionStorage = { getItem: () => null, setItem() {}, removeItem() {} };
/* No Worker in Node. bootWorker() catches this and reports it through the same
   error path the page uses, which is also worth exercising. */
globalThis.Worker = class {
  constructor() { throw new Error('Worker is unavailable outside a browser'); }
};

/* The modules read DOM ids at import time, so the HTML has to be checked too:
   a stale id in a module is silent in the browser (the lookup returns null) and
   only shows up as a control that does nothing. */
const html = readFileSync(join(root, 'static', 'index.html'), 'utf8');
const ids = new Set([...html.matchAll(/id="([^"]+)"/g)].map((m) => m[1]));

let failed = false;
const jsDir = join(root, 'static', 'js');
const { readdirSync } = await import('node:fs');
for (const name of readdirSync(jsDir).filter((f) => f.endsWith('.js') && f !== 'worker.js')) {
  const source = readFileSync(join(jsDir, name), 'utf8');
  for (const match of source.matchAll(/\$\("#([\w-]+)"\)/g)) {
    if (!ids.has(match[1])) {
      console.error(`FAIL: ${name} queries #${match[1]}, which is not in index.html`);
      failed = true;
    }
  }
}

try {
  await import(join(jsDir, 'app.js'));
  console.log('OK: every module linked, app.js evaluated, and every $("#id") resolves');
} catch (error) {
  console.error(`FAIL: ${error.constructor.name}\n${String(error.message).split('\n').slice(0, 8).join('\n')}`);
  failed = true;
}

/* boot() starts a UI timer that would otherwise hold the event loop open. */
process.exit(failed ? 1 : 0);
