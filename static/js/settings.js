/* 用户自己的凭据与请求参数。
 *
 * 这是整个重构里安全模型变化最大的地方：API key 以前归服务端所有、只从环境变量
 * 读；现在它由用户在这个页面里填，并且只存在于这个浏览器里。由此有三条硬规则：
 *   1. 只发往用户自己填的那个端点，不发往任何其他地方；
 *   2. 默认只放 sessionStorage（关掉标签页即失效），勾选「在此设备记住」才写 localStorage；
 *   3. 绝不写进日志、绝不写进下载产物、渲染前再兜一次脱敏。
 */
import { $, el, note } from './dom.js';

var SESSION_KEY = "pixel_settings";
var LOCAL_KEY = "pixel_settings_remembered";

export var settings = {
  model: "",
  api_key: "",
  base_url: "",          // 空 = 用运行时给的官方端点
  api_version: "",       // 空 = 用运行时给的默认版本
  timeout: "",           // 空 = 用运行时给的默认值
  prompt: "",
  refine_prompt: "",
  passes: 2,
  remember: false
};

function safeParse(raw) {
  try { return raw ? JSON.parse(raw) : null; } catch (e) { return null; }
}

export function loadSettings() {
  var stored = null;
  try { stored = safeParse(sessionStorage.getItem(SESSION_KEY)); } catch (e) { stored = null; }
  if (!stored) {
    try {
      stored = safeParse(localStorage.getItem(LOCAL_KEY));
      if (stored) stored.remember = true;   // 只在本地存过 = 用户当初勾了记住
    } catch (e) { stored = null; }
  }
  if (stored) {
    for (var k in settings) {
      if (Object.prototype.hasOwnProperty.call(stored, k)) settings[k] = stored[k];
    }
  }
  return settings;
}

export function saveSettings() {
  /* 会话副本永远写：同一标签页里刷新不该丢配置。 */
  try { sessionStorage.setItem(SESSION_KEY, JSON.stringify(settings)); } catch (e) {}
  try {
    if (settings.remember) localStorage.setItem(LOCAL_KEY, JSON.stringify(settings));
    else localStorage.removeItem(LOCAL_KEY);
  } catch (e) {}
}

/* 渲染任何来自上游或运行时的字符串之前，再兜一次脱敏。
   Python 侧的 to_envelope 已经脱过一次；这一层是因为 key 现在就活在这个页面里，
   一次漏网就会把用户的 key 印在屏幕上、复制按钮里和任何一张截图里。 */
export function redactSecrets(text) {
  var out = String(text === null || text === undefined ? "" : text);
  var key = settings.api_key;
  if (key && key.length >= 6) {
    out = out.split(key).join("[redacted:api_key]");
    try { out = out.split(btoa(key)).join("[redacted:api_key:b64]"); } catch (e) {}
  }
  out = out.replace(/(x-goog-api-key\s*[:=]\s*)\S+/gi, "$1[redacted]");
  out = out.replace(/(Authorization\s*[:=]\s*)\S+/gi, "$1[redacted]");
  out = out.replace(/(Bearer\s+)\S+/gi, "$1[redacted]");
  out = out.replace(/\bAIza[0-9A-Za-z_\-]{35}\b/g, "[redacted:key-shaped]");
  out = out.replace(/(https?:\/\/)[^/@\s]+@/g, "$1[redacted]@");
  return out;
}

/* 送给 pixel_pipeline 的 upstream 块。空值交给 Python 的 make_config 兜底，
   这样「官方端点 + 默认版本」只需要在 Python 里维护一份默认值。 */
export function upstreamBlock() {
  return {
    model: settings.model,
    api_key: settings.api_key,
    base_url: settings.base_url,
    api_version: settings.api_version,
    timeout: settings.timeout ? Number(settings.timeout) : null
  };
}

/* 是否具备发起模型调用的条件。仅本地渲染路径不看这个。 */
export function upstreamReady() {
  return !!(settings.model && settings.api_key);
}

export function currentBaseUrl(meta) {
  return settings.base_url || (meta && meta.default_base_url) || "";
}

/* 端点主机名，用于状态条展示。刻意剥掉 userinfo：URL 里可能带 user:pass@。 */
export function upstreamHost(meta) {
  var url = currentBaseUrl(meta) || "";
  var withoutScheme = url.replace(/^[a-zA-Z][a-zA-Z0-9+.\-]*:\/\//, "");
  return withoutScheme.split("@").pop().split("/")[0].split("?")[0];
}

/* ---------------- 设置面板 ---------------- */

var FIELD_IDS = {
  model: "#cfg-model",
  api_key: "#cfg-key",
  base_url: "#cfg-base-url",
  api_version: "#cfg-api-version",
  timeout: "#cfg-timeout",
  prompt: "#cfg-prompt",
  refine_prompt: "#cfg-refine-prompt",
  passes: "#cfg-passes",
  remember: "#cfg-remember"
};

function readField(key) {
  var node = $(FIELD_IDS[key]);
  if (!node) return null;
  if (node.type === "checkbox") return node.checked;
  if (key === "passes") return parseInt(node.value, 10) === 1 ? 1 : 2;
  return node.value;
}

function writeField(key) {
  var node = $(FIELD_IDS[key]);
  if (!node) return;
  if (node.type === "checkbox") node.checked = !!settings[key];
  else node.value = settings[key] === null || settings[key] === undefined ? "" : String(settings[key]);
}

export function syncSettingsUI() {
  for (var key in FIELD_IDS) writeField(key);
}

/* 把输入框接上状态。onChange 由 app.js 传入，用来刷新状态条与按钮可用性。 */
export function wireSettingsUI(onChange) {
  Object.keys(FIELD_IDS).forEach(function (key) {
    var node = $(FIELD_IDS[key]);
    if (!node) return;
    var ev = (node.type === "checkbox" || node.tagName === "SELECT") ? "change" : "input";
    node.addEventListener(ev, function () {
      var value = readField(key);
      if (value !== null) settings[key] = value;
      saveSettings();
      if (key === "remember") {
        note(settings.remember
          ? "已勾选「在此设备记住」：key 会写进这个浏览器的 localStorage，换人用这台机器请先清除。"
          : "已取消「在此设备记住」：key 只留在本次会话里，关掉标签页即失效。");
      }
      if (onChange) onChange(key);
    });
  });
}

export function clearStoredKey() {
  settings.api_key = "";
  try { sessionStorage.removeItem(SESSION_KEY); } catch (e) {}
  try { localStorage.removeItem(LOCAL_KEY); } catch (e) {}
  syncSettingsUI();
}

export function knownModelsDatalist(meta) {
  var list = (meta && meta.known_models) || [];
  var node = $("#known-models");
  if (!node) return;
  while (node.firstChild) node.removeChild(node.firstChild);
  list.forEach(function (id) {
    node.appendChild(el("option", { value: id }));
  });
}
