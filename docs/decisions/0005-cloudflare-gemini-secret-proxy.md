# 5. 通过 Cloudflare Pages Function 使用服务端 Gemini Key

- **状态**: Accepted
- **日期**: 2026-09-28
- **决定者**: 项目维护者

## 背景

公开静态站点若把维护者的 Gemini API Key 写进 JavaScript 或构建环境，密钥会随静态资源发送给每位访客。该项目将改为由站点统一使用维护者配置的 Key，并移除页面上的端点、模型、Key、提示词和轮次输入项。

## 决策

1. 使用 Cloudflare Pages 静态资源与 Pages Functions；`/api/config` 只返回公开模型名和 Key 配置状态。
2. `POST /api/generate` 将 Gemini 原生 JSON 请求转发至固定的 `generativelanguage.googleapis.com/v1beta/models/{model}:generateContent`，从 Pages Secret `GEMINI_API_KEY` 读取密钥并添加 `x-goog-api-key`。
3. 本地开发使用 Git 忽略的 `.dev.vars`；生产密钥通过 Pages 加密 Secret 配置，不写入构建产物、页面状态、请求日志或导出文件。
4. Pyodide、Pillow、NumPy 和全部像素化算法仍在浏览器运行；图片只在请求期间经过 Pages Function 并发送给 Google Gemini，不落入本站存储。
5. 模型可由非敏感环境变量 `GEMINI_MODEL` 设置，默认 `gemini-3.1-flash-lite-image`。页面端不再允许更换模型、端点、提示词或生成轮数。

## 结果与权衡

- 浏览器不再持有维护者的 API Key，且不再直接访问 Gemini 上游。
- 所有访客共用一份 Key 与 Gemini 配额；此版本没有用户身份、每用户限额或持久化。
- 图片和提示词会经过 Cloudflare 并发送给 Google Gemini；继续在本地运行的只有图像后处理。
- Pages Function 增加一次服务端转发请求；静态预览服务器本身不提供 Gemini 生成功能。

## 后续验证

- Pages 部署应分别验证静态资源、`/api/config`、缺少 Secret 时的错误、成功 Gemini 请求及上游错误透传。
- 不应在测试或日志中输出真实 API Key。
