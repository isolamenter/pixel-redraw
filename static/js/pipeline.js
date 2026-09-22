/* 编排：把一次运行从「用户点了生成」送到「结果画在屏幕上」。
 *
 * 这里替代的是过去的整条运行生命周期 —— POST /api/generate、SSE 事件流、
 * /state 轮询、run_id 重挂。那套东西存在的唯一理由是任务活在一个别的进程里；
 * 现在任务就在这个页面旁边的 Worker 里，一次 await 就结束了它。
 *
 * 与 Worker 的契约（见 worker.js）：
 *   → {type:'boot', indexURL, pythonFiles}
 *   ← {type:'ready', meta, numpy} | {type:'boot-failed', ...}
 *   → {type:'run', id, sourceB64, requestJson}
 *   ← {type:'progress', id, phase, label, detail}
 *   ← {type:'done', id, envelope}
 */
import { PYODIDE_INDEX_URL, PHASE_ZH } from './config.js';
import { S } from './state.js';
import { $, addTimeline, b64ToBlob, clear, el, note, num } from './dom.js';
import { pushError, report } from './errors.js';
import { cancelBtn, indetEl, indetLbl, renderStepper, setStatus, updateGenerateEnabled, upbarEl } from './stepper.js';
import { paletteForRequest } from './palette.js';
import { blobToBase64 } from './image.js';
import { finishRun, renderIntermediate, revokeResultUrls } from './results.js';
import { settings, upstreamBlock, upstreamReady } from './settings.js';

/* 运行时要加载的 Python 源码。零构建：这些文件由站点当静态文件发出，
   启动时读进 WASM 文件系统再 import —— 磁盘上的 .py 就是唯一一份，
   不打包、不转译、不复制。

   路径写成站点根下的绝对路径，这样两种布局都能用：
     · 部署（nginx 镜像）：web 根就是站点根，index.html 在 /，三个 .py 也在 /；
     · 本地开发（仓库根起 http.server）：访问 /static/index.html，而 /pixel_redraw.py
       正好落在仓库根上，两份文件是同一次读取。
   相对的 worker 路径不受影响：它是相对文档 URL 解析的。 */
var PYTHON_FILES = ["/pixel_redraw.py", "/pixel_palettes.py", "/pixel_pipeline.py"];

/* 冷启动要下约 7.5MB，分步显示比一个不动的「加载中」诚实得多。 */
export var BOOT_STEP_TEXT = {
  sources: "正在读取 Python 源码…",
  interpreter: "正在下载并启动 Pyodide 解释器（约 5.8MB）…",
  pillow: "正在加载 Pillow（约 1MB）…",
  numpy: "正在加载 numpy（约 2.9MB）…",
  python: "正在初始化像素化模块…"
};

async function fetchPythonSources() {
  var sources = {};
  for (var i = 0; i < PYTHON_FILES.length; i++) {
    var path = PYTHON_FILES[i];
    var response = await fetch(path, { cache: "no-cache" });
    if (!response.ok) {
      throw new Error("无法读取 " + path + "（HTTP " + response.status + "）");
    }
    sources[path.replace(/^\//, "")] = await response.text();
  }
  return sources;
}

export async function bootWorker() {
  if (S.worker) return;
  S.bootState = "loading";
  var worker;
  try {
    worker = new Worker("./js/worker.js", { type: "module" });
  } catch (error) {
    failBoot("无法创建 Worker：" + String((error && error.message) || error));
    return;
  }
  S.worker = worker;

  worker.onmessage = function (event) {
    var message = event.data || {};
    if (message.type === "boot-progress") {
      S.bootStep = message.step;
      setStatus("busy", BOOT_STEP_TEXT[message.step] || "正在加载运行时…");
      return;
    }
    if (message.type === "ready") {
      S.bootState = "ready";
      S.numpyOk = !!message.numpy;
      applyRuntimeMeta(message.meta);
      if (!S.numpyOk) {
        pushError({
          kind: "frontend", where: "worker.boot", phase: "received",
          message: "numpy 未能加载；固定调色板 + 高密度输出会非常慢。",
          hint: "纯 Python 的最近色映射在 64 色下约 33µs/像素，一次两轮运行可能从几秒变成几分钟。" +
                "自动调色板（auto）不受影响。若内网访问不到 CDN 的 numpy wheel，把 " +
                "PYODIDE_INDEX_URL 指向自托管副本即可（见 README）。"
        }, { source: "runtime" });
      }
      return;
    }
    if (message.type === "boot-failed") {
      failBoot(message.message, message.detail, message.indexURL);
      return;
    }
    if (message.type === "progress") {
      if (message.id !== S.runSeq) return;      // 旧运行的回包，丢弃
      onProgress(message.phase, message.label, message.detail);
      return;
    }
    if (message.type === "done") {
      if (message.id !== S.runSeq) return;
      onDone(message.envelope);
    }
  };

  worker.onerror = function (event) {
    failBoot("Worker 启动失败：" + String((event && event.message) || event));
  };

  /* Python 源码由主线程取好再交给 Worker：Worker 里一切都要先过 Pyodide 的
     文件系统，而抓取失败必须能变成一条能读的错误，而不是一个悬着的 boot。 */
  var sources;
  try {
    setStatus("busy", BOOT_STEP_TEXT.sources);
    sources = await fetchPythonSources();
  } catch (error) {
    failBoot(String((error && error.message) || error), "", PYODIDE_INDEX_URL);
    return;
  }

  worker.postMessage({
    type: "boot",
    indexURL: PYODIDE_INDEX_URL,
    pythonFiles: sources
  });
}

function failBoot(message, detail, indexURL) {
  S.bootState = "failed";
  S.bootError = message;
  setStatus("err", "运行时加载失败");
  pushError({
    kind: "frontend", where: "worker.boot", phase: "received",
    message: String(message),
    detail: String(detail || ""),
    hint: "页面无法在没有运行时的情况下工作：像素化由 Pyodide（WASM 版 CPython）执行。" +
          "最常见的原因是这台机器访问不到 " + String(indexURL || PYODIDE_INDEX_URL) + "。" +
          "若内网不能出网，把 static/js/config.js 里的 PYODIDE_INDEX_URL 指向自托管副本" +
          "（约 17MB，见 README「离线部署」），其余代码不用改。"
  }, { source: "runtime" });
  updateGenerateEnabled();
}

/* 运行时上报的 meta 里，凡是用户可覆盖的都给控件兜底，其余照单全收。 */
function applyRuntimeMeta(meta) {
  onRuntimeMeta(meta);
}

var onRuntimeMeta = function () {};     // 由 app.js 覆盖，避免 pipeline ←→ app 的循环引用

export function setRuntimeMetaHandler(fn) {
  onRuntimeMeta = fn;
}

function onProgress(phase, label, detail) {
  if (phase && !S.phasesSeen[phase]) {
    S.phasesSeen[phase] = { elapsed_ms: S.runStartedAt ? Date.now() - S.runStartedAt : null };
  }
  S.currentPhase = phase || S.currentPhase;
  addTimeline("-", phase, label || (PHASE_ZH[phase] || phase), detail ? { detail: detail } : null,
    S.runStartedAt ? Date.now() - S.runStartedAt : null, phase === "done" ? "ok" : "info");
  renderStepper();

  if (phase === "upstream_wait" || phase === "refine_wait") {
    indetEl.hidden = false;
    indetLbl.hidden = false;
    setStatus("busy", label || "正在等待模型返回。");
  } else {
    indetEl.hidden = true;
    indetLbl.hidden = true;
    setStatus("open", label || "");
  }
  $("#phase-text").textContent = phase ? "当前阶段：" + phase + "（" + (PHASE_ZH[phase] || "—") + "）" : "";
}

function onDone(envelope) {
  S.running = false;
  S.elapsedMs = S.runStartedAt ? Date.now() - S.runStartedAt : 0;
  indetEl.hidden = true;
  indetLbl.hidden = true;
  upbarEl.hidden = true;
  cancelBtn.disabled = true;
  updateGenerateEnabled();
  renderStepper();

  if (!envelope || !envelope.ok) {
    var e = (envelope && envelope.error) || { kind: "internal", message: "运行时没有返回任何结果。" };
    S.failedPhase = e.phase || S.currentPhase;
    renderStepper();
    setStatus("err", "失败（" + (e.kind || "internal") + "）。");
    pushError(e, { source: S.lastResultSource === "repixelize" ? "repixelize" : "generate" });
    return;
  }

  var result = envelope.result;
  S.lastResultRawB64 = result.raw_png || null;
  revokeResultUrls();

  var urls = {
    pixel: URL.createObjectURL(b64ToBlob(result.pixel_png, "image/png")),
    preview: URL.createObjectURL(b64ToBlob(result.preview_png, "image/png")),
    raw: result.raw_png ? URL.createObjectURL(b64ToBlob(result.raw_png, result.raw_mime || "image/png")) : null,
    draft: null
  };
  S.objectUrls.push(urls.pixel, urls.preview);
  if (urls.raw) S.objectUrls.push(urls.raw);

  var container = {
    result: {
      size: result.report.size,
      scale: result.report.scale,
      palette_requested: result.report.palette_requested,
      palette_used: result.report.palette_used,
      color_count: result.report.color_count,
      has_raw: !!result.raw_png,
      refinement_applied: result.refinement_applied,
      urls: urls,
      report: result.report
    },
    sourceName: S.uploadName
  };
  finishRun(container);
  setStatus("open", result.refinement_applied
    ? "完成：两轮生成，且像素已量化到目标网格与调色板。"
    : "完成：像素已量化到目标网格与调色板。");

  if (!result.refinement_applied && settings.passes === 2 && !S.pixelizeOnly) {
    addTimeline("-", "note", "第二轮未生效（可能失败或未开启），本次结果是第一轮草稿量化后的样子。",
      null, null, "note");
  }
}

/* ---------------- 一次运行 ---------------- */

function baseRequest(pixelizeOnly, preserveClusters) {
  return {
    filename: S.uploadName,
    size: S.size,
    max_colors: S.maxColors,
    palette: paletteForRequest(),
    pixelize_only: !!pixelizeOnly,
    preserve_clusters: !!preserveClusters,
    passes: settings.passes,
    prompt: settings.prompt,
    refine_prompt: settings.refine_prompt,
    upstream: upstreamBlock()
  };
}

function beginRun(request, b64) {
  S.runSeq += 1;
  S.running = true;
  S.runStartedAt = Date.now();
  S.elapsedMs = 0;
  S.phasesSeen = {};
  S.currentPhase = null;
  S.failedPhase = null;
  updateGenerateEnabled();
  renderStepper();
  cancelBtn.disabled = false;
  indetEl.hidden = false;
  indetLbl.hidden = false;
  setStatus("busy", "已提交，等待运行时开始。");

  request.image = b64;
  S.worker.postMessage({
    type: "run",
    id: S.runSeq,
    sourceB64: b64,
    requestJson: JSON.stringify(request)
  });
}

export function startGenerate() {
  if (!S.uploadBlob || S.running) return;
  if (S.bootState !== "ready") {
    pushError({
      kind: "frontend", where: "startGenerate", phase: "received",
      message: "运行时尚未就绪（当前：" + S.bootState + "）。",
      hint: S.bootError || "等状态条显示运行时已就绪再点生成。"
    }, { source: "frontend" });
    return;
  }
  if (!S.pixelizeOnly && !upstreamReady()) {
    pushError({
      kind: "config", where: "startGenerate", phase: "received",
      message: "未填写模型名或 API key。",
      hint: "模型与 key 都在页面左侧的「凭据」里填。只想验证尺寸和调色板的话，勾上「仅本地渲染」，" +
            "那条路径完全在本地跑，不需要 key。"
    }, { source: "frontend" });
    return;
  }

  var maxBytes = (S.meta && S.meta.max_upload_bytes) || 12582912;
  blobToBase64(S.uploadBlob).then(function (b64) {
    if (b64.length > maxBytes) {
      pushError({
        kind: "limits", where: "client_precheck", phase: "received",
        message: "图片编码后 " + num(b64.length) + " 字节，超过上限 " + num(maxBytes) + " 字节。",
        hint: "请先缩小图片再重新选择文件。"
      }, { source: "frontend" });
      return;
    }
    resetRunUI();
    beginRun(baseRequest(S.pixelizeOnly, false), b64);
  }).catch(function (error) {
    report(error, "startGenerate");
  });
}

/* 零成本重渲染：对缓存的模型原始输出重新像素化，不调用模型、不花 token。
   改密度、换调色板、改色数上限都走这里。 */
export function scheduleRepixelize(reason) {
  if (S.repixelTimer) clearTimeout(S.repixelTimer);
  S.repixelTimer = setTimeout(function () { doRepixelize(reason); }, 250);
}

export function doRepixelize(reason) {
  if (!S.lastResultRawB64) {
    note("还没有模型输出可以重渲染：先跑一次生成。");
    return;
  }
  if (S.running) {
    note("有任务在跑，等它结束再重渲染。");
    return;
  }
  addTimeline("-", "repixelize", "本地重渲染（不调用模型）：" + (reason || ""), null, null, "info");
  S.lastResultSource = "repixelize";
  resetRunUI();
  beginRun(baseRequest(true, true), S.lastResultRawB64);
}

/* 开一次新的运行：时间线只保留本次运行，阶段表清空。
   注意这里不 revoke 结果图的 blob URL —— 上一次的结果在新图出来之前仍然显示着。 */
export function resetRunUI() {
  S.phasesSeen = {};
  S.currentPhase = null;
  S.failedPhase = null;
  S.lastResultSource = "generate";
  S.elapsedMs = 0;
  clear($("#timeline"));
  $("#timeline").appendChild(el("p", { class: "empty", text: "本次运行的事件时间线：" }));
  $("#phase-text").textContent = "";
  indetEl.hidden = true;
  indetLbl.hidden = true;
  upbarEl.hidden = true;
  renderIntermediate({ urls: { draft: null } });   // 上一次的中间图不能留在新一跑里
  addTimeline("-", "start", "开始运行",
    { size: S.size, palette: paletteForRequest(), max_colors: S.maxColors,
      pixelize_only: S.pixelizeOnly, model: settings.model || "(未填)" },
    null, "info");
  renderStepper();
}

/* 取消 = 终止 Worker 再重建。
   浏览器里没有别的办法真正打断一段同步的 WASM 计算：setInterruptBuffer 需要
   SharedArrayBuffer，而它需要 COOP/COEP 跨源隔离 —— 那套头又会挡住 CDN 上的
   Pyodide。代价是取消后要重新加载运行时（秒级）；好处是真的会停下来。 */
export function cancelRun() {
  if (!S.worker) return;
  S.runSeq += 1;                 // 让所有在途回包失效
  S.running = false;
  S.elapsedMs = S.runStartedAt ? Date.now() - S.runStartedAt : 0;
  try { S.worker.terminate(); } catch (e) {}
  S.worker = null;
  S.bootState = "loading";
  cancelBtn.disabled = true;
  indetEl.hidden = true;
  indetLbl.hidden = true;
  updateGenerateEnabled();
  setStatus("warn", "已终止本地计算，正在重新加载运行时…");
  addTimeline("-", "cancel", "已终止 Worker 并重新加载运行时", null, null, "note");
  bootWorker();
}
