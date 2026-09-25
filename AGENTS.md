# Agent Guidelines

本文档是面向 AI Agent 的规范指南（Canonical Instructions）。

---

## 1. 项目定位与入口

- **系统定位**：纯静态、无后端的 AI 像素画重绘工具。
- **运行时环境**：浏览器内通过 Web Worker 运行 Pyodide WASM (CPython 3.14+ / Pillow / NumPy)，直连 Google Gemini API，并在本地完成感知色彩量化与拓扑降采样。
- **核心入口**：
  - 算法核心：[`pixel_redraw.py`](file:///Users/akiya/Project/pixel/pixel_redraw.py), [`pixel_reduce.py`](file:///Users/akiya/Project/pixel/pixel_reduce.py), [`pixel_color.py`](file:///Users/akiya/Project/pixel/pixel_color.py)
  - 浏览器 WASM 边界：[`pixel_pipeline.py`](file:///Users/akiya/Project/pixel/pixel_pipeline.py), [`static/js/worker.js`](file:///Users/akiya/Project/pixel/static/js/worker.js)
  - 前端调度与 UI：[`static/js/app.js`](file:///Users/akiya/Project/pixel/static/js/app.js), [`static/js/pipeline.js`](file:///Users/akiya/Project/pixel/static/js/pipeline.js)

---

## 2. 常用命令手册

```bash
# 1. 核心算法单元测试（40+ 项测试，无网络依赖、0 API 消耗，极速）
.venv/bin/python3 -m unittest discover -s tests -v

# 2. 前端 ES 模块链接与 DOM 绑定静态检查（改动 JS import/export 后必跑）
node tools/linkcheck.mjs

# 3. Python 源码语法与字节码校验
python3 -m py_compile pixel_redraw.py pixel_reduce.py pixel_color.py pixel_palettes.py pixel_pipeline.py

# 4. 启动本地开发服务（带 no-store 禁止浏览器缓存；严禁使用 python3 -m http.server）
node tools/serve.mjs

# 5. 浏览器端到端烟测（需先起 serve.mjs）
node tools/browser-smoke.mjs
```

---

## 3. 核心约束与开发红线

1. **算法核心纯计算隔离 (Pure Compute Core)**：
   - [`pixel_redraw.py`](file:///Users/akiya/Project/pixel/pixel_redraw.py)、[`pixel_reduce.py`](file:///Users/akiya/Project/pixel/pixel_reduce.py)、[`pixel_color.py`](file:///Users/akiya/Project/pixel/pixel_color.py) 严禁 `import os, sys, pathlib, urllib, socket`，禁止读写磁盘与环境变量。
   - 所有网络请求必须通过外部注入或在 [`pixel_pipeline.py`](file:///Users/akiya/Project/pixel/pixel_pipeline.py) 中处理。该约束由 `tests/test_core.py` 静态代码扫描严格断言。
2. **禁止在前端复制代码算法**：
   - 像素缩放、网格对齐、Oklab 转换、QVote 多数表决及拓扑清理等算法**仅在 Python 中实现单份**，前端不得重复实现。
3. **凭据安全与双重脱敏**：
   - API Key 严禁写入服务端、持久化日志或导出文件中。
   - 任何由 Python 传向 JS 的数据必须经 `pixel_pipeline.py` 的 `to_envelope()` 脱敏；任何进入 DOM 展示的文本必须经 `static/js/settings.js` 的 `redactSecrets()` 二次脱敏。
4. **调色板确定性与顺序约束**：
   - [`pixel_palettes.py`](file:///Users/akiya/Project/pixel/pixel_palettes.py) 中的预设调色板颜色顺序是最近色平局仲裁的依据（平局取低位索引），严禁随意重排。固定调色板输出严格受子集不变量断言保护。
5. **版本兼容性**：
   - Python 代码必须同时在 CPython 3.9+ 与 Pyodide WASM (CPython 3.14+) 上完全兼容并保持逐位计算一致。
   - 前端代码采用原生 ES Module（ES5 语法风格），无需构建步骤，修改即生效。

---

## 4. 文档路由

- **系统架构与模块依赖**：👉 [ARCHITECTURE.md](file:///Users/akiya/Project/pixel/ARCHITECTURE.md)
- **像素化与降采样规范**：👉 [docs/specs/pixel-reduction-pipeline.md](file:///Users/akiya/Project/pixel/docs/specs/pixel-reduction-pipeline.md)
- **客户端运行时与安全规范**：👉 [docs/specs/client-runtime-and-security.md](file:///Users/akiya/Project/pixel/docs/specs/client-runtime-and-security.md)
- **架构决策记录**：👉 [docs/decisions/](file:///Users/akiya/Project/pixel/docs/decisions/)
- **复杂任务计划**：👉 [docs/plans/](file:///Users/akiya/Project/pixel/docs/plans/)

---

## 5. Agent 标准工作流

在执行任何开发或重构任务时，严格遵循六步工作流：

1. **Understand (理解需求)**：
   - 识别任务影响的边界（纯算法核心 / WASM 桥接 / 前端交互 / 测试工具）。
   - 明确输入输出及相关不变量。
2. **Inspect (检查现状)**：
   - 读取相关源码、规范文档及现存测试用例，明确调用链路与现有约束。
3. **Plan (制定计划)**：
   - 遵循「实现尽量简洁」与「默认禁止兼容代码」原则，设计最直接、无冗余抽象的改动方案。
   - 若属于复杂多阶段改动，在 `docs/plans/` 中记录阶段计划。
4. **Execute (落实修改)**：
   - 仅修改目标模块，保持风格一致，不破坏模块纯度契约。
5. **Verify (验证闭环)**：
   - 运行单元测试：`.venv/bin/python3 -m unittest discover -s tests -v`
   - 若改动前端模块，运行：`node tools/linkcheck.mjs`
   - 运行源码语法编译检查：`python3 -m py_compile ...`
6. **Update Docs (同步文档)**：
   - 若功能行为或架构边界发生演进，同步更新 `docs/specs/`、`docs/decisions/` 或 `ARCHITECTURE.md`，保持全套文档与代码实际状态完全一致。
7. **Deploy & Push (默认交付与本地部署)**：
   - 验证通过后，默认执行 Git commit 与 push 到远端仓库（`git push origin main`）。
   - 默认执行本地 Docker 重新构建与部署：`docker compose up -d --build`。
   - 验证容器状态为 healthy，使用户在 `http://localhost:8080` 即刻访问最新版本。
