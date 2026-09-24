/* 常量：阶段名、密度档位、错误 kind 的严重度与提示。
   这些都是程序的属性，不是用户的属性 —— 与 pixel_pipeline.web_meta() 同源；
   能由运行时回答的（密度、调色板、上限）一律从 meta 取，本文件只做兜底。 */

/* 运行时（Pyodide）从本地加载（无需外网 CDN）。 */
export var PYODIDE_INDEX_URL = new URL("../pyodide/", import.meta.url).href;

/* 上游阶段：仅本地渲染时不会出现，标记为“跳过”而不是永远转圈。 */
export var PHASE_ZH = {
  received: "接收上传", encoding: "编码图片",
  upstream_wait: "生成初稿", upstream_response: "初稿已返回",
  extract_start: "解析响应", extracted: "已取到图片",
  refine_wait: "整理轮廓与色块", refine_response: "精修已返回", refined: "最终图已就绪",
  pixelizing: "本地像素化", verifying: "校验调色板子集", saving: "生成产物",
  done: "完成", error: "失败"
};

export var STAGES = [
  { id: "received", up: false }, { id: "encoding", up: false },
  { id: "upstream_wait", up: true }, { id: "upstream_response", up: true },
  { id: "extract_start", up: true }, { id: "extracted", up: true },
  { id: "refine_wait", up: true }, { id: "refine_response", up: true },
  { id: "refined", up: true },
  { id: "pixelizing", up: false }, { id: "verifying", up: false },
  { id: "saving", up: false }
];

/* 在 8×8 / 16×16 下需要提醒的固定调色板。色值一律来自运行时，这里只有 id 与文案。 */
export var SMALL_WARN_IDS = { gameboy_dmg: 1, gameboy_bgb: 1, slso8: 1, db16: 1, c64_pepto: 1 };
export var SMALL_WARN_TEXT = "该调色板在低密度档位下颜色会不够用：细节上限由实际输出网格决定。";

export var KIND_SEV = {
  config: "warn", palette: "warn", size: "warn", limits: "warn", input_image: "warn",
  upstream_http: "err", upstream_transport: "err", upstream_timeout: "err",
  upstream_non_json: "err", upstream_protocol: "err", upstream_error_field: "err",
  no_image: "err", palette_violation: "err", internal: "err",
  /* 服务端时代遗留的 kind 已随服务端删掉（busy / not_found / forbidden /
     client_disconnect / request）。没列到的 kind 一律按 err（红）渲染，
     并把 kind 原文照抄，不替上游改口。 */
  invariant: "err", frontend: "info"
};

/* kind -> 可操作建议。
   这些提示以前由服务端下发，但服务端已经不存在了：现在的失败要么来自用户自己的
   浏览器，要么来自用户自己填的端点，所以文案里不能再出现 .env / GEMINI_* 这类
   只有在服务端部署里才存在的东西。 */
export var KIND_HINT = {
  config: "检查页面上的模型名与 API key 是否都填了。两者缺一，都不会发起请求。",
  palette: "调色板写法：#RRGGBB 逗号分隔，也接受 #RGB 简写。",
  size: "密度档位只能是 8、16、32、64、128。实际输出尺寸会按原图比例和 256 基准计算。",
  limits: "图片超过了本页的限制。请先缩小图片再重新选择文件。",
  input_image: "这个文件不是可识别的图片，或者内容已损坏。",
  upstream_http: "端点拒绝了请求。401/403 通常是 key 不对或没有出图权限；" +
    "400 且提到 responseModalities / imageConfig 时，把「高级」里的响应模态或输出尺寸清空再试。",
  upstream_transport: "连不上端点。浏览器只会给出一个笼统的网络错误，常见原因有三个：" +
    "①端点没有为本页来源发 CORS 头（自定义网关最常见）；②这台机器访问不到该地址；③代理拦截。" +
    "官方端点已确认对本页可用，自定义网关需要它自己发 Access-Control-Allow-Origin。",
  upstream_timeout: "超过设定的墙钟时限仍未返回。上游请求可能仍在生成、也可能仍在计费——" +
    "超时只是本页放弃等待，不代表上游已经停止。",
  upstream_non_json: "端点返回了非 JSON（通常是 HTML 错误页）。检查「高级」里的端点地址是否指对了。",
  upstream_protocol: "端点返回的 JSON 结构不是对象，可能这个地址不是 Gemini 协议端点。",
  upstream_error_field: "端点在 HTTP 200 里返回了 error 对象，通常是配额或权限问题。",
  no_image: "端点没有返回图片。确认模型支持出图，并且响应模态里含 IMAGE（默认就是 TEXT,IMAGE）。" +
    "完整响应可用下方按钮下载。",
  palette_violation: "这是内部一致性断言失败，说明降采样或量化出现了回归。像素本应严格来自调色板。",
  invariant: "内部一致性断言失败。",
  internal: "这是页面自身的缺陷，不是端点问题。",
  frontend: "页面自身的异常，不是端点返回。"
};
