# Architecture Overview

`pixel-redraw` 使用 **Cloudflare Pages 静态资源 + Pages Functions**。Gemini API Key 保存在服务端 Secret 中，由 Function 代理调用官方 Gemini API；Pyodide (WASM CPython) 仍在浏览器中执行图像像素化、感知调色板映射与拓扑清理。

---

## 1. 架构分层与模块划分

系统分为四层：前端交互层、WASM 边界层、纯计算核心层与基础设施层。

```mermaid
flowchart TD
    subgraph UI ["1. 前端交互层 (Main Thread)"]
        HTML["index.html + app.css"]
        App["app.js / pipeline.js"]
        UI_Modules["settings.js / palette.js / results.js / errors.js / image.js / stepper.js / dom.js / config.js"]
    end

    subgraph Bridge ["2. WASM 边界与宿主层 (Web Worker)"]
        Worker["worker.js (Pyodide Host)"]
        PyPipeline["pixel_pipeline.py (run_pipeline / web_meta / pyfetch)"]
    end

    subgraph Core ["3. 纯计算算法核心 (CPython / WASM)"]
        Redraw["pixel_redraw.py (编排 / 提示词 / 协议 / 不变量断言)"]
        Reduce["pixel_reduce.py (网格对齐 / QVote / 拓扑清理)"]
        Color["pixel_color.py (Oklab / 最近色 / K-Means)"]
        Palettes["pixel_palettes.py (预设调色板数据 & 校验)"]
    end

    subgraph External ["外部与基础设施"]
        PagesAPI["Cloudflare Pages Functions (/api/config, /api/generate)"]
        GeminiAPI["Gemini 官方 API (generateContent)"]
        LocalPyodide["本地 WASM 运行时 (static/pyodide/)"]
    end

    App <-->|postMessage JSON String| Worker
    Worker -->|import| LocalPyodide
    Worker -->|import| PyPipeline
    PyPipeline -->|同源 POST，不带密钥| PagesAPI
    PagesAPI -->|服务端 x-goog-api-key| GeminiAPI
    PyPipeline -->|调用| Redraw
    Redraw --> Reduce
    Redraw --> Palettes
    Reduce --> Color
    Color --> numpy["NumPy & Pillow"]
```

---

## 2. 模块职责与依赖方向

| 层次 | 文件 / 模块 | 核心职责 | 依赖方向 |
|---|---|---|---|
| **前端交互层** | [`static/index.html`](file:///Users/akiya/Project/pixel/static/index.html)<br>[`static/app.css`](file:///Users/akiya/Project/pixel/static/app.css)<br>[`static/js/app.js`](file:///Users/akiya/Project/pixel/static/js/app.js) | UI 布局、状态呈现、事件驱动、模块装配与用户交互。 | 依赖各前端功能子模块 |
| | [`static/js/pipeline.js`](file:///Users/akiya/Project/pixel/static/js/pipeline.js) | 管理 Worker 生命周期、任务排队与进度分发。 | 依赖 `worker.js` 通信 |
| | [`static/js/settings.js`](file:///Users/akiya/Project/pixel/static/js/settings.js) | 读取公开模型元数据、检查服务端配置状态与渲染层脱敏；不保存 API Key。 | 无业务算法依赖 |
| | [`static/js/palette.js`](file:///Users/akiya/Project/pixel/static/js/palette.js) / [`results.js`](file:///Users/akiya/Project/pixel/static/js/results.js) / [`errors.js`](file:///Users/akiya/Project/pixel/static/js/errors.js) / [`image.js`](file:///Users/akiya/Project/pixel/static/js/image.js) | 调色板选择、图像 Canvas 渲染、错误徽章呈现及诊断导出。 | 纯 UI 辅助 |
| **WASM 边界层** | [`static/js/worker.js`](file:///Users/akiya/Project/pixel/static/js/worker.js) | 加载本地 Pyodide 运行环境（CPython 3.14+ / Pillow / NumPy）并装载 Python 脚本。 | 依赖本地 `static/pyodide/` |
| | [`pixel_pipeline.py`](file:///Users/akiya/Project/pixel/pixel_pipeline.py) | **唯一的 Pyodide 适配模块**。提供 `web_meta()` 和 `run_pipeline()` 接口，使用 `pyodide.http.pyfetch` 进行网络传输，包装脱敏错误信封。 | 依赖 `pixel_redraw.py`, `pixel_palettes.py`, `pyodide` |
| **纯计算核心层** | [`pixel_redraw.py`](file:///Users/akiya/Project/pixel/pixel_redraw.py) | **纯计算**。Gemini 请求 payload 组装、响应解析、两轮生成流程编排、调色板子集断言。 | 依赖 `pixel_reduce.py`, `pixel_palettes.py`, `pixel_color.py` |
| | [`pixel_reduce.py`](file:///Users/akiya/Project/pixel/pixel_reduce.py) | **纯计算**。目标网格计算、网格相位微移对齐 (Grid Phase Alignment)、QVote (Quantize-then-Vote) 区域多数表决量化、置信度拓扑清理。 | 依赖 `pixel_color.py`, `PIL.Image`, `numpy` |
| | [`pixel_color.py`](file:///Users/akiya/Project/pixel/pixel_color.py) | **纯计算**。`sRGB` $\leftrightarrow$ `Linear RGB` $\leftrightarrow$ `Oklab` 感知色彩转换、Oklab 最近色映射、确定性 K-Means 自动调色板生成、加权感知误差贪心剪枝子集提取 (`select_sub_palette`)。 | 依赖 `numpy` |
| | [`pixel_palettes.py`](file:///Users/akiya/Project/pixel/pixel_palettes.py) | 15 个硬件与艺术预设调色板数据及启动时自检。 | 依赖 `pixel_redraw.py` (仅用于语法自检) |
| **基础设施与工具** | [`functions/api/config.js`](file:///Users/akiya/Project/pixel/functions/api/config.js) / [`functions/api/generate.js`](file:///Users/akiya/Project/pixel/functions/api/generate.js) | 提供公开模型状态并将 Gemini 原生请求转发到固定官方主机；从运行时 Secret 读取 API Key，不记录请求内容。 | Cloudflare Pages Functions |
| | [`tools/build-pages.mjs`](file:///Users/akiya/Project/pixel/tools/build-pages.mjs) / [`wrangler.jsonc`](file:///Users/akiya/Project/pixel/wrangler.jsonc) | 整理静态资源、Python 源码及 Pages Functions 部署配置。 | Cloudflare Pages |
| | [`tools/serve.mjs`](file:///Users/akiya/Project/pixel/tools/serve.mjs) / [`tools/linkcheck.mjs`](file:///Users/akiya/Project/pixel/tools/linkcheck.mjs) | 本地开发服务器（带 `no-store` 防止缓存）及静态链接检查。 | Node.js 运行环境 |
| | [`tests/test_core.py`](file:///Users/akiya/Project/pixel/tests/test_core.py) / [`tests/test_refactor.py`](file:///Users/akiya/Project/pixel/tests/test_refactor.py) | 核心算法单元测试、纯度契约扫描、像素化不变量验证。 | CPython 3.9+ |

---

## 3. 关键数据流

### 3.1 双轮生成与像素化数据流 (Two-Pass Pipeline)

```text
[ 用户输入原图 Base64 ]
        │
        ├─► [ 本地自适应降采样 (Auto色板 + 5档自适应密度) ] ──► [ 生成自适应参考 Guide (输出至左侧卡片) ]
        │                                                                     │
        ▼ (Pass 1: 语义构图与草稿)                                             │
[ 组装 Prompt 1 (原图 + 自适应 Guide) ] ──► [ pyfetch → Pages Function → Gemini ] ──► [ 初稿高分辨率图 Raw 1 ]
                                                                                │
                                                                                ▼
                                                                [ Reducer (按用户目标色板与密度量化) ]
                                                                                │
        ┌───────────────────────────────────────────────────────────────────────┘
        ▼
[ 生成目标风格草稿 (Nearest-Neighbor 放大) ]
        │
        ▼ (Pass 2: 目标风格点阵精修)
[ 组装 Prompt 2 (目标草稿 + 原图参考) ] ──► [ pyfetch → Pages Function → Gemini ] ──► [ 精修高分辨率图 Raw 2 ]
                                                                                │
                                                        (失败时回退至首轮) ◄──────┤
                                                                                ▼
                                                                [ Reducer (按用户目标色板与密度量化) ]
                                                                                │
                                                                                ▼
                                                                [ 调色板闭包断言检查 ]
                                                                                │
                                                                                ▼
                                                                [ 最终逻辑像素图 (输出至右侧卡片) + 脱敏元数据 JSON ]
```

### 3.2 仅本地渲染 / 调色板重采样数据流 (Repixelize Flow)

当用户勾选「仅本地渲染」或在生成后调整密度/切换调色板时：
1. 前端跳过模型调用，将当前输入图或内存中缓存的原始模型输出（Raw Output）直接传入 `run_pipeline`。
2. Reducer 直接在 WASM 内运行 QVote 与色彩映射，耗时仅需数十毫秒，消耗 0 Token。

---

## 4. 架构边界与核心契约

### 4.1 纯计算契约 (Pure Compute Boundary)
`pixel_redraw.py`、`pixel_reduce.py`、`pixel_color.py` 必须保持无副作用（不读环境变量、不访问文件系统、不持有网络套接字）。浏览器管线通过注入的回调访问同源 API，Pages Function 负责服务端 Gemini 网络请求。该纯算法契约受 `tests/test_core.py` 源码扫描测试严格保护。

### 4.2 跨语言传输契约 (JSON String Barrier)
JavaScript 与 Python (Pyodide) 之间传递数据时，仅传递纯文本 JSON 字符串与 Base64 图像，严禁传递复杂的 JS/Python 包装对象（`PyProxy`），从而从根源上避免内存泄漏与垃圾回收生命周期冲突。

### 4.3 凭据安全与脱敏防线 (Security Boundary)
API Key 仅存在于 Cloudflare Pages Secret 与 Function 运行时内存中，绝不进入静态构建产物、Worker 请求、Python/WASM、日志或导出文件。Function 不持久化请求与响应；Python 侧 `to_envelope()` 与前端 `redactSecrets()` 继续过滤上游错误内容。

上传图片及模型提示词会经本站 Function 转发到 Google Gemini；像素化仍留在用户浏览器。访客共用服务端 Key，因此每次生成会消耗该 Key 所属项目的配额。
