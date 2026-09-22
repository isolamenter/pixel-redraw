/* 进度 UI：阶段步进器、状态行、耗时、上传进度条。
 *
 * 与服务端时代相比，这里反而变简单了：进度不再是从 SSE 流里猜出来的
 * （连接断没断、有没有收到终态帧、要不要拿 /state 兜底），而是 Worker 直接推过来的
 * 事实。因此 readiness 只剩两种状态：运行时在加载，或者可以跑。 */
import { PHASE_ZH, STAGES } from './config.js';
import { S } from './state.js';
import { $, clear, el, fmtBytes } from './dom.js';
import { settings, upstreamReady } from './settings.js';
import { BOOT_STEP_TEXT } from './pipeline.js';

export var stepperEl = $("#stepper"), statusDot = $("#status-dot"), statusText = $("#status-text"),
    elapsedText = $("#elapsed-text"), phaseText = $("#phase-text"),
    indetEl = $("#indet"), indetLbl = $("#indet-lbl"),
    upbarEl = $("#upbar"), upfillEl = $("#upfill"), uptextEl = $("#uptext"),
    cancelBtn = $("#cancel"), repixelizeBtn = $("#repixelize");

export function renderStepper() {
  clear(stepperEl);
  var lastSeen = S.currentPhase;
  for (var i = 0; i < STAGES.length; i++) {
    var st = STAGES[i];
    var rec = S.phasesSeen[st.id];
    var cls = "st", mk = "○", extra = "";
    var skipped = S.pixelizeOnly && st.up && !rec && !S.phasesSeen.upstream_wait;
    if (rec) {
      mk = "✓"; cls += " done";
      if (rec.elapsed_ms !== null && rec.elapsed_ms !== undefined) extra = "+" + rec.elapsed_ms + "ms";
    } else if (st.id === lastSeen) {
      mk = "◉"; cls += " current";
    } else if (skipped) {
      mk = "–"; cls += " skipped"; extra = "仅本地渲染跳过";
    } else {
      cls += " pending";
    }
    if (st.id === S.failedPhase) { cls = "st failed"; mk = "✕"; extra = "失败"; }
    var chip = el("span", { class: cls });
    chip.appendChild(el("span", { class: "mk", text: mk }));
    chip.appendChild(el("span", { text: st.id + " " + PHASE_ZH[st.id] }));
    if (extra) chip.appendChild(el("span", { class: "el", text: extra }));
    stepperEl.appendChild(chip);
  }
}

export function setStatus(kind, text) {
  statusDot.className = "dot" + (kind ? " " + kind : "");
  statusText.textContent = text;
}

export function tickUI() {
  if (!S.running) {
    if (S.elapsedMs) elapsedText.textContent = "上次运行用 " + (S.elapsedMs / 1000).toFixed(1) + "s";
    else elapsedText.textContent = "";
    return;
  }
  var secs = (Date.now() - S.runStartedAt) / 1000;
  /* 这个超时现在是墙钟：Python 侧用 asyncio.wait_for 包住 fetch，
     到达时限就真的放弃等待（旧实现是逐 socket 超时，永远不会到点）。 */
  var timeout = (settings.timeout ? Number(settings.timeout)
    : (S.meta && S.meta.default_timeout)) || 180;
  var text = "已用 " + secs.toFixed(1) + "s / 超时 " + timeout + "s（墙钟：到点本地放弃等待）";
  if (secs > timeout) text += "  已超时，等待取消";
  elapsedText.textContent = text;
}

export function describeConnection() {
  if (S.bootState === "failed") { setStatus("err", "运行时加载失败，无法运行。"); return; }
  if (S.bootState === "loading") {
    /* 冷启动要下约 7.5MB：这一步的文案由 Worker 逐步上报（见 pipeline.js 的
       BOOT_STEP_TEXT），这里只负责在没有更具体的信息时兜底。注意这个函数每 500ms
       跑一次，所以它必须尊重 S.bootStep，否则会把更具体的那条覆盖掉。 */
    setStatus("busy", (S.bootStep && BOOT_STEP_TEXT[S.bootStep])
      || "正在加载运行时（首次约 7.5MB，之后走缓存）…");
    return;
  }
  if (S.running) {
    /* 慢路径提示：固定调色板 + 无 numpy 时最近色映射会退化成纯 Python 双重循环 */
    if (S.numpyOk === false && S.paletteMode === "preset") {
      setStatus("warn", "运行中……（numpy 缺失，固定调色板的最近色映射会非常慢）");
      return;
    }
    return;      // 运行中的状态由 onProgress 负责，这里不覆盖
  }
  if (S.numpyOk === false) {
    setStatus("warn", "就绪（numpy 未加载：固定调色板 + 高密度会很慢）");
    return;
  }
  if (!S.pixelizeOnly && !upstreamReady()) {
    setStatus("warn", "就绪，但还没有填模型名与 API key。");
    return;
  }
  setStatus("", "就绪。");
}

export function setUploadProgress(loaded, total) {
  upbarEl.hidden = false;
  var pct = total ? Math.round((loaded / total) * 100) : null;
  /* 上传进度是真实测得的字节数，所以这里允许百分比；模型调用阶段不允许 */
  uptextEl.textContent = (pct === null ? "" : pct + "%  ") + fmtBytes(loaded) + " / " + fmtBytes(total);
  upfillEl.style.width = (pct === null ? 100 : pct) + "%";
}

/* 生成按钮的唯一权威。以前还要考虑「服务端是单任务锁，第二个请求会 409 busy」，
   现在没有服务端、也没有锁：只是本页一次跑一个。 */
export function updateGenerateEnabled() {
  var btn = $("#generate");
  var why = "";
  var ok = true;
  if (S.bootState !== "ready") { ok = false; why = "运行时尚未就绪。"; }
  else if (!S.uploadBlob) { ok = false; why = "先给一张图。"; }
  else if (S.running) { ok = false; why = "已有任务在跑；可以点「终止」。"; }
  else if (!S.pixelizeOnly && !upstreamReady()) {
    ok = false;
    why = "还没填模型名与 API key。勾上「仅本地渲染」可以完全离线地验证尺寸与调色板。";
  }
  btn.disabled = !ok;
  $("#generate-why").textContent = why;
  if (repixelizeBtn) repixelizeBtn.disabled = !(S.lastResultRawB64 && !S.running && S.bootState === "ready");
}
