# Spec: 客户端运行时与安全规范 (Client Runtime & Security)

## 1. 目标与范围

本文档定义 `pixel-redraw` 纯客户端架构下的运行时契约、安全隔离模型、API 凭据生命周期及网络协议规范。

对应实现文件：
- [`static/js/worker.js`](file:///Users/akiya/Project/pixel/static/js/worker.js) —— Pyodide Web Worker 宿主
- [`static/js/pipeline.js`](file:///Users/akiya/Project/pixel/static/js/pipeline.js) —— 前端 Worker 通信与管线调度
- [`static/js/settings.js`](file:///Users/akiya/Project/pixel/static/js/settings.js) —— 本地凭据管理与第二道脱敏
- [`static/js/errors.js`](file:///Users/akiya/Project/pixel/static/js/errors.js) —— 错误分类呈现与脱敏导出
- [`pixel_pipeline.py`](file:///Users/akiya/Project/pixel/pixel_pipeline.py) —— Python 与 JS 边界桥接、Pyodide pyfetch 传输、第一道脱敏信封

---

## 2. 运行时架构模型

```text
+-------------------------------------------------------------------------+
| 用户浏览器 (Client-Side Only)                                            |
|                                                                         |
|  [ Main Thread UI ]                                                     |
|    - static/index.html + static/app.css                                 |
|    - settings.js (sessionStorage / localStorage API Key)                |
|    - DOM, Canvas 预览, 调色板交互                                       |
|           |                                                             |
|       postMessage (JSON 字符串传输，无 PyProxy 泄漏)                     |
|           v                                                             |
|  [ Web Worker: worker.js ]                                              |
|    - Pyodide WASM (CPython + Pillow + NumPy)                            |
|    - pixel_pipeline.py                                                  |
|         |-- run_pipeline(src_b64, req_json)                             |
|         |-- pyodide.http.pyfetch() ----------------+                    |
|         +-- to_envelope() [第一道脱敏]             |                    |
+----------------------------------------------------+--------------------+
                                                     |
                                                     | 直连请求 (携带 x-goog-api-key)
                                                     v
                                           +--------------------+
                                           | Gemini API 端点     |
                                           | (官方或自定义网关)   |
                                           +--------------------+
```

### 2.1 零后端与 Web Worker 隔离
- **零后端**：部署仅依赖 Nginx 容器提供静态文件托管，无任何后端应用服务或数据库。
- **计算隔离**：Pyodide WASM 运行在独立 Web Worker 中，密集计算不阻塞主线程 UI 渲染和用户交互。
- **跨边界数据传递**：JS 与 Python 交互统一以 JSON 文本字符串作为输入输出载体，杜绝 `PyProxy` 跨界对象泄漏风险。

---

## 3. 凭据与安全规范

### 3.1 客户端凭据生命周期
- **存储位置**：
  - 默认：存放在 `sessionStorage` 中，关闭标签页即刻销毁。
  - 用户勾选「在此设备记住」：存放在 `localStorage` 中。
  - 点击「清除」：同时清空 `sessionStorage`、`localStorage` 及内存状态。
- **去向受限**：API Key 仅在浏览器 Worker 发起 HTTP POST 请求时作为 `x-goog-api-key` 请求头发送给用户指定的 `base_url` 端点。严禁将 Key 写入任何服务端、遥测系统或外部日志。

### 3.2 双重脱敏机制 (Two-Tier Secret Redaction)
无论请求成功或失败，系统均提供双重脱敏防线，确保 API Key 不被泄漏到屏幕、截屏或导出的 JSON 文件中：

1. **第 1 层：Python 边界脱敏 (`pixel_pipeline.py` / `to_envelope()`)**
   - 构造回传信封时，递归扫描 request payload、response payload、traceback 和错误消息。
   - 对 `x-goog-api-key` 及符合 API Key 模式的字符串执行掩码替换（如 `[REDACTED_API_KEY]`）。
2. **第 2 层：前端 UI 渲染脱敏 (`static/js/settings.js` / `redactSecrets()`)**
   - 所有在页面展示、报错徽章、复制到剪贴板或提供下载的字符串，在进入 DOM 前必须经由 `redactSecrets()` 再次过滤。

---

## 4. 上游 API 协议规范

### 4.1 请求格式 (Gemini 原生协议)
- **URL**: `POST {base_url}/{api_version}/models/{model}:generateContent`
  - 默认 `base_url`: `https://generativelanguage.googleapis.com`
  - 默认 `api_version`: `v1beta`
- **Headers**:
  - `Content-Type: application/json`
  - `x-goog-api-key: <API_KEY>`
- **Body**:
  - 输入图像使用 `contents[].parts[].inline_data`（含 `mime_type` 与 base64 数据）。
  - `generationConfig.responseModalities` 设为 `["TEXT", "IMAGE"]`。

### 4.2 响应解析与边界约束
- **图像格式支持**：支持 `inlineData` 及 `inline_data` 驼峰与下划线两种字段命名，兼容 standard base64 及 data URI 形式。
- **图片 URL 拦截**：模型若仅返回外部图片 URL，系统直接拒绝并报错，避免浏览器跨域取图受到 CORS 阻断或导致不可控网络行为。
- **非图像响应处理**：若模型仅返回纯文本（如拒答、安全过滤触发），提取文本内容并作为特定错误徽章（Safety / Upstream Block）呈现。

---

## 5. 部署与环境约束

1. **MIME 类型配置**：
   - 托管服务器（如 Nginx）必须为 `.mjs` 配置 `application/javascript`，为 `.wasm` 配置 `application/wasm`。
2. **安全上下文与剪贴板**：
   - 在 HTTP + 裸 IP 访问时（非 Secure Context），浏览器会禁用 `navigator.clipboard`。页面降级提示手动选中，文件下载通过 `<a download>` + Blob 保证可用。
3. **本地内置与离线部署**：
   - 默认将 Pyodide WASM 运行时及 Pillow / NumPy wheels 内置于 `static/pyodide/`，消除对公共 CDN 的运行时依赖。可通过 `tools/fetch-dist.mjs` 重新拉取或升级本地运行时。
