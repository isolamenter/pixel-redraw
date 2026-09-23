/* 结果渲染：三张卡片、缩放、复制、下载。 */
import { S } from './state.js';
import { $, addTimeline, clear, el, note, num, safeStr } from './dom.js';
import { copyText, pushError, report } from './errors.js';
import { cancelBtn, indetEl, indetLbl, renderStepper, repixelizeBtn, updateGenerateEnabled, upbarEl } from './stepper.js';
import { cssColor, parseHexList } from './palette.js';
import { blobToBase64 } from './image.js';

/* 收窄成「把运行时交回来的结果整理成渲染需要的形状」。
   以前这里最麻烦的一段是从容器里推出 run_id 再拼出 /api/runs/<id>/... 的 URL，
   还要用 safeApiUrl 挡住不属于本源的地址；现在结果图是本页内存里的 blob: URL，
   由调用方直接给过来，所以那段整体消失。 */
export function normalizeResult(container) {
  var r = (container && (container.result || container.dto)) || container || {};
  var urls = r.urls || {};
  var report = r.report || {};
  var size = null;
  if (r.size && r.size.length >= 2) size = [r.size[0], r.size[1]];
  else if (report.size && report.size.length >= 2) size = [report.size[0], report.size[1]];
  var scale = (typeof r.scale === "number") ? r.scale
    : ((typeof report.scale === "number") ? report.scale : null);
  var colors = (typeof r.color_count === "number") ? r.color_count
    : ((typeof report.color_count === "number") ? report.color_count : null);
  return {
    /* 下载文件名以前以 run_id 开头；现在没有 run_id 了，用原图文件名更清楚。 */
    sourceName: (container && container.sourceName) || "pixel",
    size: size,
    base_size: report.base_size || null,
    scale: scale,
    palette_requested: r.palette_requested || report.palette_requested || null,
    palette_used: r.palette_used || report.palette_used || null,
    /* 子集断言仍由运行时的 assert_palette_subset 执行，但它失败会直接变成
       一条 kind=palette_violation 的错误信封，不会再以「结果里带个 false」的形式到达。 */
    subset_ok: null,
    color_count: colors,
    elapsed_s: (typeof r.elapsed_s === "number") ? r.elapsed_s : null,
    has_raw: !!r.has_raw,
    refinement_applied: !!r.refinement_applied,
    reducer: report.reducer || null,
    mean_vote_confidence: report.mean_vote_confidence !== undefined ? report.mean_vote_confidence : null,
    low_confidence_cells: report.low_confidence_cells !== undefined ? report.low_confidence_cells : null,
    cleanup_changes: report.cleanup_changes !== undefined ? report.cleanup_changes : null,
    grid_offset: report.grid_offset || null,
    grid_alignment_applied: !!report.grid_alignment_applied,
    draft_size: (r.draft_size && r.draft_size.length >= 2) ? [r.draft_size[0], r.draft_size[1]] : null,
    draft_scale: (typeof r.draft_scale === "number") ? r.draft_scale : null,
    urls: {
      draft: urls.draft || null,
      pixel: urls.pixel || null,
      preview: urls.preview || null,
      raw: urls.raw || null
    }
  };
}

export function finishRun(container) {
  var res = normalizeResult(container);
  S.result = res;
  S.running = false;
  cancelBtn.disabled = true;
  updateGenerateEnabled();
  repixelizeBtn.disabled = !S.lastResultRawB64;
  indetEl.hidden = true;
  indetLbl.hidden = true;
  upbarEl.hidden = true;
  renderStepper();
  renderResult(res);
}

/* 换新结果前把旧的 blob: URL 收掉，否则每跑一次都会泄漏几张全尺寸图的内存。 */
export function revokeResultUrls() {
  for (var i = 0; i < S.objectUrls.length; i++) {
    try { URL.revokeObjectURL(S.objectUrls[i]); } catch (e) {}
  }
  S.objectUrls = [];
  S.blobs.pixel = null;
  S.blobs.preview = null;
}

export function applyDefaultZoom() {
  var n = S.size;
  var ps = (S.meta && S.meta.preview_scales) || {};
  var z = ps[String(n)];
  /* preview_scales 是服务端唯一权威的放大倍数；这里只是拿它当屏幕上默认的整数缩放 */
  if (typeof z === "number" && [1, 2, 4, 8, 16, 32].indexOf(z) >= 0) S.zoom = z;
  else S.zoom = Math.max(1, Math.min(32, Math.floor(256 / n)));
  renderZoom();
}

export function renderZoom() {
  var box = $("#zooms");
  clear(box);
  var steps = [1, 2, 4, 8, 16, 32];
  steps.forEach(function (z) {
    var label = (z === 1) ? "1:1 真实尺寸" : ("×" + z);
    var b = el("button", { type: "button", class: "btn-sm", disabled: !S.result, text: label });
    /* 只允许整数倍：非整数缩放会把像素画重新插值成糊的 */
    b.addEventListener("click", function () {
      S.zoom = z;
      syncZoomPressed();
      applyZoom();
    });
    box.appendChild(b);
  });
  syncZoomPressed();
  applyZoom();
}

export function syncZoomPressed() {
  var btns = document.querySelectorAll("#zooms button");
  var steps = [1, 2, 4, 8, 16, 32];
  for (var i = 0; i < btns.length; i++) {
    var on = (S.zoom === steps[i]);
    btns[i].setAttribute("aria-pressed", on ? "true" : "false");
    btns[i].disabled = !S.result;
  }
}

export function setActionLink(link, url, filename, label) {
  var available = typeof url === "string" && url.length > 0;
  if (label !== undefined) link.textContent = label;
  link.classList.toggle("is-disabled", !available);
  link.setAttribute("aria-disabled", available ? "false" : "true");
  link.tabIndex = available ? 0 : -1;
  if (available) {
    link.href = url;
    if (filename) link.download = filename;
    else link.removeAttribute("download");
  } else {
    link.removeAttribute("href");
    link.removeAttribute("download");
  }
}

export function applyZoom() {
  var img = $("#stage-pixel").querySelector("img");
  if (!img || !S.result || !S.result.size) return;
  var n = S.result.size[0];
  var z = S.zoom || 1;
  img.style.width = (n * z) + "px";
  img.style.height = (n * z) + "px";
  /* 1:1 指的是 CSS 像素；HiDPI 屏上每个逻辑像素仍占 devicePixelRatio 个设备像素 */
  $("#zoom-info").textContent = "逻辑 " + n + "×" + S.result.size[1] + " → 显示 " + (n * z) + "×" +
    (S.result.size[1] * z) + " px" + (z === 1 ? "（1:1 真实尺寸）" : "（整数倍 ×" + z + "）") +
    (n * z > 520 ? "；超出预览框，可在框内滚动" : "");
}

export function renderResult(res) {
  var stage = $("#stage-pixel");
  clear(stage);
  /* 这里刻意不回收 blob: URL。这个函数只负责画，URL 的生命周期归 pipeline.onDone ——
     它在建新 URL 之前先调用 revokeResultUrls() 收掉上一批。曾经在这里回收过一次，
     结果是刚建好的 URL 在 fetch 之前就被撤销，预览图永远是 0×0：这个函数拿到的
     已经是「本批次」的 URL 了，不再像服务端时代那样只拿得到上一批的服务端地址。 */
  if (res.size) {
    var baseLabel = res.base_size ? " · " + res.base_size[0] + "×" + res.base_size[1] + " 基准" : "";
    $("#cap-logical").textContent = res.size[0] + "×" + res.size[1] + baseLabel;
  } else {
    $("#cap-logical").textContent = "—";
  }

  renderIntermediate(res);

  if (!res.urls.pixel) {
    stage.appendChild(el("span", { class: "stage-empty", text: "服务端没有给出图片地址。" }));
    renderZoom();
    return;
  }
  /* 预览用 <img> 而不是 canvas：canvas 需要同时满足 imageSmoothingEnabled、
     CSS image-rendering 和 devicePixelRatio 三个条件，<img> 一个都不需要。 */
  var img = el("img", { alt: "像素画结果预览" });
  if (res.size) { img.width = res.size[0]; img.height = res.size[1]; }
  stage.appendChild(img);

  /* 结果一到就把 PNG 拉进内存成 Blob：复制按钮必须在 click 处理器里同步拿到一个 Promise，
     不能等到点击时再发请求。 */
  S.blobs.pixel = fetchBlob(res.urls.pixel);
  S.blobs.preview = fetchBlob(res.urls.preview);
  S.blobs.pixel.then(function (b) {
    if (!b) return;
    var u = URL.createObjectURL(b);
    S.objectUrls.push(u);
    img.src = u;
    syncZoomPressed();
    applyZoom();
  }).catch(function (e) { report(e, "preview fetch"); });

  renderRaw(res);
  renderDownloads(res);
  renderPaletteEcho(res);
  renderResultNotes(res);

  /* 请求/响应 JSON 不再是服务端目录里可长期访问的文件：它们随失败信封一起到达，
     由错误控制台按需下载（见 errors.js）。成功的一跑没有这两份产物。 */
  $("#run-id-text").textContent = res.sourceName ? ("来源 = " + res.sourceName) : "";
  var cb = $("#copy-png"), cd = $("#copy-datauri");
  $("#copy-toggle-preview").disabled = !res.urls.pixel;
  $("#copy-toggle-logical").disabled = !res.urls.pixel;
  cb.disabled = !res.urls.pixel;
  cd.disabled = !res.urls.pixel;
  renderCopyTarget();
}

export function renderIntermediate(res) {
  var wrap = $("#draft-wrap"), stage = $("#stage-draft");
  clear(stage);
  if (!res.urls.draft) {
    wrap.hidden = false;
    stage.appendChild(el("span", { class: "stage-empty", text: "本次运行未生成 AI 初稿。" }));
    syncAiRowLayout();
    return;
  }
  wrap.hidden = false;
  syncAiRowLayout();
  var size = res.draft_size || res.size;
  var img = el("img", { alt: "首轮 AI 经 Pillow 量化后的中间图" });
  if (size && size.length >= 2) {
    img.width = size[0];
    img.height = size[1];
    img.dataset.logicalWidth = String(size[0]);
    img.dataset.logicalHeight = String(size[1]);
  }
  img.addEventListener("error", function () {
    wrap.hidden = false;
    clear(stage);
    stage.appendChild(el("span", { class: "stage-empty", text: "AI 初稿暂时无法读取。" }));
    syncAiRowLayout();
    addTimeline("-", "draft", "中间图取不到——保留初稿占位区",
      { url: res.urls.draft }, null, "note");
  });
  stage.appendChild(img);
  img.src = res.urls.draft;
  applyIntermediateZoom();
}

export function syncAiRowLayout() {
  var row = $("#ai-row");
  row.hidden = false;
  row.classList.remove("has-two");
}

export function integerFitScale(stage, width, height) {
  var availableWidth = Math.max(1, stage.clientWidth - 16);
  var availableHeight = Math.max(1, stage.clientHeight - 16);
  return Math.max(1, Math.min(32, Math.floor(Math.min(
    availableWidth / width, availableHeight / height
  ))));
}

export function applyIntermediateZoom() {
  var stage = $("#stage-draft"), img = stage.querySelector("img");
  if (!img) return;
  var width = parseInt(img.dataset.logicalWidth || "", 10);
  var height = parseInt(img.dataset.logicalHeight || "", 10);
  if (!width || !height) return;
  var scale = integerFitScale(stage, width, height);
  img.style.width = (width * scale) + "px";
  img.style.height = (height * scale) + "px";
}

export function fetchBlob(url) {
  if (!url) return Promise.resolve(null);
  return fetch(url, { cache: "no-store" }).then(function (r) {
    if (!r.ok) throw new Error("GET " + url + " → HTTP " + r.status);
    return r.blob();
  });
}

export function renderRaw(res) {
  var wrap = $("#raw-wrap"), stage = $("#stage-raw");
  clear(stage);
  /* 服务端没有保留 raw 时仍保留固定结果槽位，用说明替代碎图标；
     keep_raw 由 .env / 命令行决定，客户端不假设。 */
  if (!res.has_raw || !res.urls.raw) {
    wrap.hidden = false;
    stage.appendChild(el("span", { class: "stage-empty", text: "服务端未保留原始输出。" }));
    syncAiRowLayout();
    setActionLink($("#raw-open"), null);
    return;
  }
  wrap.hidden = false;
  syncAiRowLayout();
  setActionLink($("#raw-open"), res.urls.raw);
  var img = el("img", { alt: "模型原始输出" });
  stage.appendChild(img);
  img.addEventListener("error", function () {
    /* has_raw 为真也可能 404（例如 .env 变了）：这里退化成一句说明，而不是一个碎图标 */
    wrap.hidden = false;
    clear(stage);
    stage.appendChild(el("span", { class: "stage-empty", text: "原始输出暂时无法读取。" }));
    syncAiRowLayout();
    setActionLink($("#raw-open"), null);
    addTimeline("-", "raw", "raw.png 取不到（404）——保留原始输出占位区",
      { url: res.urls.raw }, null, "note");
  });
  img.src = res.urls.raw;
}

export function renderDownloads(res) {
  var n = res.size ? res.size[0] : S.size;
  var h = res.size ? res.size[1] : S.size;
  var colors = res.color_count !== null ? res.color_count :
    (res.palette_used ? res.palette_used.length : (res.palette_requested ? res.palette_requested.length : 0));
  /* 文件名以前以 run_id 打头（服务端时代每个 run 有自己的 id）；现在用原图名，
     用户在一个下载目录里能直接对上是哪张图。 */
  var stem = String(res.sourceName || "pixel").replace(/\.[^.]+$/, "");
  var base = stem || "pixel";
  var draftSize = res.draft_size || res.size || [S.size, S.size];
  setActionLink($("#download-draft"), res.urls.draft,
    base + "_draft_" + draftSize[0] + "x" + draftSize[1] + ".png",
    "下载首轮量化中间图");
  setActionLink($("#download-pixel"), res.urls.pixel,
    base + "_" + n + "x" + h + "_" + colors + "c.png",
    "下载 logical PNG");
  setActionLink($("#download-preview"), res.urls.preview,
    base + "_" + n + "x" + h + "_" + colors + "c_x" + (res.scale || "N") + ".png",
    "下载 nearest 放大预览");
  setActionLink($("#download-raw"), res.has_raw ? res.urls.raw : null,
    base + "_raw.png", "下载模型原始输出");
}

export function renderPaletteEcho(res) {
  var wrap = $("#pal-echo-wrap"), box = $("#pal-echo"), meta = $("#pal-echo-meta");
  clear(box); meta.textContent = "";
  var used = res.palette_used;
  var req = res.palette_requested;
  if ((!used || !used.length) && (!req || !req.length)) { wrap.hidden = true; return; }
  wrap.hidden = false;
  if (used && used.length) {
    used.forEach(function (item) {
      var hex = typeof item === "string" ? item : (item && (item.hex || item.color));
      var cnt = (item && typeof item === "object") ? (item.count !== undefined ? item.count : item.pixels) : null;
      var s = el("span", {});
      s.appendChild(el("i", { style: "background:" + cssColor(hex), title: safeStr(hex) }));
      s.appendChild(el("span", { text: safeStr(hex) + (cnt !== null ? " ×" + num(cnt) : "") }));
      box.appendChild(s);
    });
  } else {
    req.forEach(function (hex) {
      var s = el("span", {});
      s.appendChild(el("i", { style: "background:" + cssColor(hex) }));
      s.appendChild(el("span", { text: safeStr(hex) }));
      box.appendChild(s);
    });
  }
  var bits = [];
  if (res.reducer) bits.push("reducer=" + res.reducer);
  if (res.mean_vote_confidence !== null && res.mean_vote_confidence !== undefined) {
    bits.push("置信度=" + Math.round(res.mean_vote_confidence * 100) + "%");
  }
  if (res.cleanup_changes) bits.push("清理孤立点=" + res.cleanup_changes + "px");
  if (res.grid_alignment_applied && res.grid_offset) {
    bits.push("网格对齐=[" + res.grid_offset.join(",") + "]");
  }
  if (res.color_count !== null) bits.push("color_count=" + res.color_count +
    "（像素计数，不等于视觉色数，见 TECHNICAL.md §4.4）");
  if (res.subset_ok !== null) bits.push("subset_ok=" + res.subset_ok);
  if (res.scale !== null) bits.push("scale=" + res.scale);
  if (res.elapsed_s !== null) bits.push("elapsed_s=" + res.elapsed_s);
  meta.textContent = bits.join("  ·  ");
}

export function renderResultNotes(res) {
  var noteEl = $("#result-note"), warnEl = $("#result-warn");
  clear(noteEl); clear(warnEl);
  var bits = [];
  if (res.refinement_applied) {
    bits.push("两轮生成：第二轮在本地量化后的草稿上重画轮廓与色块，再做最终量化。");
  } else if (S.pixelizeOnly) {
    bits.push(S.lastResultSource === "repixelize"
      ? "本地重渲染：对缓存的首轮模型输出重新量化，没有调用模型（0 token）。"
      : "仅本地渲染：完全没有调用模型，这是本地 Pillow 对你上传原图的量化结果。");
  } else {
    bits.push("单轮生成：第一轮模型输出直接量化，未做第二轮精修。");
  }
  if (S.lastResultSource === "repixelize" && res.refinement_applied === undefined) {
    bits.push("本地重渲染（0 token）。");
  }
  noteEl.textContent = bits.join(" ");
  /* 子集断言由运行时的 assert_palette_subset 执行；它一旦失败就直接变成
     kind=palette_violation 的错误信封，不会再以「结果里带个 false」的形式到达这里。 */
}

export function renderCopyTarget() {
  var res = S.result;
  var n = res && res.size ? res.size[0] : S.size;
  var h = res && res.size ? res.size[1] : S.size;
  var scale = (res && res.scale) ? res.scale : 1;
  var pv = $("#copy-toggle-preview"), lg = $("#copy-toggle-logical");
  pv.disabled = !res;
  lg.disabled = !res;
  pv.setAttribute("aria-pressed", S.copyTarget === "preview" ? "true" : "false");
  lg.setAttribute("aria-pressed", S.copyTarget === "logical" ? "true" : "false");
  /* 明确写出当前复制的是哪一份，因为两者差一个重采样 */
  $("#copy-target").textContent = S.copyTarget === "preview"
    ? "当前复制：" + (n * scale) + "px 的 nearest 放大预览（贴到聊天工具里不会被平滑重采样）"
    : "当前复制：原始 " + n + "×" + h + "（喂素材流水线用）";
  var ok = !!(navigator.clipboard && navigator.clipboard.write && typeof ClipboardItem !== "undefined");
  if (ok && ClipboardItem.supports) {
    try { ok = ClipboardItem.supports("image/png"); } catch (e) { ok = true; }
  }
  var btn = $("#copy-png");
  btn.disabled = !ok || !S.result;
  btn.textContent = ok ? "复制 PNG" : "复制 PNG（本浏览器不支持：请用下载）";
}

export function initCopy() {
  var supported = !!(navigator.clipboard && navigator.clipboard.write && typeof ClipboardItem !== "undefined");
  if (supported && ClipboardItem.supports) {
    try { supported = ClipboardItem.supports("image/png"); } catch (e) { supported = true; }
  }
  if (!supported) {
    addTimeline("-", "copy", "本浏览器不支持 navigator.clipboard.write + ClipboardItem('image/png')，" +
      "复制按钮不可用；下载是保证可用的路径。", null, null, "note");
  }
  $("#copy-toggle-preview").addEventListener("click", function () { S.copyTarget = "preview"; renderCopyTarget(); });
  $("#copy-toggle-logical").addEventListener("click", function () { S.copyTarget = "logical"; renderCopyTarget(); });

  $("#copy-png").addEventListener("click", function () {
    /* 必须在 click 处理器内同步调用 write，并且把 Promise 作为 ClipboardItem 的值传进去：
       返回 Promise 才能满足 Firefox 的 user-gesture 要求与 WebKit 约 1s 的激活窗口。
       任何 await 都会把 write 推到手势窗口之外，浏览器就会拒绝。 */
    var which = S.copyTarget;
    var p = S.blobs[which] || S.blobs.pixel;
    if (!p) {
      pushError({ kind: "frontend", where: "clipboard.write", message: "还没有可复制的 PNG（blob 未预取）",
        hint: "等结果出现后再点。若结果已出现，看上面的预览是否加载失败。" });
      return;
    }
    var pngPromise = p.then(function (b) {
      if (!b) throw new Error("PNG blob 为空");
      /* 标明类型，避免某些实现对 blob 的 MIME 推断出问题 */
      return (b.type === "image/png") ? b : new Blob([b], { type: "image/png" });
    });
    navigator.clipboard.write([new ClipboardItem({ "image/png": pngPromise })]).then(function () {
      note("已复制 " + (which === "preview" ? "nearest 放大预览" : "原始 logical PNG") + " 到剪贴板");
    }).catch(function (err) {
      pushError({ kind: "frontend", where: "clipboard.write",
        message: String((err && err.message) || err),
        hint: "图片剪贴板不可用（常见于非安全上下文、权限被拒、或 PNG Promise 被拒绝）。" +
              "请改用「下载」按钮 —— 这是保证可用的路径。没有实现 execCommand('copy') 回退：" +
              "实测它在四种形态下都返回 true，却只在剪贴板留下 text/html。"
      });
      var d = $("#downloads");
      if (d) d.scrollIntoView({ block: "nearest" });
    });
  });

  $("#copy-datauri").addEventListener("click", function () {
    var p = S.blobs[S.copyTarget] || S.blobs.pixel;
    if (!p) { note("还没有可复制的图片"); return; }
    p.then(function (b) {
      return blobToBase64(b);
    }).then(function (b64) {
      var text = "data:image/png;base64," + b64;
      return navigator.clipboard.writeText(text).then(function () {
        note("已复制 data URI 文本（" + num(text.length) + " 字符）。注意：粘出来是一长串字符串，不是图片。");
      });
    }).catch(function (err) {
      pushError({ kind: "frontend", where: "clipboard.writeText(dataURI)",
        message: String((err && err.message) || err), hint: "请改用下载。" });
    });
  });

  $("#copy-palette").addEventListener("click", function () {
    var list = null;
    if (S.result && S.result.palette_requested && S.result.palette_requested.length)
      list = S.result.palette_requested;
    else list = parseHexList($("#custom-hex").value).colors;
    if (!list || !list.length) { note("没有可复制的调色板（当前是 auto）。"); return; }
    copyText(list.join(","), "已复制调色板，可直接粘进上面的自定义框：" + list.join(","), "复制调色板失败");
  });
}
