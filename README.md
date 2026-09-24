# pixel-redraw

以 Gemini 原生 `generateContent` 协议调用多模态大模型，并在本地通过 Pillow 与 NumPy 将生成结果高精度量化为受限调色板的真实像素画。

**这是一个纯静态站点，没有后端进程。** 浏览器直接调用用户填写的模型端点，并在本地 Web Worker 中使用内置打包的 Pyodide（WASM 版 CPython + Pillow + NumPy）完成像素化与拓扑清理，**无需从外部 CDN 下载运行时**。部署只需要一个分发静态文件的 Nginx 容器。

---

## 目录

- [功能特性](#功能特性)
- [技术栈与环境要求](#技术栈与环境要求)
- [快速开始](#快速开始)
- [本地开发](#本地开发)
- [测试与验证](#测试与验证)
- [项目结构](#项目结构)
- [文档入口](#文档入口)
- [凭据与网络安全](#凭据与网络安全)
- [离线部署](#离线部署)
- [已知限制](#已知限制)

---

## 功能特性

- **多模态输入**：支持上传、剪贴板粘贴与拖拽，自动保持原图宽高比，以 256×256 参考画布计算目标逻辑像素网格。
- **像素密度档位**：支持 8 / 16 / 32 / 64 / 128 档位（以 256×256 参考画布为基准按比例计算输出尺寸）。
- **默认双轮生成 (Dual-Pass) 与参数解耦**：
  - 第一轮（语义构图）：固定采用 `Auto` 调色板与根据原图分辨率 5 档自适应密度（8/16/32/64/128），确保大模型在充足色彩与网格特征下准确构图，杜绝首轮结构塌陷。
  - 第二轮（风格精修）：将初稿转换为用户选择的目标调色板与密度，输入大模型专注进行点阵精修与边缘梳理。
  - 容错机制：第二轮异常时自动平滑回退至按用户参数降采样的第一轮草稿，不中断渲染。
- **QVote 区域量化与拓扑清理**：
  - 网格相位对齐（Grid Phase Alignment）：微移搜索消除 AI 边缘抗锯齿与模糊。
  - QVote (Quantize-then-Vote)：先在 Oklab 空间量化再按区域众数投票，杜绝 RGB 均值导致的过渡混色伪影。
  - 拓扑清理：自适应消除低置信度孤立像素与单像素孔洞，同时保护高置信度眼睛/高光等关键细节。
- **色彩与调色板控制**：
  - 15 款硬件与复古艺术预设调色板（含 PICO-8、GameBoy、C64、MSX、Endesga 等）。
  - 支持自定义 HEX 调色板输入，固定调色板输出受严格子集不变量保护。
  - 自动调色板：基于 Oklab 感知色彩空间执行确定性 K-Means 聚类。
- **零 Token 本地重采样 (Repixelize)**：调整像素密度或切换调色板时，直接复用模型原始缓存重新量化，秒级呈现且不消耗 API Token。
- **三卡片管线可视化呈现**：直观对比「自适应参考 Guide（Pass 1 模型基准）」、「AI 原始输出（未过格式层）」与「最终像素图（受限色板+点阵精修）」，并完整呈现分阶段进度（Stepper）、耗时与脱敏后的诊断报错信息。

---

## 技术栈与环境要求

- **核心算法**：Python 3.9+ / CPython 3.14 (Pyodide WASM)、Pillow、NumPy
- **前端架构**：原生 ES Module JavaScript (Vanilla JS, 无构建打包)、Web Worker、CSS3
- **部署环境**：Docker / Nginx:alpine
- **开发工具**：Node.js 18+ (用于本地静态服务与模块链接检查)

---

## 快速开始

### 方式一：Docker 运行（推荐）

```bash
docker compose up -d --build     # 访问 http://localhost:8080/
```

然后在页面设置中填入 **模型名**（如 `gemini-3.1-flash-image` 或 `gemini-3-pro-image`）和 Google AI Studio 获取的 **API Key** 即可使用。

> 💡 **零成本体验**：勾选「仅本地渲染」，拖入任意图片即可完全不消耗 Token 体验本地像素化管线。

---

## 本地开发

1. **环境准备**：
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt -r requirements-dev.txt
   ```

2. **启动开发服务器**：
   ```bash
   node tools/serve.mjs          # 访问 http://127.0.0.1:8137/static/index.html
   ```
   > ⚠️ **注意**：`tools/serve.mjs` 服务于仓库根目录并配置了 `Cache-Control: no-store`，确保修改即时生效。**请勿使用 `python3 -m http.server`**，其缺少防缓存头可能导致浏览器加载旧模块。

---

## 测试与验证

```bash
# 1. 运行核心算法单元测试（纯计算、无网络、0 API 消耗）
.venv/bin/python3 -m unittest discover -s tests -v

# 2. 静态检查前端 ES 模块 import/export 与 DOM 绑定
node tools/linkcheck.mjs

# 3. 运行浏览器端到端烟测（需先启动 serve.mjs）
node tools/browser-smoke.mjs
```

---

## 项目结构

```text
├── pixel_redraw.py         # 核心编排：Gemini 协议生成、响应提取、两轮流程、不变量断言
├── pixel_reduce.py         # 核心算法：目标网格、网格相位对齐、QVote 区域表决、拓扑清理
├── pixel_color.py          # 核心算法：Oklab 感知色彩空间转换、K-Means 聚类、最近色匹配
├── pixel_palettes.py       # 数据与校验：15 款内置预设调色板与自检逻辑
├── pixel_pipeline.py       # 浏览器边界：唯一适配 Pyodide 的模块，负责 pyfetch 传输与脱敏信封
├── static/                 # 纯静态前端
│   ├── index.html          # 单页应用入口
│   ├── app.css             # 样式定义
│   └── js/                 # 原生 ES 模块（app.js, pipeline.js, worker.js, settings.js 等）
├── tests/                  # 单元测试（test_core.py, test_refactor.py）
├── tools/                  # 开发与测试工具（serve.mjs, linkcheck.mjs, browser-smoke.mjs 等）
├── deploy/                 # 生产部署配置（nginx.conf）
├── docs/                   # 详细技术规范、架构决策与计划
│   ├── specs/              # 功能规范
│   ├── decisions/          # 架构决策记录 (ADR)
│   └── plans/              # 复杂任务计划目录
├── ARCHITECTURE.md         # 系统架构设计
└── AGENTS.md               # 面向 AI Agent 的规范与工作流指南
```

---

## 文档入口

- [系统架构概览](file:///Users/akiya/Project/pixel/ARCHITECTURE.md)：深入了解分层设计、模块依赖、关键数据流与纯计算边界。
- [功能规范 (Specs)](file:///Users/akiya/Project/pixel/docs/specs/)：
  - [像素化与降采样管线规范](file:///Users/akiya/Project/pixel/docs/specs/pixel-reduction-pipeline.md)
  - [客户端运行时与安全规范](file:///Users/akiya/Project/pixel/docs/specs/client-runtime-and-security.md)
- [架构决策记录 (ADR)](file:///Users/akiya/Project/pixel/docs/decisions/)：了解技术选型依据与演进历史。
- [AI Agent 指南](file:///Users/akiya/Project/pixel/AGENTS.md)：AI Agent 协作开发的规范与执行工作流。

---

## 凭据与网络安全

1. **凭据仅存浏览器**：API Key 仅存放于用户的 `sessionStorage`（勾选后持久化于 `localStorage`），由浏览器直连请求模型端点，绝不上报任何中转服务器。
2. **双重脱敏保障**：Python 边界信封包装（`to_envelope()`）与前端渲染（`redactSecrets()`）两道防线，确保诊断报告、界面提示与导出的 JSON 中绝不暴露明文 Key。
3. **网络与 CORS 要求**：
   - 官方端点（`generativelanguage.googleapis.com`）已默认放行浏览器跨域。
   - 若使用自定义代理网关，该网关必须自行配置 CORS 响应头，允许跨域及 `x-goog-api-key` 请求头。

---

## 离线打包与部署

应用默认已内置完整的 Pyodide WASM 运行时（位于 `static/pyodide/`，包含 CPython 解释器、标准库、Pillow 与 NumPy），无需任何外部 CDN 依赖。

若需重新拉取或升级本地运行时：

```bash
cd tools && npm install && npm run dist      # 更新 static/pyodide/ (约 17MB)
```

---

## 已知限制

- **模型调用仍需网络**：离线打包彻底解决了静态资源与算法运行时的加载问题；但若使用 Gemini 生成功能，用户的浏览器仍需能够访问上游 API 端点。
- **无法真正撤回进行中的上游请求**：点击「终止」会销毁 Web Worker 以打断本地 WASM 计算，但已发出的 HTTP 请求仍在服务端处理。
- **无服务端持久化**：应用不包含数据库，刷新页面重置所有执行状态与历史，需通过导出按钮手动保存结果。
