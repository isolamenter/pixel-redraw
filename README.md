# pixel-redraw MVP

以 Gemini 原生 `generateContent` 协议调用支持图像输入和图像输出的模型，再在本地用 Pillow 将结果固定为真实像素画。默认走 Google 官方端点，填一个官方 key 就能跑；任何讲同一协议的网关，把 `GEMINI_BASE_URL` 指过去也一样能用。

## 使用

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，至少填写 GEMINI_API_KEY、GEMINI_MODEL（官方 key 不用配端点）
python3 pixel_redraw.py input.png
```

`.env` 支持引用已有环境变量，例如 `GEMINI_API_KEY=${GEMINI_API_KEY}`；程序不会把 Key 的实际值写回文件。

输出在 `output/`：

- `*.pixel.png`：逻辑尺寸的真像素图；
- `*.pixel_x8.png`：nearest-neighbor 放大预览；
- `*.ai.png`：模型原始结果；
- `*.report.json`：尺寸和颜色检查结果。

没有配置 API 时，可以先验证本地像素化流程：

```bash
python3 pixel_redraw.py input.png --pixelize-only --size 64x64
```

## 上游约定

请求为 Gemini 原生协议，官方端点和同协议的网关走的是同一套：

```
POST {GEMINI_BASE_URL}/{GEMINI_API_VERSION}/models/{GEMINI_MODEL}:generateContent
x-goog-api-key: <API key>
```

- `GEMINI_BASE_URL` **不填就走官方端点** `https://generativelanguage.googleapis.com`，这种情况下只填 `GEMINI_API_KEY` 和 `GEMINI_MODEL` 就够。填网关时写网关根地址（如 `http://127.0.0.1:3000`），**不要**带 `/v1beta`；版本由 `GEMINI_API_VERSION` 控制，默认 `v1beta`。
- 官方 key 可以直接用，但 Google 现在会拦截「无限制」的 key：在 AI Studio 里把它限制为仅用于 Gemini API，否则可能直接收到 403。
- 请求体使用 `contents[].parts[]`，图片以 `inline_data`（`mime_type` + base64 `data`）传入，并设置 `generationConfig.responseModalities` 为 `TEXT,IMAGE` 以获得图片输出。
- 响应解析兼容 `candidates[].content.parts[].inlineData`（camelCase，上游原生）与 `inline_data`（snake_case）两种写法，也兼容部分网关返回的 data URI / 图片 URL。
- 若模型支持指定出图分辨率，可设置 `GEMINI_IMAGE_SIZE`；官方模型接受 512/1K/2K/4K，但各模型支持的范围不同，问了不支持的尺寸是硬报错。不设则用模型默认值。
- **变量名只有 `GEMINI_*`**。Google 自家 SDK 的 `GOOGLE_API_KEY` / `GOOGLE_GEMINI_BASE_URL` 会在 `.env`（和环境）里完全没有 `GEMINI_API_KEY` / `GEMINI_BASE_URL` 时才被借用——注意这个兜底：如果你 shell 里常驻一个给别的 Google 工具用的 `GOOGLE_API_KEY`，而 `.env` 又没写 `GEMINI_API_KEY`，它就会生效。写一个空值（`GEMINI_API_KEY=`）就算明确表态，不会再去借用。
- key 和端点是不是配套，程序不猜。两边都是自定义的字符串，名字推不出签发方，所以**配错了不会有人提醒你**：请求会以 401/403 失败，响应里通常也不会说原因。

程序不会把 API key 写入输出文件或日志。远端图片 URL 下载时也不会附带 API key。

---

## Web 端（localhost）

```bash
python3 pixel_web.py            # 默认 http://127.0.0.1:8770/
python3 pixel_web.py --port 9000
```

浏览器打开上面打印的地址即可。没有新增依赖，`requirements.txt` 仍然只有 Pillow。

### 必须用 127.0.0.1 或 localhost 访问

不要用局域网 IP 访问。两个原因：

1. **剪贴板会失效**。`navigator.clipboard` 只在安全上下文（secure context）可用，`localhost` 和 `127.0.0.1` 算，裸 IP 不算——页面会直接变成 `navigator.clipboard === undefined`。
2. **这个进程持有 API key**。服务端也硬编码只绑定回环地址并校验 `Host` 头；改绑 `0.0.0.0` 会让你的 key 暴露在网络上。

### 功能

- **上传 / 粘贴 / 拖拽**三选一，都会先中心裁剪成正方形再发送（四个尺寸都是正方形，模型也只会收到 `1:1`）。
- **输出尺寸**：8×8 / 16×16 / 32×32 / 64×64。
- **调色板**：9 个内置预设（4/8/16/32/64 色），或自己填 hex，或 `auto`。**选了固定调色板后输出像素严格只从该调色板取色**——这一条有断言保护，不是尽力而为。
- **改尺寸 / 换调色板不重新调用模型**：跑完一次后，会直接对缓存的模型输出重新像素化（约 0.15 秒，零 token）。
- **复制 / 下载**：复制的是放大后的预览图（直接把 64×64 粘进 Slack/Discord 会被对方平滑重采样，硬边就没了）；要原始 1:1 像素请用下载。
- **仅本地渲染**勾选框跳过模型调用，零花费验证调色板和尺寸。
- **报错全部原样显示在前端**：分类徽章 + 原文 message + 上游响应体 + traceback + 完整 request/response JSON + 一键复制复现命令。
  失败时 `request.json` / `response.json` 同样会生成——上游的完整响应体从异常里捞回来，这是排查网关拒绝的唯一证据。复现命令也带真实的调色板和尺寸，可以直接粘进 shell 跑出同一个错误。

### key 和 model 只能在 .env 里配置

页面不提供输入框，任何路由都不会读取请求里的 key/model，`/api/meta` 只回 `upstream_configured: true/false` 和上游主机名（不回完整 URL，因为 URL 可能带 userinfo）。

需求「报错原样显示」和「key 不离开服务端」是冲突的——网关完全可以在 401 响应体里把请求头回显出来。处理方式是 `redact()` 单一收口：**报错原样，但密钥除外**，且前端会标注这一点。这一条由测试保障，不是靠承诺：测试会对每一种失败各驱动一次，然后断言 key 的原文、base64 形式和 `AIza`/`sk-` 形状都没有出现在任何响应体、响应头或日志行里。

已知残余风险：脱敏基于模式匹配，一个形状特殊的 key、或者网关把 key base64 后再分块返回，可能绕过。测试也覆盖不到这种情况。

### 产物与目录

每次运行写 `runs/<run_id>/`：

| 文件 | 内容 |
|---|---|
| `<run_id>.pixel.png` | 真像素画，逻辑尺寸 |
| `<run_id>.pixel_x{N}.png` | NEAREST 放大预览 |
| `<run_id>.ai.png` | 模型原始输出（也是 repixelize 的数据来源） |
| `<run_id>.report.json` | 尺寸 / 色数 / palette |

`runs/` 已在 `.gitignore` 里，且**没有保留策略**——和 CLI 的 `output/` 一样会一直累积。删掉旧目录是安全的。

### 已知限制

- **进程内单任务**，第二次请求返回 409。重启服务端会丢掉运行记录（图片文件仍在 `runs/` 里）。
- **没有真正的取消**。底层的 `urllib` 阻塞读没有中断接口，所以「停止查看」只停止前端观察，上游请求仍在跑、可能仍在计费。按钮文案已经写明这一点。
- **超时不是墙钟**。`GEMINI_TIMEOUT` 是逐 socket 超时，不是总时限，所以前端不会、也不应该根据「已等待超过 180 秒」判定失败。

---

## 测试

```bash
python3 test/test_upstream_config.py  # 21 项：变量名解析 / 借用别名 / 默认端点
python3 test/test_web.py              # 34 项：调色板子集 / 密钥不泄漏 / 校验 / 本地管线 / 终止帧形状
python3 test/test_cli_unchanged.py    # CLI 回归：32/64 与修改前逐位相同
PIXEL_WEB_LIVE_TEST=1 python3 test/test_web.py EndToEnd   # 花一次真实模型调用
```
