/* 密度与调色板控件。色值一律来自运行时（pixel_palettes），本文件不内联任何色值。 */
import { SMALL_WARN_IDS, SMALL_WARN_TEXT } from './config.js';
import { S } from './state.js';
import { $, clear, el, note } from './dom.js';
/* 改密度 / 换调色板都要对缓存的首轮模型输出重新量化（0 token），
   那条路径在 pipeline.js 里。这里的循环引用是良性的：两边都只在运行时调用对方。 */
import { scheduleRepixelize } from './pipeline.js';
import { applyDefaultZoom } from './results.js';

export var SIZE_TRUTH = {
  8: "极限抽象，适合图标 / 表情",
  16: "图标级，保留主要轮廓",
  32: "角色 / 道具级",
  64: "细节较丰富，适合角色头像",
  128: "高密度，适合保留纹理",
  256: "超高密度，适合大场景 / 复杂画面",
  512: "极高细节，适合超大场景 / 精细画幅"
};

export function renderSizes() {
  var box = $("#sizes");
  clear(box);
  var sizes = (S.meta && S.meta.sizes) || [8, 16, 32, 64, 128, 256, 512];
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

export function swatchStrip(colors, max) {
  var wrap = el("span", { class: "sw" });
  var list = colors || [];
  var cap = max || 24;
  for (var i = 0; i < list.length && i < cap; i++)
    wrap.appendChild(el("i", { style: "background:" + cssColor(list[i]), title: list[i] }));
  if (list.length > cap) wrap.appendChild(el("span", { class: "tiny dim", text: "+" + (list.length - cap) }));
  return wrap;
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

export function renderMaxColorChoices() {
  var select = $("#max-colors");
  var choices = (S.meta && S.meta.color_choices) || [8, 12, 16, 24, 32, 48, 64];
  clear(select);
  choices.forEach(function (n) {
    select.appendChild(el("option", { value: String(n), text: n + " 色" }));
  });
  var wanted = String(S.maxColors || 16);
  var found = false;
  for (var i = 0; i < choices.length; i++) if (String(choices[i]) === wanted) found = true;
  if (!found) wanted = String(choices[0] || 16);
  select.value = wanted;
  S.maxColors = parseInt(wanted, 10);
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
  if (!isAuto && p.colors && p.colors.length) btn.appendChild(swatchStrip(p.colors, 16));
  var note = p.note_zh || p.note;
  if (note && !isAuto) btn.appendChild(el("span", { class: "note", text: note }));
  if (isAuto) btn.appendChild(el("span", { class: "note", text: "不绑定固定色板，按 max colors 自动量化。" }));
  btn.addEventListener("click", function () {
    if (isAuto) {
      S.paletteMode = "auto";
      S.preset = null;
      $("#custom-hex").value = "";
      $("#custom-hex").disabled = true;
    } else {
      S.paletteMode = "preset";
      S.preset = p.id;
      $("#custom-hex").disabled = false;
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

export function syncPaletteUI() {
  markSelectedPreset();
  var auto = S.paletteMode === "auto";
  $("#auto-options").hidden = !auto;
  $("#max-colors").hidden = !auto;
  $("#max-colors-lbl").hidden = !auto;
  $("#max-colors-hint").hidden = !auto;
  $("#custom-hex").disabled = auto;

  var caution = $("#palette-caution");
  clear(caution);
  if (!auto && S.size <= 16) {
    var p = presetById(S.preset);
    var warn = false, text = SMALL_WARN_TEXT;
    if (p && Array.isArray(p.weak_at)) {
      /* weak_at 是服务端给的“这套色板在哪些尺寸下会不够用”，它才是权威：
         同样 16 色的 pico8 在 8×8 没问题、db16 就有问题，光看颜色数推不出来。
         所以这里不做二次推断，服务端说什么就是什么。 */
      warn = p.weak_at.indexOf(S.size) >= 0;
      if (warn) {
        text = "服务端标注这套色板在 " + p.weak_at.map(function (n) { return n + "×" + n; }).join(" / ") +
               " 密度档位下颜色会不够用：细节上限由实际输出网格决定。";
      }
    } else if (p) {
      /* 服务端没给 weak_at（只按 CONTRACT.md 实现的那一版没有这个字段）时的保守退路 */
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
