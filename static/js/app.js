/* 页面入口：装配所有模块、把运行时元数据落到控件上、接好事件。
 *
 * 图像像素化任务仍在本页 Worker 中运行；只有 Gemini 请求通过站点 API 函数转发。 */
import { PYODIDE_INDEX_URL } from './config.js';
import { S } from './state.js';
import { $, addTimeline, el, json, safeStr } from './dom.js';
import { pushError, report } from './errors.js';
import {
  cancelBtn, describeConnection, renderStepper, setStatus, tickUI, updateGenerateEnabled,
} from './stepper.js';
import { renderPresets, renderSizes, syncPaletteUI, wireMaxColorsControls } from './palette.js';
import { wireImageInput } from './image.js';
import { applyDefaultZoom, applyZoom, initCopy } from './results.js';
import {
  loadSettings, settings, upstreamHost, upstreamReady,
} from './settings.js';
import {
  bootWorker, cancelRun, doRepixelize, scheduleRepixelize, setRuntimeMetaHandler, startGenerate,
} from './pipeline.js';

/* 状态条：版本、服务端模型与 Gemini 官方端点。 */
function refreshChips() {
  var meta = S.meta || {};
  var version = $("#chip-version");
  version.textContent = "版本 ";
  version.appendChild(el("b", { text: meta.version || "—" }));

  var model = $("#chip-model");
  model.textContent = "模型 ";
  model.appendChild(el("b", { text: settings.model || "—" }));

  var upstream = $("#chip-upstream");
  var ready = upstreamReady();
  var host = upstreamHost();
  upstream.textContent = "端点 ";
  upstream.appendChild(el("b", { text: ready ? host : "未配置" }));
  upstream.title = ready
    ? "本站服务端使用私密 API Key 调用 Gemini 官方端点"
    : (settings.configError ? "无法读取本站 Gemini 配置：" + settings.configError : "本站尚未配置 Gemini API Key");
  upstream.style.borderColor = ready ? "" : "var(--warn)";
  upstream.style.color = ready ? "" : "var(--warn)";
}

/* 运行时上报的 meta：密度档位、色数、调色板预设、各项上限。
   这些是程序的属性，所以仍然由运行时说了算；浏览器语言环境相关的只做兜底。 */
export function applyMeta(meta) {
  S.meta = meta;
  S.size = meta.default_size || 32;
  S.preset = null;
  S.paletteMode = "auto";
  S.maxColors = meta.default_max_colors || 16;
  renderSizes();
  renderPresets();
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
  loadSettings().then(function () {
    refreshChips();
    updateGenerateEnabled();
    describeConnection();
  });

  refreshChips();
  initCopy();
  wireImageInput();
  wireMaxColorsControls();

  $("#generate").addEventListener("click", startGenerate);
  $("#repixelize").addEventListener("click", function () { doRepixelize("手动重新渲染"); });
  cancelBtn.addEventListener("click", cancelRun);
  document.addEventListener("click", function (e) {
    var a = e.target && e.target.closest ? e.target.closest("a") : null;
    if (a && (a.classList.contains("is-disabled") || a.getAttribute("aria-disabled") === "true" || !a.getAttribute("href"))) {
      e.preventDefault();
    }
  });

  $("#pixelize-only").addEventListener("change", function (e) {
    S.pixelizeOnly = e.target.checked;
    renderStepper();
    updateGenerateEnabled();
    describeConnection();
    addTimeline("-", "mode", S.pixelizeOnly
      ? "已切到「仅本地渲染」：不调用模型，直接用本地 Pillow 像素化你上传的原图（0 成本）"
      : "已关闭「仅本地渲染」：将调用模型重绘", null, null, "note");
  });
  $("#custom-hex").addEventListener("input", function () {
    S.paletteMode = "custom";
    S.preset = null;
    syncPaletteUI();
    scheduleRepixelize("自定义十六进制");   // 每次按键都会触发，所以防抖
  });

  var resizeTimer = null;
  window.addEventListener("resize", function () {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () { applyZoom(); }, 120);
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

  /* 剪贴板依赖安全上下文。HTTP + 裸 IP 访问时复制会降级为手动选择。 */
  if (!window.isSecureContext) {
    addTimeline("-", "secure-context",
      "当前不是安全上下文（HTTP + 裸 IP）：navigator.clipboard 与 localStorage 之外的存储能力都会受限，" +
      "复制功能会降级为「请手动选中」。「下载」按钮不受影响。", null, null, "note");
  }

  setRuntimeMetaHandler(applyMeta);
  addTimeline("-", "boot", "页面启动，正在从 " + PYODIDE_INDEX_URL + " 加载运行时",
    null, null, "info");
  setStatus("busy", "正在加载运行时（首次约 7.5MB，之后走缓存）…");
  renderStepper();
  updateGenerateEnabled();
  refreshChips();
  bootWorker();
}

try { boot(); } catch (e) { report(e, "boot"); }
