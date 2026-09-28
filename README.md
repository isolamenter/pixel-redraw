# pixel-redraw

以 Gemini 原生 `generateContent` 协议调用多模态大模型，并在本地通过 Pillow 与 NumPy 将生成结果高精度量化为受限调色板的真实像素画。

前端静态资源由 Cloudflare Pages 分发；Gemini 请求通过 Pages Function 转发到官方端点，API Key 保存在服务端 Secret 中。像素化仍在浏览器 Web Worker 中由 Pyodide WASM（CPython + Pillow + NumPy）完成。

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
- **部署环境**：Cloudflare Pages + Pages Functions（Docker/Nginx 仅作静态预览）
- **开发工具**：Node.js 18+ (用于本地静态服务与模块链接检查)

---

## 快速开始

### 本地静态预览

```bash
docker compose up -d --build     # 访问 http://localhost:8080/
```

Docker/Nginx 只提供静态资源和本地像素处理。要调用 Gemini，请用 Wrangler 启动 Pages Function，或部署到 Cloudflare Pages。

> 💡 **零成本体验**：勾选「仅本地渲染」，拖入任意图片即可完全不消耗 Token 体验本地像素化管线。

---

## 本地开发

1. **环境准备**：
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt -r requirements-dev.txt
   ```

2. **本地静态预览**：
   ```bash
   node tools/serve.mjs          # 访问 http://127.0.0.1:8137/static/index.html
   ```
   该命令用于 UI 与本地像素化预览；Gemini 生成需要下一节的 Pages Function。

### Gemini 本地开发

```bash
cp .dev.vars.example .dev.vars  # 把 Gemini API Key 填入 GEMINI_API_KEY
npm --prefix tools install
npm --prefix tools run dist
node tools/build-pages.mjs
npx wrangler pages dev dist
```

访问 Wrangler 输出的本地地址。`.dev.vars` 已加入 `.gitignore`，不要把实际 Key 提交到仓库。

### Cloudflare Pages 部署

在 Pages 项目设置中使用以下构建配置：

- **Root directory**：仓库根目录（`functions/` 和 `wrangler.jsonc` 位于这里）
- **Build command**：`npm --prefix tools install && npm --prefix tools run dist && node tools/build-pages.mjs`
- **Build output directory**：`dist`
- 在 Pages 项目的 **Variables and Secrets** 中添加加密 Secret `GEMINI_API_KEY`；可选添加普通变量 `GEMINI_MODEL`，默认模型为 `gemini-3.1-flash-lite-image`。

部署后，前端不会收到 API Key；请求会由 `/api/generate` 转发至 `generativelanguage.googleapis.com`。

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
├── pixel_pipeline.py       # 浏览器 WASM 边界：请求同源 Gemini 转发接口并构建脱敏信封
├── functions/api/          # Cloudflare Pages Functions：公开配置与 Gemini 官方 API 转发
├── wrangler.jsonc          # Cloudflare Pages 配置
├── static/                 # 纯静态前端
│   ├── index.html          # 单页应用入口
│   ├── app.css             # 样式定义
│   └── js/                 # 原生 ES 模块（app.js, pipeline.js, worker.js, settings.js 等）
├── tests/                  # 单元测试（test_core.py, test_refactor.py）
├── tools/                  # 开发与测试工具（serve.mjs, linkcheck.mjs, browser-smoke.mjs 等）
├── deploy/                 # Nginx 静态预览配置
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

1. **服务端密钥**：`GEMINI_API_KEY` 只保存在本地 `.dev.vars` 或 Cloudflare Pages 加密 Secret 中，不进入浏览器静态资源、WASM、日志或导出文件。
2. **调用路径**：浏览器只请求同源 `/api/generate`；Pages Function 固定转发到 Gemini 官方 `v1beta` `generateContent` 端点。
3. **图片去向**：图像与提示词会经过 Pages Function 并发送给 Google Gemini；像素化运算仍在浏览器中完成。
4. **配额归属**：所有访客共用部署配置的 Gemini Key，每次生成会消耗该 Key 所属项目的配额。

---

## 离线打包与部署

Cloudflare Pages 构建会通过 `tools/fetch-dist.mjs` 获取 Pyodide WASM 运行时、Pillow 与 NumPy，并由 `tools/build-pages.mjs` 与静态资源一起打包。

若需重新拉取或升级本地运行时：

```bash
cd tools && npm install && npm run dist      # 更新 static/pyodide/ (约 17MB)
```

---

## 已知限制

- **模型调用仍需网络**：用户浏览器需要访问部署站点；Pages Function 需要能访问 Gemini 官方 API。
- **无法真正撤回进行中的上游请求**：点击「终止」会销毁 Web Worker 以打断本地 WASM 计算，但已发出的 HTTP 请求仍在服务端处理。
- **共享 Key 的公开调用**：公开站点上的访客会共用服务端 Gemini Key，站点请求量会影响该项目的配额与费用。
- **无业务数据持久化**：应用不包含数据库，刷新页面重置所有执行状态与历史，需通过导出按钮手动保存结果。
