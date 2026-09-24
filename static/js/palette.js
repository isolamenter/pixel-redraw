/* 密度与调色板控件。色值一律来自运行时（pixel_palettes），本文件不内联任何色值。 */
import { SMALL_WARN_IDS, SMALL_WARN_TEXT } from './config.js';
import { S } from './state.js';
import { $, clear, el, note, safeStr } from './dom.js';
/* 改密度 / 换调色板都要对缓存的首轮模型输出重新量化（0 token），
   那条路径在 pipeline.js 里。这里的循环引用是良性的：两边都只在运行时调用对方。 */
import { scheduleRepixelize } from './pipeline.js';
import { applyDefaultZoom } from './results.js';

export var SIZE_TRUTH = {
  8: "极限抽象，适合微图标 / 表情",
  16: "经典复古，适合图标 / 道具",
  32: "角色 / 道具级（黄金尺寸）",
  64: "细节较丰富，适合角色头像 / 场景",
  128: "高密度，适合复杂画幅 / 纹理"
};

export function renderSizes() {
  var box = $("#sizes");
  clear(box);
  var sizes = (S.meta && S.meta.sizes) || [8, 16, 32, 64, 128];
  sizes.forEach(function (n) {
    var id = "size-" + n;
    var lab = el("label", { for: id });
    var input = el("input", { type: "radio", name: "out-size", id: id, value: String(n) });
    input.checked = (n === S.size);
    input.addEventListener("change", function () {
      if (!input.checked) return;
      S.size = n;
      applyDefaultZoom();
      scheduleRepixelize("密度档位 → " + n + "×" + n + "（256 基准）");
    });
    lab.appendChild(input);
    lab.appendChild(el("span", {}, [
      el("span", { class: "n", text: n + "×" + n + " 基准" }), el("br"), el("span", { class: "t", text: SIZE_TRUTH[n] || "" })
    ]));
    box.appendChild(lab);
  });
}

export function cssColor(v) {
  return (typeof v === "string" && /^#[0-9a-fA-F]{3}([0-9a-fA-F]{3})?$/.test(v)) ? v : "transparent";
}

export function renderPresets() {
  var autoBox = $("#auto-choice");
  var box = $("#presets");
  clear(autoBox);
  clear(box);
  var presets = (S.meta && S.meta.presets) || [];
  autoBox.appendChild(presetButton({
    id: "auto", label: "auto", label_zh: "自动", colors: [],
    note: "may use colours outside any palette",
    note_zh: "可能使用任何调色板之外的颜色", auto: true
  }));
  presets.forEach(function (p) { box.appendChild(presetButton(p)); });
  markSelectedPreset();
}

export function presetButton(p) {
  var isAuto = !!p.auto || p.id === "auto";
  var btn = el("button", {
    type: "button", class: "preset" + (isAuto ? " auto-preset" : ""), "data-id": p.id
  });
  var head = el("div", { class: "ph" });
  head.appendChild(el("span", { text: p.label_zh || p.label || p.id }));
  if (!isAuto && typeof p.count === "number") head.appendChild(el("span", { class: "pn", text: p.count + " 色" }));
  if (p.verified) head.appendChild(el("span", { class: "badge-ok", text: "verified" }));
  btn.appendChild(head);
  var note = p.note_zh || p.note;
  if (note && !isAuto) btn.appendChild(el("span", { class: "note", text: note }));
  if (isAuto) btn.appendChild(el("span", { class: "note", text: "不绑定固定色板，按 Oklab K-Means 自动提取。" }));
  btn.addEventListener("click", function () {
    if (isAuto) {
      S.paletteMode = "auto";
      S.preset = null;
    } else {
      S.paletteMode = "preset";
      S.preset = p.id;
      $("#custom-hex").value = (p.colors || []).join(", ");
    }
    syncPaletteUI();
    scheduleRepixelize("调色板 → " + (p.label_zh || p.id));
  });
  return btn;
}

export function markSelectedPreset() {
  var btns = document.querySelectorAll("#auto-choice .preset, #presets .preset");
  for (var i = 0; i < btns.length; i++) {
    var id = btns[i].getAttribute("data-id");
    var on = (S.paletteMode === "preset" && id === S.preset) ||
             (S.paletteMode === "auto" && id === "auto");
    btns[i].setAttribute("aria-pressed", on ? "true" : "false");
  }
}

export function presetById(id) {
  var ps = (S.meta && S.meta.presets) || [];
  for (var i = 0; i < ps.length; i++) if (ps[i].id === id) return ps[i];
  return null;
}

export function parseHexList(text) {
  var out = [], bad = [];
  var items = String(text || "").split(/[;,\s]+/);
  for (var i = 0; i < items.length; i++) {
    var raw = items[i].trim();
    if (!raw) continue;
    var it = raw.charAt(0) === "#" ? raw.slice(1) : raw;
    if (it.length === 3) it = it.charAt(0) + it.charAt(0) + it.charAt(1) + it.charAt(1) + it.charAt(2) + it.charAt(2);
    if (/^[0-9a-fA-F]{6}$/.test(it)) out.push("#" + it.toLowerCase());
    else bad.push(raw);
  }
  return { colors: out, bad: bad };
}

export function paletteForRequest() {
  if (S.paletteMode === "auto") return null;
  if (S.paletteMode === "custom") {
    var items = String($("#custom-hex").value || "").split(/[;,\s]+/).filter(function (x) { return x.trim(); });
    if (!items.length) return null;      /* 空 = auto */
    return { colors: items.map(function (x) { return x.trim(); }) };
  }
  return S.preset ? { preset: S.preset } : null;
}

export function wireMaxColorsControls() {
  var modeAll = $("#max-colors-mode-all");
  var modeCustom = $("#max-colors-mode-custom");
  var valInput = $("#max-colors-val");
  if (!modeAll || !modeCustom || !valInput) return;

  modeAll.addEventListener("change", function () {
    if (modeAll.checked) {
      S.maxColorsMode = "all";
      valInput.disabled = true;
      updateMaxColorsStatus();
      renderLivePaletteEcho();
      if (S.paletteMode !== "auto") {
        scheduleRepixelize("颜色限制 → 全部");
      }
    }
  });

  modeCustom.addEventListener("change", function () {
    if (modeCustom.checked) {
      S.maxColorsMode = "custom";
      valInput.disabled = S.paletteMode === "auto";
      var num = parseInt(valInput.value, 10);
      if (isNaN(num) || num < 2) num = 8;
      S.maxColorsLimit = num;
      updateMaxColorsStatus();
      renderLivePaletteEcho();
      if (S.paletteMode !== "auto") {
        scheduleRepixelize("颜色限制 → " + num + " 色");
      }
    }
  });

  valInput.addEventListener("input", function () {
    var num = parseInt(valInput.value, 10);
    if (!isNaN(num) && num >= 2) {
      S.maxColorsLimit = num;
      updateMaxColorsStatus();
      renderLivePaletteEcho();
      if (S.paletteMode !== "auto") {
        scheduleRepixelize("颜色限制 → " + num + " 色");
      }
    }
  });
}

export function updateMaxColorsStatus() {
  var st = $("#max-colors-status");
  if (!st) return;
  if (S.paletteMode === "auto") {
    st.textContent = "Auto 模式下由算法自动提取色板";
    return;
  }
  if (S.maxColorsMode === "all") {
    st.textContent = "当前：使用所选色板全部颜色";
  } else {
    st.textContent = "当前：限定最多使用 " + (S.maxColorsLimit || 8) + " 色";
  }
}

export function syncPaletteUI() {
  markSelectedPreset();
  var auto = S.paletteMode === "auto";
  var modeAll = $("#max-colors-mode-all");
  var modeCustom = $("#max-colors-mode-custom");
  var valInput = $("#max-colors-val");
  
  if (modeAll && modeCustom && valInput) {
    modeAll.disabled = auto;
    modeCustom.disabled = auto;
    valInput.disabled = auto || (S.maxColorsMode === "all");
  }

  var caution = $("#palette-caution");
  clear(caution);
  if (!auto && S.size <= 16) {
    var p = presetById(S.preset);
    var warn = false, text = SMALL_WARN_TEXT;
    if (p && Array.isArray(p.weak_at)) {
      warn = p.weak_at.indexOf(S.size) >= 0;
      if (warn) {
        text = "服务端标注这套色板在 " + p.weak_at.map(function (n) { return n + "×" + n; }).join(" / ") +
               " 密度档位下颜色会不够用：细节上限由实际输出网格决定。";
      }
    } else if (p) {
      if (p.min_size && S.size < p.min_size) { warn = true; }
      if (p.warn_small || p.small_warning) { warn = true; text = p.small_warning || p.warn_small; }
      else if (SMALL_WARN_IDS[p.id]) { warn = true; }
    }
    if (warn) {
      caution.appendChild(el("div", { class: "caution", text: "⚠ " + S.size + "×" + S.size + " 基准 + " +
        ((p && (p.label_zh || p.label)) || S.preset) + "：" + text }));
    }
  }
  updateHexStatus();
  updateMaxColorsStatus();
  renderLivePaletteEcho();
}

export function updateHexStatus() {
  var st = $("#hex-status");
  if (S.paletteMode === "auto") { st.textContent = "auto：不使用固定调色板"; return; }
  var r = parseHexList($("#custom-hex").value);
  if (r.bad.length) {
    st.textContent = "已识别 " + r.colors.length + " 色；有 " + r.bad.length +
      " 项无法解析（" + r.bad.slice(0, 3).join(" ") + "），服务端会返回权威错误";
  } else {
    st.textContent = "已识别 " + r.colors.length + " 色";
  }
  var maxc = (S.meta && S.meta.max_palette_colors) || 256;
  if (r.colors.length > maxc) st.textContent += "；超过上限 " + maxc + " 色，服务端会拒绝";
}

export function renderLivePaletteEcho() {
  var box = $("#pal-echo");
  var meta = $("#pal-echo-meta");
  if (!box || !meta) return;

  clear(box);
  meta.textContent = "";

  if (S.paletteMode === "auto") {
    var pItem = el("span", { class: "dim tiny", text: "Auto 模式：将在生成/量化时根据图像内容动态提取最佳色彩（Oklab K-Means）。" });
    box.appendChild(pItem);
    meta.textContent = "模式 = auto  ·  K-Means 聚类";
    return;
  }

  var colors = [];
  if (S.paletteMode === "preset") {
    var p = presetById(S.preset);
    if (p && p.colors) colors = p.colors;
  } else if (S.paletteMode === "custom") {
    var r = parseHexList($("#custom-hex").value);
    colors = r.colors;
  }

  if (!colors.length) {
    box.appendChild(el("span", { class: "dim tiny", text: "尚未选择或输入有效颜色。" }));
    return;
  }

  colors.forEach(function (hex) {
    var s = el("span", {});
    s.appendChild(el("i", { style: "background:" + cssColor(hex), title: safeStr(hex) }));
    s.appendChild(el("span", { text: safeStr(hex) }));
    box.appendChild(s);
  });

  var bits = ["共 " + colors.length + " 色"];
  if (S.maxColorsMode === "custom" && S.maxColorsLimit && S.maxColorsLimit < colors.length) {
    bits.push("限定提取最多 " + S.maxColorsLimit + " 色子集（量化时按加权感知误差剪枝）");
  } else {
    bits.push("使用全部 " + colors.length + " 色");
  }
  meta.textContent = bits.join("  ·  ");
}
