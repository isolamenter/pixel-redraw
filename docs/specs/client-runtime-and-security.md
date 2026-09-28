# Spec: 客户端运行时与 Gemini 代理安全规范

## 1. 目标与范围

浏览器继续使用 Web Worker + Pyodide WASM 执行图像处理；Cloudflare Pages Function 仅持有 Gemini API Key 并代理模型请求。

对应实现：
- `static/js/worker.js`：Pyodide Web Worker 宿主。
- `static/js/pipeline.js`：前端 Worker 通信与任务调度。
- `pixel_pipeline.py`：Pyodide 适配、同源 API 请求、脱敏错误信封。
- `functions/api/config.js`：返回公开模型名和 Key 是否已配置，不返回密钥。
- `functions/api/generate.js`：将 Gemini 原生 JSON 请求转发到固定官方主机。

## 2. 运行时与数据流

```text
用户浏览器
  Main Thread UI
      │ 图片与生成选项
      ▼
  Web Worker: Pyodide + Pillow + NumPy
      │ 本地像素处理；模型请求 POST /api/generate
      ▼
Cloudflare Pages Function
      │ 添加 x-goog-api-key，不持久化请求，不写请求日志
      ▼
Google Gemini 官方 API
```

- 图像像素化、调色板映射、网格对齐和拓扑清理仅在浏览器运行。
- 上传图片与提示词会经过 Pages Function 并发送至 Google Gemini。
- `GET /api/config` 仅返回 `model` 与 `ready` 两个公开字段。
- `POST /api/generate` 只转发到 `generativelanguage.googleapis.com` 的 `v1beta/models/{model}:generateContent`；模型由服务端环境变量选择，浏览器不能指定上游主机或 API Key。
- Function 校验 JSON 类型；若请求提供 `Content-Length`，拒绝超过 32 MiB 的请求；存在 `Origin` 时要求与本站匹配。Cloudflare 网络层还会执行账户计划规定的请求体上限。接口不提供自定义端点或跨域 CORS。

## 3. 凭据与安全

- 本地开发使用仓库根目录 `.dev.vars`；样例为 `.dev.vars.example`。实际 `.dev.vars` 必须保持 Git 忽略。
- Pages 生产环境使用加密 Secret `GEMINI_API_KEY`；可选普通变量 `GEMINI_MODEL`，默认 `gemini-3.1-flash-lite-image`。
- API Key 仅由 Pages Function 在请求期间读取并写入发往 Gemini 的 `x-goog-api-key` 请求头。不得返回给客户端、写入构建产物、写入日志或导出文件。
- 所有访客共用服务端 Key；每轮生成会使用该 Key 所属项目的配额。当前没有用户身份或每用户额度隔离。
- 图片及提示词会离开浏览器并经本站 Function 中转至 Google；Function 不保存它们。
- Python 侧 `to_envelope()` 与 UI 侧 `redactSecrets()` 继续过滤错误内容中的密钥样式文本。浏览器并不持有真实 API Key。

## 4. Gemini 请求协议

- 官方 URL：`https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`
- Function 请求头：`Content-Type: application/json`、`x-goog-api-key: <GEMINI_API_KEY>`。
- 请求体由 `pixel_redraw.py` 组装，图像使用 `contents[].parts[].inline_data`，生成模态为 `TEXT` 与 `IMAGE`。
- 浏览器只访问同源 `/api/generate`，不直连 Gemini、不处理 CORS，也不发送密钥。

## 5. 构建与本地运行

- Pages 输出目录由 `tools/build-pages.mjs` 生成；Cloudflare 构建前需通过 `tools/fetch-dist.mjs` 准备 Pyodide、Pillow 与 NumPy。
- Wrangler Pages 本地开发读取 `.dev.vars`，命令为 `npx wrangler pages dev dist`。
- `node tools/serve.mjs` 只用于静态 UI 与本地像素化预览，不包含 Gemini Function。
- Pages 静态文件不应包含 `.dev.vars`、`.env` 或任何真实凭据。
