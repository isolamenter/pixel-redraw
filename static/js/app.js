/* 页面入口：装配所有模块、把运行时元数据落到控件上、接好事件。
 *
 * 这个文件替代了过去的 boot() + applyMeta() + reattach()。其中 reattach 整条
 * 消失是有意的：它存在的理由是「任务活在服务端，刷新页面要重新挂上去」；
 * 现在任务就在这个标签页里，刷新即重置，没有需要重新挂载的东西。 */
import { PYODIDE_INDEX_URL } from './config.js';
import { S } from './state.js';
import { $, addTimeline, el, json, note, safeStr } from './dom.js';
import { pushError, report } from './errors.js';
import {
  cancelBtn, describeConnection, renderStepper, setStatus, tickUI, updateGenerateEnabled,
} from './stepper.js';
import { renderMaxColorChoices, renderPresets, renderSizes, syncPaletteUI } from './palette.js';
import { wireImageInput } from './image.js';
import { applyDefaultZoom, applyIntermediateZoom, applyZoom, initCopy } from './results.js';
import {
  clearStoredKey, knownModelsDatalist, loadSettings, settings, syncSettingsUI,
  upstreamHost, upstreamReady, wireSettingsUI,
} from './settings.js';
import {
  bootWorker, cancelRun, doRepixelize, scheduleRepixelize, setRuntimeMetaHandler, startGenerate,
} from './pipeline.js';

/* 状态条：版本、模型、端点。
   以前这三格都由服务端上报；现在模型与端点是用户自己填的，所以它们必须随设置变化
   而重画，而不是在启动时写死一次。 */
function refreshChips() {
  var meta = S.meta || {};
  var version = $("#chip-version");
  version.textContent = "版本 ";
  version.appendChild(el("b", { text: meta.version || "—" }));

  var model = $("#chip-model");
  model.textContent = "模型 ";
  model.appendChild(el("b", { text: settings.model || "未填" }));

  var upstream = $("#chip-upstream");
  var ready = upstreamReady();
  var host = upstreamHost(meta);
  upstream.textContent = "端点 ";
  upstream.appendChild(el("b", { text: ready ? (host || "—") : "未就绪" }));
  upstream.title = ready
    ? "请求将直接发往 " + (host || "") + "（由本页发出，不经过任何服务器）"
    : "填上模型名与 API key 后，请求会从本页直接发往端点";
  upstream.style.borderColor = ready ? "" : "var(--warn)";
  upstream.style.color = ready ? "" : "var(--warn)";

  var keyState = $("#cfg-key-state");
  if (keyState) {
    keyState.textContent = settings.api_key
      ? (settings.remember ? "已填入，并保存在本机 localStorage" : "已填入，仅本次会话有效")
      : "未填写";
  }
}

/* 运行时上报的 meta：密度档位、色数、调色板预设、各项上限。
   这些是程序的属性，所以仍然由运行时说了算；浏览器语言环境相关的只做兜底。 */
export function applyMeta(meta) {
  S.meta = meta;
  S.size = meta.default_size || 32;
  S.preset = null;
  S.paletteMode = "auto";
  S.maxColors = meta.default_max_colors || 16;
  knownModelsDatalist(meta);
  if (!settings.model && (meta.known_models || []).length) {
    settings.model = meta.known_models[0];   // 只是给个起点，用户随时可改
    syncSettingsUI();
  }
  renderSizes();
  renderPresets();
  renderMaxColorChoices();
  syncPaletteUI();
  applyDefaultZoom();
  renderStepper();
  updateGenerateEnabled();
  refreshChips();
  addTimeline("-", "runtime", "运行时已就绪（Pyodide + Pillow" +
    (S.numpyOk ? " + numpy" : "，无 numpy") + "）",
    { sizes: meta.sizes, default_size: meta.default_size, presets: (meta.presets || []).length,
      max_upload_bytes: meta.max_upload_bytes, max_image_pixels: meta.max_image_pixels,
      numpy: S.numpyOk }, null, "ok");
  describeConnection();
}

function boot() {
  loadSettings();
  syncSettingsUI();
  wireSettingsUI(function (key) {
    refreshChips();
    updateGenerateEnabled();
    describeConnection();
    if (key === "passes") {
      note(settings.passes === 2
        ? "已开启两轮精修：会调用模型两次。"
        : "已改为单轮：只调用模型一次，省一半 token。");
    }
  });

  initCopy();
  wireImageInput();

  $("#generate").addEventListener("click", startGenerate);
  $("#repixelize").addEventListener("click", function () { doRepixelize("手动重新渲染"); });
  cancelBtn.addEventListener("click", cancelRun);
  var clearKey = $("#cfg-clear-key");
  if (clearKey) {
    clearKey.addEventListener("click", function () {
      clearStoredKey();
      refreshChips();
      updateGenerateEnabled();
      note("已清除本页保存的 API key（sessionStorage 与 localStorage 都清掉了）。");
    });
  }

  $("#pixelize-only").addEventListener("change", function (e) {
    S.pixelizeOnly = e.target.checked;
    renderStepper();
    updateGenerateEnabled();
    describeConnection();
    addTimeline("-", "mode", S.pixelizeOnly
      ? "已切到「仅本地渲染」：不调用模型，直接用本地 Pillow 像素化你上传的原图（0 成本）"
      : "已关闭「仅本地渲染」：将调用模型重绘", null, null, "note");
  });
  $("#max-colors").addEventListener("change", function (e) {
    var v = parseInt(e.target.value, 10);
    if (isNaN(v)) v = 16;
    e.target.value = String(v);
    S.maxColors = v;
    if (S.paletteMode !== "auto") return;
    doRepixelize("auto 色数上限 → " + v);
  });
  $("#custom-hex").addEventListener("input", function () {
    S.paletteMode = "custom";
    syncPaletteUI();
    scheduleRepixelize("自定义十六进制");   // 每次按键都会触发，所以防抖
  });

  var resizeTimer = null;
  window.addEventListener("resize", function () {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () { applyZoom(); applyIntermediateZoom(); }, 120);
  });

  window.addEventListener("error", function (ev) {
    /* 前端自己的 bug 也走同一个面板：这样前端异常和上游 4xx 一样可调试 */
    pushError({
      kind: "frontend", where: "window.onerror",
      message: safeStr(ev.message) + "  @ " + safeStr(ev.filename) + ":" + safeStr(ev.lineno) + ":" + safeStr(ev.colno),
      detail: (ev.error && ev.error.stack) ? String(ev.error.stack) : ""
    }, { source: "frontend" });
  });
  window.addEventListener("unhandledrejection", function (ev) {
    var r = ev.reason;
    pushError({
      kind: "frontend", where: "unhandledrejection",
      message: (r && r.message) ? String(r.message) : json(r),
      detail: (r && r.stack) ? String(r.stack) : ""
    }, { source: "frontend" });
  });

  S.uiTimer = setInterval(function () {
    tickUI();
    describeConnection();
  }, 500);

  /* 剪贴板与「记住 key」都依赖安全上下文。HTTP + 裸 IP 访问时两者都不可用，
     所以启动就把这件事说清楚，而不是等用户点了复制才弹一条错误。 */
  if (!window.isSecureContext) {
    addTimeline("-", "secure-context",
      "当前不是安全上下文（HTTP + 裸 IP）：navigator.clipboard 与 localStorage 之外的存储能力都会受限，" +
      "复制功能会降级为「请手动选中」。「下载」按钮不受影响。", null, null, "note");
  }

  setRuntimeMetaHandler(applyMeta);
  addTimeline("-", "boot", "页面启动，正在从 " + PYODIDE_INDEX_URL + " 加载运行时（本页无后端）",
    null, null, "info");
  setStatus("busy", "正在加载运行时（首次约 7.5MB，之后走缓存）…");
  renderStepper();
  updateGenerateEnabled();
  refreshChips();
  bootWorker();
}

try { boot(); } catch (e) { report(e, "boot"); }
