# Repository Guidelines

## 项目结构与职责

`pixel_redraw.py` 实现 Gemini 请求、图片解析及 Pillow 像素化，也是命令行入口；`pixel_web.py` 提供仅限本机访问的 HTTP 接口，复用核心像素化逻辑。`pixel_palettes.py` 保存预设调色板及校验，`static/index.html` 是前端页面。依赖见 `requirements.txt`，配置示例见 `.env.example`。`output/` 和 `runs/` 是生成产物，已被 Git 忽略。

## 安装、运行与验证

先执行 `python3 -m venv .venv`、`source .venv/bin/activate` 和 `python3 -m pip install -r requirements.txt`。复制 `.env.example` 为 `.env` 并填写 `GEMINI_API_KEY`、`GEMINI_MODEL` 后，用 `python3 pixel_redraw.py input.png` 运行 CLI；`python3 pixel_web.py` 在 `http://127.0.0.1:8770/` 启动网页。无需调用模型时，用 `python3 pixel_redraw.py input.png --pixelize-only --size 64x64` 检查本地流程。

## 代码风格与命名

遵循现有 Python 风格：四空格缩进，模块、函数及变量使用 `snake_case`，类使用 `PascalCase`，常量使用 `UPPER_SNAKE_CASE`；为公共接口保留类型标注。修改像素算法时集中在 `pixel_redraw.py`，避免在 Web 层复制算法。仓库未配置统一格式化或 lint 命令，提交前至少检查语法与差异。

## 测试

README 记载了 `test/test_upstream_config.py`、`test/test_web.py` 和 `test/test_cli_unchanged.py`，但当前仓库未包含 `test/`（它被 Git 忽略）。本地若有这些脚本，按 README 中的命令运行；否则至少执行 `python3 -m py_compile pixel_redraw.py pixel_web.py pixel_palettes.py`，并用自备图片验证相关 CLI 或网页流程。真实模型端到端测试会产生 API 调用费用，仅在需要时运行。

## 提交与 Pull Request

现有提交主题使用 `pixel-redraw: <简短变更说明>` 格式；延续此前缀并让每次提交聚焦一个改动。PR 请说明行为变化、验证命令及结果，关联相关 issue；涉及页面时附截图。不要提交 `.env`、密钥、`output/` 或 `runs/` 中的产物。

## 配置与安全

密钥只从环境或 `.env` 读取，不要放进页面、请求参数、日志或提交内容。Web 服务保持绑定 `127.0.0.1`；修改错误展示逻辑时检查脱敏，防止上游响应回显密钥。
