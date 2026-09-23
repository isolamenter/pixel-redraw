/**
 * Run the pixel-redraw core under Pyodide in Node, and report what it produced.
 *
 * This is the check that decides whether reusing the Python core in the browser
 * is viable at all: the browser gets Pillow 12.2.0 compiled to wasm32, while
 * the unit tests run against a native Pillow.  The quantization step is the
 * product, so if it behaved differently under wasm the whole approach would be
 * wrong -- better to learn that here than in a user's tab.
 *
 * It loads the runtime from tools/pyodide-dist/ (a local copy of the same
 * release the deployed page fetches from the CDN), because Node cannot resolve
 * a remote indexURL for its own module imports.  Run `npm run dist` first.
 *
 * Usage: node tools/smoke.mjs
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

// A plain filesystem path, not a file:// URL: Pyodide concatenates indexURL
// onto 'pyodide.asm.mjs' and hands the result to import(), which wants a path.
const PYODIDE_INDEX_URL = fileURLToPath(new URL('./pyodide-dist/', import.meta.url));
const { loadPyodide } = await import(new URL('./pyodide-dist/pyodide.mjs', import.meta.url));
const repoRoot = new URL('..', import.meta.url);
const read = (name) => readFileSync(new URL(name, repoRoot), 'utf8');

const pyodide = await loadPyodide({ indexURL: PYODIDE_INDEX_URL });
await pyodide.loadPackage(['pillow', 'numpy']);

pyodide.FS.writeFile('/pixel_redraw.py', read('pixel_redraw.py'));
pyodide.FS.writeFile('/pixel_palettes.py', read('pixel_palettes.py'));
pyodide.FS.writeFile('/pixel_pipeline.py', read('pixel_pipeline.py'));
pyodide.FS.writeFile('/pixel_color.py', read('pixel_color.py'));
pyodide.FS.writeFile('/pixel_reduce.py', read('pixel_reduce.py'));
pyodide.FS.mkdir('/tools');
pyodide.FS.writeFile('/tools/digest.py', read('tools/digest.py'));

// A real JS callback, not a Python stand-in: the browser hands Python a
// function across the boundary, and that marshalling is worth exercising here.
const progressEvents = [];
pyodide.globals.set('js_progress', (phase, label, detail) => {
  progressEvents.push({ phase, label, detail });
});

const result = await pyodide.runPythonAsync(`
import base64, importlib.util, io, json, sys
sys.path.insert(0, "/")
spec = importlib.util.spec_from_file_location("digest", "/tools/digest.py")
digest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(digest)

report = await digest.main()
import PIL, numpy
report["pillow"] = PIL.__version__
report["numpy"] = numpy.__version__
report["python"] = sys.version.split()[0]
report["platform"] = sys.platform

# --- the browser entry point, driven exactly the way the worker will ---
import pixel_pipeline
from pixel_pipeline import web_meta, run_pipeline

meta = json.loads(web_meta())
report["meta_presets"] = len(meta["presets"])
report["meta_sizes"] = meta["sizes"]

source_b64 = base64.b64encode(digest.fixture()).decode("ascii")
request = {
    "image": source_b64,
    "filename": "fixture.png",
    "size": 16,
    "max_colors": 8,
    "palette": {"preset": "pico8"},
    "pixelize_only": True,
}
envelope = json.loads(await run_pipeline(source_b64, json.dumps(request), js_progress))
report["pipeline_ok"] = envelope["ok"]
report["pipeline_error"] = (envelope.get("error") or {}).get("message") if not envelope["ok"] else None

result = envelope.get("result") or {}
report["pipeline_has_pixel_png"] = bool(result.get("pixel_png"))
if result:
    from PIL import Image
    image = Image.open(io.BytesIO(base64.b64decode(result["pixel_png"]))).convert("RGB")
    report["pipeline_size"] = list(image.size)
    pico8 = {tuple(int(c[i:i+2], 16) for i in (1, 3, 5))
             for c in next(p for p in meta["presets"] if p["id"] == "pico8")["colors"]}
    present = {rgb for _, rgb in image.getcolors(maxcolors=1 << 20)}
    report["pipeline_palette_only"] = present <= pico8

# Hand the same inputs to the JS side for the bridge check below. Plain
# assignments: runPythonAsync evaluates in __main__, so pyodide.globals sees them.
pr_source_b64 = source_b64
pr_request_json = json.dumps(request)

json.dumps(report, sort_keys=True)
`);

const report = JSON.parse(result);
const failures = [];

/* --- the exact bridge shape worker.js uses -------------------------------
   JS gets a Python coroutine function out of globals, awaits it, and passes a
   JS callback in. This is the one interop pattern the whole app rests on, so it
   is checked here rather than discovered in a browser. */
await pyodide.runPythonAsync(`
import pixel_pipeline
async def __pr_run(source_b64, request_json, on_progress):
    return await pixel_pipeline.run_pipeline(source_b64, request_json, on_progress)
`);

const bridgeEvents = [];
const bridge = pyodide.globals.get('__pr_run');
const bridgeSource = pyodide.globals.get('pr_source_b64');
const bridgeRequest = pyodide.globals.get('pr_request_json');
const bridgeEnvelope = JSON.parse(
  await bridge(bridgeSource, bridgeRequest, (phase) => bridgeEvents.push(phase)),
);
bridge.destroy();

if (!bridgeEnvelope.ok) {
  failures.push(`JS-awaiting-Python bridge failed: ${bridgeEnvelope.error?.message}`);
}
if (!bridgeEvents.includes('done')) {
  failures.push(`bridge progress callback saw no "done": ${bridgeEvents.join(',')}`);
}
report.bridge_ok = bridgeEnvelope.ok;
report.bridge_phases = bridgeEvents.length;
console.log(JSON.stringify(report, null, 2, true));

if (report.platform !== 'emscripten') failures.push(`not wasm: ${report.platform}`);
if (!report.nearest_palette_fast_path_matches_loop) {
  failures.push('numpy fast path diverges from the pure-Python reference under wasm');
}
if (!report.auto_mediancut?.sha256) failures.push('MEDIANCUT produced nothing');
if (!report.fixed_palette?.sha256) failures.push('fixed-palette path produced nothing');

// The output grid is density-relative: 16/256 of a 64px source is 4x4.
if (report.auto_mediancut?.size?.join('x') !== '4x4') {
  failures.push(`unexpected output size ${report.auto_mediancut?.size}`);
}

// --- the browser path: the pipeline entry point the worker calls ---
if (!report.pipeline_ok) failures.push(`run_pipeline failed: ${report.pipeline_error}`);
if (!report.pipeline_has_pixel_png) failures.push('run_pipeline returned no pixel PNG');
if (!report.pipeline_palette_only) {
  failures.push('output escaped the pico8 preset after pipeline palette resolution');
}
if (report.meta_sizes?.join(',') !== '8,16,32,64,128,256,512') {
  failures.push(`unexpected meta sizes: ${report.meta_sizes}`);
}
const phasesSeen = progressEvents.map((event) => event.phase);
if (!phasesSeen.includes('done')) {
  failures.push(`JS progress callback never saw "done": ${phasesSeen.join(',')}`);
}

if (failures.length) {
  console.error('\nFAILED:\n  - ' + failures.join('\n  - '));
  process.exit(1);
}
console.log('\nOK: the core runs under Pyodide/wasm and the quantizers agree.');
