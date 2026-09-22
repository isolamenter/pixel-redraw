/* 常驻错误控制台。
   过去的错误信封由服务端产出；现在它由 Pyodide 里的 pixel_pipeline 产出，
   形状与 kind 契约不变，所以这个面板本身几乎没动 —— 改动集中在三处：
   ①不再有服务端调试 URL，改为把信封里自带的 request/response 对象下载成 JSON；
   ②复现命令不再是一行 CLI（CLI 已删除），而是「重试」按钮；
   ③渲染前再做一次本地脱敏，因为现在 key 就在这个页面里。 */
import { KIND_HINT, KIND_SEV, PHASE_ZH } from './config.js';
import { S } from './state.js';
import { $, el, note, safeStr, stamp } from './dom.js';
import { redactSecrets } from './settings.js';
import { startGenerate } from './pipeline.js';

export var consoleEl = $("#console");
export var consoleEmpty = $("#console-empty");
export var consolePanel = $("#console-panel");
export var errBadge = $("#err-badge");

/* 调试产物：信封里带的原始 request / response，做成可下载的 JSON。
   服务端时代这是 runs/<id>/request.json，现在本地生成、本地下载。 */
function artifactButton(label, value) {
  if (value === null || value === undefined) return null;
  return el("button", {
    type: "button", class: "btn-sm", text: label,
    on: { click: function () {
      var text = JSON.stringify(value, null, 2);
      var url = URL.createObjectURL(new Blob([text], { type: "application/json" }));
      var a = el("a", { href: url, download: label.replace("↧ ", "") });
      document.body.appendChild(a);
      a.click();
      document.body.removeChild(a);
      setTimeout(function () { URL.revokeObjectURL(url); }, 10000);
      note("已下载 " + label);
    } }
  });
}

export function pushError(env, opts) {
  opts = opts || {};
  var e = env || {};
  var sev = KIND_SEV[e.kind] || "err";
  var block = el("div", { class: "errblock" });

  var head = el("div", { class: "errhead" });
  head.appendChild(el("span", { class: "kind sev-" + sev, text: safeStr(e.kind || "unknown") }));
  if (e.http_status !== null && e.http_status !== undefined && e.http_status !== "")
    head.appendChild(el("span", { class: "meta-chip", text: "HTTP " + e.http_status }));
  if (e.where) head.appendChild(el("span", { class: "meta-chip", text: "where=" + e.where }));
  if (e.phase) head.appendChild(el("span", { class: "meta-chip", text: "phase=" + e.phase }));
  if (e.exception) head.appendChild(el("span", { class: "meta-chip", text: e.exception }));
  if (e.model) head.appendChild(el("span", { class: "meta-chip", text: "model=" + e.model }));
  head.appendChild(el("span", { class: "meta-chip", text: stamp() }));
  if (opts.source) head.appendChild(el("span", { class: "meta-chip", text: opts.source }));
  block.appendChild(head);

  /* message：永远是原文（脚本侧已脱敏），不翻译、不改写 */
  block.appendChild(el("div", { class: "lbl2", text: "message（原文，未翻译）" }));
  block.appendChild(el("pre", { class: "blk msg", text: redactSecrets(safeStr(e.message || "(无 message)")) }));

  var hint = e.hint || KIND_HINT[e.kind] || "";
  if (hint) {
    block.appendChild(el("div", { class: "lbl2", text: "hint（可操作建议）" }));
    block.appendChild(el("pre", { class: "blk", text: redactSecrets(safeStr(hint)) }));
  }
  if (e.detail) {
    block.appendChild(el("div", { class: "lbl2", text: "detail（上游原始返回，最多 4000 字符）" }));
    block.appendChild(el("pre", { class: "blk", text: redactSecrets(safeStr(e.detail)) }));
  }
  if (e.traceback) {
    var d = el("details");
    d.appendChild(el("summary", { text: "traceback（Python 侧，已脱敏）" }));
    d.appendChild(el("pre", { class: "blk", text: redactSecrets(safeStr(e.traceback)) }));
    block.appendChild(d);
  }

  var artifacts = el("div", { class: "row-tight", style: "margin-top:8px" });
  var requestBtn = artifactButton("↧ request.json", e.request);
  var responseBtn = artifactButton("↧ response.json", e.response);
  if (requestBtn) artifacts.appendChild(requestBtn);
  if (responseBtn) artifacts.appendChild(responseBtn);
  if (requestBtn || responseBtn) {
    block.appendChild(el("div", { class: "lbl2",
      text: "调试产物（本地生成，不上传）：上游拒绝了请求时，完整响应体是唯一的证据。" }));
    block.appendChild(artifacts);
  }

  var btns = el("div", { class: "row-tight", style: "margin-top:8px" });
  btns.appendChild(el("button", {
    type: "button", class: "btn-sm",
    text: "复制错误（已脱敏）",
    on: { click: function () {
      copyText(errorToText(e), "已复制错误原文（已脱敏）", "复制失败");
    } }
  }));
  if (S.uploadBlob) {
    btns.appendChild(el("button", {
      type: "button", class: "btn-sm", text: "重试（用当前参数重跑）",
      on: { click: function () { startGenerate(); } }
    }));
  }
  block.appendChild(btns);

  /* 失败路径的关键信息：这一条到底发生在哪个阶段 */
  if (e.phase && PHASE_ZH[e.phase]) {
    block.appendChild(el("p", { class: "hintline",
      text: "失败阶段：" + e.phase + "（" + PHASE_ZH[e.phase] + "）—— 时间线里标红的那一步。" }));
  }

  consoleEl.insertBefore(block, consoleEl.firstChild);   /* 新的在最上面，旧的永不删除 */
  S.errorCount += 1;
  consoleEmpty.hidden = true;
  consolePanel.classList.add("has-error");
  errBadge.hidden = false;
  errBadge.textContent = S.errorCount + " 个错误 ↓";
  errBadge.classList.remove("pulse");
  void errBadge.offsetWidth;      /* 重启动画 */
  errBadge.classList.add("pulse");
  if (block.getBoundingClientRect().top < 0) block.scrollIntoView({ block: "nearest" });
  return block;      // 调用方可能要在原地追加“后续：已恢复”之类的标注
}

export function errorToText(e) {
  var lines = [];
  lines.push("kind=" + safeStr(e.kind) + "  http_status=" + safeStr(e.http_status) +
             "  where=" + safeStr(e.where) + "  phase=" + safeStr(e.phase));
  if (e.exception) lines.push("exception=" + e.exception);
  lines.push("message: " + safeStr(e.message));
  if (e.hint) lines.push("hint: " + safeStr(e.hint));
  if (e.detail) lines.push("detail: " + safeStr(e.detail));
  if (e.traceback) lines.push("traceback:\n" + safeStr(e.traceback));
  lines.push("（message/detail 由运行时原样透传；key 已在 Python 侧 redact() 统一替换，此处再兜一次）");
  return redactSecrets(lines.join("\n"));
}

export function copyText(text, okMsg, failMsg) {
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(
      function () { note(okMsg); },
      function (err) {
        pushError({ kind: "frontend", where: "clipboard.writeText",
          message: String((err && err.message) || err),
          hint: failMsg + "：请手动选中文本复制。" });
      });
  } else {
    pushError({ kind: "frontend", where: "clipboard.writeText",
      message: "navigator.clipboard.writeText 不可用",
      hint: "HTTP + 裸 IP 访问时页面不是安全上下文（secure context），浏览器会直接禁用剪贴板 API。" +
            "请手动选中文本复制，或改用「下载」。这个页面有意不实现 execCommand('copy') 回退：" +
            "实测它会返回 true，却只在剪贴板里留下 text/html。" });
  }
}

/* 把任何异常/拒绝收敛成一条控制台记录。
   没有服务端的 __env 信封了，所以这里只有两种来源：本页 JS 异常，
   或者一个已经成形的信封（pipeline 从 Pyodide 拿回来的）。 */
export function report(err, source) {
  if (err && err.__env) pushError(err.__env, { source: source });
  else if (err instanceof Error) pushError({
    kind: "frontend", where: source || "frontend",
    message: err.message, detail: err.stack || "", http_status: null
  }, { source: source });
  else pushError({ kind: "frontend", where: source || "frontend",
    message: JSON.stringify(err), http_status: null }, { source: source });
}
