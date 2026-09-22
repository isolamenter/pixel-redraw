/* 微型 DOM 助手（绝不拼接 HTML 字符串）与时间线。 */

export function el(tag, opts, kids) {
  var n = document.createElement(tag);
  if (opts) {
    for (var k in opts) {
      var v = opts[k];
      if (v === null || v === undefined) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = String(v);
      else if (k === "on") { for (var ev in v[k]) n.addEventListener(ev, v[k][ev]); }
      else n.setAttribute(k, String(v));
    }
  }
  if (kids) {
    var list = Object.prototype.toString.call(kids) === "[object Array]" ? kids : [kids];
    for (var i = 0; i < list.length; i++) {
      var kid = list[i];
      if (kid === null || kid === undefined || kid === false) continue;
      n.appendChild(typeof kid === "string" ? document.createTextNode(kid) : kid);
    }
  }
  return n;
}

export function $(sel) { return document.querySelector(sel); }

export function clear(node) { while (node.firstChild) node.removeChild(node.firstChild); }

export function pad(n, w) { var s = String(n); while (s.length < w) s = "0" + s; return s; }

export function stamp() {
  var d = new Date();
  return pad(d.getHours(), 2) + ":" + pad(d.getMinutes(), 2) + ":" + pad(d.getSeconds(), 2) +
         "." + pad(d.getMilliseconds(), 3);
}

export function fmtBytes(b) {
  if (typeof b !== "number" || !isFinite(b)) return "—";
  if (b < 1024) return b + " B";
  if (b < 1024 * 1024) return (b / 1024).toFixed(1) + " KiB";
  return (b / 1048576).toFixed(2) + " MiB";
}

export function num(n) { return typeof n === "number" ? n.toLocaleString("en-US") : String(n); }

export function json(v) { try { return JSON.stringify(v); } catch (e) { return String(v); } }

export function safeStr(v) { return v === null || v === undefined ? "" : String(v); }

export var timelineEl = $("#timeline");

/* 运行时把 PNG 以 base64 交回来（字符串跨 JS/Python 边界最便宜、且不留 PyProxy），
   页面再把它变回 Blob —— 结果图因此从「服务端的一个 URL」变成「本页内存里的一个 blob:」。 */
export function b64ToBlob(b64, mime) {
  var binary = atob(b64);
  var bytes = new Uint8Array(binary.length);
  for (var i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
  return new Blob([bytes], { type: mime || "application/octet-stream" });
}

export function note(msg) {
  addTimeline("-", "note", msg, null, null, "note");
}

export function addTimeline(seq, phase, label, detail, elapsedMs, cls) {
  if (timelineEl.firstChild && timelineEl.firstChild.classList &&
      timelineEl.firstChild.classList.contains("empty")) {
    clear(timelineEl);
  }
  var row = el("div", { class: "tl-row " + (cls || "") });
  row.appendChild(el("span", { class: "t", text: stamp() }));
  row.appendChild(el("span", { class: "p", text: safeStr(phase) }));
  if (seq !== null && seq !== undefined) row.appendChild(el("span", { text: "#" + seq }));
  if (label) row.appendChild(el("span", { text: safeStr(label) }));
  if (elapsedMs !== null && elapsedMs !== undefined) row.appendChild(el("span", { text: "elapsed_ms=" + elapsedMs }));
  if (detail !== null && detail !== undefined) row.appendChild(el("span", { class: "d", text: json(detail) }));
  timelineEl.appendChild(row);
  /* 只有时间线本身当前可见时才滚动，避免把正在看预览的用户硬拽上来 */
  var box = timelineEl.getBoundingClientRect();
  if (box.bottom > 0 && box.top < window.innerHeight) row.scrollIntoView({ block: "nearest" });
  return row;
}
