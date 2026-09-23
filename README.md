# pixel-redraw

以 Gemini 原生 `generateContent` 协议调用支持图像输入和图像输出的模型，再在本地用 Pillow
把结果固定为真实像素画。

**这是一个纯静态站点，没有后端。** 浏览器直接调用你填的模型端点，并用 Pyodide
（WASM 版 CPython）+ Pillow 在本地完成像素化 —— 跑的就是仓库里这份 `pixel_redraw.py`。
部署只需要一个发文件的 nginx 容器。

## 目录

- [快速开始](#快速开始)
- [网络前提（先读这一条）](#网络前提先读这一条)
- [功能](#功能)
- [凭据与安全](#凭据与安全)
- [上游约定](#上游约定)
- [架构](#架构)
- [离线部署](#离线部署)
- [本地开发](#本地开发)
- [测试](#测试)
- [已知限制](#已知限制)

## 快速开始

```bash
docker compose up -d --build     # 打开 http://<这台机器>:8080/
```

然后在页面上填两件事：**模型名**和 **API key**。官方端点用
`gemini-3.1-flash-image`（或 `gemini-3.1-flash-lite-image` / `gemini-3-pro-image`），
key 用 AI Studio 的 key，其余不用配。

**不想花 token 先看看效果**：勾上「仅本地渲染」，拖一张图进去 —— 那条路径完全不调用模型，
零成本，而且不需要 key。

## 网络前提（先读这一条）

这个架构把模型调用放在了浏览器里，所以**每个使用者的浏览器都必须能直接访问模型端点**，
不只是服务器能访问：

- 内网只有服务器能出网 → **这个方案不成立**，没有代理也没有服务端兜底。
- 需要 HTTPS 拦截/解密的企业代理 → 可能因为证书校验失败而连不上。
- 自定义网关 → 那个网关必须自己发 CORS 头（`Access-Control-Allow-Origin`，并允许
  `x-goog-api-key` 请求头）。这不是本页能控制的，**配错了不会有人提醒你**，浏览器只会给
  一个笼统的网络错误。官方端点已确认对任意来源放行。

另外，页面从 jsDelivr 加载 Pyodide（首次约 7.5MB），浏览器也访问不到公共 CDN 时请见
[离线部署](#离线部署)。

## 功能

- **上传 / 粘贴 / 拖拽**三选一，保留原图比例，不裁剪；按 256×256 参考画布计算实际像素网格。
- **像素密度档位**：8×8 / 16×16 / 32×32 / 64×64 / 128×128 / 256×256 / 512×512（256 基准，实际输出尺寸随原图比例变化）。
- **默认两轮生成**：第一轮出初稿，本地量化后再让模型整理轮廓与色块，第二轮返回后再做最终量化。
  可在「高级」里切回单轮（省一半 token）。第二轮失败会沿用首轮草稿，报告里 `refinement_applied` 会标明。
- **自动色数**：8 / 12 / 16 / 24 / 32 / 48 / 64 色。
- **调色板**：15 个内置预设（4/8/16/32/46/64 色，含像素艺术与 CGA/MSX 硬件色板），
  或自己填 hex，或 `auto`。**选了固定调色板后输出像素严格只从该调色板取色**——这一条有断言保护。
- **改尺寸 / 换调色板不重新调用模型**：对缓存的模型输出重新量化，0 token。
- **显示中间结果**：第二轮开始前会展示第一轮 AI 输出经 Pillow 量化后的逻辑像素图。
- **报错全部原样显示**：分类徽章 + 原文 message + 上游响应体 + traceback + 可下载的
  request/response JSON，密钥已脱敏。

## 凭据与安全

**API key 只存在你自己的浏览器里**，由页面直接发给**你填的那个端点**，不经过任何服务器
（本来也没有服务器）。

| | |
|---|---|
| 默认存放 | `sessionStorage`：关掉标签页即失效 |
| 勾选「在此设备记住」 | 写入这个浏览器的 `localStorage`，下次自动填上 |
| 清除 | 「清除」按钮同时清掉两处 |
| 脱敏 | Python 侧 `to_envelope()` 统一 redact 一次，页面渲染前再兜一次（key 现在就活在页面里） |
| 不会发生 | 写日志、写进下载产物、发给除你填写的端点以外的任何地方 |

**共用一台机器时注意**：勾了「在此设备记住」，下一个用这台机器的人也会继承这把 key。

**HTTP + 裸 IP 访问时页面不是安全上下文**，浏览器会禁用这些能力：

- `navigator.clipboard` —— 复制按钮会降级为「请手动选中」。页面**有意不实现**
  `execCommand('copy')` 回退：实测它会返回 true，却只在剪贴板里留下 text/html。
- Service Worker —— 所以没有离线缓存，资产缓存交给 nginx 与浏览器。
- `crypto.randomUUID()` —— 代码里没有用它。

`<a download>` + Blob 下载在 HTTP 下正常工作，**「下载」是保证可用的路径**。

页面**没有鉴权**，靠内网隔离：任何能访问到它的人都能打开并使用自己的 key。因为 key 是各人自带的，
风险落在「谁都能打开」而不是「谁都能花你的钱」。要收紧就自己在前面的反向代理上加一层认证。

## 上游约定

请求为 Gemini 原生协议：

```
POST {base_url}/{api_version}/models/{model}:generateContent
x-goog-api-key: <API key>
```

- `base_url` 留空即官方端点 `https://generativelanguage.googleapis.com`，**不要**带 `/v1beta`，
  版本由「协议版本」控制（默认 `v1beta`）。
- 官方 key 现在会被 Google 拦截「无限制」的 key：在 AI Studio 里把它限制为仅用于 Gemini API。
- 请求体用 `contents[].parts[]`，图片以 `inline_data`（`mime_type` + base64 `data`）传入，
  `generationConfig.responseModalities` 为 `TEXT,IMAGE` 以获得图片输出。
- 响应解析兼容 `inlineData` / `inline_data` 两种写法，也兼容 data URI。
  **模型回复里如果是图片 URL 则不再自动下载**：浏览器跨源取图会被 CORS 挡掉，
  页面会把该 URL 作为失败原因显示出来。
- 官方模型接受 `imageSize` 512/1K/2K/4K，各模型支持范围不同，问了不支持的尺寸是硬报错。

## 架构

```
浏览器（每个用户一个，互不共享）
├── index.html + app.css + js/*.js          UI、设置、渲染
├── js/worker.js  (module Worker)
│   ├── Pyodide v314.0.7  ← jsDelivr CDN
│   │   ├── CPython 3.14.2 + Pillow 12.2.0 + numpy 2.4.6
│   │   └── pixel_redraw.py / pixel_palettes.py / pixel_pipeline.py
│   │       ← 由站点当静态文件发出，读进 WASM 文件系统再 import
│   └── pyfetch ──────────────────────────► 你填的端点（默认官方）
└── sessionStorage / localStorage（只有凭据，且要用户勾选才持久化）

部署：nginx:alpine 容器，只发静态文件，无状态、无持久化、无数据库
```

| 文件 | 职责 |
|---|---|
| `pixel_redraw.py` | 核心：纯计算，不读环境变量、不开文件、不持有 socket。协议构造、响应解析、Pillow 像素化、错误分类与脱敏 |
| `pixel_palettes.py` | 15 个预设调色板数据 + 校验。**调色板顺序是结果的一部分**（平局取低位索引） |
| `pixel_pipeline.py` | 浏览器边界：`web_meta()` 与 `run_pipeline()`，用 `pyodide.http.pyfetch` 发请求。除它以外没有任何模块知道 Pyodide 的存在 |
| `static/js/*.js` | 前端十个模块，见下 |

为什么两轮编排和错误分类留在 Python 而不是搬到 JS：它们本来就是 Python，而且
`tests/test_core.py` 直接测的就是它们——注入一个假的 upstream 就能跑完两轮流程，
不需要网络也不需要浏览器。

**实测的一致性**（`tools/digest.py` 与 `tools/smoke.mjs` 对同一张固定输入取摘要）：
原生 CPython 3.9.6 + Pillow 11.3.0 与 Pyodide/wasm CPython 3.14.2 + Pillow 12.2.0
产出**逐位相同**的结果，`nearest_palette` 的 numpy 快路径也与纯 Python 参考实现逐位相同。
这是「复用 Python 核心」这个前提的实测依据，不是推测。

## 离线部署

`static/js/config.js` 里的 `PYODIDE_INDEX_URL` 是唯一的第三方依赖。改成自托管路径即可完全不碰公网：

```bash
cd tools && npm install && npm run dist      # 产出 tools/pyodide-dist/（约 17MB）
```

把 `pyodide-dist/` 放到 web 根下（例如 `/pyodide/`），然后把常量改成 `"/pyodide/"`。

**注意**：自托管只解决 CDN 依赖。模型调用仍然需要浏览器能访问端点，那一条无法本地化。

## 本地开发

```bash
node tools/serve.mjs          # http://127.0.0.1:8137/static/index.html
```

它服务仓库根目录，所以 `/pixel_redraw.py` 这类绝对路径和部署时一致；并且对自己发的文件加了
`Cache-Control: no-store`——否则浏览器会继续用旧版模块，你会去调试一份已经改过的代码。

不要用 `python3 -m http.server`：它不发 no-store，改完不刷新就能骗到你。

## 测试

```bash
.venv/bin/python3 -m unittest discover -s tests -v   # 32 项核心测试，无网络、无 API 花费
cd tools && npm test                                  # 链接检查 + Pyodide/wasm 烟测
node tools/browser-smoke.mjs                          # 真实浏览器端到端（需要先起 serve.mjs）
```

三层各管一件事：

1. **`tests/test_core.py`** —— 用注入的假 upstream 跑真实管线：两轮顺序、精修失败回退、
   调色板子集不变量、numpy 与纯 Python 逐位一致、参数边界、体积/像素上限、脱敏。
   还有一条**纯度测试**：扫描 `pixel_redraw.py` 源码，断言它没有 `os`/`Path`/`urllib`/
   `argparse`/`sys`/`mimetypes`——这个契约靠断言而不是靠自觉。
2. **`tools/linkcheck.mjs`** —— 前端拆成十个模块后，import 写错是整页白屏且报错很难看到。
   Node 在链接期就能报出来，比浏览器早得多。
3. **`tools/browser-smoke.mjs`** —— 真浏览器里跑完整流程，包括一次真实的本地像素化
   （不需要 key）。冷缓存时要下 7.5MB，慢；改到 worker、桥接或启动流程时再跑。

`tools/smoke.mjs` 需要先 `npm install && npm run dist`。

## 已知限制

- **首次加载慢**。冷缓存下要拉约 7.5MB（Pyodide 解释器 + 标准库 + Pillow + numpy），
  之后由 CDN 与浏览器缓存兜住。实测在到公网链路差的环境里可以到几分钟。
- **固定调色板 + 高密度输出是最慢的路径**。最近色映射的代价是每像素 O(调色板色数)，
  所以它走 numpy 快路径；numpy 没加载时页面会明确警告，而不是让你面对一次「卡住」。
- **没有真正的取消上游请求**。「终止」会销毁 Worker 并重载运行时（浏览器里唯一能真正打断
  同步 WASM 计算的办法），但已经发出去的那次上游调用撤不回来，**可能仍在计费**。
- **超时是墙钟**。到点后本页放弃等待，不代表上游停止。
- **没有运行记录**。刷新即重置：结果、进度、时间线都只活在当前标签页里。
  要留档请用下载。
- **调色板预设的 `weak_at` 提示**：少数调色板在 8×8 / 16×16 下颜色会不够用，页面会提醒。
