/**
 * Drive the real page in headless Chrome and run the free path end to end.
 *
 * This is the only check that exercises everything at once: ES modules, the
 * module Worker, the CDN Pyodide load, `web_meta()`, and a real pixelization by
 * Pillow compiled to wasm — with no API key and no model call, because
 * 「仅本地渲染」 needs neither.
 *
 * It is deliberately the slowest and most fragile check, so it is not part of
 * the default test run; run it when touching the worker, the bridge, or the
 * boot sequence.
 *
 * Usage: node tools/browser-smoke.mjs [url]     (default http://127.0.0.1:8137/static/index.html)
 */
import { spawn } from 'node:child_process';
import { setTimeout as sleep } from 'node:timers/promises';

const URL_UNDER_TEST = process.argv[2] || 'http://127.0.0.1:8137/static/index.html';
const PORT = 9333;
const CHROME = '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome';

/* Start on about:blank and navigate only after the DevTools session is attached
   and enabled. Passing the URL on the command line races the attach: the page can
   finish booting before we can observe it, and then the polling loop reports a
   boot that already happened. */
const chrome = spawn(CHROME, [
  '--headless=new', '--no-sandbox', '--disable-gpu',
  `--remote-debugging-port=${PORT}`,
  '--user-data-dir=/tmp/pr-browser-smoke-profile',
  'about:blank',
], { stdio: ['ignore', 'ignore', 'pipe'] });

let chromeStderr = '';
chrome.stderr.on('data', (chunk) => { chromeStderr += chunk; });

async function targetUrl() {
  for (let attempt = 0; attempt < 60; attempt += 1) {
    try {
      const list = await (await fetch(`http://127.0.0.1:${PORT}/json/list`)).json();
      const page = list.find((t) => t.type === 'page' && t.webSocketDebuggerUrl);
      if (page) return page.webSocketDebuggerUrl;
    } catch (error) { /* not up yet */ }
    await sleep(500);
  }
  throw new Error('Chrome DevTools endpoint never came up');
}

const ws = new WebSocket(await targetUrl());
await new Promise((resolve, reject) => {
  ws.onopen = resolve;
  ws.onerror = () => reject(new Error('could not attach to the page'));
});

let nextId = 1;
const pending = new Map();
const consoleErrors = [];
const exceptions = [];

ws.onmessage = (event) => {
  const message = JSON.parse(event.data);
  if (message.id && pending.has(message.id)) {
    pending.get(message.id)(message);
    pending.delete(message.id);
    return;
  }
  if (message.method === 'Runtime.exceptionThrown') {
    const d = message.params.exceptionDetails;
    exceptions.push(d.exception?.description || d.text);
  }
  if (message.method === 'Runtime.consoleAPICalled' && message.params.type === 'error') {
    consoleErrors.push(message.params.args.map((a) => a.value ?? a.description).join(' '));
  }
};

function send(method, params = {}) {
  const id = nextId++;
  return new Promise((resolve) => {
    pending.set(id, resolve);
    ws.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const reply = await send('Runtime.evaluate', {
    expression, awaitPromise: true, returnByValue: true,
  });
  if (reply.result?.exceptionDetails) {
    throw new Error(reply.result.exceptionDetails.exception?.description
      || reply.result.exceptionDetails.text);
  }
  return reply.result?.result?.value;
}

await send('Runtime.enable');
await send('Page.enable');
await send('Page.navigate', { url: URL_UNDER_TEST });

const failures = [];
const status = async () => evaluate("document.querySelector('#status-text').textContent");

/* Report where the runtime is actually coming from: config.js is the one line that
   switches between the CDN and a self-hosted copy, and this message used to claim
   "from CDN" even when it was testing the self-hosted path. */
const indexUrl = await evaluate(
  `(async () => (await import(new URL('js/config.js', document.baseURI).href)).PYODIDE_INDEX_URL)()`,
).catch(() => '(unknown)');
console.log(`waiting for the runtime to boot from ${indexUrl} (~7.5MB on a cold cache)…`);
let booted = false;
let lastStatus = '';
for (let attempt = 0; attempt < 300; attempt += 1) {
  const ready = await evaluate(
    "!!(document.querySelector('#sizes') && document.querySelector('#sizes').children.length > 0)");
  if (ready) { booted = true; break; }
  const now = await status();
  if (now && now !== lastStatus) {
    lastStatus = now;
    console.log(`  [${attempt}s] ${now}`);
  }
  await sleep(1000);
}
if (!booted) failures.push(`runtime never booted; status text was: ${await status()}`);

if (booted) {
  /* meta 驱动的控件：密度档位与调色板预设都是运行时给的，渲染出来就证明
     web_meta() 真的在 wasm 里跑过了。 */
  const sizes = await evaluate("document.querySelectorAll('#sizes input').length");
  const presets = await evaluate("document.querySelectorAll('#presets button').length");
  const version = await evaluate("document.querySelector('#chip-version b').textContent");
  console.log(`  sizes=${sizes} presets=${presets} version=${version}`);
  if (sizes !== 5) failures.push(`expected 5 density options, got ${sizes}`);
  if (presets < 15) failures.push(`expected the 15 presets plus the auto button, got ${presets}`);
  if (!version || version === '—') failures.push(`version chip never filled in: ${version}`);

  /* 一条真正的端到端本地渲染：造一张图丢进 drop 区，只跑本地 Pillow，不调用模型。 */
  console.log('  running a local-only pixelization (no key, no model call)…');
  /* 「仅本地渲染」必须先勾上：没有 key 时生成按钮是被这一项才放开的。 */
  await evaluate("document.querySelector('#pixelize-only').click()");
  await evaluate(`
    (async () => {
      const canvas = document.createElement('canvas');
      canvas.width = 128; canvas.height = 128;
      const ctx = canvas.getContext('2d');
      const grad = ctx.createLinearGradient(0, 0, 128, 128);
      grad.addColorStop(0, '#ff0000'); grad.addColorStop(1, '#0000ff');
      ctx.fillStyle = grad; ctx.fillRect(0, 0, 128, 128);
      const blob = await new Promise((r) => canvas.toBlob(r, 'image/png'));
      const file = new File([blob], 'smoke.png', { type: 'image/png' });
      const dt = new DataTransfer();
      dt.items.add(file);
      document.querySelector('#drop').dispatchEvent(
        new DragEvent('drop', { dataTransfer: dt, bubbles: true, cancelable: true }));
    })()
  `);

  for (let attempt = 0; attempt < 30; attempt += 1) {
    if (await evaluate("!document.querySelector('#generate').disabled")) break;
    await sleep(500);
  }
  const canGenerate = await evaluate("!document.querySelector('#generate').disabled");
  if (!canGenerate) {
    failures.push(`generate never enabled; reason shown: ${await evaluate("document.querySelector('#generate-why').textContent")}`);
  } else {
    await evaluate("document.querySelector('#generate').click()");
    /* Wait for the decoded pixels, not just the element: renderResult appends the
       <img> synchronously and fills its src from a blob promise a moment later, so
       reading naturalWidth too early reports 0x0 on a perfectly good result. */
    let done = false;
    for (let attempt = 0; attempt < 120; attempt += 1) {
      done = await evaluate(`(function(){
        var img = document.querySelector('#stage-pixel img');
        return !!(img && img.complete && img.naturalWidth > 0);
      })()`);
      if (done) break;
      await sleep(1000);
    }
    if (!done) {
      failures.push(`local render produced no image; status: ${await status()}`);
      const dump = await evaluate(`(async () => {
        var img = document.querySelector('#stage-pixel img');
        var out = { hasImg: !!img, src: img ? String(img.src).slice(0, 80) : null };
        if (img && img.src) {
          try { var r = await fetch(img.src); out.fetchStatus = r.status; out.blobSize = (await r.blob()).size; }
          catch (e) { out.fetchError = String(e); }
        }
        out.consoleText = document.querySelector('#console').textContent.slice(0, 600);
        return out;
      })()`);
      console.log('  diagnostic: ' + JSON.stringify(dump, null, 2));
    } else {
      const result = await evaluate(`(function(){
        var img = document.querySelector('#stage-pixel img');
        return { width: img.naturalWidth, height: img.naturalHeight,
                 caption: document.querySelector('#cap-logical').textContent,
                 errors: document.querySelectorAll('.errblock').length,
                 src: String(img.src || '').slice(0, 90),
                 complete: img.complete };
      })()`);
      console.log(`  result: ${result.width}x${result.height} — ${result.caption}`);
      if (!result.width) {
        const probe = await evaluate(`(async () => {
          var img = document.querySelector('#stage-pixel img');
          var out = { src: String(img.src || '').slice(0, 80) };
          try { var r = await fetch(img.src); out.fetchStatus = r.status; out.size = (await r.blob()).size; }
          catch (e) { out.fetchError = String(e); }
          out.consoleText = document.querySelector('#console').textContent.slice(0, 400);
          return JSON.stringify(out);
        })()`);
        console.log('  probe: ' + probe);
      }
      /* 128px 源图在 32 密度（256 基准）下应得 16x16 逻辑网格 */
      if (result.width !== 16 || result.height !== 16) {
        failures.push(`expected a 16x16 logical grid for a 128px source at density 32, got ${result.width}x${result.height}`);
      }
      if (result.errors > 0) failures.push(`${result.errors} error block(s) in the console`);
      const downloadEnabled = await evaluate("!document.querySelector('#download-pixel').classList.contains('is-disabled')");
      if (!downloadEnabled) failures.push('download link was not enabled for the result');
    }
  }
}

if (exceptions.length) failures.push(`uncaught exceptions: ${exceptions.slice(0, 3).join(' | ')}`);
if (consoleErrors.length) failures.push(`console errors: ${consoleErrors.slice(0, 3).join(' | ')}`);

ws.close();
chrome.kill();

if (failures.length) {
  console.error('\nFAILED:\n  - ' + failures.join('\n  - '));
  if (chromeStderr.trim()) console.error('\nchrome stderr tail:\n' + chromeStderr.split('\n').slice(-8).join('\n'));
  process.exit(1);
}
console.log('\nOK: the page booted the runtime from the CDN and rendered a real pixelized image.');
