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
  /* 未列到的 kind 一律按 err（红）渲染，并把 kind 原文照抄。 */
  invariant: "err", frontend: "info"
};

/* kind -> 可操作建议。 */
export var KIND_HINT = {
  config: "本站尚未配置 Gemini API Key，请检查服务端环境变量 GEMINI_API_KEY。",
  palette: "调色板写法：#RRGGBB 逗号分隔，也接受 #RGB 简写。",
  size: "密度档位只能是 8、16、32、64、128。实际输出尺寸会按原图比例和 256 基准计算。",
  limits: "图片超过了本页的限制。请先缩小图片再重新选择文件。",
  input_image: "这个文件不是可识别的图片，或者内容已损坏。",
  upstream_http: "Gemini 官方端点拒绝了请求。401/403 通常是服务端 key 无效或没有出图权限；" +
    "400 且提到 responseModalities / imageConfig 时，通常是模型不支持对应参数。",
  upstream_transport: "无法连接本站的 Gemini 转发接口。检查 Pages Function 部署，以及服务端到 Gemini 官方端点的网络。",
  upstream_timeout: "超过墙钟时限仍未返回。Gemini 请求可能仍在生成或计费；本页超时只会放弃等待。",
  upstream_non_json: "Gemini 官方端点返回了非 JSON 响应。",
  upstream_protocol: "Gemini 官方端点返回的 JSON 结构不是对象。",
  upstream_error_field: "Gemini 官方端点返回了 error 对象，通常与配额或权限有关。",
  no_image: "模型没有返回图片。确认配置的模型支持图像生成，并且响应包含 IMAGE。" +
    "完整响应可用下方按钮下载。",
  palette_violation: "这是内部一致性断言失败，说明降采样或量化出现了回归。像素本应严格来自调色板。",
  invariant: "内部一致性断言失败。",
  internal: "这是页面自身的缺陷，不是端点问题。",
  frontend: "页面自身的异常，不是端点返回。"
};
