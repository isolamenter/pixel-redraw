/* 上传与客户端图像上限。 */
import { S } from './state.js';
import { $, addTimeline, clear, el, fmtBytes, json, note, num } from './dom.js';
import { pushError, report } from './errors.js';
import { updateGenerateEnabled } from './stepper.js';

import { refreshSizesOutput, updateSpecSummary } from './palette.js';

export var dropEl = $("#drop");

export function readDimensions(buf) {
  var u8 = new Uint8Array(buf), dv = new DataView(buf);
  if (u8.length < 16) return null;
  /* PNG：签名 + IHDR，宽在字节 16、高在字节 20（大端 u32） */
  if (u8[0] === 0x89 && u8[1] === 0x50 && u8[2] === 0x4e && u8[3] === 0x47 && u8.length >= 24)
    return { format: "png", w: dv.getUint32(16, false), h: dv.getUint32(20, false) };
  /* GIF：逻辑屏幕宽高在字节 6 / 8（小端 u16） */
  if (u8[0] === 0x47 && u8[1] === 0x49 && u8[2] === 0x46 && u8[3] === 0x38)
    return { format: "gif", w: dv.getUint16(6, true), h: dv.getUint16(8, true) };
  /* WebP：RIFF....WEBP，再按 VP8X / VP8 / VP8L 三种 chunk 各读各的 */
  if (u8.length >= 30 && u8[0] === 0x52 && u8[1] === 0x49 && u8[2] === 0x46 && u8[3] === 0x46 &&
      u8[8] === 0x57 && u8[9] === 0x45 && u8[10] === 0x42 && u8[11] === 0x50) {
    var cc = String.fromCharCode(u8[12], u8[13], u8[14], u8[15]);
    if (cc === "VP8X") {
      var w = 1 + (u8[24] | (u8[25] << 8) | (u8[26] << 16));
      var h = 1 + (u8[27] | (u8[28] << 8) | (u8[29] << 16));
      return { format: "webp/vp8x", w: w, h: h };
    }
    if (cc === "VP8 ") {
      if (!(u8[23] === 0x9d && u8[24] === 0x01 && u8[25] === 0x2a)) return null;
      return { format: "webp/vp8", w: dv.getUint16(26, true) & 0x3fff, h: dv.getUint16(28, true) & 0x3fff };
    }
    if (cc === "VP8L" && u8[20] === 0x2f) {
      var bits = dv.getUint32(21, true);
      return { format: "webp/vp8l", w: 1 + (bits & 0x3fff), h: 1 + ((bits >> 14) & 0x3fff) };
    }
    return null;
  }
  /* BMP */
  if (u8[0] === 0x42 && u8[1] === 0x4d && u8.length >= 26)
    return { format: "bmp", w: Math.abs(dv.getInt32(18, true)), h: Math.abs(dv.getInt32(22, true)) };
  /* JPEG：扫 marker，遇到 SOF0–SOF15（除 DHT/JPG/DAC）取高/宽 */
  if (u8[0] === 0xff && u8[1] === 0xd8) {
    var i = 2, n = u8.length;
    while (i + 9 < n) {
      if (u8[i] !== 0xff) { i++; continue; }
      var m = u8[i + 1];
      if (m === 0xff) { i++; continue; }
      if (m >= 0xd0 && m <= 0xd9) { i += 2; continue; }
      if (m === 0x01) { i += 2; continue; }
      var len = dv.getUint16(i + 2, false);
      if (len < 2) break;
      if ((m >= 0xc0 && m <= 0xcf) && m !== 0xc4 && m !== 0xc8 && m !== 0xcc)
        return { format: "jpeg", h: dv.getUint16(i + 5, false), w: dv.getUint16(i + 7, false) };
      i += 2 + len;
    }
    return null;
  }
  /* AVIF / HEIC：尽力找 ISO-BMFF 的 ispe box（宽高都是大端 u32） */
  if (u8.length >= 32 && u8[4] === 0x66 && u8[5] === 0x74 && u8[6] === 0x79 && u8[7] === 0x70) {
    for (var j = 0; j + 16 < u8.length; j++) {
      if (u8[j] === 0x69 && u8[j + 1] === 0x73 && u8[j + 2] === 0x70 && u8[j + 3] === 0x65)
        return { format: "isobmff/ispe", w: dv.getUint32(j + 8, false), h: dv.getUint32(j + 12, false) };
    }
  }
  return null;
}

export function verdictOnDimensions(w, h, src) {
  var maxDim = (S.meta && S.meta.max_dimension) || 4096;
  var maxPix = (S.meta && S.meta.max_image_pixels) || 16000000;
  if (w > maxDim || h > maxDim || w * h > maxPix) {
    return {
      kind: "limits", where: "client_precheck", phase: "received",
      message: "Image is " + w + "x" + h + " (" + num(w * h) + " pixels); the limit is " +
               num(maxPix) + " pixels (" + maxDim + " per side)",
      detail: "来源：" + src + "；尺寸由浏览器解析容器头得到，未解码。",
      hint: "服务端会在读取 body / Image.open 之前做同样的检查（12 MiB、单边 " + maxDim +
            "、共 " + num(maxPix) + " 像素）。请先缩小图片再上传。",
      debug_urls: null
    };
  }
  return null;
}

export function acceptFile(file, source) {
  if (!file) return;
  try {
    if (!file.type || file.type.indexOf("image/") !== 0) {
      pushError({ kind: "input_image", where: "client_precheck", phase: "received",
        message: "Not an image: type=" + json(file.type || "") + " name=" + json(file.name || ""),
        hint: "只接受 image/* 。截图粘贴时若浏览器没给 type，请改用拖入或文件选择。",
        http_status: null }, { source: source });
      return;
    }
    var maxBytes = (S.meta && S.meta.max_upload_bytes) || 12582912;
    if (file.size > maxBytes) {
      pushError({ kind: "limits", where: "client_precheck", phase: "received",
        message: "File is " + num(file.size) + " bytes (" + fmtBytes(file.size) + "); the limit is " +
                 num(maxBytes) + " bytes (" + fmtBytes(maxBytes) + ")",
        hint: "服务端会用 Content-Length 在读取 body 前做同样的检查（413）。",
        http_status: null }, { source: source });
      return;
    }

    file.slice(0, Math.min(file.size, 262144)).arrayBuffer().then(function (buf) {
      var dim = null;
      try { dim = readDimensions(buf); } catch (e) { dim = null; }
      if (dim) {
        var bad = verdictOnDimensions(dim.w, dim.h, source + " / " + dim.format);
        addTimeline("-", "precheck", "容器头解析成功（未解码）",
          { format: dim.format, w: dim.w, h: dim.h, bytes: file.size }, null, "info");
        if (bad) { pushError(bad, { source: source }); return; }
      } else {
        addTimeline("-", "precheck", "无法解析容器头，跳过尺寸预检（解码后再校验）",
          { name: file.name || "", type: file.type }, null, "note");
      }
      return decodeAndStore(file, source, dim);
    }).catch(function (e) { report(e, "acceptFile/header"); });
  } catch (e) { report(e, "acceptFile"); }
}

export function decodeAndStore(file, source, dim) {
  return createImageBitmap(file).then(function (bmp) {
    var w = bmp.width, h = bmp.height;
    var bad = verdictOnDimensions(w, h, source + " / decoded");
    if (bad) { if (bmp.close) bmp.close(); pushError(bad, { source: source }); return; }

    if (S.uploadSourceBitmap && S.uploadSourceBitmap.close) {
      try { S.uploadSourceBitmap.close(); } catch (e) {}
    }
    S.uploadSourceBitmap = bmp;
    S.uploadOriginalFile = file;
    S.uploadSource = source;
    S.uploadName = file.name || (source + "-image");

    return applyScaleAndEncode(S.inputScale || "original", source);
  });
}

export function applyScaleAndEncode(scaleKey, source) {
  if (!S.uploadSourceBitmap) return Promise.resolve(null);
  var bmp = S.uploadSourceBitmap;
  var ow = bmp.width, oh = bmp.height;
  var tw = ow, th = oh;

  if (scaleKey && scaleKey !== "original") {
    var maxDim = parseInt(scaleKey, 10);
    if (maxDim > 0) {
      var ratio = Math.min(1.0, maxDim / Math.max(ow, oh));
      tw = Math.max(1, Math.round(ow * ratio));
      th = Math.max(1, Math.round(oh * ratio));
    }
  }

  var cv = document.createElement("canvas");
  cv.width = tw; cv.height = th;
  var ctx = cv.getContext("2d");
  ctx.imageSmoothingEnabled = true;
  ctx.imageSmoothingQuality = "high";
  ctx.drawImage(bmp, 0, 0, tw, th);

  return new Promise(function (resolve) {
    cv.toBlob(function (blob) {
      if (!blob) {
        pushError({ kind: "frontend", where: "canvas.toBlob", message: "canvas.toBlob 返回 null",
          hint: "浏览器无法编码 PNG；换一个浏览器或换一张图。" }, { source: source });
        return;
      }
      var b64len = Math.ceil(blob.size / 3) * 4;
      var maxBytes = (S.meta && S.meta.max_upload_bytes) || 12582912;
      if (b64len + 512 > maxBytes) {
        pushError({ kind: "limits", where: "client_precheck", phase: "encoding",
          message: "Preserved-aspect PNG encodes to " + num(b64len) + " bytes of base64; the limit is " +
                   num(maxBytes) + " bytes",
          detail: "保留比例 " + tw + "x" + th + "，PNG " + fmtBytes(blob.size) + "。",
          hint: "base64 会膨胀 4/3。请先缩小或压缩图片（例如降低分辨率档位）。",
          http_status: null }, { source: source });
        return;
      }

      S.uploadBlob = blob;
      S.inputScale = scaleKey;
      var srcBytes = S.uploadOriginalFile ? S.uploadOriginalFile.size : blob.size;
      S.uploadInfo = { ow: ow, oh: oh, w: tw, h: th, srcBytes: srcBytes, encBytes: blob.size, scale: scaleKey };

      renderUploadInfo(source || S.uploadSource || "upload");
      renderScalePills();
      refreshSizesOutput();
      updateSpecSummary();
      updateGenerateEnabled();

      addTimeline("-", "input", "已处理图片（" + (scaleKey === "original" ? "原图" : scaleKey + "档") + "）",
        { original: ow + "x" + oh, scaled: tw + "x" + th, bytes: blob.size }, null, "info");

      resolve(blob);
    }, "image/png");
  });
}

export function renderScalePills() {
  var box = $("#scale-pills");
  if (!box) return;
  clear(box);
  var options = S.inputScaleOptions || [
    { key: "original", label: "原图" },
    { key: "1024", label: "1024 边长" },
    { key: "512", label: "512 边长" },
    { key: "256", label: "256 边长" },
    { key: "128", label: "128 边长" }
  ];

  var hasBmp = !!S.uploadSourceBitmap;
  var ow = hasBmp ? S.uploadSourceBitmap.width : 0;
  var oh = hasBmp ? S.uploadSourceBitmap.height : 0;

  options.forEach(function (opt) {
    var btn = el("button", {
      type: "button",
      class: "scale-pill" + (S.inputScale === opt.key ? " active" : ""),
      "data-key": opt.key
    });

    var dimStr = "—";
    if (hasBmp) {
      if (opt.key === "original") {
        dimStr = ow + "×" + oh;
      } else {
        var maxDim = parseInt(opt.key, 10);
        var ratio = Math.min(1.0, maxDim / Math.max(ow, oh));
        var tw = Math.max(1, Math.round(ow * ratio));
        var th = Math.max(1, Math.round(oh * ratio));
        dimStr = tw + "×" + th;
      }
    }

    btn.appendChild(el("span", { class: "p-name", text: opt.label }));
    btn.appendChild(el("span", { class: "p-dim", text: dimStr }));

    btn.addEventListener("click", function () {
      if (S.inputScale === opt.key) return;
      applyScaleAndEncode(opt.key, "scale-pill");
    });
    box.appendChild(btn);
  });
}

export function renderUploadInfo(source) {
  var box = $("#upload-info");
  clear(box);
  if (S.uploadThumbUrl) { try { URL.revokeObjectURL(S.uploadThumbUrl); } catch (e) {} S.uploadThumbUrl = null; }
  if (!S.uploadBlob) {
    box.hidden = true;
    dropEl.hidden = false;
    return;
  }
  var info = S.uploadInfo;
  var url = URL.createObjectURL(S.uploadBlob);
  S.uploadThumbUrl = url;

  dropEl.hidden = true;

  var prevWrap = el("div", { class: "upload-card-preview" }, [
    el("img", { src: url, alt: "输入源图清晰预览" })
  ]);

  var isScaled = (info.w !== info.ow || info.h !== info.oh);
  var dimText = isScaled
    ? "等比缩放 " + info.w + "×" + info.h + "（PNG " + fmtBytes(info.encBytes) + "）"
    : "保留原图 " + info.w + "×" + info.h + "（PNG " + fmtBytes(info.encBytes) + "）";

  var metaWrap = el("div", { class: "upload-card-meta" }, [
    el("div", { class: "row-tight", style: "justify-content:space-between" }, [
      el("span", { class: "badge-ok", text: "已就绪 (" + source + ")" }),
      el("span", { class: "tiny dim", text: S.uploadName || "" })
    ]),
    el("div", { class: "kv", text: "原始尺寸：" + info.ow + "×" + info.oh + "（" + fmtBytes(info.srcBytes) + "）" }),
    el("div", { class: "kv", text: dimText }),
    el("div", { class: "kv", text: "Base64 约 " + fmtBytes(Math.ceil(info.encBytes / 3) * 4) })
  ]);

  var actWrap = el("div", { class: "upload-card-actions" });
  var reselectLabel = el("label", {
    for: "file-input",
    class: "btn btn-sm",
    style: "cursor:pointer",
    text: "更换图片"
  });
  var clearBtn = el("button", {
    type: "button",
    class: "btn btn-sm btn-danger",
    text: "清除图片"
  });
  clearBtn.addEventListener("click", clearUploadedImage);

  actWrap.appendChild(reselectLabel);
  actWrap.appendChild(clearBtn);
  metaWrap.appendChild(actWrap);

  box.appendChild(prevWrap);
  box.appendChild(metaWrap);
  box.hidden = false;

  var clearGlobal = $("#clear-file");
  if (clearGlobal) clearGlobal.disabled = false;
  $("#generate-why").textContent = "";
  updateGenerateEnabled();
}

export function clearUploadedImage() {
  if (S.uploadSourceBitmap && S.uploadSourceBitmap.close) {
    try { S.uploadSourceBitmap.close(); } catch (e) {}
  }
  S.uploadSourceBitmap = null;
  S.uploadOriginalFile = null;
  S.uploadBlob = null;
  S.uploadInfo = null;
  S.uploadName = "";
  if (S.uploadThumbUrl) { try { URL.revokeObjectURL(S.uploadThumbUrl); } catch (e) {} S.uploadThumbUrl = null; }

  var box = $("#upload-info");
  clear(box);
  box.hidden = true;
  dropEl.hidden = false;

  var clearGlobal = $("#clear-file");
  if (clearGlobal) clearGlobal.disabled = true;

  renderScalePills();
  refreshSizesOutput();
  updateSpecSummary();
  updateGenerateEnabled();
}

/* 三条输入路径：拖拽、粘贴、文件选择器。 */
export function wireImageInput() {
  renderScalePills();

  var leftBox = $("#input-spec-left") || dropEl;
  ["dragenter", "dragover"].forEach(function (ev) {
    leftBox.addEventListener(ev, function (e) {
      e.preventDefault();
      e.stopPropagation();
      dropEl.classList.add("over");
      if (e.dataTransfer) e.dataTransfer.dropEffect = "copy";
    });
  });
  ["dragleave", "dragend"].forEach(function (ev) {
    leftBox.addEventListener(ev, function () { dropEl.classList.remove("over"); });
  });
  leftBox.addEventListener("drop", function (e) {
    e.preventDefault();
    e.stopPropagation();
    dropEl.classList.remove("over");
    var files = (e.dataTransfer && e.dataTransfer.files) || [];
    for (var i = 0; i < files.length; i++) {
      if (files[i].type && files[i].type.indexOf("image/") === 0) { acceptFile(files[i], "drop"); return; }
    }
    if (files.length) acceptFile(files[0], "drop");
  });

  /* 粘贴挂在 window 上，所以在页面任何位置 Cmd/Ctrl+V 都生效 */
  window.addEventListener("paste", function (e) {
    var cd = e.clipboardData;
    if (!cd) return;
    var file = null;
    if (cd.items && cd.items.length) {
      for (var i = 0; i < cd.items.length; i++) {
        var it = cd.items[i];
        if (it.kind === "file" && it.type && it.type.indexOf("image/") === 0) {
          file = it.getAsFile();
          if (file) break;
        }
      }
    }
    if (!file && cd.files && cd.files.length) {
      for (var j = 0; j < cd.files.length; j++) {
        if (cd.files[j].type && cd.files[j].type.indexOf("image/") === 0) { file = cd.files[j]; break; }
      }
    }
    if (!file) return;
    e.preventDefault();
    acceptFile(file, "paste");
  });

  $("#file-input").addEventListener("change", function (e) {
    var f = e.target.files && e.target.files[0];
    if (f) acceptFile(f, "picker");
    e.target.value = "";
  });

  var clearGlobal = $("#clear-file");
  if (clearGlobal) {
    clearGlobal.addEventListener("click", clearUploadedImage);
  }
}

export function blobToBase64(blob) {
  return new Promise(function (resolve, reject) {
    var fr = new FileReader();
    fr.onload = function () {
      var s = String(fr.result || "");
      var comma = s.indexOf(",");
      resolve(comma >= 0 ? s.slice(comma + 1) : s);
    };
    fr.onerror = function () { reject(new Error("FileReader 读取失败")); };
    fr.readAsDataURL(blob);
  });
}
