# Repository Guidelines

## 项目结构与职责

这是一个**纯静态站点，没有后端进程**。浏览器直接调用用户填的模型端点，并用 Pyodide
（WASM 版 CPython）在浏览器里运行 Python 核心完成像素化。

- `pixel_redraw.py` —— 核心算法与协议。**必须是纯计算**：不读环境变量、不开文件、不持有 socket，
  因为它要在 CPython（跑测试）和 Pyodide（跑浏览器）两个运行时里原样工作。
- `pixel_palettes.py` —— 预设调色板数据与校验。**顺序是结果的一部分**（最近色平局取低位索引），
  不要为了调整输出而重排列表。
- `pixel_pipeline.py` —— 浏览器边界。唯一 import `pyodide` 的模块，负责 `web_meta()`、
  `run_pipeline()`、`pyfetch` 传输与错误信封。
- `static/index.html` + `static/app.css` + `static/js/*.js` —— 前端。JS 之间是 ES module，
  **没有构建步骤**：磁盘上的文件就是浏览器执行的文件。
- `static/js/worker.js` —— Pyodide 宿主，必须独立成文件（Worker 只能从 URL 构造）。
- `tools/` —— 三个验证脚本与本地 dev server，见「测试」。
- `Dockerfile` / `docker-compose.yml` / `deploy/nginx.conf` —— 静态站部署。

## 安装、运行与验证

```bash
python3 -m venv .venv && source .venv/bin/activate && python3 -m pip install -r requirements.txt
node tools/serve.mjs                      # http://127.0.0.1:8137/static/index.html
```

页面需要填模型名与 API key。**要验证本地流程不需要 key**：勾上「仅本地渲染」。

不要用 `python3 -m http.server` 做开发服务器：它不发 `Cache-Control: no-store`，
浏览器会继续用旧版模块，你会去调试一份已经改过的代码。

## 代码风格与命名

遵循现有 Python 风格：四空格缩进，模块、函数与变量 `snake_case`，类 `PascalCase`，
常量 `UPPER_SNAKE_CASE`，公共接口保留类型标注。JS 沿用现有的 ES5 风格写法
（`var` + `function`，与项目其余部分一致），注释用中文，与代码同语气。

**核心算法只在 `pixel_redraw.py` 里有一份，前端不得复制。** 前端要做的任何像素处理都必须
通过 `run_pipeline` 回到 Python。这条以前写作「不要在 Web 层复制算法」，意思没变。

`pixel_redraw.py` 必须保持与 Pyodide 的 CPython 3.14 和本机 3.9 都能跑
（`from __future__ import annotations` 已处理标注语法；其余按最小公倍数写）。
本机 `python3` 是 3.9.6。

仓库未配置统一格式化或 lint 命令，提交前至少 `python3 -m py_compile pixel_redraw.py pixel_palettes.py pixel_pipeline.py`
与 `node tools/linkcheck.mjs`。

## 测试

```bash
.venv/bin/python3 -m unittest discover -s tests -v   # 核心，无网络、无花费
cd tools && npm test                                  # 链接检查 + wasm 烟测
node tools/browser-smoke.mjs                          # 真浏览器端到端
```

改前端模块的 import/export 后**务必**跑 `node tools/linkcheck.mjs`：十个模块里写错一个
import 是整页白屏，而浏览器给的报错很难看到；Node 在链接期就能报出来。
改动 worker、桥接或启动流程后跑 `tools/browser-smoke.mjs`。

## 提交与 Pull Request

提交主题沿用 `pixel-redraw: <简短变更说明>` 格式，一次提交聚焦一个改动。
PR 请说明行为变化、验证命令及结果，关联相关 issue；涉及页面时附截图。
不要提交密钥、`.venv/`、`tools/node_modules/`、`tools/pyodide-dist/` 中的产物。

## 配置与安全

- **密钥只存在用户浏览器里**，由用户自己填，默认 `sessionStorage`，勾选后才写 `localStorage`。
  只允许发往用户填写的那个端点；**不要**把它写进日志、下载产物、错误信封或提交内容。
- 渲染任何来自运行时或上游的字符串之前，先过 `redactSecrets()`（`static/js/settings.js`）；
  Python 侧的 `to_envelope()` 已经脱敏一次，这是第二道。key 现在就活在页面里，
  一次漏网就会把它印在屏幕上、复制按钮里和任何一张截图里。
- 错误展示逻辑改动时检查脱敏，防止上游响应回显密钥。
- 部署默认**没有鉴权**，靠内网隔离。不要把服务暴露到公网。
- `deploy/nginx.conf` 里有两个不能删的 MIME 声明（`.mjs` 与 `.wasm`）：漏了它们浏览器会
  拒绝执行 Pyodide 的 ES module 或拒绝实例化 WASM，症状都像网络问题。
